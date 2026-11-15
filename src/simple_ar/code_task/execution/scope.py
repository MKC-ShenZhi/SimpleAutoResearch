"""Task-owned execution for code inspection probes and validation."""

from dataclasses import dataclass
from contextlib import nullcontext
from pathlib import Path
import subprocess
from typing import Any, Mapping

from simple_ar.core.budget import BudgetLedger
from simple_ar.core.process_sandbox import ProcessSandbox
from simple_ar.experiment.execution.backend import LocalExecutionBackend, RunRequest, RunResult


@dataclass(frozen=True)
class CodeTaskProcessScope:
    sandbox: ProcessSandbox
    ledger: BudgetLedger
    session_id: str
    attempt_id: str
    output_root: Path
    environment: dict[str, str]
    validator_timeout_sec: int

    def probe(self, argv: list[str], *, cwd: Path, timeout_sec: int) -> RunResult:
        """Run one optional observation under the same scope and task budget."""
        backend = LocalExecutionBackend(budget_ledger=self.ledger, sandbox=self.sandbox,
                                        accounting_purpose='code_probe')
        # A nonessential probe must not consume the remaining validator slot.
        with self.ledger.protect_remaining({'process_invocations': 1,
                'process_wall_seconds': self.validator_timeout_sec}, additional=True):
            return backend.run(RunRequest(argv, cwd, timeout_sec, label='code_probe',
                env=self.environment, output_dir=self.output_root,
                session_id=self.session_id, attempt_id=self.attempt_id))


def run_code_task_probe(argv: list[str], *, cwd: Path, timeout_sec: int,
                        process_scope: Any | None = None,
                        ledger: BudgetLedger | None = None,
                        session_id: str = '', attempt_id: str = '',
                        output_root: Path | None = None,
                        environment: dict[str, str] | None = None,
                        protect: Mapping[str, int | float] | None = None):
    """Run an optional observation under its enclosing task's execution owner.

    Historical tasks without a ledger retain their original local execution.
    A workspace scope owns its own sandbox and later-validator protection.
    """
    if process_scope is not None:
        return process_scope.probe(argv, cwd=cwd, timeout_sec=timeout_sec)
    if ledger is None:
        return subprocess.run(argv, cwd=cwd, env=environment, capture_output=True, text=True,
                              timeout=timeout_sec, check=False)
    guard = ledger.protect_remaining(protect, additional=True) if protect else nullcontext()
    with guard:
        return LocalExecutionBackend(budget_ledger=ledger, accounting_purpose='code_probe').run(
            RunRequest(argv, cwd, timeout_sec, label='code_probe', env=environment, output_dir=output_root,
                       session_id=session_id, attempt_id=attempt_id))
