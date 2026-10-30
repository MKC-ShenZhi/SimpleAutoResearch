from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Mapping, Protocol

from simple_ar.integrations.llm import LLMError, parse_json_object

from .metrics import build_metric_summary
from .schema import (
    AnalysisAudit,
    AnalysisClaim,
    AnalysisContext,
    AnalysisRecommendation,
    AnalysisResult,
    AnalysisStatus,
    GoalAssessment,
)


class JsonLLMClient(Protocol):
    def ask(self, system: str, user: str, *, label: str = "") -> str:
        ...

    def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, Any]:
        ...


SYSTEM_PROMPT = """You are a rigorous benchmark and experiment result analyst.
Use only the provided task, criteria, metrics, artifacts, and writeup.
Do not invent metrics, datasets, judge outcomes, or unsupported claims.
Every supported or partially_supported claim must cite metric_refs or concrete evidence.
Evaluate the stated hypothesis, not a vote across favorable metrics. An ancillary
improvement does not support a contradicted primary objective or non-regression condition.
Mean changes and sample standard deviations alone do not establish significance
or exclude seed variance as an explanation.
If evidence is missing, mark the claim as not_evaluated or unsupported.
Prefer concise, reviewable evidence over promotional language."""


def run_result_analysis(
    context: AnalysisContext | dict[str, Any],
    *,
    output_dir: Path | None = None,
    client: JsonLLMClient | None = None,
    use_llm: bool = False,
    label: str = "result-analysis",
) -> AnalysisResult:
    ctx = context if isinstance(context, AnalysisContext) else AnalysisContext.model_validate(context)
    metric_summary = build_metric_summary(ctx)
    metric_summary["result_tables"] = build_result_tables(ctx)
    metric_summary["rubric_categories"] = build_rubric_categories(ctx)
    metric_summary["weak_metric_signals"] = sorted(
        dict.fromkeys(
            list(metric_summary.get("weak_metric_signals") or [])
            + evidence_chain_warnings(ctx, metric_summary["result_tables"])
        )
    )
    result = deterministic_result(ctx, metric_summary)
    raw_response: dict[str, Any] | None = None

    if use_llm:
        if client is None:
            raise LLMError(
                "LLM analysis was requested but no client was provided; "
                "refusing deterministic fallback."
            )
        raw_response = request_json_with_diagnostics(
            client,
            SYSTEM_PROMPT,
            build_prompt(ctx, metric_summary, result),
            label=label,
            output_dir=output_dir,
        )
        error = _recommendation_error(raw_response, ctx) or _goal_assessment_error(
            raw_response, ctx, metric_summary,
        )
        if error:
            raw_response = request_json_with_diagnostics(
                client, SYSTEM_PROMPT,
                build_prompt(ctx, metric_summary, result)
                + "\n\nCorrect the rejected recommendation, preserving the measured evidence. "
                + error + "\nPrevious response: " + json.dumps(raw_response, ensure_ascii=False),
                label=label + "-recommendation-repair", output_dir=output_dir,
            )
            error = _recommendation_error(raw_response, ctx)
            if error:
                raise LLMError("Invalid research recommendation after one correction: " + error)
        result = normalize_llm_result(raw_response, ctx, metric_summary, fallback=result)

    result.raw_llm_response = raw_response
    result.status, result.status_reasons = derive_analysis_status(ctx, metric_summary)
    if output_dir is not None:
        write_analysis_artifacts(output_dir, ctx, result)
    return result


def _recommendation_error(response: Mapping[str, Any], context: AnalysisContext) -> str:
    recommendation = response.get("recommendation")
    if context.metadata.get("research_goal"):
        if not isinstance(recommendation, Mapping):
            return "Provide a structured research recommendation or explicitly request_input; do not infer stop from a missing recommendation."
        action = str(recommendation.get("action") or "").strip().lower().replace("-", "_")
        action = {"revise": "revise_candidate", "revision": "revise_candidate"}.get(action, action)
        disposition = str(recommendation.get("task_disposition") or "").strip().lower()
        if action not in {"supplement", "revise_candidate", "stop", "request_input"}:
            return "Choose a supported recommendation action: supplement, revise_candidate, stop, or request_input."
        if action in {"supplement", "revise_candidate"} and disposition != "continue":
            return "A bounded research follow-up must declare task_disposition=continue."
        if action == "revise_candidate":
            purpose = str(recommendation.get("revision_purpose") or "").strip().lower()
            if purpose == "verification_only":
                return (
                    "Verification-only instrumentation is not a scientific candidate revision and "
                    "cannot justify another identical training run. Use existing evidence, choose a "
                    "distinct method change if supported, or stop/request_input with the limitation."
                )
            if purpose != "method_change":
                return (
                    "Classify the intended candidate revision as revision_purpose=method_change "
                    "only when the tested behavior or hypothesis changes; otherwise do not use "
                    "revise_candidate."
                )
        if action == "request_input" and disposition != "undecided":
            return "A request for missing input must declare task_disposition=undecided."
        if action == "stop":
            if disposition != "stop" or not str(recommendation.get("reason") or "").strip():
                return "A task-level stop needs task_disposition=stop and a concrete reason; rejecting one candidate is not enough."
            if (context.metadata.get("remaining_authorized_rounds", 0) > 0
                    and not normalize_string_list(recommendation.get("alternatives_considered"))
                    and not normalize_string_list(recommendation.get("continuation_conditions"))):
                return "With research rounds remaining, explain the alternatives considered or the conditions needed to continue before stopping the task."
    if not isinstance(recommendation, Mapping) or recommendation.get("action") != "supplement":
        return ""
    protocol = context.metadata.get("execution_protocol", {})
    if protocol.get("can_extend_seed_condition") is False:
        return "This fixed-command task cannot extend seeds. Choose a supported revision, stop, or request_input; do not invent a seed flag."
    supplement = recommendation.get("supplement")
    if not isinstance(supplement, Mapping):
        return "supplement must be a JSON object with one or more explicit integer seeds and an evidence gap, not prose or an empty object."
    seed = supplement.get("seed")
    seeds = supplement.get("seeds")
    valid_single = type(seed) is int and seeds is None
    valid_many = (
        isinstance(seeds, list)
        and bool(seeds)
        and all(type(value) is int for value in seeds)
        and len(set(seeds)) == len(seeds)
        and seed is None
    )
    if not (valid_single or valid_many):
        return "supplement must provide either one integer seed or a non-empty unique integer seeds list, plus an evidence gap."
    return ""


def _goal_assessment_error(
    response: Mapping[str, Any], context: AnalysisContext, metric_summary: Mapping[str, Any],
) -> str:
    """Offer one bounded correction for unresolvable goal evidence, not a new verdict."""
    if not context.metadata.get("research_goal"):
        return ""
    assessment = response.get("goal_assessment")
    if not isinstance(assessment, Mapping):
        return ""
    refs = normalize_string_list(assessment.get("evidence_refs"))
    known = _known_goal_evidence(context, metric_summary)
    invalid = [ref for ref in refs if ref not in known]
    if not invalid and (refs or assessment.get("status") == "inconclusive"):
        return ""
    # A missing or malformed reference must not turn an inconclusive outcome
    # into a failure or let the model invent evidence. The ordinary parser
    # remains the conservative fallback if this one correction does not help.
    # Lead with the compact facts the model can actually cite. Detailed
    # per-condition IDs can be numerous and would otherwise crowd out every
    # declared metric from a lexically sorted sample.
    examples = list(sorted(set(context.metrics) | set(context.artifacts.values())))[:16]
    examples.extend([ref for ref in sorted(known) if ref not in examples][:8])
    return (
        "Goal assessment evidence_refs must resolve to observed metric names, "
        "result-table evidence_id values, or supplied artifact paths. "
        f"Invalid refs: {invalid!r}. Available examples: {examples!r}. "
        "Preserve the measured verdict and recommendation; if no relevant "
        "evidence exists, use status=inconclusive instead of inventing a ref."
    )


def request_json_with_diagnostics(
    client: JsonLLMClient,
    system: str,
    user: str,
    *,
    label: str,
    output_dir: Path | None,
) -> dict[str, Any]:
    raw = client.ask(system, user + "\n\nReturn valid JSON only. Do not include markdown or extra text.", label=label)
    parsed = parse_json_object(raw)
    if parsed is None:
        hint = ""
        if output_dir is not None:
            output_dir.mkdir(parents=True, exist_ok=True)
            write_text(output_dir / "analysis_prompt.txt", user)
            write_text(output_dir / "analysis_raw_response.txt", raw)
            hint = f" Raw response saved to {output_dir / 'analysis_raw_response.txt'}."
        raise ValueError("LLM result-analysis response did not contain a valid JSON object." + hint)
    return parsed


def deterministic_result(context: AnalysisContext, metric_summary: dict[str, Any]) -> AnalysisResult:
    claims = existing_claims(context)
    if not claims:
        claims = hypothesis_placeholder_claims(context, metric_summary)
    rubric_coverage = deterministic_rubric_coverage(context, metric_summary)
    audit = AnalysisAudit(
        llm_used=False,
        missing_required_metrics=list(metric_summary.get("missing_required_metrics") or []),
        weak_metric_signals=list(metric_summary.get("weak_metric_signals") or []),
        limitations=deterministic_limitations(metric_summary),
        notes=["deterministic result-analysis fallback"],
    )
    result = AnalysisResult(
        readme_markdown="",
        claims=claims,
        metric_summary=metric_summary,
        rubric_coverage=rubric_coverage,
        audit=audit,
        recommendation=deterministic_recommendation(context),
        decision_context=dict(context.metadata),
    )
    return _finalize_result(result, context, metric_summary)


def deterministic_recommendation(context: AnalysisContext) -> AnalysisRecommendation:
    """Keep offline analysis conservative until a scientific proposal exists."""

    refs = context.metadata.get("evidence_refs", [])
    evidence_refs = normalize_string_list(refs)[:12]
    return AnalysisRecommendation(
        action="request_input",
        task_disposition="undecided",
        reason=(
            "Deterministic result analysis does not choose a scientific follow-up; "
            "a model or explicit user decision must propose the next bounded action."
        ),
        evidence_refs=evidence_refs,
    )


def existing_claims(context: AnalysisContext) -> list[AnalysisClaim]:
    source = context.project_results
    rows: Any = None
    for key in ("claims", "hypothesis_verdicts", "hypotheses"):
        value = source.get(key) if isinstance(source, dict) else None
        if isinstance(value, list) and value:
            rows = value
            break
        if isinstance(value, dict) and value:
            rows = [
                {"claim_id": claim_id, "hypothesis_id": claim_id, **row}
                if isinstance(row, dict)
                else {"claim_id": claim_id, "hypothesis_id": claim_id, "claim": str(row)}
                for claim_id, row in value.items()
            ]
            break
    if not isinstance(rows, list):
        return contract_claims(context)
    statements = hypothesis_statement_map(context.hypotheses)
    claims: list[AnalysisClaim] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        claim_id = str(row.get("claim_id") or row.get("hypothesis_id") or row.get("hypothesis") or f"claim-{index}")
        claim_text = str(
            row.get("claim")
            or row.get("statement")
            or row.get("summary")
            or statements.get(claim_id)
            or row.get("evidence")
            or ""
        ).strip()
        if not claim_text:
            continue
        verdict = row_verdict(row)
        claims.append(
            AnalysisClaim(
                claim_id=claim_id,
                claim=claim_text,
                verdict=verdict,
                evidence=normalize_evidence(row.get("evidence")),
                metric_refs=normalize_metric_refs(row.get("metric_refs")),
                limitations=normalize_string_list(row.get("limitations")),
                confidence=normalize_confidence(row.get("confidence")),
            )
        )
    return claims


def contract_claims(context: AnalysisContext) -> list[AnalysisClaim]:
    contract = context.task_contract if isinstance(context.task_contract, dict) else {}
    rows = contract.get("claim_specs")
    claims: list[AnalysisClaim] = []
    if isinstance(rows, list):
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            statement = str(row.get("statement") or row.get("claim") or row.get("description") or "").strip()
            if not statement:
                continue
            claims.append(
                AnalysisClaim(
                    claim_id=str(row.get("claim_id") or row.get("id") or f"claim-{index}"),
                    claim=statement,
                    verdict="not_evaluated",
                    evidence=[],
                    metric_refs=normalize_metric_refs(row.get("metric_refs") or row.get("required_metrics")),
                    limitations=["Claim was derived from task_contract and requires measured evidence."],
                    confidence="low",
                )
            )
    if claims:
        return claims
    evidence_plan = contract.get("evidence_plan") if isinstance(contract.get("evidence_plan"), dict) else {}
    hypotheses = evidence_plan.get("hypotheses") if isinstance(evidence_plan, dict) else []
    for index, item in enumerate(hypotheses if isinstance(hypotheses, list) else [], start=1):
        statement = str(item).strip()
        if not statement:
            continue
        claims.append(
            AnalysisClaim(
                claim_id=f"contract-hypothesis-{index}",
                claim=statement,
                verdict="not_evaluated",
                evidence=[],
                metric_refs=[],
                limitations=["Hypothesis was derived from task_contract.evidence_plan and requires measured evidence."],
                confidence="low",
            )
        )
    return claims


def hypothesis_statement_map(hypotheses: list[dict[str, Any]]) -> dict[str, str]:
    statements: dict[str, str] = {}
    for index, row in enumerate(hypotheses, start=1):
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or row.get("hypothesis_id") or f"hypothesis-{index}")
        text = str(row.get("statement") or row.get("claim") or "").strip()
        if key and text:
            statements[key] = text
    return statements


def row_verdict(row: dict[str, Any]) -> str:
    if isinstance(row.get("supported"), bool):
        return "supported" if row["supported"] else "unsupported"
    return normalize_verdict(row.get("verdict") or row.get("status"))


def hypothesis_placeholder_claims(context: AnalysisContext, metric_summary: dict[str, Any]) -> list[AnalysisClaim]:
    hypotheses = context.hypotheses or []
    if not hypotheses and isinstance(context.task_contract, dict):
        evidence_plan = context.task_contract.get("evidence_plan")
        if isinstance(evidence_plan, dict) and isinstance(evidence_plan.get("hypotheses"), list):
            hypotheses = [
                {"id": f"contract-hypothesis-{index}", "statement": str(item)}
                for index, item in enumerate(evidence_plan["hypotheses"], start=1)
                if str(item).strip()
            ]
    if not hypotheses:
        return [
            AnalysisClaim(
                claim_id="claim-1",
                claim="The run produced numeric metrics, but no explicit hypothesis-level claim was found.",
                verdict="partially_supported" if metric_summary.get("metric_count") else "not_evaluated",
                evidence=[],
                metric_refs=available_metric_names(metric_summary),
                limitations=["No structured hypothesis verdict was available in the run artifacts."],
                confidence="low",
            )
        ]
    claims: list[AnalysisClaim] = []
    paired_summaries = _paired_summary_rows(context.project_results)
    for index, row in enumerate(hypotheses, start=1):
        statement = str(row.get("statement") or row.get("claim") or row).strip()
        evidence = normalize_evidence(row.get("evidence") or row.get("evidence_refs"))
        metric_refs = normalize_metric_refs(row.get("metric_refs") or row.get("metrics"))
        verdict = "not_evaluated"
        limitations = ["No grounded verdict was found in the run artifacts."]
        if paired_summaries:
            evidence, limitations = _paired_hypothesis_evidence(
                metric_refs, evidence, paired_summaries, metric_summary
            )
        claims.append(
            AnalysisClaim(
                claim_id=str(row.get("id") or f"hypothesis-{index}"),
                claim=statement,
                verdict=verdict,
                evidence=evidence,
                metric_refs=metric_refs,
                limitations=limitations,
                confidence="low",
            )
        )
    return claims


def _paired_summary_rows(project_results: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the canonical paired summaries available to result analysis."""
    candidates: list[Any] = [project_results.get("paired_summary")]
    execution = project_results.get("execution_result")
    if isinstance(execution, Mapping):
        candidates.append(execution.get("paired_summary"))
    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]
    return []


def _paired_hypothesis_evidence(
    metric_refs: list[str],
    evidence: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    metric_summary: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Describe paired changes without inferring what a hypothesis asserts.

    A metric's preferred direction is not the hypothesis's predicted direction,
    conditions, or strength of evidence. Keep the measured changes available
    to a later evaluation rather than assigning a hypothesis verdict from them.
    """
    directions = {
        str(row.get("name")): str(row.get("direction") or "unknown")
        for row in metric_summary.get("metrics", []) or []
        if isinstance(row, Mapping) and row.get("name")
    }
    found = False
    unavailable: list[str] = []
    added_evidence = list(evidence)
    for reference in metric_refs:
        names = [reference]
        if ":" in reference:
            names.append(reference.split(":", 1)[0])
        matches = [
            row for name in names for row in summaries
            if str(row.get("metric") or "").strip() == name
        ]
        if len(matches) != 1:
            unavailable.append(reference)
            continue
        row = matches[0]
        metric = str(row.get("metric") or reference).strip()
        direction = directions.get(metric, "unknown")
        delta = row.get("delta_mean")
        if (direction not in {"higher", "lower"}
                or isinstance(delta, bool)
                or not isinstance(delta, (int, float))
                or not math.isfinite(float(delta))
                or type(row.get("n")) is not int or row["n"] < 1):
            unavailable.append(reference)
            continue
        delta_value = float(delta)
        favorable = (delta_value > 0) if direction == "higher" else (delta_value < 0)
        found = True
        added_evidence.append({
            "source": f"paired_summary:{metric}",
            "metric": metric,
            "baseline_mean": row.get("baseline_mean"),
            "candidate_mean": row.get("candidate_mean"),
            "delta": delta_value,
            "direction": direction,
            "n": row.get("n"),
            "favorable": favorable,
        })

    if not found:
        return added_evidence, [
            "No unique paired measurement with a known direction was available for the declared metrics."
        ]
    limitations = [
        "Directional comparison is descriptive; no significance test or population uncertainty was established.",
        "Metric preferences and observed paired means do not determine whether the stated hypothesis "
        "or its conditions are supported; evaluate the claim separately.",
    ]
    if unavailable:
        limitations.append(
            "Some declared metrics were unavailable, ambiguous, or had no usable direction: "
            + ", ".join(unavailable) + "."
        )
    return added_evidence, limitations


def deterministic_markdown(
    context: AnalysisContext,
    metric_summary: dict[str, Any],
    claims: list[AnalysisClaim],
    rubric_coverage: list[dict[str, Any]],
    audit: AnalysisAudit,
) -> str:
    lines = [
        f"# Result Analysis: {context.task_id or context.title or 'experiment'}",
        "",
        "## Task",
        "",
        context.research_question or context.title or "(not provided)",
        "",
        "## Rubric Coverage",
        "",
        "| Category | Leaves | Status | Evidence |",
        "| --- | ---: | --- | --- |",
    ]
    for row in rubric_coverage:
        lines.append(
            f"| {row.get('category', 'Uncategorized')} | {row.get('leaf_count', 0)} | "
            f"{row.get('verdict', 'not_evaluated')} | {escape_table_text(row.get('evidence', ''))} |"
        )
    lines.extend(
        [
            "",
        "## Metrics",
        "",
        "| Metric | Value | Direction | Issues |",
        "| --- | ---: | --- | --- |",
        ]
    )
    for metric in metric_summary.get("metrics", []):
        lines.append(
            f"| `{metric.get('name')}` | {format_metric_value(metric.get('value'))} | "
            f"{metric.get('direction')} | {', '.join(metric.get('issues') or []) or '-'} |"
        )
    result_tables = metric_summary.get("result_tables") or {}
    primary_rows = result_tables.get("primary_metric_rows") if isinstance(result_tables, dict) else None
    if isinstance(primary_rows, list) and primary_rows:
        lines.extend(["", "## Primary Metric Table", "", "| Dataset | Condition | Metric | Mean | Std | Count | Evidence ID |", "| --- | --- | --- | ---: | ---: | ---: | --- |"])
        for row in primary_rows[:40]:
            lines.append(
                f"| {row.get('dataset', '')} | {row.get('condition', '')} | {row.get('metric', '')} | "
                f"{format_metric_value(row.get('mean'))} | {format_metric_value(row.get('std'))} | "
                f"{row.get('count', '-')} | `{row.get('evidence_id', '')}` |"
            )
    lines.extend(["", "## Claims", "",
        "Verdicts reflect supplied or model judgments; a source reference does not independently prove a claim.", ""])
    for claim in claims:
        lines.append(f"- **{claim.verdict}** `{claim.claim_id}`: {claim.claim}")
        if claim.metric_refs:
            lines.append(f"  Metric refs: {', '.join(claim.metric_refs)}")
        if claim.limitations:
            lines.append(f"  Limitations: {'; '.join(claim.limitations)}")
    if audit.limitations or audit.weak_metric_signals:
        lines.extend(["", "## Limitations", ""])
        for item in list(audit.limitations) + list(audit.weak_metric_signals):
            lines.append(f"- {item}")
    return "\n".join(lines).strip() + "\n"


def build_prompt(
    context: AnalysisContext,
    metric_summary: dict[str, Any],
    fallback: AnalysisResult,
) -> str:
    context_payload = context.model_dump(mode="json")
    context_payload["project_results"] = compact_project_results_for_prompt(context.project_results, metric_summary)
    context_payload["task_contract"] = compact_task_contract_for_prompt(context.task_contract)
    payload = {
        "context": context_payload,
        "metric_summary": metric_summary,
        "deterministic_claims": [claim.model_dump(mode="json") for claim in fallback.claims],
        "deterministic_limitations": fallback.audit.limitations,
    }
    return (
        "Regenerate an experiment result analysis from the provided JSON.\n\n"
        "Return JSON with exactly these keys:\n"
        "- summary: object with method, results, limitations, reproduction_notes. Values must be short plain strings, not Markdown.\n"
        "- rubric_coverage: list of objects with category, verdict, evidence, limitations. Use categories from rubric_categories.\n"
        "- claims: list of claim objects. Each needs claim_id, claim, verdict, evidence, metric_refs, limitations, confidence.\n"
        "- analysis_audit: object with missing_required_metrics, weak_metric_signals, unsupported_claims, limitations, notes.\n"
        "- recommendation: object with action, task_disposition, reason, evidence_refs, alternatives_considered, continuation_conditions, revision_intent, revision_purpose, revision_constraints, revision_base, supplement.\n\n"
        "- goal_assessment: object with task_type (improvement/reproduction/evaluation/unknown), "
        "status (met/not_met/inconclusive), reason, evidence_refs, requested_delivery (auto/paper/analysis_report). "
        "Only set requested_delivery=paper when the user explicitly asks for a paper even with negative results; "
        "a generic report request or conditional paper request means auto. Assess the user's original goal, "
        "not merely whether execution passed. Cite evidence_id values from result_tables, "
        "provided metric names or artifact paths. Reproduction does not require beating a baseline. "
        "For improvement, check the primary objective and constraints, not any favorable metric. "
        "Use inconclusive when the success criteria or necessary evidence are missing.\n\n"
        "Rules:\n"
        "- Use only provided metrics and artifacts.\n"
        "- Canonical execution comparisons remain evidence even when result_tables are empty; "
        "preserve their source references and comparability limitations. Missing protocol metadata "
        "does not mean the measured baseline is absent.\n"
        "- The task_contract is a proposed scientific claim, not an inventory of executed candidates. "
        "The analysis_checkpoint measurement refs are the authority for what was actually run. "
        "A design that mentions several controls does not establish that those controls already exist. "
        "Keep such claims partially supported or not evaluated, but separately decide how to obtain "
        "the remaining evidence through authorized actions. In particular, the first measured candidate "
        "is the initial candidate; a future alternative is a possible next action, not a prerequisite "
        "for assessing that first result.\n"
        "- Do not claim judge success unless judge evidence appears in context.\n"
        "- supported/partially_supported claims must include metric_refs or evidence.\n"
        "- Use metric_refs from result_tables evidence_id values, not raw JSON objects.\n"
        "- Use unsupported when the measured evidence refutes a hypothesis; do not use not_evaluated for refuted hypotheses.\n"
        "- If metrics are weak, missing, all zero, or only resource signals, say so clearly.\n\n"
        "- recommendation.action must be one of supplement, revise_candidate, stop, request_input.\n"
        "- task_disposition must be continue for supplement/revise_candidate, stop only when the entire research task should end, and undecided when the recommendation is not grounded enough.\n"
        "- A task-level stop must include a concrete reason plus alternatives_considered or continuation_conditions; rejecting the current candidate alone is not a task stop.\n"
        "- supplement must be a JSON object, e.g. {\"seed\": 9, \"gap\": \"seed sensitivity\"} or {\"seeds\": [9, 10], \"gap\": \"seed sensitivity\"}; "
        "the example is a shape, not a seed choice. Use {} for other actions.\n"
        "- Missing protocol documentation is not a measurement failure: repeating an unchanged command "
        "does not resolve missing metadata. Request the missing facts or stop with limitations.\n"
        "- A supplement must name the evidence gap and concrete bounded conditions in supplement; "
        "do not invent commands, files, datasets, permissions, or seed mechanisms. Use the accepted "
        "context.metadata.execution_protocol as the authority: provide one explicit integer seed or a "
        "short explicit integer seeds list only "
        "when can_extend_seed_condition is true. If it is false, seed-based supplement is unavailable; "
        "this does not prohibit revise_candidate within an accepted CodeTask boundary. Never derive a "
        "seed from the iteration.\n"
        "- The research_goal states the final objective. Use context.metadata.analysis_checkpoint and "
        "its artifact refs to distinguish what is already measured from what remains to be done. A future "
        "candidate or comparison requested by the goal is not missing input merely because it has not "
        "been run yet. At a post-measurement checkpoint, assess the evidence and remaining authorized "
        "rounds, then recommend one justified bounded action or stop. Request input only for an actual "
        "missing external condition, permission, or user decision; do not request a future result that an "
        "already-authorized action can produce. Do not force a revision when evidence does not justify one.\n"
        "- revise_candidate must state the intended change and constraints, with revision_purpose=method_change; "
        "it is a proposal for the "
        "existing CodeTask boundary, not an assertion that a patch was applied. Set revision_base to "
        "candidate when continuing the current candidate; set it to baseline only when the evidence "
        "requires a fresh direction from the original baseline.\n"
        "- Adding logs, assertions, or other verification-only instrumentation does not revise the "
        "scientific candidate. Mark revision_purpose=verification_only and do not request "
        "revise_candidate or an identical training rerun solely to obtain that evidence. "
        "Use already saved logs/artifacts where possible; otherwise preserve method_validation as "
        "unverified and stop with a concrete limitation or request an authorized low-cost check.\n"
        "- A technically validated patch and validation_hints do not prove the candidate method. "
        "Use only candidate-specific execution evidence to describe mechanism fidelity; when its "
        "method_validation is 未检查, retain that limitation separately from measured metric deltas.\n"
        "- stop is appropriate when the goal is met, evidence is insufficient, no justified change remains, "
        "or the configured round/budget boundary is exhausted.\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2, default=str)}"
    )


def compact_task_contract_for_prompt(contract: dict[str, Any], *, max_task_chars: int = 1600) -> dict[str, Any]:
    if not isinstance(contract, dict) or not contract:
        return {}
    metric_contract = contract.get("metric_contract") if isinstance(contract.get("metric_contract"), dict) else {}
    artifact_contract = contract.get("artifact_contract") if isinstance(contract.get("artifact_contract"), dict) else {}
    evidence_plan = contract.get("evidence_plan") if isinstance(contract.get("evidence_plan"), dict) else {}
    task = str(contract.get("task") or "")
    return {
        "schema_version": contract.get("schema_version", ""),
        "contract_id": contract.get("contract_id", ""),
        "version_hash": contract.get("version_hash", ""),
        "task_kind": contract.get("task_kind", ""),
        "objective": contract.get("objective", ""),
        "hard_constraints": normalize_string_list(contract.get("hard_constraints"))[:20],
        "task_excerpt": task[:max_task_chars],
        "success_criteria": normalize_string_list(contract.get("success_criteria"))[:20],
        "metric_contract": {
            "primary_metric": metric_contract.get("primary_metric", ""),
            "required_metrics": normalize_string_list(metric_contract.get("required_metrics"))[:30],
            "metric_directions": dict(metric_contract.get("metric_directions") or {}),
        },
        "artifact_contract": {
            "required_artifacts": normalize_string_list(artifact_contract.get("required_artifacts"))[:30],
            "required_comparisons": normalize_string_list(artifact_contract.get("required_comparisons"))[:30],
        },
        "evidence_plan": {
            "hypotheses": normalize_string_list(evidence_plan.get("hypotheses"))[:20],
            "required_conditions": normalize_string_list(evidence_plan.get("required_conditions"))[:30],
            "required_datasets": normalize_string_list(evidence_plan.get("required_datasets"))[:30],
            "required_comparisons": normalize_string_list(evidence_plan.get("required_comparisons"))[:30],
        },
        "claim_specs": [
            row for row in contract.get("claim_specs", []) if isinstance(row, dict)
        ][:20]
        if isinstance(contract.get("claim_specs"), list)
        else [],
    }


def normalize_llm_result(
    response: dict[str, Any],
    context: AnalysisContext,
    metric_summary: dict[str, Any],
    *,
    fallback: AnalysisResult,
) -> AnalysisResult:
    claims = parse_claims(response.get("claims"), fallback=fallback.claims)
    rubric_coverage = parse_rubric_coverage(response.get("rubric_coverage"), fallback=fallback.rubric_coverage)
    audit_data = response.get("analysis_audit") if isinstance(response.get("analysis_audit"), dict) else {}
    audit = AnalysisAudit(
        llm_used=True,
        missing_required_metrics=list(metric_summary.get("missing_required_metrics") or []),
        weak_metric_signals=list(metric_summary.get("weak_metric_signals") or []),
        unsupported_claims=normalize_string_list(audit_data.get("unsupported_claims")),
        downgraded_claims=list(fallback.audit.downgraded_claims) if claims is fallback.claims else [],
        limitations=normalize_string_list(audit_data.get("limitations")),
        notes=normalize_string_list(audit_data.get("notes")),
    )
    recommendation = parse_recommendation(
        response.get("recommendation"), fallback=fallback.recommendation,
    )
    result = AnalysisResult(
        readme_markdown="",
        claims=claims,
        metric_summary=metric_summary,
        rubric_coverage=rubric_coverage,
        audit=audit,
        recommendation=recommendation,
        goal_assessment=parse_goal_assessment(response.get("goal_assessment"), context, metric_summary),
        decision_context=dict(context.metadata),
    )
    return _finalize_result(result, context, metric_summary, summary=response.get("summary"))


def _finalize_result(
    result: AnalysisResult, context: AnalysisContext, metric_summary: dict[str, Any], *, summary: Any = None,
) -> AnalysisResult:
    """Audit once before deriving any published representation of the claims."""
    result.audit = audit_result(result, metric_summary, context)
    result.claims_payload = claims_payload(context, metric_summary, result.claims, result.rubric_coverage)
    if result.audit.downgraded_claims and isinstance(summary, dict):
        # Free-form results may repeat a verdict just downgraded by the audit;
        # the original response remains available, but is not a safe conclusion.
        summary = {**summary, "results": "Some proposed claims lack traceable support; "
            "see the audited claim verdicts below."}
        result.audit.limitations.append("The model's results narrative was not adopted because some claims "
            "lacked supporting references; the original response is retained.")
    result.readme_markdown = render_analyzed_markdown(
        summary, context, metric_summary, result.claims, result.rubric_coverage, result.audit,
    ).strip() + "\n"
    return result


def parse_goal_assessment(
    value: Any, context: AnalysisContext, metric_summary: Mapping[str, Any],
) -> GoalAssessment:
    """Keep missing/ungrounded goal judgments uncertain, not silently successful."""
    if not isinstance(value, Mapping):
        return GoalAssessment()
    known = _known_goal_evidence(context, metric_summary)
    refs = normalize_string_list(value.get("evidence_refs"))
    status = str(value.get("status") or "inconclusive")
    task_type = str(value.get("task_type") or "unknown")
    reason = str(value.get("reason") or "").strip()
    if status not in {"met", "not_met", "inconclusive"}:
        status = "inconclusive"
    if task_type not in {"improvement", "reproduction", "evaluation", "unknown"}:
        task_type = "unknown"
    if not reason or not refs or any(ref not in known for ref in refs):
        status = "inconclusive"
        reason = (reason + " " if reason else "") + "Goal judgment lacks resolvable evidence references."
    delivery = str(value.get("requested_delivery") or "auto")
    if delivery not in {"auto", "paper", "analysis_report"}:
        delivery = "auto"
    return GoalAssessment(task_type=task_type, requested_delivery=delivery, status=status, reason=reason,
                          evidence_refs=[ref for ref in refs if ref in known])


def _known_goal_evidence(context: AnalysisContext, metric_summary: Mapping[str, Any]) -> set[str]:
    known = set(context.metrics) | set(context.artifacts.values())
    known.update(
        str(row["evidence_id"])
        for row in (metric_summary.get("result_tables") or {}).get("all_metric_rows", [])
        if isinstance(row, Mapping) and row.get("evidence_id")
    )
    return known


def parse_recommendation(
    value: Any, *, fallback: AnalysisRecommendation,
) -> AnalysisRecommendation:
    """Normalize the small model proposal without granting execution authority."""

    if not isinstance(value, Mapping):
        return AnalysisRecommendation(
            action="request_input",
            task_disposition="undecided",
            reason="The analysis did not provide a structured research recommendation; no scientific stop was inferred.",
            evidence_refs=fallback.evidence_refs,
        )
    action = str(value.get("action") or "").strip().lower().replace("-", "_")
    aliases = {"revise": "revise_candidate", "revision": "revise_candidate"}
    action = aliases.get(action, action)
    if action not in {"supplement", "revise_candidate", "stop", "request_input"}:
        return AnalysisRecommendation(
            action="request_input",
            task_disposition="undecided",
            reason=f"The analysis recommendation used unsupported action {action!r}; a bounded follow-up decision is required.",
            evidence_refs=fallback.evidence_refs,
        )
    disposition = str(value.get("task_disposition") or "undecided").strip().lower()
    if disposition not in {"continue", "stop", "undecided"}:
        disposition = "undecided"
    evidence_refs = normalize_string_list(value.get("evidence_refs"))[:12]
    alternatives = normalize_string_list(value.get("alternatives_considered"))[:12]
    continuation_conditions = normalize_string_list(value.get("continuation_conditions"))[:12]
    supplement = value.get("supplement")
    supplement = dict(supplement) if isinstance(supplement, Mapping) else {}
    # Commands and paths are application facts, not model-controlled fields.
    supplement = {
        key: supplement[key]
        for key in ("seed", "seeds", "gap", "conditions", "metric", "reason")
        if key in supplement
    }
    return AnalysisRecommendation(
        action=action,
        task_disposition=disposition,
        reason=str(value.get("reason") or "").strip(),
        evidence_refs=evidence_refs,
        alternatives_considered=alternatives,
        continuation_conditions=continuation_conditions,
        revision_intent=str(value.get("revision_intent") or "").strip(),
        revision_purpose=(
            str(value.get("revision_purpose") or "unspecified").strip().lower()
            if str(value.get("revision_purpose") or "unspecified").strip().lower()
            in {"method_change", "verification_only"} else "unspecified"
        ),
        revision_constraints=normalize_string_list(value.get("revision_constraints"))[:12],
        revision_base=(
            str(value.get("revision_base") or "candidate").strip().lower()
            if str(value.get("revision_base") or "candidate").strip().lower() in {"candidate", "baseline"}
            else "candidate"
        ),
        supplement=supplement,
    )


def parse_claims(value: Any, *, fallback: list[AnalysisClaim]) -> list[AnalysisClaim]:
    if isinstance(value, dict):
        for key in ("claims", "hypothesis_verdicts"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        return fallback
    claims: list[AnalysisClaim] = []
    for index, row in enumerate(value, start=1):
        if not isinstance(row, dict):
            continue
        claim = str(row.get("claim") or row.get("statement") or "").strip()
        if not claim:
            continue
        claims.append(
            AnalysisClaim(
                claim_id=str(row.get("claim_id") or row.get("hypothesis_id") or f"claim-{index}"),
                claim=claim,
                verdict=row_verdict(row),
                evidence=normalize_evidence(row.get("evidence")),
                metric_refs=normalize_metric_refs(row.get("metric_refs")),
                limitations=normalize_string_list(row.get("limitations")),
                confidence=normalize_confidence(row.get("confidence")),
            )
        )
    return claims or fallback


def render_analyzed_markdown(
    summary: Any,
    context: AnalysisContext,
    metric_summary: dict[str, Any],
    claims: list[AnalysisClaim],
    rubric_coverage: list[dict[str, Any]],
    audit: AnalysisAudit,
) -> str:
    if not isinstance(summary, dict):
        return deterministic_markdown(context, metric_summary, claims, rubric_coverage, audit)
    lines = [
        f"# Result Analysis: {context.task_id or context.title or 'experiment'}",
        "",
        "## Task",
        "",
        context.research_question or context.title or "(not provided)",
        "",
        "## Method",
        "",
        str(summary.get("method") or "(not provided)").strip(),
        "",
        "## Results",
        "",
        str(summary.get("results") or "(not provided)").strip(),
        "",
        "## Rubric Coverage",
        "",
        "| Category | Leaves | Status | Evidence |",
        "| --- | ---: | --- | --- |",
    ]
    for row in rubric_coverage:
        lines.append(
            f"| {row.get('category', 'Uncategorized')} | {row.get('leaf_count', 0)} | "
            f"{row.get('verdict', 'not_evaluated')} | {escape_table_text(row.get('evidence', ''))} |"
        )
    lines.extend(
        [
            "",
            "## Global Metrics",
            "",
        "| Metric | Value | Direction | Issues |",
        "| --- | ---: | --- | --- |",
        ]
    )
    for metric in metric_summary.get("metrics", []):
        lines.append(
            f"| `{metric.get('name')}` | {format_metric_value(metric.get('value'))} | "
            f"{metric.get('direction')} | {', '.join(metric.get('issues') or []) or '-'} |"
        )
    result_tables = metric_summary.get("result_tables") or {}
    primary_rows = result_tables.get("primary_metric_rows") if isinstance(result_tables, dict) else None
    if isinstance(primary_rows, list) and primary_rows:
        lines.extend(
            [
                "",
                "## Primary Metric Table",
                "",
                "| Dataset | Condition | Metric | Mean | Std | Count | Evidence ID |",
                "| --- | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for row in primary_rows[:40]:
            lines.append(
                f"| {row.get('dataset', '')} | {row.get('condition', '')} | {row.get('metric', '')} | "
                f"{format_metric_value(row.get('mean'))} | {format_metric_value(row.get('std'))} | "
                f"{row.get('count', '-')} | `{row.get('evidence_id', '')}` |"
            )
    lines.extend(["", "## Claim Verdicts", "",
        "Verdicts reflect supplied or model judgments; a source reference does not independently prove a claim.", ""])
    for claim in claims:
        lines.append(f"- **{claim.verdict}** `{claim.claim_id}`: {claim.claim}")
        if claim.metric_refs:
            lines.append(f"  Metric refs: {', '.join(claim.metric_refs)}")
        if claim.limitations:
            lines.append(f"  Limitations: {'; '.join(claim.limitations)}")
    limitations = [str(summary.get("limitations") or "").strip()]
    limitations.extend(item for item in audit.limitations if item and item not in limitations)
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "; ".join(item for item in limitations if item) or "(not provided)",
            "",
            "## Reproduction Notes",
            "",
            str(summary.get("reproduction_notes") or "(not provided)").strip(),
        ]
    )
    return "\n".join(lines)


def audit_result(result: AnalysisResult, metric_summary: dict[str, Any],
                 context: AnalysisContext | None = None) -> AnalysisAudit:
    context = context or AnalysisContext()
    audit = result.audit.model_copy(deep=True)
    audit.missing_required_metrics = sorted(
        set(audit.missing_required_metrics) | set(metric_summary.get("missing_required_metrics") or [])
    )
    audit.weak_metric_signals = sorted(
        set(audit.weak_metric_signals) | set(metric_summary.get("weak_metric_signals") or [])
    )
    available = {
        str(row["name"]) for row in metric_summary.get("metrics", [])
        if isinstance(row, dict) and row.get("name") and is_number(row.get("value"))
        and math.isfinite(float(row["value"]))
    }
    tables = metric_summary.get("result_tables") or {}
    rows = extract_result_table_rows(context.project_results) if context.project_results else tables.get("all_metric_rows", [])
    available.update(str(row["evidence_id"]) for row in rows
        if isinstance(row, dict) and row.get("evidence_id") and is_number(row.get("mean"))
        and math.isfinite(float(row["mean"])))
    downgraded: list[str] = []
    unsupported: list[str] = []
    for claim in result.claims:
        if claim.verdict in {"supported", "partially_supported", "unsupported"}:
            refs = set(claim.metric_refs)
            has_quote = False
            for evidence in claim.evidence:
                for key in ("source", "ref", "evidence_id", "metric_ref", "artifact", "path"):
                    if isinstance(evidence.get(key), str):
                        refs.add(evidence[key])
                for key in ("quote", "text"):
                    quote = evidence.get(key)
                    if isinstance(quote, str) and quote.strip() and quote in context.existing_writeup:
                        has_quote = True
            missing = sorted(ref for ref in refs if ref and ref not in available)
            if missing:
                claim.limitations.append("References not found in the supplied analysis inputs: " + ", ".join(missing))
            if not (refs & available or has_quote):
                claim.verdict = "not_evaluated"
                claim.confidence = "low"
                claim.limitations.append("Claim was downgraded because no reference resolves to a supplied metric, "
                    "result row, or exact source passage. A path is not an observation; "
                    "a reference alone does not prove the claim.")
                downgraded.append(claim.claim_id)
        if claim.verdict in {"unsupported", "not_evaluated"}:
            unsupported.append(claim.claim_id)
    audit.downgraded_claims = sorted(set(audit.downgraded_claims) | set(downgraded))
    audit.unsupported_claims = sorted(set(audit.unsupported_claims) | set(unsupported))
    if not result.claims:
        audit.limitations.append("No claims were generated.")
    return audit


def derive_analysis_status(
    context: AnalysisContext,
    metric_summary: Mapping[str, Any],
) -> tuple[AnalysisStatus, list[str]]:
    """Classify observed experiment evidence without choosing a next step.

    A standalone analysis has no execution outcome to classify, so it remains
    ``incomplete``. When a canonical execution record is present, execution
    failure and blocking are preserved, guard or metric deficiencies are
    reported as incomplete, and only an explicit comparison verdict can
    produce ``metric_below_target``. Research policy remains in the
    caller-owned session layer instead of being inferred from prose or raw
    metric values.
    """

    execution = context.project_results.get("execution_result")
    if not isinstance(execution, Mapping):
        return "incomplete", ["No canonical execution result was provided."]

    execution_status = str(execution.get("status") or "unknown").strip().lower()
    if execution_status in {"blocked", "blocked_by_validation"}:
        return "blocked", [f"Execution was blocked with status `{execution_status}`."]
    if execution_status not in {"passed", "completed"}:
        return "failed", [f"Execution did not pass: `{execution_status}`."]

    guard = execution.get("guard")
    if isinstance(guard, Mapping) and str(guard.get("status") or "").strip().lower() == "failed":
        return "incomplete", ["The result guard reported blocking errors."]
    missing = list(metric_summary.get("missing_required_metrics") or [])
    if missing:
        return "incomplete", [
            "Required metrics are missing: " + ", ".join(str(item) for item in missing[:12])
        ]

    comparisons = execution.get("comparisons")
    if isinstance(comparisons, list):
        for comparison in comparisons:
            if not isinstance(comparison, Mapping):
                continue
            verdict = str(comparison.get("verdict") or "").strip().lower()
            if verdict in {"regressed", "metric_below_target"}:
                return "metric_below_target", [
                    f"An explicit experiment comparison returned `{verdict}`."
                ]

    return "passed", ["Execution passed and required result evidence is present."]


def write_analysis_artifacts(output_dir: Path, context: AnalysisContext, result: AnalysisResult) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "analysis_context.json", context.model_dump(mode="json"))
    write_json(output_dir / "metric_summary.json", result.metric_summary)
    write_json(output_dir / "rubric_coverage.json", result.rubric_coverage)
    write_json(output_dir / "claims.json", result.claims_payload)
    write_text(output_dir / "analysis_report.md", result.readme_markdown)
    write_json(output_dir / "analysis_audit.json", result.audit.model_dump(mode="json"))
    write_json(
        output_dir / "analysis_status.json",
        {
            "schema_version": "analysis_status.v1",
            "status": result.status,
            "reasons": result.status_reasons,
        },
    )
    if result.raw_llm_response is not None:
        write_json(output_dir / "analysis_response.json", result.raw_llm_response)


def claims_payload(
    context: AnalysisContext,
    metric_summary: dict[str, Any],
    claims: list[AnalysisClaim],
    rubric_coverage: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "simple_ar_result_claims.v1",
        "task_id": context.task_id,
        "topic_id": context.task_id,
        "summary_metrics": context.metrics,
        "metric_summary": metric_summary,
        "rubric_coverage": rubric_coverage,
        "task_contract": {
            "contract_id": context.task_contract.get("contract_id", ""),
            "version_hash": context.task_contract.get("version_hash", ""),
            "schema_version": context.task_contract.get("schema_version", ""),
        }
        if isinstance(context.task_contract, dict) and context.task_contract
        else {},
        "hypothesis_verdicts": [claim.model_dump(mode="json") for claim in claims],
        "claims": [claim.model_dump(mode="json") for claim in claims],
    }


def build_rubric_categories(context: AnalysisContext) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for criterion in context.criteria:
        if not isinstance(criterion, dict):
            continue
        category = str(criterion.get("task_category") or "Uncategorized")
        group = groups.setdefault(category, {"category": category, "leaf_count": 0, "weight": 0.0, "leaves": []})
        group["leaf_count"] += 1
        try:
            group["weight"] += float(criterion.get("weight") or 0.0)
        except (TypeError, ValueError):
            pass
        group["leaves"].append(
            {
                "id": criterion.get("id"),
                "requirements": criterion.get("requirements"),
                "weight": criterion.get("weight"),
                "finegrained_task_category": criterion.get("finegrained_task_category"),
            }
        )
    return list(groups.values())


def deterministic_rubric_coverage(
    context: AnalysisContext,
    metric_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group in metric_summary.get("rubric_categories", []) or build_rubric_categories(context):
        category = str(group.get("category") or "Uncategorized")
        if category == "Code Execution" and metric_summary.get("metric_count"):
            verdict = "partially_supported"
            evidence = "Numeric metrics were produced; detailed criterion coverage still needs review."
        elif category == "Result Analysis" and context.existing_writeup:
            verdict = "partially_supported"
            evidence = "A writeup was found; claim grounding still needs review."
        else:
            verdict = "not_evaluated"
            evidence = "No category-specific review was generated."
        rows.append(
            {
                "category": category,
                "leaf_count": group.get("leaf_count", 0),
                "weight": group.get("weight", 0.0),
                "verdict": verdict,
                "evidence": evidence,
                "limitations": [],
            }
        )
    return rows


def parse_rubric_coverage(value: Any, *, fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return fallback
    fallback_by_category = {
        str(row.get("category") or "Uncategorized"): row
        for row in fallback
        if isinstance(row, dict)
    }
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        category = str(row.get("category") or "Uncategorized")
        base = fallback_by_category.get(category, {})
        leaf_count = safe_positive_int(row.get("leaf_count"), default=safe_positive_int(base.get("leaf_count"), default=0))
        weight = row.get("weight", base.get("weight", 0.0))
        rows.append(
            {
                "category": category,
                "leaf_count": leaf_count,
                "weight": weight,
                "verdict": normalize_verdict(row.get("verdict")),
                "evidence": str(row.get("evidence") or ""),
                "limitations": normalize_string_list(row.get("limitations")),
            }
        )
        seen.add(category)
    for category, row in fallback_by_category.items():
        if category not in seen:
            rows.append(row)
    return rows or fallback


def build_result_tables(context: AnalysisContext) -> dict[str, Any]:
    table_rows = extract_result_table_rows(context.project_results)
    primary_metric = ""
    expected = context.expected_metrics or []
    if expected and isinstance(expected[0], dict):
        primary_metric = str(expected[0].get("name") or "")
    primary_metric = primary_metric or infer_primary_metric(context)
    primary_rows = [row for row in table_rows if metric_names_match(str(row.get("metric") or ""), primary_metric)]
    return {
        "primary_metric": primary_metric,
        "primary_metric_rows": primary_rows,
        "all_metric_rows": table_rows[:240],
    }


def infer_primary_metric(context: AnalysisContext) -> str:
    for name, direction in context.metric_directions.items():
        if direction not in {"resource", "ignore"} and not is_auxiliary_metric_name(str(name)):
            return str(name)
    for name in context.metrics:
        text = str(name)
        if not is_auxiliary_metric_name(text):
            return text
    return next(iter(context.metrics), "")


def metric_names_match(observed: str, expected: str) -> bool:
    if not observed or not expected:
        return False
    return bool(metric_name_aliases(observed) & metric_name_aliases(expected))


def metric_name_aliases(name: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    aliases = {normalized} if normalized else set()
    if not normalized:
        return aliases

    prefixes = (
        "test",
        "heldout",
        "held_out",
        "validation",
        "val",
        "mean",
        "avg",
        "average",
        "overall",
        "final",
    )
    for prefix in prefixes:
        marker = prefix + "_"
        if normalized.startswith(marker) and len(normalized) > len(marker):
            aliases.add(normalized[len(marker) :])

    suffixes = ("_mean", "_score")
    for suffix in suffixes:
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            aliases.add(normalized[: -len(suffix)])

    # Keep semantically specific names such as balanced_accuracy distinct, but
    # let common project-level labels match per-cell metric records.
    if normalized in {"test_accuracy", "heldout_accuracy", "held_out_accuracy", "mean_accuracy", "avg_accuracy"}:
        aliases.add("accuracy")
    if normalized in {"test_rmse", "mean_rmse", "avg_rmse", "overall_rmse"}:
        aliases.add("rmse")
    if normalized in {"test_mae", "mean_mae", "avg_mae", "overall_mae"}:
        aliases.add("mae")
    return aliases


def evidence_chain_warnings(context: AnalysisContext, result_tables: dict[str, Any]) -> list[str]:
    rows = result_tables.get("all_metric_rows") if isinstance(result_tables, dict) else None
    if isinstance(rows, list) and rows:
        return []
    warnings: list[str] = []
    if context.metrics:
        warnings.append("no condition-level result table extracted")
    raw_count = len(discover_raw_records(context.project_results))
    if raw_count:
        warnings.append(f"raw condition records were present but not normalized into result tables: {raw_count}")
    return warnings


def find_per_cell_records(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    summary = data.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("per_cell"), list):
        return [row for row in summary["per_cell"] if isinstance(row, dict)]
    if isinstance(data.get("per_cell"), list):
        return [row for row in data["per_cell"] if isinstance(row, dict)]
    return []


def extract_result_table_rows(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, (dict, list)):
        return []
    rows: list[dict[str, Any]] = []
    for record in aggregate_records(data):
        rows.extend(normalized_metric_rows(record))
    rows.extend(aggregate_raw_records(raw_records(data)))
    if rows:
        return dedupe_table_rows(rows)
    return []


def aggregate_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return discover_aggregate_records(data)
    if not isinstance(data, dict):
        return []
    records: list[dict[str, Any]] = []
    summary = data.get("summary")
    if isinstance(summary, dict) and isinstance(summary.get("per_cell"), list):
        records.extend(row for row in summary["per_cell"] if isinstance(row, dict))
    for key in (
        "per_cell",
        "cells",
        "aggregates",
        "aggregate_rows",
        "summaries",
        "condition_aggregates",
    ):
        value = data.get(key)
        if isinstance(value, list):
            records.extend(row for row in value if isinstance(row, dict))
    records.extend(discover_aggregate_records(data))
    return records


def discover_aggregate_records(value: Any, *, depth: int = 0, max_depth: int = 4) -> list[dict[str, Any]]:
    if depth > max_depth:
        return []
    records: list[dict[str, Any]] = []
    if isinstance(value, list):
        for row in value:
            if isinstance(row, dict) and looks_like_aggregate_record(row):
                records.append(row)
            elif isinstance(row, (dict, list)):
                records.extend(discover_aggregate_records(row, depth=depth + 1, max_depth=max_depth))
        return records
    if not isinstance(value, dict):
        return records
    if looks_like_aggregate_record(value):
        records.append(value)
    for key, child in value.items():
        if key in {"split_rows", "rows", "runs", "splits", "seed_evidence"}:
            continue
        if isinstance(child, (dict, list)):
            records.extend(discover_aggregate_records(child, depth=depth + 1, max_depth=max_depth))
    return records


def looks_like_aggregate_record(record: dict[str, Any]) -> bool:
    if not record_dataset(record) or not record_condition(record):
        return False
    if isinstance(record.get("mean"), dict):
        return True
    if record.get("metric") and is_number(record.get("mean")):
        return True
    for key, value in record.items():
        if isinstance(value, dict) and "mean" in value:
            return True
        if key.endswith("_mean") and is_number(value):
            return True
        if key.startswith("mean_") and is_number(value):
            return True
    return False


def raw_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return dedupe_records(discover_raw_records(data))
    if not isinstance(data, dict):
        return []
    for keys in (
        ("cells", "cell_records", "records", "per_cell"),
        ("rows", "runs", "splits", "seed_evidence"),
        ("condition_summaries", "summaries"),
    ):
        records: list[dict[str, Any]] = []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                records.extend(row for row in value if isinstance(row, dict))
        if records:
            return dedupe_records(records)
    return dedupe_records(discover_raw_records(data))


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        try:
            key = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
        except TypeError:
            key = repr(sorted(record.items()))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def discover_raw_records(value: Any, *, depth: int = 0, max_depth: int = 4) -> list[dict[str, Any]]:
    if depth > max_depth:
        return []
    records: list[dict[str, Any]] = []
    if isinstance(value, list):
        dict_rows = [row for row in value if isinstance(row, dict)]
        raw_rows = [
            row
            for row in dict_rows
            if looks_like_raw_result_record(row) and not looks_like_aggregate_record(row)
        ]
        if raw_rows:
            return raw_rows
        for row in value:
            if isinstance(row, (dict, list)):
                records.extend(discover_raw_records(row, depth=depth + 1, max_depth=max_depth))
        return records
    if not isinstance(value, dict):
        return []
    for child in value.values():
        if isinstance(child, (dict, list)):
            records.extend(discover_raw_records(child, depth=depth + 1, max_depth=max_depth))
    return records


def looks_like_raw_result_record(record: dict[str, Any]) -> bool:
    if not record_condition(record):
        return False
    numeric_keys = [
        key
        for key, value in record.items()
        if is_number(value) and key not in RAW_NON_METRIC_KEYS and not key.endswith("_id")
    ]
    nested_metrics = record.get("metrics")
    return bool(
        numeric_keys
        or (
            isinstance(nested_metrics, dict)
            and any(is_number(value) for value in nested_metrics.values())
        )
    )


def normalized_metric_rows(record: dict[str, Any]) -> list[dict[str, Any]]:
    dataset = record_dataset(record)
    condition = record_condition(record)
    if not dataset or not condition:
        return []
    count = first_present(record, "count", "n", "n_seeds", "seed_count", "n_splits", "split_count")
    rows: list[dict[str, Any]] = []

    for key, value in record.items():
        if isinstance(value, dict) and "mean" in value:
            rows.append(table_row(dataset, condition, key, value.get("mean"), value.get("std"), value.get("count", count)))

    mean_map = record.get("mean")
    std_map = record.get("std")
    if isinstance(mean_map, dict):
        for metric, mean in mean_map.items():
            std = std_map.get(metric) if isinstance(std_map, dict) else None
            rows.append(table_row(dataset, condition, str(metric), mean, std, count))

    scalar_metric = record.get("metric")
    if scalar_metric and is_number(record.get("mean")):
        rows.append(table_row(dataset, condition, str(scalar_metric), record.get("mean"), record.get("std"), count))

    for key, value in record.items():
        if key.endswith("_mean") and is_number(value):
            metric = key[: -len("_mean")]
            std = record.get(f"{metric}_std")
            rows.append(table_row(dataset, condition, metric, value, std, count))
        elif key.startswith("mean_") and is_number(value):
            metric = key[len("mean_") :]
            std = record.get(f"std_{metric}")
            rows.append(table_row(dataset, condition, metric, value, std, count))

    return rows


def aggregate_raw_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[float]] = {}
    for record in records:
        dataset = record_dataset(record)
        condition = record_condition(record)
        if not dataset or not condition:
            continue
        metric_values = raw_metric_values(record)
        for metric, value in metric_values.items():
            if is_number(value):
                buckets.setdefault((dataset, condition, metric), []).append(float(value))
    rows: list[dict[str, Any]] = []
    for (dataset, condition, metric), values in buckets.items():
        if not values:
            continue
        mean = sum(values) / len(values)
        std = sample_std(values)
        rows.append(table_row(dataset, condition, metric, mean, std, len(values)))
    return rows


def raw_metric_values(record: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    nested = record.get("metrics")
    if isinstance(nested, dict):
        values.update(nested)
    for key, value in record.items():
        if key in RAW_NON_METRIC_KEYS or key.endswith("_id") or key.endswith("_name"):
            continue
        if isinstance(value, (dict, list, tuple, set)):
            continue
        if is_number(value):
            values[key] = value
    return values


RAW_NON_METRIC_KEYS = {
    "seed",
    "split_seed",
    "split_idx",
    "fold",
    "count",
    "n",
    "n_train",
    "n_test",
    "n_features",
    "n_classes",
    "n_samples",
    "dataset_num_classes",
    "dataset_split_seed",
    "fallback_used",
    "is_modal_selection",
}


def is_auxiliary_metric_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        token in lowered
        for token in (
            "runtime",
            "time",
            "wall_clock",
            "memory",
            "seed_count",
            "metric_count",
            "dataset_count",
            "condition_count",
            "row_count",
            "n_samples",
        )
    )


def record_dataset(record: dict[str, Any]) -> str:
    aliases = ("dataset_name", "dataset", "dataset_id", "family", "regime", "source", "data")
    value = first_nonempty(record, aliases)
    if isinstance(value, dict):
        value = value.get("name") or value.get("id") or value.get("dataset_id") or ""
    text = str(value).strip()
    if text:
        return text
    nested = nested_identity(record)
    value = first_nonempty(nested, aliases)
    if value:
        return str(value).strip()
    for key in ("n_train", "train_size", "sample_size", "horizon", "step"):
        if nested.get(key) is not None:
            return f"{key}={nested[key]}"
    for key in ("n_train", "train_size", "sample_size", "horizon", "step"):
        if record.get(key) is not None:
            return f"{key}={record[key]}"
    return "all"


def record_condition(record: dict[str, Any]) -> str:
    aliases = (
        "condition_name",
        "condition_id",
        "strategy_name",
        "model",
        "model_name",
        "model_type",
        "method",
        "method_name",
        "algorithm",
        "algorithm_name",
        "detector",
        "detector_name",
        "estimator",
        "estimator_name",
        "learner",
        "learner_name",
        "kernel",
        "kernel_name",
        "condition",
        "schedule",
    )
    value = first_nonempty(record, aliases)
    if isinstance(value, dict):
        value = (
            value.get("condition_id")
            or value.get("name")
            or value.get("id")
            or value.get("method")
            or value.get("model")
            or ""
        )
    text = str(value).strip()
    nested = nested_identity(record)
    if not text:
        text = str(first_nonempty(nested, aliases) or "").strip()
    if not text:
        text = inferred_identifier(record)
    if not text:
        text = inferred_identifier(nested)
    if not text and record.get("n_train") is not None and record.get("method") is not None:
        text = f"{record.get('method')}:n_train={record.get('n_train')}"
    return text


def first_nonempty(record: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return ""


def nested_identity(record: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("key", "identity", "configuration", "config", "parameters"):
        value = record.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def inferred_identifier(record: Mapping[str, Any]) -> str:
    excluded = {
        "dataset_id",
        "hypothesis_id",
        "claim_id",
        "seed_id",
        "split_id",
        "fold_id",
        "run_id",
        "trial_id",
    }
    for key, value in record.items():
        lowered = str(key).lower()
        if lowered in excluded or not lowered.endswith("_id"):
            continue
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def table_row(dataset: str, condition: str, metric: str, mean: Any, std: Any, count: Any) -> dict[str, Any]:
    evidence_id = f"{metric}:{dataset}:{condition}".replace(" ", "_")
    return {
        "evidence_id": evidence_id,
        "dataset": dataset,
        "condition": condition,
        "metric": metric,
        "mean": mean,
        "std": std,
        "count": count,
    }


def dedupe_table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        key = (str(row.get("dataset")), str(row.get("condition")), str(row.get("metric")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def first_present(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if record.get(key) is not None:
            return record[key]
    return None


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def sample_std(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return variance ** 0.5


def compact_project_results_for_prompt(data: Any, metric_summary: dict[str, Any]) -> Any:
    if not isinstance(data, dict):
        return data
    compact: dict[str, Any] = {
        "available_keys": sorted(str(key) for key in data.keys()),
        "result_tables": metric_summary.get("result_tables", {}),
    }
    for key in ("claims", "hypothesis_verdicts", "verdicts", "hypotheses", "metrics", "metric_bundle", "limitations", "comparisons", "paired_summary", "implementation"):
        value = data.get(key)
        if value is not None:
            compact[key] = value
    failure_diagnosis = data.get("failure_diagnosis")
    if isinstance(failure_diagnosis, dict):
        compact["failure_diagnosis"] = failure_diagnosis
    execution = data.get("execution_result")
    if isinstance(execution, dict):
        # Keep canonical measurements and their meaning, not process logs.
        compact["execution_result"] = {
            key: execution[key] for key in (
                "status", "execution_status", "returncode", "timed_out", "metrics",
                "command", "measurement", "experiment_contract", "comparisons",
                "limitations", "missing_measurements", "failed_measurements",
                "implementation_ref", "candidate_revision", "superseded_candidates",
                "guard", "diagnosis", "output_evidence",
            ) if key in execution
        }
    if isinstance(data.get("summary"), dict):
        compact["summary"] = data["summary"]
    source = data.get("_artifact_source")
    if source:
        compact["_artifact_source"] = source
    tables = metric_summary.get("result_tables")
    all_rows = tables.get("all_metric_rows") if isinstance(tables, dict) else None
    if not all_rows:
        records = raw_records(data)
        if records:
            compact["raw_result_count"] = len(records)
            compact["raw_result_sample"] = bounded_record_sample(records)
    return compact


def bounded_record_sample(
    records: list[dict[str, Any]],
    *,
    max_records: int = 8,
    max_chars: int = 12000,
) -> list[Any]:
    sample: list[Any] = []
    used = 0
    for record in records[:max_records]:
        rendered = json.dumps(record, ensure_ascii=False, default=str)
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(rendered) <= remaining:
            sample.append(record)
            used += len(rendered)
            continue
        sample.append({"_truncated_record": rendered[:remaining]})
        break
    return sample


def deterministic_limitations(metric_summary: dict[str, Any]) -> list[str]:
    limitations: list[str] = []
    if metric_summary.get("missing_required_metrics"):
        limitations.append("Some expected metrics were missing.")
    if metric_summary.get("weak_metric_signals"):
        limitations.append("Metric signals were weak or incomplete.")
    if not metric_summary.get("metric_count"):
        limitations.append("No numeric metrics were found.")
    return limitations


def normalize_verdict(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"supported", "support", "supports", "pass", "passed"}:
        return "supported"
    if text in {"partial", "partially_supported", "partially supported"}:
        return "partially_supported"
    if text in {"unsupported", "failed", "fail", "refute", "refuted", "contradicted", "false"}:
        return "unsupported"
    return "not_evaluated"


def safe_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def normalize_confidence(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"high", "medium", "low"}:
        return text
    return "low"


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)]
    if isinstance(value, (list, tuple, set)):
        rows: list[str] = []
        for item in value:
            if item is None:
                continue
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            else:
                text = str(item).strip()
            if text:
                rows.append(text)
        return rows
    text = str(value).strip()
    return [text] if text else []


def normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"text": str(item)} for item in value]
    if isinstance(value, dict):
        return [value]
    if value:
        return [{"text": str(value)}]
    return []


def normalize_metric_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            metric = item.get("metric")
            dataset = item.get("dataset_name") or item.get("dataset")
            condition = item.get("condition_name") or item.get("condition") or item.get("model")
            if metric and dataset and condition:
                refs.append(f"{metric}:{dataset}:{condition}".replace(" ", "_"))
            elif metric:
                refs.append(str(metric))
            else:
                refs.append(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str))
        elif item:
            refs.append(str(item))
    return refs


def available_metric_names(metric_summary: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for row in metric_summary.get("metrics", []):
        if isinstance(row, dict) and row.get("present") and row.get("name"):
            names.append(str(row["name"]))
    return names


def format_metric_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def escape_table_text(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
