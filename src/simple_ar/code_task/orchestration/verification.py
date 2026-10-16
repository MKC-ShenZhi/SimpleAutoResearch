"""Review and validate applied repairs; experiment actions own measurement."""

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import subprocess
from typing import Any, Callable, Mapping

from simple_ar.core.artifacts import read_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.code_task.editing.patching import apply_patch_edits
from simple_ar.code_task.execution.repair import RepairEvidence, RepairProposalResult, propose_repair_edits
from simple_ar.code_task.execution.runner import CodeTaskRunResult, recover_implementation_process, run_code_task_benchmark
from simple_ar.code_task.execution.validation import validate_code_task
from simple_ar.code_task.review import review_code_task_changes
from simple_ar.integrations.llm import LLMClient

MessageCallback = Callable[[str], None]
def validate_repair_patch(
    run_dir: Path, *, llm_client: LLMClient | None = None, model: str | None = None,
    use_llm: bool = True, message_callback: MessageCallback | None = None,
) -> None:
    """Review and statically validate an applied repair without measuring it."""
    _emit(message_callback, "Reviewing embedded code-task repair before validation.")
    from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
    report = code_task_paths(run_dir).meta_dir / 'review_report_post_repair.json'
    retained = report.is_file() and code_task_paths(run_dir).manifest_path.is_file() and load_code_task_manifest(run_dir).get('code_task', {}).get('native_binding')
    review_status = read_json(report)['status'] if retained else review_code_task_changes(
        run_dir, llm_client=llm_client, phase="post_repair", model=model,
        use_llm=use_llm, message_callback=message_callback).status
    if review_status == "failed":
        raise RuntimeError(
            "Embedded code-task review blocked the repair patch. "
            f"See {report}."
        )
    manifest = load_code_task_manifest(run_dir)
    if (manifest.get('code_task', {}).get('kind') == 'greenfield'
            and manifest.get('implementation', {}).get('mode') == 'controlled_patch'):
        from simple_ar.code_task.orchestration.execute import _rerun_greenfield_review
        whole = _rerun_greenfield_review(run_dir, code_task_paths(run_dir), max_files=6, max_generated_lines=1600)
        if whole['status'] == 'failed':
            raise RuntimeError('Generated revision failed the retained whole-project/API contract')
    repaired_validation = validate_code_task(run_dir)
    if repaired_validation.status == "failed":
        raise RuntimeError(
            "Embedded code-task validation failed after repair. "
            f"See {repaired_validation.report_path}."
        )
@dataclass(frozen=True)
class ImplementationVerificationResult:
    validation: CodeTaskRunResult
    repairs: tuple[dict[str, Any], ...]
    failed_artifacts: tuple[tuple[Path, Path], ...]
    repair: RepairProposalResult | None


def apply_verified_repair(
    run_dir: Path, repair: RepairProposalResult, *, approval_note: str,
    llm_client: LLMClient, message_callback: MessageCallback | None = None,
) -> None:
    """Apply an already authorized proposal; proposal-only entry points do not call this."""
    if not approval_note.strip():
        raise ValueError("Applying a repair requires explicit isolated-workspace approval.")
    if repair.mode != "llm" or not repair.edit_count:
        raise ValueError("Applying a repair requires a nonempty model proposal.")
    apply_patch_edits(run_dir, edits_file=repair.proposal_path)
    validate_repair_patch(run_dir, llm_client=llm_client, message_callback=message_callback)


def verify_implementation(
    run_dir: Path, *, command: tuple[str, ...], approval_note: str,
    llm_client: LLMClient, max_repairs: int = 0, timeout_sec: int = 60,
    max_files: int = 6, max_source_chars_per_file: int = 12_000,
    asset_guard: Callable[[], Mapping[str, Any]],
    budget_ledger: BudgetLedger | None = None, session_id: str = "", attempt_id: str = "",
    message_callback: MessageCallback | None = None,
) -> ImplementationVerificationResult:
    """Run an authorized technical check and bounded repairs, never scientific measurement.

    The caller owns protocol assets and supplies its guard. Every completed
    check is guarded before its diagnostics can trigger another model request.
    Actual failed reports remain immutable; no transport or unknown-use replay.
    """
    if not approval_note.strip() or not command:
        raise ValueError("Verification requires an approved nonempty command.")
    if type(max_repairs) is not int or max_repairs < 0:
        raise ValueError("max_repairs must be a non-negative integer")
    command_text = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, implementation_file_hashes
    paths = code_task_paths(run_dir)
    binding = load_code_task_manifest(run_dir).get('code_task', {}).get('native_binding') if paths.manifest_path.is_file() else None
    report_path = paths.run_artifact_dir / 'patched' / 'execution_report.json'
    if binding is not None and budget_ledger is not None:
        original = recover_implementation_process(run_dir, budget_ledger=budget_ledger,
            session_id=session_id, attempt_id=attempt_id)
        if original is not None:
            # The runner consumes this finalized invocation and uses its normal
            # result projection; it cannot launch another invocation here.
            run_code_task_benchmark(run_dir, command=command_text, timeout_sec=timeout_sec,
                skip_validation=True, run_label='patched', budget_ledger=budget_ledger,
                session_id=session_id, attempt_id=attempt_id)
    recovered = None
    completed_repairs = 0
    repair = None
    if binding is not None and report_path.is_file():
        saved = read_json(report_path)
        if (saved.get('native_binding') != binding
                or saved.get('command_text') != command_text or saved.get('timeout_sec') != timeout_sec):
            raise ValueError('Saved validation no longer matches the approved candidate; retain it without replay')
        recovered = CodeTaskRunResult(run_dir=run_dir, label='patched', status=saved['status'],
            report_path=report_path, stdout_path=report_path.parent / 'stdout.txt',
            stderr_path=report_path.parent / 'stderr.txt', metrics_path=report_path.parent / 'metrics.json',
            metrics=saved['metric_values'], returncode=saved['returncode'], timed_out=saved['timed_out'])
        repair_state = load_code_task_manifest(run_dir).get('repair', {})
        completed_repairs = int(repair_state.get('repair_count', 0))
        if completed_repairs > max_repairs:
            raise ValueError('Recorded repairs exceed this original approval; no new allowance')
        if saved.get('implementation_file_hashes') != implementation_file_hashes(run_dir):
            if repair_state.get('status') != 'repair_applied' or completed_repairs > max_repairs:
                raise ValueError('Saved validator targets a different candidate without a committed authorized repair')
            recovered = None  # Validate the already applied repair, not the old candidate again.
            validate_repair_patch(run_dir, llm_client=llm_client, message_callback=message_callback)
        elif repair_state.get('status') == 'repair_proposed':
            proposal_path = Path(run_dir) / repair_state['latest_proposed_edits']
            proposal = read_json(proposal_path)
            repair = RepairProposalResult(run_dir, proposal_path.parent, proposal_path,
                'llm', len(proposal['edits']), tuple(repair_state.get('selected_files', [])))
            apply_verified_repair(run_dir, repair, approval_note=approval_note,
                llm_client=llm_client, message_callback=message_callback)
            recovered = None
    repairs: list[dict[str, Any]] = []
    failures: list[tuple[Path, Path]] = []
    for index in range(completed_repairs, max_repairs + 1):
        validation = recovered or run_code_task_benchmark(
            run_dir, command=command_text, timeout_sec=timeout_sec, skip_validation=True,
            run_label="patched", budget_ledger=budget_ledger,
            session_id=session_id, attempt_id=attempt_id,
        )
        recovered = None
        if repairs:
            repairs[-1]["outcome"] = validation.status
        if asset_guard()["status"] == "changed":
            if repairs:
                repairs[-1]["outcome"] = "protected_asset_changed"
            break
        creation_blocked = (validation.status == 'blocked_by_validation' and binding is not None
            and read_json(validation.report_path).get('preflight', {}).get('reason') == 'required_new_files')
        if (validation.status not in {"failed", "timed_out"} and not creation_blocked) or index == max_repairs:
            break
        remaining = budget_ledger.remaining("process_invocations") if budget_ledger is not None else None
        wall_remaining = budget_ledger.remaining("process_wall_seconds") if budget_ledger is not None else None
        if (remaining is not None and remaining < 1
                or wall_remaining is not None and wall_remaining < timeout_sec):
            break
        report = read_json(validation.report_path)
        history = report.get("history_report") if isinstance(report, Mapping) else None
        if not isinstance(history, str) or not history.startswith("code_task/run/patched/attempts/"):
            raise ValueError("Bug validation did not record an immutable failed-run report.")
        archived = Path(run_dir) / history
        if (not archived.resolve().is_relative_to((Path(run_dir) / "code_task/run/patched/attempts").resolve())
                or not archived.is_file() or not (archived.parent / "stderr.txt").is_file()):
            raise ValueError("Bug validation failed-run history is incomplete or outside its attempt boundary.")
        failures.append((archived, archived.parent / "stderr.txt"))
        streams = [(name, path.read_text(encoding="utf-8", errors="replace"))
                   for name, path in (("stderr", validation.stderr_path), ("stdout", validation.stdout_path))]
        streams = [(name, text) for name, text in streams if text.strip()]
        limit = 8000 // max(1, len(streams))
        details = "\n\n".join(f"{name}:\n{text[-limit:]}" for name, text in streams)
        _emit(message_callback, f"Bug validation failed; proposing bounded technical repair {index + 1}/{max_repairs}.")
        repair = propose_repair_edits(
            run_dir, llm_client=llm_client, max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file, message_callback=message_callback,
            failure_evidence=RepairEvidence(str(archived), {
                "execution_status": validation.status, "returncode": validation.returncode,
                "timed_out": validation.timed_out, "command": list(command),
                **({'preflight': report['preflight']} if creation_blocked else {}),
            }, details or f"Validation command {validation.status} without captured output."),
        )
        repairs.append({"round": index + 1, "failed_validation_report": str(archived),
                        "repair_proposal": str(repair.proposal_path), "edit_count": repair.edit_count})
        if repair.mode != "llm" or not repair.edit_count:
            repairs[-1]["outcome"] = "no_repair_edits"
            break
        apply_verified_repair(run_dir, repair, approval_note=approval_note,
                              llm_client=llm_client, message_callback=message_callback)
        if asset_guard()["status"] == "changed":
            repairs[-1]["outcome"] = "protected_asset_changed"
            break
    return ImplementationVerificationResult(validation, tuple(repairs), tuple(failures), repair)


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
