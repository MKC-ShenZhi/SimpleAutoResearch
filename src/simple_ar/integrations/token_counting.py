"""Optional local prompt counting for a configured model connection."""
from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=4)
def _deepseek_v41_counter(model: str, tokenizer_file: str):
    try:
        from deepseek_recipe import (
            ConversionOptions, DeepseekV41Encoding, ModelVersion, Tokenizer,
        )
    except ImportError as exc:
        raise ValueError("Install simple-autoresearch[deepseek-tokenizer] to use deepseek_v41 counting") from exc
    try:
        tokenizer = Tokenizer.from_file(tokenizer_file)
        encoding = DeepseekV41Encoding().with_tokenizer(tokenizer)
        options = ConversionOptions().with_model_alias(model, ModelVersion.DeepseekV41Flash)
    except Exception as exc:
        raise ValueError("Could not load the configured DeepSeek V4.1 tokenizer") from exc
    return encoding, options


def deepseek_v41_input_tokens(*, model: str, tokenizer_file: str,
                              messages: list[dict[str, Any]], tools: list[dict[str, Any]],
                              thinking_mode: str, reasoning_effort: str,
                              response_format: dict[str, Any] | None = None) -> int:
    """Estimate a text Chat request using the model's published prompt encoding.

    The gateway may add its own tokens, so the local count includes modest
    headroom and remains an estimate, not a provider-guaranteed hard bound.
    """
    encoding, options = _deepseek_v41_counter(model, tokenizer_file)
    from deepseek_recipe import ChatCompletionRequest
    request: dict[str, Any] = {"model": model, "messages": messages}
    if tools:
        request["tools"] = tools
    if thinking_mode:
        request["thinking"] = {"type": thinking_mode}
    if reasoning_effort:
        request["reasoning_effort"] = reasoning_effort
    if response_format:
        request["response_format"] = response_format
    try:
        count = len(encoding.encode(ChatCompletionRequest(request).convert(options).conversation))
    except Exception as exc:
        raise ValueError("Could not count the configured DeepSeek V4.1 chat request") from exc
    return (count * 102 + 99) // 100 + 32
