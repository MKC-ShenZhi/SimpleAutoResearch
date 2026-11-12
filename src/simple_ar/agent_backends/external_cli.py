"""Subprocess-backed external agent backends."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.process import ProcessSpec, run_process, verify_process_accounting

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

    def retained_result(self, request: AgentRunRequest) -> AgentRunResult | None:
        """Reuse a finished handoff, or leave an interrupted execution untouched."""
        root = request.handoff_dir.resolve()
        meta_path = root / 'agent_run.json'
        ledger_path = Path(request.run_dir) / 'budget_ledger.json'
        ledger = self.ledger or (BudgetLedger.load(ledger_path) if ledger_path.exists() else None)
        if not meta_path.is_file():
            processes = list((root / 'processes').glob('*/invocation.json'))
            unresolved = ledger and any(row.purpose == 'external_agent' and row.status == 'reserved'
                and row.session_id == self.session_id and row.attempt_id == self.attempt_id for row in ledger.entries)
            if unresolved or any(read_json(path).get('status') != 'launch_failed' for path in processes):
                raise RuntimeError('External agent handoff was interrupted; inspect its original process and ledger before continuing')
            return None
        payload = read_json(meta_path)
        if payload.get('usage_status') != 'unknown':
            referenced = (root / payload.get('process_record', '')).resolve()
            processes = (root / 'processes').glob('*/invocation.json')
            unresolved = ledger and any(row.reservation_id == payload.get('usage_reservation')
                and row.status in {'reserved', 'unknown'} for row in ledger.entries)
            if unresolved or any((path.resolve() != referenced or payload.get('usage_status') == 'starting')
                                 and read_json(path).get('status') != 'launch_failed' for path in processes):
                raise RuntimeError('External agent handoff was interrupted; inspect its original process and ledger before continuing')
            return None
        if ledger is None:
            raise ValueError('Retained external agent result has no original ledger')
        entries = [row for row in ledger.entries if row.reservation_id == payload.get('usage_reservation')]
        if (len(entries) != 1 or entries[0].status != 'unknown' or entries[0].purpose != 'external_agent'
                or entries[0].session_id != self.session_id or entries[0].attempt_id != self.attempt_id
                or payload.get('provider') != request.provider or payload.get('cwd') != str(root)
                or payload.get('workspace_dir') != (str(request.workspace_dir) if request.workspace_dir else None)):
            raise ValueError('Retained external agent result does not belong to this task and attempt')
        process_path = (root / payload['process_record']).resolve()
        if not process_path.is_relative_to(root):
            raise ValueError('External agent process record is outside its handoff')
        process = read_json(process_path)
        status = 'passed' if process.get('returncode') == 0 and not process.get('stop_reason') else 'failed'
        if (process.get('status') not in {'finished', 'stopped'} or process.get('cwd') != str(root)
                or process.get('session_id') != self.session_id or process.get('attempt_id') != self.attempt_id
                or payload.get('status') != status or payload.get('returncode') != process.get('returncode')):
            raise ValueError('Retained external agent process has no matching final result')
        verify_process_accounting(process, ledger, session_id=self.session_id, attempt_id=self.attempt_id,
            timeout_sec=process['timeout_sec'], accounting_purpose='external_agent_process')
        artifacts = [(root / path).resolve() for path in payload.get('artifacts', [])]
        if any(not path.is_relative_to(root) for path in artifacts):
            raise ValueError('External agent result contains an artifact outside its handoff')
        return AgentRunResult(provider=request.provider, status=status, message=payload.get('message', ''),
            artifacts=[str(path) for path in [*artifacts, meta_path]], result_path=str(meta_path),
            review_path=str(root / 'review.md') if (root / 'review.md').exists() else '',
            elapsed_sec=payload.get('elapsed_sec', 0), metadata={'usage_status': 'unknown',
                'usage_reservation': entries[0].reservation_id, 'process_record': str(process_path)})

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        started = time.perf_counter()
        request.handoff_dir.mkdir(parents=True, exist_ok=True)
        run_meta = request.handoff_dir / "agent_run.json"
        stdout_path = request.handoff_dir / "stdout.txt"
        stderr_path = request.handoff_dir / "stderr.txt"

        if (retained := self.retained_result(request)) is not None:
            return retained

        if not self.config.enabled:
            payload = self._run_payload(
                request,
                status="blocked",
                command=[self.config.binary, '<external agent arguments omitted>'],
                message="External agent backend is disabled by configuration.",
                returncode=None,
                timed_out=False,
                elapsed_sec=round(time.perf_counter() - started, 6),
            )
            write_json(run_meta, payload)
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
        process_id = uuid4().hex
        process_dir = request.handoff_dir / 'processes' / process_id
        process_record = process_dir / 'invocation.json'
        write_json(run_meta, {'provider': request.provider, 'status': 'starting',
            'cwd': str(request.handoff_dir.resolve()), 'usage_status': 'starting',
            'usage_reservation': reservation, 'process_record': str(process_record.relative_to(request.handoff_dir))})
        timeout = max(1, self.config.timeout_sec)
        ledger.reserve(reservation, {'llm_requests': 0, 'total_tokens': 0},
                       session_id=self.session_id, attempt_id=self.attempt_id,
                       logical_call_id=reservation, purpose='external_agent')
        returncode: int | None = None
        stop_reason: str | None = None
        message = "External agent completed."
        timed_out = False
        try:
            completed = run_process(ProcessSpec(argv=command, cwd=request.handoff_dir.resolve(),
                timeout_sec=timeout, env=self._safe_env(request), output_dir=process_dir,
                invocation_id=process_id, session_id=self.session_id, attempt_id=self.attempt_id,
                display_argv=[command[0], '<external agent arguments omitted>']), budget_ledger=ledger,
                accounting_purpose='external_agent_process')
        except BaseException as exc:
            try:
                not_started = not process_record.exists() or read_json(process_record).get('status') == 'launch_failed'
            except (OSError, ValueError, AttributeError):
                not_started = False
            if not_started:
                ledger.release(reservation, reason='External agent could not be started')
            else:
                ledger.mark_unknown(reservation, reason='External agent execution was interrupted; model use is unknown')
            if not isinstance(exc, OSError):
                raise
            message = f"External agent execution failed: {exc}"
            usage_status = 'not_started' if not_started else 'unknown'
        else:
            ledger.mark_unknown(reservation, reason='External agent model use is not reported')
            usage_status = 'unknown'
            returncode = completed.returncode
            stop_reason = completed.stop_reason
            timed_out = stop_reason == 'timeout'
            stdout_path.write_text(completed.stdout, encoding="utf-8", errors="replace")
            stderr_path.write_text(completed.stderr, encoding="utf-8", errors="replace")
            if timed_out:
                message = f"External agent timed out after {timeout}s."
            elif completed.returncode != 0 or stop_reason:
                message = _failure_message(completed.returncode, stderr=completed.stderr or "",
                                           stdout=completed.stdout or "", provider=self.name)

        elapsed = round(time.perf_counter() - started, 6)
        status = "passed" if returncode == 0 and not stop_reason else "failed"
        artifacts = _collect_agent_artifacts(request.handoff_dir)
        artifacts.extend(path for path in (stdout_path, stderr_path, process_record,
            process_dir / 'stdout.log', process_dir / 'stderr.log') if path.exists())
        payload = self._run_payload(
            request,
            status=status,
            command=[command[0], '<external agent arguments omitted>'],
            message=message,
            returncode=returncode,
            timed_out=timed_out,
            elapsed_sec=elapsed,
        )
        payload["artifacts"] = [str(path.relative_to(request.handoff_dir)) for path in artifacts]
        payload['usage_status'] = usage_status
        payload['usage_reservation'] = reservation
        if process_record.exists():
            payload['process_record'] = str(process_record.relative_to(request.handoff_dir))
        write_json(run_meta, payload)
        artifacts.append(run_meta)

        return AgentRunResult(
            provider=request.provider,
            status=status,
            message=message,
            artifacts=[str(path) for path in artifacts],
            result_path=str(run_meta),
            review_path=str(request.handoff_dir / "review.md") if (request.handoff_dir / "review.md").exists() else "",
            elapsed_sec=elapsed,
            metadata={'usage_status': usage_status, 'usage_reservation': reservation,
                      **({'process_record': str(process_record)} if process_record.exists() else {})},
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
