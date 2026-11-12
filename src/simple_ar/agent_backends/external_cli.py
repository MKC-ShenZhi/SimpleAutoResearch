"""Subprocess-backed external agent backends."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from simple_ar.core.budget import BudgetError, BudgetLedger

from .base import AgentBackend, AgentRunRequest, AgentRunResult


DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "HOME",
    "USERPROFILE",
)


@dataclass(frozen=True)
class ExternalCliBackendConfig:
    provider: str
    binary: str
    enabled: bool = False
    model: str | None = None
    timeout_sec: int = 600
    extra_args: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    command_template: tuple[str, ...] = field(default_factory=tuple)


class ExternalCliBackend(AgentBackend):
    """Run an external coding/research agent CLI against a handoff directory."""

    def __init__(self, config: ExternalCliBackendConfig, *, ledger: BudgetLedger | None = None,
                 session_id: str = '', attempt_id: str = '') -> None:
        self.config = config
        self.ledger, self.session_id, self.attempt_id = ledger, session_id, attempt_id

    @property
    def name(self) -> str:
        return self.config.provider

    def accounting_ledger(self, run_dir: Path) -> BudgetLedger:
        path = Path(run_dir) / 'budget_ledger.json'
        ledger = self.ledger or (BudgetLedger.load(path) if path.exists() else BudgetLedger(storage_path=path))
        if any(ledger.limits.get(name) is not None for name in ('llm_requests', 'total_tokens')):
            raise BudgetError('External agent CLI cannot enforce the task model budget or report its usage; '
                              'use a budget-accounted backend for this task.')
        return ledger

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        started = time.perf_counter()
        request.handoff_dir.mkdir(parents=True, exist_ok=True)
        run_meta = request.handoff_dir / "agent_run.json"
        stdout_path = request.handoff_dir / "stdout.txt"
        stderr_path = request.handoff_dir / "stderr.txt"

        if not self.config.enabled:
            payload = self._run_payload(
                request,
                status="blocked",
                command=self.preview_command(request),
                message="External agent backend is disabled by configuration.",
                returncode=None,
                timed_out=False,
                elapsed_sec=round(time.perf_counter() - started, 6),
            )
            run_meta.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            return AgentRunResult(
                provider=request.provider,
                status="blocked",
                message=payload["message"],
                artifacts=[str(run_meta)],
                result_path=str(run_meta),
                elapsed_sec=payload["elapsed_sec"],
            )

        ledger = self.accounting_ledger(request.run_dir)
        command = self.build_command(request)
        reservation = 'external-agent-' + uuid4().hex
        timeout = max(1, self.config.timeout_sec)
        ledger.reserve(reservation, {'llm_requests': 0, 'total_tokens': 0,
                                     'process_invocations': 1, 'process_wall_seconds': timeout},
                       session_id=self.session_id, attempt_id=self.attempt_id,
                       logical_call_id=reservation, purpose='external_agent')
        timed_out = False
        returncode: int | None = None
        message = "External agent completed."
        process_started = time.perf_counter()

        def record_unknown(reason: str, *, measured: bool = True) -> None:
            actual = {'process_invocations': 1, 'process_wall_seconds': time.perf_counter() - process_started}
            ledger.mark_unknown(reservation, reason=reason, known_actual=actual if measured else None)

        try:
            completed = subprocess.run(
                command,
                cwd=str(request.handoff_dir.resolve()),
                env=self._safe_env(request),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            ledger.release(reservation, reason='External agent could not be started')
            message = f"External agent could not be started: {exc}"
            usage_status = 'not_started'
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            message = f"External agent timed out after {self.config.timeout_sec}s."
            record_unknown('External agent model use is not reported')
            usage_status = 'unknown'
            stdout_path.write_text(_as_text(exc.stdout), encoding="utf-8", errors="replace")
            stderr_path.write_text(_as_text(exc.stderr), encoding="utf-8", errors="replace")
        except OSError as exc:
            record_unknown('External agent execution failed; model use is unknown', measured=False)
            usage_status = 'unknown'
            message = f"External agent execution failed: {exc}"
        except BaseException:
            record_unknown('External agent execution was interrupted; model use is unknown', measured=False)
            raise
        else:
            record_unknown('External agent model use is not reported')
            usage_status = 'unknown'
            returncode = completed.returncode
            stdout_path.write_text(completed.stdout or "", encoding="utf-8", errors="replace")
            stderr_path.write_text(completed.stderr or "", encoding="utf-8", errors="replace")
            if completed.returncode != 0:
                message = _failure_message(completed.returncode, stderr=completed.stderr or "",
                                           stdout=completed.stdout or "", provider=self.name)

        elapsed = round(time.perf_counter() - started, 6)
        status = "passed" if returncode == 0 and not timed_out else "failed"
        artifacts = _collect_agent_artifacts(request.handoff_dir)
        artifacts.extend(path for path in (stdout_path, stderr_path) if path.exists())
        payload = self._run_payload(
            request,
            status=status,
            command=self.preview_command(request),
            message=message,
            returncode=returncode,
            timed_out=timed_out,
            elapsed_sec=elapsed,
        )
        payload["artifacts"] = [str(path.relative_to(request.handoff_dir)) for path in artifacts]
        payload['usage_status'] = usage_status
        payload['usage_reservation'] = reservation
        run_meta.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        artifacts.append(run_meta)

        return AgentRunResult(
            provider=request.provider,
            status=status,
            message=message,
            artifacts=[str(path) for path in artifacts],
            result_path=str(run_meta),
            review_path=str(request.handoff_dir / "review.md") if (request.handoff_dir / "review.md").exists() else "",
            elapsed_sec=elapsed,
            metadata={'usage_status': usage_status, 'usage_reservation': reservation},
        )

    def build_command(self, request: AgentRunRequest) -> list[str]:
        if self.config.command_template:
            return _render_command_template(self.config.command_template, request, self.config)
        prompt = _instruction_prompt(request.handoff_dir)
        command = [_resolve_binary(self.config.binary)]
        command.extend(self.config.extra_args)
        command.append(prompt)
        return command

    def preview_command(self, request: AgentRunRequest) -> list[str]:
        command = self.build_command(request)
        return [_redact_arg(arg) for arg in command]

    def _safe_env(self, request: AgentRunRequest) -> dict[str, str]:
        allowed = set(self.config.env_allowlist) | set(request.env_allowlist)
        return {key: value for key, value in os.environ.items() if key in allowed}

    def _run_payload(
        self,
        request: AgentRunRequest,
        *,
        status: str,
        command: Sequence[str],
        message: str,
        returncode: int | None,
        timed_out: bool,
        elapsed_sec: float,
    ) -> dict:
        return {
            "provider": request.provider,
            "backend": self.name,
            "status": status,
            "message": message,
            "cwd": str(request.handoff_dir.resolve()),
            "workspace_dir": str(request.workspace_dir) if request.workspace_dir else None,
            "command": list(command),
            "returncode": returncode,
            "timed_out": timed_out,
            "elapsed_sec": elapsed_sec,
        }


class CodexCliBackend(ExternalCliBackend):
    def __init__(
        self,
        *,
        binary: str = "codex",
        model: str = "",
        enabled: bool = False,
        timeout_sec: int = 600,
        extra_args: tuple[str, ...] = (),
        ledger: BudgetLedger | None = None, session_id: str = '', attempt_id: str = '',
    ) -> None:
        super().__init__(
            ExternalCliBackendConfig(
                provider="codex",
                binary=binary,
                enabled=enabled,
                model=model or None,
                timeout_sec=timeout_sec,
                extra_args=extra_args,
            ), ledger=ledger, session_id=session_id, attempt_id=attempt_id
        )

    def build_command(self, request: AgentRunRequest) -> list[str]:
        handoff_root = str(request.handoff_dir.resolve())
        command = [
            _resolve_binary(self.config.binary),
            "exec",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            handoff_root,
        ]
        if self.config.model:
            command.extend(["-m", self.config.model])
        command.extend(self.config.extra_args)
        command.append(_instruction_prompt(request.handoff_dir))
        return command


class ClaudeCodeCliBackend(ExternalCliBackend):
    def __init__(
        self,
        *,
        binary: str = "claude",
        model: str = "",
        enabled: bool = False,
        timeout_sec: int = 600,
        extra_args: tuple[str, ...] = (),
        ledger: BudgetLedger | None = None, session_id: str = '', attempt_id: str = '',
    ) -> None:
        super().__init__(
            ExternalCliBackendConfig(
                provider="claude_code",
                binary=binary,
                enabled=enabled,
                model=model or None,
                timeout_sec=timeout_sec,
                extra_args=extra_args,
            ), ledger=ledger, session_id=session_id, attempt_id=attempt_id
        )

    def build_command(self, request: AgentRunRequest) -> list[str]:
        command = [_resolve_binary(self.config.binary), "-p", _instruction_prompt(request.handoff_dir)]
        if self.config.model:
            command.extend(["--model", self.config.model])
        command.extend(self.config.extra_args)
        return command


class OpenCodeCliBackend(ExternalCliBackend):
    def __init__(
        self,
        *,
        binary: str = "opencode",
        model: str = "",
        enabled: bool = False,
        timeout_sec: int = 600,
        extra_args: tuple[str, ...] = (),
        ledger: BudgetLedger | None = None, session_id: str = '', attempt_id: str = '',
    ) -> None:
        super().__init__(
            ExternalCliBackendConfig(
                provider="opencode",
                binary=binary,
                enabled=enabled,
                model=model or None,
                timeout_sec=timeout_sec,
                extra_args=extra_args,
            ), ledger=ledger, session_id=session_id, attempt_id=attempt_id
        )

    def build_command(self, request: AgentRunRequest) -> list[str]:
        command = [_resolve_binary(self.config.binary), "run"]
        if self.config.model:
            command.extend(["-m", self.config.model])
        command.extend(self.config.extra_args)
        command.append(_instruction_prompt(request.handoff_dir))
        return command


def _as_text(value: str | bytes | None) -> str:
    return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''


def _instruction_prompt(handoff_dir: Path) -> str:
    return (
        "Follow the instructions in instructions.md. Write all outputs inside this handoff "
        "directory. For generated code, place files under generated_files/."
    )


def _render_command_template(
    template: Sequence[str], request: AgentRunRequest, config: ExternalCliBackendConfig
) -> list[str]:
    prompt = _instruction_prompt(request.handoff_dir)
    values = {
        "binary": _resolve_binary(config.binary),
        "handoff_dir": str(request.handoff_dir.resolve()),
        "workspace_dir": str(request.workspace_dir or ""),
        "instructions": str(request.handoff_dir / "instructions.md"),
        "prompt": prompt,
        "model": config.model or "",
    }
    rendered = [part.format(**values) for part in template]
    if config.extra_args:
        rendered.extend(config.extra_args)
    return rendered


def _resolve_binary(binary: str) -> str:
    """Resolve CLI shims for subprocess without shell=True.

    PowerShell can find npm-installed ``*.ps1`` shims, but Python subprocess
    with ``shell=False`` needs a directly executable file. On Windows, prefer
    the matching ``.cmd``/``.exe``/``.bat`` wrapper before falling back to
    ``shutil.which``.
    """

    raw = binary.strip()
    if not raw:
        return binary
    path = Path(raw)
    if path.is_absolute() or path.parent != Path("."):
        return str(path)
    if os.name == "nt" and not path.suffix:
        for suffix in (".cmd", ".exe", ".bat", ".com"):
            found = shutil.which(raw + suffix)
            if found:
                return found
    found = shutil.which(raw)
    return found or raw


def _collect_agent_artifacts(handoff_dir: Path) -> list[Path]:
    candidates = [
        handoff_dir / "agent_result.json",
        handoff_dir / "review.md",
        handoff_dir / "patch.diff",
        handoff_dir / "proposed_edits.json",
        handoff_dir / "results.json",
    ]
    artifacts = [path for path in candidates if path.exists()]
    generated = handoff_dir / "generated_files"
    if generated.exists():
        artifacts.extend(path for path in generated.rglob("*") if path.is_file())
    return artifacts


def _redact_arg(arg: str) -> str:
    lowered = arg.lower()
    if "api_key" in lowered or "token" in lowered or "secret" in lowered:
        return "<redacted>"
    return arg


def _failure_message(returncode: int, *, stderr: str, stdout: str, provider: str) -> str:
    text = (stderr or stdout or "").strip()
    if not text:
        return f"External agent exited with code {returncode}."
    tail = _tail_text(text)
    hint = ""
    lowered = tail.lower()
    if "model is not supported" in lowered or "unsupported model" in lowered:
        hint = (
            " Hint: check [implementation].agent_model; leave it empty to use "
            f"the {provider} CLI default model, or set a model supported by your account."
        )
    elif "not found" in lowered or "is not recognized" in lowered:
        hint = " Hint: check [implementation].agent_binary and PATH."
    return f"External agent exited with code {returncode}. {tail}{hint}"


def _tail_text(text: str, *, max_chars: int = 900) -> str:
    lines = [line.strip() for line in text.replace("\r", "\n").splitlines() if line.strip()]
    if not lines:
        compact = " ".join(text.split())
    else:
        compact = " ".join(lines[-6:])
    if len(compact) <= max_chars:
        return compact
    return "..." + compact[-max_chars:]
