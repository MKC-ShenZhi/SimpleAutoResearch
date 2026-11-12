"""Factory for internal and external agent backends."""

from __future__ import annotations

from typing import Iterable

from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient

from .base import AgentBackend
from .external_cli import (
    ClaudeCodeCliBackend,
    CodexCliBackend,
    ExternalCliBackend,
    ExternalCliBackendConfig,
    OpenCodeCliBackend,
)
from .fake import FakeAgentBackend
from .local_llm import LocalLlmAgentBackend


def create_agent_backend(
    provider: str,
    *,
    enabled: bool = False,
    client: LLMClient | None = None,
    model: str | None = None,
    timeout_sec: int = 600,
    binary: str | None = None,
    extra_args: Iterable[str] = (),
    budget_ledger: BudgetLedger | None = None,
    session_id: str = '',
    attempt_id: str = '',
) -> AgentBackend:
    normalized = provider.strip().lower().replace("-", "_")
    args = tuple(extra_args)
    if normalized in {"fake", "dry_run", "dryrun"}:
        return FakeAgentBackend()
    if normalized in {"local_llm", "llm"}:
        return LocalLlmAgentBackend(client)
    if normalized == "codex":
        return CodexCliBackend(binary=binary or "codex", model=model or "", enabled=enabled, timeout_sec=timeout_sec, extra_args=args,
                               ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id)
    if normalized in {"claude", "claude_code"}:
        return ClaudeCodeCliBackend(binary=binary or "claude", model=model or "", enabled=enabled, timeout_sec=timeout_sec, extra_args=args,
                                    ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id)
    if normalized == "opencode":
        return OpenCodeCliBackend(binary=binary or "opencode", model=model or "", enabled=enabled, timeout_sec=timeout_sec, extra_args=args,
                                 ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id)
    if normalized in {"external_cli", "cli"}:
        if not binary:
            raise ValueError("external_cli backend requires a binary.")
        return ExternalCliBackend(
            ExternalCliBackendConfig(
                provider=normalized,
                binary=binary,
                enabled=enabled,
                model=model,
                timeout_sec=timeout_sec,
                extra_args=args,
            ), ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id
        )
    raise ValueError(f"Unknown agent backend provider: {provider}")


def is_external_agent_provider(provider: str) -> bool:
    normalized = provider.strip().lower().replace("-", "_")
    return normalized in {
        "fake",
        "dry_run",
        "dryrun",
        "local_llm",
        "llm",
        "codex",
        "claude",
        "claude_code",
        "opencode",
        "external_cli",
        "cli",
    }
