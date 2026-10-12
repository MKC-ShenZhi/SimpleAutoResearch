"""Bounded writing context projected from adopted drafts, not a second memory.

The controller already saves complete sections in its checkpoint. Rebuild this
view from those sections on every call, including recovery; a rejected candidate
or stale claim record must not become evidence for the next section.
"""

from collections.abc import Mapping, Sequence
import json
from typing import Any
import re

from simple_ar.research.contracts import CLAIM_SCOPE_RULES, COMPARISON_SCOPE_RULE
from simple_ar.report.execution_evidence import report_execution_evidence
from simple_ar.report.document_plan import ARGUMENT_PLAN_SCHEMA, ARGUMENT_PLANNING_RULES, LENGTH_REQUEST_RULE, LENGTH_REQUEST_SCHEMA, check_document_length, document_tokens, manuscript_body_tokens, reserve_document_words, supplied_figure_sources, validate_length_request
from simple_ar.report.data_delivery import DELIVERY_RULES, attach_delivery_block, supplied_data_delivery

from simple_ar.report.schema import (
    ClaimEvidenceRecord, ReportContext, ReportDocumentPlan, ReportIterationRecord, ReportMemory, ReportRuntimeConfig, ReportSectionDraft,
    ReportSectionPlan, ReportSectionReview,
    ReportTemplateBundle, ReportToolResult, SourceHandle, finding_requires_resolution,
)
from simple_ar.report.templates import BUILTIN_TEMPLATE_NAMES, planning_template_guidance


DERIVED_CONTEXT_STATUS = {
    "evidence_role": "recorded_derived_context",
    "independent_verification": "not_performed",
}

PLANNING_CONTEXT_STATUS = {
    **DERIVED_CONTEXT_STATUS,
    "scope": "organizational_intent_not_current_prose_or_scientific_support",
}

REVIEW_OPINIONS_STATUS = {
    **DERIVED_CONTEXT_STATUS,
    "scope": "fallible_allegations_and_proposed_remedies_not_source_facts",
}

REVISION_PREMISE_RULE = (
    "Test an alleged defect against the actual baseline prose and source evidence before following its proposed remedy. "
    "An opinion may describe absent or superseded prose; do not manufacture that assertion in the candidate to satisfy the opinion. "
    "A candidate must solve a supported problem, not merely repeat a proposed replacement. "
    "Preserve any uncertainty when the baseline or evidence is incomplete."
)


def _experiment_delivery_view(context: ReportContext, config: ReportRuntimeConfig) -> list[dict[str, Any]]:
    """Expose the same assembly-owned experiment text, not another draft."""
    from simple_ar.report.projection import _append_verified_experiment_evidence
    return [{"section_id": row.section_id, "heading": row.heading,
             "markdown": row.draft_markdown, "markdown_token_count": len(row.draft_markdown.split()),
             "preview_status": "recorded_package_preview"}
            for row in _append_verified_experiment_evidence((), context, config)]


def report_edit_scope(section_ids: Sequence[str]) -> dict[str, Any]:
    """Describe the actual existing Writer/assembly boundary, not new powers."""
    return {
        "eligible_section_ids": list(section_ids),
        "writer_call_unit": "One selected section's model-authored body, display heading and supported metadata only",
        "read_only_components": ["other adopted section bodies in this Writer call", "frozen document title and plan",
            "registered source data, measurements and execution records", "assembly-owned references, citation numbering, captions, tables, links and appendix"],
        "unavailable_remedy": "An assembly/input defect stays unresolved for its owner; a section Writer cannot fix it by claiming to change or omit protected text. Do not demand that remedy as this candidate's acceptance condition.",
    }


def effective_revision_instructions(review: ReportSectionReview | None) -> list[str]:
    """Same actionable requirements in normal drafting, recovery and checking.

    Retain explicit review instructions. A required finding's proposed change
    supplements them; an advisory or verification-only opinion is not promoted
    into a rewrite instruction. Its message remains visible as an opinion.
    """
    if review is None:
        return []
    return list(dict.fromkeys([*review.revision_instructions,
        *(row.suggested_action for row in review.findings
          if row.required_action not in {"advisory", "verify"}
          and (finding_requires_resolution(row) or (row.required_action is None and review.verdict in {"revise_required", "fail"}))
          and row.suggested_action.strip())]))


def report_objective(context: ReportContext, memory: ReportMemory) -> str:
    """Use the saved task request, not the short memory summary, as the contract.

    Keep distinct problem/goal inputs intact. Exact duplicates need only one
    copy. Old snapshots without either input retain their recorded objective;
    completed plans and drafts are not changed by this read-only projection.
    """
    requests = list(dict.fromkeys(text.strip() for text in (
        context.problem_markdown, context.goal_markdown,
    ) if text.strip()))
    return "\n\n".join(requests) or memory.objective or context.topic


def prior_draft_context(context: ReportContext) -> dict[str, Any]:
    """Expose selected old prose for editing, never as an evidence choice."""
    return {
        "drafts": [{**_prompt_handle_view(row),
            **({'retained_body': row.metadata['draft_markdown'],
                'body_scope': 'Complete retained earlier Markdown; not original-source evidence.'}
               if isinstance(row.metadata.get('draft_markdown'), str) else {})}
            for row in context.source_handles if row.kind == "prior_draft"],
        "use": "These are earlier drafts selected for revision or reorganization, not primary evidence. "
               "Use them to identify what to preserve or change. Resolve factual claims from the actual sources "
               "and recorded results; new supported evidence can supersede old prose. Do not cite a draft "
               "as proof or keep both an obsolete statement and its correction. Retained read tools can inspect its full text.",
    }


def report_tool_context(
    result: ReportToolResult, *, source_evidence: list[dict[str, Any]] | None = None,
    include_reading_notes: bool = True, evidence_reference: str = "source_evidence",
) -> dict[str, Any]:
    """Project saved brief handles too; do not rewrite historical tool records.

    Only typed handle lists are projected. Source windows, lookup diagnostics,
    text coverage and all other tool content keep their original semantics.
    """
    data = result.model_dump(mode="json")
    if result.tool_name == "get_synthesis_brief":
        # Saved summaries are not original source text. Carry that distinction
        # on legacy results too, without rewriting their recorded assertions.
        data["content"]["text_status"] = dict(DERIVED_CONTEXT_STATUS)
    for key in ("handles", "matching_handles"):
        rows = data["content"].get(key)
        if not isinstance(rows, list):
            continue
        projected = []
        for row in rows:
            if not isinstance(row, dict):
                projected.append(row)
                continue
            fields = {key: value for key, value in row.items() if key in SourceHandle.model_fields}
            handle = SourceHandle.model_validate(fields)
            # Extra protocol fields such as cite_as survive old snapshots.
            view = {**{key: value for key, value in row.items() if key not in SourceHandle.model_fields},
                    **_prompt_handle_view(handle)}
            if not include_reading_notes:
                view = review_source_evidence([view])[0]
            if source_evidence is not None and view in source_evidence:
                # Exact duplicate only: a newer/different passage or conflicting
                # metadata must not be replaced merely because its id matches.
                view = {"handle": view["handle"], "evidence_reference": evidence_reference}
            projected.append(view)
        data["content"][key] = projected
    return data


def review_source_evidence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep recorded passages, not earlier reading cards, in independent review.

    The source handle/store and the Writer's view are unchanged. Explicit source
    requests can still retrieve the card, labelled as derived in review rules.
    This projection does not select passages or certify their interpretation.
    """
    projected = []
    for row in rows:
        metadata = row.get("metadata")
        notes = metadata.get("reading_notes") if isinstance(metadata, Mapping) else None
        if not isinstance(notes, Mapping):
            projected.append(row)
            continue
        primary = {key: value for key, value in metadata.items() if key != "reading_notes"}
        primary["derived_reading_notes_on_request"] = {
            **DERIVED_CONTEXT_STATUS, "recorded_fields": list(notes),
            "reading_artifact": metadata.get("reading_artifact", ""),
            "omitted_from_this_view": True,
        }
        coverage = notes.get("reading_coverage")
        if isinstance(coverage, Mapping):
            # Preserve the recorded access limits, not the card's conclusions.
            # This is still reader-reported coverage, not source verification.
            primary["derived_reading_notes_on_request"]["reading_coverage"] = dict(coverage)
        projected.append({**row, "metadata": primary})
    return projected


def map_evidence_labels(value: Any, replacements: Mapping[str, str], *, field: str = "") -> Any:
    """Translate exact structured source references, never quotes or prose.

    Request-local labels are an interface projection; persisted source identity
    and the existing validators remain authoritative. Unknown labels survive so
    validation can reject them rather than guessing a source.
    """
    if isinstance(value, Mapping):
        return {key: map_evidence_labels(item, replacements, field=key) for key, item in value.items()}
    if isinstance(value, list):
        return [map_evidence_labels(item, replacements, field=field) for item in value]
    if isinstance(value, str) and field in {
        "handle", "source_handle", "source_handles", "evidence_handles",
        "counterevidence_handles", "evidence_handle_choices",
    }:
        return replacements.get(value, value)
    return value


def outline_evidence_labels(payload: dict[str, Any], handles: Sequence[SourceHandle]) -> tuple[dict[str, Any], dict[str, str]]:
    """Use short unambiguous labels within one outline request only."""
    originals = list(dict.fromkeys(row.handle for row in handles))
    aliases: dict[str, str] = {}
    index = 1
    for handle in originals:
        while f"source_{index}" in originals:
            index += 1
        aliases[handle] = f"source_{index}"
        index += 1
    projected = map_evidence_labels(payload, aliases)
    projected["planning_rules"] = [*projected["planning_rules"],
        "Use the short source_N labels shown in evidence_handle_choices for structured source references, including context_requests arguments.handle. Labels identify sources in this request only; do not infer or construct internal source identifiers. They do not replace citation keys, document IDs, chunk IDs or metric IDs."]
    return projected, {label: handle for handle, label in aliases.items()}


def evidence_outline_context(
    context: ReportContext, memory: ReportMemory, config: ReportRuntimeConfig, *, retry: bool = False, retry_error: str = "",
    rejected_response: Mapping[str, Any] | None = None,
    template: ReportTemplateBundle | None = None,
) -> dict[str, Any]:
    """Project existing inputs for organization without declaring new facts.

    The persisted context/plan remains authoritative. Every shortened input
    reports its coverage; omitted content is not treated as absent evidence.
    """
    def excerpt(value: str, limit: int) -> dict[str, Any]:
        return {"text": value[:limit], "total_characters": len(value), "truncated": len(value) > limit}

    metrics = _prompt_metrics(memory, detail="summary")
    objective = report_objective(context, memory)
    metric_rows = metrics["rows"]
    handles = [row for row in memory.source_handles if row.kind != "prior_draft"]
    claims = memory.claims_evidence_matrix
    adaptive_builtin = (template is not None and template.name in BUILTIN_TEMPLATE_NAMES
        and config.template in {"", "auto", *BUILTIN_TEMPLATE_NAMES}
        and config.outline_strategy != "template")
    payload = {
        "task": "plan_evidence_organized_document", "topic": context.topic,
        "report_mode": context.report_mode, "template": memory.template,
        # Requirements are not disposable evidence excerpts. A later clause
        # must reach the planner as well as the writer and verifier.
        "objective": excerpt(objective, len(objective)),
        "synthesis": excerpt(context.synthesis_markdown, 3000),
        "synthesis_status": dict(DERIVED_CONTEXT_STATUS),
        "evidence_summary": excerpt(context.evidence_summary, 3000),
        "source_comparisons": context.source_comparisons,
        "comparison_status": dict(DERIVED_CONTEXT_STATUS),
        "template_responsibilities": [
            {"heading": section.heading, "goal": section.goal, "evidence_handles": section.evidence_handles,
             "target_words": section.target_words} for section in memory.section_plan if not adaptive_builtin
        ],
        "sources": [_prompt_handle_view(handle) for handle in handles[:40]],
        "previous_document": prior_draft_context(context),
        "sources_omitted": max(0, len(handles) - 40),
        "evidence_handle_choices": [handle.handle for handle in handles[:40]],
        "supplied_figures": supplied_figure_sources(context),
        "assembly_owned_content": supplied_data_delivery(context, config=config,
            plan=memory.document_plan, section_ids=[row.section_id for row in memory.section_plan])
            + _experiment_delivery_view(context, config),
        "input_claims": [{"claim_id": claim.claim_id, "claim": excerpt(claim.claim, 600),
                          "status": claim.status, "evidence_handles": claim.evidence_handles,
                          "metric_ids": claim.metric_ids, "notes": excerpt(claim.notes, 600)} for claim in claims[:24]],
        "claims_omitted": max(0, len(claims) - 24),
        "recorded_metrics": {"columns": metrics["columns"], "rows": metric_rows[:48],
                             "rows_omitted": max(0, len(metric_rows) - 48)},
        "results": _compact_execution_results(context.results),
        "execution_evidence": report_execution_evidence(context),
        "limits": [*memory.limitations, *memory.open_questions][:16],
        "limits_omitted": max(0, len(memory.limitations) + len(memory.open_questions) - 16),
        "delivery_constraints": {"max_cited_sources": config.max_cited_sources or None,
                                 "max_section_sources": config.max_section_sources or None},
        "planning_rules": [
            *ARGUMENT_PLANNING_RULES,
            *DELIVERY_RULES,
            "Copy evidence_handles from evidence_handle_choices (the sources' top-level handle). Passage chunk_id/document_id and prose citation keys locate evidence inside a source; they are not replacement source handles. Do not construct new identifiers by concatenating them.",
            "Use the requested genre and actual evidence to define concise sections with distinct responsibilities. Template headings are starting points, not compulsory new claims.",
            "Distinguish a research paper draft, reproduction report, analysis report and supplied-material account; do not invent novelty, theorems, experiments, baselines or ablations to resemble a reference paper.",
            "Name the question, what the inputs establish, how comparisons were made, findings and limitations. Put detailed numeric comparisons in one responsible section; others interpret rather than repeat them.",
            "Source summaries and input claims are recorded assertions, not independent verification. A source handle or completed invocation is not proof of a method claim.",
            "No new experiment or external source retrieval is authorized. Registered read-only tools, when offered, can inspect retained inputs. Unknowns and omitted material remain unknown; scope results to recorded conditions, and distinguish reported paper values from local observations.",
            "Keep goals and headings reader-facing, not pipeline steps. Give each section only supplied evidence handles; no minimum citations or word quota beyond the user's existing configuration.",
            "If the request specifies an overall length, allocate target_words across sections within that total, not the same total to every section. Use fewer purposeful sections for short reports. Do not pad to template section lengths or repeat scope disclaimers to fill space.",
            LENGTH_REQUEST_RULE,
            "Propose a concise reader-facing title describing the actual scope, not a copy of task instructions or a stronger claim than the evidence. Give scope and validity details one primary section; other sections use brief qualifications without repeating the full disclaimer.",
            "For each supplied_figures package, use its document_id to read the figure inventory in results.supplied_analyses. Choose inline figures by their recorded captions and encoding, rather than displaying every chart. Assign at most one owner using visual_intents kind=figure, view=supplied-data and one exact registered handle present in that section's evidence. Use figure_paths with exact package-relative paths from that inventory; [] links the complete package without inline charts. All unselected data and editable figures remain in the linked package. Do not fabricate charts, paths or new measurements. Other sections can cite the same source without attaching it again.",
            "Return 2-12 sections as needed. References are appended separately. Do not return a References section.",
        ],
        "output_schema": {"title": "Concise evidence-scoped title",
                          "argument_plan": ARGUMENT_PLAN_SCHEMA,
                          "length_request": dict(LENGTH_REQUEST_SCHEMA),
                          "sections": [{"section_key": "Distinct short literal key reused by arguments and visuals", "heading": "Short heading", "goal": "Purpose, claim boundaries and evidence to use",
                                         "evidence_handles": ["exact supplied handle"], "target_words": 0,
                                         "subsections": ["optional purposeful subsection"]}],
                          "visual_intents": [{"kind": "figure", "view": "supplied-data", "section_key": "exact key from your sections",
                              "title": "What the supplied data figure compares", "purpose": "Why this figure belongs here",
                              "evidence_handles": ["one exact supplied_figures handle"],
                              "figure_paths": ["Exact paths from results.supplied_analyses for that package; [] for linked-only"]}]},
    }
    if template is not None:
        payload["template_guidance"] = planning_template_guidance(template, config)
    if retry:
        payload["validation_error"] = retry_error
        payload["rejected_response"] = rejected_response
        payload["retry_instruction"] = "Correct this rejected proposal against the same inputs. Check every section owner, evidence handle and metric_id, including pointers not named in the first validation error. Copy metric_id from recorded_metrics, not its display name. Return the complete corrected proposal; do not add evidence or remove the substantive argument to make validation pass."
    return payload


def adopted_claims(
    initial: Sequence[ClaimEvidenceRecord], sections: Sequence[ReportSectionDraft],
) -> list[ClaimEvidenceRecord]:
    """Keep input guards and the current drafts' declarations, never old revisions.

    Claim ids supplied by different sections need not be globally unique. Keep
    both records instead of silently replacing one section's evidence with another.
    These are model declarations, not independently verified support.
    """
    return [*initial, *(claim for section in sections for claim in section.claims)]


def adopted_memory_notes(
    initial: ReportMemory, recorded: ReportMemory, sections: Sequence[ReportSectionDraft],
    iterations: Sequence[ReportIterationRecord], pending_draft: ReportSectionDraft | None = None,
) -> dict[str, list[str]]:
    """Rebuild current notes using the frozen input and existing draft owners.

    Retain input constraints even when identical to a superseded model note.
    Trace old append-only notes to recorded drafts before withdrawing them;
    legacy notes without a known owner survive rather than being guessed away.
    Pending/rejected drafts establish provenance, never current authority.
    No stored input, draft or diagnostic history is mutated or certified here.
    """
    drafts = [*sections, *(row.draft for row in iterations if row.draft is not None),
              *(draft for row in iterations for draft in row.drafts)]
    if pending_draft is not None:
        drafts.append(pending_draft)
    result = {}
    for field in ("limitations", "open_questions"):
        owned = {value for draft in drafts for value in getattr(draft, field)}
        values = [*getattr(initial, field),
            *(value for value in getattr(recorded, field) if value not in owned),
            *(value for section in sections for value in getattr(section, field))]
        result[field] = list(dict.fromkeys(value for value in values if value))
    return result


def delivery_text_observation(
    context: ReportContext, memory: ReportMemory, sections: Sequence[ReportSectionDraft],
    config: ReportRuntimeConfig, *, additions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Observe canonical delivery length and references from the same preview.

    No second renderer or saved state. Missing package fields or invalid citation
    inputs make the count unknown; omission is never interpreted as zero prose.
    """
    from simple_ar.literature.verify import CitationError
    from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document

    blocks = supplied_data_delivery(context, config=config, plan=memory.document_plan,
        section_ids=[row.section_id for row in sections]) if additions is None else additions
    result = {
        "counting_rule": "whitespace-separated tokens in canonical pre-render Markdown, including title, section headings, registered data captions/tables, experiment appendix and cited references; not a language-independent word-limit verifier",
        "scope": "current supplied sections only; future drafts, renderer-generated figures and attachment rechecks are not certified",
        "preview_status": "unavailable", "markdown_token_count": None,
        "rendering_performed": False,
        "references": None,
        "pending_owner_sections": [],
        "pending_draft_sections": [row.section_id for row in (memory.document_plan.sections
            if memory.document_plan and memory.document_plan.sections else memory.section_plan)
            if not any(draft.section_id == row.section_id and draft.draft_markdown.strip() for draft in sections)],
        "length_check": check_document_length(memory.document_plan.length_budget if memory.document_plan else {},
            objective=report_objective(context, memory), token_count=None),
    }
    if not any(row.draft_markdown.strip() for row in sections) or any(row.get("preview_status") == "unavailable" for row in blocks):
        result["unavailable_reason"] = "No current draft or incomplete registered attachment preview."
        return result
    assembled = tuple(sections)
    planned = {row.section_id: row for row in memory.document_plan.sections} if memory.document_plan else {}
    for block in blocks:
        if not block["heading"] and not any(row.section_id == block["section_id"] for row in assembled):
            owner = planned.get(block["section_id"])
            if owner is None:
                result["unavailable_reason"] = "Registered attachment owner has no current draft or frozen heading."
                return result
            # The frozen owner already exists; count its registered attachment
            # without inventing a future model draft or changing placement.
            assembled = (*assembled, ReportSectionDraft(section_id=owner.section_id, heading=owner.heading, draft_markdown=""))
            result["pending_owner_sections"].append(owner.section_id)
        assembled = attach_delivery_block(assembled, block)
    try:
        preview = preview_report_document(ReportAssemblyRequest(title=context.topic, sections=assembled,
            config=config, document_plan=memory.document_plan, template_name=memory.template,
            papers=tuple(context.papers), citation_key_map=context.citation_key_map,
            experiment_context=context if context.report_mode == "experiment" else None))
    except CitationError as exc:
        result["unavailable_reason"] = str(exc)
    else:
        budget = memory.document_plan.length_budget if memory.document_plan else {}
        body_count = manuscript_body_tokens(preview.report_body_markdown)
        delivery_count = len(preview.report_markdown.split())
        selected_count = document_tokens(preview.report_body_markdown, preview.report_markdown, budget.get("scope", ""))
        if budget.get("scope") == "manuscript_body":
            result["counting_rule"] = "whitespace-separated tokens in canonical body Markdown, including tables, captions and body attachments, excluding ATX title/section headings and appended bibliography; not a language-independent word-limit verifier"
        elif budget.get("scope") == "manuscript":
            result["counting_rule"] = "whitespace-separated tokens in canonical body Markdown, including title, headings, tables, captions and body attachments, excluding appended bibliography; not a language-independent word-limit verifier"
        result.update(preview_status="pre_render_text_preview",
                      markdown_token_count=selected_count,
                      full_delivery_markdown_token_count=delivery_count,
                      body_markdown_token_count=body_count,
                      references={"markdown": preview.references_markdown,
                          "citation_numbers": dict(preview.citation_numbers),
                          "model_keys": {key: paper_id for key, paper_id in context.citation_key_map.items()
                                         if paper_id in preview.citation_numbers}},
                      length_check=check_document_length(budget,
                          objective=report_objective(context, memory), token_count=selected_count),
                      removed_citations=list(preview.removed_citations))
    return result


def budget_document_plan(
    context: ReportContext, memory: ReportMemory, config: ReportRuntimeConfig,
    plan: ReportDocumentPlan, request: Any,
) -> ReportDocumentPlan:
    """Reserve known assembly text without drafting, rendering or new state.

    One internal marker per section makes the actual assembler include its
    heading. Subtract those marker tokens after the canonical preview. No
    marker is saved as prose and this forecast never certifies a real report.
    Unselected references cannot be previewed yet, so remain explicit unknowns.
    """
    validated = validate_length_request(request, objective=report_objective(context, memory))
    if not validated:
        return plan
    skeleton = [ReportSectionDraft(section_id=row.section_id, heading=row.heading,
        draft_markdown="SARPlanningPlaceholder") for row in plan.sections]
    observed = delivery_text_observation(context.model_copy(update={"papers": []}),
        memory.model_copy(update={"document_plan": plan.model_copy(update={"length_budget": validated})}), skeleton, config)
    count = observed.get("markdown_token_count")
    forecast = {
        "status": "known_assembly_forecast" if observed["preview_status"] == "pre_render_text_preview" else "unavailable",
        "known_fixed_markdown_tokens": count - len(skeleton) if type(count) is int else None,
        "counting_rule": observed["counting_rule"],
        "rendering_performed": False,
        "unresolved_future_components": (["cited_reference_selection_and_display"] if context.papers and validated["scope"] == "whole_document" else [])
            + (["new_model_authored_visuals_and_renderer_text"] if any(intent.view != "supplied-data" for intent in plan.visual_intents) else []),
        "scope_note": "Known assembly text within the task's selected count scope only; no future model prose. Whitespace tokens are a planning aid, not a language-independent word verifier.",
    }
    return reserve_document_words(plan, request=validated, forecast=forecast)


def narrative_context(
    memory: ReportMemory, section: ReportSectionPlan,
    adopted: Sequence[ReportSectionDraft],
    *, context: ReportContext | None = None, config: ReportRuntimeConfig | None = None,
    current_draft: ReportSectionDraft | None = None,
    independent_review: bool = False,
) -> dict:
    """Expose section responsibilities and actual preceding prose with coverage.

    No semantic summary is invented when the Writer omits optional claim metadata.
    Head/tail excerpts keep both a section's introduction and qualification visible;
    explicit omitted counts prevent treating this view as a full-document review.
    """
    plans = memory.document_plan.sections if memory.document_plan else memory.section_plan
    plan_by_id = {row.section_id: row for row in plans}
    others = [row for row in adopted if row.section_id != section.section_id]
    # Normal plans have at most twelve sections. Bound custom templates too;
    # favor recent drafts rather than allowing early provenance guards to crowd out prose.
    visible = others[-12:]
    # Reuse the original 12*1000 prose window as one document allowance.
    # Short drafts must not lose their middle just because each section used
    # to get an independent 1000-character cap. Distribute spare space fairly
    # over longer sections; saved text and coverage remain authoritative.
    lengths = [len(row.draft_markdown) for row in visible]
    allowances = [0] * len(visible)
    remaining = 12_000
    active = list(range(len(visible)))
    while active and remaining:
        share = max(1, remaining // len(active))
        for index in active:
            added = min(share, lengths[index] - allowances[index], remaining)
            allowances[index] += added
            remaining -= added
        active = [index for index in active if allowances[index] < lengths[index]]
    view = {
        "edit_scope": report_edit_scope([section.section_id]),
        "planning_status": dict(PLANNING_CONTEXT_STATUS),
        "section_purpose": section.goal,
        "length_observation": {
            "counting_rule": "whitespace-separated tokens in full adopted Markdown, including tables; a planning aid, not a language-independent word-limit verifier",
            "adopted_token_count": sum(len(row.draft_markdown.split()) for row in others),
            "planned_total_words": sum(row.target_words for row in plans),
            "this_section_target_words": section.target_words,
        },
        "section_responsibilities": [
            {"section_id": row.section_id, "heading": row.heading, "purpose": row.goal}
            for row in plans[:24]
        ],
        "responsibilities_omitted": max(0, len(plans) - 24),
        "adopted_sections": [
            {
                "section_id": row.section_id, "heading": row.heading,
                "purpose": plan_by_id[row.section_id].goal if row.section_id in plan_by_id else "",
                **_excerpt(row.draft_markdown, allowance),
                "table_excerpt": _table_excerpt(row.draft_markdown),
                "declared_claims": [claim.model_dump(mode="json") for claim in row.claims[:4]],
                "declared_claims_omitted": max(0, len(row.claims) - 4),
                "support_status": "not_independently_verified_by_this_projection",
            }
            for row, allowance in zip(visible, allowances)
        ],
        "adopted_sections_omitted": max(0, len(others) - len(visible)),
        "writing_rules": [
            *CLAIM_SCOPE_RULES,
            REVISION_PREMISE_RULE,
            "Address this section's purpose; use other sections' responsibilities to give each detailed fact a home.",
            "Use adopted prose to avoid contradictory or duplicated explanations; excerpts are not primary-source evidence.",
            "Abstract and conclusion synthesize the actual body, including negative results and limitations; do not add findings.",
            "Revisit a fact only for a different analytical purpose, not by repeating setup and provenance in every section.",
            "Numeric tables in adopted sections already own those detailed values; refer to them when relevant instead of duplicating the table or listing all values again. The table excerpt is adopted prose, not source verification.",
            "Review the prose itself for important claims even when optional claim metadata is empty or incomplete.",
            "Request source context for material uncertainties; missing excerpts do not establish absence from the original source.",
        ],
    }
    if context is not None and config is not None:
        view["source_comparisons"] = context.source_comparisons
        view["comparison_status"] = dict(DERIVED_CONTEXT_STATUS)
        additions = supplied_data_delivery(context, config=config, plan=memory.document_plan,
            section_ids=[row.section_id for row in plans])
        view["assembly_owned_content"] = additions + _experiment_delivery_view(context, config)
        observed = ([row for row in adopted if row.section_id != current_draft.section_id] + [current_draft]
                    if current_draft is not None else adopted)
        view["delivery_text_observation"] = delivery_text_observation(context, memory, observed, config, additions=additions)
        view["length_observation"]["assembly_owned_markdown_tokens"] = sum(
            row["markdown_token_count"] for row in view["assembly_owned_content"])
        view["length_observation"]["assembly_preview_complete"] = all(
            row.get("preview_status") != "unavailable" for row in additions)
        view["writing_rules"].extend(DELIVERY_RULES)
    if independent_review:
        # Review current prose against task, criteria and source passages, not
        # the proposed drafting answer. Retain section identities and actual
        # neighboring prose/coverage; no saved plan or draft is modified.
        for key in ("section_purpose", "writing_rules", "source_comparisons", "comparison_status"):
            view.pop(key, None)
        for row in view["section_responsibilities"]:
            row.pop("purpose")
        for row in view["adopted_sections"]:
            row.pop("purpose")
            row.pop("declared_claims")
            row.pop("declared_claims_omitted")
    elif memory.document_plan is not None:
        # All section prompts already include the frozen document_plan. Do not
        # repeat every long goal here (and again for each adopted section).
        # Unplanned/legacy callers retain the self-contained responsibility view.
        view["responsibilities_source"] = "document_plan.sections"
        view["argument_plan_source"] = "document_plan.argument_plan"
        view["argument_section_id"] = section.section_id
        view.pop("section_responsibilities")
        view.pop("responsibilities_omitted")
        for row in view["adopted_sections"]:
            row.pop("purpose")
            row["purpose_section_id"] = row["section_id"]
    return view


def _table_excerpt(text: str, limit: int = 1000) -> dict:
    """Expose literal table rows hidden between prose head/tail windows.

    Character positions point to the adopted draft. No inferred facts, summaries
    or persistent memory; this view cannot certify its table values.
    """
    matches = list(re.finditer(r"(?m)^[ \t]*\|[^\n]+\|[ \t]*$", text))
    windows = []
    remaining = limit
    for match in matches[:12]:
        if remaining <= 0:
            break
        end = min(match.end(), match.start() + remaining)
        windows.append({"start": match.start(), "end": end, "text": text[match.start():end]})
        remaining -= end - match.start()
    return {"windows": windows, "position_unit": "unicode_characters",
            "table_rows_available": len(matches), "table_rows_shown": len(windows),
            "characters_omitted": sum(len(match.group()) for match in matches) - (limit - remaining)}


def _excerpt(text: str, limit: int = 1000) -> dict:
    if len(text) <= limit:
        windows = [{"start": 0, "end": len(text), "text": text}]
    else:
        half = limit // 2
        tail = limit - half
        windows = [{"start": 0, "end": half, "text": text[:half]},
                   {"start": len(text) - tail, "end": len(text), "text": text[-tail:]}]
    return {"prose_windows": windows, "position_unit": "unicode_characters",
            "prose_characters": len(text),
            "prose_characters_omitted": len(text) - sum(len(row["text"]) for row in windows)}


def pending_revision_review(
    iterations: Sequence[ReportIterationRecord], section_id: str,
) -> tuple[ReportSectionReview | None, ReportSectionDraft | None]:
    """Recover the last revision's request and baseline from existing events."""
    events = [row for row in iterations if row.section_id == section_id]
    candidate_index = next((i for i in range(len(events) - 1, -1, -1)
                            if events[i].draft is not None), None)
    if candidate_index is None or events[candidate_index].action != "revise":
        return None, None
    prior = events[:candidate_index]
    review = next((row for row in reversed(prior) if row.action in {"review", "review_revision"}), None)
    baseline = next((row.draft for row in reversed(prior) if row.draft is not None), None)
    if review is None:
        return None, baseline
    return ReportSectionReview(section_id=section_id, verdict=review.status,
                               findings=review.findings, revision_instructions=review.revision_instructions), baseline


def revision_context(
    review: ReportSectionReview | None, baseline: ReportSectionDraft | None,
    *, candidate: ReportSectionDraft | None = None,
) -> dict:
    if review is None:
        return {}
    return {
        "review_opinions_status": dict(REVIEW_OPINIONS_STATUS),
        "target_findings": [row.model_dump(mode="json") for row in review.findings],
        "revision_instructions": review.revision_instructions,
        "effective_instructions": effective_revision_instructions(review),
        "original_section": {"section_id": baseline.section_id, **_excerpt(baseline.draft_markdown, 6000)} if baseline else {},
        "length_observation": {
            "counting_rule": "Whitespace tokens in each complete model-authored section only, not assembled delivery or a language-independent word verifier",
            "baseline_section_tokens": len(baseline.draft_markdown.split()) if baseline else None,
            "candidate_section_tokens": len(candidate.draft_markdown.split()) if candidate else None,
            "candidate_minus_baseline_tokens": len(candidate.draft_markdown.split()) - len(baseline.draft_markdown.split()) if baseline and candidate and baseline.section_id == candidate.section_id else None,
            "interpretation": "A changed status is not evidence the instruction was met. Check the actual change against effective instructions and the canonical delivery observation. Growth is not automatically a failure: necessary supported additions can increase length.",
        },
        "verification_rules": [
            REVISION_PREMISE_RULE,
            "Check each original finding and instruction against the candidate, not just its fluency or the generic template.",
            "Do not clear an unresolved defect solely because the wording or finding id changed.",
            "Removing unsupported or duplicated text is a valid correction; preserve supported content needed for this section, not the original word count.",
            "Reject new unsupported claims or lost necessary qualifications/citations. Use source evidence, not the original draft, to establish facts.",
            "If the baseline is absent or excerpted, state the visibility limit; omitted text is not proof that a fact was absent.",
        ],
    }


def pending_document_revisions(
    iterations: Sequence[ReportIterationRecord],
    *, include_rejected: bool = False,
) -> dict[str, tuple[ReportIterationRecord, ReportSectionReview]]:
    """Recover the last candidate and correction contract from existing events.

    Rejected drafts may be continued within the configured allowance. Carry the
    original request as well as the verifier's new defects, never just the latter.
    Historical verification events without instructions remain readable.
    """
    pending = {}
    seen = set()
    for index in range(len(iterations) - 1, -1, -1):
        row = iterations[index]
        if row.action != "document_revise" or row.section_id in seen:
            continue
        seen.add(row.section_id)
        verification = next((later for later in iterations[index + 1:]
                             if later.section_id == row.section_id and later.action == "document_verify"), None)
        if row.draft is None or row.adopted is True or (verification and not include_rejected):
            continue
        request = next((earlier for earlier in reversed(iterations[:index])
                        if earlier.section_id == row.section_id and earlier.action == "document_review"), None)
        if request is not None:
            pending[row.section_id] = (row, ReportSectionReview(section_id=row.section_id,
                verdict=request.status,
                findings=[*request.findings, *(verification.findings if verification else [])],
                revision_instructions=list(dict.fromkeys([
                    *request.revision_instructions,
                    *(verification.revision_instructions if verification else []),
                ]))))
    return pending

def _compact_source_metadata(metadata: dict[str, Any]) -> dict[str, str]:
    keys = ("method", "contribution", "evaluation", "relevance", "venue", "year")
    compact: dict[str, str] = {}
    for key in keys:
        value = metadata.get(key) if isinstance(metadata, dict) else None
        if value not in (None, ""):
            compact[key] = str(value)[:240]
    return compact


def _compact_execution_results(results: Mapping[str, Any] | object) -> dict[str, Any]:
    """Expose bounded registered results, not independent method certification.

    The deterministic report assembly already appends execution evidence after
    the agent pass.  Giving the Writer and Reviewer the same compact result
    projection prevents them from treating a real comparison as missing while
    keeping raw stdout, paths, and unrelated run metadata out of the prompt.
    """
    if not isinstance(results, Mapping):
        return {}

    def _mapping(value: object) -> Mapping[str, Any] | None:
        return value if isinstance(value, Mapping) else None

    def _metrics(value: object) -> dict[str, Any]:
        mapping = _mapping(value)
        if mapping is None:
            return {}
        blocked = {"stdout", "stderr", "command", "logs", "trace", "raw_output"}
        return {
            str(key): item
            for key, item in list(mapping.items())[:32]
            if str(key).lower() not in blocked
            and (item is None or isinstance(item, (bool, int, float, str)))
            and (not isinstance(item, str) or len(item) <= 200)
        }

    compact: dict[str, Any] = {}
    analyses = results.get("supplied_analyses")
    if isinstance(analyses, list):
        from simple_ar.result_analysis.table import table_input_handling
        compact["supplied_analyses"] = [{"document_id": row["document_id"], "evidence_role": row["evidence_role"],
            **({"schema_version": "code_analysis.v1", "artifact": row.get("artifact", ""),
                "results_scope": "Saved script results are retained in source chunks; not independently recomputed in writing."}
               if row.get("schema_version") == "code_analysis.v1" else
               {"spec": row["spec"], "records": row["records"][:12], "records_truncated": len(row["records"]) > 12})}
            for row in analyses[:6]]
        for projected, original in zip(compact["supplied_analyses"], analyses):
            if original.get("schema_version") == "code_analysis.v1":
                projected["figures"] = original["figures"][:12]
                projected["figures_omitted"] = max(0, len(original["figures"]) - 12)
                continue
            handling = table_input_handling(original)
            if isinstance(handling["observed_use"], list):
                handling["observed_use_omitted"] = max(0, len(handling["observed_use"]) - 24)
                handling["observed_use"] = handling["observed_use"][:24]
            projected["input_handling"] = handling
            figures = original.get("figures", [])
            projected["figures"] = figures[:12]
            projected["figures_omitted"] = max(0, len(figures) - 12)
            if "row_count" in original:
                projected["input_row_count"] = original["row_count"]
            projected["records_scope"] = (
                "Computed group/column summaries, not individual raw rows. Each record's count belongs to its "
                "own retained rows and declared group/column/coordinate key. Column summaries can use different "
                "nonmissing rows; paired_comparisons use only rows complete in both columns. A paired mean "
                "difference is not the difference of marginal column means unless their row sets match. "
                "Label these sample bases and their respective counts in a combined table, or separate the tables; "
                "do not put a paired count beside unlabeled marginal means. The imported analysis package retains "
                "its copied input separately; an uncomputed joint relationship is not evidence that row-level data are unavailable."
                if original["spec"].get("mode", "observations") == "observations" else
                "Supplied values/coordinates without inferred aggregation. Row meanings remain user-declared; "
                "the short records preview is not the complete input or proof of independence."
            )
            if original.get("coordinate_summaries"):
                projected["coordinate_summaries"] = original["coordinate_summaries"][:24]
                projected["coordinate_summaries_omitted"] = max(0, len(original["coordinate_summaries"]) - 24)
                projected["coordinate_summary_scope"] = (
                    "Exact count and marginal five-number summaries over all retained rows with both coordinates, "
                    "per declared group/column; quantiles interpolate at (n-1)*p. Raw points are not aggregated or replaced. "
                    "Separate axis distributions alone do not establish within-group association or joint overlap shape. "
                    "If an explicit association field is present, it reports descriptive complete-pair Pearson r for this group/column, "
                    "not candidate-minus-baseline differences, significance, causality, population inference or inspected-image evidence. "
                    "The short records preview is not representative of every group; use summaries or read full records."
                )
            if original.get("observation_summaries"):
                projected["observation_summaries"] = original["observation_summaries"][:24]
                projected["observation_summaries_omitted"] = max(0, len(original["observation_summaries"]) - 24)
                projected["observation_summary_scope"] = (
                    "Empirical marginal distributions of every nonmissing value per group/column; "
                    "quartiles interpolate at (n-1)*p. Not confidence intervals, paired differences, "
                    "joint relationships or population inference. The short records preview is not representative of every group."
                )
            if original.get("paired_comparisons"):
                projected["paired_comparisons"] = [
                    {key: value for key, value in pair.items() if key != "differences"}
                    for pair in original["paired_comparisons"]]
        compact["supplied_analyses_truncated"] = len(analyses) > 6
    implementation = _mapping(results.get("implementation"))
    if implementation is not None:
        compact["implementation"] = {
            key: implementation[key] for key in (
                "artifact", "status", "asset_integrity", "method_validation", "interpretation",
            )
            if key in implementation
        }
        integrity = _mapping(implementation.get("asset_integrity"))
        if integrity is not None:
            compact["implementation"]["asset_integrity"] = {
                key: integrity[key]
                for key in ("status", "changed_assets", "errors", "content_fingerprint")
                if key in integrity
            }
        evidence = _mapping(implementation.get("evidence"))
        if evidence is not None:
            # Patches are already bounded at the artifact boundary. Keep the
            # cumulative lineage when a repair attempt stores only a delta;
            # Keep observed reading/check records as well as the patch.
            # Their absence from a preview is not proof that no check occurred.
            # All excerpts retain the projection owner's bounds/provenance.
            compact_evidence: dict[str, Any] = {
                key: evidence[key] for key in ("patch", "source_context", "validation", "validation_report", "validation_stdout")
                if key in evidence
            }
            patches = evidence.get("patches")
            if isinstance(patches, list):
                compact_evidence["patches"] = [
                    item for item in patches[:6] if isinstance(item, Mapping)
                ]
            compact["implementation"]["evidence"] = compact_evidence
    for key in ("status", "primary_metric"):
        if key in results:
            compact[key] = results[key]
    if "metrics" in results:
        compact["metrics"] = _metrics(results.get("metrics"))
    diagnosis = _mapping(results.get("failure_diagnosis"))
    if diagnosis is not None:
        compact["failure_diagnosis"] = {
            key: diagnosis[key]
            for key in ("status", "summary", "deficiencies", "stderr_tail")
            if key in diagnosis
        }

    for label in ("baseline", "patched", "candidate"):
        run = _mapping(results.get(label))
        if run is None:
            continue
        compact[label] = {
            key: run[key]
            for key in ("status", "returncode", "timed_out")
            if key in run
        }
        compact[label]["metrics"] = _metrics(run.get("metrics"))

    history = results.get("measurement_history")
    if isinstance(history, list):
        rows = [item for item in history if isinstance(item, Mapping)]
        selected = rows if len(rows) <= 24 else [*rows[:8], *rows[-16:]]
        compact["measurement_history_total"] = len(rows)
        compact["measurement_history_omitted"] = len(rows) - len(selected)
        compact["passed_candidate_measurements"] = sum(
            1 for item in rows
            if str(item.get("status") or "").lower() == "passed"
            and not str(item.get("action") or "").startswith((
                "baseline", "matrix_baseline", "supplement_baseline",
            ))
        )
        compact["measurement_history"] = [
            {
                "action": str(item.get("action") or ""),
                "status": str(item.get("status") or "unknown"),
                "metrics": _metrics(item.get("metrics")),
                "artifact": str(item.get("artifact") or ""),
                "implementation_artifact": str(
                    item.get("implementation_ref", {}).get("path") or ""
                ) if isinstance(item.get("implementation_ref"), Mapping) else "",
            }
            for item in selected
        ]

    comparisons = results.get("comparisons")
    if isinstance(comparisons, list):
        compact["comparisons"] = []
        for item in comparisons[:4]:
            comparison = _mapping(item)
            if comparison is None:
                continue
            row: dict[str, Any] = {
                key: comparison[key]
                for key in ("status", "verdict", "name", "condition", "reasons")
                if key in comparison
            }
            if "metrics" in comparison:
                metric_rows = comparison.get("metrics")
                if isinstance(metric_rows, list):
                    row["metrics"] = [
                        {
                            key: metric[key]
                            for key in ("name", "baseline", "patched", "candidate", "delta", "direction", "status")
                            if key in metric
                        }
                        for metric in metric_rows
                        if isinstance(metric, Mapping)
                        and "_after_task_" not in str(metric.get("name") or "")
                    ]
                else:
                    row["metrics"] = _metrics(metric_rows)
            compact["comparisons"].append(row)
    return compact


def _compact_experiment_plan(plan: Mapping[str, Any] | object) -> dict[str, Any]:
    """Keep report prompts focused while preserving the executed protocol.

    The persisted report snapshot keeps the complete experiment contract.  A
    Writer or Reviewer only needs the user-facing plan plus one compact copy of
    each distinct paired protocol; the same protocol was previously repeated
    once per seed and could dominate a long provider request.
    """
    if not isinstance(plan, Mapping):
        return {}

    compact = {
        str(key): value
        for key, value in plan.items()
        if key != "paired_protocols"
    }
    paired = plan.get("paired_protocols")
    if not isinstance(paired, list):
        return compact

    runs: list[dict[str, Any]] = []
    protocols: list[dict[str, Any]] = []
    seen_protocols: set[str] = set()
    protocol_keys = (
        "contract_id",
        "schema_version",
        "protocol_revision",
        "dataset_refs",
        "split_spec",
        "metric_specs",
        "comparison_conditions",
        "protected_assets",
    )
    for item in paired:
        if not isinstance(item, Mapping):
            continue
        run = {
            key: item[key]
            for key in ("seed", "condition", "artifact")
            if key in item
        }
        if run:
            runs.append(run)
        protocol = item.get("protocol")
        if not isinstance(protocol, Mapping):
            continue
        projection = {
            key: protocol[key]
            for key in protocol_keys
            if key in protocol and protocol[key] not in (None, "", [], {})
        }
        identity = json.dumps(projection, sort_keys=True, ensure_ascii=False)
        if projection and identity not in seen_protocols:
            seen_protocols.add(identity)
            protocols.append(projection)

    if runs:
        compact["paired_runs"] = runs
    if protocols:
        compact["paired_protocols"] = protocols
    return compact


def _compact_execution_context(value: object) -> str:
    """Legacy narrative view; structured declared/observed evidence is separate."""
    if not isinstance(value, str):
        return ""
    narrative = value.split("## Prepared execution specification", 1)[0].strip()
    return narrative[:5000]


def document_plan_context(memory: ReportMemory, *, independent_review: bool = False) -> dict[str, Any]:
    """One organizational plan view for every writing/review scope; not facts."""
    plan = memory.document_plan
    view = {
        "planning_status": dict(PLANNING_CONTEXT_STATUS),
        "interpretation_rules": [
            COMPARISON_SCOPE_RULE,
            "Section purposes and the frozen document plan organize the work; they are not observed claims or source evidence. Follow the original task and supported sources when a planning interpretation overreaches. Do not add an unsupported assertion merely to fulfill a named item in the plan.",
            "Develop the planned argument using source passages and recorded results: explain what the comparison means and how it advances the reader's question. Correct or omit a planned point if its premise is unsupported; a frozen plan is not scientific truth. Section identities, title, delivery scope and edit ownership remain fixed, not the truth of a proposed explanation.",
        ],
    }
    if plan is None or independent_review:
        # Reviewers judge current prose without the proposed drafting answer.
        # Legacy callers have no resolved plan; do not invent one for them.
        return view
    return {
        **view,
        "schema_version": plan.schema_version,
        "status": plan.status,
        "title": plan.title,
        "target_words": plan.target_words,
        "length_budget": plan.length_budget,
        "argument_plan": plan.argument_plan.model_dump(mode="json") if plan.argument_plan else None,
        "sections": [
            {
                "section_id": section.section_id,
                "heading": section.heading,
                "goal": section.goal,
                "target_words": section.target_words,
                "min_citations": section.min_citations,
                "subsections": section.subsections[:6],
            }
            for section in plan.sections
        ],
        "visual_budget": plan.visual_budget,
        "visual_intents": [
            {
                "kind": intent.kind,
                "title": intent.title,
                "purpose": intent.purpose,
                "section_id": intent.section_id,
                "view": intent.view,
                "columns": intent.columns,
            }
            for intent in plan.visual_intents
        ],
    }


def _prompt_metrics(
    memory: ReportMemory, *, detail: str = "full",
    section_id: str | None = None, metric_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Build a compact model-facing table while retaining raw evidence elsewhere.

    Paired experiments also keep task-by-task measurements in the session for
    audit and export.  Sending those rows to every section writer duplicates a
    large amount of context without helping ordinary paper prose; aggregate
    and non-task-level seed rows provide the default overview. Section argument
    references and Reviewer draft references retain their exact measurements,
    including task-level rows. Legacy plans keep the complete compact overview.
    """
    columns = ["metric_id", "name", "value", "label", "direction", "condition_id", "unit", "source_kind"]
    metrics = list(memory.metric_sources)
    requested = set(metric_ids or [])
    argument = memory.document_plan.argument_plan if memory.document_plan else None
    if section_id is not None and argument is not None:
        # Ownership is independent of heading, language and drafting order.
        requested.update(metric_id for point in argument.points
                         if point.section_id == section_id for metric_id in point.metric_ids)
        detail = "summary"
    paired_summary = [
        metric for metric in metrics
        if metric.label.startswith("paired_summary:")
        and "_after_task_" not in metric.name
    ]
    if paired_summary:
        seed_metrics = [
            metric for metric in metrics
            if not metric.label.startswith("paired_summary:")
            and "_after_task_" not in metric.name
        ]
        metrics = (
            paired_summary
            if detail == "summary"
            else [*paired_summary, *seed_metrics]
        )
    # Explicit evidence references also retain task-level rows otherwise omitted
    # from the compact overview. Unknown IDs never fabricate measurements.
    selected = {metric.metric_id for metric in metrics}
    metrics.extend(metric for metric in memory.metric_sources
                   if metric.metric_id in requested and metric.metric_id not in selected)
    return {"columns": columns, "rows": [
        [getattr(metric, column) for column in columns] for metric in metrics
    ]}


def _prompt_handle_view(handle: Any, *, navigation: bool = False) -> dict[str, Any]:
    """Return a compact model-facing handle with short citation guidance.

    The raw handle is retained for source provenance and chunk backtracking, but
    prose citations should use ``cite_as``. This keeps long provider ids out of
    normal body citation generation.
    """
    data = handle.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    if handle.kind == "experiment_output":
        data["attribution_guidance"] = (
            "Name this recorded producer attachment in prose. It is not a literature source and has no paper citation key. "
            "The handle is for read tools and provenance, not a Markdown link target; do not fabricate bibliography citations."
        )
    if "title" in data:
        data["title"] = str(data["title"])[:240]
    if "summary" in data:
        data["summary"] = str(data["summary"])[:800]
    metadata = _compact_source_metadata(data.get("metadata", {}))
    data["metadata"] = {key: value[:160] for key, value in metadata.items()}
    # Writer and Reviewer must see the same evidence, not just an abstract.
    # Keep source excerpts distinct from model-derived reading notes.
    source_metadata = handle.metadata
    if (handle.kind == 'material'
            and source_metadata.get('evidence_role') == 'supplied_figure_not_visually_or_scientifically_verified'
            and isinstance(source_metadata.get('caption'), str)):
        # Explicit composition captions are bound authoring input, not a
        # navigation preview. The export manifest consumes the same full text.
        data['metadata']['caption'] = source_metadata['caption']
    if handle.kind == "prior_draft":
        data["attribution_guidance"] = "Earlier prose selected for editing, not primary evidence or a bibliography source."
        data["metadata"]["evidence_role"] = "prior_draft_not_primary_evidence"
    directory = source_metadata.get("source_directory")
    if isinstance(directory, dict):
        # The retained directory is an address book, not evidence. Repeating
        # long section/chunk identities in every planning/writing/review prompt
        # obscures the actual passages. Search supplies exact chunk addresses
        # when needed; group headings without changing the source snapshot.
        data["metadata"]["source_directory"] = {
            "headings": directory.get("headings", list(dict.fromkeys(
                str(row.get("heading") or "") for row in directory.get("sections", [])))),
            "total_sections": directory.get("total_sections"),
            "truncated": directory.get("truncated", False),
            "scope": "Distinct headings in retained directory entries, not reading coverage or paragraph adjacency."}
        data["source_navigation"] = (
            "This directory locates retained sections; headings are not evidence. "
            "When the requested experiment, definition or comparison is absent from the overview, "
            "use search_source_chunks with a specific heading/question, then get_neighbor_chunks with a returned chunk_id "
            "before concluding that the retained source lacks it."
        )
    if isinstance(source_metadata.get("recorded_execution_evidence"), dict):
        data["metadata"]["recorded_execution_evidence"] = source_metadata["recorded_execution_evidence"]
        data["attribution_guidance"] = (
            "These supplied records describe a previous execution, not work performed in this writing task. "
            "Use their declaration/observation roles and exact verification scope; do not cite them as a paper."
        )
    title_source = source_metadata.get("title_source")
    if isinstance(title_source, dict):
        data["metadata"]["title_source"] = {
            key: str(title_source.get(key) or "")[:240] for key in ("section_id", "quote", "scope")
        }
    origins = source_metadata.get("bibliographic_sources")
    if isinstance(origins, dict):
        data["metadata"]["bibliographic_sources"] = {
            field: {"section_id": str(origin.get("section_id") or ""),
                    "quote": str(origin.get("quote") or "")[:480],
                    "quote_truncated": len(str(origin.get("quote") or "")) > 480,
                    "scope": str(origin.get("scope") or "")}
            for field, origin in origins.items() if isinstance(origin, dict)
        }
    bibliography = source_metadata.get("bibliography")
    if isinstance(bibliography, dict):
        # Bibliography is recorded metadata, not reading notes or source support.
        authors = bibliography.get("authors", [])
        recorded_count = bibliography.get("recorded_authors_count")
        if type(recorded_count) is not int or recorded_count < len(authors):
            recorded_count = len(authors)
        prior_notes_omitted = bibliography.get("notes_omitted")
        prior_notes_omitted = max(0, prior_notes_omitted) if type(prior_notes_omitted) is int else 0
        data["metadata"]["bibliography"] = {
            **{key: str(bibliography.get(key) or "")[:480]
               for key in ("title", "published", "year", "doi", "url", "source", "verification_status")},
            "authors": [str(name)[:160] for name in authors[:6]],
            "recorded_authors_count": recorded_count,
            "author_names_omitted_from_prompt": max(0, recorded_count - min(len(authors), 6)),
            "author_visibility": "Shown names are a bounded prompt view of the recorded list; omission here is not a provider completeness finding.",
            "missing_fields": list(bibliography.get("missing_fields", [])),
            "notes": [str(note)[:240] for note in bibliography.get("notes", [])[:4]],
            "notes_omitted": prior_notes_omitted + max(0, len(bibliography.get("notes", [])) - 4),
            "consistency_issues": [str(issue)[:240] for issue in bibliography.get("consistency_issues", [])[:4]],
        }
    for key in ("document_id", "extraction_status", "reading_artifact", "reading_state", "reading_notes_kind", "evidence_role"):
        if source_metadata.get(key):
            data["metadata"][key] = str(source_metadata[key])[:240]
    notes = source_metadata.get("reading_notes")
    if isinstance(notes, dict):
        projected_notes = {}
        notes_truncated = bool(source_metadata.get("reading_notes_truncated"))
        for key in ("problem", "method", "datasets", "metrics", "key_claims", "limitations", "open_questions", "confidence", "evidence_refs"):
            value = notes.get(key)
            if isinstance(value, list):
                projected_notes[key] = [str(item)[:400] for item in value[:6]]
                notes_truncated |= len(value) > 6 or any(len(str(item)) > 400 for item in value[:6])
            elif isinstance(value, str):
                projected_notes[key] = value[:600]
                notes_truncated |= len(value) > 600
        scopes = notes.get("claim_scopes", [])
        if isinstance(scopes, list):
            projected_notes["claim_scopes"] = []
            for row in scopes[:4]:
                if not isinstance(row, dict):
                    continue
                claim = {key: str(row.get(key) or "unknown")[:600]
                         for key in ("claim_id", "claim", "object", "property", "evidence_kind", "scope")}
                for key in ("conditions", "evidence_refs"):
                    values = row.get(key, [])
                    if not isinstance(values, list):
                        claim[key] = []
                        notes_truncated = True
                        continue
                    claim[key] = [str(value)[:240] for value in values[:6]]
                    notes_truncated |= len(values) > 6 or any(len(str(value)) > 240 for value in values[:6])
                notes_truncated |= any(len(str(row.get(key) or "")) > 600 for key in claim if key not in {"conditions", "evidence_refs"})
                projected_notes["claim_scopes"].append(claim)
            prior_omitted = notes.get("claim_scopes_omitted")
            prior_omitted = max(0, prior_omitted) if type(prior_omitted) is int else 0
            projected_notes["claim_scopes_omitted"] = prior_omitted + max(0, len(scopes) - 4)
            notes_truncated |= len(scopes) > 4
        coverage = notes.get("reading_coverage")
        if isinstance(coverage, dict):
            bounded_coverage = {key: value if type(value) is int else str(value)[:160]
                                for key in ("available_chunks", "selection", "excerpt_chars", "semantic_verification")
                                if (value := coverage.get(key)) is not None}
            for key in ("shown_chunk_ids", "shortened_chunk_ids", "followup_shown_chunk_ids"):
                ids = coverage.get(key)
                if isinstance(ids, list):
                    bounded_coverage[key] = [str(item)[:240] for item in ids[:12]]
                    notes_truncated |= len(ids) > 12 or any(len(str(item)) > 240 for item in ids[:12])
            projected_notes["reading_coverage"] = bounded_coverage
        followup = notes.get("reading_followup")
        if isinstance(followup, dict):
            projected_notes["reading_followup"] = {
                "revision_performed": bool(followup.get("revision_performed")),
                "scope": str(followup.get("scope", ""))[:160],
                "pending_queries": [str(query)[:500] for query in followup.get("pending_queries", [])[:2]],
                "lookups": [
                    {key: row[key] for key in ("query", "status", "matched_chunk_ids", "new_window_count", "omitted_window_count", "semantic_verification") if key in row}
                    for row in followup.get("lookups", [])[:2] if isinstance(row, dict)
                ],
            }
        data["metadata"]["reading_notes"] = projected_notes
        data["metadata"]["reading_notes_truncated"] = notes_truncated
    passages = source_metadata.get("evidence_passages")
    if isinstance(passages, list):
        # A single presentation budget, not a second six-passage sample of
        # referenced evidence. Both Writer and independent Reviewer use this view.
        selected_passages = []
        # Explicit evidence is already selected and bounded at ingestion. Do
        # not turn that selection back into an overview, losing late conditions.
        explicit = not navigation and source_metadata.get("evidence_selection") == "explicit_retained_chunks"
        passage_limit = 96000 if explicit else 1400
        remaining = 96000 if explicit else 24000
        for row in (passages[:1] if navigation else passages):
            if not isinstance(row, dict):
                continue
            text = str(row.get("text", ""))[:min(passage_limit, remaining)]
            if not text:
                continue
            selected_passages.append({**row, "text": text,
                "truncated": bool(row.get("truncated")) or len(str(row.get("text", ""))) > len(text)})
            remaining -= len(text)
            if not remaining:
                break
        data["metadata"]["evidence_passages"] = [
            {"chunk_id": str(row.get("chunk_id", "")), "text": str(row.get("text", "")),
             "character_start": row.get("character_start"), "character_end": row.get("character_end"),
             "total_characters": row.get("total_characters"), "selection": row.get("selection", ""),
             **{key: row[key] for key in ("heading", "section_id") if key in row},
             "truncated": bool(row.get("truncated"))}
            for row in selected_passages
        ]
        data["metadata"]["evidence_passages_truncated"] = (bool(source_metadata.get("evidence_passages_truncated"))
            or len(selected_passages) < len(passages) or any(row["truncated"] for row in selected_passages))
    if "section" in data:
        data["section"] = str(data["section"])[:240]
    citation_key = data.get("citation_key") or ""
    if citation_key:
        data["cite_as"] = f"[@{citation_key}]"
        data["paper_id_for_display"] = citation_key
        data.pop("paper_id", None)
        data["tool_args"] = {"citation_key": citation_key}
    return data
