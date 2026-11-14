"""Execution backend protocol and local subprocess implementation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from simple_ar.experiment.metrics import parse_metric_lines
from simple_ar.core.process import ProcessSpec, confine_process, run_process, with_process_outputs
from simple_ar.core.process_sandbox import ProcessSandbox
from simple_ar.core.process_output import ProcessOutput
from simple_ar.core.budget import BudgetLedger


class ExecutionError(RuntimeError):
    """Raised when an execution request is invalid before process launch."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    command: list[str]
    cwd: Path
    timeout_sec: int
    label: str = "experiment"
    env: dict[str, str] | None = None
    output_dir: Path | None = None
    session_id: str = ""
    attempt_id: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    returncode: int | None
    timed_out: bool
    stdout: str
    stderr: str
    metrics: dict[str, float] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    duration_sec: float = 0.0
    backend: str = "local"
    label: str = "experiment"
    process_record: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.timed_out:
            return "timed_out"
        if self.returncode == 0:
            return "passed"
        return "failed"

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status
        return data


class ExecutionBackend(Protocol):
    name: str

    def run(self, request: RunRequest) -> RunResult:
        """Run one execution request and return a normalized result."""


@dataclass(slots=True)
class LocalExecutionBackend:
    name: str = "local"
    budget_ledger: BudgetLedger | None = field(default=None, repr=False)
    message_callback: Callable[[str], None] | None = field(default=None, repr=False)
    sandbox: ProcessSandbox | None = field(default=None, repr=False)
    accounting_purpose: str = 'local_process'

    def run(self, request: RunRequest) -> RunResult:
        if request.timeout_sec < 1:
            raise ExecutionError("timeout_sec must be at least 1")
        if not request.cwd.is_dir():
            raise ExecutionError(f"Working directory not found: {request.cwd}")
        if not request.command:
            raise ExecutionError("Execution command is empty")
        if request.output_dir is None and any("{output_dir}" in arg for arg in request.command):
            raise ExecutionError("Command contains {output_dir} but RunRequest.output_dir is not configured")

        spec = ProcessSpec(
            argv=request.command, cwd=request.cwd, timeout_sec=request.timeout_sec,
            env=request.env, output_dir=request.output_dir,
            session_id=request.session_id, attempt_id=request.attempt_id,
        )
        spec = with_process_outputs(spec)
        if self.sandbox is not None:
            spec = confine_process(spec, self.sandbox)
        if self.message_callback:
            self.message_callback(f"Experiment {request.label}: cwd={request.cwd}; timeout={request.timeout_sec}s")
            if spec.output_dir:
                self.message_callback(f"Process logs: {spec.output_dir}")
        output = ProcessOutput(self.message_callback) if self.message_callback else None
        try:
            result = run_process(spec, budget_ledger=self.budget_ledger,
                                 output_callback=output.feed if output else None,
                                 **({'accounting_purpose': self.accounting_purpose}
                                    if self.accounting_purpose != 'local_process' else {}))
        finally:
            if output:
                output.finish()
        timed_out = result.stop_reason == "timeout"
        stderr = result.stderr
        if timed_out:
            stderr = stderr.rstrip() + f"\nTimed out after {request.timeout_sec} seconds."
        return RunResult(
            returncode=None if timed_out else result.returncode,
            timed_out=timed_out, stdout=result.stdout, stderr=stderr,
            metrics=parse_metric_lines(result.stdout),
            command=list(spec.argv), cwd=str(request.cwd),
            duration_sec=result.duration_sec, process_record=result.record,
            backend=self.name, label=request.label,
        )
