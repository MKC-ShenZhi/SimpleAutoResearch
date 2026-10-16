from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.integrations.usage import record_usage
from simple_ar.core.artifacts import read_json, read_jsonl, read_text, write_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.code_task.editing.attempts import (
    LoadedCodeTaskBatch,
    create_code_task_batch,
    load_latest_code_task_batch,
    update_code_task_batch_state,
)
from simple_ar.code_task.execution.environment import probe_code_task_environment
from simple_ar.code_task.execution.baseline_policy import (
    load_provided_baseline_metrics,
    normalize_baseline_policy,
)
from simple_ar.code_task.execution.failure import analyze_code_task_failure
from simple_ar.code_task.analysis.context import (
    build_code_task_context_pack,
    load_latest_code_task_context_pack,
    planned_context_paths,
)
from simple_ar.code_task.editing.patching import EditBudgetApprovalRequired, PatchValidationError, apply_patch_edits, propose_patch_edits
from simple_ar.code_task.editing.planning import generate_patch_plan, record_plan_decision
from simple_ar.code_task.generation.greenfield import generate_greenfield_code_task, result_schema_from_manifest
from simple_ar.code_task.generation.task_contract import (
    build_greenfield_task_contract,
    contract_coverage,
    load_task_contract,
    save_task_contract,
)
from simple_ar.code_task.generation.generated_project_repair import (
    repair_generated_project_from_review,
    repair_generated_project_from_run_failure,
)
from simple_ar.code_task.generation.review import is_current_greenfield_review, review_generated_project
from simple_ar.code_task.execution.repair import propose_repair_edits
from simple_ar.code_task.review import review_code_task_changes
from simple_ar.code_task.execution.runner import (
    record_provided_code_task_baseline,
    run_code_task_baseline,
    run_code_task_benchmark,
)
from simple_ar.code_task.memory import (
    code_task_memory_paths,
    ensure_task_memory,
    record_code_task_memory_event,
    record_edit_history,
    record_repair_memory,
    record_review_finding,
    task_memory_context,
)
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.execution.summary import write_code_task_summary
from simple_ar.code_task.execution.validation import validate_code_task
from simple_ar.code_task.editing.work_plan import generate_code_task_work_plan
from simple_ar.integrations.llm import LLMClient, LLMError
from simple_ar.code_task.runtime.config import EXECUTE_STEPS, normalize_review_gate


MessageCallback = Callable[[str], None]

GREENFIELD_RUNTIME_REVIEW_BLOCKERS = (
    "unsafe",
    "path_traversal",
    "missing_entrypoint",
    "missing_file",
    "python_compile",
    "syntax",
    "stdlib_module_shadow",
    "missing_local_api",
    "import_error",
    "module_not_found",
    "nested_artifact_path_risk",
    "network",
    "shell",
    "subprocess",
    "credential",
    "secret",
    "home directory",
    "resource",
    "timeout",
    "infinite",
    "unbounded",
)


@dataclass(frozen=True)
class ExecuteStepRecord:
    """One action considered by the code-task orchestrator.

    Args:
        step: Step name from ``EXECUTE_STEPS``.
        status: ``done``, ``skipped``, ``blocked``, or ``would_run``.
        detail: Short human-readable explanation.
    """

    step: str
    status: str
    detail: str


@dataclass(frozen=True)
class CodeTaskExecuteResult:
    """Result returned by the code-task execute orchestrator.

    Args:
        run_dir: Code-task run directory.
        steps: Ordered step decisions and actions.
        stop_reason: Why execution stopped.
        next_action: Suggested next command or review action.
        summary_path: Human-readable code-task summary path.
    """

    run_dir: Path
    steps: tuple[ExecuteStepRecord, ...]
    stop_reason: str
    next_action: str
    summary_path: Path


def execute_code_task(
    run_dir: Path,
    *,
    to_step: str = "run",
    dry_run: bool = False,
    llm_client: LLMClient | None = None,
    model: str | None = None,
    planner_model: str | None = None,
    writer_model: str | None = None,
    reviewer_model: str | None = None,
    editor_model: str | None = None,
    repair_model: str | None = None,
    use_llm: bool = True,
    timeout_sec: int = 60,
    skip_validation: bool = False,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
    strict_validation: bool = False,
    validation_max_file_bytes: int = 500_000,
    stream_benchmark_output: bool | str = False,
    baseline_policy: str = "auto",
    baseline_metrics_file: str | Path | None = None,
    apply_proposed_edits: bool = False,
    approval_note: str = "",
    allow_large_edits: bool = False,
    allow_planning_fallback: bool = False,
    planning_mode: str = "compact",
    planning_review_rounds: int = 0,
    llm_retry_attempts: int = 1,
    repair_rounds: int = 0,
    planning_snapshot_from: str | Path | None = None,
    review_gate: str = "strict",
    budget_profile: str | None = None,
    edit_budget_overrides: dict[str, Any] | None = None,
    max_batches: int | None = None,
    cost_cap_usd: float | None = None,
    max_files: int = 8,
    max_source_chars_per_file: int = 4000,
    max_generated_lines: int = 1600,
    implementation_provider: str = "local",
    implementation_agent_mode: str = "",
    implementation_allow_external_agent: bool = False,
    implementation_agent_model: str = "",
    implementation_agent_binary: str = "",
    implementation_agent_args: tuple[str, ...] = (),
    implementation_agent_timeout_sec: int = 600,
    message_callback: MessageCallback | None = None,
    budget_ledger: BudgetLedger | None = None,
    session_id: str = "",
    attempt_id: str = "",
) -> CodeTaskExecuteResult:
    """Run a conservative state-aware code-task workflow.

    The orchestrator coordinates the existing primitive
    steps, skips artifacts that are already present, and stops at review gates
    instead of silently applying model proposals.

    Args:
        run_dir: Existing code-task run directory created by ``code-task init``.
        to_step: Last step the orchestrator may attempt.
        dry_run: Preview the next executable action without writing artifacts.
        approval_note: Explicit authorization to continue a pending plan and
            its proposal in this call. Empty preserves interactive review gates.
        model: Optional fallback LLM model override for planning/edit/repair
            steps.
        planner_model: Optional model override for work-plan and patch-plan
            steps, including greenfield architecture planning.
        writer_model: Optional model override for greenfield file generation.
            Existing-project edit proposals continue to use ``editor_model``.
        reviewer_model: Optional model override for code-task and greenfield
            review steps.
        editor_model: Optional model override for controlled edit proposals.
        repair_model: Optional model override for repair proposals.
        use_llm: Whether planning/edit/repair steps may call the LLM.
        timeout_sec: Benchmark timeout for baseline and patched runs.
        skip_validation: Allow benchmark execution even when validation fails.
        env_mode: Optional environment mode override for probe and runs.
        python_executable: External interpreter when ``env_mode`` is external.
        strict_validation: Treat risky validation warnings as errors.
        validation_max_file_bytes: Per-file validation scan budget.
        stream_benchmark_output: Relay benchmark stdout/stderr while baseline
            or patched runs are executing. ``True`` uses ``auto`` mode, which
            understands carriage-return progress output such as tqdm.
        baseline_policy: Existing-project baseline handling. ``auto`` and
            ``run`` execute the benchmark, ``skip``/``none`` continue without
            comparison evidence, and ``provided`` records user-supplied metrics.
        baseline_metrics_file: JSON or metric-line file used when
            ``baseline_policy`` is ``provided``.
        apply_proposed_edits: Allow execute to apply an existing or generated
            proposal after the patch plan has been approved.
        allow_large_edits: Allow proposals that exceed the normal edit budget
            but fit the large profile.
        allow_planning_fallback: Allow deterministic offline work/patch plans
            and greenfield fallbacks after LLM attempts fail. By default, LLM
            failures stop the run without writing fallback plans so the same
            execute command can retry cleanly.
        planning_mode: Complete greenfield architecture work unit. ``tool_agent``
            is a deprecated alias for ``compact``; historical stages are retained
            with a visible method migration receipt before new planning.
        planning_review_rounds: Deprecated. Nonzero values produce a migration
            notice, never additional calls. Set to zero for the replacement.
        llm_retry_attempts: Number of stage-level attempts for LLM-backed work
            planning, patch planning, greenfield architecture/file generation,
            and repair before stopping or explicitly falling back.
        repair_rounds: Maximum repair proposals execute may create after a
            validation or benchmark failure. Proposals are never auto-applied.
        planning_snapshot_from: Optional source run containing an accepted
            greenfield plan. Only planning metadata is imported after contract
            hash verification; generated code and mutable memory are excluded.
        review_gate: Greenfield review gate mode. ``strict`` stops on any
            blocking review finding. ``runtime`` records review findings but
            only stops before execution for safety or direct executability
            blockers such as unsafe paths, missing files, Python compile
            failures, missing entrypoint, or resource-limit violations.
        budget_profile: Optional edit budget profile passed to edit proposal
            normalization.
        edit_budget_overrides: Optional numeric budget overrides from config.
        max_batches: Optional hard cap on attempt/batch creation.
        cost_cap_usd: Optional LLM cost cap. When no cost estimate is
            available from the provider, this guard is informational only.
        max_files: Context file budget for LLM planning/edit/repair steps.
        max_source_chars_per_file: Source snippet budget per selected file.
        message_callback: Optional progress callback.

    Returns:
        Ordered execute decisions and suggested next action.
    """
    if to_step not in EXECUTE_STEPS:
        raise ValueError("to_step must be one of: " + ", ".join(EXECUTE_STEPS))
    if timeout_sec < 1:
        raise ValueError("timeout_sec must be at least 1")
    if repair_rounds < 0:
        raise ValueError("repair_rounds must be non-negative")
    if planning_review_rounds < 0:
        raise ValueError("planning_review_rounds must be non-negative")
    review_gate = normalize_review_gate(review_gate)
    if max_batches is not None and max_batches < 1:
        raise ValueError("max_batches must be at least 1 when provided")
    if cost_cap_usd is not None and cost_cap_usd < 0:
        raise ValueError("cost_cap_usd must be non-negative when provided")
    if llm_retry_attempts < 1:
        raise ValueError("llm_retry_attempts must be at least 1")
    baseline_policy = normalize_baseline_policy(baseline_policy)

    root = Path(run_dir)
    paths = code_task_paths(root)
    manifest = load_code_task_manifest(root)
    ensure_task_memory(root)
    steps: list[ExecuteStepRecord] = []
    if (_code_task_kind(manifest) == "greenfield"
            and manifest.get('implementation', {}).get('mode') != 'controlled_patch'):
        return _execute_greenfield_code_task(
            root,
            paths,
            steps,
            budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
            to_step=to_step,
            dry_run=dry_run,
            llm_client=llm_client,
            model=model,
            planner_model=planner_model,
            writer_model=writer_model,
            reviewer_model=reviewer_model,
            repair_model=repair_model,
            use_llm=use_llm,
            timeout_sec=timeout_sec,
            skip_validation=skip_validation,
            env_mode=env_mode,
            python_executable=python_executable,
            strict_validation=strict_validation,
            validation_max_file_bytes=validation_max_file_bytes,
            stream_benchmark_output=stream_benchmark_output,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
            max_generated_lines=max_generated_lines,
            allow_planning_fallback=allow_planning_fallback,
            planning_mode=planning_mode,
            planning_review_rounds=planning_review_rounds,
            llm_retry_attempts=llm_retry_attempts,
            repair_rounds=repair_rounds,
            planning_snapshot_from=planning_snapshot_from,
            review_gate=review_gate,
            budget_profile=budget_profile, edit_budget_overrides=edit_budget_overrides,
            allow_large_edits=allow_large_edits,
            implementation_provider=implementation_provider,
            implementation_agent_mode=implementation_agent_mode,
            implementation_allow_external_agent=implementation_allow_external_agent,
            implementation_agent_model=implementation_agent_model,
            implementation_agent_binary=implementation_agent_binary,
            implementation_agent_args=implementation_agent_args,
            implementation_agent_timeout_sec=implementation_agent_timeout_sec,
            message_callback=message_callback,
        )

    def review_patch(phase: str) -> str:
        report_path = paths.meta_dir / ("review_report.json" if phase == "post_apply" else "review_report_post_run.json")
        if report_path.is_file():
            status = read_json(report_path)["status"]
            _record(steps, "review", "skipped", f"{phase} review already recorded: {status}")
        else:
            _emit(message_callback, f"Running {phase} code review.")
            review = review_code_task_changes(
                root, phase=phase, llm_client=llm_client,
                model=reviewer_model or model, use_llm=use_llm,
                max_source_chars_per_file=max_source_chars_per_file,
                message_callback=message_callback,
            )
            status = review.status
            _record(steps, "review", "done",
                    f"{phase} status {review.status}; blocking {review.blocking_count}; warnings {review.warning_count}")
        if (_code_task_kind(manifest) == 'greenfield'
                and manifest.get('implementation', {}).get('mode') == 'controlled_patch'):
            whole = _rerun_greenfield_review(root, paths, max_files=max_files,
                                             max_generated_lines=max_generated_lines)
            if whole['status'] == 'failed':
                status = 'failed'
        return status

    manifest = load_code_task_manifest(root)
    # Stages are sequential: each stop point owns admission to the next stage.
    # Only failure analysis/repair need a separate requested-boundary check.
    if _environment_report_exists(paths):
        _record(steps, "probe", "skipped", "environment_report.json already exists")
    elif dry_run:
        return _dry_result(paths, steps, "probe", "record environment signals")
    else:
        _emit(message_callback, "Running environment probe.")
        result = probe_code_task_environment(
            root,
            env_mode=env_mode,
            python_executable=python_executable,
        )
        _record(steps, "probe", "done", f"wrote {result.report_path}")
        _memory_event(
            root,
            "probe",
            "Recorded environment and dependency signals.",
            status="done",
            artifacts=[_relative_to_run(root, result.report_path)],
        )
    if to_step == "probe":
        return _result(paths, steps, "stop_point", "Stopped after probe as requested.")

    manifest = load_code_task_manifest(root)
    baseline = _run_record(manifest, "baseline")
    if baseline:
        _record(steps, "baseline", "skipped", f"baseline status is {baseline.get('status', 'unknown')}")
    elif dry_run:
        return _dry_result(paths, steps, "baseline", _baseline_dry_run_detail(baseline_policy))
    elif baseline_policy in {"skip", "none"}:
        _record_baseline_policy(root, policy=baseline_policy, status="skipped")
        write_code_task_summary(root)
        _record(steps, "baseline", "skipped", f"baseline policy is {baseline_policy}")
        _memory_event(
            root,
            "baseline",
            f"Skipped unchanged baseline because baseline_policy={baseline_policy}.",
            status="skipped",
            metadata={"baseline_policy": baseline_policy},
        )
    elif baseline_policy == "provided":
        metrics, source = load_provided_baseline_metrics(
            root,
            baseline_metrics_file,
            missing_message=(
                "baseline_policy=provided requires [execute].baseline_metrics_file "
                "or --baseline-metrics-file."
            ),
        )
        _emit(message_callback, f"Recording provided baseline metrics from {source}.")
        result = record_provided_code_task_baseline(
            root,
            metrics=metrics,
            source_path=source,
            env_mode=env_mode,
            python_executable=python_executable,
        )
        _record(steps, "baseline", "done", f"provided metrics from {source}")
        _memory_event(
            root,
            "baseline",
            "Recorded user-provided baseline metrics.",
            status="provided",
            artifacts=[
                _relative_to_run(root, result.report_path),
                _relative_to_run(root, result.metrics_path),
            ],
            metadata={"metrics": result.metrics, "source": source},
        )
    else:
        _emit(message_callback, "Running baseline benchmark.")
        result = run_code_task_baseline(
            root,
            timeout_sec=timeout_sec,
            skip_validation=skip_validation,
            env_mode=env_mode,
            python_executable=python_executable,
            stream_output=stream_benchmark_output,
            output_callback=_benchmark_output_callback(message_callback),
            budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        )
        _record(steps, "baseline", "done", f"status {result.status}")
        _memory_event(
            root,
            "baseline",
            f"Baseline benchmark finished with status {result.status}.",
            status=result.status,
            artifacts=[
                _relative_to_run(root, result.report_path),
                _relative_to_run(root, result.metrics_path),
            ],
            metadata={"metrics": result.metrics},
        )
        if result.status != "passed":
            return _result(
                paths,
                steps,
                "baseline_failed",
                "Review code_task/run/baseline/ before asking for edits.",
            )
    baseline = _run_record(load_code_task_manifest(root), "baseline")
    if baseline and baseline.get("status") != "passed":
        return _result(
            paths,
            steps,
            "baseline_failed",
            "Review code_task/run/baseline/ before asking for edits.",
        )
    if to_step == "baseline":
        return _result(paths, steps, "stop_point", "Stopped after baseline as requested.")

    # Batch decomposition is explicit; ordinary tasks need one approved patch plan.
    if to_step in {"work-plan", "batch"}:
        if _work_plan_exists(paths):
            _record(steps, "work-plan", "skipped", "work_plan.json already exists")
        elif dry_run:
            return _dry_result(paths, steps, "work-plan", "generate batch-oriented work plan")
        elif _cost_cap_exceeded(paths.meta_dir, cost_cap_usd):
            return _result(paths, steps, "cost_cap_exceeded", "LLM cost cap reached before work planning.")
        else:
            _emit(message_callback, "Generating batch-oriented work plan.")
            try:
                result = generate_code_task_work_plan(
                    root,
                    llm_client=llm_client,
                    model=planner_model or model,
                    use_llm=use_llm,
                    allow_llm_fallback=allow_planning_fallback,
                    llm_retry_attempts=llm_retry_attempts,
                    max_files=max_files,
                    max_source_chars_per_file=max_source_chars_per_file,
                    message_callback=message_callback,
                )
            except LLMError as exc:
                _record(steps, "work-plan", "blocked", _first_error_line(str(exc)))
                return _result(
                    paths,
                    steps,
                    "llm_planning_failed",
                    (
                        "LLM work planning failed and no offline fallback was written. "
                        "Rerun the same execute command to retry, use --no-llm for a "
                        "deterministic plan, or pass --allow-planning-fallback if an "
                        "offline fallback is acceptable."
                    ),
                )
            _record(steps, "work-plan", "done", f"mode {result.mode}; items {result.item_count}")
            _memory_event(
                root,
                "work_plan",
                f"Generated {result.item_count} work-plan item(s) in {result.mode} mode.",
                status="done",
                artifacts=[
                    _relative_to_run(root, result.work_plan_path),
                    _relative_to_run(root, result.work_plan_markdown_path),
                ],
                metadata={"selected_files": list(result.selected_files)},
            )
    if to_step == "work-plan":
        return _result(paths, steps, "stop_point", "Stopped after work-plan as requested.")

    manifest = load_code_task_manifest(root)
    latest_batch = _latest_batch(manifest)
    if latest_batch:
        _record(steps, "batch", "skipped", f"latest batch is {latest_batch}")
    elif not _work_plan_exists(paths):
        _record(steps, "batch", "skipped", "single-task path uses the patch plan directly")
    elif dry_run:
        return _dry_result(paths, steps, "batch", "create attempt/batch state for first work item")
    elif _batch_cap_exceeded(root, max_batches):
        return _result(paths, steps, "batch_budget_exceeded", "Configured batch cap was reached.")
    else:
        work_item = _first_work_item_id(paths)
        _emit(message_callback, f"Creating batch state for {work_item}.")
        result = create_code_task_batch(root, work_item_id=work_item)
        _record(steps, "batch", "done", f"{result.attempt_id}/{result.batch_id} for {result.work_item_id}")
    if to_step == "batch":
        return _result(paths, steps, "stop_point", "Stopped after batch as requested.")

    manifest = load_code_task_manifest(root)
    plan_status = _plan_status(manifest)
    if plan_status in {"approved", "rejected", "revision_requested", "pending_approval"}:
        _record(steps, "plan", "skipped", f"plan status is {plan_status}")
    elif dry_run:
        return _dry_result(paths, steps, "plan", "generate patch_plan.md")
    elif _cost_cap_exceeded(paths.meta_dir, cost_cap_usd):
        return _result(paths, steps, "cost_cap_exceeded", "LLM cost cap reached before patch planning.")
    else:
        _emit(message_callback, "Generating patch plan.")
        try:
            result = generate_patch_plan(
                root,
                llm_client=llm_client,
                model=planner_model or model,
                use_llm=use_llm,
                allow_llm_fallback=allow_planning_fallback,
                llm_retry_attempts=llm_retry_attempts,
                max_files=max_files,
                max_source_chars_per_file=max_source_chars_per_file,
                message_callback=message_callback,
            )
        except LLMError as exc:
            _record(steps, "plan", "blocked", _first_error_line(str(exc)))
            return _result(
                paths,
                steps,
                "llm_planning_failed",
                (
                    "LLM patch planning failed and no offline fallback was written. "
                    "Rerun the same execute command to retry, use --no-llm for a "
                    "deterministic plan, or pass --allow-planning-fallback if an "
                    "offline fallback is acceptable."
                ),
            )
        _record(steps, "plan", "done", f"mode {result.mode}; pending approval")
        _memory_event(
            root,
            "patch_plan",
            f"Generated patch plan in {result.mode} mode; waiting for review.",
            status="pending_approval",
            artifacts=[_relative_to_run(root, result.patch_plan_path)],
            metadata={"selected_files": list(result.selected_files)},
        )
        plan_status = "pending_approval"
    if plan_status == "pending_approval" and approval_note.strip() and not dry_run:
        record_plan_decision(root, decision="approve", note=approval_note, reviewer="research-application")
        plan_status = "approved"
    if plan_status == "pending_approval":
        return _result(
            paths,
            steps,
            "approval_required",
            "Review code_task/patch_plan.md, then run code-task decide-plan --decision approve.",
        )
    if plan_status == "revision_requested":
        return _result(paths, steps, "plan_revision_requested", "Revise the task or regenerate the plan.")
    if plan_status == "rejected":
        return _result(paths, steps, "plan_rejected", "Stop this run or create a new plan.")

    if to_step == "plan":
        return _result(paths, steps, "stop_point", "Stopped after plan as requested.")

    manifest = load_code_task_manifest(root)
    patch_status = _patch_status(manifest)
    proposal_exists = _proposal_exists(paths)
    patch_recovery = proposal_exists and use_llm and _patch_validation_recovery_needed(root)
    if patch_status == "applied":
        _record(steps, "propose-edits", "skipped", "patch already applied")
    elif proposal_exists and _proposal_edit_count(paths) > 0 and not patch_recovery:
        _record(steps, "propose-edits", "skipped", "proposed_edits.json already exists")
    elif _plan_status(manifest) != "approved":
        return _result(paths, steps, "approval_required", "Approve the patch plan before proposing edits.")
    elif dry_run:
        return _dry_result(paths, steps, "propose-edits", "generate controlled edit proposal")
    elif _cost_cap_exceeded(paths.meta_dir, cost_cap_usd):
        return _result(paths, steps, "cost_cap_exceeded", "LLM cost cap reached before edit proposal.")
    else:
        if patch_recovery:
            _emit(message_callback, "Regenerating edit proposal after patch validation failure.")
        elif proposal_exists:
            _emit(message_callback, "Regenerating empty edit proposal with refreshed context.")
        _ensure_context_pack_for_current_batch(
            root,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
            message_callback=message_callback,
        )
        _emit(message_callback, "Generating controlled edit proposal.")
        result = propose_patch_edits(
            root,
            llm_client=llm_client,
            model=editor_model or model,
            use_llm=use_llm,
            force=proposal_exists,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
            allow_large_edits=allow_large_edits,
            budget_profile=budget_profile,
            edit_budget_overrides=edit_budget_overrides,
            message_callback=message_callback,
        )
        if patch_recovery:
            record_review_finding(
                root,
                {
                    "key": "apply-edits-validation-recovery-completed",
                    "severity": "info",
                    "category": "patch_validation",
                    "summary": "A corrected edit proposal was generated using the recorded failure and current source context.",
                    "evidence": [
                        "code_task/meta/proposed_edits.json",
                        "code_task/memory/review_findings.jsonl",
                    ],
                    "recommendation": "Use exact current source snippets; stop if the corrected proposal fails validation again.",
                    "source": "code-task.execute",
                },
            )
        _record(steps, "propose-edits", "done", f"mode {result.mode}; edits {result.edit_count}")
        _memory_event(
            root,
            "edit_proposal",
            f"Generated controlled edit proposal with {result.edit_count} edit(s) in {result.mode} mode.",
            status="proposal_ready" if result.edit_count else "empty",
            artifacts=[_relative_to_run(root, result.proposal_path)],
            metadata={"selected_files": list(result.selected_files)},
        )
        if result.edit_count == 0:
            proposal = read_json(result.proposal_path)
            budget_status = proposal.get("budget", {}).get("status", "")
            if budget_status in {"rejected_absolute", "rejected_large"}:
                detail = "; ".join(str(item) for item in proposal.get("warnings", []))
                detail = detail or f"Edit proposal rejected by {budget_status}."
                record_review_finding(root, {
                    "key": "edit-proposal-budget-rejected", "severity": "blocking",
                    "category": "patch_validation", "summary": detail,
                    "evidence": ["code_task/meta/proposed_edits.json"],
                    "recommendation": "Use focused exact-source edits within the existing limits; do not expand scope or rewrite the whole file.",
                    "source": "code-task.execute",
                })
                return _result(paths, steps, "edit_budget_rejected", detail)
            reasons = [str(item) for item in proposal.get("validation", []) if str(item).strip()]
            detail = str(proposal.get("summary") or "No edits were proposed.")
            if reasons:
                detail += " " + "; ".join(reasons)
            return _result(
                paths,
                steps,
                "no_edits_proposed",
                detail + " See code_task/meta/proposed_edits.json. "
                + ("Resolve the reported implementation/design blocker before retrying."
                   if result.mode == "llm" else "Provide edits or enable the LLM editor."),
            )
        if not (approval_note.strip() and apply_proposed_edits):
            return _result(
                paths,
                steps,
                "proposal_review_required",
                "Review code_task/meta/proposed_edits.json, then rerun execute with --apply-proposed-edits.",
            )

    if to_step == "propose-edits":
        return _result(paths, steps, "stop_point", "Stopped after propose-edits as requested.")

    manifest = load_code_task_manifest(root)
    if _patch_status(manifest) == "applied":
        _record(steps, "apply-edits", "skipped", "patch already applied")
    elif not _proposal_exists(paths):
        return _result(paths, steps, "missing_proposal", "Generate or provide proposed_edits.json first.")
    elif not apply_proposed_edits:
        return _result(
            paths,
            steps,
            "proposal_review_required",
            "Review code_task/meta/proposed_edits.json, then rerun execute with --apply-proposed-edits.",
        )
    elif dry_run:
        return _dry_result(paths, steps, "apply-edits", "apply reviewed proposed edits")
    else:
        _emit(message_callback, "Applying reviewed edit proposal.")
        try:
            result = apply_patch_edits(root, allow_large_edits=allow_large_edits)
        except (PatchValidationError, EditBudgetApprovalRequired) as exc:
            if isinstance(exc, EditBudgetApprovalRequired):
                key, category, stop_reason = "apply-edits-large-edit-approval-required", "edit_budget", "large_edit_approval_required"
                recommendation = "Confirm the broader edit is intentional before rerunning with large-edit approval."
                next_action = "Review the larger proposal, then rerun with --allow-large-edits if it is intentional."
            else:
                key, category, stop_reason = "apply-edits-validation-failed", "patch_validation", "patch_apply_failed"
                recommendation = "Review and regenerate the proposed edit JSON before applying it."
                next_action = "Review code_task/meta/proposed_edits.json; the rejected patch was not committed."
            detail = _first_error_line(str(exc))
            restoration_errors = getattr(exc, "__notes__", ())
            if restoration_errors:
                key, category, stop_reason = "apply-edits-rollback-failed", "rollback", "patch_rollback_failed"
                detail += " " + "; ".join(restoration_errors)
                recommendation = "Inspect the snapshot and current workspace before proposing or applying any new edits."
                next_action = recommendation
            _record(steps, "apply-edits", "blocked", detail)
            record_review_finding(
                root,
                {
                    "key": key,
                    "severity": "blocking",
                    "category": category,
                    "summary": detail,
                    "evidence": ["code_task/meta/proposed_edits.json"],
                    "recommendation": recommendation,
                    "source": "code-task.execute",
                },
            )
            return _result(paths, steps, stop_reason, next_action)
        _record(steps, "apply-edits", "done", f"changed {len(result.changed_files)} file(s)")
        record_edit_history(
            root,
            changed_files=list(result.changed_files),
            reason=f"Applied reviewed edit proposal to {len(result.changed_files)} file(s).",
            proposal="code_task/meta/proposed_edits.json",
            patch_diff=_relative_to_run(root, result.patch_diff_path),
            metadata={"applied_edits": _relative_to_run(root, result.applied_edits_path)},
        )

    if to_step == "apply-edits":
        return _result(paths, steps, "stop_point", "Stopped after apply-edits as requested.")

    if _patch_status(load_code_task_manifest(root)) != "applied":
        return _result(paths, steps, "patch_not_applied", "Apply reviewed edits before review.")
    if dry_run and not _review_report_exists(paths, "post_apply"):
        return _dry_result(paths, steps, "review", "review applied patch for scope, interface, and risk")
    if review_patch("post_apply") == "failed":
        return _result(paths, steps, "review_failed",
                       "Review code_task/meta/review_report.json before validation or benchmark execution.")
    if to_step == "review":
        return _result(paths, steps, "stop_point", "Stopped after review as requested.")

    if _patch_status(load_code_task_manifest(root)) != "applied":
        return _result(paths, steps, "patch_not_applied", "Apply reviewed edits before validation.")
    if dry_run:
        return _dry_result(paths, steps, "validate", "run static validation")
    _emit(message_callback, "Running static validation.")
    validation = validate_code_task(
        root,
        strict=strict_validation,
        max_file_bytes=validation_max_file_bytes,
    )
    _record(steps, "validate", "done", f"status {validation.status}")
    _memory_event(
        root,
        "validation",
        f"Static validation finished with status {validation.status}.",
        status=validation.status,
        artifacts=["code_task/meta/validation_report.json"],
        metadata={"error_count": validation.error_count, "warning_count": validation.warning_count},
    )
    if validation.error_count or validation.warning_count:
        severity = "blocking" if validation.error_count else "warning"
        record_review_finding(
            root,
            {
                "key": f"static-validation-{validation.status}",
                "severity": severity,
                "category": "static_validation",
                "summary": (
                    f"Static validation reported {validation.error_count} error(s) "
                    f"and {validation.warning_count} warning(s)."
                ),
                "evidence": ["code_task/meta/validation_report.json"],
                "recommendation": "Inspect validation_report.json before running or repairing the benchmark.",
                "source": "code-task.validate",
            },
        )
    if validation.status == "failed":
        return _handle_failure(
            root,
            paths,
            steps,
            to_step=to_step,
            failed_step="validate",
            repair_rounds=repair_rounds,
            max_batches=max_batches,
            cost_cap_usd=cost_cap_usd,
            llm_client=llm_client,
            model=model,
            repair_model=repair_model,
            use_llm=use_llm,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
            message_callback=message_callback,
        )

    if to_step == "validate":
        return _result(paths, steps, "stop_point", "Stopped after validate as requested.")

    if _patch_status(load_code_task_manifest(root)) != "applied":
        return _result(paths, steps, "patch_not_applied", "Apply reviewed edits before running benchmark.")
    _emit(message_callback, "Running patched benchmark.")
    result = run_code_task_benchmark(
        root,
        timeout_sec=timeout_sec,
        # The execute path runs static validation immediately before this
        # benchmark step, so avoid recording the same validation twice.
        skip_validation=True,
        env_mode=env_mode,
        python_executable=python_executable,
        stream_output=stream_benchmark_output,
        output_callback=_benchmark_output_callback(message_callback),
        budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
    )
    _record(steps, "run", "done", f"status {result.status}")
    _memory_event(
        root,
        "patched_run",
        f"Patched benchmark finished with status {result.status}.",
        status=result.status,
        artifacts=[
            _relative_to_run(root, result.report_path),
            _relative_to_run(root, result.metrics_path),
        ],
        metadata={"metrics": result.metrics},
    )
    if result.status != "passed":
        return _handle_failure(
            root,
            paths,
            steps,
            to_step=to_step,
            failed_step="run",
            repair_rounds=repair_rounds,
            max_batches=max_batches,
            cost_cap_usd=cost_cap_usd,
            llm_client=llm_client,
            model=model,
            repair_model=repair_model,
            use_llm=use_llm,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
            message_callback=message_callback,
        )
    if review_patch("post_run") == "failed":
        return _result(paths, steps, "review_failed",
                       "Review code_task/meta/review_report_post_run.json before treating the patch as complete.")

    if to_step == "run":
        return _result(paths, steps, "completed", "Review code_task/summary.md and patch.diff.")

    return _result(paths, steps, "completed", "Review code_task/summary.md.")


def implementation_resume_allowed(run_dir: Path, *, ledger: BudgetLedger, attempt_id: str) -> bool:
    """Recognize committed CodeTask gates; never restart an uncommitted request."""
    from simple_ar.code_task.runtime.state import implementation_file_hashes, native_preparation_processes
    try:
        manifest = load_code_task_manifest(run_dir)
        paths = code_task_paths(run_dir)
        if not manifest.get('code_task', {}).get('native_binding'):
            return False
        entries = [row for row in ledger.entries if row.attempt_id == attempt_id]
        if any(row.status not in {'settled', 'released'} for row in entries):
            return False
        preparation_ids = native_preparation_processes(run_dir, ledger=ledger,
            session_id='', attempt_id=attempt_id)
        # The physical owner must be checked even when a changed/missing ledger
        # attempt leaves no matching entries. Candidate hashes alone cannot
        # establish that an existing invocation is safe to resume.
        from simple_ar.code_task.execution.runner import recover_implementation_process
        recovered = recover_implementation_process(run_dir, budget_ledger=ledger,
            session_id='', attempt_id=attempt_id)
        for entry in entries:
            if entry.status == 'released':
                continue
            label = entry.purpose
            if label.startswith('code-task-plan'):
                ready = _plan_status(manifest) in {'approved', 'pending_approval', 'rejected', 'revision_requested'} and (paths.task_dir / 'patch_plan.md').is_file()
            elif label.startswith('code-task-propose-edits'):
                ready = _proposal_exists(paths)
            elif label.startswith('code-task-review-post_apply'):
                ready = (paths.meta_dir / 'review_report.json').is_file()
            elif label.startswith('code-task-repair'):
                ready = bool(manifest.get('repair', {}).get('latest_proposed_edits'))
            elif label.startswith('code-task-review-post_repair'):
                ready = (paths.meta_dir / 'review_report_post_repair.json').is_file()
            elif label.startswith(('greenfield-architecture', 'greenfield-unit-', 'greenfield-code-review-')):
                # Resume only a completely published/reviewed generation.
                # Individual completed units in an unpublished staging tree
                # do not authorize regenerating or replaying earlier calls.
                ready = (manifest.get('code_task', {}).get('kind') == 'greenfield'
                    and manifest.get('implementation', {}).get('status') == 'generated'
                    and all((paths.meta_dir / name).is_file() for name in (
                        'architecture_plan.json', 'task_contract.json', 'code_artifacts.json',
                        'implementation_plan.json', 'review_report.json'))
                    and is_current_greenfield_review(read_json(paths.meta_dir / 'review_report.json')))
            elif label == 'local_process':
                if entry.logical_call_id in preparation_ids:
                    continue  # The shared owner already verified its actual preparation evidence.
                reports = [paths.run_artifact_dir / 'patched' / 'execution_report.json',
                    *(paths.run_artifact_dir / 'patched' / 'attempts').glob('*/execution_report.json')]
                ready = any(path.is_file() and read_json(path).get('process', {}).get('invocation_id') == entry.logical_call_id
                            for path in reports)
                if not ready:
                    ready = recovered is not None and recovered.record['invocation_id'] == entry.logical_call_id
            else:
                ready = False  # A new label needs evidence, not a permissive replay default.
            if not ready:
                return False
        current = implementation_file_hashes(run_dir)
        report = paths.run_artifact_dir / 'patched' / 'execution_report.json'
        indexed = {row['path']: row['sha256'] for row in read_json(paths.meta_dir / 'codebase_index.json')['files']}
        extra = set(current) - set(indexed)
        if extra:
            # Explicit paths omitted by the browsing index still need committed
            # application evidence; do not bless arbitrary bytes on recovery.
            receipts = [paths.meta_dir / 'applied_edits.json', *paths.task_dir.glob('batches/*/applied_edits.json')]
            for receipt in receipts:
                if receipt.is_file():
                    for edit in read_json(receipt).get('edits', []):
                        if edit.get('path') in extra and edit.get('new_sha256') == current[edit['path']]:
                            indexed[edit['path']] = edit['new_sha256']
        if report.is_file() and read_json(report).get('implementation_file_hashes') == current:
            return True
        return current == indexed
    except (OSError, KeyError, IndexError, TypeError, ValueError, RuntimeError):
        return False


def implement_code_task(
    run_dir: Path, *, approval_note: str, proposal_corrections: int = 0, **options: Any,
) -> CodeTaskExecuteResult:
    """Run the existing preparation gates through validation, never a benchmark.

    The caller supplies explicit isolated-workspace approval and owns measurement.
    Rejected plans remain rejected. An explicitly requested single correction
    only replaces a completed patch/budget rejection, never a transport failure,
    rejected plan, runtime failure or unknown-use request. Limits stay unchanged.
    """
    if not approval_note.strip():
        raise ValueError("CodeTask implementation requires an explicit approval note.")
    if type(proposal_corrections) is not int or proposal_corrections not in (0, 1):
        raise ValueError("proposal_corrections must be zero or one")
    settings = {
        **options, "baseline_policy": "skip", "to_step": "validate",
        "apply_proposed_edits": True, "approval_note": approval_note,
    }
    first = execute_code_task(run_dir, **settings)
    if not proposal_corrections or first.stop_reason not in {"patch_apply_failed", "edit_budget_rejected"}:
        return first
    _emit(options.get("message_callback"),
          "Edit proposal failed patch or budget validation; retrying once with recorded failure context and unchanged limits.")
    second = execute_code_task(run_dir, **settings)
    return CodeTaskExecuteResult(second.run_dir, (*first.steps, *second.steps),
                                 second.stop_reason, second.next_action, second.summary_path)


def _execute_greenfield_code_task(
    root: Path,
    paths: object,
    steps: list[ExecuteStepRecord],
    *,
    to_step: str,
    dry_run: bool,
    llm_client: LLMClient | None = None,
    model: str | None,
    planner_model: str | None,
    writer_model: str | None,
    reviewer_model: str | None,
    repair_model: str | None,
    use_llm: bool,
    timeout_sec: int,
    skip_validation: bool,
    env_mode: str | None,
    python_executable: str | Path | None,
    strict_validation: bool,
    validation_max_file_bytes: int,
    stream_benchmark_output: bool | str,
    max_files: int,
    max_source_chars_per_file: int,
    max_generated_lines: int,
    allow_planning_fallback: bool,
    planning_mode: str,
    planning_review_rounds: int,
    llm_retry_attempts: int,
    repair_rounds: int,
    planning_snapshot_from: str | Path | None,
    review_gate: str,
    budget_profile: str | None,
    edit_budget_overrides: dict[str, Any] | None,
    allow_large_edits: bool,
    implementation_provider: str,
    implementation_agent_mode: str,
    implementation_allow_external_agent: bool,
    implementation_agent_model: str,
    implementation_agent_binary: str,
    implementation_agent_args: tuple[str, ...],
    implementation_agent_timeout_sec: int,
    message_callback: MessageCallback | None,
    budget_ledger: BudgetLedger | None,
    session_id: str,
    attempt_id: str,
) -> CodeTaskExecuteResult:
    """Execute the unified greenfield code-task path.

    Greenfield runs share the code-task runtime, memory, reviewer, validation,
    runner, and summary artifacts. The mode-specific part is limited to
    generating an implementation inside the empty workspace.
    """

    def resolve_review(review: Mapping[str, Any] | Path, *, summary: str, gate: str = review_gate) -> bool:
        """Apply the same review policy to newly generated and resumed code."""
        if not _greenfield_review_should_block(review, review_gate=gate):
            _emit(message_callback, "Generated project review has non-runtime blockers; continuing to validation/run.")
            _record(steps, "review-gate", "warning", "review gate runtime mode allowed benchmark execution")
            return True
        return _attempt_greenfield_review_repair(
            root, paths, steps, llm_client=llm_client, model=model,
            repair_model=repair_model or model, use_llm=use_llm,
            repair_rounds=repair_rounds, max_files=max_files,
            max_generated_lines=max_generated_lines,
            message_callback=message_callback, summary=summary,
        )

    if _environment_report_exists(paths):
        _record(steps, "probe", "skipped", "environment_report.json already exists")
    elif dry_run:
        return _dry_result(paths, steps, "probe", "record environment and resource signals")
    else:
        _emit(message_callback, "Running environment and resource probe.")
        result = probe_code_task_environment(
            root,
            env_mode=env_mode,
            python_executable=python_executable,
        )
        _record(steps, "probe", "done", f"wrote {result.report_path}")
        _memory_event(
            root,
            "probe",
            "Recorded environment and resource signals for greenfield generation.",
            status="done",
            artifacts=[
                _relative_to_run(root, result.report_path),
                "code_task/meta/resource_probe.json",
                "code_task/meta/resource_decision.json",
            ],
        )
    if to_step == "probe":
        return _result(paths, steps, "stop_point", "Stopped after probe as requested.")

    _record(steps, "baseline", "skipped", "greenfield mode has no unchanged baseline")
    if to_step == "baseline":
        return _result(paths, steps, "stop_point", "Stopped after baseline as requested.")

    manifest = load_code_task_manifest(root)
    implementation = manifest.get("implementation")
    generated = isinstance(implementation, dict) and implementation.get("status") == "generated"
    if generated:
        _record(steps, "work-plan", "skipped", "greenfield implementation already generated")
    elif dry_run:
        return _dry_result(paths, steps, "work-plan", "plan and generate greenfield project")
    else:
        _emit(message_callback, "Planning and generating greenfield project.")
        result = generate_greenfield_code_task(
            root,
            llm_client=llm_client,
            model=model,
            planner_model=planner_model or model,
            writer_model=writer_model or model,
            reviewer_model=reviewer_model or model,
            use_llm=use_llm,
            max_files=max_files,
            max_generated_lines=max_generated_lines,
            max_source_chars_per_file=max_source_chars_per_file,
            allow_planning_fallback=allow_planning_fallback,
            planning_mode=planning_mode,
            planning_review_rounds=planning_review_rounds,
            llm_retry_attempts=llm_retry_attempts,
            implementation_provider=implementation_provider,
            implementation_agent_mode=implementation_agent_mode,
            implementation_allow_external_agent=implementation_allow_external_agent,
            implementation_agent_model=implementation_agent_model,
            implementation_agent_binary=implementation_agent_binary,
            implementation_agent_args=implementation_agent_args,
            implementation_agent_timeout_sec=implementation_agent_timeout_sec,
            planning_snapshot_from=planning_snapshot_from,
            message_callback=message_callback,
            budget_profile=budget_profile, edit_budget_overrides=edit_budget_overrides,
            allow_large_edits=allow_large_edits,
        )
        _record(
            steps,
            "work-plan",
            "done",
            f"generated {len(result.generated_files)} file(s); review {result.review_status}",
        )
        _memory_event(
            root,
            "greenfield_generation",
            f"Generated greenfield project with {len(result.generated_files)} file(s).",
            status=result.review_status,
            artifacts=[
                _relative_to_run(root, result.implementation_plan_path),
                _relative_to_run(root, result.code_artifacts_path),
                _relative_to_run(root, result.review_report_path),
            ],
            metadata={"generated_files": list(result.generated_files)},
        )
        _record_review_report_findings(root, result.review_report_path)
        if result.review_status == "failed":
            if not resolve_review(
                result.review_report_path,
                summary="Repaired generated project after review failure and passed deterministic rereview.",
            ):
                return _result(paths, steps, "review_failed",
                               "Review code_task/meta/review_report.json before validation or execution.")
    if to_step == "work-plan":
        return _result(paths, steps, "stop_point", "Stopped after greenfield generation as requested.")

    for skipped_step in ("batch", "plan", "propose-edits", "apply-edits"):
        _record(steps, skipped_step, "skipped", "greenfield mode uses generated implementation artifacts")
        if to_step == skipped_step:
            return _result(paths, steps, "stop_point", f"Stopped after {skipped_step} as requested.")

    review_path = paths.meta_dir / "review_report.json"
    if review_path.is_file():
        review = read_json(review_path)
        if not isinstance(review, dict) or not is_current_greenfield_review(review):
            _emit(message_callback, "Review contract changed; rerunning generated project review.")
            review = _rerun_greenfield_review(
                root,
                paths,
                max_files=max_files,
                max_generated_lines=max_generated_lines,
            )
            _record(steps, "review", "done", f"refreshed status {review.get('status', 'unknown')}")
        else:
            _record(steps, "review", "skipped", "greenfield review_report.json is current")
        if isinstance(review, dict) and review.get("status") == "failed":
            if not resolve_review(
                review,
                summary="Repaired existing generated project after review failure and passed deterministic rereview.",
            ):
                return _result(paths, steps, "review_failed",
                               "Review code_task/meta/review_report.json before validation or execution.")
    elif dry_run:
        return _dry_result(paths, steps, "review", "review generated project")
    else:
        return _result(paths, steps, "review_missing", "Generate the greenfield project before review.")
    if to_step == "review":
        return _result(paths, steps, "stop_point", "Stopped after review as requested.")

    if dry_run:
        return _dry_result(paths, steps, "validate", "run static validation")
    _emit(message_callback, "Running static validation.")
    validation = validate_code_task(
        root,
        strict=strict_validation,
        max_file_bytes=validation_max_file_bytes,
    )
    _record(steps, "validate", "done", f"status {validation.status}")
    _memory_event(
        root,
        "validation",
        f"Static validation finished with status {validation.status}.",
        status=validation.status,
        artifacts=["code_task/meta/validation_report.json"],
        metadata={"error_count": validation.error_count, "warning_count": validation.warning_count},
    )
    if validation.status == "failed":
        return _result(
            paths,
            steps,
            "validation_failed",
            "Review code_task/meta/validation_report.json before running.",
        )
    write_code_task_summary(root)
    if to_step == "validate":
        return _result(paths, steps, "stop_point", "Stopped after validate as requested.")

    after_repair = False
    while True:
        _emit(message_callback, "Running generated project benchmark after repair." if after_repair else "Running generated project benchmark.")
        result = run_code_task_benchmark(
            root,
            timeout_sec=timeout_sec,
            # Static validation just ran in this execute path unless the user
            # stopped earlier, so do not duplicate it inside the runner.
            skip_validation=True,
            run_label="patched",
            env_mode=env_mode,
            python_executable=python_executable,
            stream_output=stream_benchmark_output,
            output_callback=_benchmark_output_callback(message_callback),
            budget_ledger=budget_ledger, session_id=session_id, attempt_id=attempt_id,
        )
        _record(steps, "run", "done", f"status {result.status}")
        _memory_event(
            root,
            "generated_run_repair" if after_repair else "generated_run",
            f"Generated project benchmark{' after repair' if after_repair else ''} finished with status {result.status}.",
            status=result.status,
            artifacts=[
                _relative_to_run(root, result.report_path),
                _relative_to_run(root, result.metrics_path),
                *(["code_task/meta/run_repair.json"] if after_repair else []),
            ],
            metadata={"metrics": result.metrics},
        )
        if result.status == "passed":
            break
        repaired = _attempt_greenfield_run_repair(
            root,
            paths,
            steps,
            repair_rounds=repair_rounds,
            llm_client=llm_client,
            model=model,
            repair_model=repair_model or model,
            use_llm=use_llm,
            message_callback=message_callback,
        )
        if not repaired:
            return _result(paths, steps, "benchmark_failed", "Review code_task/run/patched/ for generated project failure.")
        review = _rerun_greenfield_review(
            root,
            paths,
            max_files=max_files,
            max_generated_lines=max_generated_lines,
        )
        _record(steps, "review", "done", f"post-run-repair status {review.get('status', 'unknown')}")
        if isinstance(review, dict) and review.get("status") == "failed":
            if not resolve_review(
                review, gate="strict",
                summary="Repaired generated project after run repair introduced review-blocking contract issues.",
            ):
                return _result(
                    paths,
                    steps,
                    "review_failed",
                    "Review code_task/meta/review_report.json after generated project run repair.",
                )
        _emit(message_callback, "Run repair patched generated project; rerunning static validation.")
        validation = validate_code_task(
            root,
            strict=strict_validation,
            max_file_bytes=validation_max_file_bytes,
        )
        _record(steps, "validate", "done", f"post-repair status {validation.status}")
        if validation.status == "failed":
            return _result(
                paths,
                steps,
                "validation_failed",
                "Review code_task/meta/validation_report.json after generated project repair.",
            )
        write_code_task_summary(root)
        after_repair = True
    write_code_task_summary(root)
    if to_step == "run":
        return _result(paths, steps, "completed", "Review code_task/summary.md and generated_project/.")

    return _result(paths, steps, "completed", "Review code_task/summary.md.")


def _handle_failure(
    run_dir: Path,
    paths: object,
    steps: list[ExecuteStepRecord],
    *,
    to_step: str,
    failed_step: str,
    repair_rounds: int,
    max_batches: int | None,
    cost_cap_usd: float | None,
    llm_client: LLMClient | None = None,
    model: str | None,
    repair_model: str | None,
    use_llm: bool,
    max_files: int,
    max_source_chars_per_file: int,
    message_callback: MessageCallback | None,
) -> CodeTaskExecuteResult:
    # Both callers have already stopped dry runs before validation/execution.
    # Failure analysis and repair share one boundary, independent of the source.
    if to_step not in {"analyze-failure", "repair"}:
        if failed_step == "validate":
            return _result(paths, steps, "validation_failed",
                           "Review code_task/meta/validation_report.json or run execute --to-step analyze-failure.")
        return _result(paths, steps, "benchmark_failed",
                       "Review code_task/run/patched/ or run execute --to-step analyze-failure.")
    _emit(message_callback, "Analyzing latest failure.")
    analysis = analyze_code_task_failure(run_dir)
    _record(steps, "analyze-failure", "done", f"source {analysis.source}; status {analysis.status}")
    analysis_text = read_text(analysis.analysis_path) if analysis.analysis_path.is_file() else ""
    repair_index = _repair_count(load_code_task_manifest(run_dir))
    record_repair_memory(
        run_dir,
        failure_summary=_failure_summary_for_memory(analysis_text)
        or f"Failure analysis status {analysis.status} from {analysis.source}.",
        status=analysis.status,
        artifacts=[_relative_to_run(run_dir, analysis.analysis_path)],
        metadata={
            "source": analysis.source,
            "failure_signature": _failure_signature(analysis_text),
            "repair_count": repair_index,
        },
        key=f"failure-analysis:{analysis.source}:{analysis.status}:{repair_index}:{_failure_signature(analysis_text)[:40]}",
    )
    if to_step != "repair" or repair_rounds <= _repair_count(load_code_task_manifest(run_dir)):
        return _result(
            paths,
            steps,
            "failure_analyzed",
            "Review failure_analysis.md. Use --to-step repair and --repair-rounds to request a bounded repair proposal.",
        )
    if _batch_cap_exceeded(run_dir, max_batches):
        return _result(
            paths,
            steps,
            "batch_budget_exceeded",
            "Configured batch cap was reached before creating a repair batch.",
        )
    if _cost_cap_exceeded(paths.meta_dir, cost_cap_usd):
        return _result(
            paths,
            steps,
            "cost_cap_exceeded",
            "LLM cost cap reached before repair proposal.",
        )
    repair_batch = _create_repair_batch(run_dir)
    _emit(message_callback, "Generating bounded repair proposal.")
    repair = propose_repair_edits(
        run_dir,
        llm_client=llm_client,
        model=repair_model or model,
        use_llm=use_llm,
        max_files=max_files,
        max_source_chars_per_file=max_source_chars_per_file,
        message_callback=message_callback,
    )
    if repair_batch is not None:
        update_code_task_batch_state(
            run_dir,
            repair_batch.batch_state_path,
            state="proposal_ready" if repair.edit_count else "failed",
            artifacts={"repair_proposal": _relative_to_run(run_dir, repair.proposal_path)},
            detail="Repair proposal generated for failed validation or benchmark evidence.",
            extra={"repair_edit_count": repair.edit_count},
        )
    _record(steps, "repair", "done", f"mode {repair.mode}; edits {repair.edit_count}")
    record_repair_memory(
        run_dir,
        failure_summary="Generated bounded repair proposal after failed validation or benchmark.",
        attempted_fix=f"Repair proposal contains {repair.edit_count} edit(s) in {repair.mode} mode.",
        status="proposal_ready" if repair.edit_count else "empty",
        artifacts=[_relative_to_run(run_dir, repair.proposal_path)],
        metadata={"selected_files": list(repair.selected_files)},
        key=f"repair-proposal:{_relative_to_run(run_dir, repair.proposal_path)}",
    )
    return _result(
        paths,
        steps,
        "repair_review_required",
        "Review the repair proposal, then apply it explicitly with code-task apply-edits --edits-file.",
    )


def _record(steps: list[ExecuteStepRecord], step: str, status: str, detail: str) -> None:
    steps.append(ExecuteStepRecord(step=step, status=status, detail=detail))


def _memory_event(
    run_dir: Path,
    event_type: str,
    summary: str,
    *,
    status: str = "",
    artifacts: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    record_code_task_memory_event(
        run_dir,
        event_type=event_type,
        summary=summary,
        status=status,
        artifacts=artifacts or [],
        metadata=metadata or {},
        key=f"{event_type}:{status}:{summary}",
    )


def _dry_result(paths: object, steps: list[ExecuteStepRecord], step: str, detail: str) -> CodeTaskExecuteResult:
    _record(steps, step, "would_run", detail)
    return _result(paths, steps, "dry_run", "Dry run only; no artifacts were written.")


def _result(
    paths: object,
    steps: list[ExecuteStepRecord],
    stop_reason: str,
    next_action: str,
) -> CodeTaskExecuteResult:
    return CodeTaskExecuteResult(
        run_dir=paths.run_dir,
        steps=tuple(steps),
        stop_reason=stop_reason,
        next_action=next_action,
        summary_path=paths.task_dir / "summary.md",
    )


def _environment_report_exists(paths: object) -> bool:
    return (paths.meta_dir / "environment_report.json").is_file()


def _proposal_exists(paths: object) -> bool:
    return (paths.meta_dir / "proposed_edits.json").is_file()


def _proposal_edit_count(paths: object) -> int:
    proposal_path = paths.meta_dir / "proposed_edits.json"
    if not proposal_path.is_file():
        return 0
    try:
        proposal = read_json(proposal_path)
    except Exception:
        return 0
    if not isinstance(proposal, dict):
        return 0
    edits = proposal.get("edits")
    return len(edits) if isinstance(edits, list) else 0


def _patch_validation_recovery_needed(run_dir: Path) -> bool:
    """Allow one model correction after exact-text or edit-budget rejection."""

    findings_path = code_task_memory_paths(run_dir).review_findings_jsonl
    if not findings_path.is_file():
        return False
    try:
        findings = read_jsonl(findings_path)
    except (OSError, ValueError):
        return False
    keys = {str(row.get("key") or "") for row in findings if isinstance(row, dict)}
    # Legacy 'recovery' findings record a start, not a generated proposal.
    return bool(keys & {"apply-edits-validation-failed", "edit-proposal-budget-rejected"}) and "apply-edits-validation-recovery-completed" not in keys


def _ensure_context_pack_for_current_batch(
    run_dir: Path,
    *,
    max_files: int,
    max_source_chars_per_file: int,
    message_callback: MessageCallback | None,
) -> None:
    loaded = load_latest_code_task_context_pack(run_dir)
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    latest_batch = load_latest_code_task_batch(run_dir)
    batch = latest_batch.state if latest_batch is not None else {}
    work_item = batch.get("work_item", {})
    if isinstance(work_item, dict) and isinstance(work_item.get("target_files"), list):
        preferred = [path for path in work_item["target_files"] if isinstance(path, str)]
    else:
        index = read_json(paths.meta_dir / "codebase_index.json")
        known = {row["path"] for row in index.get("files", []) if isinstance(row, dict) and isinstance(row.get("path"), str)}
        plan_path = paths.task_dir / "patch_plan.md"
        preferred = planned_context_paths(read_text(plan_path), known) if plan_path.is_file() else []
    requirements = manifest.get("context_requirements")
    required_paths = (
        {path for path in requirements.get("read_only_paths", []) if isinstance(path, str)}
        if isinstance(requirements, dict) and isinstance(requirements.get("read_only_paths"), list)
        else set()
    )
    read_only_paths = {
        str(row.get("path")) for row in loaded.context_pack.get("selected_files", [])
        if isinstance(row, dict) and row.get("access_role") != "editable"
    } if loaded is not None else set()
    if (loaded is not None and loaded.selected_files and required_paths.issubset(read_only_paths)
            and set(preferred[:max_files]).issubset(loaded.selected_files)):
        _emit(message_callback, f"Using existing code-task context pack: {_relative_to_run(run_dir, loaded.context_pack_path)}")
        return
    query = _batch_context_query(latest_batch.state if latest_batch is not None else {})
    _emit(message_callback, "Building code-task context pack for current batch.")
    context_pack = build_code_task_context_pack(
        run_dir,
        query=query or None,
        top_k=max(8, max_files * 2),
        max_files=max_files,
        max_source_chars_per_file=max_source_chars_per_file,
        max_total_chars=max(max_files * max_source_chars_per_file, max_source_chars_per_file),
        preferred_paths=tuple(preferred),
    )
    if latest_batch is not None:
        update_code_task_batch_state(
            run_dir,
            latest_batch.batch_state_path,
            state="context_ready",
            artifacts={
                "context_pack": _relative_to_run(run_dir, context_pack.context_pack_path),
                "batch_context": _relative_to_run(run_dir, context_pack.prompt_context_path),
            },
            detail="Context pack built before edit proposal.",
        )


def _batch_context_query(batch_state: dict[str, Any]) -> str:
    work_item = batch_state.get("work_item")
    if not isinstance(work_item, dict):
        return ""
    parts: list[str] = []
    for key in ("title", "summary", "description"):
        value = work_item.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    context_request = work_item.get("context_request")
    if isinstance(context_request, dict):
        query = context_request.get("query")
        if isinstance(query, str) and query.strip():
            parts.append(query.strip())
        for key in ("files", "symbols"):
            values = context_request.get(key)
            if isinstance(values, list):
                parts.extend(str(value).strip() for value in values if str(value).strip())
    for key in ("target_files", "read_only_evidence"):
        values = work_item.get(key)
        if isinstance(values, list):
            parts.extend(str(value).strip() for value in values if str(value).strip())
    return "\n".join(dict.fromkeys(parts))


def _review_report_exists(paths: object, phase: str) -> bool:
    name = "review_report.json" if phase == "post_apply" else f"review_report_{phase}.json"
    return (paths.meta_dir / name).is_file()


def _greenfield_repair_available(run_dir: Path, repair_rounds: int, *, phase: str) -> bool:
    if repair_rounds <= 0:
        return False
    manifest = load_code_task_manifest(run_dir)
    return _repair_count(manifest, field=f"{phase}_repair_count") < repair_rounds


def _greenfield_repair_client(
    meta_dir: Path,
    *,
    llm_client: LLMClient | None = None,
    model: str | None,
    use_llm: bool,
    message_callback: MessageCallback | None,
) -> LLMClient | None:
    if not use_llm:
        return None
    try:
        return LLMClient.for_task(
            client=llm_client,
            model=model,
            usage_callback=lambda usage: record_usage(
                meta_dir,
                usage,
                stage="code_task.greenfield_review_repair",
                message_callback=message_callback,
            ),
        )
    except LLMError as exc:
        _emit(message_callback, f"LLM unavailable for review repair; retaining the failed review and original files. {exc}")
        return None




def _attempt_greenfield_review_repair(
    run_dir: Path,
    paths: object,
    steps: list[ExecuteStepRecord],
    *,
    llm_client: LLMClient | None = None,
    model: str | None,
    repair_model: str | None,
    use_llm: bool,
    repair_rounds: int,
    max_files: int,
    max_generated_lines: int,
    message_callback: MessageCallback | None,
    summary: str,
) -> bool:
    if not _greenfield_repair_available(run_dir, repair_rounds, phase="review"):
        _record(steps, "repair", "skipped", "review repair budget exhausted or disabled")
        return False
    _emit(message_callback, "Generated project review failed; attempting bounded review repair.")
    repair = _repair_greenfield_review_failure(
        run_dir,
        paths,
        llm_client=llm_client,
        model=repair_model or model,
        use_llm=use_llm,
        message_callback=message_callback,
    )
    _record(steps, "repair", "done", f"review repair {repair.get('status', 'unknown')}")
    made_changes = bool(repair.get("changed_files") or repair.get("regenerated_files"))
    if repair.get("status") != "patched" and not made_changes:
        return False
    if repair.get("status") == "patched":
        _emit(message_callback, "Review repair patched generated project; rerunning review.")
    else:
        _emit(message_callback, "Review repair made partial progress; refreshing review state.")
    review = _rerun_greenfield_review(
        run_dir,
        paths,
        max_files=max_files,
        max_generated_lines=max_generated_lines,
    )
    _record(steps, "review", "done", f"status {review.get('status', 'unknown')}")
    if review.get("status") == "failed":
        return False
    _memory_event(
        run_dir,
        "greenfield_review_repair",
        summary,
        status=str(review.get("status", "unknown")),
        artifacts=[
            "code_task/meta/review_repair.json",
            "code_task/meta/review_report.json",
        ],
    )
    return True




def _attempt_greenfield_run_repair(
    run_dir: Path,
    paths: object,
    steps: list[ExecuteStepRecord],
    *,
    repair_rounds: int,
    llm_client: LLMClient | None = None,
    model: str | None,
    repair_model: str | None,
    use_llm: bool,
    message_callback: MessageCallback | None,
) -> bool:
    _emit(message_callback, "Analyzing generated project benchmark failure.")
    analysis = analyze_code_task_failure(run_dir)
    _record(steps, "analyze-failure", "done", f"source {analysis.source}; status {analysis.status}")
    if not _greenfield_repair_available(run_dir, repair_rounds, phase="run"):
        _record(steps, "repair", "skipped", "run repair budget exhausted or disabled")
        write_code_task_summary(run_dir)
        return False
    _emit(message_callback, "Attempting bounded generated project run repair.")
    stderr_path = paths.run_artifact_dir / "patched" / "stderr.txt"
    stdout_path = paths.run_artifact_dir / "patched" / "stdout.txt"
    stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
    stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
    runtime_output_text = "\n".join(
        part
        for part in (
            "STDERR:\n" + stderr_text if stderr_text.strip() else "",
            "STDOUT:\n" + stdout_text if stdout_text.strip() else "",
        )
        if part
    )
    previous_context = (
        task_memory_context(run_dir, max_events=14, max_findings=8, max_repairs=8)
    )
    client = _greenfield_repair_client(
        paths.meta_dir,
        llm_client=llm_client,
        model=repair_model or model,
        use_llm=use_llm,
        message_callback=message_callback,
    )
    failure_graph = _read_optional_dict(paths.run_artifact_dir / "patched" / "failure_graph.json")
    repair = repair_generated_project_from_run_failure(
        project_dir=paths.workspace_dir / "generated_project",
        failure_analysis={
            "status": analysis.status,
            "source": analysis.source,
            "analysis": _relative_to_run(run_dir, analysis.analysis_path),
            "failure_graph": "code_task/run/patched/failure_graph.json",
            "failure_graph_data": failure_graph,
            "implicated_files": list(analysis.implicated_files),
        },
        stderr_text=runtime_output_text or stderr_text,
        output_path=paths.meta_dir / "run_repair.json",
        attempt_id=f"round-{_repair_count(load_code_task_manifest(run_dir), field='run_repair_count') + 1}",
        code_artifacts=_read_optional_dict(paths.meta_dir / "code_artifacts.json"),
        architecture_plan=_read_optional_dict(paths.meta_dir / "architecture_plan.json"),
        result_schema=result_schema_from_manifest(load_code_task_manifest(run_dir)),
        contract=_greenfield_contract_for_review(paths),
        dependency_advice=_read_optional_dict(paths.meta_dir / "dependency_advice.json"),
        previous_repair_context=previous_context,
        client=client,
    )
    _record(steps, "repair", "done", f"run repair {repair.get('status', 'unknown')}")
    _update_generated_repair_artifacts(
        paths.meta_dir, paths.workspace_dir / "generated_project", repair,
    )
    repair_section = _record_greenfield_repair_result(run_dir, phase="run", repair=repair)
    write_code_task_summary(run_dir)
    changed_files = _greenfield_repair_changed_files(repair)
    record_repair_memory(
        run_dir,
        failure_summary=_failure_summary_for_memory(runtime_output_text or stderr_text)
        or f"Generated project benchmark failure from {analysis.source}.",
        attempted_fix=_greenfield_repair_attempt_summary(repair),
        status=str(repair.get("status", "unknown")),
        artifacts=[
            _relative_to_run(run_dir, analysis.analysis_path),
            "code_task/meta/run_repair.json",
        ],
        metadata={
            "changed_files": changed_files,
            "stderr_signature": _failure_signature(runtime_output_text or stderr_text),
            "repair_status": repair.get("status", "unknown"),
            "run_repair_count": repair_section["run_repair_count"],
        },
        key=(
            "greenfield-run-repair:"
            f"{repair_section['run_repair_count']}:"
            f"{_failure_signature(runtime_output_text or stderr_text)[:40]}"
        ),
    )
    if repair.get("status") != "patched":
        return False
    _memory_event(
        run_dir,
        "greenfield_run_repair",
        "Patched generated project after benchmark failure.",
        status=str(repair.get("status", "unknown")),
        artifacts=[
            _relative_to_run(run_dir, analysis.analysis_path),
            "code_task/meta/run_repair.json",
        ],
        metadata={"changed_files": changed_files},
    )
    return True


def _repair_greenfield_review_failure(
    run_dir: Path,
    paths: object,
    *,
    llm_client: LLMClient | None = None,
    model: str | None,
    use_llm: bool,
    message_callback: MessageCallback | None,
) -> dict[str, Any]:
    review_path = paths.meta_dir / "review_report.json"
    review = read_json(review_path) if review_path.is_file() else {}
    review = review if isinstance(review, dict) else {}
    repair_path = paths.meta_dir / "review_repair.json"
    manifest = load_code_task_manifest(run_dir)
    code_artifacts = _read_optional_dict(paths.meta_dir / "code_artifacts.json")
    previous_context = (
        task_memory_context(run_dir, max_events=14, max_findings=8, max_repairs=8)
    )
    client = _greenfield_repair_client(
        paths.meta_dir,
        llm_client=llm_client,
        model=model,
        use_llm=use_llm,
        message_callback=message_callback,
    )
    repair = repair_generated_project_from_review(
        project_dir=paths.workspace_dir / "generated_project",
        review_report=review,
        output_path=repair_path,
        attempt_id=f"round-{_repair_count(manifest, field='review_repair_count') + 1}",
        code_artifacts=code_artifacts,
        architecture_plan=_read_optional_dict(paths.meta_dir / "architecture_plan.json"),
        result_schema=result_schema_from_manifest(manifest),
        contract=_greenfield_contract_for_review(paths),
        dependency_advice=_read_optional_dict(paths.meta_dir / "dependency_advice.json"),
        previous_repair_context=previous_context,
        client=client,
    )
    repair_section = _record_greenfield_repair_result(run_dir, phase="review", repair=repair)
    _update_generated_repair_artifacts(
        paths.meta_dir, paths.workspace_dir / "generated_project", repair,
    )
    changed_files = _greenfield_repair_changed_files(repair)
    record_repair_memory(
        run_dir,
        failure_summary=_review_report_summary_for_memory(review),
        attempted_fix=_greenfield_repair_attempt_summary(repair),
        status=str(repair.get("status", "unknown")),
        artifacts=["code_task/meta/review_report.json", "code_task/meta/review_repair.json"],
        metadata={
            "changed_files": changed_files,
            "review_repair_count": repair_section.get("review_repair_count", 0),
            "repair_status": repair.get("status", "unknown"),
        },
        key=f"greenfield-review-repair:{repair_section.get('review_repair_count', 0)}:{_review_signature(review)[:40]}",
    )
    write_code_task_summary(run_dir)
    return repair


def _rerun_greenfield_review(
    run_dir: Path,
    paths: object,
    *,
    max_files: int,
    max_generated_lines: int,
) -> dict[str, Any]:
    manifest = load_code_task_manifest(run_dir)
    code_artifacts = _read_optional_dict(paths.meta_dir / "code_artifacts.json")
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
            for row in build_codebase_index(project)['files']]}
    review = review_generated_project(
        project_dir=paths.workspace_dir / "generated_project",
        code_artifacts=code_artifacts,
        result_schema=result_schema_from_manifest(manifest),
        resource_plan=resource_plan,
        contract=_greenfield_contract_for_review(paths),
        dependency_advice=_read_optional_dict(paths.meta_dir / "dependency_advice.json"),
        implementation_memory=_read_optional_dict(paths.task_dir / "memory" / "implementation_memory.json"),
        architecture_plan=_read_optional_dict(paths.meta_dir / "architecture_plan.json"),
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
    _record_review_report_findings(run_dir, paths.meta_dir / "review_report.json")
    manifest = load_code_task_manifest(run_dir)
    implementation = manifest_section(manifest, "implementation")
    implementation["review_status"] = review.get("status", "unknown")
    implementation["status"] = "generated" if review.get("status") != "failed" else "review_failed"
    implementation["reviewed_at"] = utcnow_iso()
    manifest["implementation"] = implementation
    save_code_task_manifest(run_dir, manifest)
    write_code_task_summary(run_dir)
    return review


def _record_review_report_findings(run_dir: Path, report_path: Path) -> None:
    if not report_path.is_file():
        return
    report = read_json(report_path)
    if not isinstance(report, dict):
        return
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


def _greenfield_review_should_block(review_or_path: Mapping[str, Any] | Path, *, review_gate: str) -> bool:
    review = read_json(review_or_path) if isinstance(review_or_path, Path) else dict(review_or_path)
    if not isinstance(review, dict) or review.get("status") != "failed":
        return False
    if review_gate == "strict":
        return True
    if review_gate != "runtime":
        return True
    findings = review.get("findings")
    if not isinstance(findings, list):
        return True
    blocking = [row for row in findings if isinstance(row, dict) and str(row.get("severity") or "") == "blocking"]
    if not blocking:
        return False
    return any(_review_finding_blocks_runtime(row) for row in blocking)


def _review_finding_blocks_runtime(finding: Mapping[str, Any]) -> bool:
    text = " ".join(
        str(finding.get(key) or "")
        for key in ("category", "summary", "evidence", "recommendation")
    ).lower()
    if not text.strip():
        return True
    return any(marker in text for marker in GREENFIELD_RUNTIME_REVIEW_BLOCKERS)




def _record_greenfield_repair_result(run_dir: Path, *, phase: str, repair: dict[str, Any]) -> dict[str, Any]:
    manifest = load_code_task_manifest(run_dir)
    repair_section = manifest_section(manifest, "repair")
    previous_count = _repair_count(manifest, field=f"{phase}_repair_count")
    repair_section.update(
        {
            "status": repair.get("status", "unknown"),
            f"{phase}_repair_count": previous_count + 1,
            f"latest_{phase}_repair": f"code_task/meta/{phase}_repair.json",
            f"latest_{phase}_repair_at": utcnow_iso(),
            f"latest_{phase}_repair_changed_files": repair.get("changed_files", []),
        }
    )
    manifest["repair"] = repair_section
    save_code_task_manifest(run_dir, manifest)
    return repair_section


def _greenfield_resource_plan(paths: object, *, max_files: int, max_generated_lines: int) -> dict[str, Any]:
    decision = _read_optional_dict(paths.meta_dir / "resource_decision.json")
    return {
        "schema_version": "code_task_greenfield_resource_plan.v1",
        "profile": str(decision.get("profile") or "local_cpu"),
        "allow_gpu": bool(decision.get("allow_gpu")),
        "max_files": max_files,
        "max_generated_lines": max_generated_lines,
        "decision": decision,
    }


def _greenfield_contract_for_review(paths: object) -> dict[str, Any]:
    contract = load_task_contract(paths.meta_dir)
    if contract:
        architecture = _read_optional_dict(paths.meta_dir / "architecture_plan.json")
        write_json(paths.meta_dir / "task_contract_coverage.json", contract_coverage(contract, architecture))
        return contract
    task_path = paths.task_dir / "task.md"
    task = read_text(task_path) if task_path.is_file() else ""
    manifest = load_code_task_manifest(paths.run_dir)
    result_schema = result_schema_from_manifest(manifest)
    architecture = _read_optional_dict(paths.meta_dir / "architecture_plan.json")
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








def _update_generated_repair_artifacts(
    meta_dir: Path, project_dir: Path, repair: dict[str, Any],
) -> None:
    """Project explicit repair records and actual files into the generated inventory."""
    if not (repair.get("changed_files") or repair.get("regenerated_files")):
        return
    artifact_path = meta_dir / "code_artifacts.json"
    if not artifact_path.is_file():
        return
    artifacts = read_json(artifact_path)
    rows = {
        row["path"].replace("\\", "/"): dict(row)
        for row in artifacts["generated_files"]
    }
    for replacement in repair.get("regenerated_files", []):
        path = replacement["path"].replace("\\", "/")
        row = rows.setdefault(path, {"path": path})
        for key in ("mode", "summary", "public_api"):
            if key in replacement:
                row[key] = replacement[key]
    generated = []
    for path, row in rows.items():
        target = project_dir / path
        if target.is_file():
            row["line_count"] = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
            generated.append(row)
    artifacts["generated_files"] = generated
    artifacts["total_lines"] = sum(row["line_count"] for row in generated)
    write_json(artifact_path, artifacts)


def _read_optional_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def _work_plan_exists(paths: object) -> bool:
    return (paths.task_dir / "work_plan.json").is_file()


def _batch_cap_exceeded(run_dir: Path, max_batches: int | None) -> bool:
    if max_batches is None:
        return False
    return _batch_count(run_dir) >= max_batches


def _batch_count(run_dir: Path) -> int:
    root = Path(run_dir) / "code_task" / "attempts"
    if not root.is_dir():
        return 0
    return sum(1 for path in root.glob("attempt-*/batches/batch-*/batch_state.json") if path.is_file())


def _cost_cap_exceeded(meta_dir: Path, cost_cap_usd: float | None) -> bool:
    if cost_cap_usd is None:
        return False
    summary_path = meta_dir / "llm_usage_summary.json"
    if not summary_path.is_file():
        return False
    summary = read_json(summary_path)
    if not isinstance(summary, dict):
        return False
    cost = summary.get("estimated_cost_usd")
    return isinstance(cost, (int, float)) and float(cost) >= cost_cap_usd


def _create_repair_batch(run_dir: Path) -> LoadedCodeTaskBatch | None:
    parent = load_latest_code_task_batch(run_dir)
    if parent is None:
        return None
    work_item_id = str(parent.state.get("work_item_id") or "").strip()
    if not work_item_id:
        return None
    result = create_code_task_batch(
        run_dir,
        work_item_id=work_item_id,
        attempt_id=parent.attempt_id,
        kind="repair",
        parent_batch_id=parent.batch_id,
        force=True,
    )
    return load_latest_code_task_batch(result.run_dir)


def _latest_batch(manifest: dict[str, object]) -> str:
    attempts = manifest.get("attempts", {})
    if not isinstance(attempts, dict):
        return ""
    value = attempts.get("latest_batch")
    return value if isinstance(value, str) else ""


def _first_work_item_id(paths: object) -> str:
    work_plan_path = paths.task_dir / "work_plan.json"
    if not work_plan_path.is_file():
        raise FileNotFoundError(f"Missing work plan: {work_plan_path}")
    work_plan = read_json(work_plan_path)
    if not isinstance(work_plan, dict):
        raise RuntimeError(f"Expected JSON object in {work_plan_path}")
    items = [item for item in work_plan.get("items", []) if isinstance(item, dict)]
    for item in items:
        if _is_executable_work_item(item):
            item_id = str(item.get("id") or "").strip()
            if item_id:
                return item_id
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if item_id:
            return item_id
    raise RuntimeError(f"Work plan has no executable items: {work_plan_path}")


def _is_executable_work_item(item: dict[str, object]) -> bool:
    objective = str(item.get("objective") or "").lower()
    done = " ".join(str(value).lower() for value in item.get("done_criteria", []) if isinstance(value, str))
    text = objective + "\n" + done
    target_files = item.get("target_files")
    if not isinstance(target_files, list) or not any(isinstance(path, str) and path for path in target_files):
        return False
    edit_patterns = (
        r"\badd(?:s|ed|ing)?\b",
        r"\bchang(?:e|es|ed|ing)\b",
        r"\bfix(?:es|ed|ing)?\b",
        r"\bimplement(?:s|ed|ing)?\b",
        r"\bimprov(?:e|es|ed|ing)\b",
        r"\bmodif(?:y|ies|ied|ying)\b",
        r"\boptimi[sz](?:e|es|ed|ing)\b",
        r"\brefactor(?:s|ed|ing)?\b",
        r"\brepair(?:s|ed|ing)?\b",
        r"\btun(?:e|es|ed|ing)\b",
        r"\bupdat(?:e|es|ed|ing)\b",
    )
    analysis_patterns = (
        r"\banaly[sz](?:e|es|ed|ing)\b",
        r"\bcaptur(?:e|es|ed|ing)\b",
        r"\bdocument(?:s|ed|ing)?\b",
        r"\bfinali[sz](?:e|es|ed|ing)\b",
        r"\bidentif(?:y|ies|ied|ying)\b",
        r"\binspect(?:s|ed|ing)?\b",
        r"\bmeasur(?:e|es|ed|ing)\b",
        r"\breview(?:s|ed|ing)?\b",
        r"\bunderstand(?:s|ing)?\b",
    )
    if any(re.search(pattern, text) for pattern in edit_patterns):
        return True
    return not any(re.search(pattern, objective) for pattern in analysis_patterns)


def _run_record(manifest: dict[str, object], label: str) -> dict[str, object]:
    benchmark = manifest.get("benchmark", {})
    if not isinstance(benchmark, dict):
        return {}
    runs = benchmark.get("runs", {})
    if not isinstance(runs, dict):
        return {}
    record = runs.get(label, {})
    return record if isinstance(record, dict) else {}


def _baseline_dry_run_detail(policy: str) -> str:
    if policy in {"skip", "none"}:
        return f"record baseline_policy={policy} without running unchanged benchmark"
    if policy == "provided":
        return "record provided baseline metrics"
    return "run unchanged benchmark"


def _record_baseline_policy(run_dir: Path, *, policy: str, status: str) -> None:
    manifest = load_code_task_manifest(run_dir)
    benchmark = manifest_section(manifest, "benchmark")
    benchmark["baseline_policy"] = {
        "policy": policy,
        "status": status,
        "updated_at": utcnow_iso(),
        "source": "execute_config",
    }
    manifest["benchmark"] = benchmark
    if policy in {"skip", "none"} and status == "skipped":
        manifest["status"] = "baseline_skipped"
    save_code_task_manifest(run_dir, manifest)


def _plan_status(manifest: dict[str, object]) -> str:
    plan = manifest.get("plan", {})
    if not isinstance(plan, dict):
        return "not_started"
    return str(plan.get("status") or "not_started")


def _patch_status(manifest: dict[str, object]) -> str:
    patch = manifest.get("patch", {})
    if not isinstance(patch, dict):
        return "not_started"
    return str(patch.get("status") or "not_started")


def _code_task_kind(manifest: dict[str, object]) -> str:
    section = manifest.get("code_task")
    if isinstance(section, dict):
        return str(section.get("kind") or "existing_project").strip().lower()
    return "existing_project"


def _failure_summary_for_memory(text: str) -> str:
    signal = _failure_signature(text)
    if signal:
        return f"Failure signal: {signal}"
    return ""


def _failure_signature(text: str, *, max_chars: int = 240) -> str:
    if not text:
        return ""
    candidates: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            "error" in lowered
            or "failed" in lowered
            or "traceback" in lowered
            or "assert" in lowered
            or "not found" in lowered
            or "has no attribute" in lowered
            or "unexpected keyword" in lowered
        ):
            candidates.append(stripped)
    if not candidates:
        candidates = [line.strip() for line in text.splitlines() if line.strip()]
    return _clip_inline(candidates[-1] if candidates else "", max_chars=max_chars)


def _review_report_summary_for_memory(review: dict[str, Any]) -> str:
    findings = review.get("findings")
    rows = findings if isinstance(findings, list) else []
    summaries: list[str] = []
    for row in rows[:4]:
        if not isinstance(row, dict):
            continue
        severity = str(row.get("severity", "info"))
        category = str(row.get("category", "general"))
        summary = str(row.get("summary") or row.get("issue") or row.get("recommendation") or "").strip()
        if summary:
            summaries.append(f"{severity}/{category}: {summary}")
    if summaries:
        return "Review finding: " + " | ".join(_clip_inline(item, max_chars=180) for item in summaries)
    status = str(review.get("status") or "unknown")
    return f"Generated project review status {status}."


def _review_signature(review: dict[str, Any], *, max_chars: int = 240) -> str:
    return _failure_signature(_review_report_summary_for_memory(review), max_chars=max_chars)


def _greenfield_repair_changed_files(repair: dict[str, Any]) -> list[str]:
    raw_changed = repair.get("changed_files")
    changed = (
        [str(path) for path in raw_changed if str(path).strip()]
        if isinstance(raw_changed, list)
        else []
    )
    regenerated = repair.get("regenerated_files")
    if isinstance(regenerated, list):
        for row in regenerated:
            if isinstance(row, dict) and str(row.get("path", "")).strip():
                changed.append(str(row["path"]))
    return list(dict.fromkeys(changed))


def _greenfield_repair_attempt_summary(repair: dict[str, Any]) -> str:
    status = str(repair.get("status", "unknown"))
    changed = _greenfield_repair_changed_files(repair)
    notes = repair.get("notes")
    note_text = "; ".join(str(item) for item in notes[:3]) if isinstance(notes, list) else ""
    parts = [f"repair status={status}"]
    if changed:
        parts.append("changed " + ", ".join(changed[:8]))
    if note_text:
        parts.append(_clip_inline(note_text, max_chars=260))
    return "; ".join(parts)


def _repair_count(manifest: dict[str, Any], *, field: str = "repair_count") -> int:
    """Missing history starts at zero; corrupt history must not grant retries."""
    value = manifest.get("repair", {}).get(field, 0)
    if type(value) is not int or value < 0:
        raise ValueError(f"Invalid persisted repair counter: repair.{field}={value!r}")
    return value


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _benchmark_output_callback(callback: MessageCallback | None) -> Callable[[str, str], None] | None:
    if callback is None:
        return None
    repeated: dict[str, int] = {}

    def _relay(stream: str, line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        if stream == "stderr":
            signature = _repeated_warning_signature(text)
            if signature:
                repeated[signature] = repeated.get(signature, 0) + 1
                count = repeated[signature]
                if count > 3:
                    if count == 4:
                        callback(f"benchmark {stream}: repeated warning suppressed after 3 occurrence(s): {signature}")
                    elif count % 50 == 0:
                        callback(f"benchmark {stream}: repeated warning still occurring ({count} occurrence(s)): {signature}")
                    return
        callback(f"benchmark {stream}: {text}")

    return _relay


def _repeated_warning_signature(text: str) -> str:
    normalized = " ".join(str(text).split())
    lowered = normalized.lower()
    if not normalized:
        return ""
    warning_markers = (
        "warning",
        "convergencewarning",
        "runtimewarning",
        "userwarning",
        "stop: total no. of iterations reached limit",
        "increase the number of iterations",
        "you might also want to scale the data",
    )
    if not any(marker in lowered for marker in warning_markers):
        return ""
    normalized = re.sub(r'File "[^"]+", line \d+', 'File "<path>", line <n>', normalized)
    normalized = re.sub(r"[A-Za-z]:[\\/][^\s:]+", "<path>", normalized)
    normalized = re.sub(r"/[^\s:]+", "<path>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?\b", "<num>", normalized)
    return normalized[:220]


def _first_error_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped != "Patch validation failed:":
            return stripped.removeprefix("- ").strip()
    return "patch validation failed"


def _clip_inline(text: str, *, max_chars: int) -> str:
    value = " ".join(str(text).split())
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def _relative_to_run(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(run_dir.resolve()).as_posix()
    except ValueError:
        return str(path)
