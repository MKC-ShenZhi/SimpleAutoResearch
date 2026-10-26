from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import asyncio
import base64
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence, TypeVar
from uuid import uuid4

from PIL import Image

os.environ.setdefault("LITELLM_LOG", "ERROR")
from dotenv import load_dotenv

from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.integrations.model_profiles import ModelCatalog, ModelConfigError, load_model_catalog, resolve_proxy_env


T = TypeVar("T")
UsageCallback = Callable[["LLMUsage"], None]

# Local admission limits, not provider-specific size/token rules. The token
# allowance is a deliberately conservative estimate, never reported as usage.
MAX_VISION_BYTES = 20 * 1024 * 1024
MAX_VISION_PIXELS = 4_194_304
ESTIMATED_IMAGE_INPUT_TOKENS = 8192

# Public diagnostics contain event names only, never HTTPX trace payloads.
_TRANSPORT_PHASES = frozenset({"unobserved"} | {
    f"{operation}.{phase}" for operation in (
        "connection.connect_tcp", "connection.start_tls",
        "http11.send_request_headers", "http11.send_request_body",
        "http11.receive_response_headers", "http11.receive_response_body", "http2.send_request_headers",
        "http2.send_request_body", "http2.receive_response_headers", "http2.receive_response_body")
    for phase in ("started", "complete", "failed")
})


class LLMError(RuntimeError):
    """Raised when the LLM layer cannot satisfy a request."""


class LLMImageInputError(LLMError):
    """Local image admission failure with a safe, actionable public message."""


class LLMResponseError(LLMError):
    """A completed response is unusable, with explicit correction eligibility."""

    def __init__(self, message: str, *, raw_response: str = "",
                 correction_allowed: bool = True, finish_reason: str | None = None) -> None:
        super().__init__(message)
        # Content is for task-local diagnostics, never part of error/log text.
        self.raw_response = raw_response
        self.correction_allowed = correction_allowed
        self.finish_reason = finish_reason


class LLMStreamError(LLMError):
    """An opened response stream failed before a complete response was assembled."""


class LLMDeadlineError(LLMError):
    """Chat stream wall-clock deadline elapsed; final usage is unknown."""

    def __init__(self, message: str, *, transport_phase: str = "",
                 stream_opened: bool | None = None) -> None:
        super().__init__(message)
        self.transport_phase = transport_phase if transport_phase in _TRANSPORT_PHASES else ""
        self.stream_opened = stream_opened if type(stream_opened) is bool else None

    def public_diagnostics(self) -> dict[str, Any]:
        return {key: value for key, value in (
            ("transport_phase", self.transport_phase), ("stream_opened", self.stream_opened))
            if value is not None and value != ""}


@dataclass(frozen=True)
class LLMSettings:
    """Connection settings for an OpenAI-compatible chat provider.

    Args:
        model: Model name passed to the provider.
        api_key: API key used for authentication.
        base_url: Optional OpenAI-compatible API base URL.
        input_price_per_million: Optional input-token price used for local cost
            estimates.
        output_price_per_million: Optional output-token price used for local
            cost estimates.
        request_timeout_sec: Optional per-request provider timeout in
            seconds; finite SDK Chat streaming also has an elapsed-time
            deadline and up to two seconds of cleanup. Other transports
            retain their network timeout semantics. Direct instances may use ``None`` to
            disable it; ``from_env`` uses a documented finite default unless
            the environment explicitly disables the timeout.
        max_output_tokens: Optional client-wide default output-token budget per
            request. ``None`` disables the client-wide default; an explicit
            per-call cap supplied by a pipeline step still applies.
        context_input_tokens: Local native action-loop input working-set target,
            including tool schemas and reasoning history. Not a provider context
            limit or request parameter; total task budget still takes precedence.
        retry_attempts: Total provider attempts for transient transport/server
            failures. Includes the first request.
        connect_retry_attempts: Optional larger attempt ceiling only for typed
            failures known to precede dispatch. Does not replay uncertain usage.
        connect_timeout_sec: Optional finite positive SDK connection timeout.
            Defaults to the legacy 20-second cap for finite request timeouts;
            explicit values are capped by a finite positive request timeout.
            Named profiles only; no environment override or provider parameter.
        native_response_corrections: Strict integer 0 (default) or 1. Authorize
            one settled length-response correction in native Chat only, within
            the existing ledger and transport retry limits. Not a network retry.
        retry_base_delay_sec: Initial exponential-backoff delay.
        retry_max_delay_sec: Maximum delay between provider retries.
        transport_backend: Provider client backend. ``openai`` uses the OpenAI
            Python SDK directly; ``litellm`` keeps the old compatibility path.
        api_mode: Provider API surface. ``responses`` uses Responses API-style
            ``instructions`` and ``input``. ``chat`` uses Chat Completions-style
            ``messages``. Each mode retries its own transient failures only.
            ``auto`` explicitly tries ``responses`` and then ``chat`` for
            compatibility with gateways that expose only one surface.
        json_response_format: JSON response-format mode for ``ask_json``.
            ``off`` keeps prompt-only JSON parsing for broad provider
            compatibility. ``auto`` tries provider-native JSON mode and retries
            without it only when the provider rejects the parameter.
            ``json_object`` always sends the parameter.
        chat_token_limit_param: Chat Completions output-limit parameter.
            ``auto`` uses ``max_completion_tokens`` for newer reasoning-style
            models such as GPT-5/o-series/Codex and ``max_tokens`` otherwise.
            Override only when a provider gateway requires a specific name.
        reasoning_effort: Optional provider-specific reasoning effort forwarded
            to a compatible Chat Completions request through ``extra_body``.
            Leave empty unless the selected model/provider documents the
            option. This is a capability setting, not a provider-specific
            client branch.
        thinking_mode: Optional provider-specific ``thinking.type`` value for
            Chat Completions. Empty preserves provider defaults. Enable or
            disable only when the selected model documents support for it.
        reasoning_output_tokens: Optional fallback output cap used only when
            an active ``reasoning_effort`` is configured, thinking is not
            explicitly disabled, and neither the caller nor the
            client settings provide a cap. An explicit caller cap always wins;
            reasoning configuration never silently raises it.
        stream: Use streamed Chat Completions responses and assemble their
            content before returning. Responses API calls remain non-streamed
            because this client only normalizes Chat Completions chunks.
        http2: Opt in to the SDK's HTTP/2 transport; requires the optional
            HTTP/2 dependency and does not change the provider request body.
        proxy_env: Optional environment reference for SDK-only HTTP(S) proxy
            transport. The resolved URL is never part of settings or identity.
    """

    model: str = "gpt-4o-mini"
    api_key: str = field(default="", repr=False)
    base_url: str = ""
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    request_timeout_sec: float | None = None
    max_output_tokens: int | None = None
    context_input_tokens: int = 24000
    retry_attempts: int = 3
    connect_retry_attempts: int = 1
    retry_base_delay_sec: float = 1.0
    retry_max_delay_sec: float = 12.0
    transport_backend: str = "openai"
    api_mode: str = "responses"
    json_response_format: str = "auto"
    chat_token_limit_param: str = "auto"
    reasoning_effort: str = ""
    thinking_mode: str = ""
    reasoning_output_tokens: int | None = None
    stream: bool = False
    http2: bool = False
    proxy_env: str = ""  # Environment reference only; resolved URLs are call-local.
    native_response_corrections: int = 0
    connect_timeout_sec: float | None = None


@dataclass(frozen=True)
class LLMUsage:
    """Token usage for one LLM request.

    Args:
        model: Model used for the request.
        label: Optional caller-supplied request label.
        prompt_tokens: Input token count.
        completion_tokens: Output token count.
        total_tokens: Total token count.
        source: ``provider``, text-only ``estimated``, or visual ``unknown``.
            Unknown visual token counts and cost are None, never zero usage.
        estimated_cost_usd: Estimated USD cost when pricing is configured.
        provider_attempts: Number of lower-level provider calls used for this
            successful ``ask()`` request, including transient retries and
            explicit API-surface attempts handled within that call.
    """

    model: str
    label: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    source: str
    estimated_cost_usd: float | None = None
    provider_attempts: int = 1
    estimated_image_input_tokens: int = 0

    def to_row(self) -> dict[str, Any]:
        """Convert usage into a JSON-serializable record."""
        return {
            "model": self.model,
            "label": self.label,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "source": self.source,
            "estimated_cost_usd": self.estimated_cost_usd,
            "provider_attempts": self.provider_attempts,
            **({"estimated_image_input_tokens": self.estimated_image_input_tokens}
               if self.estimated_image_input_tokens else {}),
        }


@dataclass(frozen=True)
class LLMRequest:
    """Single prompt request for text or JSON generation.

    Args:
        system: System instruction sent to the model.
        user: User prompt sent to the model.
        label: Optional identifier used in batch error messages.
    """

    system: str
    user: str
    label: str = ""


class LLMClient:
    """Small OpenAI-compatible wrapper used by the pipeline stages.

    The wrapper keeps provider access explicit: one request in, one response
    out. Batch helpers only add bounded concurrency and preserve result order.
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        usage_callback: UsageCallback | None = None,
        budget_ledger: BudgetLedger | None = None,
        budget_session_id: str = "",
        budget_attempt_id: str = "",
        model_catalog: ModelCatalog | None = None,
        profile_name: str = "",
        purpose: str = "text",
        task_output_limit: int | None = None,
    ) -> None:
        """Create a client from validated LLM settings.

        Args:
            settings: Provider connection settings.
            usage_callback: Optional callback invoked after each successful
                request with token usage metadata.
            budget_ledger: Optional shared ledger. When supplied, each
                provider attempt reserves before transport and settles after a
                successful response.
            budget_session_id: Session identity recorded in ledger entries.
            budget_attempt_id: Attempt identity recorded in ledger entries.

        Raises:
            LLMError: If the API key is missing.
        """
        if (type(settings.native_response_corrections) is not int
                or settings.native_response_corrections not in (0, 1)):
            raise LLMError("native_response_corrections must be integer 0 or 1")
        if settings.native_response_corrections and settings.api_mode != "chat":
            raise LLMError("native_response_corrections=1 requires explicit chat API mode")
        if settings.proxy_env and settings.transport_backend != "openai":
            raise LLMError("proxy_env is supported by the OpenAI SDK transport only")
        if settings.connect_timeout_sec is not None:
            if not _finite_timeout(settings.connect_timeout_sec):
                raise LLMError("connect_timeout_sec must be a finite positive number")
            if settings.transport_backend != "openai":
                raise LLMError("connect_timeout_sec is supported by the OpenAI SDK transport only")
        if not settings.api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        if settings.http2 and settings.transport_backend != "openai":
            raise LLMError("http2 is supported by the OpenAI SDK transport only")
        if type(settings.context_input_tokens) is not int or settings.context_input_tokens <= 0:
            raise LLMError("context_input_tokens must be a positive integer")
        self.model = settings.model
        self._openai_model = settings.model.strip()
        self._provider_model = _litellm_model(settings)
        self._settings = settings
        self._model_catalog = model_catalog
        self._profile_name = profile_name
        self._purpose = purpose
        self._task_output_limit = task_output_limit
        self._usage_callback = usage_callback
        self._budget_ledger = budget_ledger
        self._budget_session_id = budget_session_id
        self._budget_attempt_id = budget_attempt_id
        self._budget_namespace = uuid4().hex[:12]
        self._budget_call_sequence = 0
        self._budget_reservation_sequence = 0
        self._usage_lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        model: str | None = None,
        *,
        api_mode: str | None = None,
        max_output_tokens: int | None = None,
        usage_callback: UsageCallback | None = None,
        budget_ledger: BudgetLedger | None = None,
        budget_session_id: str = "",
        budget_attempt_id: str = "",
        models_config: str | None = None,
        fallback_models_config: str | None = None,
        purpose: str = "text",
    ) -> "LLMClient":
        """Load provider settings from ``.env`` and environment variables.

        Args:
            model: Optional model override. When omitted, ``SIMPLE_AR_MODEL`` is
                used, falling back to ``gpt-4o-mini``.
            fallback_models_config: Previously selected catalog location, used
                only when no current explicit path/environment selection exists.
                Task owners must verify their saved binding before dispatch.
            usage_callback: Optional usage callback for token accounting.
            budget_ledger: Optional shared ledger for bounded sessions.
            budget_session_id: Session identity recorded in ledger entries.
            budget_attempt_id: Attempt identity recorded in ledger entries.

        Returns:
            Configured ``LLMClient`` instance.
        """
        # Credentials belong to the invoking project/environment, not the
        # installed package's directory discovered from the Python call stack.
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        try:
            catalog = load_model_catalog(models_config, fallback_path=fallback_models_config)
            if catalog is not None:
                name, profile = catalog.select(model, purpose=purpose)
                values = profile.text_settings()
                if api_mode is not None and api_mode != values["api_mode"]:
                    raise ModelConfigError("api_mode conflicts with the selected model profile")
                if max_output_tokens is not None:
                    values["max_output_tokens"] = max_output_tokens
                return cls(LLMSettings(**values), model_catalog=catalog, profile_name=name, purpose=purpose,
                    usage_callback=usage_callback, budget_ledger=budget_ledger,
                    budget_session_id=budget_session_id, budget_attempt_id=budget_attempt_id)
            if model and model.startswith(("profile:", "route:")):
                raise ModelConfigError("Named models require a model catalog; set SIMPLE_AR_MODELS_CONFIG")
            if purpose == "vision":
                raise ModelConfigError("Vision requires a named profile declaring vision capability")
        except ModelConfigError as exc:
            raise LLMError(str(exc)) from None
        settings = LLMSettings(
            model=model or os.environ.get("SIMPLE_AR_MODEL", "gpt-4o-mini"),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url=os.environ.get("OPENAI_BASE_URL", ""),
            input_price_per_million=_optional_float("SIMPLE_AR_INPUT_PRICE_PER_1M"),
            output_price_per_million=_optional_float("SIMPLE_AR_OUTPUT_PRICE_PER_1M"),
            request_timeout_sec=_optional_positive_float("SIMPLE_AR_LLM_TIMEOUT_SEC", default=180.0),
            max_output_tokens=max_output_tokens if max_output_tokens is not None else _optional_positive_int("SIMPLE_AR_MAX_OUTPUT_TOKENS", default=None),
            retry_attempts=_positive_int("SIMPLE_AR_LLM_RETRY_ATTEMPTS", default=3),
            retry_base_delay_sec=_positive_float("SIMPLE_AR_LLM_RETRY_BASE_DELAY_SEC", default=1.0),
            retry_max_delay_sec=_positive_float("SIMPLE_AR_LLM_RETRY_MAX_DELAY_SEC", default=12.0),
            transport_backend=_llm_transport_backend("SIMPLE_AR_LLM_BACKEND"),
            api_mode=_llm_api_mode_value(api_mode) if api_mode else _llm_api_mode("SIMPLE_AR_LLM_API"),
            json_response_format=_json_response_format_mode("SIMPLE_AR_JSON_RESPONSE_FORMAT"),
            chat_token_limit_param=_chat_token_limit_param_mode("SIMPLE_AR_CHAT_TOKEN_LIMIT_PARAM"),
            reasoning_effort=_reasoning_effort_mode("SIMPLE_AR_LLM_REASONING_EFFORT"),
            thinking_mode=_thinking_mode("SIMPLE_AR_LLM_THINKING"),
            reasoning_output_tokens=_optional_positive_int("SIMPLE_AR_LLM_REASONING_OUTPUT_TOKENS", default=None),
            stream=_boolean_env("SIMPLE_AR_LLM_STREAM", default=False),
        )
        return cls(
            settings,
            usage_callback=usage_callback,
            budget_ledger=budget_ledger,
            budget_session_id=budget_session_id,
            budget_attempt_id=budget_attempt_id,
        )

    def ask(
        self,
        system: str,
        user: str,
        *,
        label: str = "",
        max_output_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        budget_call_id: str | None = None,
        image_paths: tuple[Path, ...] = (),
    ) -> str:
        """Send one text request with optional local visual evidence.

        Args:
            system: System instruction.
            user: User prompt.
            label: Optional label used in usage records.
            max_output_tokens: Optional per-request output cap. When omitted,
                the client-wide ``SIMPLE_AR_MAX_OUTPUT_TOKENS`` setting is used.
            response_format: Optional provider-native structured-output hint
                forwarded to LiteLLM/OpenAI-compatible providers.
            budget_call_id: Optional stable logical-call identity used by the
                shared budget ledger. A local identity is generated when it
                is omitted.
            image_paths: Up to four local static PNG/JPEG/WebP files (20 MiB
                total, 4,194,304 pixels each). Requires an explicit named vision
                client and chat/responses API. Images remain inline in memory.

        Returns:
            Model output with surrounding whitespace removed.

        Raises:
            LLMError: If LiteLLM cannot complete the request.
        """
        image_urls = self._image_inputs(image_paths)
        request = self._build_request(system, user)
        if image_urls:
            if self._settings.api_mode == "chat":
                request["messages"][-1]["content"] = [{"type": "text", "text": user},
                    *({"type": "image_url", "image_url": {"url": url}} for url in image_urls)]
            else:
                request["input"][0]["content"] = [{"type": "input_text", "text": user},
                    *({"type": "input_image", "image_url": url} for url in image_urls)]
        response = self._complete_request(request, system, user, label=label,
            max_output_tokens=max_output_tokens, response_format=response_format,
            budget_call_id=budget_call_id, image_count=len(image_urls))
        output = _content_from_response(response).strip()
        if not output:
            raise LLMResponseError(_empty_response_message(response),
                correction_allowed=False, finish_reason=_finish_reason_from_response(response) or None)
        finish_reason = _finish_reason_from_response(response)
        if finish_reason in {'length', 'incomplete'}:
            # Partial text can still contain a parseable nested JSON object.
            # Reject the provider's incomplete response before parsing or a
            # consumer can mistake truncation for a correctable format defect.
            raise LLMResponseError(
                f'Model response was truncated (finish_reason={finish_reason}; '
                f'content_chars={len(output)}, output_limit={self.effective_output_limit(max_output_tokens)}); '
                'partial text was not adopted', correction_allowed=False, finish_reason=finish_reason)
        return output

    def chat_turn(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        label: str = "",
        max_output_tokens: int | None = None,
        budget_call_id: str | None = None,
    ) -> dict[str, Any]:
        """Return a native assistant message, without executing its tool requests.

        History belongs to the session caller, including tool results and provider
        reasoning fields. Do not flatten it into a prompt or silently switch APIs.
        This first migration slice supports text/tool Chat histories only; local
        visual evidence continues to use the explicit named vision client.
        """
        if self._settings.api_mode != "chat":
            raise LLMError("Native Chat turns require explicit chat API mode")
        if not isinstance(messages, list) or not messages or any(not isinstance(row, dict) for row in messages):
            raise LLMError("messages must be a nonempty list of native Chat messages")
        if not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools):
            raise LLMError("tools must be a list of native function tool schemas")
        request = self._build_request("", "")
        request["messages"] = messages
        if tools:
            request["tools"] = tools
        if _has_image_input(request):
            raise LLMError("Use the named vision client for visual evidence")
        history = json.dumps(messages, ensure_ascii=False)
        contract = json.dumps(tools, ensure_ascii=False)
        corrections = self._settings.native_response_corrections
        if corrections:
            if self._budget_ledger is None or self.effective_output_limit(max_output_tokens) is None:
                raise LLMError('Native response correction requires the original budget ledger and a finite output cap')
            budget_call_id = budget_call_id or label or self._next_budget_call_id()
        response = self._complete_request(request, contract, history, label=label,
            max_output_tokens=max_output_tokens, budget_call_id=budget_call_id,
            require_settled_response=bool(corrections))
        if corrections and _finish_reason_from_response(response) == "length":
            diagnostic = {"role": "system", "content": (
                "The previous complete response was truncated (finish_reason=length) "
                "and was not executed. Output token limit: "
                f"{self.effective_output_limit(max_output_tokens)}. "
                "Return a complete, executable operation of appropriate size within "
                "this limit, following the existing instructions and permissions.")}
            # Keep native history/tools, but never feed back the partial message,
            # reasoning or tool arguments. The caller's history is not mutated.
            request = {**request, "messages": [*messages, diagnostic]}
            response = self._complete_request(request, contract,
                json.dumps(request["messages"], ensure_ascii=False), label=label,
                max_output_tokens=max_output_tokens, budget_call_id=budget_call_id,
                require_settled_response=True)
        choices = _get_value(response, "choices")
        message = _get_value(choices[0], "message") if isinstance(choices, list) and choices else None
        if not isinstance(message, dict):
            dump = getattr(message, "model_dump", None)
            message = dump(exclude_none=True) if callable(dump) else None
        if _finish_reason_from_response(response) == "length":
            # Preserve enough non-content evidence to distinguish reasoning
            # exhaustion from a large answer/tool payload. Never log or adopt
            # the partial body. Any explicitly authorized correction is exhausted.
            observed = message if isinstance(message, dict) else {}
            usage = _usage_from_response(response)
            argument_chars = sum(len(str(_get_value(_get_value(call, 'function'), 'arguments') or ''))
                                 for call in observed.get('tool_calls') or [])
            counts = (
                f"content_chars={len(_text_from_content(observed.get('content')))}, "
                f"reasoning_chars={len(_reasoning_content_from_response(response))}, "
                f"tool_argument_chars={argument_chars}, "
                f"completion_tokens={usage[1] if usage else 'unknown'}, "
                f"output_limit={self.effective_output_limit(max_output_tokens)}")
            raise LLMResponseError("Native Chat turn was truncated (finish_reason=length; "
                                   + counts + "); do not execute partial tool arguments")
        if corrections and _finish_reason_from_response(response) not in {"stop", "tool_calls"}:
            raise LLMResponseError("Native Chat turn has an unsupported finish reason")
        if not isinstance(message, dict) or not (message.get("content") or message.get("tool_calls")):
            raise LLMResponseError(_empty_response_message(response))
        if corrections and not (message.get("tool_calls") or _text_from_content(message.get("content")).strip()):
            raise LLMResponseError(_empty_response_message(response))
        return {**message, "role": "assistant"}

    def tool_turn(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]],
                  label: str = '', budget_call_id: str | None = None) -> dict[str, Any]:
        """Read-only or domain conversations on the configured API, without executing calls.

        Chat keeps its native path. Responses retains complete output items, including
        reasoning, alongside the normalized assistant message so a subsequent tool
        result can be paired without flattening history or switching a named route.
        """
        if self._settings.api_mode == 'chat':
            return self.chat_turn(messages,tools=tools,label=label,budget_call_id=budget_call_id)
        if (not isinstance(messages,list) or not messages
                or any(not isinstance(row,dict) for row in messages)
                or not isinstance(tools,list) or any(not isinstance(tool,dict) for tool in tools)):
            raise LLMError('Native tools require message and function-schema lists')
        request = self._build_request('','')
        request.pop('instructions',None)
        request.pop('input',None)
        request.update(messages=messages,tools=tools,_native_tools=True)
        if _has_image_input(request):
            raise LLMError('Use the named vision client for visual evidence')
        response = self._complete_request(request,json.dumps(tools,ensure_ascii=False),
            json.dumps(messages,ensure_ascii=False),label=label,budget_call_id=budget_call_id)
        if _finish_reason_from_response(response) in {'length','incomplete','failed','cancelled'}:
            raise LLMResponseError('Native tool response was incomplete; no calls were executed',
                                   correction_allowed=False)
        choices = _get_value(response,'choices')
        if isinstance(choices,list) and choices:
            message = _get_value(choices[0],'message')
            dump = getattr(message,'model_dump',None)
            message = dump(exclude_none=True) if callable(dump) else message
            if isinstance(message,dict) and (message.get('content') or message.get('tool_calls')):
                return {**message,'role':'assistant'}
        items = _get_value(response,'output') or []
        items = [item.model_dump(exclude_none=True) if callable(getattr(item,'model_dump',None))
                 else item for item in items]
        if any(not isinstance(item,dict) for item in items):
            raise LLMResponseError('Native Responses output items are malformed',correction_allowed=False)
        calls = [{'id':item['call_id'],'type':'function','function':{
            'name':item['name'],'arguments':item['arguments']}}
            for item in items if item.get('type')=='function_call']
        content = _content_from_response(response).strip()
        if not (calls or content):
            raise LLMResponseError(_empty_response_message(response),correction_allowed=False)
        return {'role':'assistant','content':content or None,'tool_calls':calls,
                '_responses_output':items}

    def effective_output_limit(self, max_output_tokens: int | None = None) -> int | None:
        """Resolve the actual request limit for transport and budget planning."""
        output_cap = max_output_tokens
        if output_cap is None:
            output_cap = self._task_output_limit
        if output_cap is None:
            output_cap = self._settings.max_output_tokens
        if (output_cap is None and self._settings.reasoning_effort not in {"", "none"}
                and self._settings.thinking_mode != "disabled"):
            output_cap = self._settings.reasoning_output_tokens
        if output_cap is not None:
            try:
                output_cap = int(output_cap)
            except (TypeError, ValueError) as exc:
                raise LLMError("max_output_tokens must be a positive integer") from exc
            if output_cap < 1:
                raise LLMError("max_output_tokens must be a positive integer")
        return output_cap

    def _complete_request(
        self, request: dict[str, Any], system: str, user: str, *,
        label: str, max_output_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        budget_call_id: str | None = None, image_count: int = 0,
        require_settled_response: bool = False,
    ) -> object:
        """Shared transport, deadline and accounting for text and native turns."""
        if self._settings.base_url:
            request["api_base"] = self._settings.base_url
            request["base_url"] = self._settings.base_url
        # An explicit per-request cap is meaningful even when the client-wide
        # environment setting is unset.  The caller is allowed to bound one
        # expensive or structurally sensitive request without imposing the
        # same cap on every request made by this client.
        output_cap = self.effective_output_limit(max_output_tokens)
        if output_cap is not None:
            request["max_output_tokens"] = output_cap
        if response_format is not None:
            if self._settings.api_mode in {"responses", "auto"}:
                request["text"] = {"format": response_format}
            else:
                request["response_format"] = response_format
        if image_count and self._budget_ledger is not None and output_cap is None:
            raise LLMError("Budgeted vision requests require an explicit max_output_tokens cap")
        prompt_tokens = (estimate_tokens(system) + estimate_tokens(user)
                         + image_count * ESTIMATED_IMAGE_INPUT_TOKENS)
        logical_call_id = budget_call_id or label or self._next_budget_call_id()
        prior_reservations = ({entry.reservation_id for entry in self._budget_ledger.entries}
                              if require_settled_response and self._budget_ledger is not None else set())
        response, provider_attempts, reservation_id = self._request_with_retry(
            request,
            logical_call_id=logical_call_id,
            prompt_tokens=prompt_tokens,
            output_cap=output_cap,
            label=label,
        )

        output = _content_from_response(response).strip()
        # Tool arguments and thinking are generated output too. Include them in
        # the estimated path when the provider omits final usage metadata.
        choices = _get_value(response, "choices")
        message = _get_value(choices[0], "message") if isinstance(choices, list) and choices else None
        tool_calls = _get_value(message, "tool_calls")
        reasoning = _reasoning_content_from_response(response)
        if tool_calls:
            dump = getattr(message, "model_dump", None)
            output = json.dumps(dump() if callable(dump) else message, ensure_ascii=False)
        elif reasoning:
            output += reasoning
        elif _get_value(response,'output'):
            # Responses function arguments and reasoning are generated output too.
            items = _get_value(response,'output')
            output = json.dumps([item.model_dump(exclude_none=True)
                if callable(getattr(item,'model_dump',None)) else item for item in items],ensure_ascii=False)
        try:
            usage = self._build_usage_record(
                response,
                system,
                user,
                output,
                label=label,
                provider_attempts=provider_attempts,
                visual=bool(image_count),
                estimated_image_input_tokens=image_count * ESTIMATED_IMAGE_INPUT_TOKENS,
            )
        except Exception as exc:
            self._mark_budget_unknown(reservation_id, reason=f"usage reconciliation failed: {exc}")
            raise
        reason = ('Native response finish_reason=length; discarded without adoption or execution'
                  if require_settled_response and _finish_reason_from_response(response) == 'length' else '')
        if tool_calls and not request.get('tools'):
            reason = 'Provider returned native tool calls to a text-only request; no tools were executed'
        self._settle_budget(reservation_id, usage, **({'reason': reason} if reason else {}))
        self._notify_usage(usage)
        if require_settled_response:
            unsettled = any(entry.reservation_id not in prior_reservations
                and entry.logical_call_id == logical_call_id
                and entry.status in {"unknown", "reserved"}
                for entry in self._budget_ledger.entries)
            if unsettled:
                raise LLMResponseError("Native Chat turn has unsettled provider attempts; "
                                       "response correction/adoption stopped")
        return response

    def _image_inputs(self, paths: tuple[Path, ...]) -> tuple[str, ...]:
        if not isinstance(paths, tuple) or len(paths) > 4:
            raise LLMError("image_paths must be a tuple of at most four local Paths")
        if not paths:
            return ()
        if (self._settings.api_mode not in {"chat", "responses"}
                or self._settings.transport_backend != "openai"):
            raise LLMError("Vision requires explicit chat/responses mode and the OpenAI SDK transport")
        if self._purpose != "vision" or self._model_catalog is None:
            raise LLMError("Route explicitly to a named vision profile before supplying images")
        try:
            self._model_catalog.select(f"profile:{self._profile_name}", purpose="vision")
        except ModelConfigError as exc:
            raise LLMError(str(exc)) from None
        total, urls = 0, []
        try:
            for path in paths:
                if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
                    raise ValueError()
                with path.open("rb") as handle:
                    content = handle.read(MAX_VISION_BYTES - total + 1)
                total += len(content)
                if total > MAX_VISION_BYTES:
                    raise ValueError()
                with Image.open(BytesIO(content)) as image:
                    mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
                    if (not mime or image.width * image.height > MAX_VISION_PIXELS
                            or getattr(image, "is_animated", False)):
                        raise ValueError()
                    image.verify()
                with Image.open(BytesIO(content)) as image:
                    image.load()
                urls.append(f"data:{mime};base64," + base64.b64encode(content).decode("ascii"))
        except Exception:
            raise LLMImageInputError(
                f"Use valid static local PNG/JPEG/WebP images: total <= {MAX_VISION_BYTES} bytes, "
                f"each <= {MAX_VISION_PIXELS} pixels. Resize a review copy if needed; preserve the original.") from None
        return tuple(urls)

    def with_budget(
        self,
        budget_ledger: BudgetLedger,
        *,
        session_id: str = "",
        attempt_id: str = "",
    ) -> "LLMClient":
        """Return a client copy bound to one application's resource ledger."""

        return type(self)(
            self._settings,
            usage_callback=self._usage_callback,
            budget_ledger=budget_ledger,
            budget_session_id=session_id or self._budget_session_id,
            budget_attempt_id=attempt_id or self._budget_attempt_id,
            model_catalog=self._model_catalog, profile_name=self._profile_name,
            purpose=self._purpose,
            task_output_limit=self._task_output_limit,
        )

    def with_output_limit(self, limit: int) -> "LLMClient":
        """Clone a task operational setting without changing named connections."""
        if type(limit) is not int or limit < 1:
            raise LLMError('max_output_tokens must be a positive integer')
        return type(self)(self._settings, usage_callback=self._usage_callback,
            budget_ledger=self._budget_ledger, budget_session_id=self._budget_session_id,
            budget_attempt_id=self._budget_attempt_id, model_catalog=self._model_catalog,
            profile_name=self._profile_name, purpose=self._purpose, task_output_limit=limit)

    def connection_binding(self) -> dict[str, Any]:
        """Non-secret named configuration for the existing task snapshot.

        Key values are deliberately absent; rotating them does not invalidate a
        saved task. Pin this connection and its nested code route, not unrelated
        image profiles that may be registered while a text task is paused.
        """
        if self._model_catalog is None:
            return {}
        catalog = self._model_catalog
        code_name = (self._profile_name if self._purpose == "code"
                     else catalog.routes.get("code") or catalog.routes.get("default"))
        code = catalog.profiles.get(code_name or "")
        binding = {
            "profile": self._profile_name,
            "api_key_env": catalog.profiles[self._profile_name].api_key_env,
            "settings": {key: value for key, value in vars(self._settings).items()
                           if key not in {"api_key", "proxy_env"} and not (key == "http2" and value is False)
                           and not (key == "connect_timeout_sec" and value is None)
                           and not (key == 'native_response_corrections' and value == 0)},
            "code_route": {"profile": code_name, "connection": code.model_dump(
                mode="json", exclude={"proxy_env"} | ({"http2"} if not code.http2 else set()))} if code else None,
        }
        # An explicit writing route belongs to this task's possible consumers.
        # With no route, preserve older snapshots and the explicitly selected
        # parent connection; adding a profile alone must not reroute writing.
        writing_name = catalog.routes.get("writing")
        if writing_name:
            writing = catalog.profiles[writing_name]
            binding["writing_route"] = {"profile": writing_name, "connection": writing.model_dump(
                mode="json", exclude={"proxy_env"} | ({"http2"} if not writing.http2 else set()))}
        return binding

    @classmethod
    def for_task(
        cls, *, client: "LLMClient | None" = None,
        model: str | None = None, usage_callback: UsageCallback | None = None,
        purpose: str = "code",
    ) -> "LLMClient":
        """Use an injected session client while preserving task usage reporting."""
        if client is None:
            return cls.from_env(model=model, usage_callback=usage_callback, purpose=purpose)

        def observe(usage: LLMUsage) -> None:
            if client._usage_callback is not None:
                client._usage_callback(usage)
            if usage_callback is not None and usage_callback is not client._usage_callback:
                usage_callback(usage)

        settings = replace(client._settings, model=model or client.model)
        name = client._profile_name
        if client._model_catalog is not None:
            try:
                # Existing owners propagate the parent model. It is not a
                # command to combine that model with the code connection.
                selector = None if model == client.model else model
                if selector is None and client._purpose == purpose:
                    selector = f"profile:{client._profile_name}"
                if selector is None and purpose == "writing" and "writing" not in client._model_catalog.routes:
                    selector = f"profile:{client._profile_name}"
                name, profile = client._model_catalog.select(selector, purpose=purpose)
                settings = LLMSettings(**profile.text_settings())
            except ModelConfigError as exc:
                raise LLMError(str(exc)) from None
        elif model and model.startswith(("profile:", "route:")):
            routed = cls.from_env(model=model, purpose=purpose)
            settings, name = routed._settings, routed._profile_name
            return cls(settings, model_catalog=routed._model_catalog, profile_name=name, purpose=purpose,
                usage_callback=observe, budget_ledger=client._budget_ledger,
                budget_session_id=client._budget_session_id, budget_attempt_id=client._budget_attempt_id,
                task_output_limit=client._task_output_limit)
        return cls(
            settings,
            usage_callback=observe,
            budget_ledger=client._budget_ledger,
            budget_session_id=client._budget_session_id,
            budget_attempt_id=client._budget_attempt_id,
            model_catalog=client._model_catalog, profile_name=name,
            purpose=purpose,
            task_output_limit=client._task_output_limit,
        )

    def _build_request(self, system: str, user: str) -> dict[str, Any]:
        if self._settings.api_mode in {"responses", "auto"}:
            request = {
                "model": self._model_for_backend(),
                "instructions": system,
                "input": [{"role": "user", "content": user}],
                "api_key": self._settings.api_key,
            }
            if self._settings.request_timeout_sec is not None:
                request["timeout"] = self._settings.request_timeout_sec
        else:
            request = {
                "model": self._model_for_backend(),
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "api_key": self._settings.api_key,
            }
            if self._settings.request_timeout_sec is not None:
                request["timeout"] = self._settings.request_timeout_sec
        if self._settings.api_mode == "chat" and self._settings.reasoning_effort:
            request["extra_body"] = {"reasoning_effort": self._settings.reasoning_effort}
        return request

    def _model_for_backend(self) -> str:
        if self._settings.transport_backend == "litellm":
            return self._provider_model
        return self._openai_model

    def _request_with_retry(
        self,
        request: dict[str, Any],
        *,
        logical_call_id: str = "",
        prompt_tokens: int = 0,
        output_cap: int | None = None,
        label: str = "",
    ) -> tuple[object, int, str | None]:
        """Call the provider with bounded backoff and optional accounting."""
        if self._settings.transport_backend == "litellm":
            _load_litellm()
        try:
            proxy = resolve_proxy_env(self._settings.proxy_env)
        except ModelConfigError as exc:
            raise LLMError(str(exc)) from None
        if (self._settings.transport_backend == "openai" and self._settings.stream
                and "chat" in _api_attempt_order(self._settings.api_mode)
                and _finite_timeout(request.get("timeout"))):
            _require_no_running_loop()
        normal_attempts = max(1, int(self._settings.retry_attempts or 1))
        attempts = max(normal_attempts, int(self._settings.connect_retry_attempts or 1))
        last_error: Exception | None = None
        mode_attempts: list[tuple[str, int, Exception]] = []
        provider_attempts = 0
        visual = _has_image_input(request)
        reserved_total_tokens = prompt_tokens + (output_cap or 0)
        for api_mode in _api_attempt_order(self._settings.api_mode):
            unbounded_chat_stream = (self._settings.transport_backend == 'openai'
                and self._settings.stream and api_mode == 'chat' and (output_cap is None or visual))
            mode_request = _request_for_api_mode(
                request,
                api_mode,
                chat_token_limit_param=self._settings.chat_token_limit_param,
                reasoning_effort=self._settings.reasoning_effort,
                thinking_mode=self._settings.thinking_mode,
                stream=self._settings.stream,
            )
            attempted = 0
            for attempt in range(1, attempts + 1):
                try:
                    reservation_id = self._reserve_budget(
                        logical_call_id=logical_call_id,
                        label=label,
                        provider_attempt=provider_attempts + 1,
                        reserved_total_tokens=reserved_total_tokens,
                    )
                except LLMError as exc:
                    if last_error is not None and isinstance(exc.__cause__, BudgetError):
                        # Keep the budget stop as the cause. Chaining the raw
                        # provider error here could expose bodies/credentials.
                        exc.args = (f"{exc}; preceding provider failure after "
                                    f"{provider_attempts} physical attempt(s): "
                                    f"{_provider_failure_summary(last_error)}",)
                    raise
                attempted = attempt
                provider_attempts += 1
                try:
                    response = _call_provider(
                        self._settings.transport_backend,
                        api_mode,
                        mode_request,
                        http2=self._settings.http2,
                        **({"proxy": proxy} if proxy else {}),
                        **({"connect_timeout_sec": self._settings.connect_timeout_sec}
                           if self._settings.connect_timeout_sec is not None else {}),
                    )
                    if mode_request.get("stream") is True:
                        response = _collect_chat_stream(response)
                    return response, provider_attempts, reservation_id
                except (KeyboardInterrupt, SystemExit) as exc:
                    self._reconcile_failed_budget_attempt(
                        reservation_id, exc, reserved_total_tokens=reserved_total_tokens,
                        has_output_cap=output_cap is not None, visual=visual)
                    raise
                except Exception as exc:
                    last_error = exc
                    self._reconcile_failed_budget_attempt(
                        reservation_id,
                        exc,
                        reserved_total_tokens=reserved_total_tokens,
                        has_output_cap=output_cap is not None,
                        visual=visual,
                    )
                    # A capped text retry keeps the failed attempt's full
                    # reservation. Uncapped/visual charges cannot be bounded.
                    if isinstance(exc, LLMDeadlineError) and (output_cap is None or visual):
                        raise
                    if unbounded_chat_stream and _is_budget_consumption_unknown(exc):
                        break  # Unbounded uncertain charges cannot justify automatic replay.
                    retry_limit = (attempts if _transport_failure_phase(exc) == 'not_dispatched'
                                   else normal_attempts)
                    if attempt >= retry_limit or not _is_transient_llm_error(exc):
                        break
                    if (
                        self._settings.api_mode == "auto"
                        and api_mode == "responses"
                        and _is_response_transport_disconnect(exc)
                    ):
                        break
                    delay = _retry_delay(self._settings, attempt)
                    logging.getLogger(__name__).warning(
                        "LLM %s failed (%s); retry %s/%s in %.1fs",
                        label or api_mode, type(exc).__name__, attempt + 1, retry_limit, delay,
                    )
                    time.sleep(delay)
            if last_error is None:
                continue
            if isinstance(last_error, LLMDeadlineError):
                raise last_error  # Retry the same API only; never switch after a deadline.
            mode_attempts.append((api_mode, attempted, last_error))
            if (not _is_transient_llm_error(last_error)
                    or (unbounded_chat_stream and _is_budget_consumption_unknown(last_error))):
                break
        if (proxy or visual) and last_error is not None and _is_response_format_error(last_error):
            raise LLMError("Provider response_format is unsupported; transport details omitted") from None
        diagnostic = _provider_failure_summary(last_error) if last_error is not None else ''
        phase_detail = f"; {diagnostic}" if diagnostic else ''
        if last_error is not None and _is_timeout_error(last_error):
            if proxy or visual:
                raise LLMError(f"LLM request timed out ({diagnostic}); transport details omitted") from None
            raise LLMError(
                f"LLM request timed out after {_attempt_summary(mode_attempts)} attempt(s): {last_error}{phase_detail}"
            ) from last_error
        if proxy or visual:
            raise LLMError(f"LLM request failed ({diagnostic}); transport details omitted") from None
        raise LLMError(
            f"LLM request failed after {_attempt_summary(mode_attempts)} attempt(s): {last_error}{phase_detail}"
        ) from last_error

    def _reserve_budget(
        self,
        *,
        logical_call_id: str,
        label: str,
        provider_attempt: int,
        reserved_total_tokens: int,
    ) -> str | None:
        if self._budget_ledger is None:
            return None
        reservation_id = self._next_budget_reservation_id()
        try:
            self._budget_ledger.reserve(
                reservation_id,
                {
                    "llm_requests": 1,
                    "total_tokens": reserved_total_tokens,
                },
                session_id=self._budget_session_id,
                attempt_id=self._budget_attempt_id,
                logical_call_id=logical_call_id,
                purpose=label or "llm_request",
            )
        except BudgetError as exc:
            raise LLMError(
                f"LLM budget prevented provider attempt {provider_attempt}"
                f" for {label or logical_call_id}: {exc}"
            ) from exc
        return reservation_id

    def _reconcile_failed_budget_attempt(
        self,
        reservation_id: str | None,
        error: BaseException,
        *,
        reserved_total_tokens: int,
        has_output_cap: bool,
        visual: bool = False,
    ) -> None:
        if self._budget_ledger is None or reservation_id is None:
            return
        reason = 'provider attempt failed: ' + _provider_failure_summary(error)
        try:
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                self._budget_ledger.mark_unknown(reservation_id,
                    reason='Provider request cancelled; final usage unavailable',
                    known_actual={'llm_requests': 1},
                    retain_reservation=has_output_cap and not visual)
            elif _transport_failure_phase(error) == 'not_dispatched':
                self._budget_ledger.settle(reservation_id,
                    {'llm_requests': 1, 'total_tokens': 0}, actual_source='transport_pre_dispatch',
                    reason='Connection/pool acquisition failed before HTTP dispatch')
            elif visual and not _is_known_provider_rejection(error):
                self._budget_ledger.mark_unknown(reservation_id, reason=reason,
                    known_actual={"llm_requests": 1})
            elif _is_budget_consumption_unknown(error):
                self._budget_ledger.mark_unknown(
                    reservation_id, reason=reason,
                    known_actual={"llm_requests": 1},
                    retain_reservation=has_output_cap,
                )
            elif _is_transient_llm_error(error):
                # A retryable server/transport failure is still a physical
                # provider attempt.  No usage payload is available, so keep a
                # conservative estimate rather than silently dropping it.
                self._budget_ledger.settle(
                    reservation_id,
                    {
                        "llm_requests": 1,
                        "total_tokens": reserved_total_tokens,
                    },
                    actual_source="estimated",
                    reason=reason,
                )
            elif _is_known_provider_rejection(error):
                # Only an explicit rejection justifies releasing the reservation.
                self._budget_ledger.release(reservation_id, reason=reason)
            else:
                # An unclassified provider error is not evidence of zero usage.
                # Do not retry it blindly, but retain its uncertain charge.
                self._budget_ledger.mark_unknown(
                    reservation_id, reason=reason,
                    known_actual={"llm_requests": 1},
                    retain_reservation=has_output_cap,
                )
        except BudgetError as exc:
            # This runs inside the provider exception handler. The implicit
            # context would otherwise print that error's raw SDK cause/body.
            exc.__suppress_context__ = True
            raise LLMError(
                f"Could not reconcile failed LLM budget attempt: {exc}; "
                f"provider failure: {_provider_failure_summary(error)}"
            ) from exc

    def ask_json(
        self,
        system: str,
        user: str,
        *,
        label: str = "",
        max_output_tokens: int | None = None,
        budget_call_id: str | None = None,
        image_paths: tuple[Path, ...] = (),
    ) -> dict[str, Any]:
        """Send one request and parse the response as a JSON object.

        Args:
            system: System instruction.
            user: User prompt. A JSON-only instruction is appended internally.
            label: Optional label used in usage records.
            max_output_tokens: Optional per-request output cap. When omitted,
                the client-wide ``SIMPLE_AR_MAX_OUTPUT_TOKENS`` setting is used.
            budget_call_id: Optional stable logical-call identity for the
                shared budget ledger.
            image_paths: Same named-vision local-image contract as ``ask``.

        Returns:
            Parsed JSON object.

        Raises:
            LLMError: If the request fails or no JSON object can be parsed.
        """
        json_user = user + "\n\nReturn valid JSON only. Do not include markdown or extra text."
        response_format = _json_response_format_request(self._settings.json_response_format)
        try:
            raw = self.ask(
                system,
                json_user,
                label=label,
                max_output_tokens=max_output_tokens,
                response_format=response_format,
                budget_call_id=budget_call_id,
                **({"image_paths": image_paths} if image_paths != () else {}),
            )
        except LLMError as exc:
            if self._settings.json_response_format == "auto" and _is_response_format_error(exc):
                raw = self.ask(
                    system,
                    json_user,
                    label=f"{label}-no-response-format" if label else "",
                    max_output_tokens=max_output_tokens,
                    budget_call_id=budget_call_id,
                    **({"image_paths": image_paths} if image_paths != () else {}),
                )
            else:
                raise
        parsed = parse_json_object(raw)
        if parsed is None:
            raise LLMResponseError(
                "LLM response did not contain a JSON object "
                f"(received {len(raw.strip())} characters)", raw_response=raw,
            )
        return parsed

    def ask_many(
        self,
        requests: Sequence[LLMRequest],
        *,
        max_workers: int = 4,
    ) -> list[str]:
        """Send multiple text requests concurrently.

        Args:
            requests: Prompt requests to execute.
            max_workers: Maximum number of worker threads for this batch.

        Returns:
            Text responses in the same order as ``requests``.

        Raises:
            LLMError: If any request fails.
        """
        return self._run_many(
            requests,
            lambda request: self.ask(request.system, request.user, label=request.label),
            max_workers=max_workers,
        )

    def ask_json_many(
        self,
        requests: Sequence[LLMRequest],
        *,
        max_workers: int = 4,
    ) -> list[dict[str, Any]]:
        """Send multiple JSON requests concurrently.

        Args:
            requests: Prompt requests to execute.
            max_workers: Maximum number of worker threads for this batch.

        Returns:
            Parsed JSON objects in the same order as ``requests``.

        Raises:
            LLMError: If any request fails or returns invalid JSON.
        """
        return self._run_many(
            requests,
            lambda request: self.ask_json(request.system, request.user, label=request.label),
            max_workers=max_workers,
        )

    def _run_many(
        self,
        requests: Sequence[LLMRequest],
        handler: Callable[[LLMRequest], T],
        *,
        max_workers: int,
    ) -> list[T]:
        """Execute a bounded batch of LLM requests while preserving order."""
        if not requests:
            return []
        if max_workers < 1:
            raise LLMError("max_workers must be at least 1")

        worker_count = llm_worker_limit(min(max_workers, len(requests)))
        remaining = iter(enumerate(requests))
        results: dict[int, T] = {}
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            pending: dict[Future[T], tuple[int, LLMRequest]] = {}

            def submit_next() -> bool:
                try:
                    index, request = next(remaining)
                except StopIteration:
                    return False
                future = executor.submit(handler, request)
                pending[future] = (index, request)
                return True

            for _ in range(worker_count):
                submit_next()
            while pending:
                future = next(as_completed(tuple(pending)))
                index, request = pending.pop(future)
                try:
                    results[index] = future.result()
                except Exception as exc:
                    for unfinished in pending:
                        unfinished.cancel()
                    label = f" for {request.label}" if request.label else ""
                    raise LLMError(f"LLM batch request failed{label}: {exc}") from exc
                submit_next()
        return [results[index] for index in range(len(requests))]

    def _record_usage(
        self,
        response: object,
        system: str,
        user: str,
        output: str,
        *,
        label: str,
        provider_attempts: int = 1,
    ) -> LLMUsage:
        """Build and notify one usage record for compatibility callers."""
        record = self._build_usage_record(
            response,
            system,
            user,
            output,
            label=label,
            provider_attempts=provider_attempts,
        )
        self._notify_usage(record)
        return record

    def _build_usage_record(
        self,
        response: object,
        system: str,
        user: str,
        output: str,
        *,
        label: str,
        provider_attempts: int = 1,
        visual: bool = False,
        estimated_image_input_tokens: int = 0,
    ) -> LLMUsage:
        """Build usage without notifying observers or changing state."""
        usage = _usage_from_response(response)
        if visual and usage is None:
            return LLMUsage(self.model, label, None, None, None, "unknown",
                            provider_attempts=max(1, int(provider_attempts)),
                            estimated_image_input_tokens=estimated_image_input_tokens)
        if usage is None:
            prompt_tokens = estimate_tokens(system) + estimate_tokens(user)
            completion_tokens = estimate_tokens(output)
            total_tokens = prompt_tokens + completion_tokens
            source = "estimated"
        else:
            prompt_tokens, completion_tokens, total_tokens = usage
            source = "provider"
            if total_tokens < prompt_tokens + completion_tokens:
                total_tokens = prompt_tokens + completion_tokens

        record = LLMUsage(
            model=self.model,
            label=label,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            source=source,
            estimated_cost_usd=self._estimated_cost(prompt_tokens, completion_tokens),
            provider_attempts=max(1, int(provider_attempts)),
            estimated_image_input_tokens=estimated_image_input_tokens,
        )

        return record

    def _notify_usage(self, record: LLMUsage) -> None:
        """Notify the legacy usage observer exactly once after settlement."""
        if self._usage_callback is not None:
            with self._usage_lock:
                self._usage_callback(record)

    def _settle_budget(self, reservation_id: str | None, usage: LLMUsage, *, reason: str = '') -> None:
        if self._budget_ledger is None or reservation_id is None:
            return
        try:
            if usage.source == "unknown":
                self._budget_ledger.mark_unknown(reservation_id,
                    reason="Vision provider usage missing; input reservation was estimated, not actual usage",
                    known_actual={"llm_requests": 1})
                return
            self._budget_ledger.settle(
                reservation_id,
                {
                    "llm_requests": 1,
                    "total_tokens": usage.total_tokens,
                },
                actual_source=usage.source,
                reason=reason,
            )
        except BudgetError as exc:
            raise LLMError(f"Could not settle LLM usage in budget ledger: {exc}") from exc

    def _mark_budget_unknown(self, reservation_id: str | None, *, reason: str) -> None:
        if self._budget_ledger is None or reservation_id is None:
            return
        try:
            self._budget_ledger.mark_unknown(reservation_id, reason=reason)
        except BudgetError as exc:
            raise LLMError(f"Could not mark LLM budget usage unknown: {exc}") from exc

    def _next_budget_call_id(self) -> str:
        with self._usage_lock:
            self._budget_call_sequence += 1
            return f"llm-call-{self._budget_namespace}-{self._budget_call_sequence:06d}"

    def _next_budget_reservation_id(self) -> str:
        with self._usage_lock:
            self._budget_reservation_sequence += 1
            return f"llm-reservation-{self._budget_namespace}-{self._budget_reservation_sequence:06d}"

    def _estimated_cost(self, prompt_tokens: int, completion_tokens: int) -> float | None:
        """Estimate request cost when caller has configured model pricing."""
        input_price = self._settings.input_price_per_million
        output_price = self._settings.output_price_per_million
        if input_price is None or output_price is None:
            return None
        cost = (prompt_tokens / 1_000_000 * input_price) + (
            completion_tokens / 1_000_000 * output_price
        )
        return round(cost, 8)


def _decode_json_candidate(text: str) -> Any:
    """Decode one candidate; preserve literal characters in illegal escapes.

    Valid JSON is never rewritten. Only unknown string escape sequences have
    an unambiguous literal representation; malformed Unicode, raw controls,
    missing delimiters and quotes are not reconstructed.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        if exc.msg != "Invalid \\escape":
            raise
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            in_string = not in_string
        if in_string and char == "\\" and index + 1 < len(text):
            following = text[index + 1]
            if following not in '\\"/bfnrtu':
                output.append("\\")
            output.extend((char, following))
            index += 2
            continue
        output.append(char)
        index += 1
    return json.loads("".join(output))


def normalize_terminal_json_object(raw: str) -> tuple[dict, str] | None:
    """Close only missing terminal containers of one plain JSON object.

    Return the decoded object and exact suffix appended to ``raw``; already
    valid objects return an empty suffix. Nesting and suffix length are each
    limited to 64. Whitespace is preserved, and no scalar or interior syntax
    is repaired. Duplicate keys and non-JSON constants are rejected throughout.

    Callers must establish physical response completion first: finish_reason
    ``length`` and transport-incomplete responses are never eligible. A number
    at EOF can be syntactically complete even if its intended digits are not;
    this pure function cannot determine provider completion or author intent.
    """
    text = raw.lstrip(" \t\r\n")
    if not text.startswith("{"):
        return None

    stack: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif not stack and index > 0:
            if char not in " \t\r\n":
                return None
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
            if len(stack) > 64:
                return None
        elif char in "}]":
            if not stack or stack.pop() != char:
                return None
    if in_string:
        return None
    suffix = "".join(reversed(stack))
    if len(suffix) > 64:
        return None
    candidate = raw + suffix

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(token: str) -> Any:
        raise ValueError("Non-JSON constant")

    try:
        # The shared decoder supports legacy escape recovery. This strict gate
        # prevents that or duplicate-key acceptance in this append-only utility.
        value = json.loads(candidate, object_pairs_hook=unique_object,
                           parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return None
    return (value, suffix) if isinstance(value, dict) else None


def _first_json_object(text: str) -> str | None:
    """Locate the first outer object, never salvage a nested object on failure."""
    opening = re.search(r"[\[{]", text)
    if opening is None or opening.group() != "{":
        return None
    start = opening.start()
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Extract a JSON object from plain, fenced, or lightly wrapped text.

    Args:
        text: Raw model output.

    Returns:
        Parsed JSON object, or ``None`` when no object can be recovered.
    """
    if not text.strip():
        return None

    try:
        value = _decode_json_candidate(text)
        if isinstance(value, dict):
            return value
        # A few OpenAI-compatible gateways wrap an otherwise valid structured
        # response in a one-item array.  Accepting that shape is safe while
        # still rejecting arbitrary arrays, because the public contract
        # remains a single JSON object.
        if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
            return value[0]
        return None
    except json.JSONDecodeError:
        pass

    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        try:
            value = _decode_json_candidate(fence.group(1).strip())
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    # A damaged array is not a wrapper whose individual element can be chosen.
    if text.lstrip().startswith("["):
        return None
    candidate = _first_json_object(text)
    if candidate is not None:
        try:
            value = _decode_json_candidate(candidate)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using a conservative character heuristic.

    Args:
        text: Prompt or response text.

    Returns:
        Estimated token count. This is not model-tokenizer exact, but it is
        deterministic and dependency-free.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def _usage_from_response(response: object) -> tuple[int, int, int] | None:
    """Extract token usage from Responses or chat-completions response objects."""
    usage = _get_value(response, "usage")
    if usage is None:
        return None

    prompt_tokens = _int_value(usage, "input_tokens")
    if prompt_tokens is None:
        prompt_tokens = _int_value(usage, "prompt_tokens")

    completion_tokens = _int_value(usage, "output_tokens")
    if completion_tokens is None:
        completion_tokens = _int_value(usage, "completion_tokens")

    total_tokens = _int_value(usage, "total_tokens")
    if prompt_tokens is None or completion_tokens is None:
        return None
    if total_tokens is None:
        total_tokens = prompt_tokens + completion_tokens
    return prompt_tokens, completion_tokens, total_tokens


def _content_from_response(response: object) -> str:
    choices = _get_value(response, "choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        message = _get_value(first, "message")
        content = _get_value(message, "content") if message is not None else None
        if content is not None:
            return _text_from_content(content)
        text = _get_value(first, "text")
        if text is not None:
            return str(text)
    output_text = _get_value(response, "output_text")
    if output_text is not None:
        return str(output_text)
    output = _get_value(response, "output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            content = _get_value(item, "content")
            if content is not None:
                text = _text_from_content(content)
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return ""


def _chat_accounting_tail(chunks: object) -> tuple[int, int, int] | None:
    """A completed choice must not wait indefinitely for optional accounting.

    Only trailing metadata is read off-thread. Normal generation stays on the
    caller thread; the caller closes the stream/client after this short grace.
    A daemon prevents an uncooperative gateway from keeping the CLI alive.
    """
    result: queue.Queue = queue.Queue(maxsize=1)
    def collect() -> None:
        try:
            for chunk in chunks:
                usage = _usage_from_response(chunk)
                if usage is not None:
                    result.put_nowait(usage)
                    return
        except Exception:
            pass  # Content already finished; accounting may be unavailable.
        result.put_nowait(None)
    threading.Thread(target=collect, name="llm-accounting-tail", daemon=True).start()
    try:
        return result.get(timeout=2.0)
    except queue.Empty:
        return None


@dataclass
class _ChatStreamState:
    """One normalization contract for synchronous and asynchronous chunks."""
    parts: list[str] = field(default_factory=list)
    reasoning_parts: list[str] = field(default_factory=list)
    tool_calls: dict[int, dict[str, Any]] = field(default_factory=dict)
    usage: tuple[int, int, int] | None = None
    finish_reason: str | None = None
    received_chunks: int = 0

    def add(self, chunk: object) -> tuple[int, int, int] | None:
        self.received_chunks += 1
        chunk_usage = _usage_from_response(chunk)
        self.usage = chunk_usage or self.usage
        choices = _get_value(chunk, "choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        delta = _get_value(choice, "delta")
        content = _get_value(delta, "content") if delta is not None else None
        if content is None:
            content = _get_value(choice, "text")
        if content is not None:
            self.parts.append(_text_from_content(content))
        reasoning = _get_value(delta, "reasoning_content")
        if reasoning is not None:
            self.reasoning_parts.append(_text_from_content(reasoning))
        for fragment in _get_value(delta, "tool_calls") or []:
            index = _get_value(fragment, "index")
            if not isinstance(index, int) or index < 0:
                raise LLMStreamError("Tool stream omitted a valid call index")
            call = self.tool_calls.setdefault(index, {
                "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            call_id = _get_value(fragment, "id")
            if call_id:
                call["id"] = str(call_id)
            function = _get_value(fragment, "function")
            for key in ("name", "arguments"):
                value = _get_value(function, key)
                if value is not None:
                    call["function"][key] += str(value)
        value = _get_value(choice, "finish_reason")
        if value is not None:
            self.finish_reason = str(value)
        return chunk_usage

    def progress_description(self) -> str:
        """Safe phase evidence, never partial provider prose or tool arguments."""
        return (f"chunks={self.received_chunks}, content_chars={sum(map(len, self.parts))}, "
                f"reasoning_chars={sum(map(len, self.reasoning_parts))}, "
                f"tool_argument_chars={sum(len(c['function']['arguments']) for c in self.tool_calls.values())}, "
                f"finish_received={self.finish_reason is not None}, usage_received={self.usage is not None}")

    def response(self) -> dict[str, Any]:
        if self.finish_reason is None:
            raise LLMStreamError(
                "Response stream ended without a completion marker: "
                f"content_chars={sum(map(len, self.parts))}, usage_received={self.usage is not None}"
            )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.parts)}
        if self.reasoning_parts:
            message["reasoning_content"] = "".join(self.reasoning_parts)
        if self.tool_calls:
            message["tool_calls"] = [self.tool_calls[index] for index in sorted(self.tool_calls)]
        response: dict[str, Any] = {
            "choices": [{"message": message,
                         "finish_reason": self.finish_reason}],
        }
        if self.usage is not None:
            response["usage"] = dict(zip(
                ("prompt_tokens", "completion_tokens", "total_tokens"), self.usage
            ))
        return response


def _stream_error_description(exc: Exception) -> str:
    # SDK SSE errors often keep the useful reason in their parsed body.
    # Never expose provider prose: it may contain credentials or input text.
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        body = body.get("error", body)
    message = body.get("message", "") if isinstance(body, dict) else ""
    overloaded = isinstance(message, str) and any(
        marker in message.lower() for marker in ("overloaded", "over capacity")
    )
    details = [type(exc).__name__]
    status = _provider_error_status(exc)
    if status is not None:
        details.append(f"HTTP status={status}")
    # Only known protocol identifiers are diagnostic facts. Arbitrary codes,
    # messages, request IDs and URLs may themselves contain private input.
    if isinstance(body, dict):
        for key in ('code', 'type'):
            value = body.get(key)
            if value in ('server_error', 'internal_error', 'invalid_request_error',
                         'authentication_error', 'invalid_api_key', 'model_not_found',
                         'context_length_exceeded', 'rate_limit_exceeded',
                         'insufficient_quota', 'content_filter', 'overloaded'):
                details.append(f"provider {key}={value}")
    reason = "; provider reported overload" if overloaded else ""
    return f"Response stream interrupted ({'; '.join(details)}){reason}; final usage unavailable"


def _collect_chat_stream(stream_response: object) -> dict[str, Any]:
    """Normalize Chat Completions chunks into the existing response shape.

    Streaming is a transport choice, not a second LLM result contract. The
    caller still receives one assembled response, so parsing, usage settlement
    and retry handling remain unchanged. Missing final usage is handled by
    ``ask``: text-only estimation or unknown visual usage, respectively.
    """

    if isinstance(stream_response, (dict, list, str, bytes)):
        return stream_response  # type: ignore[return-value]
    try:
        chunks = iter(stream_response)  # type: ignore[arg-type]
    except TypeError:
        return stream_response  # type: ignore[return-value]

    state = _ChatStreamState()
    try:
        for chunk in chunks:
            chunk_usage = state.add(chunk)
            # Content terminality and accounting terminality are independent.
            # Preserve normal final usage, but never wait the full content timeout
            # or indefinite gateway heartbeats for an optional metadata tail.
            if state.finish_reason is not None:
                if chunk_usage is None:
                    state.usage = _chat_accounting_tail(chunks) or state.usage
                break
    except Exception as exc:
        # The response has already opened: a failure here may be billable even
        # when a gateway uses a generic exception with unfamiliar wording.
        # Never adopt the partial draft as a successfully completed response.
        if state.finish_reason is None:
            raise LLMStreamError(
                f"Response stream interrupted: {state.progress_description()}; "
                f"{_stream_error_description(exc)}"
            ) from exc
        # Generation completed explicitly; a missing accounting tail must not
        # resend an already completed generation. Use the estimated usage path.
        state.usage = None
    finally:
        close = getattr(stream_response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                # Cleanup must not replace the response or its original error.
                pass

    return state.response()


def _empty_response_message(response: object) -> str:
    """Explain why a provider response had no usable final text."""
    finish_reason = _finish_reason_from_response(response)
    reasoning = _reasoning_content_from_response(response)
    choices = _get_value(response, 'choices')
    message = _get_value(choices[0], 'message') if isinstance(choices, list) and choices else None
    if _get_value(message, 'tool_calls'):
        return ('LLM response contained native tool calls instead of final text. '
                'No tools were executed; use a declared native tool conversation for tool requests.')
    if reasoning:
        message = (
            "LLM response contained reasoning content but no final content "
            f"({len(reasoning)} reasoning characters)."
        )
        if finish_reason:
            message += f" finish_reason={finish_reason!r}."
        message += " Lower reasoning effort or increase the provider output cap."
        return message
    message = "LLM response did not contain final text."
    if finish_reason:
        message += f" finish_reason={finish_reason!r}."
    if finish_reason == "length":
        message += " Increase the per-call output token cap or shorten the request context."
    return message


def _finish_reason_from_response(response: object) -> str:
    choices = _get_value(response, "choices")
    if isinstance(choices, list) and choices:
        finish_reason = _get_value(choices[0], "finish_reason")
        if finish_reason is not None:
            return str(finish_reason)
    status = _get_value(response, "status")
    return str(status) if status is not None else ""


def _reasoning_content_from_response(response: object) -> str:
    """Read provider reasoning only for diagnostics, never as final output."""
    choices = _get_value(response, "choices")
    if isinstance(choices, list) and choices:
        message = _get_value(choices[0], "message")
        for name in ("reasoning_content", "reasoning"):
            value = _get_value(message, name)
            if value is not None:
                return _text_from_content(value)
    for name in ("reasoning_content", "reasoning"):
        value = _get_value(response, name)
        if value is not None:
            return _text_from_content(value)
    output = _get_value(response, "output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if str(_get_value(item, "type") or "").lower() == "reasoning":
                content = _get_value(item, "content")
                if content is not None:
                    parts.append(_text_from_content(content))
        return "\n".join(part for part in parts if part)
    return ""


def _text_from_content(content: object) -> str:
    """Return textual content from string or OpenAI-style content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            text = _get_value(chunk, "text")
            if text is None:
                text = _get_value(chunk, "content")
            if text is not None:
                rendered = _text_from_content(text)
                if rendered:
                    parts.append(rendered)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = _get_value(content, "text")
        if text is not None:
            return _text_from_content(text)
        nested = _get_value(content, "content")
        if nested is not None:
            return _text_from_content(nested)
        return ""
    return str(content) if content is not None else ""


def _int_value(obj: object, name: str) -> int | None:
    value = _get_value(obj, name)
    if isinstance(value, int):
        return value
    return None


def _get_value(obj: object, name: str) -> object | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _litellm_model(settings: LLMSettings) -> str:
    """Return the LiteLLM model string for default or custom OpenAI endpoints."""
    model = settings.model.strip()
    if settings.base_url and "/" not in model:
        return f"openai/{model}"
    return model


def _optional_float(env_name: str) -> float | None:
    value = os.environ.get(env_name, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _positive_float(env_name: str, *, default: float) -> float:
    value = os.environ.get(env_name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _optional_positive_float(env_name: str, *, default: float | None) -> float | None:
    value = os.environ.get(env_name, "").strip().lower()
    if not value:
        return default
    if value in {"0", "false", "no", "none", "off", "disabled", "unlimited"}:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else None


def _optional_positive_int(env_name: str, *, default: int | None) -> int | None:
    value = os.environ.get(env_name, "").strip().lower()
    if not value:
        return default
    if value in {"0", "false", "no", "none", "off", "disabled", "unlimited"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else None


def llm_worker_limit(requested: int) -> int:
    """Apply an optional process setting to each batch's worker count.

    This is an operational transport limit, not a persisted research-protocol
    choice. Leaving the environment unset preserves each caller's own bound.
    """
    if requested < 1:
        raise ValueError("requested LLM workers must be at least 1")
    configured = _positive_int("SIMPLE_AR_LLM_MAX_WORKERS", default=requested)
    return min(requested, configured)


def _boolean_env(env_name: str, *, default: bool) -> bool:
    value = os.environ.get(env_name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on", "enabled"}:
        return True
    if value in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _positive_int(env_name: str, *, default: int) -> int:
    value = os.environ.get(env_name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _json_response_format_mode(env_name: str) -> str:
    value = os.environ.get(env_name, "auto").strip().lower().replace("-", "_")
    aliases = {
        "": "off",
        "auto": "auto",
        "1": "json_object",
        "true": "json_object",
        "yes": "json_object",
        "on": "json_object",
        "json": "json_object",
        "json_object": "json_object",
        "response_format": "json_object",
        "0": "off",
        "false": "off",
        "no": "off",
        "none": "off",
        "disabled": "off",
        "off": "off",
    }
    return aliases.get(value, "off")


def _llm_api_mode(env_name: str) -> str:
    return _llm_api_mode_value(os.environ.get(env_name, "responses"))


def _llm_api_mode_value(value: str | None) -> str:
    value = str(value or "responses").strip().lower().replace("-", "_")
    aliases = {
        "": "responses",
        "auto": "auto",
        "compat": "auto",
        "responses_then_chat": "auto",
        "response_then_chat": "auto",
        "chat": "chat",
        "completion": "chat",
        "completions": "chat",
        "chat_completion": "chat",
        "chat_completions": "chat",
        "messages": "chat",
        "responses": "responses",
        "response": "responses",
        "input": "responses",
    }
    return aliases.get(value, "responses")


def _llm_transport_backend(env_name: str) -> str:
    value = os.environ.get(env_name, "openai").strip().lower().replace("-", "_")
    aliases = {
        "": "openai",
        "openai": "openai",
        "sdk": "openai",
        "openai_sdk": "openai",
        "direct": "openai",
        "native": "openai",
        "litellm": "litellm",
        "lite_llm": "litellm",
        "compat": "litellm",
    }
    return aliases.get(value, "openai")


def _chat_token_limit_param_mode(env_name: str) -> str:
    value = os.environ.get(env_name, "auto").strip().lower().replace("-", "_")
    aliases = {
        "": "auto",
        "auto": "auto",
        "max_tokens": "max_tokens",
        "tokens": "max_tokens",
        "legacy": "max_tokens",
        "max_completion_tokens": "max_completion_tokens",
        "completion_tokens": "max_completion_tokens",
        "reasoning": "max_completion_tokens",
    }
    return aliases.get(value, "auto")


def _reasoning_effort_mode(env_name: str) -> str:
    """Read an optional provider-documented reasoning effort value."""
    value = os.environ.get(env_name, "").strip().lower().replace("-", "_")
    if value in {"", "auto", "off", "disabled"}:
        return ""
    if value in {"none", "minimal", "low", "medium", "high", "max", "xhigh"}:
        return value
    return ""


def _thinking_mode(env_name: str) -> str:
    """Read an optional provider-specific Chat thinking switch."""
    value = os.environ.get(env_name, "").strip().lower()
    if value in {"", "default", "auto"}:
        return ""
    if value in {"enabled", "disabled"}:
        return value
    raise LLMError(f"{env_name} must be default, enabled, or disabled.")


def _chat_token_limit_param(mode: str, model: str) -> str:
    normalized = (mode or "auto").strip().lower().replace("-", "_")
    if normalized in {"max_tokens", "max_completion_tokens"}:
        return normalized
    model_name = model.lower().removeprefix("openai/")
    reasoning_prefixes = ("gpt-5", "gpt-4.1", "o1", "o3", "o4", "codex")
    if model_name.startswith(reasoning_prefixes):
        return "max_completion_tokens"
    return "max_tokens"


def _api_attempt_order(api_mode: str) -> list[str]:
    mode = (api_mode or "responses").strip().lower().replace("-", "_")
    if mode == "chat":
        return ["chat"]
    if mode == "auto":
        return ["responses", "chat"]
    if mode == "responses":
        return ["responses"]
    return ["responses"]


def _call_provider(backend: str, api_mode: str, request: dict[str, Any], *, http2: bool = False,
                   proxy: str | None = None, connect_timeout_sec: float | None = None) -> object:
    if backend == "litellm":
        if connect_timeout_sec is not None:
            raise LLMError("connect_timeout_sec is supported by the OpenAI SDK transport only")
        if proxy:
            raise LLMError("proxy is supported by the OpenAI SDK transport only")
        return _call_litellm(api_mode, request)
    if backend == "openai":
        options: dict[str, Any] = {}
        if http2:
            options["http2"] = True
        if proxy:
            options.update(http2=http2, proxy=proxy)
        if connect_timeout_sec is not None:
            options["connect_timeout_sec"] = connect_timeout_sec
        return _call_openai_sdk(api_mode, request, **options)
    raise ValueError(f"Unsupported LLM transport backend: {backend}")


def _load_litellm() -> Any:
    try:
        import litellm
    except ModuleNotFoundError as exc:
        if exc.name != "litellm":
            raise
        raise LLMError(
            "The litellm backend requires the optional dependency: "
            "install simple-autoresearch[litellm], or select the openai backend."
        ) from None
    return litellm


def _call_litellm(api_mode: str, request: dict[str, Any]) -> object:
    litellm = _load_litellm()

    litellm.suppress_debug_info = True
    if api_mode == "responses":
        return litellm.responses(**request)
    if api_mode == "chat":
        return litellm.completion(**request)
    raise ValueError(f"Unsupported LLM API mode: {api_mode}")


def _finite_timeout(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


_SDK_CONNECT_TIMEOUT_SEC = 20.0


class _SDKTransportObservation:
    """One request's safe transport facts, shared by sync and async SDK paths."""

    def __init__(self) -> None:
        self.event = 'unobserved'
        self.request_started = False

    def trace(self, event: str, _info: dict[str, Any]) -> None:
        if event in _TRANSPORT_PHASES:
            self.event = event
            if event.startswith(('http11.send_request_headers.', 'http2.send_request_headers.')):
                self.request_started = True

    def attach(self, request: Any) -> None:
        request.extensions['trace'] = self.trace

    async def async_trace(self, event: str, info: dict[str, Any]) -> None:
        self.trace(event, info)

    async def async_attach(self, request: Any) -> None:
        request.extensions['trace'] = self.async_trace

    def annotate(self, error: Exception) -> None:
        # Never retain trace info: headers, URLs, payloads and exceptions can
        # contain credentials. The original SDK exception/cause stays intact.
        error._simple_ar_transport_event = self.event
        error._simple_ar_request_started = self.request_started


def _sdk_timeout(value: object, connect_timeout_sec: float | None = None) -> object:
    # Keep legacy non-finite/absent timeout semantics unless explicitly tuned.
    if connect_timeout_sec is None and not _finite_timeout(value):
        return value
    import httpx
    connect = _SDK_CONNECT_TIMEOUT_SEC if connect_timeout_sec is None else connect_timeout_sec
    if _finite_timeout(value):
        connect = min(float(value), connect)
    if value is None:
        # Absent request timeout previously left the SDK defaults in charge.
        # An explicit connect retune changes only that phase of its defaults.
        from openai import DEFAULT_TIMEOUT
        return httpx.Timeout(connect=connect, read=DEFAULT_TIMEOUT.read,
                             write=DEFAULT_TIMEOUT.write, pool=DEFAULT_TIMEOUT.pool)
    return httpx.Timeout(value, connect=connect)


def _require_no_running_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise LLMError("Synchronous SDK Chat streaming cannot run inside an asyncio loop; "
                   "use await asyncio.to_thread(client.ask, ...) (or client.ask_json).")


async def _collect_chat_stream_async(stream: Any, state: _ChatStreamState) -> None:
    chunks = stream.__aiter__()
    try:
        async for chunk in chunks:
            chunk_usage = state.add(chunk)
            if state.finish_reason is not None:
                if chunk_usage is None:
                    # The enclosing wall-clock deadline also bounds this tail.
                    try:
                        async with asyncio.timeout(2.0):
                            async for tail in chunks:
                                usage = _usage_from_response(tail)
                                if usage is not None:
                                    state.usage = usage
                                    break
                    except Exception:
                        pass  # Explicitly completed content survives missing accounting.
                return
    except Exception as exc:
        raise LLMStreamError(f"{_stream_error_description(exc)}; {state.progress_description()}") from exc


async def _call_openai_chat_stream(
    payload: dict[str, Any], client_kwargs: dict[str, Any], *, http2: bool, timeout: float,
    proxy: str | None = None,
) -> dict[str, Any]:
    from openai import AsyncOpenAI, DefaultAsyncHttpxClient

    state = _ChatStreamState()
    client = stream = http_client = None
    observation = _SDKTransportObservation()

    deadline = asyncio.timeout(timeout)
    try:
        try:
            async with deadline:
                try:
                    http_client = DefaultAsyncHttpxClient(http2=http2,
                        event_hooks={'request': [observation.async_attach]}, **({"proxy": proxy} if proxy else {}))
                except ImportError:
                    raise LLMError("HTTP/2 requires the optional dependency: install simple-autoresearch[http2]") from None
                client = AsyncOpenAI(**client_kwargs, http_client=http_client)
                stream = await client.chat.completions.create(**_drop_none_values(payload))
                await _collect_chat_stream_async(stream, state)
        except TimeoutError:
            if not deadline.expired():
                raise  # Ordinary network timeouts keep the existing retry policy.
            if state.finish_reason is None:
                raise LLMDeadlineError(
                    "SDK Chat stream wall-clock deadline exceeded; usage unknown; "
                    f"transport_phase={observation.event}; "
                    f"stream_opened={stream is not None}; {state.progress_description()}",
                    transport_phase=observation.event, stream_opened=stream is not None,
                ) from None
            # Finish was observed: do not resend merely for an absent usage tail.
        return state.response()
    except Exception as exc:
        # Keep the SDK exception/cause intact for retry and usage classification.
        # Ordinary network timeouts need the same safe phase as wall deadlines;
        # never copy the trace payload, headers, URL or exception body.
        observation.annotate(exc)
        raise
    finally:
        # A separate bounded cleanup grace prevents pool/stream close hanging.
        # Cancellation cannot prove provider-side termination or free usage.
        cleanup_deadline = asyncio.get_running_loop().time() + 2.0
        for resource, method in ((stream, "close"),
                                 (client or http_client, "close" if client is not None else "aclose")):
            if resource is not None:
                try:
                    async with asyncio.timeout_at(cleanup_deadline):
                        await getattr(resource, method)()
                except Exception:
                    pass  # Cleanup must not replace a result or the request error.


_proxy_log_lock = threading.Lock()
_proxy_log_users = 0
_proxy_log_levels: dict[str, int] = {}


@contextmanager
def _quiet_proxy_transport(proxy: str | None):
    """Suppress transport URL diagnostics; overlapping calls share the grace."""
    global _proxy_log_users
    if not proxy:
        yield
        return
    with _proxy_log_lock:
        if not _proxy_log_users:
            names = {"openai", "httpx", "httpcore"} | {
                name for name in logging.root.manager.loggerDict
                if name.startswith(("openai.", "httpx.", "httpcore."))}
            for name in names:
                logger = logging.getLogger(name)
                _proxy_log_levels[name] = logger.level
                logger.setLevel(logging.CRITICAL + 1)
        _proxy_log_users += 1
    try:
        yield
    finally:
        with _proxy_log_lock:
            _proxy_log_users -= 1
            if not _proxy_log_users:
                for name, level in _proxy_log_levels.items():
                    logging.getLogger(name).setLevel(level)
                _proxy_log_levels.clear()


def _call_openai_sdk(api_mode: str, request: dict[str, Any], *, http2: bool = False,
                     proxy: str | None = None, connect_timeout_sec: float | None = None) -> object:
    # SDK DEBUG can expose inline pixels and credentials; retain them in memory.
    with _quiet_proxy_transport(proxy or ("vision" if _has_image_input(request) else None)):
        return _call_openai_sdk_request(api_mode, request, http2=http2, proxy=proxy,
                                        connect_timeout_sec=connect_timeout_sec)


def _call_openai_sdk_request(api_mode: str, request: dict[str, Any], *, http2: bool,
                             proxy: str | None, connect_timeout_sec: float | None = None) -> object:
    from openai import OpenAI, DefaultHttpxClient

    payload = dict(request)
    api_key = str(payload.pop("api_key", "") or "")
    base_url = payload.pop("base_url", None)
    api_base = payload.pop("api_base", None)
    base_url = base_url or api_base
    timeout = payload.pop("timeout", None) if "timeout" in payload else None
    client_kwargs: dict[str, Any] = {
        "api_key": api_key,
        # Keep retry accounting in SimpleAutoResearch. The OpenAI SDK defaults
        # to hidden internal retries, which can duplicate long planning calls
        # while the CLI only reports a single outer attempt.
        "max_retries": 0,
    }
    if timeout is not None or connect_timeout_sec is not None:
        client_kwargs["timeout"] = _sdk_timeout(timeout, connect_timeout_sec)
    if base_url:
        client_kwargs["base_url"] = str(base_url)
    if api_mode == "chat" and payload.get("stream") and _finite_timeout(timeout):
        _require_no_running_loop()
        return asyncio.run(_call_openai_chat_stream(
            payload, client_kwargs, http2=http2, timeout=float(timeout),
            **({"proxy": proxy} if proxy else {}),
        ))
    observation = _SDKTransportObservation()
    try:
        http_client = DefaultHttpxClient(http2=http2, event_hooks={'request': [observation.attach]},
            **({"proxy": proxy} if proxy else {}))
    except ImportError:
        raise LLMError("HTTP/2 requires the optional dependency: install simple-autoresearch[http2]") from None
    client_kwargs["http_client"] = http_client
    try:
        client = OpenAI(**client_kwargs)
    except BaseException:
        if http_client is not None:
            http_client.close()
        raise
    payload = _drop_none_values(payload)
    try:
        if api_mode == "responses":
            return client.responses.create(**payload)
        if api_mode == "chat":
            response = client.chat.completions.create(**payload)
            return _collect_chat_stream(response) if payload.get("stream") else response
        raise ValueError(f"Unsupported LLM API mode: {api_mode}")
    except Exception as exc:
        observation.annotate(exc)
        raise
    finally:
        client.close()


def _request_for_api_mode(
    request: dict[str, Any],
    api_mode: str,
    *,
    chat_token_limit_param: str = "auto",
    reasoning_effort: str = "",
    thinking_mode: str = "",
    stream: bool = False,
) -> dict[str, Any]:
    if request.get('_native_tools'):
        request = dict(request)
        request.pop('_native_tools')
        if api_mode == 'responses':
            request = _native_responses_request(request)
        else:
            request['messages'] = [{key:value for key,value in row.items() if key!='_responses_output'}
                                   for row in request['messages']]
    if api_mode == "responses":
        if thinking_mode:
            raise LLMError("Provider thinking.type is supported only with Chat Completions mode.")
        return _as_responses_request(request)
    if api_mode == "chat":
        converted = _as_chat_request(
            request,
            chat_token_limit_param=chat_token_limit_param,
            reasoning_effort=reasoning_effort,
            thinking_mode=thinking_mode,
        )
        if stream:
            converted["stream"] = True
            # SSE is incremental. Ask intermediaries not to compress/buffer it;
            # preserve an explicit caller override, as with other extra headers.
            headers = dict(converted.get("extra_headers") or {})
            if not any(key.lower() == "accept-encoding" for key in headers):
                headers["Accept-Encoding"] = "identity"
            converted["extra_headers"] = headers
        else:
            converted.pop("stream", None)
        return converted
    raise ValueError(f"Unsupported LLM API mode: {api_mode}")


def _native_responses_request(request: dict[str, Any]) -> dict[str, Any]:
    """Translate conversation envelopes only; original Responses items stay intact."""
    converted = dict(request)
    items = []
    for message in converted.pop('messages'):
        if message.get('_responses_output') is not None:
            items.extend(message['_responses_output'])
        elif message['role']=='tool':
            items.append({'type':'function_call_output','call_id':message['tool_call_id'],
                          'output':message['content']})
        else:
            if message.get('content'):
                items.append({'role':message['role'],'content':message['content']})
            for call in message.get('tool_calls') or []:
                items.append({'type':'function_call','call_id':call['id'],
                    'name':call['function']['name'],'arguments':call['function']['arguments']})
    converted['instructions'] = ''
    converted['input'] = items
    converted['tools'] = [{'type':'function','strict':False,**tool['function']}
                          for tool in converted.get('tools',[])]
    return converted


def _has_image_input(request: dict[str, Any]) -> bool:
    return any(isinstance(block, dict) and block.get("type") in {"image_url", "input_image"}
        for row in (request.get("messages") or request.get("input") or [])
        if isinstance(row, dict) and isinstance(row.get("content"), list)
        for block in row["content"])


def _as_responses_request(request: dict[str, Any]) -> dict[str, Any]:
    if "messages" in request and _has_image_input(request):
        raise LLMError("Vision input cannot switch API modes; build native Responses content")
    if "instructions" in request and "input" in request:
        converted = dict(request)
    else:
        messages = request.get("messages")
        system = ""
        user_parts: list[str] = []
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                content = _message_content_text(message.get("content"))
                if role == "system":
                    system = content
                else:
                    user_parts.append(content)
        converted = {
            "model": request.get("model"),
            "instructions": system,
            "input": [{"role": "user", "content": "\n\n".join(part for part in user_parts if part)}],
            "api_key": request.get("api_key"),
            "timeout": request.get("timeout"),
        }
        _copy_optional_request_fields(request, converted, ("api_base", "base_url"))
        if "response_format" in request:
            converted["text"] = {"format": request["response_format"]}
    if "max_output_tokens" not in converted:
        if "max_completion_tokens" in converted:
            converted["max_output_tokens"] = converted["max_completion_tokens"]
        elif "max_tokens" in converted:
            converted["max_output_tokens"] = converted["max_tokens"]
    converted.pop("max_completion_tokens", None)
    converted.pop("max_tokens", None)
    # Stream normalization is intentionally Chat-only in this lightweight
    # client; Responses events have a different shape and are not consumed by
    # _collect_chat_stream.
    converted.pop("stream", None)
    return _drop_none_values(converted)


def _as_chat_request(
    request: dict[str, Any],
    *,
    chat_token_limit_param: str = "auto",
    reasoning_effort: str = "",
    thinking_mode: str = "",
) -> dict[str, Any]:
    if "messages" not in request and _has_image_input(request):
        raise LLMError("Vision input cannot switch API modes; build native Chat content")
    if "messages" in request:
        converted = dict(request)
    else:
        system = str(request.get("instructions") or "")
        user = _responses_input_text(request.get("input"))
        converted = {
            "model": request.get("model"),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "api_key": request.get("api_key"),
            "timeout": request.get("timeout"),
        }
        _copy_optional_request_fields(request, converted, ("api_base", "base_url"))
        text_config = request.get("text")
        if isinstance(text_config, dict) and isinstance(text_config.get("format"), dict):
            converted["response_format"] = text_config["format"]
    output_cap = converted.pop("max_output_tokens", request.get("max_output_tokens"))
    if output_cap is not None:
        converted.pop("max_tokens", None)
        converted.pop("max_completion_tokens", None)
        converted[_chat_token_limit_param(chat_token_limit_param, str(converted.get("model") or ""))] = output_cap
    if thinking_mode == "disabled" and reasoning_effort not in {"", "none"}:
        raise LLMError("thinking_mode='disabled' requires reasoning_effort='' or 'none'.")
    if reasoning_effort or thinking_mode:
        provider_options = converted.get("extra_body")
        extra_body = dict(provider_options) if isinstance(provider_options, dict) else {}
        if reasoning_effort:
            extra_body["reasoning_effort"] = reasoning_effort
        if thinking_mode:
            extra_body["thinking"] = {"type": thinking_mode}
        converted["extra_body"] = extra_body
    return _drop_none_values(converted)


def _responses_input_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                parts.append(_message_content_text(item.get("content")))
            else:
                parts.append(str(item))
        return "\n\n".join(part for part in parts if part)
    if value is None:
        return ""
    return str(value)


def _message_content_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                text = item.get("text")
                if text is None:
                    text = item.get("content")
                if text is not None:
                    parts.append(str(text))
            elif item is not None:
                parts.append(str(item))
        return "\n".join(parts)
    if value is None:
        return ""
    return str(value)


def _copy_optional_request_fields(
    source: dict[str, Any], target: dict[str, Any], names: Sequence[str]
) -> None:
    for name in names:
        if name in source:
            target[name] = source[name]


def _drop_none_values(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _attempt_summary(mode_attempts: Sequence[tuple[str, int, Exception]]) -> str:
    if not mode_attempts:
        return "0"
    return ", ".join(f"{mode}={attempts}" for mode, attempts, _ in mode_attempts)


def _provider_failure_summary(exc: BaseException) -> str:
    """Retain diagnostic facts without retaining raw provider text or causes."""
    details = [type(exc).__name__]
    status = _provider_error_status(exc)
    if status is not None:
        details.append(f"HTTP status={status}")
    phase = _transport_failure_phase(exc)
    if phase:
        details.append(f"transport phase={phase}")
    event = getattr(exc, '_simple_ar_transport_event', '')
    if event in _TRANSPORT_PHASES and event != 'unobserved':
        details.append(f"transport event={event}")
    causes, seen = [], set()
    current = exc.__cause__
    while current is not None and id(current) not in seen and len(causes) < 8:
        seen.add(id(current))
        name = type(current).__name__
        if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
            causes.append(name)
        current = current.__cause__
    if causes:
        details.append('causes=' + '>'.join(causes))
    if ((isinstance(exc, LLMStreamError) and "provider reported overload" in str(exc))
            or "provider reported overload" in _stream_error_description(exc)):
        details.append("provider reported overload")
    return "; ".join(details)


def _json_response_format_request(mode: str) -> dict[str, Any] | None:
    return {"type": "json_object"} if mode in {"auto", "json_object"} else None


def _retry_delay(settings: LLMSettings, attempt: int) -> float:
    base = settings.retry_base_delay_sec if settings.retry_base_delay_sec > 0 else 1.0
    maximum = settings.retry_max_delay_sec if settings.retry_max_delay_sec > 0 else base
    return min(maximum, base * (2 ** max(0, attempt - 1)))


def _is_transient_llm_error(exc: Exception) -> bool:
    if isinstance(exc, LLMDeadlineError):
        return True
    if isinstance(exc, LLMStreamError):
        return True
    status = _provider_error_status(exc)
    if status is not None:
        return status in {408, 429, 500, 502, 503, 504, 524}
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    transient_markers = (
        "timeout",
        "timed out",
        "connection error",
        "connection reset",
        "connection aborted",
        "connection refused",
        "api connection",
        "service unavailable",
        "temporarily unavailable",
        "server overloaded",
        "servers are currently overloaded",
        "server is busy",
        "over capacity",
        "rate limit",
        "ratelimit",
        "too many requests",
        "internalservererror",
        "internal server error",
        "bad gateway",
        "gateway timeout",
        "origin timeout",
        "origin time-out",
        "server disconnected",
        "remote protocol error",
        "peer closed connection",
        "incomplete chunked read",
        "httpstatuserror",
        "429",
        "500",
        "502",
        "503",
        "504",
        "524",
    )
    permanent_markers = (
        "authentication",
        "invalid api key",
        "permission denied",
        "not found",
        "context_length",
        "context length",
        "invalid request",
        "badrequest",
        "400",
        "401",
        "403",
        "404",
    )
    # Some OpenAI-compatible gateways report a temporarily unavailable
    # routing channel as "model not found". Treat that precise combination as
    # retryable before applying the generic permanent "not found" rule.
    gateway_unavailable = (
        "no available channel",
        "no available provider",
        "distributor",
    )
    if any(marker in message for marker in gateway_unavailable) and any(
        marker in message for marker in ("429", "500", "502", "503", "504", "service unavailable")
    ):
        return True
    if any(marker in message or marker in name for marker in permanent_markers):
        return False
    return _is_response_transport_disconnect(exc) or any(
        marker in message or marker in name for marker in transient_markers
    )


def _is_response_transport_disconnect(exc: Exception) -> bool:
    if isinstance(exc, LLMStreamError):
        return True
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    markers = (
        "server disconnected",
        "remote protocol",
        "remoteprotocolerror",
        "peer closed connection",
        "incomplete chunked read",
        "connection reset",
        "connection aborted",
        "http/2 stream failed",
        "http2 stream failed",
        "stream reset",
        "stream closed",
        "stream terminated",
    )
    return any(marker in message or marker in name for marker in markers)


def _provider_error_status(exc: Exception) -> int | None:
    """Prefer SDK status metadata to matching arbitrary response-body digits."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _is_known_provider_rejection(exc: Exception) -> bool:
    status = _provider_error_status(exc)
    if status is not None:
        return 400 <= status < 500 and status not in {408, 429}
    description = f"{type(exc).__name__} {exc}".lower()
    return any(marker in description for marker in (
        "authentication", "invalid api key", "permission denied",
        "context_length", "context length", "invalid request", "badrequest",
        "model not found", "404 not found",
    ))


def _is_budget_consumption_unknown(exc: Exception) -> bool:
    """Return whether a failed call may have reached the provider."""

    phase = _transport_failure_phase(exc)
    if phase == 'not_dispatched':
        return False
    return (phase == 'possibly_dispatched' or isinstance(exc, LLMDeadlineError)
            or _is_response_transport_disconnect(exc) or _is_timeout_error(exc))


def _transport_failure_phase(exc: BaseException) -> str:
    """Use typed transport causes, never an endpoint/body/message heuristic."""
    import httpx

    seen = set()
    current: BaseException | None = exc
    uncertain = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        # A later redirected connection failure cannot erase an earlier send,
        # even when the annotated SDK exception is wrapped by another owner.
        if isinstance(current, LLMStreamError) or getattr(current, '_simple_ar_request_started', False):
            return 'possibly_dispatched'
        if isinstance(current, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
            return 'not_dispatched'
        if isinstance(current, (httpx.ReadError, httpx.ReadTimeout, httpx.WriteError,
                                httpx.WriteTimeout, httpx.RemoteProtocolError)):
            uncertain = True
        # The SDK intentionally hides HTTPX details in its public error. If
        # no classified cause is retained, lack of a response proves nothing.
        if type(current).__name__ in {'APIConnectionError', 'APITimeoutError'}:
            uncertain = True
        current = current.__cause__
    return 'possibly_dispatched' if uncertain else ''


def _is_timeout_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return "timeout" in name or "timed out" in message or "timeout" in message


def _is_response_format_error(exc: Exception) -> bool:
    message = str(exc).lower()
    markers = (
        "response_format",
        "response format",
        "json_object",
        "json object",
        "structured output",
        "structured outputs",
    )
    rejection_markers = (
        "unsupported",
        "not supported",
        "unrecognized",
        "unknown parameter",
        "extra inputs are not permitted",
        "invalid request",
        "badrequest",
        "400",
    )
    return any(marker in message for marker in markers) and any(
        marker in message for marker in rejection_markers
    )
