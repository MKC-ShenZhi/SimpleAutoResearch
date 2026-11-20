"""Review and validate applied repairs; experiment actions own measurement."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.core.artifacts import read_json, read_text, write_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.code_task.runtime.accounting import account_code_task
from simple_ar.code_task.editing.patching import apply_patch_edits
from simple_ar.code_task.execution.repair import RepairEvidence, RepairProposalResult, propose_repair_edits
from simple_ar.code_task.execution.runner import CodeTaskRunResult, _argv_command, run_code_task_benchmark
from simple_ar.code_task.execution.validation import validate_code_task
from simple_ar.code_task.review import review_code_task_changes, review_report_path
from simple_ar.code_task.execution.summary import write_code_task_summary
from simple_ar.code_task.generation.review import review_generated_project
from simple_ar.code_task.generation.task_contract import (
    build_greenfield_task_contract, contract_coverage, load_task_contract, save_task_contract,
)
from simple_ar.code_task.memory import record_review_finding
from simple_ar.code_task.runtime.state import (
    CodeTaskPaths, code_task_paths, code_task_workspace_boundary, load_code_task_manifest,
    manifest_section, read_optional_object, save_code_task_manifest, utcnow_iso,
)
from simple_ar.integrations.llm import LLMClient, bind_task_budget

MessageCallback = Callable[[str], None]


@account_code_task
def validate_repair_patch(
    run_dir: Path, *, llm_client: LLMClient | None = None, model: str | None = None,
    use_llm: bool = True, message_callback: MessageCallback | None = None,
    process_scope: Any | None = None,
    budget_ledger: BudgetLedger | None = None, session_id: str = "", attempt_id: str = "",
    process_protection: dict[str, int | float] | None = None,
) -> None:
    """Review and statically validate an applied repair without measuring it."""
    _emit(message_callback, "Reviewing embedded code-task repair before validation.")
    paths = code_task_paths(run_dir)
    manifest = load_code_task_manifest(run_dir)
    report = review_report_path(run_dir, 'post_repair')
    retained = report.is_file() and manifest.get('code_task', {}).get('native_binding')
    review_status = read_json(report)['status'] if retained else review_code_task_changes(
        run_dir, llm_client=llm_client, phase="post_repair", model=model,
        use_llm=use_llm, message_callback=message_callback,
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id).status
    if review_status == "failed":
        raise RuntimeError(
            "Embedded code-task review blocked the repair patch. "
            f"See {report}."
        )
    if (manifest.get('code_task', {}).get('kind') == 'greenfield'
            and manifest.get('implementation', {}).get('mode') == 'controlled_patch'):
        whole = review_saved_greenfield(run_dir, paths, max_files=6, max_generated_lines=1600)
        if whole['status'] == 'failed':
            raise RuntimeError('Generated revision failed the retained whole-project/API contract')
    repaired_validation = validate_code_task(run_dir,
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        process_protection=process_protection,
        **({'process_scope': process_scope} if process_scope is not None else {}))
    if repaired_validation.status == "failed":
        raise RuntimeError(
            "Embedded code-task validation failed after repair. "
            f"See {repaired_validation.report_path}."
        )


def review_saved_greenfield(
    run_dir: Path,
    paths: CodeTaskPaths,
    *,
    max_files: int,
    max_generated_lines: int,
) -> dict[str, Any]:
    from simple_ar.code_task.generation.greenfield import result_schema_from_manifest
    from simple_ar.code_task.runtime.state import review_workspace_boundary

    manifest = load_code_task_manifest(run_dir)
    boundary = review_workspace_boundary(paths.workspace_dir / 'generated_project',
        boundary=code_task_workspace_boundary(run_dir))
    code_artifacts = read_optional_object(paths.meta_dir / "code_artifacts.json")
    resource_plan = _greenfield_resource_plan(paths, max_files=max_files, max_generated_lines=max_generated_lines)
    if manifest.get('implementation', {}).get('mode') == 'controlled_patch':
        if not (paths.meta_dir / 'task_contract.json').is_file():
            raise ValueError('Generated revision is missing its original task contract')
        resource_plan = read_json(paths.meta_dir / 'implementation_plan.json')['resource_plan']
        project = paths.workspace_dir / 'generated_project'
        from simple_ar.code_task.analysis.index import build_codebase_index
        code_artifacts = {**code_artifacts, 'generated_files': [
            {'path': row['path'], 'mode': 'controlled_revision',
             'line_count': len((project / row['path']).read_text(encoding='utf-8', errors='replace').splitlines())}
            for row in build_codebase_index(project, access_boundary=boundary)['files']]}
    review = review_generated_project(
        access_boundary=boundary,
        project_dir=paths.workspace_dir / "generated_project",
        code_artifacts=code_artifacts,
        result_schema=result_schema_from_manifest(manifest),
        resource_plan=resource_plan,
        contract=greenfield_review_contract(paths),
        dependency_advice=read_optional_object(paths.meta_dir / "dependency_advice.json"),
        implementation_memory=read_optional_object(paths.task_dir / "memory" / "implementation_memory.json"),
        architecture_plan=read_optional_object(paths.meta_dir / "architecture_plan.json"),
        client=None,
        meta_dir=paths.meta_dir,
        use_llm=False,
    )
    report_path = paths.meta_dir / "review_report.json"
    if report_path.is_file():
        # Contract migration refreshes the active view, not historical facts.
        # Keep the complete prior report before replacing that view; this is
        # an immutable artifact, not another review state or replay policy.
        previous = read_json(report_path)
        digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
        historical = paths.meta_dir / "review_history" / f"{digest}.json"
        if historical.is_file():
            if read_json(historical) != previous:
                raise ValueError("Retained historical review changed; refusing to replace the active report")
        else:
            write_json(historical, previous)
    write_json(report_path, review)
    record_generated_review_findings(run_dir, review)
    manifest = load_code_task_manifest(run_dir)
    implementation = manifest_section(manifest, "implementation")
    implementation["review_status"] = review.get("status", "unknown")
    implementation["status"] = "generated" if review.get("status") != "failed" else "review_failed"
    implementation["reviewed_at"] = utcnow_iso()
    manifest["implementation"] = implementation
    save_code_task_manifest(run_dir, manifest)
    write_code_task_summary(run_dir)
    return review


def record_generated_review_findings(run_dir: Path, report: Mapping[str, Any]) -> None:
    findings = report.get("findings")
    if not isinstance(findings, list):
        return
    for row in findings:
        if not isinstance(row, dict):
            continue
        record_review_finding(
            run_dir,
            {
                "key": row.get("key")
                or f"greenfield:{row.get('category', '')}:{str(row.get('summary', ''))[:48]}",
                "severity": row.get("severity", "info"),
                "category": row.get("category", "general"),
                "summary": row.get("summary", ""),
                "evidence": row.get("evidence", []),
                "recommendation": row.get("recommendation", ""),
                "source": row.get("source", "greenfield-reviewer"),
            },
        )


def _greenfield_resource_plan(paths: CodeTaskPaths, *, max_files: int, max_generated_lines: int) -> dict[str, Any]:
    decision = read_optional_object(paths.meta_dir / "resource_decision.json")
    return {
        "schema_version": "code_task_greenfield_resource_plan.v1",
        "profile": str(decision.get("profile") or "local_cpu"),
        "allow_gpu": bool(decision.get("allow_gpu")),
        "max_files": max_files,
        "max_generated_lines": max_generated_lines,
        "decision": decision,
    }


def greenfield_review_contract(paths: CodeTaskPaths) -> dict[str, Any]:
    from simple_ar.code_task.generation.greenfield import result_schema_from_manifest

    contract = load_task_contract(paths.meta_dir)
    if contract:
        architecture = read_optional_object(paths.meta_dir / "architecture_plan.json")
        write_json(paths.meta_dir / "task_contract_coverage.json", contract_coverage(contract, architecture))
        return contract
    task_path = paths.task_dir / "task.md"
    task = read_text(task_path) if task_path.is_file() else ""
    manifest = load_code_task_manifest(paths.run_dir)
    result_schema = result_schema_from_manifest(manifest)
    architecture = read_optional_object(paths.meta_dir / "architecture_plan.json")
    generation_plan = architecture.get("generation_plan") if isinstance(architecture.get("generation_plan"), dict) else {}
    contract = build_greenfield_task_contract(
        task,
        benchmark_command=str((manifest.get("benchmark") or {}).get("command") or "python generated_project/main.py")
        if isinstance(manifest.get("benchmark"), dict)
        else "python generated_project/main.py",
        max_files=_positive_int(generation_plan.get("max_files"), 8),
        max_generated_lines=_positive_int(generation_plan.get("max_generated_lines"), 1600),
        result_schema=result_schema,
        source={
            "kind": "legacy_reconstructed",
            "task_file": "code_task/task.md",
            "origin": "greenfield_review_repair_compatibility",
        },
    )
    save_task_contract(paths.meta_dir, contract)
    write_json(paths.meta_dir / "task_contract_coverage.json", contract_coverage(contract, architecture))
    return contract


def _positive_int(value: object, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default








@dataclass(frozen=True)
class ImplementationVerificationResult:
    validation: CodeTaskRunResult
    repairs: tuple[dict[str, Any], ...]
    failed_artifacts: tuple[tuple[Path, Path], ...]
    repair: RepairProposalResult | None


@account_code_task
def apply_verified_repair(
    run_dir: Path, repair: RepairProposalResult, *, approval_note: str,
    llm_client: LLMClient, message_callback: MessageCallback | None = None,
    process_scope: Any | None = None,
    review_use_llm: bool = True,
    direct_edit: bool = False,
    budget_ledger: BudgetLedger | None = None, session_id: str = "", attempt_id: str = "",
    process_protection: dict[str, int | float] | None = None,
) -> None:
    """Apply an already authorized proposal; proposal-only entry points do not call this."""
    if not approval_note.strip():
        raise ValueError("Applying a repair requires explicit isolated-workspace approval.")
    if repair.mode != "llm" or not repair.edit_count:
        raise ValueError("Applying a repair requires a nonempty model proposal.")
    plan = load_code_task_manifest(run_dir).get('plan', {})
    direct_without_plan = (direct_edit and isinstance(plan, dict) and plan.get('status') in (None, '', 'not_started')
        and not (code_task_paths(run_dir).task_dir / 'patch_plan.md').exists())
    apply_patch_edits(run_dir, edits_file=repair.proposal_path,
        **({'allow_unapproved_plan': True} if direct_without_plan else {}))
    validate_repair_patch(run_dir, llm_client=llm_client, message_callback=message_callback,
        use_llm=review_use_llm,
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        process_protection=process_protection,
        **({'process_scope': process_scope} if process_scope is not None else {}))


@account_code_task
def verify_implementation(
    run_dir: Path, *, command: tuple[str, ...], approval_note: str,
    llm_client: LLMClient, max_repairs: int = 0, timeout_sec: int = 60,
    max_files: int = 6, max_source_chars_per_file: int = 12_000,
    asset_guard: Callable[[], Mapping[str, Any]],
    budget_ledger: BudgetLedger | None = None, session_id: str = "", attempt_id: str = "",
    message_callback: MessageCallback | None = None,
    process_scope: Any | None = None,
    review_use_llm: bool = True,
    direct_edit: bool = False,
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
    def task_client(enabled: bool = True) -> LLMClient:
        if not enabled:
            return llm_client
        return bind_task_budget(llm_client, budget_ledger, session_id=session_id, attempt_id=attempt_id)
    command_text = _argv_command(list(command))
    from simple_ar.code_task.runtime.state import implementation_file_hashes
    paths = code_task_paths(run_dir)
    binding = load_code_task_manifest(run_dir).get('code_task', {}).get('native_binding') if paths.manifest_path.is_file() else None
    report_path = paths.run_artifact_dir / 'patched' / 'execution_report.json'
    def run_validation(*, recovery_only: bool = False):
        return run_code_task_benchmark(run_dir, command=command_text, timeout_sec=timeout_sec,
            skip_validation=True, run_label='patched', budget_ledger=budget_ledger,
            session_id=session_id, attempt_id=attempt_id,
            **({'process_scope': process_scope} if process_scope is not None else {}),
            **({'_recovery_only': True} if recovery_only else {}))

    if binding is not None and budget_ledger is not None:
        # Only the runner may reconcile a settled invocation with no report.
        run_validation(recovery_only=True)
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
            validate_repair_patch(run_dir, llm_client=task_client(review_use_llm), message_callback=message_callback,
                use_llm=review_use_llm,
                budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
                process_protection={'process_invocations': 1, 'process_wall_seconds': timeout_sec},
                **({'process_scope': process_scope} if process_scope is not None else {}))
        elif repair_state.get('status') == 'repair_proposed':
            proposal_path = Path(run_dir) / repair_state['latest_proposed_edits']
            proposal = read_json(proposal_path)
            repair = RepairProposalResult(run_dir, proposal_path.parent, proposal_path,
                'llm', len(proposal['edits']), tuple(repair_state.get('selected_files', [])))
            apply_verified_repair(run_dir, repair, approval_note=approval_note,
                llm_client=task_client(review_use_llm), message_callback=message_callback,
                review_use_llm=review_use_llm,
                budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
                process_protection={'process_invocations': 1, 'process_wall_seconds': timeout_sec},
                **({'direct_edit': True} if direct_edit else {}),
                **({'process_scope': process_scope} if process_scope is not None else {}))
            recovered = None
    repairs: list[dict[str, Any]] = []
    failures: list[tuple[Path, Path]] = []
    for index in range(completed_repairs, max_repairs + 1):
        validation = recovered or run_validation()
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
            run_dir, llm_client=task_client(), max_files=max_files,
            budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
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
                              llm_client=task_client(review_use_llm), message_callback=message_callback,
                              review_use_llm=review_use_llm,
                              budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
                              process_protection={'process_invocations': 1, 'process_wall_seconds': timeout_sec},
                              **({'direct_edit': True} if direct_edit else {}),
                              **({'process_scope': process_scope} if process_scope is not None else {}))
        if asset_guard()["status"] == "changed":
            repairs[-1]["outcome"] = "protected_asset_changed"
            break
    return ImplementationVerificationResult(validation, tuple(repairs), tuple(failures), repair)


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
