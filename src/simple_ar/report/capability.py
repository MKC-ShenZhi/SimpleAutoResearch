"""Explicit report assembly capability.

The report writing capability owns LLM planning, writing, and review. This
module exposes only the downstream boundary: section drafts become one
report artifact, with optional deterministic figure rendering.  It does not
choose sections, call an LLM, or run an audit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
import shutil
from typing import Any

from simple_ar.core.capabilities import ArtifactRef, CapabilityContext, CapabilityResult
from simple_ar.literature.bibtex import papers_to_bibtex
from simple_ar.literature.models import Paper
from simple_ar.literature.verify import CitationError, find_citation_ids, validate_citations
from simple_ar.report.assembler import (
    ReportAssemblyMode,
    apply_section_numbering,
    assemble_report_sections,
    split_report_references,
    validate_whole_article,
)
from simple_ar.report.citations import (
    append_references_section,
    cited_papers,
    citation_display_map,
    citation_map_artifact,
    display_citation_numbers,
    expand_short_citation_keys,
    normalize_bare_source_id_citations,
    sanitize_report_citations,
    strip_references_section,
)
from simple_ar.report.figures import ReportFigureRecord
from simple_ar.report.data_delivery import analysis_delivery_block, attach_delivery_block
from simple_ar.report.ports import DeterministicFigureRenderer, FigureRenderer
from simple_ar.report.schema import (
    ReportDocumentPlan,
    ReportContext,
    ReportRuntimeConfig,
    ReportSectionDraft,
)


@dataclass(frozen=True, slots=True)
class ReportAssemblyRequest:
    """Explicit sectional inputs or one completed article, with legacy defaults.

    Callers opt new whole-article work units in explicitly; historical partial
    assemblies must retain SECTION_DRAFTS rather than infer mode from labels.
    """

    title: str
    sections: tuple[ReportSectionDraft | Mapping[str, Any], ...]
    config: ReportRuntimeConfig | Mapping[str, Any] = field(
        default_factory=ReportRuntimeConfig
    )
    document_plan: ReportDocumentPlan | Mapping[str, Any] | None = None
    template_name: str = ""
    papers: tuple[Mapping[str, Any], ...] = ()
    citation_key_map: Mapping[str, str] = field(default_factory=dict)
    paired_comparisons: tuple[Mapping[str, Any], ...] = ()
    paired_summaries: tuple[Mapping[str, Any], ...] = ()
    table_analyses: tuple[ArtifactRef, ...] = ()
    # Source identity projected from the Writer snapshot, not another plan.
    analysis_handles: Mapping[str, str] = field(default_factory=dict)
    experiment_context: ReportContext | Mapping[str, Any] | None = None
    experiment_inputs: tuple[ArtifactRef, ...] = ()
    assembly_mode: ReportAssemblyMode | str = ReportAssemblyMode.SECTION_DRAFTS

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("ReportAssemblyRequest.title cannot be empty.")
        object.__setattr__(self, "sections", tuple(
            section if isinstance(section, ReportSectionDraft) else ReportSectionDraft.model_validate(section)
            for section in self.sections
        ))
        object.__setattr__(self, "assembly_mode", ReportAssemblyMode(self.assembly_mode))
        if self.assembly_mode == ReportAssemblyMode.WHOLE_ARTICLE:
            validate_whole_article(self.sections)
        if not isinstance(self.config, ReportRuntimeConfig):
            object.__setattr__(self, "config", ReportRuntimeConfig.model_validate(self.config))
        if self.document_plan is not None and not isinstance(self.document_plan, ReportDocumentPlan):
            object.__setattr__(self, "document_plan", ReportDocumentPlan.model_validate(self.document_plan))
        object.__setattr__(self, "papers", tuple(dict(paper) for paper in self.papers))
        object.__setattr__(self, "citation_key_map", dict(self.citation_key_map))
        object.__setattr__(self, "analysis_handles", dict(self.analysis_handles))
        if self.experiment_context is not None and not isinstance(self.experiment_context, ReportContext):
            object.__setattr__(self, "experiment_context", ReportContext.model_validate(self.experiment_context))
        object.__setattr__(self, "experiment_inputs", tuple(self.experiment_inputs))
        if self.experiment_inputs and (self.experiment_context is None or self.experiment_context.report_mode != "experiment"):
            raise ValueError("Experiment source copies require an experiment context.")


@dataclass(frozen=True, slots=True)
class ReportAssemblyResult:
    """Rendered report text, auditable body, and figure records produced for it."""

    report_markdown: str
    report_body_markdown: str = ""
    figures: tuple[ReportFigureRecord, ...] = ()
    removed_citations: tuple[str, ...] = ()
    citation_numbers: Mapping[str, int] = field(default_factory=dict)
    # Transient assembly output; persisted formats share these exact identities.
    cited_papers: tuple[Paper, ...] = ()

    @property
    def references_markdown(self) -> str:
        """Read the actual displayed bibliography, without regenerating it."""
        return split_report_references(self.report_markdown)[1]


def assemble_report_document(
    request: ReportAssemblyRequest,
    *,
    report_dir: Path,
    figure_renderer: FigureRenderer | None = None,
) -> ReportAssemblyResult:
    """Assemble explicit section drafts without changing their meaning."""

    report_body, papers, removed_citations, config, document_plan = _prepare_assembly_text(request)
    if request.paired_comparisons and figure_renderer is None:
        from simple_ar.report.figures import add_paired_measurement_figures
        # Bind by the accepted argument's exact metric IDs, never a heading
        # keyword. Ambiguous ownership retains the legacy comparison appendix.
        owners: dict[str, set[str]] = {}
        if document_plan and document_plan.argument_plan and request.experiment_context:
            names = {row.metric_id: row.name for row in request.experiment_context.metric_sources}
            for point in document_plan.argument_plan.points:
                for metric_id in point.metric_ids:
                    if metric_id in names:
                        owners.setdefault(names[metric_id], set()).add(point.section_id)
        headings = {row.section_id: row.heading for row in request.sections}
        anchors = {name: headings[next(iter(sections))] for name, sections in owners.items()
                   if len(sections) == 1 and next(iter(sections)) in headings}
        rendered = add_paired_measurement_figures(report_markdown=report_body, report_dir=report_dir,
            comparisons=list(request.paired_comparisons), summaries=list(request.paired_summaries),
            config=config.figures, anchors=anchors)
    else:
        # Whole articles already own their figures. Sectional saved config and
        # plans retain the original contract; a fresh default/template alone
        # must never trigger a second interpretation of the Writer's body.
        legacy_figures = ("figures" in config.model_fields_set
                          and (request.assembly_mode == ReportAssemblyMode.SECTION_DRAFTS or document_plan is not None))
        figure_sections = document_plan.sections if document_plan and request.assembly_mode == ReportAssemblyMode.WHOLE_ARTICLE else request.sections
        renderer = figure_renderer or DeterministicFigureRenderer(legacy=legacy_figures,
            section_headings=None if legacy_figures else {row.section_id: row.heading for row in figure_sections})
        rendered = renderer.render(
            report_markdown=report_body,
            report_dir=report_dir,
            config=config.figures,
            template_name=request.template_name,
            document_plan=document_plan,
        )
    # Keep figures in the citation-key body shared by Markdown, audit, and
    # downstream exports instead of leaving them only in the display report.
    report_body = rendered.report_markdown
    return replace(
        _display_report_text(report_body, papers, config, request.template_name),
        figures=tuple(rendered.figures),
        removed_citations=tuple(removed_citations),
    )


def preview_report_document(request: ReportAssemblyRequest) -> ReportAssemblyResult:
    """Use canonical text assembly without files or figure rendering.

    Registered attachment text must already be placed in request.sections.
    This predicts headings, citation display and references, not future renderer
    output, attachment rechecks or the scientific validity of the content.
    """
    body, papers, removed, config, _ = _prepare_assembly_text(request)
    return replace(
        _display_report_text(body, papers, config, request.template_name),
        removed_citations=tuple(removed),
    )


def _prepare_assembly_text(
    request: ReportAssemblyRequest,
) -> tuple[str, list[Paper], list[str], ReportRuntimeConfig, ReportDocumentPlan | None]:
    """The single text preparation owner for read-only preview and delivery."""

    if request.table_analyses:
        raise ValueError("Analysis packages require run_report_capability with registered inputs.")

    sections = request.sections
    if not sections or not any(section.draft_markdown.strip() for section in sections):
        raise ValueError("Report assembly requires at least one non-empty section draft.")

    config, document_plan = request.config, request.document_plan
    supplemental_sections: list[ReportSectionDraft] = []
    if request.experiment_context is not None:
        from simple_ar.report.projection import _append_verified_experiment_evidence
        projected = _append_verified_experiment_evidence(sections, request.experiment_context, config)
        if request.assembly_mode == ReportAssemblyMode.WHOLE_ARTICLE:
            supplemental_sections = list(projected[len(sections):])
        else:
            sections = projected
    report_body = assemble_report_sections(
        # The frozen plan is shared by drafting, review and final assembly.
        # Legacy/custom plans without a title retain the caller's heading.
        title=document_plan.title if document_plan and document_plan.title else request.title,
        sections=_order_sections(sections, document_plan),
        assembly_mode=request.assembly_mode,
        supplemental_sections=supplemental_sections,
    )
    report_body, papers, removed_citations = _prepare_report_citations(
        report_body,
        request.papers,
        request.citation_key_map,
    )
    return report_body, papers, removed_citations, config, document_plan


def _display_report_text(report_body: str, papers: list[Paper], config: ReportRuntimeConfig, template_name: str) -> ReportAssemblyResult:
    """Resolve final body references once, including renderer-inserted captions."""
    validate_citations(report_body, {paper.id for paper in papers})
    if config.max_cited_sources:
        from simple_ar.report.citations import body_citation_ids
        if len(body_citation_ids(report_body, {paper.id for paper in papers})) > config.max_cited_sources:
            raise ValueError('Final report exceeds max_cited_sources; no citations were silently dropped.')
    cited = cited_papers(report_body, papers)
    if papers and not cited:
        raise CitationError("Report body did not cite any paper from papers.jsonl")
    citation_map = citation_display_map(cited)
    report = append_references_section(
        display_citation_numbers(report_body, citation_map),
        cited,
        citation_map,
    )
    report = apply_section_numbering(
        report,
        mode=config.section_numbering,
        template_name=template_name,
        style=config.style,
    )
    return ReportAssemblyResult(report_markdown=report, report_body_markdown=report_body,
                                citation_numbers=citation_map, cited_papers=tuple(cited))


def _prepare_report_citations(
    report_body: str,
    paper_rows: tuple[Mapping[str, Any], ...],
    citation_key_map: Mapping[str, str],
) -> tuple[str, list[Paper], list[str]]:
    """Normalize the writer-facing body against the registered bibliography.

    Citation membership is checked here; metadata accuracy and support for
    individual claims are not independently certified by assembly.
    """

    papers = [Paper.from_row(dict(row)) for row in paper_rows]
    allowed_ids = {paper.id for paper in papers}
    body = strip_references_section(report_body)
    body = expand_short_citation_keys(body, dict(citation_key_map))
    body = normalize_bare_source_id_citations(body, allowed_ids)
    body, removed = sanitize_report_citations(body, allowed_ids)
    validate_citations(body, allowed_ids)
    if papers and not find_citation_ids(body):
        raise CitationError("Report body did not cite any paper from papers.jsonl")
    return body, papers, removed


def _order_sections(
    sections: tuple[ReportSectionDraft, ...],
    document_plan: ReportDocumentPlan | None,
) -> list[ReportSectionDraft]:
    """Apply the frozen document order without dropping unplanned drafts."""

    if document_plan is None or not document_plan.sections:
        return list(sections)

    ordered_plans = sorted(
        enumerate(document_plan.sections),
        key=lambda item: (
            item[1].final_order if item[1].final_order > 0 else item[0],
            item[0],
        ),
    )
    planned_order = {
        section.section_id: index
        for index, (_, section) in enumerate(ordered_plans)
    }
    return sorted(
        sections,
        key=lambda section: (planned_order.get(section.section_id, len(planned_order)),),
    )


def run_report_capability(
    *,
    context: CapabilityContext,
    request: ReportAssemblyRequest,
    figure_renderer: FigureRenderer | None = None,
) -> CapabilityResult:
    """Persist one assembled report through the session capability boundary."""

    request, attachments, imported_figures = _attach_table_analyses(context, request)
    attachments.extend(_attach_experiment_records(context, request))
    # Leave the default renderer implicit so the assembly boundary can choose
    # the structured paired-measurement renderer when experiment comparisons
    # are present.  A caller-supplied renderer remains authoritative.
    result = assemble_report_document(
        request,
        report_dir=context.store.root,
        figure_renderer=figure_renderer,
    )
    if imported_figures:
        result = replace(result, figures=(*result.figures, *imported_figures))
    config = request.config
    if config.figures.max_figures > 0 and len(result.figures) > config.figures.max_figures:
        raise ValueError("Supplied and generated figures exceed the explicit report max_figures; no figures were silently dropped.")
    report_ref = context.store.write_text(
        "report.md",
        result.report_markdown,
        kind="report",
        schema="report.v1",
        producer="report.assembly",
    )
    body_ref = context.store.write_text(
        "report_body.md",
        result.report_body_markdown,
        kind="report_body",
        schema="report_body.v1",
        producer="report.assembly",
    )
    cited = list(result.cited_papers)
    references_ref = context.store.write_text(
        "references.bib",
        papers_to_bibtex(cited),
        kind="report_references",
        schema="references.bib.v1",
        producer="report.assembly",
    )
    citation_map_ref = context.store.write_json(
        "citation_map.json",
        citation_map_artifact(dict(result.citation_numbers), cited, dict(request.citation_key_map)),
        kind="citation_map",
        schema="citation_map.v1",
        producer="report.assembly",
    )
    artifacts: list[ArtifactRef] = [report_ref, body_ref, references_ref, citation_map_ref, *attachments]
    artifacts.append(context.store.write_json(
        "citation_cleanup.json", {"removed_citations": list(result.removed_citations)},
        kind="citation_cleanup", schema="citation_cleanup.v1", producer="report.assembly",
    ))
    diagnostics: list[str] = []
    figure_refs: list[ArtifactRef] = []
    for figure in result.figures:
        for path in dict.fromkeys((figure.path, *figure.exports.values())):
            status = "available" if context.store.exists(path) else "missing"
            figure_refs.append(context.store.ref(
                path, kind="figure", schema="report_figure.v1",
                producer=f"report.{figure_renderer.name if figure_renderer is not None else DeterministicFigureRenderer.name}", status=status,  # type: ignore[arg-type]
            ))
            if status == "missing":
                diagnostics.append(f"Figure renderer reported a missing artifact: {path}.")
    artifacts.extend(figure_refs)
    if result.figures:
        manifest_ref = context.store.write_json(
            "figures/figures_manifest.json",
            {
                "schema_version": "report_figures.v1",
                "figure_count": len(result.figures),
                "figures": [figure.model_dump(mode="json") for figure in result.figures],
            },
            kind="report_figures",
            schema="report_figures.v1",
            producer="report.assembly",
        )
        artifacts.append(manifest_ref)
    return CapabilityResult(
        status="partial" if diagnostics else "completed",
        artifacts=tuple({ref.path: ref for ref in artifacts}.values()),
        diagnostics=tuple(diagnostics),
        usage={
            "section_count": len(request.sections),
            "figure_count": len(result.figures),
            "cited_paper_count": len(cited),
        },
        provenance={
            "capability": "report",
            "assembly": request.assembly_mode.value,
            "result_schema": "report.v1",
        },
    )


def _attach_experiment_records(context: CapabilityContext, request: ReportAssemblyRequest) -> list[ArtifactRef]:
    """Keep the exact projected records and explicitly registered source files.

    Fixed local links work in a session attempt or a standalone package. Old
    session paths remain provenance, never assumed relative download links.
    No directory discovery, model calls or scientific certification occurs.
    """
    from simple_ar.report.projection import experiment_record_snapshot, experiment_record_markdown

    recorded = request.experiment_context
    if recorded is None or recorded.report_mode != "experiment" or not recorded.metric_sources:
        return []
    attachments = []
    sources = []
    for index, ref in enumerate(dict.fromkeys(request.experiment_inputs), 1):
        path = context.require_input(ref)
        input_root = (context.input_store or context.store).root.resolve()
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(input_root):
            raise ValueError("Experiment records must be regular registered files inside their source store.")
        local = f"experiment_sources/{index:03d}{path.suffix}"
        target = context.store.resolve(local)
        if target.is_symlink() or not target.resolve().is_relative_to(context.store.root.resolve()):
            raise ValueError("Experiment copy destination must stay inside the report store.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        attachments.append(context.store.ref(local, kind="experiment_source_copy"))
        sources.append({"source": ref.to_dict(), "copied_path": local})
    attachments.append(context.store.write_json("experiment_evidence.json", {
        "schema_version": "report_experiment_evidence.v1", "records": experiment_record_snapshot(recorded),
        "source_artifacts": sources, "source_copy_scope": "explicit_registered_files_only" if sources else "projected_records_only",
        "interpretation": "Declarations, executor records, producer outputs and derived values keep their original ownership; this package does not independently verify implementation or science.",
    }, kind="report_experiment_evidence", schema="report_experiment_evidence.v1", producer="report.assembly"))
    attachments.append(context.store.write_text("experiment_evidence.md", experiment_record_markdown(recorded, sources),
        kind="report_experiment_evidence", schema="report_experiment_evidence.v1", producer="report.assembly"))
    return attachments


def _attach_table_analyses(
    context: CapabilityContext, request: ReportAssemblyRequest,
) -> tuple[ReportAssemblyRequest, list[ArtifactRef], list[ReportFigureRecord]]:
    """Copy registered data into the report; leave pure text assembly unchanged."""
    from simple_ar.result_analysis.table import copy_analysis_package

    config = request.config
    attachments: list[ArtifactRef] = []
    figures: list[ReportFigureRecord] = []
    sections = list(request.sections)
    plan = request.document_plan
    for index, ref in enumerate(request.table_analyses, start=1):
        prefix = f"analyses/analysis-{index:03d}"
        source = context.require_input(ref)
        if ref.schema == "code_analysis.v1":
            from simple_ar.result_analysis.script_project import copy_code_analysis_package
            result = copy_code_analysis_package(source, context.store.root / prefix)
        else:
            result = copy_analysis_package(source, context.store.root / prefix)
        block = analysis_delivery_block(result, index=index,
            handle=request.analysis_handles.get(ref.path, ""), config=config, plan=plan,
            section_ids=[row.section_id for row in sections])
        for figure in block["figures"]:
            figures.append(ReportFigureRecord(figure_id=f"supplied-analysis-{index}-{len(figures)+1}",
                title="Supplied descriptive data", path=figure["path"],
                caption=figure["caption"], source_artifacts=[ref.path], anchor=block["section_id"],
                exports=figure.get("exports", {})))
        sections = list(attach_delivery_block(sections, block))
        attachments.extend(context.store.ref(item.relative_to(context.store.root), kind="analysis_attachment")
                           for item in (context.store.root / prefix).rglob("*") if item.is_file())
    if request.table_analyses:
        request = replace(request, sections=tuple(sections), table_analyses=())
    return request, attachments, figures


__all__ = [
    "ReportAssemblyMode",
    "ReportAssemblyRequest",
    "ReportAssemblyResult",
    "assemble_report_document",
    "preview_report_document",
    "run_report_capability",
]
