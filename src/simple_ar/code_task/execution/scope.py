"""Task-owned execution for code inspection probes and validation."""

from dataclasses import dataclass
from pathlib import Path

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
