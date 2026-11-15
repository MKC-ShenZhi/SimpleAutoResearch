from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from itertools import count
from typing import Any, Mapping

from simple_ar.core.capabilities import ArtifactRef, CapabilityContext, CapabilityResult
from simple_ar.literature.models import Paper, bibliographic_details
from simple_ar.report.document_plan import check_document_length, document_tokens
from simple_ar.report.narrative import report_objective
from simple_ar.report.projection import _declared_report_metrics, _verified_experiment_evidence
from simple_ar.report.schema import (
    FACTUAL_REVIEW_FINDING_TYPES,
    finding_requires_resolution,
    CitationAudit,
    ClaimAudit,
    MetricAudit,
    ReportAudit,
    ReportContext,
    ReportMemory,
    ReviewerFinding,
)


CITATION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_.:-]+)")
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:(?:\d{1,3}(?:,\d{3})+)|(?:\d+(?:\.\d+)?))"
    r"(?:[eE][-+]?\d+)?"
    r"(?:%|ms|s|sec|seconds)?(?![A-Za-z0-9_])"
)
METRIC_VISIBILITY_WARNING = (
    "Some experiment metrics could not be mechanically matched to their values in the report body; "
    "this does not establish omission or numerical contradiction."
)


@dataclass(frozen=True, slots=True)
class ReportAuditRequest:
    """Inputs for the standalone, side-effect-free report audit."""

    report: str
    report_body: str
    context: ReportContext
    memory: ReportMemory


@dataclass(frozen=True, slots=True)
class ReportAuditCapabilityRequest:
    """Explicit artifact inputs for a controller-managed report audit."""

    report_ref: ArtifactRef
    context: ReportContext | Mapping[str, Any]
    memory: ReportMemory | Mapping[str, Any]
    report_body_ref: ArtifactRef | None = None
    citation_cleanup_ref: ArtifactRef | None = None
    experiment_evidence_ref: ArtifactRef | None = None
    experiment_records_ref: ArtifactRef | None = None


def build_report_audit(
    *,
    report: str,
    report_body: str,
    context: ReportContext,
    memory: ReportMemory,
) -> ReportAudit:
    """Build compact mechanical audit for the final report."""
    citation = _citation_audit(report_body, context)
    metric = _metric_audit(report_body, context)
    claim = _claim_audit(memory)
    findings = citation.warnings + metric.warnings + claim.warnings
    reviewer_findings = (
        list(memory.reviewer_findings)
        + _mechanical_findings(findings, verify_messages={METRIC_VISIBILITY_WARNING}
                               if metric.unmatched_metrics else set())
        + _reader_facing_handle_findings(report_body)
        + _source_scope_findings(report_body, context)
        + _bibliographic_findings(report_body, context)
        + _document_length_findings(report, context, memory, report_body=report_body)
    )
    status = _overall_status([citation.status, metric.status, claim.status])
    if any(
        finding_requires_resolution(finding) and (finding.severity == "critical"
        or (finding.severity == "major" and finding.type in FACTUAL_REVIEW_FINDING_TYPES))
        for finding in reviewer_findings
    ):
        status = "failed"
    elif any(
        finding_requires_resolution(finding)
        for finding in reviewer_findings
    ) and status == "passed":
        status = "warning"
    return ReportAudit(
        status=status,
        citation_audit=citation,
        metric_audit=metric,
        claim_audit=claim,
        reviewer_findings=reviewer_findings,
        notes=[
            "V2.4 audit combines local rule gates with Writer/Reviewer findings when agent mode is enabled.",
            "Mechanical checks remain conservative and provenance-focused.",
            "Semantic support of final prose is unchecked; metric visibility and section review do not prove final conclusions.",
            "Length checking uses only an explicit task-anchored word budget and its frozen count scope. Its whitespace-separated Markdown token count is not language-independent word counting or semantic validation of that interpretation; absent contracts are not inferred.",
        ],
    )


def _document_length_findings(report: str, context: ReportContext, memory: ReportMemory, *, report_body: str) -> list[ReviewerFinding]:
    """Check final text against the existing frozen interpretation, not a new policy."""
    budget = memory.document_plan.length_budget if memory.document_plan else {}
    selected = report_body if budget.get("scope") in {"manuscript", "manuscript_body"} else report
    count = document_tokens(report_body, report, budget.get("scope", "")) if selected.strip() else None
    request = check_document_length(budget, objective=report_objective(context, memory),
        token_count=count)
    if request["status"] == "no_contract":
        return []  # Legacy prose requirements remain for review, not silent migration.
    if request["status"] == "invalid_contract":
        return [ReviewerFinding(finding_id="document-length-contract", type="delivery_length", severity="major",
            required_action="verify", message=f"Saved document length contract cannot be checked: {request['reason']}",
            suggested_action="Inspect the original task and frozen plan; do not infer replacement bounds or rewrite completed history.")]
    if request["status"] == "unavailable":
        return [ReviewerFinding(finding_id="document-length-unavailable", type="delivery_length", severity="major",
            required_action="verify", message="Final text is unavailable for its recorded length count scope.",
            suggested_action="Supply the actual final text in the recorded scope, not a planning forecast.")]
    actual = request["markdown_token_count"]
    if request["status"] in {"within_range", "target_only"}:
        return []
    return [ReviewerFinding(finding_id="document-length-outside-budget", type="delivery_length", severity="major",
        required_action="revise",
        message=f"Final report has {actual} whitespace-separated Markdown tokens in {request['scope']}, outside its recorded range {request['min_words']}–{request['max_words']}. Task quotation: {request['request_quote']}",
        suggested_action="Check the original requirement and counting convention, then revise the document within its existing allowance. Preserve necessary evidence; do not remove registered attachments or expand the budget to pass.")]


def _bibliographic_findings(report_body: str, context: ReportContext) -> list[ReviewerFinding]:
    """Only explicit recorded inconsistencies, not missing fields or self-certified identity."""
    cited = set(CITATION_PATTERN.findall(report_body))
    findings = []
    for row in context.papers:
        if str(row.get('id', '')) not in cited:
            continue
        paper = Paper.from_row(row)
        issues = bibliographic_details(paper)['consistency_issues']
        if issues:
            findings.append(ReviewerFinding(finding_id=f'bibliography:{paper.id}',
                type='citation_misuse', severity='minor', required_action='verify',
                message=' '.join(issues), evidence_handles=[f'paper:{paper.id}'],
                suggested_action='Check authoritative source metadata; preserve unknown identity and do not choose a version by title or earliest year.'))
    return findings


def _source_scope_findings(report_body: str, context: ReportContext) -> list[ReviewerFinding]:
    """Enforce an explicit user delivery bound, not a default source quota."""
    raw_limit = context.survey_contract.get("max_cited_sources")
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
        return []
    cited = set(CITATION_PATTERN.findall(report_body))
    if len(cited) <= raw_limit:
        return []
    return [ReviewerFinding(
        finding_id="source-scope-exceeded", type="delivery_scope", severity="critical",
        message=f"Report cites {len(cited)} distinct sources; the requested maximum is {raw_limit}.",
        suggested_action="Select the strongest supported sources and revise the report; do not delete citations without revising their claims.",
    )]


def _reader_facing_handle_findings(report_body: str) -> list[ReviewerFinding]:
    """Keep internal provenance handles out of a published report body.

    The source map and audit artifacts still retain those handles for inspection;
    this check only flags their accidental appearance in reader-facing prose.
    """
    if not re.search(r"(?<![A-Za-z0-9_])artifact:[A-Za-z0-9_.-]+", report_body):
        return []
    return [ReviewerFinding(
        finding_id="internal-artifact-handle",
        type="style",
        severity="major",
        message="An internal artifact handle appears in the reader-facing report body.",
        suggested_action="Replace the handle with a reader-facing source description; keep provenance in run artifacts.",
    )]


def audit_report(request: ReportAuditRequest) -> ReportAudit:
    """Audit one report without invoking the writer or changing artifacts."""

    return build_report_audit(
        report=request.report,
        report_body=request.report_body,
        context=request.context,
        memory=request.memory,
    )


def run_report_audit_capability(
    *,
    context: CapabilityContext,
    request: ReportAuditCapabilityRequest,
) -> CapabilityResult:
    """Audit explicit report artifacts through the session boundary.

    The adapter preserves the existing ``report_audit.json`` shape and leaves
    writer/revision policy to the caller. A separate body reference is
    optional because callers that do not persist a pre-reference report can
    audit the same text for both the final report and its body.
    """

    report = context.read_input_text(request.report_ref)
    report_body = (
        context.read_input_text(request.report_body_ref)
        if request.report_body_ref is not None
        else report
    )
    report_context = (
        request.context
        if isinstance(request.context, ReportContext)
        else ReportContext.model_validate(request.context)
    )
    report_memory = (
        request.memory
        if isinstance(request.memory, ReportMemory)
        else ReportMemory.model_validate(request.memory)
    )
    audit = audit_report(
        ReportAuditRequest(
            report=report,
            report_body=report_body,
            context=report_context,
            memory=report_memory,
        )
    )
    attachment_error = ""
    if (request.experiment_evidence_ref is None and request.experiment_records_ref is None
            and re.search(r"\]\(experiment_evidence\.(?:md|json)\)", report_body)):
        attachment_error = "Report links native experiment records but no registered record package was supplied to audit."
    if request.experiment_evidence_ref is not None or request.experiment_records_ref is not None:
        if request.experiment_evidence_ref is None or request.experiment_records_ref is None:
            raise ValueError("Experiment attachment audit requires both recorded JSON and Markdown inputs.")
        from simple_ar.report.projection import experiment_record_snapshot, experiment_record_markdown
        package = context.read_input_json(request.experiment_evidence_ref)
        records_text = context.read_input_text(request.experiment_records_ref)
        sources = package.get("source_artifacts", [])
        if not isinstance(sources, list) or any(not isinstance(row, dict)
                or not isinstance(row.get("source"), dict) or not isinstance(row["source"].get("path"), str)
                or not isinstance(row.get("copied_path"), str) for row in sources):
            raise ValueError("Experiment attachment source manifest is malformed.")
        directory = context.require_input(request.experiment_evidence_ref).parent.resolve()
        files_present = all(re.fullmatch(r"experiment_sources/[0-9]{3}(?:\.[A-Za-z0-9_-]+)?", row["copied_path"])
            and (directory / row["copied_path"]).is_file()
            and not (directory / row["copied_path"]).is_symlink()
            and (directory / row["copied_path"]).resolve().is_relative_to(directory) for row in sources)
        if (package.get("schema_version") != "report_experiment_evidence.v1"
                or package.get("records") != experiment_record_snapshot(report_context)
                or records_text != experiment_record_markdown(report_context, sources) or not files_present):
            attachment_error = "Experiment attachment differs from its recorded report inputs or a declared source copy is missing."
    if attachment_error:
        audit.metric_audit.status = "failed"
        audit.metric_audit.warnings.append(attachment_error)
        audit.status = "failed"
        audit.reviewer_findings.append(ReviewerFinding(finding_id="experiment-attachment-consistency",
            type="metric_mismatch", severity="major", message=attachment_error))
    if request.citation_cleanup_ref is not None:
        removed = context.read_input_json(request.citation_cleanup_ref)["removed_citations"]
        if removed:
            message = "Assembly removed unknown citations; associated claims need revision: " + ", ".join(removed)
            audit.citation_audit.unknown_citations = sorted(set(audit.citation_audit.unknown_citations) | set(removed))
            audit.citation_audit.warnings.append(message)
            audit.citation_audit.status = "failed"
            audit.status = "failed"
            audit.reviewer_findings.append(ReviewerFinding(
                finding_id="citation-cleanup", type="unresolved_citation", severity="major",
                message=message, suggested_action="Revise the affected claims against existing evidence; do not only delete citation markers.",
            ))
    output = context.store.write_json(
        "report_audit.json",
        audit.model_dump(mode="json"),
        kind="report_audit",
        schema="report_audit.v1",
        producer="report.audit",
    )
    warnings = (
        *audit.citation_audit.warnings,
        *audit.metric_audit.warnings,
        *audit.claim_audit.warnings,
    )
    capability_status = {
        "passed": "completed",
        "warning": "partial",
        "failed": "failed",
    }[audit.status]
    return CapabilityResult(
        status=capability_status,  # type: ignore[arg-type]
        artifacts=(output,),
        diagnostics=tuple(warnings),
        usage={
            "audit_status": audit.status,
            "citation_warning_count": len(audit.citation_audit.warnings),
            "metric_warning_count": len(audit.metric_audit.warnings),
            "claim_warning_count": len(audit.claim_audit.warnings),
        },
        provenance={
            "capability": "report_audit",
            "report_ref": request.report_ref.path,
            "report_body_ref": (
                request.report_body_ref.path if request.report_body_ref is not None else ""
            ),
            "result_schema": "report_audit.v1",
        },
    )


def _citation_audit(report_body: str, context: ReportContext) -> CitationAudit:
    known = {
        str(paper.get("id"))
        for paper in context.papers
        if isinstance(paper, dict) and str(paper.get("id") or "").strip()
    }
    found = set(CITATION_PATTERN.findall(report_body))
    unknown = sorted(found - known)
    unused = sorted(known - found)
    warnings: list[str] = []
    status = "passed"
    if unknown:
        warnings.append("Report contains citation ids that are not in papers.jsonl.")
        status = "failed"
    if known and not found:
        warnings.append("Report has paper metadata but no body citations.")
        status = "failed"
    # ``context.papers`` is the selected source pool, not the final reference
    # list.  Report assembly intentionally prunes that pool to body-cited
    # papers before writing References/references.bib.  Keep the unused ids in
    # the audit for provenance, but do not turn normal source selection into a
    # report warning.  Unknown citations and a completely uncited paper pool
    # remain hard quality signals above.
    return CitationAudit(
        status=status,
        known_citations=sorted(found & known),
        unknown_citations=unknown,
        unused_references=unused,
        warnings=warnings,
    )


def _metric_audit(report_body: str, context: ReportContext) -> MetricAudit:
    """Check metric visibility and ledger attribution, not arbitrary prose numbers."""
    if not context.metric_sources:
        errors = _measurement_table_errors(report_body, context)
        return MetricAudit(status="failed" if errors else "passed", warnings=errors)
    metrics = _report_metric_sources(context)
    # A generated figure's filename (for example, paired-1.svg) is not a
    # reported numeric result even when its alt text names a metric.
    visibility_body = re.sub(r"(?m)^[ \t]*!\[[^\]\r\n]*\]\([^\r\n]*\)[ \t]*$", "", report_body)
    matched: list[str] = []
    unmatched: list[str] = []
    for metric in metrics:
        if _metric_is_visible(visibility_body, metric):
            matched.append(metric.metric_id)
        else:
            unmatched.append(metric.metric_id)
    warnings: list[str] = []
    status = "passed"
    if unmatched:
        warnings.append(METRIC_VISIBILITY_WARNING)
        status = "warning"
    table_errors = [
        *_measurement_table_errors(report_body, context),
        *_verified_experiment_table_errors(report_body, context),
    ]
    if table_errors:
        warnings.extend(table_errors)
        status = "failed"
    return MetricAudit(
        status=status,
        matched_metrics=matched,
        unmatched_metrics=unmatched,
        warnings=warnings,
    )


def _measurement_table_errors(report_body: str, context: ReportContext) -> list[str]:
    """Check our structured metric ledger, not arbitrary natural-language tables."""
    header = ["Source", "Metric", "Value", "Unit", "Condition", "Origin"]

    def cell(value: str) -> str:
        return value.replace("|", "/").replace("\n", " ").strip()

    expected = {
        (cell(metric.label or "experiment"), f"`{cell(metric.name)}`", _format_metric(metric.value),
         cell(metric.unit or "not recorded"), cell(metric.condition_id or "not recorded"), cell(metric.source_kind))
        for metric in context.metric_sources
    }
    errors = []
    in_table = False
    for line_number, line in enumerate(report_body.splitlines(), 1):
        if not line.strip().startswith("|"):
            in_table = False
            continue
        row = [part.strip() for part in line.strip().strip("|").split("|")]
        if row == header:
            in_table = True
            continue
        if not in_table or all(re.fullmatch(r":?-+:?", part) for part in row):
            continue
        if tuple(row) not in expected:
            errors.append(f"Measurement table row {line_number} does not match its source, metric, value, unit, condition and origin.")
    return errors


def _verified_experiment_table_errors(report_body: str, context: ReportContext) -> list[str]:
    """Check rendered experiment tables against the same frozen evidence used to create them.

    This is a consistency check for our structured tables, not a semantic
    review of arbitrary prose or a guarantee that the source measurements are
    scientifically valid. Reports without these tables remain auditable as
    prose-only reports; a present but altered table cannot pass by repeating
    its numbers elsewhere in the document.
    """
    if context.report_mode != "experiment" or not context.metric_sources:
        return []
    expected = _markdown_tables(_verified_experiment_evidence(context))
    actual = _markdown_tables(report_body)
    errors: list[str] = []
    for header, expected_rows in expected.items():
        if header not in actual:
            if re.search(r"(?im)^#{1,6}\s+Verified Experiment Metrics\s*$", report_body):
                errors.append(f"Verified experiment table is missing: {header[0]}.")
            continue
        if Counter(actual[header]) != Counter(expected_rows):
            errors.append(f"Verified experiment table differs from persisted evidence: {header[0]}.")
    return errors


def _markdown_tables(body: str) -> dict[tuple[str, ...], list[tuple[str, ...]]]:
    """Index simple Markdown tables by header without trusting their numbers."""
    tables: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    header: tuple[str, ...] | None = None
    for line in body.splitlines():
        if not line.strip().startswith("|"):
            header = None
            continue
        cells = tuple(part.strip() for part in line.strip().strip("|").split("|"))
        if header is None:
            header = cells
            tables.setdefault(header, [])
        elif all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
            continue
        else:
            tables[header].append(cells)
    return tables


def _claim_audit(memory: ReportMemory) -> ClaimAudit:
    findings: list[ReviewerFinding] = []
    for claim in memory.claims_evidence_matrix:
        # ``unsupported`` may mean either a measured negative result or an
        # untested hypothesis. This audit sees the structured claim record,
        # not whether the prose asserts the hypothesis as a fact. An empty
        # evidence link is a provenance gap, never proof of a false statement
        # in the report; semantic review remains a separate responsibility.
        if claim.status == "unsupported" and not (
            claim.evidence_handles or claim.metric_ids or claim.citation_ids
        ):
            findings.append(
                ReviewerFinding(
                    finding_id=f"claim-{len(findings)+1:03d}",
                    type="unlinked_analysis_claim",
                    severity="minor",
                    message=f"Analysis claim has no linked evidence or measurement: {claim.claim}",
                    claim_id=claim.claim_id,
                    suggested_action="Link relevant observations if available; otherwise describe it as untested, not refuted.",
                )
            )
    status = "warning" if findings else "passed"
    return ClaimAudit(
        status=status,
        claims=memory.claims_evidence_matrix,
        findings=findings,
        warnings=[finding.message for finding in findings],
    )


def _mechanical_findings(messages: list[str], *, verify_messages: set[str] | None = None) -> list[ReviewerFinding]:
    ids = count(1)
    return [
        ReviewerFinding(
            finding_id=f"audit-{next(ids):03d}",
            type="mechanical_audit",
            severity="minor" if "not cited" in message.lower() else "major",
            message=message,
            required_action="verify" if message in (verify_messages or ()) else None,
            suggested_action=("Inspect the current prose or table against the registered metric, condition, unit and displayed precision. "
                "Do not infer omission from a failed literal match or force raw field names into the article. "
                "Revise only a confirmed missing or incorrect result; unresolved associations remain unverified."
                if message in (verify_messages or ()) else ""),
        )
        for message in messages
    ]




def _metric_is_visible(report_body: str, metric: Any) -> bool:
    """Require a metric name and its value in the same prose line or table row."""

    names = _metric_name_variants(str(metric.name))
    value_variants = _metric_value_variants(metric.value)
    expected_numeric = {
        numeric
        for value_text in value_variants
        if (numeric := _numeric_token_key(value_text)) is not None
    }
    for line in report_body.splitlines():
        if not any(_contains_phrase(line.lower(), name) for name in names):
            continue
        # Numeric metric names (for example pass@1) are labels, not measured
        # values. Remove only the matched label before looking for a number.
        values_line = line
        for name in sorted(names, key=len, reverse=True):
            values_line = re.sub(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                " ", values_line, flags=re.IGNORECASE,
            )
        if any(_contains_phrase(values_line.lower(), value_text.lower()) for value_text in value_variants):
            return True
        citation_free = CITATION_PATTERN.sub("", values_line)
        if any(
            (numeric := _numeric_token_key(value_text)) is not None
            and numeric in expected_numeric
            for value_text in NUMBER_PATTERN.findall(citation_free)
        ):
            return True
    return False


def _report_metric_sources(context: ReportContext) -> list[Any]:
    """Audit report-level metrics, not every detailed measurement in the source."""
    summaries = context.results.get("paired_summary") if isinstance(context.results, Mapping) else None
    if isinstance(summaries, list) and summaries:
        aggregate_names = {
            str(row.get("metric") or "").strip()
            for row in summaries
            if isinstance(row, Mapping)
            and str(row.get("metric") or "").strip()
            and "_after_task_" not in str(row.get("metric") or "")
        }
        selected = [
            metric for metric in context.metric_sources
            if metric.label.startswith("paired_summary:")
            and metric.name.split(".", 1)[0] in aggregate_names
        ]
        if selected:
            return selected

    # The report deliberately links detailed per-task results instead of
    # reproducing every source row. Its own appendix uses the declared primary
    # and required metrics, so the audit must check that same scope. Otherwise
    # a concise, correctly linked report receives a spurious major finding.
    _, declared = _declared_report_metrics(context)
    if declared is not None:
        selected = [metric for metric in context.metric_sources if metric.name in declared]
        if selected:
            return selected
    return context.metric_sources


def _metric_name_variants(name: str) -> set[str]:
    raw = name.lower().strip()
    normalized = re.sub(r"[_-]+", " ", raw).strip()
    if not normalized:
        return set()
    variants = {normalized, raw}
    if "." in raw:
        base = raw.split(".", 1)[0].strip()
        variants.update({base, re.sub(r"[_-]+", " ", base).strip()})
    words = normalized.split()
    if words and words[-1] in {
        "s",
        "sec",
        "secs",
        "second",
        "seconds",
        "ms",
        "millisecond",
        "milliseconds",
    }:
        variants.add(" ".join(words[:-1]))
    aliases = {
        "train time": "training time",
        "eval examples": "evaluation examples",
        "evaluation examples": "eval examples",
    }
    for variant in tuple(variants):
        alias = aliases.get(variant)
        if alias:
            variants.add(alias)
    if normalized == "eval examples":
        variants.add("evaluation set")
    return {variant for variant in variants if variant}


def _metric_value_variants(value: Any) -> set[str]:
    variants = {_format_metric(value), str(value)}
    if isinstance(value, float):
        variants.add(format(value, ".12g"))
        if value.is_integer():
            variants.add(str(int(value)))
    return {variant for variant in variants if variant}


def _numeric_token_key(value: object) -> tuple[Decimal, str] | None:
    """Normalize a report number while preserving an optional display unit."""

    match = re.fullmatch(
        r"\s*([-+]?(?:(?:\d{1,3}(?:,\d{3})+)|(?:\d+(?:\.\d+)?))"
        r"(?:[eE][-+]?\d+)?)(%|ms|s|sec|seconds)?\s*",
        str(value),
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    try:
        number = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    unit = (match.group(2) or "").lower()
    if unit == "seconds":
        unit = "sec"
    return number, unit


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(
        rf"(?<![A-Za-z0-9_]){re.escape(phrase)}(?![A-Za-z0-9_])",
        text,
    ) is not None




def _format_metric(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _overall_status(statuses: list[str]) -> str:
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    return "passed"
