"""Bounded cross-section review using the existing report drafts and evidence."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.research.contracts import CLAIM_SCOPE_RULES
from simple_ar.report.tools import report_tool_specs
from simple_ar.report.templates import is_builtin_template, reviewing_template_guidance
from simple_ar.report.narrative import document_plan_context, report_edit_scope, report_tool_context, review_source_evidence
from simple_ar.report.data_delivery import DELIVERY_RULES
from simple_ar.report.review_evidence import (
    EVIDENCE_QUOTE_RULES, EVIDENCE_QUOTE_SCHEMA, validate_evidence_quotes, validate_finding_anchors,
    review_draft_quote_sources, draft_quote_present, review_evidence_locator, resolve_review_evidence,
)
from simple_ar.report.schema import (
    ReportMemory,
    ReportIterationRecord,
    ReportRuntimeConfig,
    ReportSectionDraft,
    ReportSectionReview,
    ReportTemplateBundle,
    ReportToolResult,
    ReviewerFinding,
    ReportDraftQuote,
    ReportToolCall,
    REVIEW_ACTION_RULES,
    finding_requires_resolution,
)


MAX_DOCUMENT_REVIEW_CHARS = 60_000
MAX_DOCUMENT_REVISION_SECTIONS = 2


def document_revision_limit(memory: ReportMemory) -> int:
    """Argument-led documents can correct every frozen owning section.

    Existing per-section rounds and the session ledger still bound work. Old
    checkpoints without an argument plan retain their original two targets;
    recovery never creates a new plan or resets previously consumed rounds.
    """
    if memory.document_plan is not None and memory.document_plan.argument_plan is not None:
        return len(memory.document_plan.sections)
    return MAX_DOCUMENT_REVISION_SECTIONS


def edit_joint_document(
    *, memory: ReportMemory, config: ReportRuntimeConfig,
    sections: list[ReportSectionDraft], iterations: list[ReportIterationRecord],
    reviews: list[ReportSectionReview], all_findings: list[ReviewerFinding],
    checkpoint: Callable[[], None],
    draft: Callable[[ReportIterationRecord, list[ReportSectionDraft]], list[ReportSectionDraft]],
    inspect: Callable[[list[ReportSectionDraft], list[ReviewerFinding] | None], list[ReportSectionReview]],
) -> None:
    """Revise and adopt a candidate set atomically in the existing checkpoint.

    The frozen plans and source evidence stay read-only. Saved candidates and
    completed checks are reused after interruption; rounds count proposal
    starts, including unavailable calls, rather than granting recovery credit.
    """
    def needs_change(review: ReportSectionReview) -> bool:
        return (review.verdict in {"revise_required", "fail"}
                or any(finding_requires_resolution(row) for row in review.findings))

    attempts = [row for row in iterations if row.action == "document_joint_revise"]
    legacy_rounds = max(Counter(row.section_id for row in iterations
        if row.action == "document_revise").values(), default=0)
    event = attempts[-1] if attempts and attempts[-1].status == "drafted" and not attempts[-1].adopted else None
    active = coalesce_document_reviews([row for row in reviews if needs_change(row)])
    def next_reviews(rows: list[ReportSectionReview]) -> list[ReportSectionReview]:
        # Only a complete inspection can supersede the previous instructions.
        pending = [row for row in rows if needs_change(row) or row.context_requests]
        complete = {row.section_id for row in rows} == {row.section_id for row in sections}
        return coalesce_document_reviews(pending if complete else [*active, *pending])

    if event is None and attempts and attempts[-1].status == "rejected":
        checks = [row for row in iterations if row.iteration > attempts[-1].iteration
                  and row.action == "document_joint_verify" and row.status == "completed"]
        if checks:
            active = next_reviews(checks[-1].section_reviews)
    for review in reviews:
        all_findings.extend(review.findings)
        memory.reviewer_findings.extend(row for row in review.findings if row not in memory.reviewer_findings)
    while event is not None or (active and legacy_rounds + len(attempts) < config.max_review_iterations):
        if event is None:
            # A cross-section opinion can require changing its quoted neighbor,
            # not just the section that owns the opinion. Keep that identity
            # once; explicit document scope or literally anchored neighbors add targets.
            # Saved requests/candidates never acquire targets during recovery.
            current = {row.section_id: (row.heading, row.draft_markdown) for row in sections}
            related = []
            for review in active:
                if review.revision_scope == "document" and any(
                    finding_requires_resolution(row) and row.required_action == "revise"
                    for row in review.findings
                ):
                    related.extend(ReportSectionReview(section_id=sid, verdict="revise_required",
                        revision_instructions=[*review.revision_instructions,
                            *[row.suggested_action for row in review.findings if row.suggested_action],
                            "Coordinate the document-wide correction; retain unaffected prose unchanged."],
                        notes=f"Document-wide edit requested by {review.section_id!r}; findings retain their original owner.")
                        for sid in current if sid != review.section_id)
                for finding in review.findings:
                    if not finding_requires_resolution(finding) or finding.required_action in {"advisory", "verify"}:
                        continue
                    for anchor in finding.draft_quotes:
                        if anchor.section_id != review.section_id and draft_quote_present(current, anchor.section_id, anchor.quote):
                            related.append(ReportSectionReview(section_id=anchor.section_id, verdict="revise_required",
                                revision_instructions=[*review.revision_instructions,
                                    *([finding.suggested_action] if finding.suggested_action else [])],
                                notes=f"Joint context for opinion {finding.finding_id!r} owned by {review.section_id!r}; "
                                      "coordinate the correction without treating quoted context as a separate defect."))
            active = coalesce_document_reviews([*active, *related])
            event = ReportIterationRecord(iteration=len(iterations) + 1, section_id="",
                action="document_joint_revise", status="started", adopted=False,
                section_reviews=active, summary="Joint correction of frozen section targets.")
            iterations.append(event)
            attempts.append(event)
            checkpoint()
        # Recover the saved request, not a new inspection of the unchanged old
        # manuscript; it must not redefine an interrupted candidate's scope.
        active = event.section_reviews
        targets = {row.section_id for row in active}
        originals = {row.section_id: row for row in sections}
        try:
            prior_by_id = {row.section_id: row for attempt in attempts if attempt.iteration < event.iteration
                           for row in attempt.drafts}
            baseline = [prior_by_id.get(row.section_id, row) for row in sections]
            if not event.drafts:
                event.drafts = draft(event, baseline)
                event.status = "drafted"
                checkpoint()
            candidate_by_id = {row.section_id: row for row in event.drafts}
            if (set(candidate_by_id) != targets or len(event.drafts) != len(targets)
                    or not targets.issubset(originals)
                    or any(not row.draft_markdown.strip() for row in event.drafts)):
                raise ValueError("Joint revision must retain each eligible section exactly once with complete prose.")
            candidate = [candidate_by_id.get(row.section_id, row) for row in baseline]
            prior = [finding for review in active for finding in review.findings
                     if finding.type not in DOCUMENT_CONTROL_FINDING_TYPES]
            # Judge the complete candidate against the request, sources and
            # editing concerns in one independent inspection. Old opinion IDs
            # are bookkeeping, not an additional truth source or adoption gate.
            # Retain old checks in history, but never let a failed closure call
            # suppress inspection of the actual revised manuscript.
            checks = [row for row in iterations if row.iteration > event.iteration
                      and row.action == "document_joint_verify"]
            verified = next((row for row in checks if row.action == "document_joint_verify"), None)
            if verified is None:
                rows = inspect(candidate, prior)
                verified = ReportIterationRecord(iteration=len(iterations) + 1, section_id="",
                    action="document_joint_verify", status="completed", section_reviews=rows,
                    requested_findings=prior)
                iterations.append(verified)
                checkpoint()
            findings = [row for review in verified.section_reviews for row in review.findings]
            all_findings.extend(findings)
            acceptable = ({row.section_id for row in verified.section_reviews}
                          == {row.section_id for row in candidate}
                          and not any(needs_change(row) or row.context_requests
                                      for row in verified.section_reviews))
            if acceptable:
                sections[:] = candidate
                event.status, event.adopted = "verified", True
                resolved = [row for attempt in attempts for review in attempt.section_reviews for row in review.findings]
                memory.reviewer_findings = [row for row in memory.reviewer_findings if row not in resolved]
                # Candidate observations belong to the delivered manuscript
                # only after atomic adoption. Rejected observations remain in
                # the iteration history and guide the next candidate instead.
                memory.reviewer_findings.extend(row for row in findings if row not in memory.reviewer_findings)
                memory.reviewer_findings = [row for row in memory.reviewer_findings
                    if not (row.type in DOCUMENT_CONTROL_FINDING_TYPES and row.finding_id.startswith("joint-revision-"))]
                checkpoint()
                return
            event.status = "rejected"
            checkpoint()
            # A rejected candidate can be corrected in the same remaining
            # round allowance. Never adopt only the apparently good sections.
            rejected_reviews = verified.section_reviews
            active = next_reviews(rejected_reviews)
        except (LLMError, ValidationError, ValueError) as exc:
            event.status = "drafted" if event.drafts else "unavailable"
            finding = ReviewerFinding(finding_id=f"joint-revision-{event.iteration}-unavailable",
                type="document_revision_unavailable", severity="major",
                message=f"Joint correction was not adopted: {exc}",
                suggested_action="Inspect the preserved candidate and original revision request.")
            all_findings.append(finding)
            memory.reviewer_findings.append(finding)
            checkpoint()
            return
        event = None
    if active:
        finding = ReviewerFinding(finding_id="joint-revision-unresolved", type="document_revision_unresolved",
            severity="major", message="Joint correction allowance exhausted; the adopted original remains unchanged.")
        all_findings.append(finding)
        memory.reviewer_findings.append(finding)
        checkpoint()

# Controller observations are recomputed, not closed as model opinions.
DOCUMENT_CONTROL_FINDING_TYPES = frozenset({
    "document_review_unavailable", "document_revision_unavailable", "document_revision_unresolved",
    "document_length_observation",
})


def rejected_review_context_requests(
    response: Any, *, section_ids: set[str], read_only_tools: set[str], max_requests: int,
) -> list[ReportToolCall]:
    """Extract read requests, never findings/closure, from a rejected answer.

    The carrier must bind unambiguously to current sections. Arguments still go
    through the existing gateway's schema, registered-source and quota checks.
    No unknown target/name is relocated or repaired, and no rejected verdict is
    promoted to an accepted review just because its lookup request is usable.
    """
    rows = response.get("section_reviews") if isinstance(response, Mapping) else None
    if not isinstance(rows, list) or len(rows) > len(section_ids):
        return []
    targets = [row.get("section_id") if isinstance(row, Mapping) else None for row in rows]
    if any(not isinstance(target, str) or target not in section_ids for target in targets):
        return []
    if len(set(targets)) != len(targets):
        return []
    requests: list[ReportToolCall] = []
    allowance = max(0, max_requests)
    for row in rows:
        calls = row.get("context_requests")
        if not isinstance(calls, list):
            continue
        for raw in calls[:allowance - len(requests)]:
            if (not isinstance(raw, Mapping) or not isinstance(raw.get("tool_name"), str)
                    or raw["tool_name"] not in read_only_tools):
                continue
            try:
                call = ReportToolCall.model_validate({**raw, "caller": "document_reviewer"})
            except ValidationError:
                continue
            if call not in requests:
                requests.append(call)
        if len(requests) >= allowance:
            break
    return requests


def coalesce_document_reviews(reviews: list[ReportSectionReview]) -> list[ReportSectionReview]:
    """One unsent correction contract per target, without dropping requirements.

    Inspection and opinion checking can both require work on the same section.
    Keep distinct findings and instructions; do not mutate the saved role records.
    Persisted candidates must be kept outside this merge by their owning caller.
    """
    grouped: dict[str, ReportSectionReview] = {}
    ranks = {"pass": 0, "warning": 1, "revise_required": 2, "fail": 3}
    for review in reviews:
        if review.section_id not in grouped:
            grouped[review.section_id] = review.model_copy(deep=True)
            continue
        merged = grouped[review.section_id]
        if review.revision_scope == "document":
            merged.revision_scope = "document"
        if ranks[review.verdict] > ranks[merged.verdict]:
            merged.verdict = review.verdict
        for name in ("findings", "revision_instructions", "context_requests", "finding_checks"):
            target = getattr(merged, name)
            for item in getattr(review, name):
                if item not in target:
                    target.append(item.model_copy(deep=True) if hasattr(item, "model_copy") else item)
        if review.notes and review.notes != merged.notes:
            merged.notes = "\n".join(part for part in (merged.notes, review.notes) if part)
    return list(grouped.values())


def historical_opinion_handles(
    findings: list[ReviewerFinding],
) -> dict[tuple[str, str], ReviewerFinding]:
    """Request-local handles, without rewriting saved role identities.

    Model IDs are local to a review. Different reviews can reuse them for
    different allegations. Preserve unique IDs, but qualify every member of
    a collision so a raw ambiguous ID cannot close an arbitrary opinion.
    Reconstruct the mapping from the saved request order on recovery.
    """
    counts: dict[tuple[str, str], int] = {}
    for finding in findings:
        key = (finding.section_id, finding.finding_id)
        counts[key] = counts.get(key, 0) + 1
    reserved = set(counts)
    handles: dict[tuple[str, str], ReviewerFinding] = {}
    for index, finding in enumerate(findings, start=1):
        key = (finding.section_id, finding.finding_id)
        if counts[key] > 1:
            handle = f"{finding.finding_id}::opinion-{index}"
            while (finding.section_id, handle) in reserved:
                handle += ":"
            key = (finding.section_id, handle)
            reserved.add(key)
        handles[key] = finding
    return handles


def _opinion_to_check(finding: ReviewerFinding, handle: str) -> dict[str, Any]:
    """Project an allegation, not an imperative to perform its proposed fix.

    The full finding remains with the editor/reviser and in saved history.
    Priority and proposed action belong to editing, not premise checking.
    Old quotes retain their historical scope; validation uses the current view.
    """
    view = finding.model_dump(mode="json", include={
        "finding_id", "section_id", "type", "message", "claim_id", "evidence_handles",
        "draft_quotes", "evidence_quotes",
    }, exclude_defaults=True)
    view["finding_id"] = handle
    if handle != finding.finding_id:
        view["original_finding_id"] = finding.finding_id
    return view


def review_document(
    *, client: Any, template: ReportTemplateBundle, memory: ReportMemory,
    sections: list[ReportSectionDraft], config: ReportRuntimeConfig,
    execution_summary: Mapping[str, Any], metric_summary: Mapping[str, Any],
    source_evidence: list[dict[str, Any]] | None = None,
    execution_evidence: Mapping[str, Any] | None = None,
    supplementary_evidence: list[ReportToolResult] | None = None,
    requested_context: list[ReportToolResult] | None = None,
    historical_findings: list[ReviewerFinding] | None = None,
    complete_candidate: bool = False,
    on_invalid_response: Callable[[Any, str], None] | None = None,
    on_validated_subset: Callable[[list[ReportSectionReview]], None] | None = None,
    format_correction: Mapping[str, Any] | None = None,
    writing_objective: str | None = None,
    assembly_owned_content: list[dict[str, Any]] | None = None,
    delivery_text_observation: Mapping[str, Any] | None = None,
    label: str = "report-document-reviewer",
) -> list[ReportSectionReview]:
    """Review the complete document; the owner separately bounds corrections."""
    drafts = [{"section_id": row.section_id, "heading": row.heading,
               "markdown": row.draft_markdown,
               "markdown_token_count": len(row.draft_markdown.split())} for row in sections]
    additions = assembly_owned_content or []
    references = (delivery_text_observation or {}).get("references") or {}
    if (sum(len(row["markdown"]) for row in [*drafts, *additions])
            + len(references.get("markdown", "")) > MAX_DOCUMENT_REVIEW_CHARS):
        raise ValueError("Whole-document review exceeds its bounded source window; no complete review was performed.")
    known = {row.section_id for row in sections}
    # Inspect current prose independently. Prior opinions are supplied only to
    # the separate check, never as extra source facts or an inspection answer.
    prior = historical_findings or []
    joint_revision = config.draft_scope in {"document", "auto"}
    revision_capacity = len(memory.section_plan) if joint_revision else document_revision_limit(memory)
    correction_allowance = ("joint candidate rounds" if joint_revision else "per-section rounds")
    primary_sources = review_source_evidence(source_evidence or [])
    prior_by_key = historical_opinion_handles(prior)
    if any(row.section_id not in known or row.type in DOCUMENT_CONTROL_FINDING_TYPES for row in prior):
        raise ValueError("Historical finding checks require known model opinions, not controller failures.")
    # Earlier roles' summaries are not additional primary evidence for this
    # inspection. Retain original windows and observations; derived text is
    # available when this reviewer explicitly requests it. Nothing is erased
    # from the persisted tool history or from Writer context.
    history = supplementary_evidence or []
    visible_context = [row for row in history if row.tool_name != "get_synthesis_brief"]
    for row in requested_context or []:
        if row not in visible_context:
            visible_context.append(row)
    view = {
        "task": "check_historical_findings" if prior else "review_document_coherence",
        "objective": memory.objective if writing_objective is None else writing_objective,
        "document_title": memory.document_plan.title if memory.document_plan else "",
        # Share the meaning of planning, not its proposed conclusions. An
        # independent reviewer judges current prose against task and sources.
        "document_plan": document_plan_context(memory, independent_review=True),
        "document_length_budget": memory.document_plan.length_budget if memory.document_plan else {},
        "edit_scope": report_edit_scope([row["section_id"] for row in drafts]),
        "report_mode": memory.report_mode,
        "template": template.name,
        "criteria": reviewing_template_guidance(template, memory, config),
        "authored_template_requirements": template.template_markdown if not is_builtin_template(template, config) else "",
        "sections": drafts,
        "assembly_owned_content": additions,
        "delivery_text_observation": dict(delivery_text_observation or {}),
        "length_observation": {
            "counting_rule": "whitespace-separated tokens in section Markdown plus known assembly-owned additions, including tables and captions; title/headings/references and other rendering additions are not included; not a language-independent word-limit verifier",
            "total_markdown_tokens": sum(row["markdown_token_count"] for row in drafts),
            "assembly_owned_markdown_tokens": sum(row["markdown_token_count"] for row in additions),
            "known_delivery_markdown_tokens": sum(row["markdown_token_count"] for row in [*drafts, *additions]),
            "assembly_preview_complete": all(row.get("preview_status") != "unavailable" for row in additions),
            "planned_total_words": sum(row.target_words for row in memory.section_plan),
        },
        "verified_execution_results": execution_summary,
        "execution_evidence": dict(execution_evidence or {}),
        "metric_sources": metric_summary,
        "source_evidence": primary_sources,
        "supplementary_evidence": [report_tool_context(row, source_evidence=primary_sources,
                                    include_reading_notes=row in (requested_context or []))
                                   for row in visible_context[-8:]],
        "supplementary_evidence_omitted": max(0, len(visible_context) - 8),
        "historical_derived_contexts_on_request": sum(row.tool_name == "get_synthesis_brief" for row in history),
        "context_tools": [{"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
                          for spec in report_tool_specs()] if config.allow_source_backtracking else [],
        "context_request_limit": config.max_backtracking_calls if config.allow_source_backtracking else 0,
        "known_limitations": memory.limitations[:12],
        "historical_findings_to_check": [_opinion_to_check(row, key[1]) for key, row in prior_by_key.items()],
        "historical_findings_role": "prior_model_opinions_not_source_facts",
        "response_format_correction": dict(format_correction or {}),
        "correction_role": "rejected_model_answer_not_evidence",
        "focus": [
            *DELIVERY_RULES,
            *CLAIM_SCOPE_RULES,
            *([] if prior else REVIEW_ACTION_RULES),
            *EVIDENCE_QUOTE_RULES,
            "If response_format_correction is supplied, re-evaluate the rejected answer against this schema, the validation error and the current supplied evidence. Preserve requested opinion identities, not a rejected factual conclusion. Its quotation, evidence role or judgement may be wrong. Change status only when supported by current evidence; neither unsupported closure nor preserving an unsupported judgement is a valid correction. The rejected answer is not source evidence or permission to rewrite the report.",
            "Inspect the current supplied drafts and original evidence, not remembered or superseded prose. A historical reviewer statement is an opinion to test, not independent evidence. No omitted historical finding is automatically resolved.",
            "The original request governs length. document_length_budget is a planner interpretation and known assembly forecast, not semantic verification or proof the final report fits. A constraint=target or length_check.status=target_only is a soft approximate target, not a hard interval; do not invent tolerance bounds or reject solely for deviation. Still review whether the content and extent satisfy the actual task. Section shares are approximate rather than minimums; unresolved references/visual text require checking the completed canonical delivery. Do not discard required evidence to fit a quota.",
            "When historical_findings_to_check is supplied, return a finding_check for each checked opinion under its supplied target section only. Use resolved when the current draft demonstrably fixes it; not_applicable when its premise is unsupported or no longer applies and the current draft remains appropriately bounded; otherwise unresolved. Give a specific explanation and exact nonempty quotations from the current draft for any closure. Each quotation identifies its own current section_id, which can differ from the opinion target for a cross-section issue; do not move or duplicate the opinion itself to other targets. Missing evidence is not proof that a claim or source does not exist; do not require unverified facts as a replacement for a qualified statement.",
            *([] if prior else [
            "Find contradictions between sections about the same method, setting, result or conclusion.",
            "Find substantial repetition of protocol, metrics or limitations across sections; assign each fact a clear home.",
            *( ["Set revision_scope=document only when an actionable revise finding requires coordinated edits across the article (for example overall length or distributed repetition). Keep the finding under one owning section; its quote anchors the observation, not the entire edit scope. Use section for local corrections, advisory observations and evidence-only verification. Document scope permits keeping unaffected prose unchanged; it never authorizes changing sources, measurements, the frozen plan or assembly-owned content."] if joint_revision else []),
            "Compare observed length with the requested document length and genre. Treat a substantial excess or shortfall as an actionable delivery defect, not optional polish; do not remove required facts or add unrelated material to meet length.",
            "Check that abstract and conclusion do not claim more than results, and that paper versus analysis-report tone matches the evidence.",
            ]),
            "Do not request new experiments or rewrite measurements. Missing evidence for a consequential current assertion remains a gap; an old opinion's allegation that evidence is missing can itself be unsupported. Do not turn an alleged gap into an established current defect without checking its premise.",
            "Separate declared protocol, executor observations and method verification. Invocation does not prove algorithmic details; elapsed duration is not the configured timeout.",
            "Distinguish parsed source passages from model reading notes and abstract-only access. Do not deny a reported result merely because it is absent from an abstract; check the supplied passages. Missing passages are not proof that the paper omits the result.",
            "Cold document checking omits earlier model reading cards but retains their locator and original passage window. A get_paper_brief request can retrieve the recorded card; its notes remain derived interpretations, not primary support. Request original source chunks when the passage window is insufficient, and retain the gap when tools are unavailable.",
            "Recorded bibliography is not independently verified identity or edition information. Check important attribution against original source passages; retain conflicting dates/identifiers and author-list coverage limits instead of inventing metadata.",
            "Draft citation keys are provisional, not the delivered citation style. When delivery_text_observation.references is available, resolve its model_keys to paper ids and citation_numbers before alleging a mismatch with numeric References. Assembly owns this conversion; check incorrect attribution or missing mapping, not the mere difference between raw draft keys and displayed numbers.",
            *([] if prior else [f"Return at most one review per supplied section. Retain all consequential findings; do not pad or omit defects to fit the allowance. The editor can modify up to {revision_capacity} frozen target sections within its existing {correction_allowance}, prioritizing required corrections; unresolved findings remain visible."]),
        ],
        "output_schema": {"section_reviews": [{
            "section_id": "one of the supplied section ids",
            "revision_scope": "section|document; document only for an actionable whole-article correction" if joint_revision else "section",
            "verdict": "pass|warning|revise_required|fail",
            "findings": [{"finding_id": "stable id", "type": "style|unsupported_claim|metric_mismatch|citation_misuse|missing_limitation|evidence_gap",
                          "severity": "info|minor|major|critical", "message": "specific cross-section issue",
                          "required_action": "advisory|revise|verify",
                          "section_id": "same target section", "suggested_action": "bounded correction",
                          "draft_quotes": [{"section_id": "current section containing the statement", "quote": "exact current draft text"}],
                          "evidence_quotes": [EVIDENCE_QUOTE_SCHEMA]}],
            "revision_instructions": ["specific change to this section without changing measured facts"],
            "finding_checks": [{"finding_id": "one supplied historical finding id; empty list during independent inspection",
                                "status": "resolved|not_applicable|unresolved", "explanation": "reason based on current draft and supplied evidence",
                                "draft_quotes": [{"section_id": "supplied current section containing the quotation",
                                                  "quote": "exact nonempty current draft quotation supporting this judgement"}],
                                "evidence_quotes": [EVIDENCE_QUOTE_SCHEMA]}],
            "context_requests": [{"tool_name": "get_paper_brief|get_metric_source|get_code_task_result|get_neighbor_chunks|search_source_chunks|get_synthesis_brief",
                                  "arguments": {}, "caller": "document_reviewer"}],
        }]},
    }
    if complete_candidate:
        view["focus"].append(
            "This is independent inspection of a revised candidate. Review every supplied section exactly once. "
            "Judge the current prose against the task and original evidence, reporting remaining or newly "
            "introduced problems as concrete anchored findings. Do not return historical finding_checks."
        )
        view["output_schema"]["section_reviews"][0]["notes"] = "Current-section assessment against the task and evidence."
    if delivery_text_observation is not None:
        # One count scope for native callers: do not expose the partial
        # section+attachment sum as a competing complete delivery length.
        view["length_observation"]["known_delivery_markdown_tokens"] = delivery_text_observation.get("markdown_token_count")
        view["length_observation"]["counting_rule"] = delivery_text_observation.get("counting_rule", "unavailable")
        view["length_observation"]["delivery_count_scope"] = delivery_text_observation.get("preview_status", "unavailable")
        view["length_observation"]["assembly_preview_complete"] = delivery_text_observation.get("preview_status") == "pre_render_text_preview"
    if prior:
        # This role tests recorded allegations, not the old drafting directions.
        # Keep the original objective, full current prose and evidence intact;
        # independent inspection still receives its factual/style criteria.
        view.pop("criteria")
        # The projected opinions already own the exact targets and identities.
        # Describe the response shape once, not a copy of its quotation/tool
        # schema per opinion. Validation still binds every returned check to
        # prior_by_key; this changes neither targets nor closure requirements.
        example = view["output_schema"]["section_reviews"][0]
        view["output_schema"] = {"section_reviews": [{
            "section_id": "a section_id from historical_findings_to_check; group only opinions at this target",
            "verdict": example["verdict"],
            "finding_checks": example["finding_checks"],
            "context_requests": example["context_requests"],
        }]}
        view["focus"].append(
            "Only check the supplied historical opinions at their exact targets. "
            "Use each supplied finding_id exactly as this request's check handle; "
            "original_finding_id is historical metadata, not a check target. "
            "Do not produce new findings, revision instructions, or reviews of other targets. "
            "Independent inspection already owns discovery; this role records each opinion's "
            "status and reason, or requests evidence. Cross-section quotes do not create another target."
        )
        view["focus"].append(
            "Test each opinion's premise against the current draft and correctly attributed evidence before judging its status. "
            "Prior draft/evidence quotes may refer to an older window; locate any current support in this request again. "
            "The editor retains priorities and proposed remedies outside this check. Do not follow a proposed "
            "replacement merely because it appeared in an earlier opinion, and do not call absence proof of contradiction."
        )
    else:
        view["output_schema"]["section_reviews"][0]["finding_checks"] = []
    base_prompt = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    prompt_limit = config.max_document_review_prompt_chars
    if prompt_limit and len(base_prompt) > prompt_limit:
        raise ValueError("Whole-document review exceeds its bounded evidence window; no complete review was performed.")
    remaining = prompt_limit - len(base_prompt) - len(',"evidence_locator":') if prompt_limit else 4000
    try:
        locator = review_evidence_locator(view, max_chars=min(4000, max(0, remaining)))
    except ValueError:
        locator = {"coverage": "not_listed", "scope": "preserve_current_evidence_window_not_missing_sources"}
    if len(json.dumps(locator, ensure_ascii=False, separators=(",", ":"))) <= remaining:
        view["evidence_locator"] = locator
    prompt = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    if prompt_limit and len(prompt) > prompt_limit:
        raise ValueError("Whole-document review exceeds its bounded evidence window; no complete review was performed.")
    response = client.ask_json(
        ("Check only the supplied historical opinions against current drafts and evidence. "
         "Return only the requested JSON object." if prior else
         "Review the complete report for cross-section coherence. Return only the requested JSON object."),
        prompt, label=label,
        max_output_tokens=config.max_section_tokens if config.max_section_tokens > 0 else None,
    )
    try:
        reviews = _validate_document_reviews(response, sections=sections, prior_by_key=prior_by_key, evidence_view=view)
        if complete_candidate and {row.section_id for row in reviews} != known:
            raise LLMResponseError("Candidate inspection must review every supplied current section; no complete verification was performed.")
        if prior_by_key and not any(row.context_requests for row in reviews):
            checked = {(row.section_id, check.finding_id) for row in reviews for check in row.finding_checks}
            missing = set(prior_by_key) - checked
            if missing:
                raise LLMResponseError(
                    f"Historical opinion checking omitted requested decisions: {sorted(missing)!r}; "
                    "return every requested check, using unresolved when it cannot be decided."
                )
        # A complete canonical count is already available. Do not delegate its
        # range comparison to a model or ask a second model to close it. The
        # normal candidate verifier will recompute it after editing.
        observed = delivery_text_observation or {}
        length = observed.get("length_check") or {}
        if (not prior and sections and joint_revision
                and observed.get("preview_status") == "pre_render_text_preview"
                and not observed.get("pending_owner_sections")
                and length.get("status") in {"above_range", "below_range"}):
            owner = sections[0].section_id
            message = (f"Canonical delivery contains {length['markdown_token_count']} whitespace-separated tokens; "
                       f"the frozen {length['scope']} range is {length['min_words']}–{length['max_words']}. "
                       "This checks the recorded counting convention, not scientific adequacy.")
            reviews = coalesce_document_reviews([*reviews, ReportSectionReview(
                section_id=owner, revision_scope="document", verdict="revise_required",
                findings=[ReviewerFinding(finding_id="canonical-delivery-length", section_id=owner,
                    type="document_length_observation", severity="minor", required_action="revise",
                    message=message, suggested_action="Adjust article extent across sections while preserving required evidence and read-only assembly content.")],
                revision_instructions=[message])])
        return reviews
    except (LLMResponseError, ValidationError) as exc:
        # Retain the parsed answer, not just its error, before the owner stops.
        # Transport/JSON decoding failures have no parsed review to preserve.
        if on_validated_subset is not None:
            on_validated_subset(_validated_document_review_subset(response, sections=sections,
                prior_by_key=prior_by_key, evidence_view=view))
        if on_invalid_response is not None:
            on_invalid_response(response, str(exc))
        raise


def _document_review_envelope(
    response: Any, *, sections: list[ReportSectionDraft],
    prior_by_key: Mapping[tuple[str, str], ReviewerFinding],
) -> list[dict[str, Any]]:
    """Bind the entire response before validating or salvaging observations.

    Identity collisions and role violations reject the whole batch. Only
    evidence errors may be isolated by the existing correction owner. Return
    fresh rows so neither path changes the retained rejected model response.
    """
    known = {row.section_id for row in sections}
    if not isinstance(response, Mapping) or not isinstance(response.get("section_reviews"), list):
        raise LLMResponseError("Whole-document reviewer did not return section_reviews.")
    raw_reviews = response["section_reviews"]
    if prior_by_key and len(raw_reviews) > len(known):
        raise LLMResponseError("Whole-document reviewer returned more reviews than supplied sections.")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    checked: set[tuple[str, str]] = set()
    finding_keys: set[tuple[str, str]] = set()
    opinion_targets = {section_id for section_id, _ in prior_by_key}
    for raw in raw_reviews:
        if (not isinstance(raw, Mapping) or not isinstance(raw.get("section_id"), str)
                or raw["section_id"] not in known):
            raise LLMResponseError("Whole-document reviewer targeted an unknown section.")
        raw_findings = raw.get("findings") or []
        if not isinstance(raw_findings, list):
            raise LLMResponseError("Whole-document findings must be a list.")
        normalized = dict(raw)
        normalized["findings"] = [
            {**finding, "section_id": finding.get("section_id") or raw["section_id"]}
            if isinstance(finding, Mapping) else finding
            for finding in raw_findings
        ]
        checks = raw.get("finding_checks", [])
        if not isinstance(checks, list):
            raise LLMResponseError("Whole-document finding checks must be a list.")
        target = raw["section_id"]
        shell = ReportSectionReview.model_validate({**normalized, "findings": [], "finding_checks": []})
        if prior_by_key and (raw_findings or shell.revision_instructions):
            raise LLMResponseError("Historical opinion checking cannot add new findings or rewrite instructions; independent inspection owns discovery.")
        if prior_by_key and target in seen:
            raise LLMResponseError("Whole-document reviewer targeted an unknown or repeated section.")
        for finding in normalized["findings"]:
            if not isinstance(finding, Mapping) or finding.get("section_id") != target:
                raise LLMResponseError("Whole-document finding must identify its target section.")
            identifier = finding.get("finding_id")
            if not isinstance(identifier, str):
                raise LLMResponseError("Whole-document finding must identify its opinion.")
            key = (target, identifier)
            if key in finding_keys:
                raise LLMResponseError(f"Whole-document reviewer repeated a finding identity: {key}.")
            finding_keys.add(key)
        for check in checks:
            if not isinstance(check, Mapping) or not isinstance(check.get("finding_id"), str):
                raise LLMResponseError("Historical finding check must identify its opinion.")
            key = (target, check["finding_id"])
            if key not in prior_by_key or key in checked:
                raise LLMResponseError(f"Historical finding check targeted an unknown or repeated opinion: {key}.")
            checked.add(key)
        if opinion_targets and target not in opinion_targets:
            raise LLMResponseError("Historical finding check returned an unrequested target section.")
        seen.add(target)
        rows.append(normalized)
    return rows


def _validate_document_reviews(
    response: Any, *, sections: list[ReportSectionDraft],
    prior_by_key: Mapping[tuple[str, str], ReviewerFinding],
    evidence_view: Mapping[str, Any] | None = None,
) -> list[ReportSectionReview]:
    rows = _document_review_envelope(response, sections=sections, prior_by_key=prior_by_key)
    draft_by_id = review_draft_quote_sources(evidence_view) if evidence_view is not None else {
        row.section_id: (row.heading, row.draft_markdown) for row in sections}
    reviews = [_document_review_record(raw, draft_by_id=draft_by_id, evidence_view=evidence_view or {})
               for raw in rows]
    # Inspection observations can legitimately share a target. Validate every
    # record before merging; a later pass must not erase an earlier defect.
    # Historical closure keeps its one-target/one-opinion identity contract.
    return reviews if prior_by_key else coalesce_document_reviews(reviews)


def _document_review_record(raw: Mapping[str, Any], *,
    draft_by_id: Mapping[str, str | tuple[str, ...]], evidence_view: Mapping[str, Any],
) -> ReportSectionReview:
    """Validate one observation after the complete batch has been bound."""
    review = ReportSectionReview.model_validate(resolve_review_evidence(raw, evidence_view))
    if (review.verdict in {"revise_required", "fail"} and not review.findings
            and not any(check.status == "unresolved" for check in review.finding_checks)):
        raise LLMResponseError("Whole-document revision needs a concrete finding.")
    validate_finding_anchors(review, draft_by_id, evidence_view)
    for check in review.finding_checks:
        key = (review.section_id, check.finding_id)
        if not check.explanation.strip():
            raise LLMResponseError("Historical finding check needs a reason.")
        for citation in check.draft_quotes:
            quote_section = citation.section_id if isinstance(citation, ReportDraftQuote) else review.section_id
            quote = (citation.quote if isinstance(citation, ReportDraftQuote) else citation).strip()
            if not draft_quote_present(draft_by_id, quote_section, quote):
                raise LLMResponseError(f"Historical finding check quoted text absent from the current section: {quote_section!r}, opinion {key}.")
        if check.status != "unresolved" and not check.draft_quotes:
            raise LLMResponseError("Closing a historical finding requires a current-draft quotation.")
        validate_evidence_quotes(check.evidence_quotes, evidence_view)
    return review


def _validated_document_review_subset(
    response: Any, *, sections: list[ReportSectionDraft],
    prior_by_key: Mapping[tuple[str, str], ReviewerFinding], evidence_view: Mapping[str, Any],
) -> list[ReportSectionReview]:
    """Validate local observations only after the whole identity/role envelope.

    This does not make a rejected response successful. The owner keeps that
    failure and its raw response, and may use this subset only within the same
    correction allowance. Record checks are identical to complete validation.
    """
    try:
        rows = _document_review_envelope(response, sections=sections, prior_by_key=prior_by_key)
    except (LLMResponseError, ValidationError):
        return []
    # Retain the stricter salvage contract: even instructions that the legacy
    # schema coerces to empty cannot be released from a rejected opinion check.
    if prior_by_key and any(raw.get("revision_instructions") for raw in rows):
        return []

    draft_by_id = review_draft_quote_sources(evidence_view)
    result: list[ReportSectionReview] = []
    for raw in rows:
        try:
            result.append(_document_review_record(raw, draft_by_id=draft_by_id, evidence_view=evidence_view))
            continue
        except (LLMResponseError, ValidationError):
            pass
        # Do not execute unbound instructions from a mixed invalid section.
        shell = {**raw, "findings": [], "finding_checks": [], "revision_instructions": [],
                 "verdict": "warning", "notes": "Partially validated rejected review; full inspection remains incomplete."}
        accepted = ReportSectionReview.model_validate(shell)
        for field in ("findings", "finding_checks"):
            for item in raw.get(field) or []:
                single = {**shell, field: [item]}
                try:
                    validated = _document_review_record(single, draft_by_id=draft_by_id, evidence_view=evidence_view)
                except (LLMResponseError, ValidationError):
                    continue
                getattr(accepted, field).extend(getattr(validated, field))
        if accepted.findings or accepted.finding_checks:
            # The parent's verdict may depend on the rejected observation.
            # Historical unresolved opinions receive their action from the
            # retained original contract, not from this parent verdict.
            if any(finding_requires_resolution(f) and f.required_action != "verify" for f in accepted.findings):
                accepted.verdict = "revise_required"
            result.append(accepted)
    return result if prior_by_key else coalesce_document_reviews(result)
