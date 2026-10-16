"""Use the existing CodeTask executor as a non-measuring implementation action."""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.core.capabilities import ArtifactRef, CapabilityContext, CapabilityResult
from simple_ar.core.artifacts import read_json, write_json, write_text
from simple_ar.research.design import ResearchDesignResult
from simple_ar.research.task_handoff import research_handoff_text
from simple_ar.code_task.orchestration.execute import implement_code_task
from simple_ar.code_task.orchestration.verification import apply_verified_repair, verify_implementation
from simple_ar.code_task.execution.repair import RepairEvidence, propose_repair_edits
from simple_ar.code_task.analysis.context import load_latest_code_task_context_pack
from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, save_code_task_manifest, workspace_file
from simple_ar.code_task.editing.scope import protected_patterns_from_manifest
from simple_ar.experiment.execution.measurement import snapshot_protocol_assets, reconcile_protocol_assets


IMPLEMENTATION_CONTEXT_MAX_FILES = 6
IMPLEMENTATION_CONTEXT_MAX_SOURCE_CHARS = 12_000


@dataclass(frozen=True)
class ImplementationRequest:
    run_dir: Path
    cwd: Path
    approval_note: str
    llm_client: Any = field(repr=False, compare=False)
    protocol: Mapping[str, Any] | None = None
    failure_ref: ArtifactRef | None = None
    revision_instruction: str = ""
    validation_command: tuple[str, ...] | None = None
    validation_timeout_sec: int | None = None
    max_repairs: int = 0
    budget_ledger: Any | None = field(default=None, repr=False, compare=False)
    session_id: str = ""
    attempt_id: str = ""
    budget_profile: str | None = None
    edit_budget_overrides: dict[str, int] | None = None
    allow_large_edits: bool = False
    message_callback: Callable[[str], None] | None = field(default=None, repr=False, compare=False)


def run_implementation_capability(*, context: CapabilityContext, request: ImplementationRequest) -> CapabilityResult:
    if request.llm_client is None:
        raise ValueError("Implementation requires a shared LLM client; no offline patch is invented.")
    if not request.approval_note.strip():
        raise ValueError("Implementation requires explicit isolated-workspace approval.")
    manifest = load_code_task_manifest(request.run_dir)
    paths = code_task_paths(request.run_dir)
    if paths.workspace_dir.resolve() != request.cwd.resolve():
        raise ValueError("Candidate cwd must be the initialized CodeTask workspace.")
    task_ref, design = _prepare_research_task(context, request, paths.task_dir)
    before = snapshot_protocol_assets(request.protocol, request.cwd)
    conditions = request.protocol.get("comparison_conditions", {}) if request.protocol else {}
    source_config = conditions.get("source_config") if isinstance(conditions, Mapping) else None
    if isinstance(source_config, str) and source_config.strip():
        source_config = source_config.strip()
        config_file = workspace_file(request.cwd, source_config)
        if config_file is None or not config_file.is_file():
            raise ValueError(f"Active source config is absent from the isolated workspace: {source_config}")
        if "active_source_config" in before:
            raise ValueError("Protocol asset ID active_source_config is reserved for the declared source config.")
        before.update(snapshot_protocol_assets({"protected_assets": [
            {"asset_id": "active_source_config", "path": source_config},
        ]}, request.cwd))
    protected = list(protected_patterns_from_manifest(manifest))
    for snapshot in before.values():
        try:
            relative = Path(snapshot["path"]).relative_to(request.cwd).as_posix()
        except ValueError:
            continue  # Shared data outside the editable workspace is audited too.
        if relative not in protected:
            protected.append(relative)
    scope = dict(manifest.get("edit_scope", {}))
    scope["protected_patterns"] = protected
    manifest["edit_scope"] = scope
    if isinstance(source_config, str) and source_config.strip():
        manifest["context_requirements"] = {
            "read_only_paths": [source_config.strip()],
            "source": "accepted_experiment_protocol",
        }
    save_code_task_manifest(request.run_dir, manifest)
    repair_paths = {}
    validation_repairs: list[dict[str, Any]] = []
    failed_validation_artifacts: list[tuple[Path, Path]] = []
    steps = []
    validation = None
    if request.failure_ref is None:
        options = dict(
            approval_note=request.approval_note,
            llm_client=request.llm_client, use_llm=True, allow_planning_fallback=False,
            budget_profile=request.budget_profile,
            edit_budget_overrides=request.edit_budget_overrides,
            allow_large_edits=request.allow_large_edits,
            max_files=IMPLEMENTATION_CONTEXT_MAX_FILES,
            max_source_chars_per_file=IMPLEMENTATION_CONTEXT_MAX_SOURCE_CHARS,
            message_callback=request.message_callback,
        )
        outcome = implement_code_task(request.run_dir, proposal_corrections=1, **options)
        stop_reason, next_action = outcome.stop_reason, outcome.next_action
        steps = [asdict(step) for step in outcome.steps]
    else:
        failed = context.read_input_json(request.failure_ref)
        failure_dir = context.resolve_input(request.failure_ref).parent
        # Logs are attempt-relative artifacts; resolve through the same store
        # boundary rather than trusting a path supplied in the result JSON.
        store = context.input_store or context.store
        stderr_path = failure_dir / failed["artifacts"]["stderr"]
        details = store.read_text(store.ref(stderr_path))
        evidence = RepairEvidence(request.failure_ref.path, failed, details or json.dumps(failed["diagnosis"]))
        repair = propose_repair_edits(request.run_dir, llm_client=request.llm_client, failure_evidence=evidence)
        repair_paths = {"repair_proposal": repair.proposal_path,
                        "failure_evidence": repair.repair_dir / "failure_evidence.json"}
        if repair.mode != "llm" or not repair.edit_count:
            stop_reason, next_action = "empty_repair", "Inspect the failed measurement and repair proposal."
        else:
            apply_verified_repair(request.run_dir, repair, approval_note=request.approval_note,
                                  llm_client=request.llm_client, message_callback=request.message_callback)
            stop_reason, next_action = "stop_point", "Measure the repaired candidate separately."
    integrity = reconcile_protocol_assets(before)
    if (
        request.validation_command
        and stop_reason == "stop_point"
        and integrity["status"] != "changed"
    ):
        verified = verify_implementation(
            request.run_dir, command=request.validation_command, approval_note=request.approval_note,
            llm_client=request.llm_client, max_repairs=request.max_repairs,
            timeout_sec=request.validation_timeout_sec or 60,
            max_files=IMPLEMENTATION_CONTEXT_MAX_FILES,
            max_source_chars_per_file=IMPLEMENTATION_CONTEXT_MAX_SOURCE_CHARS,
            asset_guard=lambda: reconcile_protocol_assets(before),
            budget_ledger=request.budget_ledger, session_id=request.session_id,
            attempt_id=request.attempt_id, message_callback=request.message_callback,
        )
        validation = verified.validation
        validation_repairs = list(verified.repairs)
        failed_validation_artifacts = list(verified.failed_artifacts)
        if verified.repair is not None:
            repair_paths = {"repair_proposal": verified.repair.proposal_path,
                            "failure_evidence": verified.repair.repair_dir / "failure_evidence.json"}
        stop_reason = "stop_point" if validation.status == "passed" else "validation_failed"
        next_action = (
            "Review the recorded bug validation artifacts before accepting the patch."
            if validation.status != "passed"
            else "Review the patch and the passed bug validation artifacts."
        )
    integrity = reconcile_protocol_assets(before)
    if integrity["status"] == "changed":
        stop_reason = "asset_integrity_changed"
        next_action = "Protected execution assets changed; reject the patch and inspect its edit lineage."
    finished = (
        stop_reason == "stop_point"
        and integrity["status"] != "changed"
        and (validation is None or validation.status == "passed")
    )
    # Keep the small implementation evidence with its attempt, not at mutable
    # absolute paths in an independently managed CodeTask run. Do not copy data,
    # environments, checkpoints or the whole workspace into every attempt.
    evidence = {}
    source_context = load_latest_code_task_context_pack(request.run_dir)
    proposal_path = paths.meta_dir / "proposed_edits.json"
    proposal_data = read_json(proposal_path) if proposal_path.is_file() else {}
    for name, path in {
        "patch": paths.task_dir / "patch.diff",
        "validation": paths.meta_dir / "validation_report.json",
        "review": paths.meta_dir / ("review_report_post_repair.json" if request.failure_ref or validation_repairs else "review_report.json"),
        "work_plan": paths.task_dir / "work_plan.json",
        "patch_plan": paths.task_dir / "patch_plan.md",
        "research_handoff": paths.task_dir / "research_handoff.json",
        "edit_proposal": paths.meta_dir / "proposed_edits.json",
        "context_followup": paths.meta_dir / "edit_context_followup.json",
        **({f'generation_{name}': paths.meta_dir / f'{name}.json' for name in (
            'task_contract', 'architecture_plan', 'code_artifacts', 'dependency_advice', 'implementation_plan')}
            if manifest.get('code_task', {}).get('kind') == 'greenfield' else {}),
        **({"dependency_api": paths.meta_dir / "dependency_api.json"}
           if isinstance(proposal_data, Mapping) and proposal_data.get("dependency_api") else {}),
        **({"source_context": source_context.prompt_context_path} if source_context is not None else {}),
        **repair_paths,
    }.items():
        if path.is_file():
            evidence[name] = context.store.write_text(
                f"code_task/{'repair_' if name == 'repair_proposal' else ''}{path.name}",
                path.read_text(encoding="utf-8"),
                kind=f"implementation_{name}", producer="research.implementation",
            )
    for index, (report_path, stderr_path) in enumerate(failed_validation_artifacts, start=1):
        for name, path in ((f"validation_failure_{index}", report_path),
                           (f"validation_failure_stderr_{index}", stderr_path)):
            evidence[name] = context.store.write_text(
                f"code_task/{name}{path.suffix}", path.read_text(encoding="utf-8"),
                kind=f"implementation_{name}", producer="research.implementation",
            )
    if validation is not None:
        for name, path in {
            "validation_report": validation.report_path,
            "validation_stdout": validation.stdout_path,
            "validation_stderr": validation.stderr_path,
            "validation_metrics": validation.metrics_path,
        }.items():
            if path.is_file():
                evidence[name] = context.store.write_text(
                    f"code_task/{path.name}", path.read_text(encoding="utf-8"),
                    kind=f"implementation_{name}", producer="research.implementation",
                )
    validation_row = None if validation is None else {
        "command": list(request.validation_command),
        "timeout_sec": request.validation_timeout_sec,
        "status": validation.status,
        "returncode": validation.returncode,
        "timed_out": validation.timed_out,
        "metrics": dict(validation.metrics),
    }
    proposal_path = paths.meta_dir / "proposed_edits.json"
    proposal = read_json(proposal_path) if proposal_path.is_file() else {}
    feedback = proposal.get("implementation_feedback") if stop_reason == "no_edits_proposed" else None
    if finished and all((paths.workspace_dir / name).is_file() for name in
                        ("analysis.py", "tests/verify_delivery.py", "outputs/report.md", "outputs/results.json")):
        from simple_ar.result_analysis.script_project import copy_code_analysis_package
        delivery = context.store.root / "code_analysis"
        copy_code_analysis_package(paths.workspace_dir, delivery, workspace=True)
        evidence["code_analysis"] = context.store.ref("code_analysis/analysis.json", kind="code_analysis",
            schema="code_analysis.v1", producer="research.implementation")
    if (manifest.get('code_task', {}).get('native_binding') or {}).get('binding', {}).get('environment_preparations'):
        for name, kind in (('environment_setup', 'environment_setup'), ('environment_observation', 'environment_runtime')):
            evidence[name] = context.store.ref(name + '.json', kind=kind, schema=kind + '.v1', producer='research.preparation')
    artifact_payload: dict[str, Any] = {
        "schema_version": "research_implementation.v1",
        "status": "validated" if finished else "incomplete",
        "code_task_run_dir": str(request.run_dir), "workspace_dir": str(paths.workspace_dir),
        "stop_reason": stop_reason, "next_action": next_action,
        "steps": steps,
        "implementation_feedback": feedback,
        "failure_ref": request.failure_ref.to_dict() if request.failure_ref else None,
        "asset_integrity": integrity,
        "validation": validation_row,
        "validation_repairs": validation_repairs,
        "artifact_refs": {name: ref.to_dict() for name, ref in evidence.items()},
        "artifact_base": "attempt",
    }
    if design is not None:
        # A technically validated patch is not evidence that the proposed
        # research mechanism survived implementation. Hints are not checks.
        artifact_payload["method_validation"] = {
            "status": "未检查",
            "reason": "No candidate-specific behavior criterion and observed result were recorded together.",
            "validation_hints": list(design.contract.validation_hints) if design.contract else [],
        }
    native_binding = manifest.get('code_task', {}).get('native_binding')
    if native_binding is not None:
        import hashlib
        from simple_ar.code_task.runtime.state import implementation_file_hashes
        artifact_payload['native_binding'] = native_binding
        artifact_payload['implementation_file_hashes'] = implementation_file_hashes(request.run_dir)
        artifact_payload['artifact_hashes'] = {
            ref.path: hashlib.sha256(context.store.resolve(ref).read_bytes()).hexdigest()
            for ref in evidence.values()
        }
    artifact = context.store.write_json(
        "implementation.json", artifact_payload,
        kind="implementation_result", schema="research_implementation.v1", producer="research.implementation",
    )
    capability_status = "completed" if finished else "partial" if validation is not None else "blocked"
    return CapabilityResult(
        status=capability_status, artifacts=(task_ref, *evidence.values(), artifact),
        diagnostics=() if finished else (
            *(step["detail"] for step in steps if step["status"] == "blocked"),
            next_action, f"Asset integrity: {integrity['status']}",
        ),
    )


def _prepare_research_task(
    context: CapabilityContext, request: ImplementationRequest, task_dir: Path,
) -> tuple[ArtifactRef, ResearchDesignResult | None]:
    """Freeze consumed research context before planning; never reuse a stale plan."""
    design_ref = next((ref for ref in context.inputs if ref.kind == "research_design"), None)
    design = ResearchDesignResult.from_handoff_dict(context.read_input_json(design_ref)) if design_ref else None
    if design is not None and design.contract is None:
        raise ValueError("Implementation requires a selected research design contract.")
    brief_ref = next((ref for ref in context.inputs if ref.kind == "research_brief"), None)
    if design is None and brief_ref is None:
        raise ValueError("Implementation requires a research design or a bug-task brief.")
    baseline_refs = [ref for ref in context.inputs if ref.kind == "experiment_result" and ref != request.failure_ref]
    baseline = context.read_input_json(baseline_refs[0]) if baseline_refs else None
    reproduction = design is None and any(
        ref.kind == "runtime_config" and context.read_input_json(ref).get("config", {}).get("research_task_kind") == "reproduction"
        for ref in context.inputs
    )
    consumed = {
        "task_kind": "research" if design is not None else "reproduction" if reproduction else "bug_fix",
        "brief": None if brief_ref is None else context.read_input_json(brief_ref),
        "design": None if design is None else design.to_handoff_dict(),
        "protocol": request.protocol,
        "revision_instruction": request.revision_instruction.strip(),
        "baseline": None if baseline is None else {
            "source": baseline_refs[0].to_dict(), "status": baseline["status"],
            "metrics": baseline["metrics"], "measurement": baseline["measurement"],
        },
    }
    if len(baseline_refs) > 1:
        consumed["paired_baselines"] = [{"source": ref.to_dict(), "status": result["status"],
            "metrics": result["metrics"], "measurement": result["measurement"]}
            for ref in baseline_refs for result in (context.read_input_json(ref),)]
    snapshot = task_dir / "research_handoff.json"
    task_path = task_dir / "task.md"
    if snapshot.exists():
        saved = read_json(snapshot)
        saved_consumed = dict(saved["consumed"])
        saved_consumed.setdefault("revision_instruction", "")
        if saved_consumed != consumed:
            raise ValueError("Research inputs changed; prepare a new CodeTask run instead of reusing its plan.")
        original = saved["original_task"]
    else:
        if (task_dir / "work_plan.json").exists():
            raise ValueError("CodeTask already has a plan without this research handoff; use a fresh initialized run.")
        original = task_path.read_text(encoding="utf-8")
        write_json(snapshot, {"consumed": consumed, "original_task": original})
    if design is not None:
        task = (
            "# Implementation-only CodeTask\n\n"
            "This action only proposes and validates the selected code change in the "
            "isolated workspace. The outer ResearchApplication owns literature review, "
            "experiment execution, result analysis, and academic report writing. Do "
            "not create a paper, report, citations, or documentation as part of this "
            "CodeTask action.\n\n"
            "Use the selected research design and the configured execution boundary "
            "below as the implementation requirements. Keep the change within the "
            "existing CodeTask edit scope and preserve all protected assets.\n\n"
            + research_handoff_text(
                design.contract,
                execution_context=(
                    "Use the configured protocol in the execution-boundary JSON below."
                    if request.protocol else ""
                ),
            )
        )
        if design.implementation_spec:
            task += "\n## Clarified implementation specification\n\n" + design.implementation_spec + "\n"
        if design.code_task_questions:
            task += (
                "\n## Source details to verify before editing\n\n"
                "These are delegated code questions, not established facts or permission to change the method. "
                "Inspect the actual implementation and resolve them within the authorized edit scope. "
                "If a required interface is incompatible, return a design_gap instead of guessing.\n\n"
                + "\n".join(f"- {question}" for question in design.code_task_questions) + "\n"
            )
        brief = consumed.get("brief")
        if isinstance(brief, Mapping):
            objective = str(brief.get("objective") or brief.get("request_text") or "").strip()
            if objective:
                task += "\n## User research objective\n\n" + objective + "\n"
            constraints = brief.get("hard_constraints")
            if isinstance(constraints, list) and constraints:
                task += "\n## User hard constraints\n\n"
                task += "\n".join(f"- {item}" for item in constraints if str(item).strip()) + "\n"
            preferences = brief.get("preferences")
            if isinstance(preferences, list) and preferences:
                task += "\n## User preferences\n\n"
                task += "\n".join(f"- {item}" for item in preferences if str(item).strip()) + "\n"
            task += (
                "\nResolve implementation details the user objective explicitly delegates by inspecting "
                "the project and selecting a concrete option within the accepted design and CodeTask "
                "scope. Do not expand the approved change or alter the execution protocol.\n"
            )
        if request.revision_instruction.strip():
            task += (
                "\n\n## Evidence-directed revision\n\n"
                "Apply this proposed research revision only after inspecting the current workspace and "
                "existing patch. Preserve the configured edit scope and protected assets.\n\n"
                + request.revision_instruction.strip() + "\n"
            )
    else:
        brief = consumed["brief"]
        if not isinstance(brief, Mapping):
            raise ValueError("Bug-task implementation requires the persisted brief payload.")
        constraints = brief.get("hard_constraints", [])
        task = (
            "# Bug-fix CodeTask\n\n"
            "This action locates and repairs one scoped defect in the existing project. "
            "Use the isolated CodeTask workspace, preserve protected files, and make "
            "the smallest behaviorally justified patch. Do not search literature, "
            "train a model, run a research baseline, or write an academic report. "
            "The configured command is a short bug-validation command, not a research "
            "measurement.\n\n"
            f"## User request\n\n{str(brief.get('request_text') or '').strip()}\n"
            f"\n## Objective\n\n{str(brief.get('objective') or '').strip()}\n"
            + ("\n## Hard constraints\n\n" + "\n".join(f"- {item}" for item in constraints) + "\n"
               if isinstance(constraints, list) and constraints else "")
        )
        if reproduction:
            task = (
                "# Fixed-scope reproduction preparation\n\n"
                "Only connect the author program and adapt its actual results to the declared JSON/CSV contract. "
                "When an inspected author entrypoint already runs the experiment, delegate to it rather than "
                "copying its scientific trial loop into the adapter. Inspect its effective defaults and recipe "
                "overrides; do not invent cheaper method parameters. "
                "Preserve the user-confirmed method, data, seeds, evaluation conditions and protected checker. "
                "Do not innovate, fabricate metrics, install dependencies or run formal measurement. "
                "The outer application owns measurement; the independent validation command must pass first. "
                "Before measurement outputs exist, validate real readiness: import the required author modules, "
                "check the actual input path/schema, and exercise the necessary author entry on a bounded "
                "non-measurement example when feasible. An empty directory, syntax check or absent outputs "
                "alone cannot establish readiness. Exercise the adapter's actual output assembly too and "
                "resolve the declared metric selectors against that shortcheck output; independently "
                "recomputing values without checking the emitted file structure does not validate the adapter. "
                "A readiness example must not change the formal protocol "
                "or be reported as its scientific result. If readiness cannot be checked, fail with an "
                "actionable reason rather than returning success. Existing outputs may be independently "
                "checked as a separate result-contract mode, not a substitute for pre-run readiness.\n\n"
                + original + "\n\n" + request.revision_instruction + "\n"
            )
    task += "\n## Execution boundary\n\n"
    task += "The configured protocol and existing edit scope remain authoritative.\n"
    if design is not None:
        task += "Execution boundary and observed baseline are supplied facts, not model-selected targets.\n"
        task += "Baseline values below are observations, not target values to hardcode.\n"
    task += "Implement and validate only; the research application owns benchmark execution.\n\n"
    task += "```json\n" + json.dumps(consumed, ensure_ascii=False, separators=(",", ":")) + "\n```\n"
    write_text(task_path, task)
    task_ref = context.store.write_text("inputs/research_code_task.md", task, kind="task_input",
                                        schema="markdown.v1", producer="research.implementation")
    return task_ref, design
