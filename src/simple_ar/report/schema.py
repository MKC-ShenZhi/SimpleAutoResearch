from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ReportModel(BaseModel):
    """Base model for report-domain artifacts.

    Report artifacts are part of the public run record. Unknown fields are
    ignored on load so older reports remain readable as the schema evolves.
    """

    model_config = ConfigDict(extra="ignore")


class ReportAuditConfig(ReportModel):
    """Mechanical audit switches for the report stage."""

    citations: bool = True
    metrics: bool = True
    claims: bool = True
    strict: bool = False


class ReportFigureConfig(ReportModel):
    """Deterministic figure generation for report artifacts.

    User-facing reports render supported figures by default. ``mode=\"off\"``
    or an explicit ``enabled=False`` remains the opt-out for text-only output.
    """

    enabled: bool = True
    # Zero selects supported planned diagrams or representative paired metrics;
    # a positive value is an explicit expert limit, not a target to fill.
    max_figures: int = 0
    format: Literal["svg"] = "svg"
    mode: Literal["auto", "off"] = "auto"


class ReportLongformConfig(ReportModel):
    """Long-form synthesis planning, coverage, and length controls.

    These options describe a long evidence synthesis target rather than any
    benchmark-specific output format. Survey templates are the first consumer,
    but the same controls can support long related-work sections, technical
    reviews, and reader-oriented research reports.
    """

    enabled: bool = True
    target_papers: int = 0
    min_papers: int = 0
    target_words: int = 0
    min_citations_per_section: int = 3
    target_tables: int = 0
    evidence_audit: bool = True


ReportSurveyConfig = ReportLongformConfig


class ReportRuntimeConfig(ReportModel):
    """User-facing report-stage configuration."""

    mode: str = "auto"
    template: str = "auto"
    criteria: str = "auto"
    style: str = "paper"
    section_numbering: Literal["auto", "off", "academic"] = "off"
    cost_profile: Literal["auto", "fast", "balanced", "thorough"] = "auto"
    outline_strategy: Literal["auto", "template", "adaptive"] = "auto"
    # Rechecked rows always remain in the copied analysis package. A prose
    # report need not repeat every row; expert/audit reports can request them.
    data_tables: Literal["linked", "full"] = "linked"
    draft_sections: bool = False
    debug_artifacts: bool = False
    allow_llm_fallback: bool = False
    agent: str = "llm"
    reviewer: str = "llm"
    max_review_iterations: int = 2
    # Optional until cross-document editing is validated on real long-form work.
    document_review: bool = False
    # Default preserves existing section-first tasks. Document-first writing
    # adopts validated drafts, then uses the same whole-document editor; it
    # never represents those drafts as individually reviewed.
    review_scope: Literal["section", "document"] = "section"
    # Explicit joint drafting shares one article context, but commits the same
    # section objects and leaves independent inspection to the existing editor.
    # Auto composes short planned documents jointly; longer/unsized plans use
    # the existing section checkpoints and the same whole-document review.
    draft_scope: Literal["section", "document", "auto"] = "section"
    # Evidence already has bounded retrieval windows. Do not impose a second
    # hidden character cap on the assembled request: the client and session
    # token ledger own capacity/cost. Experts may opt into a smaller window.
    max_document_review_prompt_chars: int = Field(default=0, ge=0)
    # Zero means that the report writer/reviewer does not add a per-call
    # provider output cap. The provider or session token budget still bounds
    # the run when configured. A positive value is an explicit expert limit.
    max_section_tokens: int = 0
    max_section_sources: int = 8
    # Optional user-specified bound on distinct cited sources in the final
    # document. Zero keeps source selection evidence-driven and unbounded here.
    max_cited_sources: int = Field(default=0, ge=0)
    source_strategy: Literal["full", "batch_refine"] = "full"
    source_batch_size: int = 10
    max_source_batches: int = 0
    review_trace: Literal["off", "meta", "full"] = "meta"
    output_mode: Literal["overwrite", "archive", "variant"] = "overwrite"
    output_label: str = ""
    allow_source_backtracking: bool = True
    max_backtracking_calls: int = 8
    max_backtracking_tokens: int = 6000
    figures: ReportFigureConfig = Field(default_factory=ReportFigureConfig)
    longform: ReportLongformConfig = Field(default_factory=ReportLongformConfig)
    audit: ReportAuditConfig = Field(default_factory=ReportAuditConfig)

    @model_validator(mode="after")
    def require_document_review(self) -> ReportRuntimeConfig:
        if self.review_scope == "document" and not self.document_review:
            raise ValueError('review_scope="document" requires document_review=true.')
        if self.draft_scope in {"document", "auto"} and (self.review_scope != "document" or self.source_strategy != "full"):
            raise ValueError('draft_scope="document" or "auto" requires review_scope="document" and source_strategy="full".')
        return self


class ReportTemplateBundle(ReportModel):
    """Loaded writing template and reviewer criteria."""

    name: str
    mode: str
    template_path: str
    criteria_path: str
    template_markdown: str
    criteria_markdown: str


class SourceHandle(ReportModel):
    """Stable, read-only pointer to a report source artifact."""

    handle: str
    kind: str
    citation_key: str = ""
    title: str = ""
    artifact: str = ""
    paper_id: str = ""
    chunk_id: str = ""
    section: str = ""
    summary: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricSource(ReportModel):
    """Metric value and provenance used by metric audit and tools."""

    metric_id: str
    name: str
    value: float | int | str
    artifact: str
    label: str = ""
    direction: str = ""
    measurement_id: str | None = None
    protocol_id: str | None = None
    protocol_revision: int | None = None
    protocol_fingerprint: str | None = None
    condition_id: str | None = None
    unit: str = ""
    source_kind: str = "legacy_unverified"


class ClaimEvidenceRecord(ReportModel):
    """Traceable relationship between a claim and supporting evidence."""

    claim_id: str
    claim: str
    status: Literal["supported", "partially_supported", "unsupported", "speculative"] = "speculative"
    evidence_handles: list[str] = Field(default_factory=list)
    metric_ids: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class ReportSectionPlan(ReportModel):
    """One planned report section."""

    section_id: str
    heading: str
    goal: str
    evidence_handles: list[str] = Field(default_factory=list)
    target_words: int = 0
    min_citations: int = 0
    subsections: list[str] = Field(default_factory=list)
    required: bool = True
    final_order: int = 0
    draft_order: int = 0


class ReportVisualIntent(ReportModel):
    """A planned visual justified by the resolved document evidence.

    Visuals are optional communication choices, not output quotas.  A table is
    authored in its owning section; a figure is rendered only when its chosen
    view is supported by the deterministic renderer.
    """

    visual_id: str
    kind: Literal["table", "figure"]
    title: str
    purpose: str
    section_id: str
    evidence_handles: list[str] = Field(default_factory=list)
    view: str = ""
    columns: list[str] = Field(default_factory=list)
    # None preserves old whole-package placement; an explicit list selects
    # inline figures without discarding the complete linked analysis package.
    figure_paths: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)


class ReportArgumentPoint(ReportModel):
    """One proposed reasoning step, not a certified source claim."""

    claim: str
    section_id: str
    evidence_handles: list[str] = Field(default_factory=list)
    metric_ids: list[str] = Field(default_factory=list)
    reasoning: str = ""
    qualifications: list[str] = Field(default_factory=list)
    counterevidence_handles: list[str] = Field(default_factory=list)


class ReportArgumentPlan(ReportModel):
    """Reader-facing argument owned by the existing frozen document plan."""

    question: str
    answer: str
    document_kind: str = ""
    points: list[ReportArgumentPoint] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    scope_section_id: str = ""
    technical_details_placement: Literal["appendix", "body", "linked_artifacts"] = "appendix"


class ReportDocumentPlan(ReportModel):
    """Frozen, single-source plan consumed by the report-stage agents."""

    schema_version: str = "report_document_plan.v1"
    status: Literal["resolved", "fallback"] = "resolved"
    title: str = ""
    sections: list[ReportSectionPlan] = Field(default_factory=list)
    target_words: int = 0
    visual_budget: dict[str, int] = Field(default_factory=dict)
    visual_intents: list[ReportVisualIntent] = Field(default_factory=list)
    length_budget: dict[str, Any] = Field(default_factory=dict, exclude_if=lambda value: not value)
    argument_plan: ReportArgumentPlan | None = Field(default=None, exclude_if=lambda value: value is None)
    notes: list[str] = Field(default_factory=list)


class ReportDraftQuote(ReportModel):
    """An exact heading/body quotation visible in an identified current section."""

    section_id: str
    quote: str


class ReportEvidenceQuote(ReportModel):
    """A source quotation or a controller-resolved current field selection."""

    pointer: str
    quote: str
    role: Literal["declaration", "executor_record", "producer_output", "registered_result",
                  "recorded_material", "derived_context"]
    mode: Literal["quotation", "field_reference"] = Field(
        default="quotation", exclude_if=lambda mode: mode == "quotation")


class ReviewerFinding(ReportModel):
    """Structured finding from reviewer or mechanical audit."""

    finding_id: str
    type: str
    severity: Literal["info", "minor", "major", "critical"] = "info"
    message: str
    section_id: str = ""
    claim_id: str = ""
    evidence_handles: list[str] = Field(default_factory=list)
    suggested_action: str = ""
    required_action: Literal["advisory", "revise", "verify"] | None = None
    draft_quotes: list[ReportDraftQuote] = Field(default_factory=list, exclude_if=lambda quotes: not quotes)
    evidence_quotes: list[ReportEvidenceQuote] = Field(default_factory=list, exclude_if=lambda quotes: not quotes)
    delivery_targets: list[str] = Field(default_factory=list, max_length=32,
        exclude_if=lambda targets: not targets,
        description='Explicit accused attachment text fields: absolute JSON Pointers into the current frozen delivery_artifacts. Evidence quotations are counter-evidence, not repair targets. Omit for prose-only findings. Producer ownership is controller-resolved, never model-selected.')


# A model may label a factual defect "minor" even when it remains unresolved.
# Keep revision and final audit aligned on which finding kinds need correction.
FACTUAL_REVIEW_FINDING_TYPES = frozenset({
    "metric_mismatch", "unsupported_claim", "citation_misuse", "source_verification_incomplete",
})


# Severity ranks impact; it is not an instruction to ignore a correction.
# Keep historical finding behavior when no explicit action was recorded.
def finding_requires_resolution(finding: ReviewerFinding) -> bool:
    return (
        finding.severity in {"major", "critical"}
        or (finding.severity == "minor" and finding.type in FACTUAL_REVIEW_FINDING_TYPES)
        or finding.required_action in {"revise", "verify"}
    )


REVIEW_ACTION_RULES = (
    "For each finding, separate impact severity from required_action: advisory, revise or verify.",
    "Use advisory for optional polish or explicitly retained evidence limits. Use revise for a concrete change needed in this draft, including consequential repetition or missing qualifications, even if severity is minor.",
    "Use verify when a consequential statement needs original evidence. Request an allowed source tool with a specific question, or instruct the Writer to remove or bound the unsupported assertion; do not call it verified without evidence.",
    "A corrected draft may retain a candid limitation as advisory. Do not keep a resolved correction active or turn every optional limitation into required work. No new experiments are authorized by a writing review.",
)


class ReportContext(ReportModel):
    """Compact report input assembled from earlier pipeline stages."""

    topic: str
    report_mode: str
    goal_markdown: str = ""
    problem_markdown: str = ""
    synthesis_markdown: str = ""
    hypothesis_markdown: str = ""
    evidence_summary: str = ""
    source_comparisons: list[dict[str, Any]] = Field(default_factory=list)
    execution_context: str = ""
    search_meta: dict[str, Any] = Field(default_factory=dict)
    experiment_plan: dict[str, Any] = Field(default_factory=dict)
    results: dict[str, Any] = Field(default_factory=dict)
    papers: list[dict[str, Any]] = Field(default_factory=list)
    source_handles: list[SourceHandle] = Field(default_factory=list)
    metric_sources: list[MetricSource] = Field(default_factory=list)
    citation_key_map: dict[str, str] = Field(default_factory=dict)
    max_section_sources: int = 8
    survey_contract: dict[str, Any] = Field(default_factory=dict)


class ReportMemory(ReportModel):
    """Compact recoverable state for section-wise report generation."""

    schema_version: int = 1
    objective: str = ""
    template: str = ""
    report_mode: str = ""
    survey_contract: dict[str, Any] = Field(default_factory=dict)
    section_plan: list[ReportSectionPlan] = Field(default_factory=list)
    document_plan: ReportDocumentPlan | None = None
    claims_evidence_matrix: list[ClaimEvidenceRecord] = Field(default_factory=list)
    source_handles: list[SourceHandle] = Field(default_factory=list)
    metric_sources: list[MetricSource] = Field(default_factory=list)
    reviewer_findings: list[ReviewerFinding] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    outline_planning: dict[str, Any] = Field(default_factory=dict)


class ReportToolSpec(ReportModel):
    """Tool contract that can be exported to local/OpenAI/MCP adapters."""

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permissions: list[str] = Field(default_factory=lambda: ["read"])
    max_calls: int = 8
    max_output_tokens: int = 1200


class ReportToolCall(ReportModel):
    """One bounded report tool request."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    caller: str = "report"
    trace_id: str = ""


class ReportToolResult(ReportModel):
    """Structured report tool result."""

    tool_name: str
    status: Literal["ok", "not_found", "blocked", "error"] = "ok"
    summary: str = ""
    content: dict[str, Any] = Field(default_factory=dict)
    source_handles: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReportSectionDraft(ReportModel):
    """Writer output for one report section."""

    section_id: str
    heading: str
    status: Literal["drafted", "revised", "skipped"] = "drafted"
    draft_markdown: str = ""
    used_sources: list[str] = Field(default_factory=list)
    metric_ids: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ReportFindingCheck(ReportModel):
    """A judgement of a prior opinion against the current adopted draft."""

    finding_id: str
    status: Literal["resolved", "not_applicable", "unresolved"]
    explanation: str
    # Old string quotations remain target-section-only, never guessed by text.
    draft_quotes: list[str | ReportDraftQuote] = Field(default_factory=list)
    evidence_quotes: list[ReportEvidenceQuote] = Field(default_factory=list, exclude_if=lambda quotes: not quotes)


class ProducerClaimCheck(ReportModel):
    """Independent adoption of an original, version-bound producer statement."""
    declaration_id: str = Field(min_length=1)
    status: Literal['satisfied_by_execution', 'accepted_limitation', 'unresolved_work']
    explanation: str = Field(min_length=1)
    draft_quotes: list[ReportDraftQuote] = Field(default_factory=list)
    evidence_quotes: list[ReportEvidenceQuote] = Field(default_factory=list)


class ReportSectionReview(ReportModel):
    """Reviewer output for one drafted report section."""

    section_id: str
    revision_scope: Literal["section", "document"] = Field(default="section", exclude_if=lambda value: value == "section")
    verdict: Literal["pass", "warning", "revise_required", "fail"] = "warning"
    findings: list[ReviewerFinding] = Field(default_factory=list)
    context_requests: list[ReportToolCall] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)
    notes: str = ""
    finding_checks: list[ReportFindingCheck] = Field(default_factory=list, exclude_if=lambda checks: not checks)
    producer_claim_checks: list[ProducerClaimCheck] = Field(default_factory=list,
        exclude_if=lambda checks: not checks)

    @field_validator("revision_instructions", mode="before")
    @classmethod
    def _coerce_revision_instructions(cls, value: object) -> list[str]:
        """Accept reviewer instructions returned as strings or small objects."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return [str(value)]
        instructions: list[str] = []
        for item in value:
            text = _revision_instruction_text(item)
            if text:
                instructions.append(text)
        return instructions


def _revision_instruction_text(value: object) -> str:
    """Return a compact instruction string from flexible reviewer output."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        preferred = (
            value.get("instruction")
            or value.get("revision_instruction")
            or value.get("suggested_action")
            or value.get("message")
            or value.get("reason")
        )
        prefix = str(value.get("finding_id") or value.get("id") or "").strip()
        body = str(preferred).strip() if preferred is not None else ""
        if prefix and body:
            return f"{prefix}: {body}"
        if body:
            return body
    return str(value).strip()


class ReportIterationRecord(ReportModel):
    """Compact trace entry for one writer/reviewer pass."""

    iteration: int
    section_id: str
    action: str
    status: str
    summary: str = ""
    used_sources: list[str] = Field(default_factory=list)
    findings: list[ReviewerFinding] = Field(default_factory=list)
    tool_results: list[ReportToolResult] = Field(default_factory=list)
    # Optional for historical checkpoints; retain candidates not adopted.
    draft: ReportSectionDraft | None = None
    adopted: bool | None = None
    # Review instructions belong to the existing review event, not a second ledger.
    revision_instructions: list[str] = Field(default_factory=list)
    finding_checks: list[ReportFindingCheck] = Field(default_factory=list, exclude_if=lambda checks: not checks)
    # Rejected structured judgments remain diagnostic evidence, never findings.
    rejected_review: dict[str, Any] = Field(default_factory=dict, exclude_if=lambda review: not review)
    requested_findings: list[ReviewerFinding] = Field(default_factory=list, exclude_if=lambda findings: not findings)
    # Joint corrections use the same trace/checkpoint, but retain a complete
    # candidate and its original per-section requests instead of mixed adoption.
    drafts: list[ReportSectionDraft] = Field(default_factory=list, exclude_if=lambda drafts: not drafts)
    section_reviews: list[ReportSectionReview] = Field(default_factory=list, exclude_if=lambda reviews: not reviews)
    # Writer-initiated reads use the same trace and gateway allowance.
    tool_requests: list[ReportToolCall] = Field(default_factory=list, exclude_if=lambda requests: not requests)
    independent_review: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)


class AgentReportResult(ReportModel):
    """Final result from the controlled report writer/reviewer loop."""

    report_body: str
    memory: ReportMemory
    sections: list[ReportSectionDraft] = Field(default_factory=list)
    iterations: list[ReportIterationRecord] = Field(default_factory=list)
    # Historical observations, including resolved/provisional ones. Current
    # unresolved findings live in memory.reviewer_findings and drive final audit.
    reviewer_findings: list[ReviewerFinding] = Field(default_factory=list)
    tool_results: list[ReportToolResult] = Field(default_factory=list)
    used_agent: bool = False
    independent_review: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)
    # Rejected prose and its actual negative inspection, never adopted delivery.
    pending_candidate: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None)


class CitationAudit(ReportModel):
    status: Literal["passed", "warning", "failed"] = "passed"
    known_citations: list[str] = Field(default_factory=list)
    unknown_citations: list[str] = Field(default_factory=list)
    unused_references: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MetricAudit(ReportModel):
    status: Literal["passed", "warning", "failed"] = "passed"
    matched_metrics: list[str] = Field(default_factory=list)
    unmatched_metrics: list[str] = Field(default_factory=list)
    # Read historical audits; new audits do not infer support from a number whitelist.
    unmatched_numbers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ClaimAudit(ReportModel):
    status: Literal["passed", "warning", "failed"] = "passed"
    claims: list[ClaimEvidenceRecord] = Field(default_factory=list)
    findings: list[ReviewerFinding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ReportAudit(ReportModel):
    """Compact report audit package written by V2.4 report stage."""

    schema_version: int = 1
    status: Literal["passed", "warning", "failed"] = "passed"
    semantic_review_status: Literal["semantic_unchecked"] = "semantic_unchecked"
    citation_audit: CitationAudit = Field(default_factory=CitationAudit)
    metric_audit: MetricAudit = Field(default_factory=MetricAudit)
    claim_audit: ClaimAudit = Field(default_factory=ClaimAudit)
    reviewer_findings: list[ReviewerFinding] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
