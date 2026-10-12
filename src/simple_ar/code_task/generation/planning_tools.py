from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.code_task.generation.agent_step import run_json_agent_step, write_agent_step_artifact
from simple_ar.code_task.generation.common import scalar_list, text as clean_text
from simple_ar.code_task.generation.task_contract import contract_prompt_view
from simple_ar.core.artifacts import write_json
from simple_ar.integrations.llm import LLMClient, LLMResponseError


MessageCallback = Callable[[str], None]

PLANNING_STAGES = ("requirements", "architecture", "interfaces", "file_plan")


def build_tool_agent_architecture_plan(
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    domain_profile: Mapping[str, Any],
    client: LLMClient,
    retry_attempts: int = 1,
    review_rounds: int = 2,
    planning_dir: Path | None = None,
    message_callback: MessageCallback | None = None,
) -> dict[str, Any]:
    """Build a greenfield architecture plan through small reviewable LLM tools.

    The planner deliberately keeps each model call narrow: first summarize the
    task contract, then propose architecture, then freeze cross-file interfaces,
    then produce the file plan. A final reviewer can route feedback back to the
    earliest affected stage, and downstream stages are regenerated from that
    point. This keeps long prompts and repeated mistakes from becoming one
    opaque monolithic planning failure.

    retry_attempts bounds completed JSON corrections per stage; provider retry
    and accounting stay in LLMClient. review_rounds bounds content revisions.
    """

    retry_attempts = max(1, int(retry_attempts or 1))
    review_rounds = max(0, int(review_rounds or 0))
    if planning_dir is not None:
        planning_dir.mkdir(parents=True, exist_ok=True)
    state: dict[str, Any] = {}
    feedback: dict[str, list[str]] = {stage: [] for stage in PLANNING_STAGES}
    rerun_from = "requirements"
    review: dict[str, Any] = {}
    revision_count = 0
    patch_request_history: list[dict[str, Any]] = []

    for round_index in range(review_rounds + 1):
        for stage in _stages_from(rerun_from):
            state[stage] = _run_stage(
                stage,
                contract=contract,
                result_schema=result_schema,
                resource_plan=resource_plan,
                domain_profile=domain_profile,
                state=state,
                feedback=feedback.get(stage, []),
                client=client,
                retry_attempts=retry_attempts,
                attempt_index=round_index + 1,
                planning_dir=planning_dir,
                message_callback=message_callback,
            )
        review = _run_stage(
            "review",
            contract=contract,
            result_schema=result_schema,
            resource_plan=resource_plan,
            domain_profile=domain_profile,
            state=state,
            feedback=[],
            client=client,
            retry_attempts=retry_attempts,
            attempt_index=round_index + 1,
            planning_dir=planning_dir,
            message_callback=message_callback,
        )
        patch_requests = _planning_patch_requests(review, round_index=round_index + 1)
        patch_request_history.extend(patch_requests)
        _write_patch_requests(planning_dir, latest=patch_requests, history=patch_request_history)
        if _review_passed(review):
            break
        if round_index >= review_rounds:
            _emit(
                message_callback,
                "Greenfield planning review still has findings after bounded revisions; continuing with recorded risks.",
            )
            break
        rerun_from = _review_target_stage(review)
        revision_count += 1
        _merge_review_feedback(feedback, review)
        _emit(
            message_callback,
            f"Planning review requested revision from `{rerun_from}`; regenerating downstream planning artifacts.",
        )

    try:
        plan = _assemble_architecture_plan(
            contract=contract,
            result_schema=result_schema,
            resource_plan=resource_plan,
            state=state,
            review=review,
            revision_count=revision_count,
        )
    except LLMResponseError as exc:
        if planning_dir is not None:
            write_json(planning_dir / "blocking_issues.json", {
                "schema_version": "greenfield_planning_blockers.v1",
                "blockers": [str(exc)], "review": dict(review),
            })
        raise
    if planning_dir is not None:
        write_json(planning_dir / "state.json", state)
        write_json(planning_dir / "final_plan.json", plan)
    return plan


def _run_stage(
    stage: str,
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    domain_profile: Mapping[str, Any],
    state: Mapping[str, Any],
    feedback: list[str],
    client: LLMClient,
    retry_attempts: int,
    attempt_index: int,
    planning_dir: Path | None,
    message_callback: MessageCallback | None,
) -> dict[str, Any]:
    label = f"greenfield-plan-{stage}" if attempt_index == 1 else f"greenfield-plan-{stage}-r{attempt_index}"
    if stage == "review":
        _emit(message_callback, "Reviewing greenfield planning artifacts.")
        prompt = _review_prompt(
            contract=contract,
            result_schema=result_schema,
            resource_plan=resource_plan,
            domain_profile=domain_profile,
            state=state,
        )
    else:
        _emit(message_callback, f"Planning tool `{stage}`.")
        prompt = _stage_prompt(
            stage,
            contract=contract,
            result_schema=result_schema,
            resource_plan=resource_plan,
            domain_profile=domain_profile,
            state=state,
            feedback=feedback,
        )
    raw = run_json_agent_step(
        client=client,
        system=PLANNING_REVIEW_SYSTEM if stage == "review" else _stage_system(stage),
        prompt=prompt,
        label=label,
        stage=stage,
        attempt_index=attempt_index,
        retry_attempts=retry_attempts,
        max_output_tokens=900 if stage == "review" else _stage_output_tokens(stage, resource_plan),
        artifact_dir=planning_dir,
        feedback=feedback,
        output_summary_callback=_agent_step_output_summary,
        message_callback=message_callback,
    )
    normalized = _normalize_review(raw) if stage == "review" else _normalize_stage_result(stage, raw)
    write_agent_step_artifact(planning_dir, stage=stage, attempt_index=attempt_index, value=normalized)
    return normalized


def _stage_prompt(
    stage: str,
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    domain_profile: Mapping[str, Any],
    state: Mapping[str, Any],
    feedback: list[str],
) -> str:
    base = _base_context(
        contract=contract,
        result_schema=result_schema,
        resource_plan=resource_plan,
        domain_profile=domain_profile,
    )
    feedback_block = _feedback_block(feedback)
    if stage == "requirements":
        return (
            "Tool: requirements_brief.\n"
            "Extract the task into implementation-ready requirements. Return one compact strict JSON object.\n"
            "Use exactly these keys; every list must contain at most 6 short strings, and every string must be <= 120 characters:\n"
            "- objective: string\n"
            "- hard_requirements: string[]\n"
            "- implementation_obligations: at most 12 {id, requirement, evidence_terms}\n"
            "- deliverables: string[]\n"
            "- constraints: string[]\n"
            "- data_requirements: string[]\n"
            "- evaluation_targets: string[]\n"
            "- dependency_strategy: string[]\n"
            "- open_questions: string[]\n\n"
            "Rules:\n"
            "- No nested objects. No Markdown. No long prose. Do not quote the task verbatim.\n"
            "- Do not invent benchmark-specific fields. Preserve fields explicitly requested by the task/result schema.\n"
            "- Convert prose requirements into testable implementation obligations.\n"
            "- Mention optional dependencies only if they are task-relevant and installed/available in the context.\n\n"
            "- Preserve every explicit hypothesis/comparison/artifact from evidence_plan and implementation_contract as an implementation obligation.\n\n"
            f"{base}{feedback_block}"
        )
    if stage == "architecture":
        return (
            "Tool: architecture_outline.\n"
            "Design the project architecture from the requirements brief. Return one compact strict JSON object with fields:\n"
            "- architecture_summary: string\n"
            "- modules: at most 8 {name, responsibility, inputs, outputs, dependencies, obligation_ids}; nested lists at most 5 strings\n"
            "- data_flow: at most 6 ordered flow steps\n"
            "- test_strategy: at most 6 validation/smoke/benchmark checks\n"
            "- risks: at most 6 realistic implementation risks and mitigations\n\n"
            "Rules:\n"
            "- No Markdown. Keep each string <= 160 characters.\n"
            "- Keep the design bounded by resource_plan.max_files and max_generated_lines.\n"
            "- Prefer one authoritative orchestrator plus small modules with clear ownership.\n"
            "- Make cross-module dependencies explicit; downstream file planning will use this directly.\n"
            "- Assign each important implementation_contract obligation to at least one module via obligation_ids.\n"
            "- Avoid filler modules and avoid hardcoding one benchmark's hidden conventions.\n\n"
            f"{base}\nRequirements brief JSON:\n{_json_block(state.get('requirements'))}\n{feedback_block}"
        )
    if stage == "interfaces":
        return (
            "Tool: interface_contract.\n"
            "Define cross-file interfaces before files are written. Return one compact strict JSON object with fields:\n"
            "- shared_schemas: at most 8 {name, fields, producer, consumers, notes}\n"
            "- module_apis: at most 12 {module, public_api, consumes, produces}\n"
            "- cross_file_contracts: at most 10 exact call/data-flow contracts\n"
            "- stdout_contract: at most 6 parseable stdout lines/metric formats if relevant\n\n"
            "Rules:\n"
            "- No Markdown. Keep each string <= 160 characters.\n"
            "- public_api entries must be exact function/class names or concise signatures.\n"
            "- Every planned consumer must use an API declared by the producer.\n"
            "- Include artifact/metric schemas that later files can share instead of guessing independently.\n"
            "- For each required metric or hypothesis comparison, preserve the producer fields needed by downstream aggregation and reporting.\n\n"
            f"{base}\nRequirements brief JSON:\n{_json_block(state.get('requirements'))}\n"
            f"Architecture outline JSON:\n{_json_block(state.get('architecture'))}\n{feedback_block}"
        )
    if stage == "file_plan":
        return (
            "Tool: file_plan.\n"
            "Produce the final file plan. Return one compact strict JSON object with fields:\n"
            "- objective: string\n"
            "- files: array of {path, kind, purpose, dependencies, public_api, acceptance_criteria, entrypoint, contract_obligations}\n\n"
            "Rules:\n"
            "- No Markdown. Keep each purpose/criterion <= 160 characters.\n"
            "- Keep paths relative to the generated project root, POSIX-style, and inside that root.\n"
            "- kind must be source, doc, config, data, runtime_dir, or artifact_placeholder.\n"
            "- Only source/doc/config/data files will be generated by the code writer. Runtime output directories "
            "or .gitkeep placeholders must use runtime_dir or artifact_placeholder and must not contain code responsibilities.\n"
            "- Include `main.py` as the command-line entrypoint relative to that root.\n"
            "- If the run command names a directory such as `generated_project/main.py`, treat that directory "
            "as the generated project root; do not duplicate it inside file paths.\n"
            "- Keep file count within resource_plan.max_files.\n"
            "- Each file's dependencies must refer only to paths in this file plan.\n"
            "- Each dependency must be justified by a declared public_api or shared schema.\n"
            "- Acceptance criteria should be checkable and tied to task/result schema requirements.\n"
            "- contract_obligations must list ids from implementation_contract that the file owns or directly supports.\n"
            "- At least one file must own evidence preservation/reporting when evidence_plan contains hypotheses, comparisons, or required artifacts.\n"
            "- Do not add task-irrelevant boilerplate just to increase size.\n\n"
            f"{base}\nRequirements brief JSON:\n{_json_block(state.get('requirements'))}\n"
            f"Architecture outline JSON:\n{_json_block(state.get('architecture'))}\n"
            f"Interface contract JSON:\n{_json_block(state.get('interfaces'))}\n{feedback_block}"
        )
    raise ValueError(f"Unsupported planning stage: {stage}")


def _review_prompt(
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    domain_profile: Mapping[str, Any],
    state: Mapping[str, Any],
) -> str:
    return (
        "Review this greenfield planning package before code generation. Return one strict JSON object with fields:\n"
        "- status: pass or needs_revision\n"
        "- summary: short summary\n"
        "- findings: at most 8 {target_stage, severity, issue, required_change}\n\n"
        "Review criteria:\n"
        "- The plan must satisfy explicit task/result-schema requirements without benchmark-specific hidden assumptions.\n"
        "- File dependencies and public APIs must be coherent enough that per-file code generation will not guess names.\n"
        "- Required outputs, metrics, artifacts, and resource constraints must be carried into file acceptance criteria.\n"
        "- File paths are relative to the generated project root. Do not mark a plan invalid just because the "
        "external run command includes that root directory prefix.\n"
        "- Use needs_revision only for concrete defects that make code generation incoherent. Advisory quality "
        "improvements should be low/medium findings with status pass.\n"
        "- If a finding affects an upstream concept, set target_stage to the earliest stage that should be regenerated.\n"
        "- Use severity critical only when code generation should not proceed without revision.\n\n"
        f"{_base_context(contract=contract, result_schema=result_schema, resource_plan=resource_plan, domain_profile=domain_profile)}\n"
        f"Planning state JSON:\n{_json_block(state)}\n"
    )


def _assemble_architecture_plan(
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    state: Mapping[str, Any],
    review: Mapping[str, Any],
    revision_count: int,
) -> dict[str, Any]:
    # Import at assembly time: architecture's factory dispatches to this producer.
    # Only the producer canonicalizes, before final_plan.json is published.
    from simple_ar.code_task.generation.architecture import normalize_architecture_plan

    requirements = state.get("requirements") if isinstance(state.get("requirements"), Mapping) else {}
    architecture = state.get("architecture") if isinstance(state.get("architecture"), Mapping) else {}
    interfaces = state.get("interfaces") if isinstance(state.get("interfaces"), Mapping) else {}
    file_plan = state.get("file_plan") if isinstance(state.get("file_plan"), Mapping) else {}
    risks = scalar_list(architecture.get("risks"))[:8]
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    if findings:
        risks.extend(_finding_to_risk(row) for row in findings if isinstance(row, Mapping))
    plan = {
        "schema_version": "greenfield_architecture.v1",
        "mode": "greenfield_project",
        "objective": clean_text(file_plan.get("objective")) or clean_text(requirements.get("objective")) or clean_text(contract.get("objective")),
        "architecture_summary": clean_text(architecture.get("architecture_summary"))
        or "Bounded generated project with explicit interfaces and a command-line entrypoint.",
        "data_flow": scalar_list(architecture.get("data_flow"))[:8],
        "interfaces": interfaces,
        "test_strategy": scalar_list(architecture.get("test_strategy"))[:8],
        "risks": [item for item in risks if item][:10],
        "files": file_plan.get("files", []),
        "planning": {
            "mode": "tool_agent",
            "review_status": clean_text(review.get("status")) or "unknown",
            "revision_count": revision_count,
            "review_summary": clean_text(review.get("summary")),
            "patch_request_count": len(review.get("patch_requests", []))
            if isinstance(review.get("patch_requests"), list)
            else 0,
        },
        "planning_status": clean_text(review.get("status")) or "unknown",
        "result_schema": dict(result_schema),
    }
    return normalize_architecture_plan(plan, contract=contract, resource_plan=resource_plan)


def _normalize_stage_result(stage: str, value: Mapping[str, Any]) -> dict[str, Any]:
    if stage == "requirements":
        return {
            "objective": clean_text(value.get("objective")),
            "hard_requirements": scalar_list(value.get("hard_requirements"))[:50],
            "implementation_obligations": _normalize_named_rows(value.get("implementation_obligations"), max_rows=24),
            "deliverables": scalar_list(value.get("deliverables"))[:30],
            "constraints": scalar_list(value.get("constraints"))[:30],
            "data_requirements": scalar_list(value.get("data_requirements"))[:30],
            "evaluation_targets": scalar_list(value.get("evaluation_targets"))[:30],
            "dependency_strategy": scalar_list(value.get("dependency_strategy"))[:20],
            "open_questions": scalar_list(value.get("open_questions"))[:20],
        }
    if stage == "architecture":
        modules = value.get("modules")
        return {
            "architecture_summary": clean_text(value.get("architecture_summary")),
            "modules": _normalize_modules(modules),
            "data_flow": scalar_list(value.get("data_flow"))[:20],
            "test_strategy": scalar_list(value.get("test_strategy"))[:20],
            "risks": scalar_list(value.get("risks"))[:20],
        }
    if stage == "interfaces":
        return {
            "shared_schemas": _normalize_named_rows(value.get("shared_schemas"), max_rows=24),
            "module_apis": _normalize_named_rows(value.get("module_apis"), max_rows=32),
            "cross_file_contracts": scalar_list(value.get("cross_file_contracts"))[:40],
            "stdout_contract": scalar_list(value.get("stdout_contract"))[:20],
        }
    if stage == "file_plan":
        return {
            "objective": clean_text(value.get("objective")),
            "files": value.get("files", []),
        }
    return dict(value)


def _normalize_review(value: Mapping[str, Any]) -> dict[str, Any]:
    status = clean_text(value.get("status")).lower().replace("-", "_")
    if status not in {"pass", "needs_revision"}:
        status = "needs_revision"
    findings: list[dict[str, str]] = []
    raw_findings = value.get("findings")
    rows = raw_findings if isinstance(raw_findings, list) else []
    for row in rows[:20]:
        if not isinstance(row, Mapping):
            continue
        target = _normalize_target_stage(row.get("target_stage"))
        findings.append(
            {
                "target_stage": target,
                "severity": _normalize_severity(row.get("severity")),
                "issue": clean_text(row.get("issue"))[:800],
                "required_change": clean_text(row.get("required_change"))[:800],
            }
        )
    if status == "pass" and any(row.get("severity") in {"high", "critical"} for row in findings):
        status = "needs_revision"
    patch_requests = _review_findings_to_patch_requests(findings)
    return {
        "status": status,
        "summary": clean_text(value.get("summary"))[:1200],
        "findings": findings,
        "patch_requests": patch_requests,
    }


def _normalize_modules(value: object) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for row in rows[:32]:
        if not isinstance(row, Mapping):
            continue
        result.append(
            {
                "name": clean_text(row.get("name"))[:120],
                "responsibility": clean_text(row.get("responsibility"))[:500],
                "inputs": scalar_list(row.get("inputs"))[:12],
                "outputs": scalar_list(row.get("outputs"))[:12],
                "dependencies": scalar_list(row.get("dependencies"))[:12],
                "obligation_ids": scalar_list(row.get("obligation_ids") or row.get("contract_obligations"))[:24],
            }
        )
    return result


def _normalize_named_rows(value: object, *, max_rows: int) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, Any]] = []
    for row in rows[:max_rows]:
        if isinstance(row, Mapping):
            result.append({str(key): _coerce_jsonable(val) for key, val in row.items() if str(key).strip()})
        elif clean_text(row):
            result.append({"description": clean_text(row)})
    return result


def _base_context(
    *,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    resource_plan: Mapping[str, Any],
    domain_profile: Mapping[str, Any],
) -> str:
    return (
        f"Experiment contract JSON:\n{_json_block(_contract_view(contract))}\n\n"
        f"Result schema JSON:\n{_json_block(dict(result_schema))}\n\n"
        f"Resource plan JSON:\n{_json_block(dict(resource_plan))}\n\n"
        f"Domain profile JSON:\n{_json_block(_domain_profile_view(domain_profile))}\n"
    )


def _contract_view(contract: Mapping[str, Any]) -> dict[str, Any]:
    return contract_prompt_view(
        contract,
        max_task_chars=1200,
        max_requirements=24,
        max_success_criteria=16,
    )


def _domain_profile_view(profile: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(profile)
    data.pop("task_excerpt", None)
    packages = data.get("available_task_relevant_packages")
    if isinstance(packages, list):
        data["available_task_relevant_packages"] = packages[:80]
    return data


def _merge_review_feedback(feedback: dict[str, list[str]], review: Mapping[str, Any]) -> None:
    requests = _planning_patch_requests(review)
    if not requests:
        return
    for row in requests:
        target = _normalize_target_stage(row.get("target_stage"))
        message = clean_text(row.get("required_change")) or clean_text(row.get("issue"))
        if message:
            feedback.setdefault(target, []).append(message)


def _review_target_stage(review: Mapping[str, Any]) -> str:
    rows = _planning_patch_requests(review)
    ranks = {stage: index for index, stage in enumerate(PLANNING_STAGES)}
    best = "file_plan"
    for row in rows:
        target = _normalize_target_stage(row.get("target_stage"))
        if ranks[target] < ranks[best]:
            best = target
    return best


def _planning_patch_requests(review: Mapping[str, Any], *, round_index: int | None = None) -> list[dict[str, Any]]:
    # Explicit requests take precedence; findings supply only blocking changes.
    for key in ("patch_requests", "findings"):
        value = review.get(key)
        rows = value if isinstance(value, list) else []
        normalized: list[dict[str, Any]] = []
        for index, row in enumerate(rows[:12], start=1):
            if not isinstance(row, Mapping):
                continue
            severity = _normalize_severity(row.get("severity"))
            if key == "findings" and severity not in {"high", "critical"}:
                continue
            issue = clean_text(row.get("issue"))[:800]
            change = clean_text(row.get("required_change"))[:800]
            if not issue and not change:
                continue
            target = _normalize_target_stage(row.get("target_stage"))
            request_id = clean_text(row.get("id")) if key == "patch_requests" else ""
            item: dict[str, Any] = {
                "id": request_id or f"{target}-{index}",
                "target_stage": target,
                "severity": severity,
                "issue": issue,
                "required_change": change or issue,
            }
            if round_index is not None:
                item["round"] = round_index
            normalized.append(item)
        if normalized:
            return normalized
    return []


def _review_findings_to_patch_requests(
    findings: list[Mapping[str, Any]] | list[dict[str, str]],
    *,
    round_index: int | None = None,
) -> list[dict[str, Any]]:
    return _planning_patch_requests({"findings": findings}, round_index=round_index)


def _write_patch_requests(
    planning_dir: Path | None,
    *,
    latest: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> None:
    if planning_dir is None:
        return
    write_json(
        planning_dir / "review_patch_requests.json",
        {
            "schema_version": "greenfield_planning_patch_requests.v1",
            "latest": latest,
            "history": history[-40:],
        },
    )


def _review_passed(review: Mapping[str, Any]) -> bool:
    if clean_text(review.get("status")).lower() == "pass":
        return True
    findings = review.get("findings")
    rows = findings if isinstance(findings, list) else []
    return not any(
        isinstance(row, Mapping) and _normalize_severity(row.get("severity")) in {"high", "critical"}
        for row in rows
    )


def _finding_to_risk(row: Mapping[str, Any]) -> str:
    issue = clean_text(row.get("issue"))
    change = clean_text(row.get("required_change"))
    severity = clean_text(row.get("severity"))
    parts = [f"Planning review {severity} finding".strip()]
    if issue:
        parts.append(issue)
    if change:
        parts.append("Mitigation: " + change)
    return ". ".join(parts)


def _feedback_block(feedback: list[str]) -> str:
    if not feedback:
        return ""
    return "\nRevision feedback to address:\n" + "\n".join(f"- {item}" for item in feedback[-8:]) + "\n"


def _stages_from(stage: str) -> tuple[str, ...]:
    target = _normalize_target_stage(stage)
    index = PLANNING_STAGES.index(target)
    return PLANNING_STAGES[index:]


def _normalize_target_stage(value: object) -> str:
    text = clean_text(value).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "requirement": "requirements",
        "requirements_brief": "requirements",
        "architecture_outline": "architecture",
        "interface": "interfaces",
        "interface_contract": "interfaces",
        "file": "file_plan",
        "files": "file_plan",
        "fileplan": "file_plan",
    }
    target = aliases.get(text, text)
    return target if target in PLANNING_STAGES else "file_plan"


def _normalize_severity(value: object) -> str:
    text = clean_text(value).lower()
    return text if text in {"low", "medium", "high", "critical"} else "medium"


def _stage_system(stage: str) -> str:
    names = {
        "requirements": "You are a careful product and experiment requirements analyst. Extract only implementation-relevant obligations.",
        "architecture": "You are a pragmatic Python software architect. Design cohesive, bounded projects with clear module ownership.",
        "interfaces": "You are an interface-contract reviewer. Make cross-file calls, schemas, and stdout/metric contracts explicit.",
        "file_plan": "You are a code-generation file planner. Convert architecture and interfaces into a concrete, bounded file plan.",
    }
    return names[stage] + " Return strict JSON only."


PLANNING_REVIEW_SYSTEM = (
    "You are a strict planning reviewer for generated Python projects. "
    "Route each issue to the earliest planning stage that should be regenerated. Return strict JSON only."
)


def _stage_output_tokens(stage: str, resource_plan: Mapping[str, Any]) -> int:
    max_files = _positive_int(resource_plan.get("max_files"), 8)
    if stage == "requirements":
        return 900
    if stage == "architecture":
        return 1100 if max_files >= 12 else 950
    if stage == "interfaces":
        return 1200 if max_files >= 12 else 1000
    if max_files >= 24:
        return 1600
    if max_files >= 12:
        return 1400
    return 1200


def _agent_step_output_summary(stage: str, output: Mapping[str, Any]) -> dict[str, Any]:
    if stage == "requirements":
        return {
            "hard_requirement_count": len(scalar_list(output.get("hard_requirements"))),
            "deliverable_count": len(scalar_list(output.get("deliverables"))),
            "evaluation_target_count": len(scalar_list(output.get("evaluation_targets"))),
        }
    if stage == "architecture":
        modules = output.get("modules")
        return {
            "module_count": len(modules) if isinstance(modules, list) else 0,
            "risk_count": len(scalar_list(output.get("risks"))),
        }
    if stage == "interfaces":
        return {
            "shared_schema_count": len(output.get("shared_schemas")) if isinstance(output.get("shared_schemas"), list) else 0,
            "module_api_count": len(output.get("module_apis")) if isinstance(output.get("module_apis"), list) else 0,
            "contract_count": len(scalar_list(output.get("cross_file_contracts"))),
        }
    if stage == "file_plan":
        files = output.get("files")
        return {"file_count": len(files) if isinstance(files, list) else 0}
    if stage == "review":
        findings = output.get("findings")
        return {
            "review_status": output.get("status", "unknown"),
            "finding_count": len(findings) if isinstance(findings, list) else 0,
        }
    return {}


def _json_block(value: object) -> str:
    return json.dumps(value if value is not None else {}, indent=2, ensure_ascii=False)


def _coerce_jsonable(value: object) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_coerce_jsonable(item) for item in value[:24]]
    if isinstance(value, Mapping):
        return {str(key): _coerce_jsonable(val) for key, val in list(value.items())[:24]}
    return str(value)


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
