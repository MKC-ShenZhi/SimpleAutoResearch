"""Task-scoped accepted plans for the shared research application.

This module only describes and validates the small sequential plan consumed by
the application.  Attempts, budgets, and artifact storage remain owned by the
existing planning capability and :class:`SessionController`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re
from typing import Any, Mapping

from simple_ar.integrations.llm import LLMError


SCHEMA_VERSION = "research_task_plan.v1"
_EVIDENCE_CAPABILITIES = {"search_evidence": "search", "ingest_evidence": "document_ingest", "read_evidence": "read"}

_ACTION_CAPABILITIES = {
    "search": "search",
    "document_ingest": "document_ingest",
    "data_ingest": "data_ingest",
    "data_analysis": "data_analysis",
    "read": "read",
    "synthesize": "synthesize",
    "summarize": "summary",
    "assess_ideas": "assess_ideas",
    "research_design": "research_design",
    "prepare_execution": "prepare_execution",
    "implement": "implement",
    "baseline": "experiment",
    "experiment": "experiment",
    "analysis": "analysis",
    "matrix_analysis": "analysis",
    "report_write": "report_write",
    "report": "report",
    "report_audit": "report_audit",
}
_STEP_TEXT = {
    "search": ("Collect candidate sources from the accepted source plan.", "Selected papers and source diagnostics."),
    "document_ingest": ("Make selected source text available to the evidence reader.", "Records, chunks, and extraction limitations."),
    "data_ingest": ("Freeze supplied data and explicit column semantics.", "Validated input snapshot and descriptive settings."),
    "data_analysis": ("Describe selected numeric columns without inferring a scientific verdict.", "Descriptive statistics, editable figures and copied rebuilding inputs."),
    "read": ("Extract claims, methods, and limitations from available source text.", "Evidence cards and unresolved source claims."),
    "synthesize": ("Combine the evidence into an evidence-backed direction or summary.", "Synthesis status, candidate direction, and limitations."),
    "summarize": ("Persist the requested evidence-backed research summary.", "Summary artifact and its immutable source refs."),
    "assess_ideas": ("Compare candidate directions against evidence and constraints.", "Candidate assessment and unresolved evidence gaps."),
    "research_design": ("Turn the selected direction into the existing execution contract.", "Design contract and selection rationale."),
    "prepare_execution": ("Prepare the explicitly configured project workspace or task environment.", "Prepared execution boundary and observed preparation records."),
    "implement": ("Locate, scope, patch, review, and validate the requested code change.", "Patch diff, validation, and implementation lineage."),
    "baseline": ("Measure the explicitly configured baseline before a candidate.", "Canonical baseline measurement and diagnostics."),
    "experiment": ("Run the explicitly configured experiment or candidate measurement.", "Canonical measurement and execution status."),
    "analysis": ("Interpret the completed measurement using its paired artifacts.", "Analysis handoff, metrics, and limitations."),
    "matrix_analysis": ("Compare the accepted paired measurements.", "Paired comparison and measurement coverage."),
    "report_write": ("Write the report from the accepted evidence and measurements.", "Report sections and source lineage."),
    "report": ("Assemble the report without changing the underlying evidence.", "Assembled report and citation trace."),
    "report_audit": ("Audit the assembled report against its sources and protocol.", "Audit findings and semantic limitations."),
    "supplement_baseline": ("Measure the unchanged baseline for an evidence-driven supplement.", "Supplement baseline measurement and diagnostics."),
    "supplement_candidate": ("Measure the current candidate under the newly accepted condition.", "Supplement candidate measurement and diagnostics."),
    "reanalysis": ("Re-analyze the supplement together with its explicit paired evidence.", "Updated analysis, decision basis, and limitations."),
    "research_design_revision": ("Re-evaluate a rejected candidate using the supplied research alternatives and observed evidence.", "Revised research design contract or an explicit unresolved design limitation."),
    "prepare_candidate": ("Create a fresh isolated workspace from the recorded original project for a candidate revision.", "Revision workspace lineage and copy report."),
    "revise_candidate": ("Apply and validate the analysis-directed candidate revision in the isolated workspace.", "Candidate revision patch, validation, and lineage."),
    "research_candidate": ("Measure the analysis-directed candidate revision under the accepted comparison condition.", "Candidate revision measurement and diagnostics."),
    "repair_candidate": ("Repair a failed candidate revision without changing its scientific hypothesis.", "Technical repair patch and failed-run lineage."),
    "retest_candidate": ("Remeasure the technically repaired candidate under the same protocol.", "Repaired candidate measurement."),
    "refine_implementation": ("Resolve explicit implementation design questions without changing the protocol.", "Clarified design or unresolved evidence needs."),
    "prepare_implementation": ("Prepare a fresh workspace for the clarified implementation.", "New workspace with preserved original lineage."),
    "search_evidence": ("Search explicit unresolved reading questions using their saved handoff queries.", "New candidates and source diagnostics."),
    "ingest_evidence": ("Ingest new sources while preserving the previous evidence.", "Cumulative document bundle and extraction limits."),
    "read_evidence": ("Read new evidence for the recorded source questions.", "Cumulative evidence and remaining questions."),
}
_ACTION_RE = re.compile(
    r"^(?:repair:\d+|retest:\d+|matrix_repair_\d+|matrix_baseline_\d+|"
    r"matrix_candidate(?:_r\d+)?_\d+|supplement_baseline:\d+(?:_\d+)?|"
    r"supplement_candidate:\d+(?:_\d+)?|reanalysis:\d+|research_design_revision:\d+|prepare_candidate:\d+|"
    r"revise_candidate:\d+|research_candidate:\d+(?:_\d+)?|repair_candidate:\d+_\d+|"
    r"retest_candidate:\d+_\d+|refine_implementation:\d+|prepare_implementation:\d+|"
    r"(?:search_evidence|ingest_evidence|read_evidence):\d+)$"
)
_PROCESS_CAPABILITIES = {"prepare_execution", "implement", "experiment"}
_PROCESS_RESPONSIBILITIES = {
    "prepare_execution": "Prepare the configured isolated workspace or approved task venv, and record actual preparation results.",
    "implement": "Locate, scope, patch, and validate the authorized code change; it does not choose the research protocol.",
    "experiment": "Run one configured baseline or candidate measurement under the accepted protocol.",
    "analysis": "Interpret completed measurements and report limitations; it does not launch an unplanned process.",
    "summary": "Persist the requested evidence-backed source summary without creating measurements.",
    "report": "Assemble the requested report from accepted evidence and measurements without creating new evidence.",
}
_SEQUENTIAL_PREREQUISITES = {
    "read": ("document_ingest",),
    "synthesize": ("read",),
    "summarize": ("synthesize",),
    "assess_ideas": ("synthesize",),
    "research_design": ("synthesize", "assess_ideas"),
    "prepare_execution": ("research_design",),
    "baseline": ("research_design",),
    "experiment": ("research_design",),
    "analysis": ("experiment",),
    "matrix_analysis": ("matrix_candidate",),
    "report_write": ("synthesize",),
    "report": ("report_write",),
    "report_audit": ("report",),
}


@dataclass(frozen=True, slots=True)
class TaskPlanStep:
    """One logical step in the accepted sequential task plan."""

    step_id: str
    action: str
    capability: str
    state_name: str
    problem_solved: str
    observation: str
    condition: str = ""

    def to_dict(self) -> dict[str, Any]:
        row = {
            "step_id": self.step_id,
            "action": self.action,
            "capability": self.capability,
            "state_name": self.state_name,
            "problem_solved": self.problem_solved,
            "observation": self.observation,
        }
        if self.condition:
            row["condition"] = self.condition
        return row


@dataclass(frozen=True, slots=True)
class TaskPlanRequest:
    """Task context used to create one accepted plan."""

    task_kind: str
    goal: str
    request_text: str
    objective: str = ""
    hard_constraints: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    requested_outputs: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    assets: tuple[Mapping[str, Any], ...] = ()
    config: Mapping[str, object] = field(default_factory=dict)
    execution: Mapping[str, object] | None = None
    execution_protocol_accepted: bool = False
    prior_plan: TaskPlanResult | None = field(default=None, repr=False, compare=False)
    use_llm: bool = False
    llm_client: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.task_kind not in {"survey", "bug_fix", "measurement", "reproduction", "writing", "data_analysis", "research"}:
            raise ValueError(f"Unsupported task kind: {self.task_kind!r}")
        if not self.goal.strip() or not self.request_text.strip():
            raise ValueError("Task plan goal and request_text cannot be empty.")
        if self.use_llm and self.llm_client is None:
            raise ValueError("TaskPlanRequest.llm_client is required when use_llm is true.")
        object.__setattr__(self, "config", dict(self.config))
        object.__setattr__(self, "assets", tuple(dict(asset) for asset in self.assets))
        if self.execution is not None:
            object.__setattr__(self, "execution", dict(self.execution))


@dataclass(frozen=True, slots=True)
class TaskPlanResult:
    """Persisted plan and its producer metadata."""

    task_kind: str
    goal: str
    steps: tuple[TaskPlanStep, ...]
    mode: str
    model: str = ""
    assumptions: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()
    status: str = "accepted"

    def to_handoff_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": self.status,
            "task_kind": self.task_kind,
            "goal": self.goal,
            "mode": self.mode,
            "model": self.model,
            "assumptions": list(self.assumptions),
            "diagnostics": list(self.diagnostics),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_handoff_dict(cls, data: Mapping[str, Any]) -> "TaskPlanResult":
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Expected a {SCHEMA_VERSION} object.")
        rows = data.get("steps")
        if not isinstance(rows, list):
            raise ValueError("Accepted task plan steps must be a list.")
        steps = _normalize_steps(rows)
        if str(data.get("status") or "accepted") != "accepted":
            raise ValueError("Only accepted task plans can be dispatched.")
        task_kind = str(data.get("task_kind") or "research")
        goal = str(data.get("goal") or "").strip()
        if task_kind not in {"survey", "bug_fix", "measurement", "reproduction", "writing", "data_analysis", "research"} or not goal:
            raise ValueError("Accepted task plan has an invalid task kind or goal.")
        if any(step.action.split(":")[0] in _EVIDENCE_CAPABILITIES for step in steps) and not any(step.action == "search" for step in steps):
            raise ValueError("Evidence follow-up requires a search-authorized plan.")
        return cls(
            task_kind=task_kind,
            goal=goal,
            steps=steps,
            mode=str(data.get("mode") or "deterministic"),
            model=str(data.get("model") or ""),
            assumptions=_strings(data.get("assumptions")),
            diagnostics=_strings(data.get("diagnostics")),
        )


def build_task_plan(request: TaskPlanRequest, *, trace: list[dict[str, Any]] | None = None) -> TaskPlanResult:
    """Build, validate, and return the plan that the application will consume."""

    if request.execution_protocol_accepted and request.prior_plan is not None:
        return _extend_accepted_research_plan(request)
    if request.task_kind not in {"bug_fix", "data_analysis"} and not implementation_only(request.execution, request.requested_outputs) and _provided_materials_only(request) and not request.config.get("research_local_documents"):
        raise ValueError("Provided-materials planning requires supplied local documents before model planning.")
    defaults = default_task_steps(request)
    mode = "deterministic"
    model = ""
    diagnostics: list[str] = []
    steps = _normalize_steps(defaults)
    if request.use_llm:
        client = request.llm_client
        if client is None:
            raise LLMError("Task planning was requested but no client was provided.")
        prompt = _llm_prompt(request, defaults)
        for attempt in range(2):
            response = client.ask_json(
                """You are the task planner for a bounded research application.
Return JSON only. Propose a short sequential plan from the supplied
task, assets, constraints, and boundary preset. Do not invent
capabilities, processes, datasets, files, or parallel branches.
The application will validate and execute the plan.""",
                prompt,
                label="task-plan",
                max_output_tokens=_planning_output_tokens(request.config),
            )
            record = {"response": response, "validation_error": ""}
            if trace is not None:
                trace.append(record)
            try:
                raw = response.get("steps") if isinstance(response, Mapping) else None
                if not isinstance(raw, list) and isinstance(response, Mapping) and len(response) == 1:
                    # Some providers wrap a corrected JSON object once. Accept
                    # only an unambiguous single wrapper, then apply every normal
                    # action, prerequisite, condition and authorization check.
                    wrapper, nested = next(iter(response.items()))
                    if isinstance(nested, Mapping) and isinstance(nested.get("steps"), list):
                        raw = nested["steps"]
                        record["normalized_from_wrapper"] = str(wrapper)
                if not isinstance(raw, list) or not raw:
                    raise ValueError("Task-plan response must contain a non-empty steps list.")
                # Routing and storage names belong to the executor, not the model.
                rows = [
                    {key: value for key, value in row.items() if key not in {"capability", "state_name"}}
                    if isinstance(row, Mapping) else row for row in raw
                ]
                proposed = _normalize_steps(rows)
                steps, added = _complete_required_steps(request, proposed)
                if added:
                    record["compiler_added_actions"] = list(added)
                _validate_sequence(request, steps)
            except ValueError as exc:
                record["validation_error"] = str(exc)
                diagnostics.append(str(exc))
                if attempt:
                    raise ValueError(f"Task plan invalid after one correction: {exc}") from exc
                prompt = (_llm_prompt(request, defaults) + "\n\nCorrect this rejected proposal within the same permissions. "
                          "Return the complete steps array.\n" + json.dumps(record, ensure_ascii=False))
            else:
                if added:
                    diagnostics.append(
                        "Plan compiler supplied required actions omitted by the model: "
                        + ", ".join(added)
                    )
                break
        mode = "llm"
        model = str(getattr(client, "model", ""))

    else:
        _validate_sequence(request, steps)
    return TaskPlanResult(
        task_kind=request.task_kind,
        goal=request.goal.strip(),
        steps=steps,
        mode=mode,
        model=model,
        assumptions=tuple(str(item).strip() for item in request.hard_constraints if str(item).strip()),
        diagnostics=tuple(diagnostics),
    )


def _extend_accepted_research_plan(request: TaskPlanRequest) -> TaskPlanResult:
    """Append executable work without rewriting the accepted evidence route."""
    prior = request.prior_plan
    if prior is None or prior.task_kind != request.task_kind or prior.goal != request.goal.strip():
        raise ValueError("Execution extension requires the accepted plan for this same task.")
    if not prior.steps or prior.steps[-1].action != "research_design" or not request.execution:
        raise ValueError("Execution extension requires a completed design checkpoint and execution boundary.")
    if any(step.capability in _PROCESS_CAPABILITIES for step in prior.steps):
        raise ValueError("The accepted plan already contains execution actions.")
    requested = {str(item).strip().lower() for item in request.requested_outputs}
    report_condition = "" if requested & {"report", "paper", "full_paper"} else "on_request:report"
    rows = [step.to_dict() for step in prior.steps]
    rows.extend(_execution_steps(request.execution))
    rows.extend(_row(action, condition=report_condition)
                for action in ("report_write", "report", "report_audit"))
    steps = _normalize_steps(rows)
    _validate_sequence(request, steps)
    return replace(prior, steps=steps, diagnostics=(
        *prior.diagnostics, "Appended execution and delivery to the accepted evidence route.",
    ))


def default_task_steps(request: TaskPlanRequest) -> list[dict[str, Any]]:
    """Return explicit offline defaults; these are a planning seed, not dispatch."""
    if implementation_only(request.execution, request.requested_outputs):
        steps = []
        if request.config.get("research_materials_only") is False:
            steps.append(_row("search"))
        if steps or request.config.get("research_local_documents"):
            steps.extend((_row("document_ingest"), _row("read")))
        steps.extend(_bug_fix_steps(request.execution))
        if set(request.requested_outputs) & {"report", "paper", "full_paper"}:
            steps.extend(_row(action) for action in ("report_write", "report", "report_audit"))
        return steps
    if request.task_kind == "data_analysis":
        actions = ["data_ingest", "data_analysis"]
        if "report" in request.requested_outputs:
            actions.extend(("document_ingest", "report_write", "report", "report_audit"))
        return [_row(action) for action in actions]

    if request.task_kind == "bug_fix":
        return _bug_fix_steps(request.execution or {})

    if request.task_kind == "measurement":
        return ([_row("prepare_execution")] if "environment" in (request.execution or {}) else []) + [_row("experiment"), _row("analysis")]

    if request.task_kind == "writing":
        return [_row(action) for action in ("document_ingest", "report_write", "report", "report_audit")]

    if request.task_kind == "reproduction":
        execution = request.execution or {}
        steps = [_row(action) for action in ("document_ingest", "read", "synthesize", "experiment", "analysis")]
        if "environment" in execution or (execution.get("code_task") and _needs_preparation(execution)):
            steps.insert(3, _row("prepare_execution"))
        if execution.get("code_task"):
            steps.insert(next(i for i, row in enumerate(steps) if row["action"] == "experiment"), _row("implement"))
        if set(request.requested_outputs) & {"report", "paper", "full_paper"}:
            steps.extend(_row(action) for action in ("report_write", "report", "report_audit"))
        return steps

    if _provided_materials_only(request):
        # Supplied papers are an input boundary, not the output of a fake
        # search attempt.  The downstream reader and report contracts still
        # consume the canonical document bundle.
        steps = [_row("document_ingest")]
        if not report_from_documents(request):
            steps.extend(_row(action) for action in ("read", "synthesize"))
    else:
        steps = [
            _row("search"),
            _row("document_ingest"),
            _row("read"),
        ]
        if not report_from_documents(request):
            steps.append(_row("synthesize"))
    if "summarize" in _required_output_actions(request):
        steps.append(_row("summarize"))
    requested = {str(item).strip().lower() for item in request.requested_outputs}
    intents = {str(item).strip().lower() for item in request.intents}
    needs_execution = bool(requested & {"experiment", "experiments", "code", "code_task"})
    needs_assessment = needs_execution or bool(requested & {"assessment", "idea_assessment", "idea_comparison", "design", "research_design"}) or bool(
        intents & {"assess", "assessment", "evaluate_idea", "idea_assessment"}
    )
    if needs_assessment:
        steps.append(_row("assess_ideas"))
    execution = request.execution or {}
    if needs_execution or requested & {"design", "research_design"}:
        steps.append(_row("research_design"))
    if needs_execution and not request.execution_protocol_accepted:
        # The first plan is intentionally a short fact/design checkpoint. The
        # same accepted-plan capability is invoked again after design supplies
        # the executable protocol.
        return steps
    if requested & {"experiment", "experiments"}:
        steps.extend(_execution_steps(execution))
    report_condition = "" if requested & {"report", "paper", "full_paper"} else "on_request:report"
    steps.extend([
        _row("report_write", condition=report_condition),
        _row("report", condition=report_condition),
        _row("report_audit", condition=report_condition),
    ])
    return steps


def _bug_fix_steps(execution: Mapping[str, object]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if not execution or _needs_preparation(execution):
        steps.append(_row("prepare_execution"))
    steps.append(_row("implement"))
    return steps


def implementation_only(execution: Mapping[str, object] | None, outputs) -> bool:
    """Distinguish an authorized code deliverable from scientific measurement.

    The task label is not authority. A real CodeTask boundary and an explicit
    code output are required; experiment/design requests keep their protocol.
    Command/scope validation remains owned by preparation and implementation.
    """
    requested = {str(item).strip().lower() for item in outputs}
    return (isinstance(execution, Mapping) and isinstance(execution.get("code_task"), Mapping)
            and bool(requested & {"bug_fix", "code", "code_task", "code_patch", "patch"})
            and not requested & {"experiment", "experiments", "design", "research_design", "assessment", "idea_assessment", "idea_comparison"})


def append_research_followup(
    plan: TaskPlanResult,
    iteration: int,
    *,
    action: str = "supplement",
    pair_count: int = 0,
    supplement_count: int = 1,
    revision_base: str = "candidate",
    repair_count: int = 0,
) -> TaskPlanResult:
    """Extend one accepted plan with one analysis-authorized research action.

    This reuses the accepted-plan artifact and existing experiment/analysis
    capabilities. It never creates a second lifecycle or a technical repair
    alias; the application calls it only after a persisted research decision.
    """

    if type(iteration) is not int or iteration < 1:
        raise ValueError("Research follow-up iteration must be a positive integer.")
    if action not in {"supplement", "revise_candidate"}:
        raise ValueError("Research follow-up action must be supplement or revise_candidate.")
    if type(pair_count) is not int or pair_count < 0:
        raise ValueError("Research follow-up pair_count must be a non-negative integer.")
    if type(supplement_count) is not int or supplement_count < 1:
        raise ValueError("Research supplement count must be a positive integer.")
    if revision_base not in {"candidate", "baseline"}:
        raise ValueError("Research revision_base must be candidate or baseline.")
    if type(repair_count) is not int or repair_count < 0:
        raise ValueError("Research repair_count must be a non-negative integer.")
    if repair_count and pair_count:
        raise ValueError("Paired follow-up repair requires condition-specific routing and is not supported.")
    marker = f"reanalysis:{iteration}"
    if any(step.action == marker for step in plan.steps):
        return plan
    rows = [step.to_dict() for step in plan.steps]
    if action == "supplement":
        baseline_actions = tuple(
            f"supplement_baseline:{iteration}"
            if supplement_count == 1 else f"supplement_baseline:{iteration}_{index}"
            for index in range(supplement_count)
        )
        candidate_actions = tuple(
            f"supplement_candidate:{iteration}"
            if supplement_count == 1 else f"supplement_candidate:{iteration}_{index}"
            for index in range(supplement_count)
        )
        supplement_rows: list[dict[str, Any]] = []
        previous = ""
        for baseline_action in baseline_actions:
            state = _state_name(baseline_action)
            supplement_rows.append(_row(
                baseline_action,
                condition="on_decision:supplement" if not previous else f"after_success:{previous}",
            ))
            previous = state
        for candidate_action in candidate_actions:
            state = _state_name(candidate_action)
            supplement_rows.append(_row(candidate_action, condition=f"after_success:{previous}"))
            previous = state
        # A failed measurement is still an observation. Reanalysis must not be
        # skipped merely because the final planned experiment did not pass.
        supplement_rows.append(_row(marker, condition=f"after_observation:{_state_name(baseline_actions[0])}"))
        followup_rows = tuple(supplement_rows)
        message = f"Accepted one analysis-directed supplement round {iteration}."
    else:
        preparation = f"prepare_candidate:{iteration}"
        revision = f"revise_candidate:{iteration}"
        candidate_actions = (
            tuple(f"research_candidate:{iteration}_{index}" for index in range(pair_count))
            if pair_count else (f"research_candidate:{iteration}",)
        )
        candidate_rows = []
        previous = _state_name(revision)
        for candidate_action in candidate_actions:
            candidate_rows.append(_row(candidate_action, condition=f"after_success:{previous}"))
            previous = _state_name(candidate_action)
        design_revision = f"research_design_revision:{iteration}"
        prefix_rows = (
            (_row(design_revision, condition="on_decision:revise_candidate"),)
            if revision_base == "baseline" else ()
        )
        preparation_condition = (
            f"after_success:{_state_name(design_revision)}"
            if revision_base == "baseline" else "on_decision:revise_candidate"
        )
        repair_rows: list[dict[str, Any]] = []
        previous_measurement = _state_name(candidate_actions[-1])
        for repair_index in range(1, repair_count + 1):
            repair_action = f"repair_candidate:{iteration}_{repair_index}"
            retest_action = f"retest_candidate:{iteration}_{repair_index}"
            repair_rows.append(_row(repair_action, condition=f"on_failure:{previous_measurement}"))
            repair_rows.append(_row(retest_action, condition=f"after_success:{_state_name(repair_action)}"))
            previous_measurement = _state_name(retest_action)
        followup_rows = (
            *prefix_rows,
            _row(preparation, condition=preparation_condition),
            _row(revision, condition=f"after_success:{_state_name(preparation)}"),
            *candidate_rows,
            *repair_rows,
            _row(marker, condition=f"after_observation:{_state_name(candidate_actions[0])}"),
        )
        message = f"Accepted one analysis-directed candidate revision round {iteration}."
    insertion = next(
        (index for index, row in enumerate(rows)
         if str(row.get("action") or "") in {"report_write", "report", "report_audit"}),
        len(rows),
    )
    rows[insertion:insertion] = followup_rows
    return replace(
        plan,
        steps=_normalize_steps(rows),
        diagnostics=(*plan.diagnostics, message),
    )


def _execution_steps(execution: Mapping[str, object]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if _needs_preparation(execution):
        steps.append(_row("prepare_execution"))
    pairs = execution.get("pairs", ())
    if isinstance(pairs, (list, tuple)) and pairs:
        count = len(pairs)
        baseline_policy = str(execution.get("baseline_policy") or "run").strip().lower()
        if baseline_policy not in {"skip", "reuse"}:
            steps.extend(_row(f"matrix_baseline_{i}") for i in range(count))
        if isinstance(execution.get("code_task"), Mapping):
            steps.append(_row("implement"))
        limit = _repair_limit(execution)
        for revision in range(limit + 1):
            for index in range(count):
                action = _matrix_candidate_key(revision, index)
                condition = "" if revision == 0 and index == 0 else (
                    f"after_success:{_matrix_candidate_key(revision, index - 1)}"
                    if index else f"after_success:matrix_repair_{revision}"
                )
                steps.append(_row(action, condition=condition))
            if revision < limit:
                steps.append(_row(
                    f"matrix_repair_{revision + 1}",
                    condition=f"on_failure_prefix:{_matrix_candidate_key(revision, 0).rsplit('_', 1)[0]}",
                ))
        steps.append(_row("matrix_analysis"))
        return steps

    baseline_policy = str(execution.get("baseline_policy") or "").strip().lower()
    baseline_requested = baseline_policy == "run" or (
        "baseline" in execution and baseline_policy not in {"skip", "reuse"}
    )
    if baseline_requested:
        steps.append(_row("baseline"))
    if isinstance(execution.get("code_task"), Mapping):
        steps.append(_row("implement"))
    steps.append(_row("experiment"))
    previous = "experiment"
    for index in range(1, _repair_limit(execution) + 1):
        steps.append(_row(f"repair:{index}", condition=f"on_failure:{previous}"))
        current = f"experiment_repair_{index}"
        steps.append(_row(f"retest:{index}", condition=f"after_success:repair_{index}"))
        previous = current
    steps.append(_row("analysis"))
    return steps


def _planning_boundary(request: TaskPlanRequest) -> dict[str, Any]:
    """Describe the current process boundary for both prompting and validation.

    The execution rows are derived from the existing protocol materializer.  A
    pre-design research plan may see those rows as deferred, but it cannot
    dispatch them until the design capability has accepted the protocol.
    """

    execution = request.execution if isinstance(request.execution, Mapping) else None
    configured = bool(execution)
    allowed_rows: list[dict[str, Any]] = []
    deferred_rows: list[dict[str, Any]] = []
    if implementation_only(execution, request.requested_outputs) or request.task_kind == "bug_fix":
        allowed_rows = _bug_fix_steps(execution or {})
        checkpoint = "implementation"
        unauthorized_reason = "The supplied code boundary authorizes preparation and implementation, not scientific measurement."
    elif request.task_kind in {"measurement", "reproduction"}:
        allowed_rows = default_task_steps(request) if request.task_kind == "reproduction" else ([_row("prepare_execution")] if configured and "environment" in execution else []) + ([_row("experiment")] if configured else [])
        checkpoint = request.task_kind
        unauthorized_reason = "Direct measurement authorizes only its supplied execution command."
    elif request.task_kind in {"survey", "writing", "data_analysis"}:
        checkpoint = "evidence"
        unauthorized_reason = "Survey/writing routing does not authorize process actions."
    elif not configured:
        checkpoint = "evidence"
        unauthorized_reason = "No execution boundary was supplied for this research task."
    elif request.execution_protocol_accepted:
        allowed_rows = _execution_steps(execution or {})
        checkpoint = "execution"
        unauthorized_reason = "The action is not part of the accepted execution protocol."
    else:
        deferred_rows = _execution_steps(execution or {})
        checkpoint = "research_design"
        unauthorized_reason = "The action is not part of the supplied execution boundary."

    def process_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "action": row["action"],
                "condition": row.get("condition", ""),
                "capability": row["capability"],
            }
            for row in rows
            if row.get("capability") in _PROCESS_CAPABILITIES
        ]

    return {
        "protocol_accepted": bool(request.execution_protocol_accepted),
        "execution_configured": configured,
        "execution_phase": checkpoint,
        "allowed_process_steps": process_rows(allowed_rows),
        "deferred_process_steps": process_rows(deferred_rows),
        "unauthorized_process_reason": unauthorized_reason,
        "process_responsibilities": dict(_PROCESS_RESPONSIBILITIES),
    }


def insert_implementation_refinement(plan: TaskPlanResult, state_name: str, iteration: int) -> TaskPlanResult:
    """Insert recovery before the unfinished implementation, leaving measurements alone."""
    rows = [step.to_dict() for step in plan.steps]
    position = next(i for i, step in enumerate(plan.steps) if step.state_name == state_name)
    additions = [_row(f"refine_implementation:{iteration}"), _row(f"prepare_implementation:{iteration}")]
    rows[position:position] = additions
    return replace(plan, steps=tuple(_normalize_steps(rows)))


def insert_evidence_followup(
    plan: TaskPlanResult, *, round_index: int, read_state: str, queries: tuple[str, ...] | list[str],
) -> TaskPlanResult:
    """Insert at most one reading-directed evidence cycle, without a new payload.

    Queries remain in the existing read handoff. The app must verify they are
    unexecuted, that read_state is completed, and downstream work is unexecuted;
    a plan alone contains neither execution history nor remaining budgets.
    """
    if type(round_index) is not int or round_index != 1:
        raise ValueError("Evidence follow-up supports at most one round (round_index=1).")
    if not isinstance(queries, (list, tuple)) or not queries or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("Evidence follow-up requires nonempty explicit queries from the read handoff.")
    if len({" ".join(q.split()).casefold() for q in queries}) != len(queries):
        raise ValueError("Evidence follow-up queries must be distinct.")
    steps = _normalize_steps([step.to_dict() for step in plan.steps])
    if plan.status != "accepted" or not any(step.action == "search" for step in steps):
        raise ValueError("Evidence follow-up requires an accepted search-authorized plan.")
    if read_state != "read" or not any(step.action == "read" and step.state_name == read_state for step in steps):
        raise ValueError("The first evidence cycle must depend on the base read state.")
    if any(step.action == "read_evidence:1" for step in steps):
        return plan
    position = next(i for i, step in enumerate(steps) if step.state_name == read_state) + 1
    if not any(step.action in {"synthesize", "implement", "report_write"} for step in steps[position:]):
        raise ValueError("Evidence follow-up needs a downstream evidence consumer.")
    if any(step.action in {"synthesize", "implement", "report_write"} for step in steps[:position]):
        raise ValueError("Evidence follow-up must precede its evidence consumers.")
    rows = [step.to_dict() for step in steps]
    rows[position:position] = [
        _row("search_evidence:1", condition="after_success:read"),
        _row("ingest_evidence:1", condition="after_success:search_evidence_1"),
        _row("read_evidence:1", condition="after_success:documents_evidence_1"),
    ]
    return replace(plan, steps=_normalize_steps(rows))


def _row(action: str, *, condition: str = "") -> dict[str, Any]:
    problem, observation = _STEP_TEXT.get(action.split(":", 1)[0], ("Advance the accepted task plan.", "Inspect the declared capability result."))
    return {
        "step_id": action,
        "action": action,
        "capability": _capability(action),
        "state_name": _state_name(action),
        "problem_solved": problem,
        "observation": observation,
        "condition": condition,
    }


def _normalize_steps(rows: list[Any]) -> tuple[TaskPlanStep, ...]:
    steps: list[TaskPlanStep] = []
    seen: set[str] = set()
    state_seen: set[str] = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Task plan step {index + 1} must be an object.")
        action = str(raw.get("action") or "").strip()
        # The summary capability and its action have different public names.
        # Canonicalize that unambiguous spelling before validating the boundary.
        if action == "summary":
            action = "summarize"
        if action in {"plan", "task_plan"} or not (_is_known_action(action)):
            raise ValueError(f"Unsupported task plan action: {action!r}")
        step_id = str(raw.get("step_id") or action).strip()
        if not step_id or step_id in seen:
            raise ValueError(f"Task plan step ids must be unique: {step_id!r}")
        seen.add(step_id)
        expected_capability, expected_state = _capability(action), _state_name(action)
        capability = str(raw.get("capability") or expected_capability).strip()
        state_name = str(raw.get("state_name") or expected_state).strip()
        if capability != expected_capability or state_name != expected_state:
            raise ValueError(f"Task plan boundary mismatch for {action!r}.")
        if state_name in state_seen:
            raise ValueError(f"Task plan state names must be unique: {state_name!r}")
        state_seen.add(state_name)
        condition = str(raw.get("condition") or "").strip()
        if condition and not _valid_condition(condition):
            raise ValueError(f"Unsupported task plan condition: {condition!r}")
        prefix = action.split(":")[0]
        if prefix in _EVIDENCE_CAPABILITIES:
            dependency = {"search_evidence": "read", "ingest_evidence": "search_evidence_1",
                          "read_evidence": "documents_evidence_1"}[prefix]
            if action.split(":")[1] != "1" or condition != f"after_success:{dependency}" or not steps or steps[-1].state_name != dependency:
                raise ValueError("Evidence follow-up requires one round and its preceding read/search/ingest dependency.")
            if not any(p.action == "search" for p in steps) or any(p.action in {"synthesize", "report_write"} for p in steps):
                raise ValueError("Evidence follow-up requires prior search authorization and must precede synthesis/writing.")
        if condition.startswith("after_observation:"):
            observed_state = condition.split(":", 1)[1].strip()
            if not any(
                previous.state_name == observed_state and previous.capability == "experiment"
                for previous in steps
            ):
                raise ValueError(
                    f"Observation condition {condition!r} must reference an earlier experiment step."
                )
        problem, observation = _STEP_TEXT.get(action, ("Advance the accepted task plan.", "Inspect the declared capability result."))
        steps.append(TaskPlanStep(
            step_id=step_id,
            action=action,
            capability=capability,
            state_name=state_name,
            problem_solved=str(raw.get("problem_solved") or problem).strip(),
            observation=str(raw.get("observation") or observation).strip(),
            condition=condition,
        ))
    if not steps:
        raise ValueError("Accepted task plan cannot be empty.")
    evidence = [step.action for step in steps if step.action.split(":")[0] in _EVIDENCE_CAPABILITIES]
    if evidence and evidence != ["search_evidence:1", "ingest_evidence:1", "read_evidence:1"]:
        raise ValueError("Evidence follow-up must contain exactly one complete ordered cycle.")
    return tuple(steps)


def _required_output_actions(request: TaskPlanRequest) -> tuple[str, ...]:
    """Required deliverables/checkpoints, never inferred from the model's prose."""

    requested = {str(item).strip().lower() for item in request.requested_outputs}
    if implementation_only(request.execution, request.requested_outputs):
        return ("implement", "report_audit") if requested & {"report", "paper", "full_paper"} else ("implement",)
    if request.task_kind == "bug_fix":
        return ()
    if request.task_kind == "measurement":
        return ("analysis",)
    actions: list[str] = []
    if not requested or requested & {"summarize", "summary", "research_summary"}:
        actions.append("summarize")
    if requested & {"assessment", "idea_assessment", "idea_comparison"}:
        actions.append("assess_ideas")
    needs_execution = bool(requested & {"experiment", "experiments", "code", "code_task"})
    if requested & {"design", "research_design"} or needs_execution and not request.execution_protocol_accepted:
        actions.append("research_design")
    if requested & {"report", "paper", "full_paper"} and not (needs_execution and not request.execution_protocol_accepted):
        actions.append("report_audit")
    if not actions and not request.execution_protocol_accepted:
        actions.append("synthesize")
    return tuple(actions)


def report_from_documents(request: TaskPlanRequest) -> bool:
    """A report-only task can write from original sources without a separate brief."""
    requested = {str(item).strip().lower() for item in request.requested_outputs}
    intents = {str(item).strip().lower() for item in request.intents}
    return (
        request.task_kind in {"survey", "research"}
        and (not _provided_materials_only(request) or bool(request.config.get("research_local_documents")))
        and bool(requested) and requested <= {"report", "paper", "full_paper"}
        and not request.execution
        and not intents & {"assess", "assessment", "evaluate_idea", "idea_assessment"}
    )


def _prerequisites(request: TaskPlanRequest, action: str, *, actions: set[str]) -> tuple[str, ...]:
    """One input rule shared by plan compilation and order validation."""
    if implementation_only(request.execution, request.requested_outputs):
        if action == "prepare_execution":
            return ()
        if action == "implement":
            return tuple(name for name in ("prepare_execution", "read") if name in actions)
        if action == "report_write":
            return ("implement",)
    if action == "document_ingest":
        return ("search",) if "search" in actions or not request.config.get("research_local_documents") else ()
    if action == "report_write" and report_from_documents(request):
        # A selected note/brief is an actual Writer input, not post-delivery work.
        for producer in ("synthesize", "read"):
            if producer in actions:
                return (producer,)
        return ("document_ingest",) if _provided_materials_only(request) else ("read",)
    return _SEQUENTIAL_PREREQUISITES.get(action, ())


def _complete_required_steps(
    request: TaskPlanRequest, proposed: tuple[TaskPlanStep, ...],
) -> tuple[tuple[TaskPlanStep, ...], tuple[str, ...]]:
    """Compile only unambiguous prerequisites; never erase or reorder a proposal.

    This cannot legalize an unauthorized process, change a condition, invent a
    material source, or repair a model's explicitly reversed ordering. The raw
    proposal and the inserted actions remain visible in the attempt trace.
    """

    if implementation_only(request.execution, request.requested_outputs) or request.task_kind in {"bug_fix", "measurement", "reproduction", "writing", "data_analysis"}:
        return proposed, ()
    steps = list(proposed)
    explicit = {step.action for step in steps}

    def before(action: str, index: int) -> int:
        for dependency in _prerequisites(request, action, actions={step.action for step in steps}):
            if dependency in explicit or dependency in {step.action for step in steps}:
                continue
            # Only a real supplied document can replace search. An explicitly
            # materials-only task without one remains invalid, not upgraded to
            # network access by the compiler.
            if dependency == "search" and _provided_materials_only(request):
                continue
            index = before(dependency, index)
            steps.insert(index, _normalize_steps([_row(dependency)])[0])
            index += 1
        return index

    for action in _required_output_actions(request):
        if action not in {step.action for step in steps}:
            downstream = {
                "assess_ideas": {"research_design", "report_write", "report", "report_audit"},
                "research_design": {"report_write", "report", "report_audit"},
            }.get(action, set())
            index = next((index for index, step in enumerate(steps)
                          if step.action in downstream), len(steps))
            steps.insert(index, _normalize_steps([_row(action)])[0])
    for step in tuple(steps):
        index = steps.index(step)
        before(step.action, index)
    return tuple(steps), tuple(step.action for step in steps if step.action not in explicit)


def _validate_sequence(request: TaskPlanRequest, steps: tuple[TaskPlanStep, ...]) -> None:
    actions = [step.action for step in steps]
    evidence = tuple(step for step in steps if step.action.split(":")[0] in _EVIDENCE_CAPABILITIES)
    prior_evidence = tuple(step for step in request.prior_plan.steps if step.action.split(":")[0] in _EVIDENCE_CAPABILITIES) if request.prior_plan else ()
    if evidence and evidence != prior_evidence:
        raise ValueError("Evidence follow-up requires recorded reading feedback; it cannot be preplanned.")
    if any(action.startswith(("refine_implementation:", "prepare_implementation:")) for action in actions):
        raise ValueError("Implementation refinement requires recorded executor feedback; it cannot be preplanned.")
    errors = _validate_authorized_boundaries(request, steps)
    if implementation_only(request.execution, request.requested_outputs):
        # A code change consumes evidence when supplied/authorized; it does
        # not require a scientific idea assessment or a measurement design.
        allowed = {"prepare_execution", "implement"}
        if request.config.get("research_local_documents") or request.config.get("research_materials_only") is False:
            allowed.update(("document_ingest", "read"))
        if request.config.get("research_materials_only") is False:
            allowed.add("search")
        required = {"implement"}
        if _needs_preparation(request.execution):
            required.add("prepare_execution")
        if set(request.requested_outputs) & {"report", "paper", "full_paper"}:
            allowed.update(("report_write", "report", "report_audit"))
            required.update(("report_write", "report", "report_audit"))
        base = {action for action in actions if action.split(":")[0] not in _EVIDENCE_CAPABILITIES}
        if not required <= base or base - allowed:
            errors.append("Code delivery must preserve its authorized capabilities and requested outputs.")
        if "search" in actions and not {"document_ingest", "read"} <= base:
            errors.append("Searched sources must be ingested and read before code uses them.")
        for index, step in enumerate(steps):
            if step.action.split(":")[0] in _EVIDENCE_CAPABILITIES:
                continue
            if step.condition:
                errors.append("Requested code delivery steps cannot be conditional.")
            for dependency in _prerequisites(request, step.action, actions=set(actions)):
                if not any(item.action == dependency for item in steps[:index]):
                    errors.append(f"Task plan action {step.action!r} is missing prerequisite {dependency!r}.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    if request.task_kind == "data_analysis":
        if actions != [row["action"] for row in default_task_steps(request)] or any(step.condition for step in steps):
            errors.append("Data analysis freezes and analyzes supplied data, then optionally writes from that package; no research or experiment prerequisites.")
        if request.execution or set(request.requested_outputs) not in ({"data_analysis"}, {"data_analysis", "report"}):
            errors.append("Data analysis requests its descriptive package and optionally a report, without process execution.")
        if request.config.get("research_local_documents") and "report" not in request.requested_outputs:
            errors.append("Data documentation and references are consumed by the optional report, not descriptive arithmetic.")
        from simple_ar.result_analysis.table import TableSpec
        table = request.config.get("data_analysis") or {}
        TableSpec.from_config(table)
        if not table.get("file"):
            errors.append("Data analysis requires a supplied data file.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    if request.task_kind == "writing":
        if actions != [row["action"] for row in default_task_steps(request)] or any(step.condition for step in steps):
            errors.append("Writing consumes supplied material then writes, assembles and audits; it does not require research discovery or synthesis.")
        if not request.config.get("research_local_documents") or not _provided_materials_only(request):
            errors.append("Writing requires supplied local materials and materials-only scope.")
        if request.execution or set(request.requested_outputs) != {"report"}:
            errors.append("Writing requests only a report, without execution configuration.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    if request.task_kind == "bug_fix":
        if any(step.capability not in {"prepare_execution", "implement"} for step in steps) or "implement" not in actions:
            errors.append("Bug-fix plans may contain only preparation and implementation.")
        if any(action not in {"prepare_execution", "implement"} for action in actions):
            errors.append("Bug-fix plans may use only the preparation and implementation actions.")
        if (
            "prepare_execution" in actions
            and "implement" in actions
            and actions.index("implement") < actions.index("prepare_execution")
        ):
            errors.append("Bug-fix implementation must follow preparation.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    if request.task_kind == "measurement":
        if actions != [row["action"] for row in default_task_steps(request)]:
            errors.append("Direct measurement requires exactly experiment then analysis; it does not perform research discovery or design.")
        if not isinstance(request.execution, Mapping) or not request.execution.get("command"):
            errors.append("Direct measurement requires an explicit execution command.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    if request.task_kind == "reproduction":
        expected = [row["action"] for row in default_task_steps(request)]
        execution = request.execution or {}
        protocol = execution.get("protocol")
        if actions != expected:
            errors.append("Prepared reproduction must follow its supplied evidence, optional authorized adaptation, fixed measurement and analysis route.")
        if not request.config.get("research_local_documents") or not _provided_materials_only(request):
            errors.append("Prepared reproduction requires supplied documents and materials-only scope.")
        if not execution.get("command") or execution.get("pairs") or execution.get("baseline_policy") in {"run", "reuse"}:
            errors.append("Prepared reproduction requires one explicit formal command without paired baseline runs.")
        task = execution.get("code_task")
        if task is not None:
            if not isinstance(task, Mapping):
                errors.append("Reproduction code_task must be an explicit authorized mapping.")
            else:
                from simple_ar.app.research_execution import code_task_validation
                try:
                    code_task_validation(execution, required=True)
                except ValueError as exc:
                    errors.append(str(exc))
                if not str(task.get("approval_note") or "").strip():
                    errors.append("Reproduction CodeTask requires explicit isolated-edit approval.")
        if not isinstance(protocol, Mapping) or any(not str(protocol.get(key) or "").strip()
                                                   for key in ("hypothesis", "dataset", "expected_outcome")):
            errors.append("Prepared reproduction requires execution.protocol hypothesis, dataset and expected_outcome describing the exact reproduction scope and comparison criterion.")
        if not set(request.requested_outputs) <= {"experiments", "report"} or "experiments" not in request.requested_outputs:
            errors.append("Prepared reproduction outputs must include experiments and optionally report.")
        if errors:
            raise ValueError("\n".join(errors))
        return
    provided_only = "search" not in actions
    if provided_only and not request.config.get("research_local_documents"):
        errors.append("Omitting search requires supplied local documents.")
    required = {"document_ingest"}
    if not report_from_documents(request):
        required.update(("read", "synthesize"))
    if not provided_only:
        required.update(("search", "read"))
    if not required <= set(actions):
        boundary = "provided-materials" if provided_only else "search-to-evidence"
        errors.append(f"Research plans must preserve the {boundary} boundary.")
    requested = {str(item).strip().lower() for item in request.requested_outputs}
    if (not requested or requested & {"summarize", "summary", "research_summary"}) and "summarize" not in actions:
        errors.append("The requested research summary is missing from the accepted plan.")
    for index, step in enumerate(steps):
        for dependency in _prerequisites(request, step.action, actions=set(actions)):
            if not any(_action_matches(item.action, dependency) for item in steps[:index]):
                errors.append(f"Task plan action {step.action!r} is missing prerequisite {dependency!r}.")
    if errors:
        raise ValueError("\n".join(errors))
    if requested & {"experiment", "experiments", "code", "code_task"} and not request.execution_protocol_accepted and "research_design" in actions:
        if actions[-1] != "research_design" or any(
            step.action in {"report_write", "report", "report_audit"} for step in steps
        ):
            raise ValueError("Pre-design research plans must stop at research_design; execution and delivery are deferred.")
        return
    if "research_design" in actions and "report_write" in actions and actions.index("report_write") < actions.index("research_design"):
        raise ValueError("Report writing must follow the requested research design.")
    if requested & {"experiment", "experiments"} and not ("experiment" in actions or any(action.startswith("matrix_candidate") for action in actions)):
        raise ValueError("The requested experiment is missing from the accepted plan.")
    if requested & {"report", "paper", "full_paper"} and "report_audit" not in actions:
        raise ValueError("The requested report is missing its assembly/audit steps.")
    if requested & {"report", "paper", "full_paper"} and any(
        step.action in {"report_write", "report", "report_audit"} and step.condition
        for step in steps
    ):
        raise ValueError("Requested report steps cannot be conditional.")


def _provided_materials_only(request: TaskPlanRequest) -> bool:
    """Return whether the caller explicitly selected supplied-materials mode."""

    return bool(request.config.get("research_materials_only"))


def _validate_authorized_boundaries(
    request: TaskPlanRequest, steps: tuple[TaskPlanStep, ...],
) -> list[str]:
    """Return all boundary errors from one proposal; defaults are not authority."""
    boundary = _planning_boundary(request)
    permitted = {
        row["action"]: row.get("condition", "")
        for row in boundary["allowed_process_steps"]
    }
    deferred = {
        row["action"]: row.get("condition", "")
        for row in boundary["deferred_process_steps"]
    }
    errors: list[str] = []
    for step in steps:
        if step.capability in _PROCESS_CAPABILITIES:
            if step.action not in permitted:
                if step.action in deferred:
                    category = "not_ready"
                    reason = "The supplied execution boundary authorizes this process only after the research design protocol is accepted."
                    expected = f"deferred condition={deferred[step.action] or 'none'}"
                    unlock = "research_design must produce the accepted execution protocol"
                else:
                    category = "unauthorized"
                    reason = boundary["unauthorized_process_reason"]
                    expected = "an action from the current execution boundary"
                    unlock = "provide an explicit supported execution boundary or use the task's existing non-process route"
                errors.append(
                    "Task-plan boundary error (authorized protocol): "
                    f"action={step.action!r}; category={category}; reason={reason}; "
                    f"expected={expected}; unlock={unlock}."
                )
            elif step.condition != permitted[step.action]:
                errors.append(
                    "Task-plan boundary error (authorized protocol): "
                    f"action={step.action!r}; category=condition_mismatch; "
                    "reason=the model cannot change the executor's protocol condition; "
                    f"expected condition={permitted[step.action] or 'none'}; "
                    f"received condition={step.condition or 'none'}."
                )
        if step.action in {"data_ingest", "data_analysis"} and request.task_kind != "data_analysis":
            errors.append("Data actions require an explicit data_analysis task and its supplied table settings.")
        if _provided_materials_only(request) and step.action == "search":
            errors.append("Provided-materials plans cannot authorize search.")
        if step.capability not in _PROCESS_CAPABILITIES and step.condition:
            if step.action not in {"report_write", "report", "report_audit"} or step.condition != "on_request:report":
                errors.append(f"Unsupported delivery condition for {step.action}.")
    return errors


def _action_matches(action: str, expected: str) -> bool:
    return action == expected or (expected == "experiment" and (action.startswith(("retest:", "matrix_candidate", "matrix_baseline_")))) or (expected == "matrix_candidate" and action.startswith("matrix_candidate"))


def _is_known_action(action: str) -> bool:
    return action in _ACTION_CAPABILITIES or bool(_ACTION_RE.match(action))


def _capability(action: str) -> str:
    if action in _ACTION_CAPABILITIES:
        return _ACTION_CAPABILITIES[action]
    if action.split(":")[0] in _EVIDENCE_CAPABILITIES:
        return _EVIDENCE_CAPABILITIES[action.split(":")[0]]
    if action.startswith(("repair:", "matrix_repair_")):
        return "implement"
    if action.startswith(("retest:", "matrix_baseline_", "matrix_candidate")):
        return "experiment"
    if action.startswith(("supplement_baseline:", "supplement_candidate:")):
        return "experiment"
    if action.startswith("prepare_candidate:"):
        return "prepare_execution"
    if action.startswith("prepare_implementation:"):
        return "prepare_execution"
    if action.startswith("refine_implementation:"):
        return "research_design"
    if action.startswith("research_design_revision:"):
        return "research_design"
    if action.startswith("revise_candidate:"):
        return "implement"
    if action.startswith("repair_candidate:"):
        return "implement"
    if action.startswith("research_candidate:"):
        return "experiment"
    if action.startswith("retest_candidate:"):
        return "experiment"
    if action.startswith("reanalysis:"):
        return "analysis"
    raise ValueError(f"Unsupported task plan action: {action!r}")


def _state_name(action: str) -> str:
    if action.split(":")[0] in _EVIDENCE_CAPABILITIES:
        prefix, number = action.split(":")
        return f"{'documents_evidence' if prefix == 'ingest_evidence' else prefix}_{number}"
    aliases = {
        "document_ingest": "documents",
        "synthesize": "synthesis",
        "summarize": "summary",
        "assess_ideas": "assessment",
        "research_design": "design",
        "prepare_execution": "preparation",
        "implement": "implementation",
        "report_write": "writer",
        "data_ingest": "data_input",
        "matrix_analysis": "analysis",
    }
    if action in aliases:
        return aliases[action]
    if action.startswith("repair:"):
        return f"repair_{action.split(':', 1)[1]}"
    if action.startswith("retest:"):
        return f"experiment_repair_{action.split(':', 1)[1]}"
    if action.startswith("supplement_baseline:"):
        return f"baseline_supplement_{action.split(':', 1)[1]}"
    if action.startswith("supplement_candidate:"):
        return f"experiment_supplement_{action.split(':', 1)[1]}"
    if action.startswith("research_design_revision:"):
        return f"design_revision_{action.split(':', 1)[1]}"
    if action.startswith("prepare_candidate:"):
        return f"preparation_r{action.split(':', 1)[1]}"
    if action.startswith("revise_candidate:"):
        return f"implementation_r{action.split(':', 1)[1]}"
    if action.startswith("repair_candidate:"):
        iteration, repair_index = action.split(":", 1)[1].split("_", 1)
        return f"implementation_r{iteration}_repair_{repair_index}"
    if action.startswith("research_candidate:"):
        suffix = action.split(":", 1)[1]
        return f"experiment_revision_{suffix}"
    if action.startswith("retest_candidate:"):
        iteration, repair_index = action.split(":", 1)[1].split("_", 1)
        return f"experiment_revision_{iteration}_repair_{repair_index}"
    if action.startswith("reanalysis:"):
        return f"analysis_r{action.split(':', 1)[1]}"
    return action


def _valid_condition(condition: str) -> bool:
    return condition.startswith(("on_failure:", "on_failure_prefix:", "after_success:", "after_observation:", "on_request:", "on_decision:")) and bool(condition.split(":", 1)[1].strip())


def _needs_preparation(execution: Mapping[str, object]) -> bool:
    if "environment" in execution:
        return True
    if "dataset" in execution:
        return True
    task = execution.get("code_task")
    return isinstance(task, Mapping) and "code_root" in task


def _repair_limit(execution: Mapping[str, object]) -> int:
    task = execution.get("code_task")
    value = task.get("max_repairs", 0) if isinstance(task, Mapping) else 0
    return value if type(value) is int and value >= 0 else 0


def _matrix_candidate_key(revision: int, index: int) -> str:
    return f"matrix_candidate_r{revision}_{index}" if revision else f"matrix_candidate_{index}"


def _planning_output_tokens(config: Mapping[str, object]) -> int | None:
    if config.get("research_task_planning_max_output_tokens") is None:
        return None
    try:
        value = int(config.get("research_task_planning_max_output_tokens", 1200))
    except (TypeError, ValueError):
        return 1200
    return value if value > 0 else 1200


def _llm_prompt(request: TaskPlanRequest, defaults: list[dict[str, Any]]) -> str:
    planning_boundary = _planning_boundary(request)
    # Process authority and availability of evidence/report work are different
    # axes. A survey has no process budget, but still has executable capabilities.
    non_process = [
        row["action"] for row in defaults
        if row["capability"] not in _PROCESS_CAPABILITIES
    ]
    boundary = {
        "task_kind": request.task_kind,
        "goal": request.goal,
        "request": request.request_text,
        "objective": request.objective,
        "hard_constraints": list(request.hard_constraints),
        "preferences": list(request.preferences),
        "requested_outputs": list(request.requested_outputs),
        "intents": list(request.intents),
        "assets": [dict(asset) for asset in request.assets],
        "local_documents": request.config.get("research_local_documents", []),
        "execution_boundary": {
            "configured": request.execution is not None,
            "has_code_task": isinstance((request.execution or {}).get("code_task"), Mapping),
            "has_pairs": bool((request.execution or {}).get("pairs")),
            "has_dataset": "dataset" in (request.execution or {}),
            "repair_limit": _repair_limit(request.execution or {}),
        },
        "planning_boundary": planning_boundary,
        "available_non_process_actions": non_process,
        "required_output_actions": list(_required_output_actions(request)),
        "stop_after_action": (
            "research_design" if defaults and defaults[-1]["action"] == "research_design" else None
        ),
        "material_boundary": {
            "provided_materials_only": _provided_materials_only(request),
            "search_allowed": not _provided_materials_only(request),
            "report_from_original_documents": report_from_documents(request),
            "supplied_asset_count": sum(
                1 for asset in request.assets
                if str(asset.get("role") or "").strip().lower() in {"paper", "document", "reference"}
            ),
        },
        "suggested_steps": [
            {key: value for key, value in row.items() if key not in {"capability", "state_name"}}
            for row in defaults
        ],
    }
    return (
        "Interpret this task into one short sequential accepted plan. The returned JSON must have "
        "a `steps` array. The suggested steps are context only, not authorization: choose the "
        "necessary sequential stages for the task, assets, and constraints. Non-process work "
        "is listed in available_non_process_actions; an empty allowed_process_steps list forbids "
        "processes, not literature reading, synthesis or requested report delivery. Process "
        "actions must be listed as allowed in planning_boundary. Deferred process actions are "
        "not executable in this proposal. Only a non-null stop_after_action is an actual "
        "planning checkpoint: end with that action and let the application continue later. "
        "Otherwise include required_output_actions and their inputs. execution_phase is descriptive, "
        "not a step, condition or instruction to stop. Return action, step_id, problem_solved, observation, and "
        "the supplied condition if any. Do not generate capability or state_name: the executor derives them. "
        "The boundary is authoritative even when a suggested step or task wording seems to imply more. "
        "`prepare_execution` creates an isolated workspace, `implement` only locates/patches/validates "
        "authorized code, `experiment` measures a configured condition, `analysis` interprets completed "
        "measurements, and none of these actions is a substitute for research design or report writing. "
        "For measurement, use experiment then analysis, preceded by prepare_execution only when execution.environment is explicitly configured; the supplied command is the accepted measurement protocol, not a research candidate. "
        "For reproduction, preserve the supplied fixed protocol without research_design or assess_ideas. Only explicit code_task authorizes scoped result/entry adaptation and independent validation before measurement; never change method or evaluation conditions. Explicit environment preparation or a declared short check requires prepare_execution before measurement; do not omit it or invent installation/check commands otherwise. "
        "For bug_fix, use only prepare_execution (when required) and implement; implementation "
        "already includes validation and the repair explanation, so do not append summary or report steps. "
        "Do not invent dynamic indices, repair rounds, capabilities, processes, or parallel work. "
        "For provided_materials_only, begin with document_ingest over the supplied assets and do "
        "not add search. With supplied local documents you may omit search when the task calls "
        "for analysing those materials. Without supplied local documents, search is required before "
        "document_ingest, read, and synthesize. research_design requires assess_ideas after synthesis. "
        "When report_from_original_documents is true, report_write can use retained original "
        "text without a separate synthesis. A search-based investigation still requires search, "
        "document_ingest, and read before report_write; only a supplied-material report may "
        "consume document_ingest directly. Add synthesis when the user needs a separate brief "
        "or subsequent research decisions, not merely to repeat the report's argument planning. "
        "For an experiment before protocol acceptance, stop at research_design; do not include "
        "execution or delivery yet. Missing required prerequisites are recorded and compiled by "
        "the application, but unauthorized actions and reversed explicit order are rejected. "
        "Inputs are bound by capability adapters; do not invent "
        "input fields. Completion is owned by the application; do not append stop, finish, "
        "or request_input steps. Do not use `plan` or `task_plan` as a step: this planning attempt is "
        "already producing the accepted plan.\n\n"
        + json.dumps(boundary, ensure_ascii=False, indent=2, default=str)
    )


def _strings(value: object) -> tuple[str, ...]:
    return tuple(str(item).strip() for item in value if str(item).strip()) if isinstance(value, list) else ()


__all__ = ["SCHEMA_VERSION", "TaskPlanRequest", "TaskPlanResult", "TaskPlanStep", "append_research_followup", "insert_evidence_followup", "build_task_plan", "default_task_steps"]
