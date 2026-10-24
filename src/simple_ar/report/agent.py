from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from simple_ar.integrations.llm import LLMClient, LLMError, LLMResponseError
from simple_ar.research.contracts import CLAIM_SCOPE_RULES
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.report.projection import apply_report_bibliography, bibliography_planning_views
from simple_ar.report.assembler import assemble_report_sections
from simple_ar.report.data_delivery import supplied_data_delivery
from simple_ar.report.document_plan import ARGUMENT_PLAN_SCHEMA, ARGUMENT_PLANNING_RULES, LENGTH_REQUEST_RULE, LENGTH_REQUEST_SCHEMA, normalize_section_heading as _clean_outline_heading, outline_section_keys, resolve_argument_plan, resolve_document_plan, supplied_figure_sources, validate_length_request, visual_requirements
from simple_ar.report.templates import drafting_template_guidance, planning_template_guidance, reviewing_template_guidance, is_builtin_template
from simple_ar.report.editor import (
    DOCUMENT_CONTROL_FINDING_TYPES, document_revision_limit,
    coalesce_document_reviews, historical_opinion_handles, review_document, rejected_review_context_requests,
    edit_joint_document, _needs_revision,
)
from simple_ar.report.execution_evidence import report_execution_evidence
from simple_ar.report.review_evidence import (
    EVIDENCE_QUOTE_RULES, EVIDENCE_QUOTE_SCHEMA, validate_finding_anchors, review_draft_quote_sources,
    review_evidence_locator, resolve_review_evidence,
    attachment_review_blocks_editing, EVIDENCE_NAVIGATION_CONTRACTS, SHARED_PRODUCER_LOCATOR_CONTRACT,
)
from simple_ar.report.narrative import (
    DERIVED_CONTEXT_STATUS, REVIEW_OPINIONS_STATUS,
    document_plan_context, outline_evidence_labels, map_evidence_labels,
    _compact_execution_context,
    _compact_execution_results,
    _compact_experiment_plan,
    _compact_source_metadata,
    _prompt_handle_view,
    _prompt_metrics,
    adopted_claims, adopted_memory_notes, budget_document_plan, delivery_text_observation, effective_revision_instructions, evidence_outline_context, narrative_context, pending_document_revisions, _experiment_delivery_view,
    pending_revision_review, revision_context, report_objective, report_tool_context, review_source_evidence, prior_draft_context,
    prior_document_revision,
)
from simple_ar.report.schema import (
    finding_requires_resolution,
    REVIEW_ACTION_RULES,
    AgentReportResult,
    ReportArgumentPlan,
    ReportContext,
    ReportIterationRecord,
    ReportFindingCheck,
    ReportMemory,
    ReportRuntimeConfig,
    ReportSectionDraft,
    ReportSectionPlan,
    ReportSectionReview,
    ReportTemplateBundle,
    ReportToolCall,
    ReportToolResult,
    ReviewerFinding,
)
from simple_ar.report.survey import (
    _bounded_int as _coerce_int, _survey_draft_order,
    is_survey_report, route_section_sources,
)
from simple_ar.report.tool_gateway import ReportToolGateway, checkpointed_report_reads
from simple_ar.report.tools import report_tool_specs, validate_report_reads
from simple_ar.report.templates import BUILTIN_TEMPLATE_NAMES


_EXPERIMENT_EVIDENCE_RULES = """
Distinguish changes shown by the frozen patch from reused existing code and
unimplemented proposals. Do not describe an invoked utility as modified unless
its implementation appears in the patch.
Separate code that was written from behavior that was observed. A failed run
may not have reached or completed the intended operation: describe that code
as configured or attempted, not as a computation that actually occurred.
Treat claims that a failed run executed past its recorded error as factual
conflicts, even when the patch contains the intended code path.
An unchanged protocol isolates the implemented candidate as a whole, not a
unique causal mechanism. Without a relevant ablation or direct mechanism
measurement, do not assign all differences to one component. Do not invent
alternative implementation effects either: mention a plausible contributor
only if it is present in the verified implementation evidence, and distinguish
an untested explanation from an observed result. Review unsupported causal
attribution as a factual issue, not merely a style suggestion.
Means and sample standard deviations are descriptive: they do not establish
statistical significance or rule out seed variance. Such claims require an
actual appropriate statistical analysis in the supplied evidence.
With a small number of seeds and no significance test, do not call a result
conclusive, rule out seed noise, or imply stable general improvement. Use
bounded wording such as "under this protocol" and "descriptive across the
observed seeds".
Place shared validation and generalization limits in the document's designated
scope section. Qualify a result locally when needed to prevent a misleading
claim, but do not demand the same global disclaimer in every section or replace
substantive explanation with repeated evidence-status language.
Hardware, accelerator, operating-system, and runtime details stated only in the
task or prepared context are declared conditions, not observed execution
evidence. Unless an executor record or identified producer observation records those details, describe
them as requested or configured conditions and say that hardware was not
recorded; do not write that the runs executed on that hardware.
Technical implementation status such as `validated`, a passing static check,
or an LLM review warning is not research-method evidence. If
implementation.method_validation.status is `未检查`, state that the candidate
mechanism was not independently checked; if it records execution evidence of
contradiction, do not present the same candidate method as validated.
Cite directly relevant original method papers when available in the supplied
sources; surveys are not a substitute for attribution of the adopted method.
"""

WRITER_SYSTEM = """You are the SimpleAutoResearch report Writer.
Write only evidence-bounded Markdown sections for the current run.
Do not invent citations, metrics, datasets, methods, or external references.
Use the registered execution results for local baseline, candidate and comparison
values. Ground execution/resource claims in their recorded owner: requested
conditions are declarations, executor records are observations, and producer
outputs are attributed measurements. A result's presence does not validate its method.
Use only registered citation keys. When none are supplied, attribute materials
by their actual names and supplied public URLs; do not invent citation markers.
Write an argument, not a pipeline run log. State supported conclusions directly;
give each result or comparison a purpose in answering the reader's question.
Keep scientific methods and material comparison conditions in the main text.
Full commands, absolute paths and execution receipts belong in the existing
reproduction attachments unless the user requests them in the body. Explain
necessary execution controls from their recorded evidence, not declarations.
Keep paragraphs short and focused. Use as many paragraphs as the requested
section target needs; only when no substantive length target is provided,
prefer concise paragraphs or a short comparison list instead of one dense
block. A short conclusion may need only one paragraph; do not pad it.
For multi-source survey reports, synthesize across papers: build taxonomies,
contrast assumptions, compare evaluation settings, and state boundary conditions.
For a single-source review, assess that source's evidence, including its own
controls, ablations and method comparisons. One source can report several
methods: preserve a supported matched comparison within its tested setting,
without generalizing it to every task. Do not invent absent comparisons or
cross-paper consensus.
When many sources are available, use them to improve coverage and confidence;
do not make the report grow linearly by writing one paragraph per paper.
Never write prompt-planning language such as "Hint:", "Use this paper as", or
"Additional synthesis detail is available".
Return one JSON object matching the requested schema.""" + _EXPERIMENT_EVIDENCE_RULES


REVIEWER_SYSTEM = """You are the SimpleAutoResearch report Reviewer.
Review independently against the provided criteria, source handles, metrics, and current-run boundaries.
Recorded bibliographic metadata is not independent proof of source identity,
edition, publication date or a complete byline. Check important attribution
claims against the supplied original passages; retain unresolved metadata
conflicts or coverage limits instead of inventing replacements.
Optional draft source-use/citation arrays may be empty. Judge inline citations
and their support in the prose, not the presence of those optional arrays.
Do not rewrite prose unless asked. Return structured findings and optional bounded context requests.
Use registered execution results for local baseline, candidate and comparison
values, retaining declaration/executor/producer ownership for resource claims.
A result's presence does not validate its method. Do not flag a claim as
unsupported merely because the deterministic execution appendix is assembled
after the Writer/Reviewer pass.
Flag operational/provenance sections in research-only reports when they make
the report read like a pipeline log instead of an academic survey.
Flag any section that is one huge paragraph or mixes many unrelated claims
without paragraph breaks or comparison bullets.
Flag paper-by-paper note dumps, prompt/planning residue, and performance claims
without boundary conditions. Require a taxonomy or cross-paper comparison only
when multiple independent sources support one; for a single-source review,
check that the report does not invent a comparison or consensus.
Flag conclusive language, claims that seed noise has been ruled out, or claims
of stable general improvement when the supplied evidence has only a small seed
set and descriptive statistics. Ask the Writer to bound those statements to
the observed protocol unless an appropriate statistical analysis is present.
Prefer revision instructions that improve synthesis density, evidence coverage,
and section structure over requests to add more paper-by-paper detail.
Return one JSON object matching the requested schema.""" + _EXPERIMENT_EVIDENCE_RULES


OUTLINE_PLANNER_SYSTEM = """You are the SimpleAutoResearch Outline Planner.
Organize the requested document around the current evidence and its limitations.
Do not use external gold outlines, benchmark references, or hidden evaluator
expectations. Keep the outline broad, readable, and evidence-bounded.
Return one JSON object matching the requested schema."""


SECTION_DELIVERY_REVIEW_CONTRACT = 'section_delivery_review.v1'


def _section_delivery_assembly(context: ReportContext, memory: ReportMemory,
                               sections: list[ReportSectionDraft], config: ReportRuntimeConfig) -> dict[str, Any]:
    """Use the assembly owner's text preparation; never certify future rendering.

    The consumer must compare body_sha256 with actual report_body.md and check
    the separately retained transport receipts and attachment versions.
    """
    from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document

    contract = context.results['section_delivery_review']
    if contract.get('contract') != SECTION_DELIVERY_REVIEW_CONTRACT:
        raise ValueError('Unknown section delivery review contract')
    options = contract['assembly']
    if (set(options) - {'title', 'assembly_mode', 'template_name', 'figure_captions'}
            or options.get('assembly_mode') != 'section_drafts'):
        raise ValueError('Section delivery inspection requires explicit canonical section assembly options')
    if options.get('figure_captions', {}) != context.results.get('provided_figure_captions', {}):
        raise ValueError('Section inspection and assembly disagree on provided caption ownership')
    preview = preview_report_document(ReportAssemblyRequest(**options, sections=tuple(sections),
        config=config, document_plan=memory.document_plan, papers=tuple(context.papers),
        citation_key_map=context.citation_key_map))
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'contract': SECTION_DELIVERY_REVIEW_CONTRACT,
        'identity': {
            'body_sha256': hashlib.sha256(preview.report_body_markdown.encode('utf-8')).hexdigest(),
            'report_sha256': hashlib.sha256(preview.report_markdown.encode('utf-8')).hexdigest(),
            'sections_sha256': digest([row.model_dump(mode='json') for row in sections]),
            'delivery_snapshot_sha256': digest(context.results['delivery_review'])},
        'report_body_markdown': preview.report_body_markdown, 'report_markdown': preview.report_markdown}


def run_report_agent(
    *,
    client: LLMClient | None,
    context: ReportContext,
    template: ReportTemplateBundle,
    memory: ReportMemory,
    config: ReportRuntimeConfig,
    gateway: ReportToolGateway,
    emit: Callable[[str], None] | None = None,
    checkpoint_sink: Callable[[dict[str, Any]], None] | None = None,
    completed_checkpoint: Mapping[str, Any] | None = None,
    assemble_body: bool = True,
    on_delivery_review: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    evidence_navigation_contract: str | None = SHARED_PRODUCER_LOCATOR_CONTRACT,
) -> AgentReportResult | None:
    """Run the bounded Writer/Reviewer loop for the report stage.

    Args:
        client: Configured LLM client. ``None`` disables agent mode.
        context: Compact report input from earlier stages.
        template: Loaded writing template and reviewer criteria.
        memory: Recoverable report memory.
        config: Runtime report config.
        gateway: Read-only report tool gateway.
        emit: Optional progress callback.
        checkpoint_sink: Save accepted plan, completed sections, pending drafts and revision diagnostics.
        completed_checkpoint: Previously completed prefix; the caller verifies input identity.
        assemble_body: Preserve the direct-call report body by default. Capability
            callers publish sections through the assembly owner instead.
        on_delivery_review: Resolve the completed transport receipt for a validated
            delivery observation. Defaults to client.last_inspection_source;
            persistence and filesystem receipt validation stay with the caller.
        evidence_navigation_contract: New direct-call view. Capability callers
            supply the frozen memory's version, including historical None.
            Checkpoint recovery always consumes its own saved memory.

    Returns:
        Agent-generated report body and updated memory, or ``None`` when agent
        mode is disabled or a validated agent pass cannot be produced.
    """
    if client is None or config.agent == "disabled":
        return None
    if not memory.section_plan:
        return None

    current = memory.model_copy(deep=True)
    # Direct new work freezes the same view as capability inputs. A retained
    # checkpoint owns its memory, including an absent historical marker.
    if completed_checkpoint is None:
        navigation = current.survey_contract.get('evidence_navigation_contract', evidence_navigation_contract)
        if navigation not in EVIDENCE_NAVIGATION_CONTRACTS:
            raise ValueError('Unknown evidence navigation contract')
        if navigation is not None:
            current.survey_contract = {**current.survey_contract, 'evidence_navigation_contract': navigation}
    sections: list[ReportSectionDraft] = []
    iterations: list[ReportIterationRecord] = []
    all_findings: list[ReviewerFinding] = []
    all_tool_results: list[ReportToolResult] = []
    document_review_done = bool(completed_checkpoint.get("document_review_done")) if completed_checkpoint else False
    # No implicit upgrade of historical checkpoint/review protocols.
    delivery_contract = (completed_checkpoint.get('section_delivery_review_contract')
        if completed_checkpoint is not None else
        context.results.get('section_delivery_review', {}).get('contract'))
    if delivery_contract is not None:
        if delivery_contract != SECTION_DELIVERY_REVIEW_CONTRACT:
            raise ValueError('Unknown section delivery review checkpoint contract')
        if 'delivery_review' not in context.results:
            raise ValueError('Integrated section review requires frozen delivery context')
        if config.draft_scope not in {'document', 'auto'}:
            raise ValueError('Integrated section review requires joint document correction scope')
        if on_delivery_review is None:
            def retained_delivery_source(_observation: dict[str, Any]) -> dict[str, Any]:
                source = getattr(client, 'last_inspection_source', None)
                return source() if callable(source) else source
            on_delivery_review = retained_delivery_source
    # The existing checkpoint owns the method as well as the drafts. Missing
    # markers are historical opinion-check contracts, never an implicit upgrade.
    review_contract = (completed_checkpoint.get("document_review_contract") if completed_checkpoint is not None
                       else 'current_draft.v1' if config.document_review
                       and config.draft_scope in {'document', 'auto'} else None)
    if review_contract is not None and review_contract != 'current_draft.v1':
        raise ValueError('Unknown document review contract; no method substitution on recovery.')
    if review_contract and config.draft_scope not in {'document', 'auto'}:
        raise ValueError('Saved current-document review requires its joint correction scope.')

    if completed_checkpoint is not None:
        current = ReportMemory.model_validate(completed_checkpoint["memory"])
        sections = [ReportSectionDraft.model_validate(row) for row in completed_checkpoint["sections"]]
        iterations = [ReportIterationRecord.model_validate(row) for row in completed_checkpoint["iterations"]]
        current_records = any(row.action == 'document_current_review' for row in iterations)
        historical_checks = any(row.action.startswith('document_finding_') for row in iterations)
        if ((review_contract is None and current_records)
                or (review_contract is not None and historical_checks)):
            raise ValueError('Report checkpoint mixes current-document and historical opinion-check contracts.')
        all_findings = [ReviewerFinding.model_validate(row) for row in completed_checkpoint["reviewer_findings"]]
        # Old checkpoints marked an unavailable inspection as finished. Keep
        # its drafts and consumed rounds, but resume the unfinished work.
        if (any(row.type in {"document_review_unavailable", "document_revision_unavailable"}
                for row in current.reviewer_findings)
                and not any(row.rejected_review.get("format_correction") for row in iterations)):
            document_review_done = False
        all_tool_results = [ReportToolResult.model_validate(row) for row in completed_checkpoint["tool_results"]]
        # Reuse the persisted results as the read-call ledger. Resuming must
        # not silently grant another full tool allowance.
        for name in gateway.call_counts:
            gateway.call_counts[name] = max(gateway.call_counts[name],
                sum(row.tool_name == name for row in all_tool_results))
        expected = [section.section_id for section in _draft_sequence(current.section_plan)]
        if [section.section_id for section in sections] != expected[:len(sections)]:
            raise ValueError("Report checkpoint sections do not match the planned draft sequence.")
    if gateway.documents is not None:
        context, current = apply_report_bibliography(context, current, documents=gateway.documents,
            notes=current.outline_planning.get("source_notes", []))
        gateway.set_context(context)
    completed_count = len(sections)
    pending = completed_checkpoint.get("pending_draft") if completed_checkpoint else None
    pending_draft = ReportSectionDraft.model_validate(pending) if pending is not None else None
    if pending_draft is not None:
        planned = _draft_sequence(current.section_plan)
        if completed_count >= len(planned) or pending_draft.section_id != planned[completed_count].section_id:
            raise ValueError("Pending report draft does not match the next planned section.")

    def refresh_adopted_memory() -> None:
        # Frozen input remains the seed. Checkpoint history identifies obsolete
        # draft notes, but cannot promote a pending/rejected candidate to fact.
        current.claims_evidence_matrix = adopted_claims(memory.claims_evidence_matrix, sections)
        for field, values in adopted_memory_notes(memory, current, sections, iterations, pending_draft).items():
            setattr(current, field, values)

    # Reproject an older append-only checkpoint before the first resumed prompt.
    refresh_adopted_memory()

    def checkpoint() -> None:
        # Claims and notes follow the exact adopted sections saved by this owner.
        refresh_adopted_memory()
        if checkpoint_sink is not None:
            checkpoint_sink({"memory": current.model_dump(mode="json"),
                             "sections": [row.model_dump(mode="json") for row in sections],
                             "iterations": [row.model_dump(mode="json") for row in iterations],
                             "reviewer_findings": [row.model_dump(mode="json") for row in all_findings],
                             "tool_results": [row.model_dump(mode="json") for row in all_tool_results],
                             "pending_draft": pending_draft.model_dump(mode="json") if pending_draft else None,
                             "document_review_done": document_review_done,
                            **({'section_delivery_review_contract': delivery_contract} if delivery_contract else {}),
                             **({'document_review_contract': review_contract} if review_contract else {})})

    # Save the seed before selecting reads. Until document_plan is resolved,
    # an empty prefix can resume source selection/planning; accepted plans
    # and drafts retain their original identities on recovery.
    def writer_reads(requests: list[ReportToolCall] | None) -> list[ReportToolResult]:
        return _writer_evidence_reads(owner="initial_document", requests=requests,
            gateway=gateway, iterations=iterations, all_results=all_tool_results, checkpoint=checkpoint)

    try:
        # Composition and deciding what to read compete in one response: a
        # fluent draft can bypass the only experiment needed by its argument.
        # For navigable, excerpted sources, select reads first. Saved analysis
        # results need the same access as papers; document kind is not coverage.
        # Reuse the outline read trace/allowance, including an accepted empty
        # batch; old inputs and accepted checkpoints keep their prior flow.
        if (current.document_plan is None and completed_count == 0 and pending_draft is None
                and config.allow_source_backtracking
                and config.max_backtracking_calls > 0
                and not current.outline_planning.get("context_requests")
                and not any(row.action == "writer_context" for row in iterations)
                and any(h.metadata.get("source_directory")
                        and h.metadata.get("evidence_passages_truncated") for h in current.source_handles)):
            checkpoint()
            selection = client.ask_json(WRITER_SYSTEM, _json_prompt({
                "task": "Identify the reader's decisions and questions, then select necessary source reads before planning an answer; do not draft sections or an outline.",
                "objective": report_objective(context, current),
                **prior_document_revision(context, current, include_baseline=True),
                "sources": [_prompt_handle_view(h, navigation=True) for h in current.source_handles if h.kind != "prior_draft"][:40],
                "tools": [spec.model_dump(mode="json") for spec in report_tool_specs()],
                "context_request_schema": ReportToolCall.model_json_schema(),
                "output_schema": {"context_requests": []},
                "rules": [f"Return at most {min(6, config.max_backtracking_calls)} registered read-only requests.",
                          "Choose queries for missing definitions, comparison objects, experimental conditions or feasibility evidence needed by the user's questions. Do not merely repeat a draft's headline or numbers to confirm its conclusion.",
                          "An empty list is valid if visible passages already cover those needs. Headings locate text; they do not prove claims."],
            }), label="report-source-selection")
            calls = validate_report_reads(selection.get("context_requests"), limit=min(6, config.max_backtracking_calls))
            _writer_evidence_reads(owner="outline_evidence", requests=calls, gateway=gateway,
                iterations=iterations, all_results=all_tool_results, checkpoint=checkpoint)
        preplanning_reads = _writer_evidence_reads(owner="outline_evidence", requests=None,
            gateway=gateway, iterations=iterations, all_results=all_tool_results, checkpoint=checkpoint)
        selected_before_plan = current.document_plan is None and any(
            row.action == "writer_context" and row.summary == "outline_evidence" for row in iterations)
        if completed_checkpoint is None or selected_before_plan:
            current = _maybe_adapt_outline(client=client, context=context, template=template,
                memory=current, config=(config.model_copy(update={"allow_source_backtracking": False})
                    if selected_before_plan else config), emit=emit, documents=gateway.documents,
                extra_context=preplanning_reads)
            if gateway.documents is not None:
                context, current = apply_report_bibliography(context, current, documents=gateway.documents,
                    notes=current.outline_planning.get("source_notes", []))
                gateway.set_context(context)
        current = _resolve_document_plan(current, config=config, context=context,
            fallback_title=template.template_markdown.splitlines()[0].lstrip("# ").strip())
        # Freeze the evidence-informed plan before any composition call.
        checkpoint()
        # The accepted argument can identify evidence missing from the overview.
        # Consume the same initial read batch before composing, not another
        # planning call or a second read/recovery ledger.
        initial_requests = current.outline_planning.get("context_requests", [])
        # An outline lookup is not the Writer's independent gap-read batch.
        # Legacy checkpoints used initial_document for both; retain their
        # accepted batch rather than replaying it under a new owner.
        legacy_reads = (completed_checkpoint is not None and any(
            row.action == "writer_context" and row.summary == "initial_document" for row in iterations
        ) and not any(row.action == "writer_context" and row.summary == "outline_evidence" for row in iterations))
        initial_evidence = (_writer_evidence_reads(owner="outline_evidence",
            requests=[ReportToolCall.model_validate(row) for row in initial_requests] if initial_requests else None,
            gateway=gateway, iterations=iterations, all_results=all_tool_results, checkpoint=checkpoint)
            if completed_count < len(current.section_plan) and not legacy_reads else [])
        # A planning heuristic, not a token guarantee. Explicit document/section
        # choices and their historical checkpoints keep their original behavior.
        # An unsized plan uses recoverable sections rather than guessing its size.
        joint_draft = config.draft_scope == "document" or (
            config.draft_scope == "auto" and bool(current.section_plan)
            and all(row.target_words > 0 for row in current.section_plan)
            and sum(row.target_words for row in current.section_plan) <= 2000)
        if (joint_draft and len(current.section_plan) > 1
                and pending_draft is None and completed_count < len(current.section_plan)):
            remaining = _draft_sequence(current.section_plan)[completed_count:]
            _emit(emit, f"Writer jointly drafting {len(remaining)} remaining section(s).")
            joint = _draft_document_with_recovery(client=client, context=context, template=template,
                memory=current, config=config, sections=remaining, adopted_sections=sections, emit=emit,
                extra_context=initial_evidence, read_context=writer_reads,
                source_front_matter=bibliography_planning_views(context, gateway.documents))
            if gateway.documents is not None:
                context, current = apply_report_bibliography(context, current, documents=gateway.documents,
                    notes=current.outline_planning.get("source_notes", []))
                gateway.set_context(context)
            # Validate the whole returned set before adopting anything. The
            # existing checkpoint is the sole commit/recovery boundary.
            for section_index, (section, draft) in enumerate(zip(remaining, joint), start=completed_count + 1):
                iterations.append(_iteration(section_index, section, "draft", draft.status, draft.used_sources, draft=draft))
                sections.append(draft)
                _record_draft_diagnostics(current, draft, [])
            completed_count = len(sections)
            checkpoint()
        for section_index, section in enumerate(_draft_sequence(current.section_plan), start=1):
            if section_index <= completed_count:
                continue
            if pending_draft is not None:
                draft = pending_draft
            else:
                # Long reports still need multiple evidence windows. Each window
                # revises the same complete section so the Writer can reconcile
                # new evidence without accumulating duplicate prose.
                source_batches = _source_batches(
                    section.evidence_handles,
                    config,
                )
                first_batch = source_batches[0] if source_batches else section.evidence_handles
                draft_section = _section_with_evidence(section, first_batch)
                _emit(emit, f"Writer drafting `{section.heading}`.")
                draft = _section_response(
                    client=client,
                    context=context,
                    template=template,
                    memory=current,
                    section=draft_section,
                    config=config,
                    extra_context=initial_evidence,
                    label=f"report-writer-{section.section_id}",
                    source_batch_index=1,
                    source_batch_count=len(source_batches),
                    adopted_sections=sections,
                    emit=emit,
                    source_front_matter=bibliography_planning_views(context, gateway.documents),
                )
                if gateway.documents is not None:
                    context, current = apply_report_bibliography(context, current, documents=gateway.documents,
                        notes=current.outline_planning.get("source_notes", []))
                    gateway.set_context(context)
                iterations.append(_iteration(section_index, section, "draft", draft.status, draft.used_sources, draft=draft))

                if config.source_strategy == "batch_refine" and len(source_batches) > 1:
                    for batch_index, batch in enumerate(source_batches[1:], start=2):
                        _emit(
                            emit,
                            (
                                f"Writer integrating source batch {batch_index}/"
                                f"{len(source_batches)} for `{section.heading}`."
                            ),
                        )
                        batch_section = _section_with_evidence(section, batch)
                        revised = _section_response(
                            client=client,
                            context=context,
                            template=template,
                            memory=current,
                            section=batch_section,
                            config=config,
                            extra_context=initial_evidence,
                            previous_draft=draft,
                            label=f"report-integrator-{section.section_id}-{batch_index}",
                            source_batch_index=batch_index,
                            source_batch_count=len(source_batches),
                            adopted_sections=sections,
                            emit=emit,
                            draft_mode="section_revision",
                        )
                        draft = _merge_revision_draft(draft, revised)
                        iterations.append(
                            _iteration(section_index, section, "integrate_sources", draft.status, draft.used_sources, draft=draft)
                        )
            # Inspect the current argument only after its body exists.
            # This is not a fabricated per-section pass: draft history remains
            # unreviewed until the existing document editor owns the findings.
            # A saved pending revision still finishes its original contract;
            # historical single-section documents keep their section reviewer.
            document_first = (config.review_scope == "document" and config.document_review
                and (len(current.section_plan) > 1 or review_contract) and pending_draft is None)
            if config.reviewer == "disabled" or document_first:
                sections.append(draft)
                _record_draft_diagnostics(current, draft, [])
                pending_draft = None
                checkpoint()
                continue

            section_findings: list[ReviewerFinding] = []
            pending_draft = draft
            checkpoint()
            review_context = next((row.tool_results for row in reversed(iterations)
                if row.section_id == section.section_id and row.action in {"revise", "review_context"}),
                [ReportToolResult.model_validate(report_tool_context(row, include_reading_notes=False))
                 for row in initial_evidence if row.tool_name != "get_synthesis_brief"])
            revision_request, revision_baseline = pending_revision_review(iterations, section.section_id)
            revisions_used = sum(row.action == "revise" for row in iterations if row.section_id == section.section_id)
            remaining_revisions = max(0, config.max_review_iterations - revisions_used)
            last_event = next((row for row in reversed(iterations)
                if row.section_id == section.section_id), None)
            saved_review = (ReportSectionReview(section_id=section.section_id,
                verdict=last_event.status, findings=last_event.findings,
                revision_instructions=last_event.revision_instructions)
                if last_event is not None and last_event.action in {"review", "review_revision"}
                and not any(row.type == "review_agent_fallback" for row in last_event.findings) else None)
            # A review pass is always recorded. Each allowed correction then
            # receives another review, so max_review_iterations means actual
            # review -> revise cycles rather than extra reviews without edits.
            for remaining_round in range(remaining_revisions + 1):
                review_round = revisions_used + remaining_round
                action = "review" if review_round == 0 else "review_revision"
                label_suffix = "" if review_round == 0 else f"-round-{review_round + 1}"
                _emit(emit, f"Reviewer checking `{section.heading}` (round {review_round + 1}).")
                reused_review = saved_review is not None and remaining_round == 0
                review = saved_review if reused_review else _section_response(
                    client=client,
                    context=context,
                    template=template,
                    memory=current,
                    section=section,
                    draft=draft,
                    config=config,
                    label=f"report-reviewer-{section.section_id}{label_suffix}",
                    emit=emit,
                    extra_context=review_context,
                    adopted_sections=sections,
                    revision_review=revision_request, previous_draft=revision_baseline,
                )
                tool_results = [] if reused_review else _run_context_requests(gateway, review, config)
                all_tool_results.extend(tool_results)
                lookup_failed = any(row.status in {"not_found", "error"} for row in tool_results)
                needs_evidence_recheck = not reused_review and (_needs_evidence_recheck(review) or lookup_failed)
                if tool_results or needs_evidence_recheck:
                    # Save completed reads before either consumer can fail.
                    # Mixed corrections skip the extra review, not persistence.
                    iterations.append(_iteration(section_index, section, "review_context", review.verdict,
                        draft.used_sources, findings=review.findings, tool_results=[*review_context, *tool_results]))
                    all_findings.extend(review.findings)
                    checkpoint()
                if needs_evidence_recheck:
                    # A pass that asks for missing evidence is provisional.
                    # A rejected draft with a failed lookup also needs one
                    # bounded query correction before spending a prose edit.
                    # Successful mixed corrections keep the direct path.
                    if tool_results:
                        review = _section_response(client=client, context=context, template=template,
                            memory=current, section=section, draft=draft, config=config,
                            label=f"report-reviewer-{section.section_id}{label_suffix}-evidence", emit=emit,
                            extra_context=[*review_context, *tool_results], adopted_sections=sections,
                            revision_review=revision_request, previous_draft=revision_baseline)
                        # Do not retry a denied/exhausted tool. A successful
                        # verification-only read keeps its existing one-recheck
                        # contract; only a failed lookup gets query repair.
                        corrected_results = _run_context_requests(gateway, review, config) if lookup_failed else []
                        tool_results.extend(corrected_results)
                        all_tool_results.extend(corrected_results)
                        if corrected_results:
                            iterations.append(_iteration(section_index, section, "review_context", review.verdict,
                                draft.used_sources, findings=review.findings, tool_results=[*review_context, *tool_results]))
                            checkpoint()
                    review = _mark_pending_evidence(review)
                # A resumed reviewer may need no new read. Its saved evidence
                # still belongs to the Writer and the next candidate checkpoint;
                # only new reads enter all_tool_results and consume allowance.
                review_context = [*review_context, *tool_results]
                section_findings.extend(review.findings)
                if not reused_review:
                    all_findings.extend(review.findings)
                    iterations.append(
                        _iteration(
                            section_index,
                            section,
                            action,
                            review.verdict,
                            draft.used_sources,
                            findings=review.findings,
                            tool_results=tool_results,
                            revision_instructions=review.revision_instructions,
                        )
                    )
                # Persist the completed opinion before revision can fail its
                # budget/transport check. Resume reuses this event, not another
                # review request or a fresh source-read allowance.
                checkpoint()
                if not _needs_revision(review) or remaining_round >= remaining_revisions:
                    break

                _emit(
                    emit,
                    f"Writer applying reviewer findings to `{section.heading}` (revision {review_round + 1}).",
                )
                revised = _section_response(
                    client=client,
                    context=context,
                    template=template,
                    memory=current,
                    section=section,
                    config=config,
                    previous_draft=draft,
                    review=review,
                    extra_context=review_context,
                    label=f"report-reviser-{section.section_id}-round-{review_round + 1}",
                    adopted_sections=sections,
                    emit=emit,
                )
                revision_request, revision_baseline = review, draft
                draft = _merge_revision_draft(draft, revised)
                iterations.append(
                    _iteration(
                        section_index,
                        section,
                        "revise",
                        draft.status,
                        draft.used_sources,
                        draft=draft,
                        tool_results=review_context,
                    )
                )
                pending_draft = draft
                checkpoint()

            sections.append(draft)
            _record_draft_diagnostics(current, draft, section_findings)
            pending_draft = None
            checkpoint()

        # Audit the latest reviewed draft, not the union of issues from every
        # superseded draft. Iterations/all_findings retain the complete history.
        if not document_review_done and not any(row.action.startswith("document_") for row in iterations):
            # Once document work owns the saved findings, even an unfinished
            # check cannot be replaced by earlier per-section opinions on resume.
            latest: dict[str, list[ReviewerFinding]] = {}
            for record in iterations:
                if record.action not in {"review", "review_revision"}:
                    continue
                if any(finding.type == "review_agent_fallback" for finding in record.findings):
                    latest.setdefault(record.section_id, [
                        finding for finding in current.reviewer_findings if finding.section_id == record.section_id
                    ]).extend(record.findings)
                else:
                    latest[record.section_id] = list(record.findings)
            current.reviewer_findings = _dedupe_findings(
                [finding for finding in current.reviewer_findings if finding.section_id not in latest]
                + [finding for findings in latest.values() for finding in findings]
            )
        if (config.document_review and config.reviewer != "disabled"
                and sections and (len(sections) > 1 or review_contract) and not document_review_done):
            document_review_done = _edit_whole_document(
                client=client, context=context, template=template, memory=current,
                config=config, sections=sections, iterations=iterations,
                all_findings=all_findings, emit=emit,
                checkpoint=checkpoint, gateway=gateway, all_tool_results=all_tool_results,
                current_draft_review=review_contract == 'current_draft.v1',
                on_delivery_review=on_delivery_review if delivery_contract else None,
            )
        # Final section-review reconciliation also matters when whole-document
        # review is disabled or inapplicable. Persist the memory we return,
        # not the earlier append-only history from the last section checkpoint.
        checkpoint()
        body = ""
        if assemble_body:
            title = current.document_plan.title if current.document_plan and current.document_plan.title else context.topic
            body = assemble_report_sections(title=title, sections=_final_sequence(current.section_plan, sections))
        independent_review = None
        pending_candidate = None
        if delivery_contract:
            identity = _section_delivery_assembly(context, current,
                _final_sequence(current.section_plan, sections), config)['identity']
            independent_review = _matching_section_delivery_review(iterations, identity)
            pending_candidate = _pending_section_delivery_candidate(
                context, current, config, iterations, adopted_identity=identity)
        return AgentReportResult(
            report_body=body,
            memory=current,
            sections=sections,
            iterations=iterations,
            reviewer_findings=all_findings,
            tool_results=all_tool_results,
            used_agent=True,
            independent_review=independent_review,
            pending_candidate=pending_candidate,
        )
    except (LLMError, ValidationError, ValueError) as exc:
        if not config.allow_llm_fallback:
            raise LLMError(
                f"Report agent failed validation and LLM fallback is disabled: {exc}"
            ) from exc
        _emit(emit, f"Report agent failed validation; using explicit offline fallback. {exc}")
        return None


def _matching_section_delivery_review(
    iterations: list[ReportIterationRecord], identity: dict[str, Any],
) -> dict[str, Any] | None:
    return next((row.independent_review for row in reversed(iterations)
        if row.action in {'document_current_review', 'document_joint_verify'} and row.independent_review
        and row.independent_review.get('assembly_identity') == identity), None)


def _last_section_proposal(iterations: list[ReportIterationRecord]) -> tuple:
    """Reconstruct one saved proposal, including inherited section edits."""
    baseline, proposal, verification = [], None, None
    for event in iterations:
        if event.action == 'document_current_review':
            baseline = list(event.drafts)
            proposal = verification = None
        elif event.action == 'document_joint_revise':
            proposal, verification = event, None
            updates = {row.section_id: row for row in event.drafts}
            if (not baseline or len(updates) != len(event.drafts)
                    or not set(updates).issubset({row.section_id for row in baseline})):
                baseline = []
            else:
                baseline = [updates.get(row.section_id, row) for row in baseline]
        elif event.action == 'document_joint_verify' and proposal is not None:
            verification = event
    return baseline, proposal, verification


def _pending_section_delivery_candidate(
    context: ReportContext, memory: ReportMemory, config: ReportRuntimeConfig,
    iterations: list[ReportIterationRecord], *, adopted_identity: dict[str, Any],
) -> dict[str, Any] | None:
    """Project the latest proposal's persisted negative proof, without adoption.

    Joint events save only edited targets. Rebuild the verifier's complete
    baseline, including changes inherited from earlier rejected proposals.
    Receipt paths and hashes remain exactly as retained by the inspection owner.
    """
    baseline, proposal, verification = _last_section_proposal(iterations)
    if (proposal is None or proposal.adopted is not False or proposal.status != 'rejected'
            or not proposal.drafts or not baseline or verification is None
            or verification.status not in {'completed', 'failed'}):
        return None
    ordered = _final_sequence(memory.section_plan, baseline)
    ids = {row.section_id for row in ordered}
    reviews = verification.section_reviews
    if (len(ids) != len(ordered) or len(reviews) != len(ordered)
            or {row.section_id for row in reviews} != ids
            or any(row.context_requests for row in reviews)):
        return None
    assembly = _section_delivery_assembly(context, memory, ordered, config)
    identity = assembly['identity']
    review = _matching_section_delivery_review([verification], identity)
    if (identity == adopted_identity or not review or review.get('review_status') != 'needs_revision'
            or review.get('section_reviews') != [row.model_dump(mode='json') for row in reviews]
            or set(review.get('inspection_source') or {}) != {
                'request_path', 'request_sha256', 'response_path', 'response_sha256'}
            or set(review.get('inspection_payload_sha256') or {}) != {'request', 'response'}):
        return None
    delivery = review.get('delivery_review', {})
    from simple_ar.report.review_evidence import delivery_review_result
    if (delivery.get('status') not in {'reviewed', 'needs_revision'}
            or not any(_needs_revision(row) for row in reviews)
            or delivery.get('uninspected')
            or delivery != delivery_review_result(context.results['delivery_review'], reviews)):
        return None
    return {'sections': [row.model_dump(mode='json') for row in ordered],
        'report_body_markdown': assembly['report_body_markdown'],
        'assembly_identity': identity, 'independent_review': review,
        'revision_iteration': proposal.iteration, 'verification_iteration': verification.iteration}


def _edit_whole_document(
    *, client: LLMClient, context: ReportContext, template: ReportTemplateBundle,
    memory: ReportMemory, config: ReportRuntimeConfig, sections: list[ReportSectionDraft],
    iterations: list[ReportIterationRecord], all_findings: list[ReviewerFinding],
    emit: Callable[[str], None] | None,
    checkpoint: Callable[[], None], gateway: ReportToolGateway,
    all_tool_results: list[ReportToolResult],
    current_draft_review: bool = False,
    on_delivery_review: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> bool:
    """Correct frozen targets within their configured, recoverable allowance."""
    review_finished = True
    plans = {plan.section_id: plan for plan in memory.section_plan}
    by_id = {draft.section_id: index for index, draft in enumerate(sections)}
    pending = pending_document_revisions(iterations)
    continuations = pending_document_revisions(iterations, include_rejected=True)
    joint = [row for row in iterations if row.action == "document_joint_revise"]
    pending_joint = joint[-1] if joint and joint[-1].status == "drafted" and not joint[-1].adopted else None
    current_review = next((row for row in reversed(iterations)
                           if row.action == 'document_current_review'), None) if current_draft_review else None
    if current_review is not None and current_review.drafts != sections:
        adopted = {row.section_id: row for row in current_review.drafts}
        matched_adoption = False
        for row in iterations[current_review.iteration:]:
            if row.action == 'document_joint_revise':
                adopted.update((draft.section_id, draft) for draft in row.drafts)
                if row.adopted and row.status == 'verified' and list(adopted.values()) == sections:
                    matched_adoption = True
        if not matched_adoption:
            raise ValueError('Saved current-document inspection does not match the adopted manuscript.')
        # A later accepted candidate has its own inspection. If the caller
        # explicitly requests another pass, do not reuse the initial verdict.
        current_review = None
    # An interrupted document lookup ends in context events without a review.
    # Reuse that suffix for its unfinished inspection, not older roles' context.
    # A saved section candidate retains its own verification path below.
    pending_contexts: dict[str, list[ReportToolResult]] = {}
    pending_rejections: dict[str, ReportIterationRecord] = {}
    pending_inspection: list[ReportSectionReview] = []
    pending_opinions: list[ReviewerFinding] | None = None
    partial_review_owners: set[str] = set()
    completed_opinion_reviews: list[ReportSectionReview] = []
    latest_delivery_review: dict[str, Any] | None = None
    if not continuations:
        owners = {"document_context": "report-document-reviewer",
                  "document_finding_context": "report-document-finding-checker",
                  "document_verifier_context": "report-document-verifier"}
        for event in reversed(iterations):
            if event.action == "document_finding_check":
                completed_opinion_reviews[0:0] = event.section_reviews or [ReportSectionReview(
                    section_id=event.section_id, verdict=event.status, findings=event.findings,
                    finding_checks=event.finding_checks)]
            elif event.action in {"document_review_rejected", "document_finding_check_rejected",
                                "document_format_correction", "document_finding_format_correction"}:
                owner = event.rejected_review.get("label", "")
                if owner:
                    if completed_opinion_reviews and owner.startswith("report-document-finding-checker"):
                        continue  # Applied checks already own closure and retained opinions.
                    pending_rejections.setdefault(owner, event)
                    if owner.startswith("report-document-finding-checker") and pending_opinions is None:
                        pending_opinions = event.requested_findings or [ReviewerFinding.model_validate(row)
                            for row in event.rejected_review.get("historical_findings", [])] or None
            elif event.action in owners:
                pending_contexts.setdefault(owners[event.action], [])[0:0] = event.tool_results
                if event.action == "document_finding_context" and pending_opinions is None:
                    pending_opinions = event.requested_findings or None
            elif event.action == "document_inspection" and (
                    completed_opinion_reviews or "report-document-finding-checker" in pending_contexts or any(
                        owner.startswith("report-document-finding-checker") for owner in pending_rejections)):
                pending_inspection.insert(0, ReportSectionReview(section_id=event.section_id,
                    verdict=event.status, findings=event.findings, revision_instructions=event.revision_instructions))
            else:
                break

    def correction_consumed(label: str) -> bool:
        event = next((row for row in reversed(iterations)
                      if row.rejected_review.get("label", "").removesuffix("-evidence") == label), None)
        return bool(event and event.rejected_review.get("format_correction")
                    and not (event.status == "started" and "rejection_iteration" in event.rejected_review))

    def checkpointed_context(requests: list[ReportToolCall], event: ReportIterationRecord) -> None:
        checkpointed_report_reads(gateway, requests, event, all_tool_results, checkpoint)

    def retain_partial_review_failure(label: str, reason: str) -> None:
        partial_review_owners.add(label)
        identifier = {"report-document-reviewer": "document-review-unavailable",
                      "report-document-finding-checker": "document-finding-check-unavailable",
                      "report-document-verifier": "document-recheck-unavailable"}[label]
        finding = ReviewerFinding(finding_id=identifier, type="document_review_unavailable", severity="major",
            message=f"Review remains incomplete; only individually validated observations were retained: {reason}",
            suggested_action="Inspect the retained rejected response and unresolved review before publication.")
        all_findings.append(finding)
        memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, finding])
        checkpoint()

    def checked_document_reviews(label: str, *, historical_findings: list[ReviewerFinding] | None = None,
                                 complete_candidate: bool = False,
                                 candidate_sections: list[ReportSectionDraft] | None = None) -> list[ReportSectionReview]:
        nonlocal latest_delivery_review
        latest_delivery_review = None
        inspected = sections if candidate_sections is None else candidate_sections
        requested_context = list(pending_contexts.get(label, []))
        def prepare_rejected_context(event: ReportIterationRecord) -> None:
            if not config.allow_source_backtracking:
                return
            requests = rejected_review_context_requests(event.rejected_review.get("response"),
                section_ids={row.section_id for row in historical_findings} if historical_findings else set(by_id),
                max_requests=config.max_backtracking_calls,
                read_only_tools={name for name, spec in gateway.specs.items() if set(spec.permissions) == {"read"}})
            recorded = [row.model_dump(mode="json") for row in requests]
            if event.rejected_review.get("context_requests", recorded) != recorded:
                raise ValueError("Saved rejected-review lookup scope differs from the current registered scope.")
            event.rejected_review["context_requests"] = recorded
            checkpointed_context(requests, event)
            requested_context.extend(row for row in event.tool_results if row not in requested_context)

        def call(review_label: str) -> list[ReportSectionReview]:
            rejected = pending_rejections.pop(review_label, None)
            saved_correction = None
            if (rejected and rejected.status == "started"
                    and rejected.rejected_review.get("format_correction")
                    and "rejection_iteration" in rejected.rejected_review):
                # A checkpoint records intent before budget/transport. Only a
                # returned invalid response consumes the bounded correction.
                rejection_iteration = rejected.rejected_review["rejection_iteration"]
                parent = next((row for row in iterations if row.iteration == rejection_iteration), None)
                if (parent is None or parent.rejected_review.get("label") != review_label
                        or "response" not in parent.rejected_review
                        or parent.rejected_review.get("format_correction")):
                    raise LLMResponseError("Saved format correction has no matching rejected response; no replay is authorized.")
                saved_correction = rejected
                rejected = parent
            if rejected and rejected.status == "completed" and "validated_reviews" in rejected.rejected_review:
                return [ReportSectionReview.model_validate(row) for row in rejected.rejected_review["validated_reviews"]]
            if rejected and rejected.rejected_review.get("partial_ready"):
                retain_partial_review_failure(label, rejected.summary)
                return [ReportSectionReview.model_validate(row) for row in rejected.rejected_review["partial_reviews"]]
            if rejected and rejected.rejected_review.get("format_correction"):
                raise LLMResponseError(f"Document review format correction was already consumed: {rejected.summary}")
            if rejected is not None:
                prepare_rejected_context(rejected)
            correction = {"validation_error": rejected.summary,
                          "rejected_response": rejected.rejected_review["response"]} if rejected else None
            validated_subset: list[ReportSectionReview] = []
            def capture_validated_subset(rows: list[ReportSectionReview]) -> None:
                nonlocal validated_subset
                validated_subset = rows
            def retain_rejection(response: Any, reason: str) -> None:
                iterations.append(ReportIterationRecord(
                    iteration=len(iterations) + 1, section_id="",
                    action="document_finding_check_rejected" if historical_findings else "document_review_rejected",
                    status="rejected", summary=reason,
                    rejected_review={"label": review_label, "response": response,
                                     "format_correction": correction is not None,
                                     **({"partial_reviews": [row.model_dump(mode="json") for row in validated_subset],
                                         "partial_ready": correction is not None} if validated_subset else {})},
                    requested_findings=historical_findings or [],
                ))
                checkpoint()
            ordered = _final_sequence(memory.section_plan, inspected)
            additions = supplied_data_delivery(context, config=config, plan=memory.document_plan,
                section_ids=[row.section_id for row in ordered])
            from simple_ar.report.capability import supplied_caption_blocks
            caption_blocks = supplied_caption_blocks(context,
                '\n\n'.join(row.draft_markdown for row in ordered))
            delivery = delivery_text_observation(context, memory, ordered, config, additions=additions)
            delivery.update(prior_document_revision(context, memory, include_baseline=True))
            assembly = _section_delivery_assembly(context, memory, ordered, config) if on_delivery_review else None
            def retain_delivery_review(observation: dict[str, Any]) -> None:
                nonlocal latest_delivery_review
                # The callback must resolve real persisted request/response files.
                # A checkpoint is history, never a fabricated transport receipt.
                payload_hashes = {name: hashlib.sha256(json.dumps(
                    observation[name], sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                    allow_nan=False).encode('utf-8')).hexdigest() for name in ('request', 'response')}
                source = on_delivery_review({**observation, 'inspection_payload_sha256': payload_hashes})
                if not isinstance(source, dict) or set(source) != {
                        'request_path', 'request_sha256', 'response_path', 'response_sha256'}:
                    raise ValueError('Integrated review callback must return actual transport receipt paths and hashes')
                verdict = {key: observation[key] for key in ('review_status', 'section_reviews', 'delivery_review')}
                verdict.update(inspection_source=dict(source), assembly_identity=assembly['identity'])
                # Hash the actual primitive payloads, not filenames or a verdict
                # reconstructed from the checkpoint. The receipt consumer must
                # recompute these from json.loads(request['prompt']) and response.
                verdict['inspection_payload_sha256'] = payload_hashes
                latest_delivery_review = verdict
            for _ in range(2 if correction is None else 1):
                validated_subset = []
                before = len(iterations)
                correction_record = saved_correction
                if correction is not None:
                    if correction_record is None:
                        correction_record = ReportIterationRecord(
                            iteration=len(iterations) + 1, section_id="",
                            action="document_finding_format_correction" if historical_findings else "document_format_correction",
                            status="started", summary=correction["validation_error"],
                            rejected_review={"label": review_label, "format_correction": True,
                                "rejection_iteration": rejected.iteration},
                            requested_findings=historical_findings or [],
                        )
                        iterations.append(correction_record)
                    checkpoint()
                try:
                    validated = review_document(client=client, template=template, memory=memory,
                        sections=ordered, config=config,
                        execution_summary=_compact_execution_results(context.results),
                        execution_evidence=report_execution_evidence(context), supplementary_evidence=all_tool_results,
                        requested_context=requested_context, historical_findings=historical_findings,
                        complete_candidate=complete_candidate or assembly is not None,
                        on_invalid_response=retain_rejection, format_correction=correction,
                        on_validated_subset=capture_validated_subset,
                        metric_summary=_prompt_metrics(memory, detail="summary"),
                        source_evidence=[_prompt_handle_view(row) for row in memory.source_handles if row.kind in {"paper", "material"}],
                        writing_objective=report_objective(context, memory),
                        assembly_owned_content=additions + caption_blocks + _experiment_delivery_view(context, config),
                        delivery_text_observation=delivery,
                        delivery_review=context.results['delivery_review'] if assembly is not None else None,
                        delivery_assembly=assembly,
                        evidence_navigation_contract=memory.survey_contract.get('evidence_navigation_contract'),
                        on_delivery_review=retain_delivery_review if assembly is not None else None,
                        label=review_label + "-format-correction" if correction is not None else review_label)
                    if correction_record is not None:
                        correction_record.status = "completed"
                        correction_record.rejected_review["validated_reviews"] = [row.model_dump(mode="json") for row in validated]
                        checkpoint()
                    partial_review_owners.discard(label)
                    return validated
                except (LLMResponseError, ValidationError):
                    # Only a parsed answer saved by this owner permits correction.
                    # Transport, budget, and decoding failures remain separate.
                    if (correction is not None and len(iterations) > before
                            and iterations[-1].rejected_review.get("partial_ready")):
                        retain_partial_review_failure(label, iterations[-1].summary)
                        return validated_subset
                    if correction is not None or len(iterations) == before or not iterations[-1].rejected_review:
                        raise
                    prepare_rejected_context(iterations[-1])
                    rejected = iterations[-1]
                    correction = {"validation_error": rejected.summary,
                                  "rejected_response": rejected.rejected_review["response"]}
                    _emit(emit, "Reviewer correcting rejected document-review JSON once.")
            raise AssertionError("Document review correction did not terminate")

        if requested_context or label + "-evidence" in pending_rejections:
            rows = call(label + "-evidence")
            return rows if historical_findings else [_mark_pending_evidence(row) for row in rows]
        reviews = call(label)
        # This role checks opinions, not current assertions needing a prose
        # qualification. Its evidence requests belong here even if the model
        # calls the old opinion a required correction.
        provisional = [row for row in reviews if (bool(row.context_requests) if historical_findings
                                                  else _needs_evidence_recheck(row))]
        if not provisional:
            return reviews
        fetched = []
        for review in provisional:
            event = _iteration(len(iterations) + 1, plans[review.section_id],
                "document_finding_context" if historical_findings else
                    "document_verifier_context" if label == "report-document-verifier" else "document_context",
                review.verdict, inspected[by_id[review.section_id]].used_sources,
                findings=review.findings, requested_findings=historical_findings)
            iterations.append(event)
            all_findings.extend(review.findings)
            requests = [call for call in review.context_requests[:max(0, config.max_backtracking_calls)]
                        if call.tool_name] if config.allow_source_backtracking else []
            checkpointed_context(requests, event)
            fetched.extend(event.tool_results)
        requested_context.extend(fetched)
        checkpoint()
        if fetched:
            rows = call(label + "-evidence")
            return rows if historical_findings else [_mark_pending_evidence(row) for row in rows]
        if historical_findings:
            return reviews  # Pending requests gate closure/dispatch below.
        return [_mark_pending_evidence(row) if row in provisional else row for row in reviews]

    try:
        recovering_opinions = (bool(completed_opinion_reviews) or "report-document-finding-checker" in pending_contexts or
                              any(owner.startswith("report-document-finding-checker") for owner in pending_rejections))
        recovering_verifier = ("report-document-verifier" in pending_contexts or
                              any(owner.startswith("report-document-verifier") for owner in pending_rejections))
        if pending_joint is not None:
            reviews = pending_joint.section_reviews
        elif current_review is not None:
            reviews = current_review.section_reviews
        elif recovering_opinions or recovering_verifier:
            reviews = pending_inspection
        else:
            _emit(emit, "Reviewer checking whole-document coherence.")
            previous_opinions = [row for row in memory.reviewer_findings
                if row.section_id in plans and row.type not in DOCUMENT_CONTROL_FINDING_TYPES]
            reviews = checked_document_reviews("report-document-reviewer",
                complete_candidate=current_draft_review and bool(previous_opinions))
            if current_draft_review:
                if (previous_opinions and ({row.section_id for row in reviews} != set(by_id)
                        or any(row.context_requests for row in reviews)
                        or 'report-document-reviewer' in partial_review_owners)):
                    raise LLMResponseError('Current-document inspection is incomplete; retain previous opinions and prose.')
                # Current observations supersede earlier opinions, not source
                # facts. Keep both in history; only a complete inspection may
                # retire previous opinions from the current delivery audit.
                if {row.section_id for row in reviews} == set(by_id):
                    all_findings.extend(row for row in previous_opinions if row not in all_findings)
                    memory.reviewer_findings = [row for row in memory.reviewer_findings if row not in previous_opinions]
                current_review = ReportIterationRecord(iteration=len(iterations) + 1, section_id='',
                    action='document_current_review', status='completed', drafts=list(sections),
                    section_reviews=reviews, requested_findings=previous_opinions,
                    independent_review=latest_delivery_review)
                iterations.append(current_review)
                for review in reviews:
                    all_findings.extend(review.findings)
                    memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
                checkpoint()
            if "report-document-reviewer" not in partial_review_owners:
                memory.reviewer_findings = [row for row in memory.reviewer_findings
                                           if row.finding_id != "document-review-unavailable"]
    except (LLMError, ValidationError, ValueError) as exc:
        finding = ReviewerFinding(
            finding_id="document-review-unavailable", type="document_review_unavailable",
            severity="major", message=f"Whole-document review did not complete: {exc}",
            suggested_action="Inspect cross-section coherence before publication.",
        )
        all_findings.append(finding)
        memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, finding])
        return correction_consumed("report-document-reviewer")
    adopted_revision = recovering_verifier
    # New observations about an interrupted candidate's target do not replace
    # its saved verification contract. Keep them as separate, unresolved work;
    # filtering that target out of the unsent queue must not erase the issues.
    for review in reviews:
        if review.section_id in continuations:
            iterations.append(_iteration(len(iterations) + 1, plans[review.section_id],
                "document_inspection", review.verdict, sections[by_id[review.section_id]].used_sources,
                findings=review.findings, revision_instructions=review.revision_instructions))
            all_findings.extend(review.findings)
            memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
    if any(review.section_id in continuations for review in reviews):
        checkpoint()
    # A fresh inspection cannot erase earlier required opinions by omission.
    # Check only eligible prior issues, separately from discovering new ones.
    # Pending candidates retain their original verification contract below.
    prior_findings = [row for row in memory.reviewer_findings
                      if finding_requires_resolution(row) and row.section_id in plans
                      and row.type not in DOCUMENT_CONTROL_FINDING_TYPES
                      and row.section_id not in continuations]
    if current_draft_review or recovering_verifier or pending_joint is not None or completed_opinion_reviews:
        prior_findings = []
    elif recovering_opinions and pending_opinions is not None:
        prior_findings = pending_opinions
    if prior_findings:
        # The independent inspection has completed. Save its findings before
        # checking prior opinions; a rejected answer/interruption cannot lose it.
        if not recovering_opinions:
            for review in reviews:
                if review.section_id in continuations:
                    continue  # Already retained above, outside the saved candidate contract.
                iterations.append(_iteration(len(iterations) + 1, plans[review.section_id],
                    "document_inspection", review.verdict, sections[by_id[review.section_id]].used_sources,
                    findings=review.findings, revision_instructions=review.revision_instructions))
                memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
            if reviews:
                checkpoint()
        try:
            _emit(emit, "Reviewer checking prior opinions against the current draft.")
            opinion_reviews = checked_document_reviews("report-document-finding-checker",
                                                       historical_findings=prior_findings)
            originals = historical_opinion_handles(prior_findings)
            closed = []
            checked = set()
            for review in opinion_reviews:
                retained = [originals[(review.section_id, check.finding_id)] for check in review.finding_checks
                            if check.status == "unresolved" or review.context_requests]
                if retained:
                    review.findings = _dedupe_findings([*review.findings, *retained])
                    # An unresolved verification can mean its premise is still
                    # unknown, not that the current prose must be rewritten.
                    # Keep that work active; preserve an explicit correction
                    # verdict or a recorded revise/legacy requirement instead.
                    if any(finding.required_action != "verify" for finding in retained):
                        review.verdict = "revise_required"
                iterations.append(_iteration(len(iterations) + 1, plans[review.section_id],
                    "document_finding_check", review.verdict, sections[by_id[review.section_id]].used_sources,
                    findings=review.findings, finding_checks=review.finding_checks,
                    requested_findings=prior_findings))
                iterations[-1].section_reviews = [review.model_copy(deep=True)]
                all_findings.extend(review.findings)
                closed.extend(originals[(review.section_id, check.finding_id)] for check in review.finding_checks
                              if check.status != "unresolved" and not review.context_requests)
                checked.update((review.section_id, check.finding_id) for check in review.finding_checks
                               if not review.context_requests)
            memory.reviewer_findings = [row for row in memory.reviewer_findings
                                       if row not in closed]
            if checked == set(originals) and "report-document-finding-checker" not in partial_review_owners:
                # This owner actually completed the failed operation; its old
                # service error is no longer current. Semantic issues stay active.
                memory.reviewer_findings = [row for row in memory.reviewer_findings
                                           if row.finding_id != "document-finding-check-unavailable"]
            # A check of an older opinion cannot erase a newly discovered issue.
            for review in reviews:
                memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
            # Newly discovered problems are not removed by a check of old ones.
            for review in opinion_reviews:
                memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
            checkpoint()
            completed_opinion_reviews.extend(opinion_reviews)
        except (LLMError, ValidationError, ValueError) as exc:
            review_finished = correction_consumed("report-document-finding-checker")
            finding = ReviewerFinding(finding_id="document-finding-check-unavailable",
                type="document_review_unavailable", severity="major",
                message=f"Historical opinions were not checked: {exc}",
                suggested_action="Inspect retained opinions against current draft and original evidence.")
            all_findings.append(finding)
            memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, finding])
    reviews.extend(review for review in completed_opinion_reviews if not review.context_requests and (
        review.verdict in {"revise_required", "fail"}
        or any(finding_requires_resolution(finding) and finding.required_action != "verify"
               for finding in review.findings)
    ))
    # Finish persisted candidates first; a fresh review cannot change the
    # correction contract midway or cause the Writer to regenerate that draft.
    # Inspection coverage is not the correction allowance. Keep every finding,
    # and spend the frozen-plan allowance on required, severe defects.
    # Persisted contracts still take precedence on recovery.
    severity = {"info": 0, "minor": 1, "major": 2, "critical": 3}
    def priority(review: ReportSectionReview) -> tuple[bool, int]:
        required = [finding for finding in review.findings if finding_requires_resolution(finding)]
        return (bool(required), max((severity[finding.severity] for finding in required), default=0))
    unsent = coalesce_document_reviews([review for review in reviews if review.section_id not in continuations])
    reviews = [*(request for _, request in continuations.values()),
               *sorted(unsent, key=priority, reverse=True)]
    if config.draft_scope in {"document", "auto"} and not continuations:
        initial_inspection = current_review.independent_review if current_review and on_delivery_review else None
        if initial_inspection and attachment_review_blocks_editing(initial_inspection['delivery_review']):
            return review_finished

        def joint_draft(event: ReportIterationRecord, baseline: list[ReportSectionDraft]) -> list[ReportSectionDraft]:
            requests = list({call.model_dump_json(): call for review in event.section_reviews
                for call in review.context_requests[:max(0, config.max_backtracking_calls)]}.values())
            if config.allow_source_backtracking:
                checkpointed_context(requests, event)
            _emit(emit, f"Writer jointly revising {len(event.section_reviews)} section(s).")
            return _draft_document_with_recovery(client=client, context=context, template=template,
                memory=memory, config=config.model_copy(update={"allow_llm_fallback": False}),
                sections=[plans[row.section_id] for row in event.section_reviews], adopted_sections=baseline,
                extra_context=event.tool_results, reviews=event.section_reviews, emit=emit,
                read_context=lambda requests: _writer_evidence_reads(owner=f"revision_{event.iteration}",
                    requests=requests, gateway=gateway, iterations=iterations,
                    all_results=all_tool_results, checkpoint=checkpoint))

        def joint_inspect(candidate: list[ReportSectionDraft], prior: list[ReviewerFinding] | None) -> list[ReportSectionReview]:
            rows = checked_document_reviews("report-document-verifier",
                complete_candidate=True, candidate_sections=candidate)
            if {row.section_id for row in rows} != {row.section_id for row in candidate}:
                raise LLMResponseError("Candidate inspection is incomplete; retain the draft without adopting it.")
            return rows

        edit_joint_document(memory=memory, config=config, sections=sections, iterations=iterations,
            reviews=reviews, all_findings=all_findings, checkpoint=checkpoint,
            draft=joint_draft, inspect=joint_inspect,
            inspection_review=(lambda: latest_delivery_review) if on_delivery_review else None)
        return review_finished and not any(row.type == "document_revision_unavailable"
                                           for row in memory.reviewer_findings)
    # This bounded queue also handles a new defect introduced by a correction.
    # A candidate is not adopted until it passes; subsequent corrections retain
    # the original contract and consume the same section allowance.
    while reviews:
        review = reviews.pop(0)
        plan = plans[review.section_id]
        index = by_id[review.section_id]
        original = sections[index]
        all_findings.extend(review.findings)
        iterations.append(_iteration(len(iterations) + 1, plan, "document_review",
            review.verdict, original.used_sources, findings=review.findings,
            revision_instructions=review.revision_instructions))
        if not _needs_revision(review) or config.max_review_iterations == 0:
            memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings])
            continue
        try:
            # An optional editor must never replace an already reviewed section
            # with the generic writer/reviewer fallback after a provider error.
            persisted = pending.get(plan.section_id)
            prior_candidates = [row for row in iterations if row.action == "document_revise"]
            targets = {row.section_id for row in prior_candidates}
            attempts_used = sum(row.section_id == plan.section_id for row in prior_candidates)
            if persisted is None and (attempts_used >= config.max_review_iterations
                    or (plan.section_id not in targets and len(targets) >= document_revision_limit(memory))):
                finding = ReviewerFinding(finding_id=f"{plan.section_id}-document-revision-budget",
                    type="document_revision_unresolved", severity="major", section_id=plan.section_id,
                    message="The editor correction allowance was already consumed; recovery does not grant another draft.",
                    suggested_action="Inspect the retained candidates and unresolved review before publication.")
                all_findings.append(finding)
                memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, *review.findings, finding])
                continue
            previous = continuations.get(plan.section_id)
            tool_results = list(previous[0].tool_results) if previous else []
            if previous:
                candidate_index = iterations.index(previous[0])
                for event in iterations[candidate_index + 1:]:
                    if event.section_id == plan.section_id and event.action == "document_context":
                        tool_results.extend(result for result in event.tool_results if result not in tool_results)
            if persisted is None:
                fetched = _run_context_requests(gateway, review, config)
                tool_results.extend(fetched)
                all_tool_results.extend(fetched)
            strict = config.model_copy(update={"allow_llm_fallback": False})
            if persisted:
                candidate_record, revised = persisted[0], persisted[0].draft
            else:
                _emit(emit, f"Writer revising `{plan.heading}` for document coherence.")
                revised = _section_response(
                    client=client, context=context, template=template, memory=memory,
                    section=plan, config=strict, extra_context=tool_results,
                    previous_draft=previous[0].draft if previous else original,
                    review=review, label=f"report-document-reviser-{plan.section_id}",
                    draft_mode="section_revision", emit=emit, adopted_sections=sections,
                )
                iterations.append(_iteration(len(iterations) + 1, plan, "document_revise",
                    revised.status, revised.used_sources, draft=revised, tool_results=tool_results))
                candidate_record = iterations[-1]
                candidate_record.adopted = False
                checkpoint()
            _emit(emit, f"Reviewer verifying revised `{plan.heading}`.")
            verification = _section_response(
                client=client, context=context, template=template, memory=memory,
                section=plan, draft=revised, config=strict,
                label=f"report-document-verifier-{plan.section_id}", emit=emit,
                extra_context=tool_results,
                adopted_sections=sections,
                revision_review=review, previous_draft=original,
            )
            if verification.context_requests:
                fetched = _run_context_requests(gateway, verification, config)
                tool_results.extend(fetched)
                all_tool_results.extend(fetched)
                if fetched:
                    iterations.append(_iteration(len(iterations) + 1, plan, "document_context",
                        verification.verdict, revised.used_sources, tool_results=fetched))
                    checkpoint()
                if fetched and _needs_evidence_recheck(verification):
                    verification = _section_response(client=client, context=context, template=template,
                        memory=memory, section=plan, draft=revised, config=strict,
                        label=f"report-document-verifier-{plan.section_id}-evidence", emit=emit,
                        extra_context=tool_results, adopted_sections=sections,
                        revision_review=review, previous_draft=original)
                verification = _mark_pending_evidence(verification)
            all_findings.extend(verification.findings)
            iterations.append(_iteration(len(iterations) + 1, plan, "document_verify",
                verification.verdict, revised.used_sources, findings=verification.findings,
                revision_instructions=verification.revision_instructions))
            pending.pop(plan.section_id, None)
            if _needs_revision(verification):
                unresolved = verification.findings or [ReviewerFinding(
                    finding_id=f"{plan.section_id}-document-verification-failed",
                    type="document_revision_unresolved", severity="major",
                    message="The whole-document revision did not pass section verification.",
                    section_id=plan.section_id,
                )]
                memory.reviewer_findings = _dedupe_findings([
                    *memory.reviewer_findings, *review.findings, *unresolved,
                ])
                # Persist the rejection before another API call. Recovery may
                # continue this draft, but does not regenerate it or reset quota.
                checkpoint()
                if attempts_used + (persisted is None) < config.max_review_iterations:
                    continuation = pending_document_revisions(iterations, include_rejected=True)[plan.section_id]
                    continuations[plan.section_id] = continuation
                    reviews.insert(0, continuation[1])
                continue
            sections[index] = revised
            candidate_record.adopted = True
            adopted_revision = True
            memory.reviewer_findings = [
                finding for finding in memory.reviewer_findings if finding not in review.findings
            ]
            _record_draft_diagnostics(memory, revised, verification.findings)
            # Refresh before another section Writer or the immediate whole-document
            # recheck, not only when the entire editing pass finishes.
            checkpoint()
        except (LLMError, ValidationError, ValueError) as exc:
            review_finished = False
            unresolved = ReviewerFinding(
                finding_id=f"{plan.section_id}-document-revision-unavailable",
                type="document_revision_unavailable", severity="major",
                message=f"Whole-document finding for {plan.heading} was not resolved: {exc}",
                section_id=plan.section_id,
                suggested_action="Inspect the original section and the document review finding.",
            )
            all_findings.append(unresolved)
            memory.reviewer_findings = _dedupe_findings([
                *memory.reviewer_findings, *review.findings, unresolved,
            ])
    if adopted_revision:
        try:
            _emit(emit, "Reviewer rechecking whole-document coherence after revision.")
            verification_reviews = checked_document_reviews("report-document-verifier")
            if "report-document-verifier" not in partial_review_owners:
                memory.reviewer_findings = [row for row in memory.reviewer_findings
                                           if row.finding_id != "document-recheck-unavailable"]
            for review in verification_reviews:
                plan = plans[review.section_id]
                unresolved = review.findings or ([ReviewerFinding(
                    finding_id=f"{plan.section_id}-document-recheck-unresolved",
                    type="document_revision_unresolved", severity="major",
                    message="The final whole-document recheck still requested revision.",
                    section_id=plan.section_id,
                )] if _needs_revision(review) else [])
                all_findings.extend(unresolved)
                iterations.append(_iteration(len(iterations) + 1, plan, "document_recheck",
                    review.verdict, sections[by_id[review.section_id]].used_sources,
                    findings=unresolved))
                memory.reviewer_findings = _dedupe_findings([
                    *memory.reviewer_findings, *unresolved,
                ])
        except (LLMError, ValidationError, ValueError) as exc:
            review_finished = review_finished and correction_consumed("report-document-verifier")
            unavailable = ReviewerFinding(
                finding_id="document-recheck-unavailable", type="document_review_unavailable",
                severity="major", message=f"Whole-document recheck did not complete: {exc}",
                suggested_action="Inspect the revised report before publication.",
            )
            all_findings.append(unavailable)
            memory.reviewer_findings = _dedupe_findings([*memory.reviewer_findings, unavailable])
    # Completion records the bounded operation, not a successful assessment.
    # Rejected peers and service diagnostics remain visible in the same memory.
    return review_finished


def _maybe_adapt_outline(
    *,
    client: LLMClient,
    context: ReportContext,
    template: ReportTemplateBundle,
    memory: ReportMemory,
    config: ReportRuntimeConfig,
    emit: Callable[[str], None] | None,
    documents: DocumentBundle | None = None,
    extra_context: list[ReportToolResult] | None = None,
) -> ReportMemory:
    contract = memory.survey_contract if isinstance(memory.survey_contract, dict) else {}
    if memory.document_plan is not None:
        return memory
    if config.template not in {"", "auto", *BUILTIN_TEMPLATE_NAMES}:
        return memory
    survey = is_survey_report(
        template_name=template.name, style=config.style, report_mode=context.report_mode,
    )
    strategy = str(contract.get("outline_strategy") or config.outline_strategy or "auto").lower()
    if strategy == "template":
        return memory
    # Supplied visuals need the existing planner to select exact paths and a
    # prose owner. Skipping it in auto mode leaves every chart linked-only,
    # even when the user asks for an illustrated analysis. Text-only reports
    # keep their direct path; explicit/custom templates retain their topology.
    needs_visual_selection = (strategy == "auto" and config.figures.enabled
                              and config.figures.mode != "off"
                              and bool(supplied_figure_sources(context)))
    if not survey and (template.name not in BUILTIN_TEMPLATE_NAMES
                       or (strategy != "adaptive" and not needs_visual_selection)):
        return memory
    if not memory.section_plan or (survey and len(memory.section_plan) < 3):
        return memory
    errors: list[str] = []
    rejected_response = None
    planned: list[ReportSectionPlan] = []
    planning: dict[str, Any] = {}
    for attempt in (1, 2):
        try:
            planned, planning = _plan_topic_specific_outline(
                client=client,
                context=context,
                template=template,
                memory=memory,
                config=config,
                retry=attempt == 2,
                retry_error=errors[-1] if errors else "",
                rejected_response=rejected_response,
                documents=documents,
                extra_context=extra_context,
            )
            break
        except (LLMError, ValidationError, ValueError, TypeError) as exc:
            errors.append(str(exc))
            rejected_response = getattr(exc, "response", None)
            if isinstance(exc, LLMError) and not isinstance(exc, LLMResponseError):
                _emit(emit, "Outline provider request failed; transport retries belong to the model connection.")
                break
            if attempt == 1:
                _emit(emit, f"Outline planner did not yield a usable plan; retrying once. {exc}")
            else:
                if config.allow_llm_fallback:
                    _emit(emit, f"Outline planner fallback used; keeping template outline. {exc}")
                else:
                    _emit(emit, f"Outline planner failed after retry; fallback is disabled. {exc}")
    if not planned:
        if not config.allow_llm_fallback:
            raise LLMError(
                "Outline planner failed after bounded retries and LLM fallback is disabled"
            )
        return memory.model_copy(
            update={
                "outline_planning": {
                    "schema_version": "report_outline_planning.v1",
                    "status": "fallback",
                    "strategy": "deterministic_outline_with_full_evidence_budget",
                    "attempts": len(errors),
                    "errors": errors,
                    "section_source_budget": _outline_source_budget(memory.survey_contract, config),
                    "visual_candidates": [],
                },
                "key_decisions": memory.key_decisions
                + [
                    "Outline planner fallback retained the template and configured evidence budget."
                ],
            }
        )
    _emit(emit, f"Outline planner produced {len(planned)} evidence-organized section(s).")
    return memory.model_copy(
        update={
            "section_plan": planned,
            "outline_planning": {
                "schema_version": "report_outline_planning.v1",
                "status": "adapted",
                "strategy": "topic_specific_outline" if survey else "evidence_organized_outline",
                "attempts": len(errors) + 1,
                "errors": errors,
                "section_source_budget": _outline_source_budget(memory.survey_contract, config),
                **planning,
            },
            "key_decisions": memory.key_decisions
            + ["Section plan adapted to the requested document and current evidence before drafting."],
        }
    )


def _resolve_document_plan(memory: ReportMemory, *, config: ReportRuntimeConfig, context: ReportContext | None = None,
                           fallback_title: str = "Research Report") -> ReportMemory:
    """Freeze the only plan that Writer, Reviewer, renderer, and audit consume."""
    if memory.document_plan is not None:
        return memory
    candidates = memory.outline_planning.get("visual_candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    title = str(memory.outline_planning.get("title") or "")
    if not title and context is not None:
        # Topic may be the entire natural-language task, not an article title.
        # Use the same single-line/240-character title contract as planning.
        topic = context.topic.strip()
        title = topic if len(topic) <= 240 and not any(ord(char) < 32 for char in topic) else fallback_title
    plan = resolve_document_plan(
        sections=memory.section_plan,
        contract=memory.survey_contract,
        config=config,
        visual_candidates=[row for row in candidates if isinstance(row, dict)],
        status=str(memory.outline_planning.get("status") or "resolved"),
        title=title,
        supplied_figure_handles=[row["handle"] for row in supplied_figure_sources(context)],
        argument_plan=(ReportArgumentPlan.model_validate(memory.outline_planning["argument_plan"])
                       if memory.outline_planning.get("argument_plan") else None),
    )
    planning = dict(memory.outline_planning)
    if planning.get("rejected_length_request"):
        plan.notes.append("The planner's length interpretation was rejected, not the original task. "
                          "Follow and review the original length/conciseness request; no numerical contract was inferred.")
    request = planning.pop("length_request", None)
    if request is not None:
        if context is None:
            raise ValueError("Whole-document budget requires the original report context")
        plan = budget_document_plan(context, memory, config, plan, request)
    planning["document_plan"] = {
        "schema_version": plan.schema_version,
        "status": plan.status,
        "section_count": len(plan.sections),
        "visual_intent_count": len(plan.visual_intents),
        "target_words": plan.target_words,
    }
    return memory.model_copy(
        update={
            "section_plan": plan.sections,
            "document_plan": plan,
            "outline_planning": planning,
        }
    )


def _plan_topic_specific_outline(
    *,
    client: LLMClient,
    context: ReportContext,
    template: ReportTemplateBundle,
    memory: ReportMemory,
    config: ReportRuntimeConfig,
    retry: bool = False,
    retry_error: str = "",
    rejected_response: Mapping[str, Any] | None = None,
    documents: DocumentBundle | None = None,
    extra_context: list[ReportToolResult] | None = None,
) -> tuple[list[ReportSectionPlan], dict[str, Any]]:
    front_matter = bibliography_planning_views(context, documents)
    if not (memory.survey_contract.get("enabled") and is_survey_report(
        template_name=template.name, style=config.style, report_mode=context.report_mode,
    )):
        payload, source_labels = outline_evidence_labels(_outline_source_context(
            evidence_outline_context(context, memory, config, retry=retry, retry_error=retry_error,
                rejected_response=rejected_response, template=template), front_matter, config=config),
            memory.source_handles)
        if extra_context:
            payload["extra_tool_context"] = [report_tool_context(row) for row in extra_context]
        response = client.ask_json(
            OUTLINE_PLANNER_SYSTEM,
            _json_prompt(payload),
            label="report-outline-planner-retry" if retry else "report-outline-planner",
        )
        response = map_evidence_labels(response, source_labels)
        sections = _evidence_outline_sections(response, memory=memory, config=config)
        return _validated_outline_delivery(response, sections=sections, context=context, memory=memory, config=config)
    response = client.ask_json(
        OUTLINE_PLANNER_SYSTEM,
        _outline_planner_prompt(
            context=context,
            template=template,
            memory=memory,
            config=config,
            retry=retry,
            retry_error=retry_error,
            rejected_response=rejected_response,
            source_front_matter=front_matter,
            extra_context=extra_context,
        ),
        label="report-outline-planner-retry" if retry else "report-outline-planner",
    )
    sections = _normalize_outline_sections(response)
    # Organization belongs to the proposal and authored requirements, not a
    # keyword-based chapter inserter or heading blacklist.
    if not 2 <= len(sections) <= 12:
        raise ValueError("survey outline requires 2-12 usable sections")
    budget = _outline_source_budget(memory.survey_contract, config)
    planned: list[ReportSectionPlan] = []
    planned_sections = sections
    total_target_words = _outline_target_words(memory.survey_contract, default=12000)
    default_min_citations = _outline_min_citations(memory.survey_contract, default=3)
    for index, row in enumerate(planned_sections, start=1):
        heading = _clean_outline_heading(row["heading"])
        goal = row["goal"]
        keywords = " ".join(row.get("keywords", []))
        default_target_words = _planned_section_target_words(
            heading=heading,
            index=index,
            total=len(planned_sections),
            target_words=total_target_words,
        )
        target_words = _coerce_int(
            row.get("target_words") or default_target_words,
            default=default_target_words,
            lower=0,
            upper=8000,
        )
        min_citations = _coerce_int(
            row.get("min_citations") or default_min_citations,
            default=default_min_citations,
            lower=0,
            upper=20,
        )
        subsections = _string_items(row.get("subsections"))[:6]
        evidence_handles = route_section_sources(
            context=context,
            heading=heading,
            goal=f"{goal} {keywords}",
            contract=memory.survey_contract,
            budget=budget,
        )
        planned.append(
            ReportSectionPlan(
                section_id=_section_slug(heading) or f"section_{index}",
                heading=heading,
                goal=goal,
                evidence_handles=evidence_handles or _fallback_section_handles(memory, limit=budget),
                target_words=target_words,
                min_citations=min_citations,
                subsections=subsections,
                required=True,
                final_order=index,
                draft_order=_survey_draft_order(heading, index, len(planned_sections)),
            )
        )
    return _validated_outline_delivery(response, sections=_dedupe_section_ids(planned),
        context=context, memory=memory, config=config)


def _outline_source_context(payload: dict[str, Any], views: list[dict[str, Any]],
                                  *, config: ReportRuntimeConfig) -> dict[str, Any]:
    """Keep original-source planning and optional reads in the same response."""
    if config.allow_source_backtracking and config.max_backtracking_calls > 0:
        payload = {**payload, "source_reading": {
            "max_requests": min(6, config.max_backtracking_calls),
            "tools": [spec.model_dump(mode="json") for spec in report_tool_specs() if set(spec.permissions) == {"read"}]},
            "planning_rules": [*payload["planning_rules"],
                "Plan from the supplied evidence, and optionally return context_requests for definitions, transformations, assumptions or comparison conditions needed by that argument but not visible in its overview. Use specific same-source queries or retained chunk anchors, not the broad task repeated as a query. These existing read-only tools read retained inputs; they do not retrieve new papers or certify a claim. Include the complete outline in this response. One shared initial read batch will be supplied to writing and review; do not request material already visible."],
            "output_schema": {**payload["output_schema"], "context_requests": [{"tool_name": "registered read-only tool", "arguments": {}}]}}
    return _source_bibliography_context(payload, views, rules_key="planning_rules")


def _source_bibliography_context(payload: dict[str, Any], views: list[dict[str, Any]],
                                 *, rules_key: str) -> dict[str, Any]:
    """The same source-matched field contract, without requiring a planning call."""
    if not views:
        return payload
    from simple_ar.research.evidence.bibliography import CITATION_FIELD_VALUE_RULE
    return {**payload, "source_front_matter": views,
        rules_key: [*payload[rules_key],
            CITATION_FIELD_VALUE_RULE,
            "Return source_notes in this same response for the supplied source_front_matter, using [] for fields not established by that view. Copy only requested missing fields with an exact same-source quote and section_id. A filename placeholder title is a missing title, not an established publication title. Do not guess from filenames, another source or memory, overwrite recorded metadata, or certify publication identity. Every author name must occur in its exact quote, including source punctuation and affiliation markers. Prefer a short contiguous byline subset with complete=false over an unreliable full-list transcription; complete=true only if the entire author list is visible and copied exactly. Use the published date rather than received/accepted dates, copying the literal date or visible publication year without inventing a full date. Missing or truncated evidence stays unknown. Bibliography is assembled from these fields, not from free-text section goals or a Writer-generated References section."],
        "output_schema": {**payload["output_schema"], "source_notes": [
            {"paper_id": "supplied paper_id", "bibliographic_fields": [
                {"field": "title|authors|published|doi|url", "value": "literal string or authors array",
                 "quote": "exact original source quotation", "section_id": "supplied section_id", "complete": False}]}]}}


def _retain_bibliography_notes(memory: ReportMemory, response: dict[str, Any],
                               views: list[dict[str, Any]] | None) -> None:
    """Commit optional proposals only after the corresponding draft validates."""
    if not views or not isinstance(response.get("source_notes"), list):
        return
    requested = {row["paper_id"] for row in views}
    notes = memory.outline_planning.setdefault("source_notes", [])
    for row in response["source_notes"][:len(views)]:
        if (isinstance(row, dict) and isinstance(row.get("paper_id"), str)
                and row["paper_id"] in requested and row not in notes):
            notes.append(row)


class _RejectedOutline(ValueError):
    """Attempt-local correction input; never a second persisted plan."""

    def __init__(self, error: Exception, response: dict[str, Any]):
        super().__init__(str(error))
        self.response = response


def _validated_outline_delivery(
    response: dict[str, Any], *, sections: list[ReportSectionPlan], context: ReportContext,
    memory: ReportMemory, config: ReportRuntimeConfig,
) -> tuple[list[ReportSectionPlan], dict[str, Any]]:
    """Shared task/assembly contract, inside the original planning allowance."""
    title = response.get("title", "")
    if not isinstance(title, str) or len(title) > 240 or any(ord(char) < 32 for char in title) or title.startswith("#"):
        raise ValueError("document title must be plain single-line text of at most 240 characters")
    visuals = response.get("visual_intents", [])
    if not isinstance(visuals, list) or any(not isinstance(row, dict) for row in visuals):
        raise ValueError("visual_intents must be a list of visual intent objects")
    try:
        reads = validate_report_reads(response.get("context_requests", []),
            limit=min(6, max(0, config.max_backtracking_calls)) if config.allow_source_backtracking else 0)
        keys = outline_section_keys(response.get("sections"), sections)
        argument = resolve_argument_plan(response.get("argument_plan"), sections=sections,
            evidence_handles=[row.handle for row in memory.source_handles],
            metric_ids=[row.metric_id for row in memory.metric_sources], section_keys=keys)
        bound_visuals = []
        for raw in visuals:
            row = dict(raw)
            if row.get("view") == "supplied-data" and row.get("figure_paths") is None:
                # New plans must select charts; omission means linked-only.
                # Restored plans bypass this constructor and retain old intent.
                row["figure_paths"] = []
            if "section_key" in row:
                key = row.pop("section_key")
                if not isinstance(key, str) or key not in keys:
                    raise ValueError("visual intent requires an exact planned section key")
                target = keys[key]
                heading = row.get("section_heading")
                if ((row.get("section_id") and row["section_id"] != target)
                        or (heading and not any(section.section_id == target
                            and _clean_outline_heading(str(heading)) == section.heading for section in sections))):
                    raise ValueError("visual intent key and other references disagree")
                row["section_id"] = target
            bound_visuals.append(row)
    except (ValueError, TypeError) as exc:
        raise _RejectedOutline(exc, response) from exc
    preview_plan = resolve_document_plan(sections=sections, contract=memory.survey_contract, config=config,
        title=title.strip(), visual_candidates=bound_visuals,
        supplied_figure_handles=[row["handle"] for row in supplied_figure_sources(context)], argument_plan=argument)
    # Reject invented selections in the existing outline correction allowance,
    # rather than accepting a plan that can only fail later during assembly.
    documents = {row.handle: row.metadata.get("document_id") for row in context.source_handles}
    inventory = {row.get("document_id"): {figure["path"] for figure in row.get("figures", [])}
                 for row in context.results.get("supplied_analyses", [])}
    for intent in preview_plan.visual_intents:
        if intent.view == "supplied-data" and intent.figure_paths is not None:
            if set(intent.figure_paths) - inventory.get(documents.get(intent.evidence_handles[0]), set()):
                raise _RejectedOutline(ValueError("figure_paths must come from the registered analysis inventory"), response)
    # Optional model annotations are not authoritative user constraints. Keep
    # useful organization if this annotation is invalid, without inventing or
    # silently adopting a quota. Valid but infeasible budgets still fail below.
    rejected_length = None
    try:
        length_request = validate_length_request(response.get("length_request"),
            objective=report_objective(context, memory))
    except ValueError as exc:
        length_request = None
        rejected_length = {"proposal": response.get("length_request"), "reason": str(exc)}
    budgeted = budget_document_plan(context, memory, config, preview_plan, length_request or None)
    request = {key: budgeted.length_budget[key] for key in
        ("unit", "scope", "request_quote", "constraint", "min_words", "max_words", "target_words")
        if key in budgeted.length_budget} if budgeted.length_budget else None
    # A citation proposal cannot invalidate an otherwise useful outline; the
    # original source-matching owner discards unsupported fields downstream.
    notes = response.get("source_notes", [])
    notes = [row for row in notes[:len(context.papers)] if isinstance(row, dict)] if isinstance(notes, list) else []
    # Carry one validated named projection into the existing planning record,
    # rather than unpacking/repacking parallel positional fields in the caller.
    return sections, {"visual_candidates": bound_visuals, "title": title.strip(),
        **({"argument_plan": argument.model_dump(mode="json")} if argument else {}),
        **({"length_request": request} if request else {}),
        **({"rejected_length_request": rejected_length} if rejected_length else {}),
        **({"source_notes": notes} if notes else {}),
        **({"context_requests": [row.model_dump(mode="json") for row in reads]} if reads else {})}


def _evidence_outline_sections(
    response: dict[str, Any], *, memory: ReportMemory, config: ReportRuntimeConfig,
) -> list[ReportSectionPlan]:
    """Validate organization, not scientific truth; never manufacture evidence."""
    raw = response.get("sections")
    if not isinstance(raw, list) or not 2 <= len(raw) <= 12:
        raise ValueError("evidence outline requires 2-12 sections")
    if any(not isinstance(row, dict) or not str(row.get("heading") or "").strip()
           or not str(row.get("goal") or "").strip() for row in raw):
        raise ValueError("each evidence section needs a heading and reader-facing goal")
    available = {handle.handle for handle in memory.source_handles}
    budget = config.max_section_sources
    default_handles = list(dict.fromkeys(
        handle for section in memory.section_plan for handle in section.evidence_handles
        if handle in available
    ))
    default_words = sum(section.target_words for section in memory.section_plan) // len(raw)
    sections = []
    for index, row in enumerate(raw, 1):
        heading = _clean_outline_heading(str(row["heading"]))
        if not heading or heading.lower() == "references":
            raise ValueError("evidence outline must not include an empty or References section")
        handles = row.get("evidence_handles", default_handles)
        if not isinstance(handles, list) or any(not isinstance(handle, str) for handle in handles):
            raise ValueError(f"evidence_handles for {heading!r} must be a list of source handles")
        unknown = [handle for handle in handles if handle not in available]
        if unknown:
            raise ValueError(f"evidence outline references unknown evidence_handles for {heading!r}: {unknown!r}. "
                f"Copy top-level source handles from evidence_handle_choices, not passage identifiers or citation keys. "
                f"Visible choices: {[handle.handle for handle in memory.source_handles[:40]]!r}.")
        handles = list(dict.fromkeys(handles))
        if budget > 0 and len(handles) > budget:
            raise ValueError("evidence outline exceeds the configured per-section source limit")
        sections.append(ReportSectionPlan(
            section_id=_section_slug(heading) or f"section_{index}", heading=heading,
            goal=str(row["goal"]).strip(), evidence_handles=handles,
            target_words=_coerce_int(row.get("target_words"), default=default_words, lower=0, upper=8000),
            # Survey citation quotas and filler subsection templates do not
            # apply to supplied results, negative findings or reproductions.
            min_citations=0, subsections=_string_items(row.get("subsections"))[:6],
            required=True, final_order=index,
            draft_order=_survey_draft_order(heading, index, len(raw)),
        ))
    return _dedupe_section_ids(sections)


def _outline_planner_prompt(
    *,
    context: ReportContext,
    template: ReportTemplateBundle,
    memory: ReportMemory,
    config: ReportRuntimeConfig,
    retry: bool = False,
    retry_error: str = "",
    rejected_response: Mapping[str, Any] | None = None,
    source_front_matter: list[dict[str, Any]] | None = None,
    extra_context: list[ReportToolResult] | None = None,
) -> str:
    compact_contract = _compact_survey_contract(memory.survey_contract)
    # Role groups help the Writer balance sources, but exposing them as
    # ``taxonomy_facets`` anchors the outline agent to provenance labels.
    # The deterministic contract outline is a coverage fallback, not a gold
    # outline. Showing it to the planner made weaker models copy its generic
    # headings verbatim and still appear to have produced an "adapted" plan.
    payload = {
        "task": "plan_topic_specific_survey_outline",
        "topic": context.topic,
        "objective": report_objective(context, memory),
        "report_mode": context.report_mode,
        "template": template.name,
        "template_guidance": planning_template_guidance(template, config),
        "style": config.style,
        "delivery_constraints": {
            "max_cited_sources": config.max_cited_sources or None,
            "source_scope": "Final document, not each section" if config.max_cited_sources else None,
        },
        "survey_contract": compact_contract,
        "required_structure": {
            "longform_default_front_matter": ["Abstract", "Introduction"],
            "longform_default_back_matter": ["Conclusion"],
            "body_requirement": "For broad long-form surveys derive 4-7 topic-specific body sections. An explicit task-scoped length takes priority; use only the sections needed for the requested review.",
        },
        "available_source_brief": _outline_source_brief(context),
        "previous_document": prior_draft_context(context),
        **prior_document_revision(context, memory),
        "synthesis_excerpt": context.synthesis_markdown[:2500],
        "synthesis_status": dict(DERIVED_CONTEXT_STATUS),
        "evidence_summary_excerpt": context.evidence_summary[:2500],
            "planning_rules": [
            *ARGUMENT_PLANNING_RULES,
            "The complete original objective governs. For broad long-form surveys prefer 7-10 display sections. For explicit whole-document word requests choose 2-12 purposeful sections within the requested space; do not force front/back matter or unrelated coverage into a short review.",
            LENGTH_REQUEST_RULE,
            "For long-form surveys, each major body section should include 2-4 planned third-level subsection hints so the final report has a navigable internal structure.",
            "Do not add subsection hints for Abstract, Introduction, or Conclusion; those sections should remain single-section prose.",
            "Body sections must be topic-specific; do not blindly reuse generic headings if the topic suggests better axes.",
            "Do not use the default body headings `Conceptual Foundations and Taxonomy`, `Methods and System Construction`, `Applications and Use Cases`, or `Evaluation, Benchmarks, and Evidence Quality`. Replace them with concrete conceptual axes, method families, task settings, or evaluation regimes evident in the selected literature.",
            "For broad long-form surveys, at least three body headings should identify topic-specific concepts from the source brief. Short task-scoped reviews need no minimum number of body headings; choose axes that distinguish this topic without padding.",
            "Treat required facets as coverage checks, not as section titles or taxonomy axes. Derive reader-facing taxonomy and headings from the source brief and selected papers.",
            "Plan a coherent article for the user's goal; assembly owns the final title, section numbering and references.",
            "Use headings that a human survey reader would expect for this topic.",
            "For broad long-form surveys, cover foundations/scope, method families, construction, evaluation, applications, challenges and future directions where relevant. For a bounded review, cover the requested question and limitations; do not add unrelated facets to meet a generic checklist.",
            "For long surveys, keep coverage broad even when headings are topic-specific: include related surveys or adjacent fields when evidence permits, and include future directions separately when it improves reader utility.",
            "If a facet has weak evidence, include it only as a limitation or open problem instead of inventing coverage.",
            "Do not mention SimpleAutoResearch, pipeline stages, artifacts, prompts, or evaluation benchmark internals.",
            "Do not include a References section; references are appended separately.",
            "Return the requested JSON object with a non-empty `sections` list; do not return prose outside JSON.",
        ],
        "output_schema": {
            "argument_plan": ARGUMENT_PLAN_SCHEMA,
            "length_request": dict(LENGTH_REQUEST_SCHEMA),
            "sections": [
                {
                    "section_key": "A distinct short literal key, reused by arguments and visuals",
                    "heading": "Short academic section heading without numbering",
                    "goal": "Reader-facing purpose and synthesis target for this section",
                    "keywords": ["routing keywords for evidence selection"],
                    "target_words": 1200,
                    "min_citations": 3,
                    "subsections": ["optional third-level subsection heading"],
                    "required": True,
                }
            ],
            "visual_intents": [
                {
                    "kind": "table|figure",
                    "title": "Reader-facing visual title",
                    "purpose": "What comparison or structure this visual clarifies",
                    "section_key": "One exact key from sections",
                    "evidence_handles": ["optional selected source handles"],
                    "columns": ["table column", "table column"],
                    "view": "taxonomy-map|system-construction-flow|evaluation-landscape|challenge-roadmap for figures only",
                }
            ],
            "notes": "optional planning notes",
        },
    }
    if retry:
        payload["validation_error"] = retry_error
        payload["rejected_response"] = rejected_response
        payload["retry_instruction"] = (
            "Correct the invalid outline structure, title, visual intent or exact task-length interpretation. "
            "Use reader-facing axes from the evidence, not generic fallback headings. "
            "Return valid reader-facing sections with the required JSON fields and respect the original task length; do not pad to the broad-survey defaults."
            " Check every exact section and evidence pointer in the rejected response, including those not named in the first error. Return the complete corrected proposal, not a new unrelated outline."
        )
    if extra_context:
        payload["extra_tool_context"] = [report_tool_context(row) for row in extra_context]
    return _json_prompt(_outline_source_context(payload, source_front_matter or [], config=config))


def _outline_source_brief(context: ReportContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for handle in context.source_handles:
        if handle.kind not in {"paper", "paper_brief", "material"}:
            continue
        rows.append(
            {
                "handle": handle.handle,
                "cite_as": handle.citation_key,
                "title": handle.title[:180],
                "summary": handle.summary[:500],
                "section": handle.section[:120],
                "metadata": _compact_source_metadata(handle.metadata),
            }
        )
        if len(rows) >= 28:
            break
    return rows




def _normalize_outline_sections(response: dict[str, Any]) -> list[dict[str, Any]]:
    raw = response.get("sections")
    if not isinstance(raw, list):
        raise ValueError("outline planner response missing `sections` list")
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        heading = _clean_outline_heading(str(item.get("heading") or ""))
        if not heading or heading.lower() == "references":
            continue
        goal = str(item.get("goal") or "").strip()
        keywords = item.get("keywords")
        if isinstance(keywords, str):
            keyword_rows = [keywords]
        elif isinstance(keywords, list):
            keyword_rows = [str(keyword).strip() for keyword in keywords if str(keyword).strip()]
        else:
            keyword_rows = []
        rows.append(
            {
                "heading": heading,
                "goal": goal or f"Synthesize evidence relevant to {heading}.",
                "keywords": keyword_rows[:10],
                "target_words": item.get("target_words") or 0,
                "min_citations": item.get("min_citations") or 0,
                "subsections": _string_items(item.get("subsections"))[:6],
            }
        )
    return rows


def _outline_source_budget(contract: dict[str, Any], config: ReportRuntimeConfig) -> int:
    raw = contract.get("section_source_budget") if isinstance(contract, dict) else None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = 24 if config.cost_profile == "thorough" else 12
    return max(4, min(40, value))


def _outline_target_words(contract: dict[str, Any], *, default: int) -> int:
    expected = contract.get("expected_coverage") if isinstance(contract, dict) else {}
    raw = expected.get("target_words") if isinstance(expected, dict) else None
    return _coerce_int(raw, default=default, lower=1200, upper=50000)


def _outline_min_citations(contract: dict[str, Any], *, default: int) -> int:
    expected = contract.get("expected_coverage") if isinstance(contract, dict) else {}
    raw = expected.get("min_citations_per_section") if isinstance(expected, dict) else None
    return _coerce_int(raw, default=default, lower=0, upper=20)


def _planned_section_target_words(
    *,
    heading: str,
    index: int,
    total: int,
    target_words: int,
) -> int:
    lowered = heading.lower()
    if "abstract" in lowered or "conclusion" in lowered:
        return max(250, min(900, target_words // max(total * 3, 1)))
    body_count = max(1, total - 2)
    body_budget = max(600, target_words - min(1800, target_words // 5))
    return max(600, min(3500, body_budget // body_count))


def _fallback_section_handles(memory: ReportMemory, *, limit: int) -> list[str]:
    return [
        handle.handle
        for handle in memory.source_handles
        if handle.kind in {"paper", "paper_brief", "material"}
    ][:limit]


def _section_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")[:60]


def _dedupe_section_ids(sections: list[ReportSectionPlan]) -> list[ReportSectionPlan]:
    seen: dict[str, int] = {}
    result: list[ReportSectionPlan] = []
    for section in sections:
        base = section.section_id or "section"
        count = seen.get(base, 0) + 1
        seen[base] = count
        section_id = base if count == 1 else f"{base}_{count}"
        result.append(section.model_copy(update={"section_id": section_id}))
    return result


def _parse_section_draft(response: dict[str, Any], section: ReportSectionPlan) -> ReportSectionDraft:
    """One parser for section-wise and jointly generated section bodies."""
    if _is_claim_record_response(response):
        raise LLMResponseError(
            "Writer returned a claim-level metadata record instead of the required section draft."
        )
    draft = ReportSectionDraft.model_validate(_normalize_draft_response(response, section))
    if not draft.draft_markdown.strip() and draft.status != "skipped":
        keys = ", ".join(sorted(str(key) for key in response)[:12])
        raise LLMResponseError(f"Writer returned empty draft for {section.section_id}; response keys: {keys or '(none)'}")
    return draft


def _writer_evidence_reads(
    *, owner: str, requests: list[ReportToolCall] | None, gateway: ReportToolGateway,
    iterations: list[ReportIterationRecord], all_results: list[ReportToolResult],
    checkpoint: Callable[[], None],
) -> list[ReportToolResult]:
    """Retain one optional Writer read batch in the existing iteration trace."""
    event = next((row for row in reversed(iterations)
                  if row.action == "writer_context" and row.summary == owner), None)
    if event is None:
        if requests is None:
            return []
        event = ReportIterationRecord(iteration=len(iterations) + 1, section_id="",
            action="writer_context", status="started", summary=owner, tool_requests=requests)
        iterations.append(event)
        checkpoint()
    elif requests is not None and event.tool_requests != requests:
        raise ValueError("A saved Writer read batch cannot be replaced on recovery.")
    checkpointed_report_reads(gateway, event.tool_requests, event, all_results, checkpoint)
    event.status = "completed"
    checkpoint()
    return list(event.tool_results)


def _draft_document_with_recovery(
    *, client: LLMClient, context: ReportContext, template: ReportTemplateBundle,
    memory: ReportMemory, config: ReportRuntimeConfig, sections: list[ReportSectionPlan],
    adopted_sections: list[ReportSectionDraft], emit: Callable[[str], None] | None,
    extra_context: list[ReportToolResult] | None = None,
    reviews: list[ReportSectionReview] | None = None,
    read_context: Callable[[list[ReportToolCall] | None], list[ReportToolResult]] | None = None,
    source_front_matter: list[dict[str, Any]] | None = None,
) -> list[ReportSectionDraft]:
    """Joint composition, not a second Writer lifecycle or manuscript format."""
    payload = _writer_payload(context=context, template=template, memory=memory,
        section=None, document_sections=sections, config=config, extra_context=extra_context or [], previous_draft=None, review=None,
        source_batch_index=1, source_batch_count=1, include_previous_draft=False,
        draft_mode="document", adopted_sections=adopted_sections,
        source_front_matter=source_front_matter if reviews is None else None)
    if reviews is not None:
        requests = {row.section_id: row for row in reviews}
        baselines = {row.section_id: row for row in adopted_sections}
        plan_by_id = {row.section_id: row for row in sections}
        payload["task"] = "jointly_revise_report_sections"
        payload["edit_scope"]["read_only"] = "Noneligible section bodies, frozen plan/title, sources, measurements and assembly-owned attachments."
        payload["response_rules"][3] = (
            "Revise eligible sections together against their actual baselines and review requests. "
            "Do not change noneligible sections or the frozen plan. Return complete replacement bodies; "
            "reconcile shared definitions, tables and transitions rather than independently polishing each section."
        )
        for row in payload["sections"]:
            sid = row["section"]["section_id"]
            row.update(previous_draft=baselines[sid].model_dump(mode="json"),
                revision_request=requests[sid].model_dump(mode="json"),
                revision_preservation_requirement=_revision_preservation_requirement(
                    section=plan_by_id[sid],
                    previous_draft=baselines[sid], review=requests[sid]))
        payload["response_rules"].append("Review requests are fallible opinions, not source facts. Verify their premises, preserve supported claims and qualifications, and correct the actual manuscript without padding.")
    def add_read_context(results: list[ReportToolResult]) -> None:
        payload["extra_tool_context"].extend(report_tool_context(row,
            source_evidence=payload['source_handles'], evidence_reference='source_handles') for row in results)

    saved_reads = read_context(None) if read_context is not None else []
    can_read = (read_context is not None and config.allow_source_backtracking
                and config.max_backtracking_calls > 0 and not saved_reads)
    read_specs = {row.name: row for row in report_tool_specs() if set(row.permissions) == {"read"}}
    read_limit = min(6, max(0, config.max_backtracking_calls))  # Existing prompt window, not a new allowance.
    payload["source_reading"] = {
        "available": can_read, "max_requests": read_limit if can_read else 0,
        "tools": [row.model_dump(mode="json") for row in read_specs.values()] if can_read else [],
        "instruction": (
            "If a substantive assertion, metric, comparison condition or absence claim needs a passage not visible here, "
            "return context_requests only, using the registered read-only tools, before writing. "
            "One optional batch is available; after its results return the complete requested sections, not another read batch. "
            "An excerpt, a lexical miss or an unconfirmed read cannot establish that the original source lacks evidence. "
            "Use actual returned conditions instead of generic table placeholders; unresolved details remain explicit limitations."
        ),
    }
    if can_read:
        payload["output_schema"]["context_requests"] = [{"tool_name": "registered read-only tool", "arguments": {}}]
        payload["response_rules"].append("Choose either complete sections or a nonempty context_requests batch, never both.")
    add_read_context(saved_reads)
    wanted = {row.section_id: row for row in sections}
    for attempt in range(2):
        response = None
        try:
            response = client.ask_json(WRITER_SYSTEM, _json_prompt(payload),
                label=("report-document-joint-reviser" if reviews is not None else "report-writer-document") + ("-retry" if attempt else ""),
                max_output_tokens=config.max_section_tokens if config.max_section_tokens > 0 else None)
            requested = response.get("context_requests") if isinstance(response, dict) else None
            if requested:
                if (not can_read or not isinstance(requested, list) or len(requested) > read_limit
                        or response.get("sections")):
                    raise LLMResponseError("Writer reads require one bounded batch instead of sections, before drafting.")
                calls = validate_report_reads(requested, limit=read_limit)
                saved_reads = read_context(calls)
                can_read = False
                add_read_context(saved_reads)
                payload["source_reading"].update(available=False, max_requests=0, tools=[])
                payload["output_schema"].pop("context_requests", None)
                payload["response_rules"].append("The read batch is consumed. Return complete sections using confirmed passages and clearly retain unsupported or unavailable details as limitations.")
                _emit(emit, f"Writer read batch returned {len(saved_reads)} result(s); requesting complete sections in a new model call.")
                response = None  # A transport failure must not replay this post-read call.
                response = client.ask_json(WRITER_SYSTEM, _json_prompt(payload),
                    label="report-document-joint-reviser-evidence" if reviews is not None else "report-writer-document-evidence",
                    max_output_tokens=config.max_section_tokens if config.max_section_tokens > 0 else None)
            if isinstance(response, dict) and response.get("context_requests"):
                raise LLMResponseError("Writer read batch has been consumed; return complete sections.")
            rows = response.get("sections") if isinstance(response, dict) else None
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise LLMResponseError("Joint Writer requires a sections list of section draft objects.")
            identities = [row.get("section_id") for row in rows]
            if any(not isinstance(sid, str) for sid in identities) or len(set(identities)) != len(rows) or set(identities) != set(wanted):
                raise LLMResponseError(f"Joint Writer must return each requested section exactly once: {list(wanted)}; received {identities!r}.")
            drafts = {row["section_id"]: _parse_section_draft(row, wanted[row["section_id"]]) for row in rows}
            if reviews is None and source_front_matter:
                _retain_bibliography_notes(memory, response, source_front_matter)
            return [drafts[row.section_id] for row in sections]
        except (LLMError, ValueError) as exc:
            if attempt or (response is None and not isinstance(exc, LLMResponseError)) or (isinstance(exc, LLMError) and not isinstance(exc, LLMResponseError)):
                raise
            payload.update(validation_error=str(exc),
                correction="Correct the entire section set against the same inputs; no partial draft has been adopted.")
            if response is not None:
                payload["rejected_response"] = response
            _emit(emit, f"Joint Writer correcting its rejected section set once: {exc}")
    raise AssertionError("Joint Writer correction did not terminate")


def _section_response(
    *, client: LLMClient, context: ReportContext, template: ReportTemplateBundle,
    memory: ReportMemory, section: ReportSectionPlan, config: ReportRuntimeConfig,
    label: str, draft: ReportSectionDraft | None = None,
    extra_context: list[ReportToolResult] | None = None,
    adopted_sections: list[ReportSectionDraft] | None = None,
    previous_draft: ReportSectionDraft | None = None,
    review: ReportSectionReview | None = None,
    revision_review: ReportSectionReview | None = None,
    source_batch_index: int = 1, source_batch_count: int = 1,
    source_front_matter: list[dict[str, Any]] | None = None,
    emit: Callable[[str], None] | None = None, prompt_suffix: str = "",
    include_previous_draft: bool = True, draft_mode: str = "section",
    recover: bool = True, max_output_tokens: int | None = None,
) -> ReportSectionDraft | ReportSectionReview:
    """Draft or independently inspect a section under one correction policy.

    A supplied draft selects independent review, never a writing fallback.
    Prompt, evidence validation and fallback outputs retain their role contracts;
    neither role retries provider, budget or incomplete-response failures.
    """
    role = "Reviewer" if draft is not None else "Writer"
    prompt_inputs = dict(context=context, memory=memory, section=section, config=config,
        extra_context=extra_context or [], previous_draft=previous_draft, review=review,
        draft_mode=draft_mode, adopted_sections=adopted_sections, source_front_matter=source_front_matter,
        template=template, source_batch_index=source_batch_index, source_batch_count=source_batch_count)
    for attempt in range(2 if recover else 1):
        try:
            if draft is None:
                prompt = _json_prompt(_writer_payload(**prompt_inputs, recovery=bool(attempt),
                    include_previous_draft=True if attempt else include_previous_draft))
                if prompt_suffix:
                    prompt += "\n\n" + prompt_suffix
            else:
                view = _reviewer_context(
                    context=context,
                    template=template,
                    memory=memory,
                    section=section,
                    draft=draft,
                    adopted_sections=adopted_sections,
                    revision_review=revision_review, previous_draft=previous_draft,
                    extra_context=extra_context,
                    config=config,
                )
                prompt = _json_prompt(view)
                if prompt_suffix:
                    prompt = prompt + "\n\n" + prompt_suffix
                if attempt:
                    prompt += "\n\nReturn exactly one JSON object matching the reviewer schema, with `revision_instructions` as a list of strings."
            response = client.ask_json(
                REVIEWER_SYSTEM if draft is not None else WRITER_SYSTEM, prompt,
                label=f"{label}-retry" if attempt else label,
                max_output_tokens=(config.max_section_tokens if config.max_section_tokens > 0 else None)
                    if recover else max_output_tokens,
            )
            if draft is None:
                written = _parse_section_draft(response, section)
                _retain_bibliography_notes(memory, response, source_front_matter)
                return written
            review = ReportSectionReview.model_validate(resolve_review_evidence(_normalize_review_response(response, section), view))
            current = review_draft_quote_sources(view)
            validate_finding_anchors(review, current, view)
            if revision_review is not None and previous_draft is not None and config is not None:
                # A model pass cannot adopt a correction that makes a complete,
                # previously fitting delivery violate its frozen length contract.
                # Reuse the assembler/check; no new draft, quota or persisted state.
                candidate = view.get("narrative_context", {}).get("delivery_text_observation", {})
                old_sections = [row for row in (adopted_sections or []) if row.section_id != draft.section_id]
                previous = delivery_text_observation(context, memory, [*old_sections, previous_draft], config)
                before = previous.get("length_check", {})
                after = candidate.get("length_check", {})
                if (memory.section_plan and not previous.get("pending_draft_sections") and not candidate.get("pending_draft_sections")
                        and previous.get("preview_status") == candidate.get("preview_status") == "pre_render_text_preview"
                        and before.get("status") == "within_range"
                        and after.get("status") in {"above_range", "below_range"}):
                    finding = ReviewerFinding(finding_id=f"{draft.section_id}-delivery-length-regression",
                        type="delivery_length", severity="major", required_action="revise", section_id=draft.section_id,
                        message=f"This candidate changes the canonical delivery from {before['markdown_token_count']} to {after['markdown_token_count']} whitespace-separated Markdown tokens, outside the frozen range {after['min_words']}–{after['max_words']}. Task quotation: {after['request_quote']}",
                        suggested_action="Revise this candidate within the existing allowance, preserving required evidence and assembly-owned text. Do not pad, truncate, expand the contract or edit other adopted sections to pass.")
                    review = review.model_copy(update={"verdict": "revise_required", "findings": [*review.findings, finding]})
            return review
        except (LLMError, ValueError) as exc:
            if not recover:
                raise
            failure = exc
            if attempt or (isinstance(exc, LLMError) and not isinstance(exc, LLMResponseError)):
                break
            _emit(emit, f"{role} JSON validation failed for `{section.heading}`; retrying once. {exc}")
    if not config.allow_llm_fallback:
        raise LLMError(f"{role} failed for `{section.heading}` and LLM fallback is disabled: {failure}") from failure
    _emit(emit, f"{role} fallback used for `{section.heading}`. {failure}")
    if draft is None:
        return _fallback_section_draft(section)
    return ReportSectionReview(
        section_id=section.section_id,
        verdict="warning",
        findings=[ReviewerFinding(
            finding_id=f"{section.section_id}-review-fallback",
            type="review_agent_fallback",
            severity="minor",
            message=f"Reviewer could not complete: {failure}. Section kept with fallback warning.",
            section_id=section.section_id,
            evidence_handles=section.evidence_handles[:5],
            suggested_action="Manually inspect this section before publishing.",
        )],
        revision_instructions=["Manually inspect this section before publishing."],
        notes="Reviewer fallback used; semantic review is incomplete.",
    )


def _fallback_section_draft(section: ReportSectionPlan) -> ReportSectionDraft:
    handles = [handle for handle in section.evidence_handles if handle][:5]
    citation_text = " ".join(f"[@{handle}]" for handle in handles[:3])
    body = (
        f"## {section.heading}\n\n"
        "This section could not be fully drafted by the report writer after a "
        "structured-output retry. The available evidence should still be "
        "reviewed before publication"
        + (f" ({citation_text})." if citation_text else ".")
        + "\n\n"
        f"Section goal: {section.goal.strip() or 'No section goal was recorded.'}"
    )
    return ReportSectionDraft(
        section_id=section.section_id,
        heading=section.heading,
        status="drafted",
        draft_markdown=body,
        used_sources=handles,
        citations=handles,
        open_questions=[
            "Report writer failed structured-output validation for this section; inspect evidence manually."
        ],
        limitations=[
            "This section is a conservative section-level fallback, not a polished model-written section."
        ],
    )


def _writer_prompt(**kwargs: Any) -> str:
    return _json_prompt(_writer_payload(**kwargs))


def _writer_payload(
    *,
    context: ReportContext,
    template: ReportTemplateBundle | None = None,
    memory: ReportMemory,
    section: ReportSectionPlan | None,
    config: ReportRuntimeConfig,
    extra_context: list[ReportToolResult] | None = None,
    previous_draft: ReportSectionDraft | None = None,
    review: ReportSectionReview | None = None,
    source_batch_index: int = 1,
    source_batch_count: int = 1,
    include_previous_draft: bool = True,
    draft_mode: str = "section",
    adopted_sections: list[ReportSectionDraft] | None = None,
    document_sections: list[ReportSectionPlan] | None = None,
    source_front_matter: list[dict[str, Any]] | None = None,
    recovery: bool = False,
) -> dict[str, Any]:
    # Normal, joint and schema-first correction share this transient input view.
    context_draft = previous_draft if include_previous_draft else None
    tools = extra_context or []
    selected = document_sections if section is None else [section]
    if not selected:
        raise ValueError("Writer needs at least one planned section.")
    neighbors = narrative_context(memory, section, adopted_sections or [], context=context, config=config) if section else None
    additions = (neighbors.pop("assembly_owned_content", []) if neighbors is not None else
        supplied_data_delivery(context, config=config, plan=memory.document_plan,
            section_ids=[row.section_id for row in memory.section_plan]) + _experiment_delivery_view(context, config))
    sources = list({row["handle"]: row for target in selected
        for row in _handles_for_section(memory, target)}.values())
    task_context = {
        "objective": report_objective(context, memory),
        "document_plan": document_plan_context(memory),
        "previous_document": prior_draft_context(context),
        **prior_document_revision(context, memory),
        "source_comparisons": context.source_comparisons,
        "comparison_status": dict(DERIVED_CONTEXT_STATUS),
        "assembly_owned_content": additions,
        "source_handles": sources,
        "registered_citation_keys": sorted(context.citation_key_map),
        "metric_sources": _prompt_metrics(memory, section_id=section.section_id if section else None),
        # Reads are already bounded by the gateway and session token budget.
        # A second last-N filter can discard the conditions of an earlier claim.
        "extra_tool_context": [report_tool_context(row, source_evidence=sources,
            evidence_reference='source_handles') for row in tools],
        "extra_tool_context_omitted": 0,
        "review_findings": [finding.model_dump(mode="json") for finding in (review.findings if review else [])],
        "review_findings_status": dict(REVIEW_OPINIONS_STATUS),
        "review_instructions": effective_revision_instructions(review),
    }
    if section is not None:
        task_context.update(narrative_context=neighbors,
            section_constraints=_section_constraints(section), length_requirement=_section_length_requirement(section),
            visual_requirements=visual_requirements(memory.document_plan, section),
            revision_preservation_requirement=_revision_preservation_requirement(
                section=section, previous_draft=context_draft, review=review))
        # One shared writing input, including joint drafting and recovery;
        # neighboring-prose context must not duplicate the same comparisons.
        for key in ("source_comparisons", "comparison_status", "assembly_owned_content"):
            task_context["narrative_context"].pop(key, None)
    if recovery:
        # The small outer-object schema is a distinct correction protocol;
        # requirements, evidence and bibliography still have one projection owner.
        payload = {
            "task": "recover_one_report_section",
            "draft_mode": draft_mode,
            "instruction": (
                "The previous response was not a usable section draft. Return one outer JSON object "
                "whose required `draft_markdown` field contains the requested Markdown prose. "
                "Do not return a claim record, a nested object, an explanation, or Markdown fences."
            ),
            "topic": context.topic,
            **task_context,
            "execution_context": _compact_execution_context(context.execution_context),
            "experiment_plan": _compact_experiment_plan(context.experiment_plan),
            "verified_execution_results": _compact_execution_results(context.results),
            "execution_evidence": report_execution_evidence(context),
            "section": {
                "section_id": section.section_id,
                "heading": section.heading,
                "goal": section.goal,
            },
            "previous_draft": (
                {
                    "draft_markdown": previous_draft.draft_markdown,
                    "citations": previous_draft.citations,
                }
                if previous_draft is not None
                else {}
            ),
            "style_rules": [
                "Write evidence-bounded prose appropriate to the requested section purpose using only supplied sources.",
                "The original user's whole-document requirements and current revision instructions take precedence over approximate section targets. Read document_plan.length_budget when present: assembly text is reserved, section shares are not minima, and unresolved future costs still need final checking.",
                "Prepared execution context declares requested conditions, not observed execution. Keep executor observations and attributed producer measurements distinct; do not change a recorded value to agree with a declaration.",
                "Use only registered_citation_keys. Materials without keys use their supplied names or public URLs, never invented markers.",
                "Do not include a References section.",
                "`draft_markdown` is mandatory; optional metadata may be empty arrays.",
            ],
            "output_schema": _writer_response_contract(section),
        }
        if payload["visual_requirements"]["tables"]:
            payload["style_rules"].append(
                "Include every required visual_requirements table using a `**Table: <planned title>**` caption and a compact Markdown table."
            )
        return _source_bibliography_context(payload, source_front_matter or [], rules_key="style_rules")
    if template is None:
        raise ValueError("Ordinary drafting requires its template guidance.")
    payload = {
        "task": "draft_or_revise_one_report_section",
        "draft_mode": draft_mode,
        "report_mode": context.report_mode,
        "style": config.style,
        "max_section_tokens": config.max_section_tokens if config.max_section_tokens > 0 else "",
        "source_strategy": {
            "mode": config.source_strategy,
            "batch_index": source_batch_index,
            "batch_count": source_batch_count,
            "instruction": _source_strategy_instruction(
                config=config,
                batch_index=source_batch_index,
                batch_count=source_batch_count,
            ),
        },
        "template_markdown": drafting_template_guidance(template, memory, config),
        **task_context,
        "global_research_context": {
            "evidence_summary": context.evidence_summary[:3000],
            "execution_context": _compact_execution_context(context.execution_context),
            "experiment_plan": _compact_experiment_plan(context.experiment_plan),
            "verified_execution_results": _compact_execution_results(context.results),
            "execution_evidence": report_execution_evidence(context),
            "synthesis": context.synthesis_markdown[:3000],
            "hypothesis": context.hypothesis_markdown[:1500],
            "derived_context_status": dict(DERIVED_CONTEXT_STATUS),
        },
        "limitations": memory.limitations[:8],
        "prior_claim_notes": [
            f"{claim.status}: {claim.claim} (citations: {', '.join(claim.citation_ids[:4]) or 'no citation key recorded'})"
            for claim in memory.claims_evidence_matrix[:12]
        ],
        "style_rules": [
            "Write reader-facing prose appropriate to the requested template and section purpose, not pipeline documentation. Material-based analysis need not be a long academic survey.",
            "Do not include pipeline/debug internals. In experiment/reproduction setup, explain material method, comparison conditions and execution limits. Identify the existing reproduction attachments for full commands and paths; duplicate these in the body only when the user requests it. Separate declarations from observed behavior.",
            "Do not create sections named Search Scope, Evidence Summary, Pipeline, Artifacts, or Stage Outputs.",
            "Do not use prompt-planning phrases such as Hint:, Use this paper as, Paper Brief, or Additional synthesis detail.",
            "Use the resolved document plan as the only local writing plan. Treat its section target and evidence set as planning guidance, not a license to pad prose.",
            "Apply each selected section's local constraints to that section, not separately to the whole document.",
            "The original user's whole-document requirements and current revision instructions take precedence over approximate section targets. A document_plan.length_budget reserves assembly text; section shares are not minimum lengths and must not be padded. Recheck complete delivery length, including its stated unresolved costs.",
            "Cover the planned analytical scope and then stop; do not add generic background merely to hit a number.",
            "For reviewer-directed revision, preserve supported content needed for the section; removing redundancy, unsupported claims or irrelevant details may shorten it substantially. For source-batch integration, preserve valid prior coverage.",
            "Use planned subsections as meaningful `###` headings in body sections when they improve navigation; do not add them to Abstract, Introduction or Conclusion just for uniformity.",
            "Embed only real supplied or generated image artifacts using their provided paths; never invent figure files. Deterministic rendering handles planned figures separately.",
            "Draft front-matter as if it is written after the body: Abstract and Introduction should summarize the actual synthesis, not generic background.",
            "Keep conclusions within the recorded evidence. Add a local qualification when it changes that assertion's interpretation; keep shared limits in the argument plan's scope section instead of repeating them throughout the article.",
            "Prepared execution context declares the requested project, dataset, benchmark and limits; it is not an observation. Describe actual execution from executor records or attributed producer outputs, retaining unverified conditions. Neither a declaration nor a literature setting overrides a recorded observation.",
            "Use verified_execution_results for local baseline/candidate metrics, comparison verdicts, deltas, and resource changes. Do not use literature citations to support local benchmark outcomes.",
            "If verified_execution_results shows that more than one implementation factor changed, describe that as a limitation or scope boundary rather than presenting a single-factor causal claim.",
            "Keep paragraphs under roughly 120 words; split dense synthesis into short paragraphs or concise bullets.",
            "Use only registered_citation_keys for body citation markers. Materials without keys use their supplied names or public URLs, not invented keys or internal handles.",
            "The final renderer will map short citation keys back to verified source ids and numeric citations.",
        ],
    }
    if section is not None:
        payload.update(section=section.model_dump(mode="json"),
            previous_draft=previous_draft.model_dump(mode="json") if previous_draft is not None and include_previous_draft else {},
            response_rules=["`draft_markdown` is the required primary payload. Put the complete Markdown prose there and never substitute `content`, `body`, or an explanation outside the JSON object.",
                "Return the outer section object, never a metadata record or an explanation."],
            output_schema=_writer_response_contract(section))
        selected = [section]
    else:
        selected = document_sections
        payload.update(task="jointly_draft_remaining_report_sections",
            sections=[{"section": row.model_dump(mode="json"), "section_constraints": _section_constraints(row),
                "length_requirement": _section_length_requirement(row),
                "visual_requirements": visual_requirements(memory.document_plan, row)} for row in selected],
            adopted_sections=[row.model_dump(mode="json") for row in (adopted_sections or [])],
            edit_scope={"eligible_section_ids": [row.section_id for row in selected],
                "read_only": "Adopted section bodies, frozen document plan/title, sources, measurements and assembly-owned attachments."},
            output_schema={"sections": [_writer_response_contract(row) for row in selected]},
            response_rules=["Return one object with sections: a list of complete existing section draft objects, exactly one per eligible section_id.",
                "draft_markdown contains that section's body, not the title, a References block or another section's body.",
                "Compose a coherent article jointly. Give quantitative detail and shared limitations a primary home; other sections synthesize or interpret rather than restating the same list.",
                "Do not change already adopted sections or the frozen plan. Each local section constraint applies to its corresponding section, not separately to the entire article.",
                "Assembly-owned content is appended by its owner. Account for its contribution to the requested article length; do not duplicate or claim to modify it."])
    genre = template.name if is_builtin_template(template, config) else ""
    if genre in {"survey", "survey_long"}:
        payload["style_rules"].append("Synthesize the source set by supported comparison dimensions and representative evidence, not a paper-by-paper dump. Do not invent a taxonomy or consensus from a single source.")
    if genre == "survey_long":
        payload["style_rules"].append("Use the frozen plan to cover foundations, construction, applications, evaluation, related surveys, challenges and future directions without repeating them in every section.")
    if any(visual_requirements(memory.document_plan, row)["tables"] for row in selected):
        payload["style_rules"].append(
            "For every required visual_requirements table, include one compact Markdown table in its designated section. "
            "Place `**Table: <planned title>**` immediately above it, preserve the planned comparison purpose, "
            "and use only evidence-supported cells with adjacent citations. Do not create placeholder rows."
        )
    return _source_bibliography_context(payload, source_front_matter or [], rules_key="response_rules")


def _writer_recovery_prompt(**kwargs: Any) -> str:
    """Keep the schema-first correction view available to controlled callers."""
    return _json_prompt(_writer_payload(**kwargs, recovery=True))


def _writer_response_contract(section: ReportSectionPlan) -> dict[str, Any]:
    """Describe the Writer response without embedding a competing claim schema."""
    return {
        "required": {
            "section_id": section.section_id,
            "heading": section.heading,
            "status": "drafted|revised|skipped",
            "draft_markdown": "non-empty Markdown body for this section only; no References",
        },
        "optional": {
            "used_sources": "list of source handle ids, or []",
            "metric_ids": "list of metric ids, or []",
            "citations": "list of short citation keys such as P1, or []",
            "open_questions": "list of strings, or []",
            "limitations": "list of strings, or []",
        },
    }


def _reviewer_prompt(
    *,
    context: ReportContext,
    template: ReportTemplateBundle,
    memory: ReportMemory,
    section: ReportSectionPlan,
    draft: ReportSectionDraft,
    adopted_sections: list[ReportSectionDraft] | None = None,
    revision_review: ReportSectionReview | None = None,
    previous_draft: ReportSectionDraft | None = None,
    extra_context: list[ReportToolResult] | None = None,
    config: ReportRuntimeConfig | None = None,
) -> str:
    return _json_prompt(_reviewer_context(context=context, template=template, memory=memory,
        section=section, draft=draft, adopted_sections=adopted_sections,
        revision_review=revision_review, previous_draft=previous_draft, extra_context=extra_context, config=config))


def _reviewer_context(
    *, context: ReportContext, template: ReportTemplateBundle, memory: ReportMemory,
    section: ReportSectionPlan, draft: ReportSectionDraft,
    adopted_sections: list[ReportSectionDraft] | None = None,
    revision_review: ReportSectionReview | None = None,
    previous_draft: ReportSectionDraft | None = None,
    extra_context: list[ReportToolResult] | None = None,
    config: ReportRuntimeConfig | None = None,
) -> dict[str, Any]:
    section_visuals = visual_requirements(memory.document_plan, section)
    genre = template.name if is_builtin_template(template, config) else ""
    read_tools = [spec for spec in report_tool_specs() if set(spec.permissions) == {"read"}]
    source_evidence = review_source_evidence(_handles_for_section(memory, section))
    review_context = list(extra_context or [])
    payload = {
        "task": "review_one_report_section",
        "report_mode": context.report_mode,
        "section": section.model_dump(mode="json", exclude={"goal"}),
        "section_constraints": _section_constraints(section),
        "criteria_markdown": reviewing_template_guidance(template, memory, config),
        "authored_template_requirements": template.template_markdown if not is_builtin_template(template, config) else "",
        "objective": report_objective(context, memory),
        "document_plan": document_plan_context(memory, independent_review=True),
        "narrative_context": narrative_context(memory, section, adopted_sections or [], context=context, config=config,
            current_draft=draft, independent_review=True),
        "revision_context": revision_context(revision_review, previous_draft, candidate=draft),
        **prior_document_revision(context, memory, include_baseline=True),
        "visual_requirements": section_visuals,
        "known_limitations": memory.limitations[:8],
        "experiment_plan": _compact_experiment_plan(context.experiment_plan),
        "allowed_sources": source_evidence,
        "metric_sources": _prompt_metrics(
            memory, section_id=section.section_id, metric_ids=draft.metric_ids
        ),
        "verified_execution_results": _compact_execution_results(context.results),
        "execution_evidence": report_execution_evidence(context),
        "draft": draft.model_dump(mode="json"),
        "extra_tool_context": [report_tool_context(row, source_evidence=source_evidence,
            evidence_reference='allowed_sources')
            for row in review_context],
        "extra_tool_context_omitted": len(extra_context or []) - len(review_context),
        "tool_policy": {
            "allowed_tools": [spec.name for spec in read_tools],
            "only_request_tools_when_evidence_is_insufficient": True,
            "search_source_chunks_arguments": {"handle": "one allowed source handle", "query": "specific claim or condition"},
            "prefer_get_paper_brief_arguments": {"citation_key": "P1"},
        },
        "context_tools": [{"name": spec.name, "description": spec.description, "input_schema": spec.input_schema}
                          for spec in read_tools],
        "review_focus": [
            *CLAIM_SCOPE_RULES,
            *REVIEW_ACTION_RULES,
            *EVIDENCE_QUOTE_RULES,
            "Does this section fulfill the requested template and its own purpose, rather than reading like a pipeline log? Analysis notes need not be a long academic survey.",
            "Are citations adjacent to paper-specific claims?",
            "If section_constraints include target_words, min_citations, or subsections, did the draft reasonably satisfy them without padding or unsupported citations?",
            "If planned subsections are present for a body section, does the draft use clear internal `###` headings or equivalent navigational structure? Do not require this for Abstract, Introduction, or Conclusion.",
            "Are long paragraphs split into readable units?",
            "Are operational details moved out of the body unless they are reader-facing limitations?",
            *(["Does the survey synthesize the source set by supported comparison dimensions rather than listing paper briefs? Do not invent a taxonomy or consensus from one source."] if genre in {"survey", "survey_long"} else []),
            *(["Across the frozen plan, does the long survey cover foundations, construction, applications, evaluation, related surveys, challenges and future directions without forcing them into every section?"] if genre == "survey_long" else []),
            "When visual_requirements.tables is non-empty, does this section realize every required table with its planned caption, meaningful columns, and evidence-supported cells? Request revision for a missing or placeholder table.",
            "Are benchmark limitations and transfer boundaries stated near empirical claims?",
            "Use registered result/metric values without promoting their surrounding declarations or interpretations to observed execution. Do not request a missing comparison table or metric merely because the deterministic evidence is appended after this review.",
            "If verified_execution_results shows multiple implementation changes or resource changes, ensure the draft states that limitation; do not treat the presence of those changes alone as an unsupported causal claim.",
            "Do not require literature citations for local baseline/candidate metrics; those values are supported by verified execution results and metric sources.",
            "Is it free of prompt residue such as Hint, Use this paper as, Paper Brief, or Additional synthesis detail?",
        ],
        "output_schema": {
            "section_id": section.section_id,
            "verdict": "pass|warning|revise_required|fail",
            "findings": [
                {
                    "finding_id": "stable id",
                    "type": "unsupported_claim|citation_misuse|missing_limitation|missing_visual|metric_mismatch|style",
                    "severity": "info|minor|major|critical",
                    "required_action": "advisory|revise|verify",
                    "message": "specific issue",
                    "section_id": section.section_id,
                    "claim_id": "",
                    "evidence_handles": [],
                    "suggested_action": "",
                    "draft_quotes": [{"section_id": section.section_id, "quote": "exact current draft text"}],
                    "evidence_quotes": [EVIDENCE_QUOTE_SCHEMA],
                }
            ],
            "context_requests": [
                {
                    "tool_name": "get_paper_brief",
                    "arguments": {"handle": "paper:..."},
                    "caller": "reviewer",
                    "trace_id": "optional",
                }
            ],
            "revision_instructions": [],
            "notes": "",
        },
    }
    payload["evidence_locator"] = review_evidence_locator(payload)
    return payload


def _json_prompt(payload: dict[str, Any]) -> str:
    return (
        "Return exactly one JSON object. Do not wrap it in Markdown fences.\n\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )








def _compact_survey_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, dict) or not contract.get("enabled"):
        return {}
    expected = contract.get("expected_coverage") if isinstance(contract.get("expected_coverage"), dict) else {}
    selection = contract.get("paper_selection") if isinstance(contract.get("paper_selection"), dict) else {}
    selected_papers = selection.get("selected_papers") if isinstance(selection.get("selected_papers"), list) else []
    taxonomy = contract.get("taxonomy") if isinstance(contract.get("taxonomy"), dict) else {}
    return {
        "schema_version": contract.get("schema_version", "survey_contract.v1"),
        "topic": contract.get("topic", ""),
        "objective": contract.get("objective", ""),
        "reader_needs": _string_items(contract.get("reader_needs"))[:6],
        "required_facets": _string_items(contract.get("required_facets"))[:10],
        "expected_coverage": expected,
        "selected_paper_brief": [
            {
                "cite_as": item.get("citation_key") or item.get("paper_id") or "",
                "title": str(item.get("title") or "")[:160],
                "role": item.get("role") or "",
                "facets": _string_items(item.get("facets"))[:4],
            }
            for item in selected_papers[:20]
            if isinstance(item, dict)
        ],
        "taxonomy_facets": [],
        "coverage_requirements": [
            {
                "label": facet.get("label") or "",
                "evidence_count": facet.get("evidence_count") or 0,
            }
            for facet in (taxonomy.get("coverage_facets") if isinstance(taxonomy.get("coverage_facets"), list) else [])[:10]
            if isinstance(facet, dict)
        ],
        "outline_sections": [],
        "citation_policy": contract.get("citation_policy") if isinstance(contract.get("citation_policy"), dict) else {},
        "boundaries": _string_items(contract.get("boundaries"))[:6],
    }




def _string_items(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        for key in (
            "text",
            "question",
            "limitation",
            "description",
            "message",
            "content",
            "body",
            "label",
            "title",
            "name",
            "notes",
        ):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return [candidate.strip()]
        return []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            items.extend(_string_items(item))
        return items
    return []


def _bind_section_target(normalized: dict[str, Any], section: ReportSectionPlan) -> None:
    """Bind absent/legacy empty identity, never relocate another explicit target."""
    supplied = normalized.get("section_id")
    if supplied not in (None, "", section.section_id):
        raise LLMResponseError(f"Section response targets {supplied!r}, not requested {section.section_id!r}.")
    normalized["section_id"] = section.section_id


def _normalize_draft_response(response: dict[str, Any], section: ReportSectionPlan) -> dict[str, Any]:
    normalized = dict(response)
    nested = normalized.get("draft")
    if isinstance(nested, dict):
        for key, value in nested.items():
            normalized.setdefault(key, value)
    nested_section = normalized.get("section")
    if isinstance(nested_section, dict):
        for key, value in nested_section.items():
            normalized.setdefault(key, value)
    if not str(normalized.get("draft_markdown") or "").strip():
        for key in ("markdown", "content", "body", "text", "section_markdown"):
            value = normalized.get(key)
            if isinstance(value, str) and value.strip():
                normalized["draft_markdown"] = value
                break
    _bind_section_target(normalized, section)
    normalized.setdefault("heading", section.heading)
    normalized.setdefault("status", "drafted")
    normalized.setdefault("draft_markdown", "")
    normalized.setdefault("used_sources", [])
    normalized.setdefault("metric_ids", [])
    normalized.setdefault("citations", [])
    normalized.setdefault("claims", [])
    normalized.setdefault("open_questions", [])
    normalized.setdefault("limitations", [])
    normalized["open_questions"] = _string_items(normalized["open_questions"])
    normalized["limitations"] = _string_items(normalized["limitations"])
    # Some models copy a claim-level evidence label such as ``supported`` into
    # the section-level status field.  The two enums intentionally differ:
    # preserve the draft and its claim statuses while normalizing the section
    # lifecycle state to the writer contract.
    if str(normalized.get("status") or "").strip().lower() not in {"drafted", "revised", "skipped"}:
        normalized["status"] = "drafted"
    return normalized


def _is_claim_record_response(response: dict[str, Any]) -> bool:
    """Identify a valid inner claim object mistakenly returned as a section."""
    if any(key in response for key in ("draft_markdown", "markdown", "content", "body", "text")):
        return False
    return bool(
        isinstance(response.get("claim_id"), str)
        and isinstance(response.get("claim"), str)
        and any(
            key in response
            for key in ("evidence_handles", "citation_ids", "metric_ids")
        )
    )


def _normalize_review_response(response: dict[str, Any], section: ReportSectionPlan) -> dict[str, Any]:
    normalized = dict(response)
    _bind_section_target(normalized, section)
    normalized.setdefault("verdict", "warning")
    normalized.setdefault("findings", [])
    normalized.setdefault("context_requests", [])
    normalized.setdefault("revision_instructions", [])
    normalized.setdefault("notes", "")
    normalized["findings"] = [
        _normalize_finding(finding, section, index)
        for index, finding in enumerate(normalized.get("findings") or [], start=1)
        if isinstance(finding, dict)
    ]
    normalized["context_requests"] = [
        _normalize_tool_call(call, index)
        for index, call in enumerate(normalized.get("context_requests") or [], start=1)
        if isinstance(call, dict)
    ]
    return normalized


def _normalize_finding(
    finding: dict[str, Any],
    section: ReportSectionPlan,
    index: int,
) -> dict[str, Any]:
    item = dict(finding)
    item.setdefault("finding_id", f"{section.section_id}-finding-{index:03d}")
    item.setdefault("type", "review")
    item.setdefault("severity", "minor")
    item.setdefault("message", item.get("suggested_action") or "Reviewer finding.")
    _bind_section_target(item, section)
    item.setdefault("claim_id", "")
    item.setdefault("evidence_handles", [])
    item.setdefault("suggested_action", "")
    return item


def _normalize_tool_call(call: dict[str, Any], index: int) -> dict[str, Any]:
    item = dict(call)
    item.setdefault("tool_name", "")
    item.setdefault("arguments", {})
    item.setdefault("caller", "reviewer")
    item.setdefault("trace_id", f"review-tool-{index:03d}")
    return item


def _mark_pending_evidence(review: ReportSectionReview) -> ReportSectionReview:
    """Pending evidence is neither a verified pass nor proof of a false claim."""
    if not review.context_requests:
        return review
    finding = ReviewerFinding(finding_id=f"{review.section_id}-source-verification-incomplete",
        type="source_verification_incomplete", severity="major", section_id=review.section_id,
        message="Reviewer still requires source evidence after the bounded lookup, or source backtracking is unavailable. No complete source verification was performed.",
        suggested_action="Qualify/remove the unresolved claim or provide the missing source material.")
    return review.model_copy(update={"verdict": "revise_required", "findings": [*review.findings, finding]})


def _run_context_requests(
    gateway: ReportToolGateway,
    review: ReportSectionReview,
    config: ReportRuntimeConfig,
) -> list[ReportToolResult]:
    if not config.allow_source_backtracking:
        return []
    results: list[ReportToolResult] = []
    for call in review.context_requests[: config.max_backtracking_calls]:
        if not call.tool_name:
            continue
        results.append(gateway.call(call))
    return results


def _handles_for_section(memory: ReportMemory, section: ReportSectionPlan) -> list[dict[str, Any]]:
    handles = {handle.handle: handle for handle in memory.source_handles if handle.kind != "prior_draft"}
    selected: list[str] = []
    for handle in section.evidence_handles:
        if handle in handles:
            selected.append(handle)
    if not selected:
        selected = list(handles)[:8]
    return [_prompt_handle_view(handles[handle]) for handle in selected]


def _source_batches(evidence_handles: list[str], config: ReportRuntimeConfig) -> list[list[str]]:
    handles = [handle for handle in evidence_handles if handle]
    if not handles:
        return [[]]
    if config.source_strategy != "batch_refine":
        return [handles]
    batch_size = max(1, config.source_batch_size)
    batches = [handles[index : index + batch_size] for index in range(0, len(handles), batch_size)]
    if config.max_source_batches > 0:
        return batches[: config.max_source_batches]
    return batches


def _section_with_evidence(section: ReportSectionPlan, evidence_handles: list[str]) -> ReportSectionPlan:
    return section.model_copy(update={"evidence_handles": list(evidence_handles)})


def _merge_revision_draft(
    previous: ReportSectionDraft,
    revised: ReportSectionDraft,
) -> ReportSectionDraft:
    """Use the complete revised section; old metadata is not evidence for its prose."""
    if revised.section_id != previous.section_id:
        raise ValueError("Report revision must preserve the section identity.")
    return revised


def _source_strategy_instruction(
    *,
    config: ReportRuntimeConfig,
    batch_index: int,
    batch_count: int,
) -> str:
    if config.source_strategy != "batch_refine" or batch_count <= 1:
        return "Draft using the provided source handles as the section evidence set."
    if batch_index <= 1:
        return (
            "Draft an initial section from the first source batch. Leave the "
            "structure easy to revise when later source batches arrive."
        )
    return (
        "Revise the complete previous section to integrate the newly supplied "
        "source batch. Preserve valid prior coverage, update comparisons and "
        "evidence-quality judgments compactly, and return one coherent section."
    )


def _section_constraints(
    section: ReportSectionPlan,
    *,
    include_subsections: bool = True,
) -> dict[str, Any]:
    constraints: dict[str, Any] = {
        "target_words": section.target_words if section.target_words > 0 else "",
        "min_citations": section.min_citations if section.min_citations > 0 else "",
        "subsections": section.subsections[:6] if include_subsections else [],
    }
    guidance: list[str] = []
    if section.target_words > 0:
        guidance.append(
            "Aim for this approximate section length, but prefer evidence-bounded prose over padding."
        )
    if section.min_citations > 0:
        guidance.append(
            "Use at least this many distinct allowed citations when the evidence set supports it."
        )
    if section.subsections and include_subsections:
        guidance.append(
            "Use the subsection list as navigational `###` headings or close equivalents."
        )
    constraints["guidance"] = guidance
    return constraints


def _section_length_requirement(section: ReportSectionPlan) -> str:
    target_words = max(0, int(section.target_words or 0))
    if target_words <= 0:
        return ""
    return (
        f"Draft approximately {target_words} substantive words for this section. "
        "Treat this as a document budget rather than a minimum to exceed: cover the requested "
        "evidence and then stop instead of expanding into paper-by-paper notes. Use multiple "
        "focused paragraphs and subsections when useful."
    )


def _revision_preservation_requirement(
    *,
    section: ReportSectionPlan,
    previous_draft: ReportSectionDraft | None,
    review: ReportSectionReview | None,
) -> str:
    if previous_draft is None or review is None:
        return ""
    if not previous_draft.draft_markdown.strip():
        return ""
    return (
        "This is a reviewer-directed revision, not a fresh summary. Return the complete "
        "revised section, preserving supported analysis, necessary qualifications and valid citations "
        "while addressing each finding and explicit instruction. Remove unsupported or duplicated "
        "content when needed; do not retain or add prose merely to preserve the previous word count."
    )


def _draft_sequence(sections: list[ReportSectionPlan]) -> list[ReportSectionPlan]:
    return sorted(
        sections,
        key=lambda section: (
            section.draft_order if section.draft_order else section.final_order,
            section.final_order,
            section.section_id,
        ),
    )


def _final_sequence(
    plans: list[ReportSectionPlan],
    drafts: list[ReportSectionDraft],
) -> list[ReportSectionDraft]:
    order = {plan.section_id: plan.final_order or index for index, plan in enumerate(plans, start=1)}
    return sorted(drafts, key=lambda draft: (order.get(draft.section_id, 9999), draft.section_id))




def _needs_evidence_recheck(review: ReportSectionReview) -> bool:
    """Check a verification-only request before spending a writing correction.

    Mixed corrections already fetch context and revise, avoiding a redundant
    model call. Missing/blocked evidence cannot turn the provisional check into
    acceptance: the subsequent review still passes through pending-evidence guards.
    Explicit verify-only findings own dispatch even if the aggregate verdict
    asks for revision; prose/legacy findings still keep their correction path.
    """
    return bool(review.context_requests) and (
        not _needs_revision(review)
        or (any(finding.required_action == "verify" for finding in review.findings) and all(
            not finding_requires_resolution(finding) or finding.required_action == "verify"
            for finding in review.findings
        ))
    )


def _record_draft_diagnostics(
    memory: ReportMemory,
    draft: ReportSectionDraft,
    findings: list[ReviewerFinding],
) -> None:
    # Current claims/questions/limits are projected at the checkpoint boundary.
    # Only diagnostic history accumulates; superseded draft notes do not.
    memory.reviewer_findings = _dedupe_findings(memory.reviewer_findings + findings)
    decision = f"Section `{draft.section_id}` drafted with {len(draft.used_sources)} source handle(s)."
    if decision not in memory.key_decisions:
        memory.key_decisions.append(decision)


def _dedupe_findings(findings: list[ReviewerFinding]) -> list[ReviewerFinding]:
    deduped: list[ReviewerFinding] = []
    for finding in findings:
        # Identity, action, severity and evidence all belong to the allegation.
        # Similar prose cannot erase distinct role records or requirements.
        if finding not in deduped:
            deduped.append(finding)
    return deduped


def _iteration(
    iteration: int,
    section: ReportSectionPlan,
    action: str,
    status: str,
    used_sources: list[str],
    *,
    findings: list[ReviewerFinding] | None = None,
    tool_results: list[ReportToolResult] | None = None,
    draft: ReportSectionDraft | None = None,
    revision_instructions: list[str] | None = None,
    finding_checks: list[ReportFindingCheck] | None = None,
    requested_findings: list[ReviewerFinding] | None = None,
) -> ReportIterationRecord:
    return ReportIterationRecord(
        iteration=iteration,
        section_id=section.section_id,
        action=action,
        status=status,
        summary=f"{action} `{section.heading}` -> {status}",
        used_sources=used_sources[:8],
        findings=findings or [],
        tool_results=tool_results or [],
        draft=draft.model_copy(deep=True) if draft is not None else None,
        revision_instructions=revision_instructions or [],
        finding_checks=finding_checks or [],
        requested_findings=requested_findings or [],
    )


def _emit(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)
