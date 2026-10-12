"""Project persisted research evidence into report-domain inputs.

This module contains the pure joining and formatting logic needed by the
report writer and audit stages.  It does not own session lifecycle, artifact
writing, or report execution.  The application layer may translate
``ReportProjectionError`` into a product-specific error, while canonical
report callers can use this module without importing a legacy application
entry point.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path, PurePosixPath
from posixpath import relpath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from simple_ar.core import ArtifactRef, ArtifactStore
from simple_ar.literature.models import Paper, bibliographic_details
from simple_ar.report.schema import (
    ClaimEvidenceRecord,
    MetricSource,
    ReportContext,
    ReportMemory,
    ReportRuntimeConfig,
    ReportSectionDraft,
    SourceHandle,
)
from simple_ar.research.design import ResearchDesignResult
from simple_ar.research.contracts import DocumentRecord, ResearchExperimentContract
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.evidence.reader import reading_followup_context
from simple_ar.research.evidence.bibliography import apply_bibliographic_note, local_citation_fields_to_fill
from simple_ar.research.sources import SearchResult
from simple_ar.research.synthesis import SynthesisResult
from simple_ar.result_analysis.schema import AnalysisResult

_RESEARCH_CONTEXT_FIELDS = (
    "hypothesis",
    "motivation_refs",
    "expected_outcome",
    "proposed_change",
    "implementation_scope",
    "validation_hints",
    "risks",
    "report_claim_plan",
)


def _qualified_outputs(result: Mapping[str, Any], ref: ArtifactRef, store: ArtifactStore | None = None) -> list[dict[str, Any]]:
    """Qualify attempt-local producer attachments against their result owner."""
    rows = result.get("output_evidence", [])
    qualified = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        item = dict(row)
        if item.get("artifact"):
            local = ArtifactRef(path=str(item["artifact"]))
            item["artifact"] = str(PurePosixPath(ref.path).parent / local.path)
            item["handle"] = f"output:{item['artifact']}"
            if store is not None and item.get("status") == "available":
                from simple_ar.experiment.execution.outputs import read_output_window, PREVIEW_CHARACTERS
                try:
                    item["preview"] = read_output_window(store.root, store.resolve(item["artifact"]),
                        limit=PREVIEW_CHARACTERS, overview=True)
                except (OSError, ValueError, UnicodeError) as exc:
                    # Retain the saved observation, but disclose that the
                    # attachment is no longer available for a fresh reading.
                    item["preview_refresh_error"] = str(exc)
        item["measurement_artifact"] = ref.path
        qualified.append(item)
    return qualified


def _output_handles(rows: list[dict[str, Any]]) -> list[SourceHandle]:
    return [SourceHandle(handle=row["handle"], kind="experiment_output",
                         artifact=row["artifact"], title=row.get("name", ""),
                         summary="Recorded producer text, not independent implementation verification.")
            for row in rows if row.get("status") == "available" and row.get("handle")]
from simple_ar.report.execution_evidence import execution_record

if TYPE_CHECKING:
    from simple_ar.research.evidence.reader import ReadResult


class ReportProjectionError(ValueError):
    """Raised when persisted research facts are insufficient for a report."""


def build_implementation_report_inputs(*, topic: str, store: ArtifactStore,
        implementation_ref: ArtifactRef, lineage_refs: Sequence[ArtifactRef] = (),
        documents=None, documents_ref=None, read=None, read_ref=None):
    """Use actual code checks and optional source evidence without a fake study."""
    if documents is not None:
        from simple_ar.research.sources.capability import provided_materials_result
        context, memory = build_literature_report_inputs(topic=topic, brief=None,
            search=provided_materials_result(documents.records), documents=documents, brief_ref=None)
        if read is not None:
            context, memory = attach_report_read_evidence(context, memory, documents=documents,
                read=read, read_ref=read_ref, documents_ref=documents_ref)
    else:
        context = ReportContext(topic=topic, report_mode="supplied_materials")
        memory = ReportMemory(objective=topic, report_mode="supplied_materials")
    context.report_mode = memory.report_mode = "supplied_materials"
    attach_implementation_evidence(context, store, implementation_ref, lineage_refs=lineage_refs)
    memory.source_handles = list(context.source_handles)
    memory.limitations.append("Code validation establishes the recorded behavior only; it is not a scientific measurement or evidence of performance improvement.")
    return context, memory


def attach_implementation_evidence(
    context: ReportContext,
    store: ArtifactStore,
    implementation_ref: ArtifactRef,
    *,
    lineage_refs: Sequence[ArtifactRef] = (),
) -> None:
    """Expose the cumulative implementation lineage through report tools.

    A repair attempt stores only its incremental patch.  When a report points
    at that attempt, the initial implementation must remain visible as well;
    otherwise the report can mistake a repair-only diff for the whole change.
    ``implementation_ref`` is the final revision and ``lineage_refs`` are
    earlier implementation results in chronological order.
    """
    ordered_refs: list[ArtifactRef] = []
    seen: set[str] = set()
    for ref in (*lineage_refs, implementation_ref):
        if ref.path in seen:
            continue
        seen.add(ref.path)
        ordered_refs.append(ref)

    revisions: list[dict[str, Any]] = []
    for ref in ordered_refs:
        record = store.read_json(ref)
        method_validation = record.get("method_validation")
        if not isinstance(method_validation, Mapping):
            method_validation = {
                "status": "未检查",
                "reason": "No candidate-specific behavior criterion and observed result were recorded together.",
            }
        evidence: dict[str, dict[str, Any]] = {}
        for name in ("patch", "source_context", "validation", "validation_report", "validation_stdout", "review"):
            artifact_ref = record.get("artifact_refs", {}).get(name)
            if artifact_ref is None:
                continue
            path = Path(ref.path).parent / artifact_ref["path"]
            text = store.read_text(path)
            evidence[name] = {
                "artifact": path.as_posix(),
                "text": text[:12000],
                "truncated": len(text) > 12000,
            }
        revisions.append({
            "artifact": ref.path,
            "status": record.get("status", "unknown"),
            "failure_ref": record.get("failure_ref"),
            "asset_integrity": record.get("asset_integrity", {}),
            "method_validation": dict(method_validation),
            "evidence": evidence,
        })

    final = revisions[-1]
    final_evidence = dict(final["evidence"])
    patches = [
        {
            "revision": index + 1,
            "implementation_artifact": revision["artifact"],
            **revision["evidence"]["patch"],
        }
        for index, revision in enumerate(revisions)
        if "patch" in revision["evidence"]
    ]
    if len(patches) > 1:
        final_evidence["patches"] = patches
    context.results = {**context.results, "implementation": {
        "artifact": implementation_ref.path, "status": final["status"],
        "asset_integrity": final["asset_integrity"], "evidence": final_evidence,
        "method_validation": final["method_validation"],
        "lineage": [
            {
                "artifact": revision["artifact"],
                "status": revision["status"],
                "failure_ref": revision["failure_ref"],
                "method_validation_status": (
                    revision["method_validation"].get("status")
                    if isinstance(revision["method_validation"], Mapping) else None
                ),
                "evidence_kinds": sorted(revision["evidence"]),
            }
            for revision in revisions
        ],
        "interpretation": "The frozen patch, not the proposed plan, defines what was modified. Calling an existing utility with new arguments is reuse, not evidence that the utility implementation was changed. Validation and review are checks, not proof of scientific improvement. Missing or truncated evidence cannot support unseen implementation details.",
    }}
    context.source_handles.append(SourceHandle(
        handle="artifact:implementation", kind="implementation_result",
        artifact=implementation_ref.path,
        summary="Frozen implementation patch and checks are available through get_code_task_result.",
    ))


def build_research_report_inputs(
    *,
    topic: str,
    brief: SynthesisResult,
    search: SearchResult,
    documents: DocumentBundle,
    execution: Mapping[str, Any],
    analysis: AnalysisResult,
    brief_ref: ArtifactRef,
    execution_ref: ArtifactRef,
    analysis_ref: ArtifactRef,
    design: ResearchDesignResult | None = None,
    design_ref: ArtifactRef | None = None,
    metric_artifacts: Mapping[str, str] | None = None,
) -> tuple[ReportContext, ReportMemory]:
    """Build experiment-report inputs from canonical persisted artifacts."""

    execution = dict(execution)
    fallback_contract = (
        design.contract
        if design is not None and design.contract is not None
        else brief.experiment_contract
    )
    execution_contract = execution.get("experiment_contract")
    if isinstance(execution_contract, ResearchExperimentContract):
        execution_contract = execution_contract.to_row()
    if isinstance(execution_contract, Mapping) and execution_contract:
        # Prefer the protocol attached to the measured result while retaining
        # descriptive research context omitted by older execution records.
        # Dataset, metric, seed, and comparison conditions must come from the
        # measured execution contract, never from a planned fallback.
        contract_row = dict(execution_contract)
        fallback_row = fallback_contract.to_row() if fallback_contract is not None else {}
        for field in _RESEARCH_CONTEXT_FIELDS:
            if not contract_row.get(field) and fallback_row.get(field):
                contract_row[field] = fallback_row[field]
        contract = ResearchExperimentContract.from_row(contract_row)
    else:
        contract = fallback_contract
    if contract is None:
        raise ReportProjectionError(
            "Research session has no experiment contract; report input is incomplete."
        )
    topic = (topic or "").strip()
    if not topic:
        raise ReportProjectionError(
            "Research session has no topic in its persisted research plan."
        )

    source_handles = _research_source_handles(
        search=search,
        brief_ref=brief_ref,
        execution_ref=execution_ref,
        analysis_ref=analysis_ref,
        design_ref=design_ref,
        execution=execution,
        analysis=analysis,
    )
    execution["output_evidence"] = _qualified_outputs(execution, execution_ref)
    source_handles.extend(_output_handles(execution["output_evidence"]))
    metric_sources = metric_sources_from_execution(
        execution,
        artifact=execution_ref.path,
        metric_artifacts=metric_artifacts,
    )
    selected_papers = search.selected_papers
    citation_key_map = _citation_key_map(selected_papers)
    evidence_summary = (
        f"Search returned {len(search.papers)} raw paper record(s) and retained "
        f"{len(selected_papers)} selected paper(s); "
        f"document ingest retained {len(documents.records)} document(s) and "
        f"{len(documents.chunks)} text chunk(s). "
        f"Execution status={execution.get('status', 'unknown')}; "
        f"analysis status={analysis.status}."
    )
    context = ReportContext(
        topic=topic,
        report_mode="experiment",
        synthesis_markdown=_synthesis_markdown(brief),
        source_comparisons=[dict(row) for row in getattr(brief, "comparisons", ())],
        hypothesis_markdown=contract.hypothesis,
        evidence_summary=evidence_summary,
        execution_context=brief.execution_context,
        search_meta={
            "status": search.status,
            "paper_count": len(search.papers),
            "selected_paper_count": len(selected_papers),
            "response_count": len(search.responses),
            "coverage": dict(search.coverage_report),
            "diagnostics": list(search.diagnostics),
        },
        experiment_plan=contract.to_row(),
        results=execution,
        papers=[paper.to_row() for paper in selected_papers],
        source_handles=source_handles,
        metric_sources=metric_sources,
        citation_key_map=citation_key_map,
    )
    memory = ReportMemory(
        objective=contract.hypothesis,
        template="experiment",
        report_mode="experiment",
        claims_evidence_matrix=[
            claim_evidence_record_from_analysis(claim)
            for claim in analysis.claims
        ],
        source_handles=source_handles,
        metric_sources=metric_sources,
        limitations=[*analysis.audit.limitations, *contract.risks],
        key_decisions=[
            f"execution_status={execution.get('status', 'unknown')}",
            f"analysis_status={analysis.status}",
            *analysis.status_reasons,
        ],
    )
    return context, memory


def build_material_report_inputs(
    *, topic: str, documents: DocumentBundle, documents_ref: ArtifactRef,
    assets: Sequence[Any], selected_evidence: bool = False, include_memory: bool = True,
) -> tuple[ReportContext, ReportMemory | None]:
    """Use extracted user material directly, without invented research state.

    Source roles come from the intake, not filenames or document contents.
    External results remain assertions from their author; ingest is not an
    independent experiment or a semantic verification of those assertions.
    Whole-article consumers use the same facts without constructing unused
    sectional Writer memory. The default retains the historical input contract.
    """
    from simple_ar.research.sources.capability import provided_materials_result
    from simple_ar.research.store.retrieval import material_overview_views, source_chunk_views
    from simple_ar.research.documents.extractors import document_extraction_limitations
    from dataclasses import replace
    from simple_ar.core.artifacts import read_json

    json_materials = {record.document_id: read_json(Path(record.local_path))
                      for record in documents.records
                      if record.local_path and Path(record.local_path).suffix.lower() == ".json"
                      and record.metadata.get("code_analysis") is None}

    roles = {str(Path(asset.locator).resolve()): asset.role for asset in assets}
    paper_records = [record for record in documents.records
                     if (record.source_id and roles.get(str(Path(record.source_id).resolve())) in {"paper", "reference"})
                     or (record.metadata.get("retained_source_role") == "paper"
                         and roles.get(record.metadata.get("retained_bundle")) == "material")
                     or (record.is_original_source and record.metadata.get("paper_id"))]
    # Old bundles may still have stored a front-matter prefix as "abstract".
    # Prefer the retained abstract section without changing the old artifact
    # or inferring authors, publication date or any external identity.
    parsed_ids = {record.document_id for record in paper_records if record.extraction_status == "parsed"}
    abstracts = {section.document_id: section for section in documents.sections if section.section == "abstract"
                 and not (section.document_id in parsed_ids and section.line_start == 1)}
    paper_records = [replace(record, abstract=" ".join(abstracts[record.document_id].text.split())[:1200])
                     if record.document_id in abstracts else
                     replace(record, abstract="") if record.document_id in parsed_ids else record
                     for record in paper_records]
    search = provided_materials_result(paper_records)
    by_paper_id = {handle.paper_id: handle for handle in _paper_source_handles(search)}
    paper_handles = {record.document_id: by_paper_id[paper.id]
                     for record, paper in zip(paper_records, search.selected_papers, strict=True)}
    chunks_by_document: dict[str, list[Any]] = {}
    for chunk in documents.chunks:
        chunks_by_document.setdefault(chunk.document_id, []).append(chunk)
    if selected_evidence and sum(len(chunk.text) for chunk in documents.chunks) > 96000:
        raise ReportProjectionError("Explicit evidence exceeds the composition window; narrow the selection.")
    handles = []
    for record in documents.records:
        chunks = chunks_by_document.get(record.document_id, [])
        payload = json_materials.get(record.document_id)
        # The citation owner below consumes identities. A saved citation map
        # may also embed derived reading cards, not the cited original text.
        if isinstance(payload, dict) and payload.get("schema_version") == "citation_map.v1":
            continue
        passages = ([view for chunk in chunks if chunk.text
                     for view in source_chunk_views([chunk], max_chars=len(chunk.text))]
                    if selected_evidence else material_overview_views(chunks, topic))
        # A bounded overview is not the source's table of contents. Expose
        # retained section locations so readers can choose a specific lookup
        # instead of inferring that an unselected experiment does not exist.
        directory = {}
        for chunk in chunks:
            section_id = chunk.metadata.get("section_id") or chunk.chunk_id
            row = directory.setdefault(section_id, {
                "section_id": section_id, "heading": str(chunk.metadata.get("heading") or "")[:240],
                "chunk_id": chunk.chunk_id, "retained_chunks": 0,
            })
            row["retained_chunks"] += 1
        handle = paper_handles.get(record.document_id) or SourceHandle(
            handle=f"material:{record.document_id}",
            kind="prior_draft" if record.metadata.get("kind") == "prior_draft" else "material", title=record.title,
            paper_id=record.document_id,
        )
        metadata = {**handle.metadata, "document_id": record.document_id,
                    "extraction_status": record.extraction_status,
                    "extraction_coverage": dict(record.metadata.get("fulltext_extraction", {}).get("coverage", {})),
                    "evidence_role": record.metadata.get("evidence_role") or ("linked_material_not_paper_or_measured_result" if record.metadata.get("kind") == "supporting_material" else "bibliographic_source_not_independently_verified" if record.document_id in paper_handles else "user_supplied_unverified"),
                    "document_chunk_count": len(chunks),
                    "source_directory": {"sections": list(directory.values())[:80],
                                         "total_sections": len(directory),
                                         "truncated": len(directory) > 80},
                    "evidence_passages": passages,
                    "evidence_passages_truncated": (len({row["chunk_id"] for row in passages}) < len(chunks)
                                                     or any(row["truncated"] for row in passages)),
                    "evidence_selection": "explicit_retained_chunks" if selected_evidence else "structured_overview_and_task_lexical_matches"}
        if isinstance(payload, dict) and payload.get("schema_version") == "report_experiment_evidence.v1":
            # Reused records already distinguish declarations and observations.
            # Preserve that contract instead of requiring prose retrieval to
            # rediscover it; these are not executions of the current task.
            from simple_ar.report.execution_evidence import report_execution_evidence
            saved = ReportContext.model_validate({**payload.get("records", {}),
                                                 "topic": topic, "report_mode": "experiment"})
            metadata["evidence_role"] = "supplied_recorded_execution_not_current_task_or_independent_recheck"
            metadata["recorded_execution_evidence"] = report_execution_evidence(saved)
        handles.append(handle.model_copy(update={"artifact": documents_ref.path,
            "chunk_id": passages[0]["chunk_id"] if passages else "",
            "summary": paper_handles[record.document_id].summary if record.document_id in paper_handles else record.abstract,
            "metadata": metadata}))
    if not any(handle.metadata["document_chunk_count"] for handle in handles):
        raise ReportProjectionError("Supplied writing material contains no readable text; inspect extraction diagnostics.")
    limitations = []
    if include_memory:
        limitations = ["No experiment, code verification, research synthesis or online search was performed in this writing task. "
                        "Results and methods in user material are externally supplied assertions, not independently verified session measurements.",
                        "Excerpts are bounded; use retained source chunks to check disputed claims. Extraction and review do not certify a paper's scientific correctness."]
        unavailable = [record.title for record in documents.records if not chunks_by_document.get(record.document_id)]
        if unavailable:
            limitations.append("Unavailable material text: " + ", ".join(unavailable))
        if paper_records:
            limitations.append("Local source bibliographic metadata may be incomplete; do not invent authors, dates or publication venues.")
        limitations.extend(document_extraction_limitations(documents.records))
    context = ReportContext(topic=topic, report_mode="supplied_materials", source_handles=handles,
        papers=[paper.to_row() for paper in search.selected_papers],
        citation_key_map=_citation_key_map(search.selected_papers),
        evidence_summary=f"Retained source passages from {sum(bool(chunks_by_document.get(record.document_id)) for record in documents.records)} of {len(documents.records)} supplied materials. Method and result assertions remain source-reported.",
        results={"evidence_origin": "user_supplied_unverified", "session_execution": "not_requested"})
    # Assign linked-material citation identities before joining a saved map;
    # otherwise the same snapshot gains a second metadata-only paper handle.
    _attach_linked_document_references(context, documents)
    handles = list(context.source_handles)
    # Reused drafts need their recorded reference identities, not a BibTeX file
    # treated as prose. Keep this separate from original-source evidence.
    from simple_ar.report.citations import references_from_citation_map
    known_ids = {row["id"] for row in context.papers}
    retained_keys = {}
    for record in documents.records:
        code = record.metadata.get("code_analysis")
        if code is not None:
            results = code.get("results", {})
            payload = results.get("citation_map", {}) if isinstance(results, dict) else {}
        elif record.document_id in json_materials:
            payload = json_materials[record.document_id]
        else:
            continue
        if not isinstance(payload, dict):
            continue
        references, keys = references_from_citation_map(payload)
        if code is not None:
            # Producer-local aliases are not a frozen manuscript's citations.
            # The active report assigns unique keys across analysis packages.
            keys = {}
        for key, paper_id in keys.items():
            if key in retained_keys and retained_keys[key] != paper_id:
                raise ReportProjectionError("Reused citation maps have conflicting short keys; use stable paper identities before combining drafts.")
            retained_keys[key] = paper_id
        for paper in references:
            if paper.id in known_ids:
                continue
            known_ids.add(paper.id)
            context.papers.append(paper.to_row())
            if any(handle.paper_id == paper.id for handle in handles):
                # Bibliographic metadata may describe a linked material that
                # already has its own text/coverage. Do not promote that input
                # into a second, metadata-only paper evidence handle.
                continue
            handles.append(SourceHandle(handle=f"reference:{paper.id}", kind="paper",
                paper_id=paper.id, title=paper.title, artifact=documents_ref.path,
                metadata={"evidence_role": "reused_reference_metadata_not_primary_text",
                          "document_chunk_count": 0, "evidence_passages": []}))
    # Do not renumber old aliases while their original map is visible to the
    # writer. Assign only unused keys to newly supplied references.
    for row in context.papers:
        if row["id"] not in retained_keys.values():
            index = 1
            while f"P{index}" in retained_keys:
                index += 1
            retained_keys[f"P{index}"] = row["id"]
    context.citation_key_map = retained_keys
    by_id = {paper_id: key for key, paper_id in retained_keys.items()}
    handles = [handle.model_copy(update={"citation_key": by_id.get(handle.paper_id, handle.citation_key)})
               for handle in handles]
    analyses = []
    for record in documents.records:
        table = record.metadata.get("table_analysis")
        if table is not None:
            analyses.append({**table, "artifact": (Path(documents_ref.path).parent / table["artifact"]).as_posix(),
                             "document_id": record.document_id, "evidence_role": "recomputed_from_user_supplied_data"})
        code = record.metadata.get("code_analysis")
        if code is not None:
            analyses.append({**code, "artifact": (Path(documents_ref.path).parent / code["artifact"]).as_posix(),
                             "document_id": record.document_id})
    if analyses:
        context.results["supplied_analyses"] = analyses
        if include_memory:
            limitations.append("Table packages are arithmetically recomputed; code-analysis packages preserve validated script outputs without independent recomputation. Neither certifies data collection, semantics or scientific validity.")
        context.evidence_summary += f" {len(analyses)} supplied analysis package(s) retain results, source inputs and figure encodings. Check each package's evidence_role before describing verification."
    context = context.model_copy(update={"source_handles": handles})
    memory = (ReportMemory(objective=topic, report_mode=context.report_mode,
                          source_handles=context.source_handles, limitations=limitations)
              if include_memory else None)
    return apply_report_bibliography(context, memory, documents=documents, notes=[])


def build_literature_report_inputs(
    *,
    topic: str,
    brief: SynthesisResult | None,
    search: SearchResult,
    documents: DocumentBundle,
    brief_ref: ArtifactRef | None,
) -> tuple[ReportContext, ReportMemory]:
    """Project literature evidence without inventing measurements."""

    handles = _paper_source_handles(search)
    if brief_ref is not None:
        handles.insert(0, SourceHandle(handle="artifact:synthesis", kind="synthesis", artifact=brief_ref.path))
    citation_key_map = _citation_key_map(search.selected_papers)
    limitation = (
        "No experiment was requested or executed; cited results describe "
        "prior work, not measurements from this session."
    )
    context = ReportContext(
        topic=topic,
        report_mode="research_only",
        synthesis_markdown=_synthesis_markdown(brief) if brief is not None else "",
        source_comparisons=[dict(row) for row in getattr(brief, "comparisons", ())],
        evidence_summary=(
            f"Search retained {len(search.selected_papers)} selected papers; "
            f"ingest retained {len(documents.records)} documents and "
            f"{len(documents.chunks)} chunks. {limitation}"
        ),
        search_meta={
            "status": search.status,
            "coverage": dict(search.coverage_report),
            "diagnostics": list(search.diagnostics),
        },
        papers=[paper.to_row() for paper in search.selected_papers],
        source_handles=handles,
        citation_key_map=citation_key_map,
    )
    memory = ReportMemory(
        objective=topic,
        template="survey",
        report_mode="research_only",
        source_handles=handles,
        limitations=[limitation],
    )
    return context, memory


def attach_paired_report_measurements(
    context: ReportContext,
    memory: ReportMemory,
    measurements: list[tuple[int, str, ArtifactRef, Mapping[str, Any]]],
    *,
    comparisons: list[Mapping[str, Any]],
    comparison_ref: ArtifactRef,
    summaries: Sequence[Mapping[str, Any]] = (),
) -> tuple[ReportContext, ReportMemory]:
    """Project measured conditions into report provenance rows."""

    metrics: list[MetricSource] = []
    handles = list(context.source_handles)
    for seed, role, ref, result in measurements:
        label = f"{role}:seed={seed}"
        handles.append(
            SourceHandle(
                handle=f"artifact:{label}",
                kind="experiment_result",
                artifact=ref.path,
                summary=f"{label}; execution status={result['status']}",
            )
        )
        if result["status"] != "passed":
            memory.limitations.append(
                f"{label} did not pass; its residual metrics are not valid measurements for the report."
            )
            continue
        metrics.extend(
            row.model_copy(update={"metric_id": f"metric:{label}:{row.name}", "label": label})
            for row in metric_sources_from_execution(result, artifact=ref.path)
        )

    passed = {
        (seed, role)
        for seed, role, _, result in measurements
        if result["status"] == "passed"
    }
    for comparison in comparisons:
        seed = comparison["seed"]
        if (seed, "baseline") not in passed or (seed, "candidate") not in passed:
            continue
        label = f"comparison_delta:seed={seed}"
        metrics.extend(
            row.model_copy(
                update={
                    "metric_id": f"metric:{label}:{row.name}",
                    "label": label,
                    "condition_id": f"paired:seed={seed}",
                }
            )
            for row in metric_sources_from_execution(
                {"comparisons": [comparison]},
                artifact=comparison_ref.path,
            )
        )
    for summary in summaries:
        label = f"paired_summary:{summary['group_id']}"
        for field in ("n", "baseline_mean", "candidate_mean", "delta_mean", "delta_sample_std"):
            if summary[field] is not None:
                name = f"{summary['metric']}.{field}"
                metrics.append(
                    MetricSource(
                        metric_id=f"metric:{label}:{name}",
                        name=name,
                        value=summary[field],
                        artifact=comparison_ref.path,
                        label=label,
                        unit="count" if field == "n" else str(summary.get("unit", "")),
                        condition_id=f"seeds={','.join(map(str, summary['seeds']))}",
                        source_kind="derived_summary",
                    )
                )

    context.metric_sources = metrics
    memory.metric_sources = metrics
    context.source_handles = handles
    memory.source_handles = handles
    context.experiment_plan = {
        **context.experiment_plan,
        "paired_protocols": [
            {
                "seed": seed,
                "condition": role,
                "artifact": ref.path,
                "protocol": result.get("experiment_contract"),
            }
            for seed, role, ref, result in measurements
        ]
    }
    memory.key_decisions.append(
        "Report every seed separately. Do not turn the last run into a mean, "
        "or claim significance from these descriptive comparisons."
    )
    return context, memory


def attach_experiment_history(
    context: ReportContext,
    memory: ReportMemory,
    measurements: Sequence[tuple[str, ArtifactRef, Mapping[str, Any]]],
    *,
    current_ref: ArtifactRef,
    include_prior_metrics: bool = True,
    output_store: ArtifactStore | None = None,
) -> tuple[ReportContext, ReportMemory]:
    """Keep every accepted-plan observation visible, without pooling revisions.

    The newest result alone cannot answer whether an earlier candidate ran or
    measured a metric. Rows are read from immutable experiment artifacts; failed
    runs contribute status and provenance, never invented metric values.
    """

    history: list[dict[str, Any]] = []
    handles = list(context.source_handles)
    metrics = list(context.metric_sources)
    seen: set[str] = set()
    for action, ref, result in measurements:
        if ref.path in seen:
            continue
        seen.add(ref.path)
        status = str(result.get("execution_status") or result.get("status") or "unknown").lower()
        measured = result.get("metrics") if status == "passed" else None
        row = {
            "action": action,
            "artifact": ref.path,
            "status": status,
            "metrics": dict(measured) if isinstance(measured, Mapping) else {},
            "implementation_ref": result.get("implementation_ref"),
            "execution_record": execution_record(result),
            "output_evidence": _qualified_outputs(result, ref, output_store),
            "measurement": {
                key: value for key in ("condition_id", "protocol_fingerprint", "seed", "source_kind")
                if isinstance(result.get("measurement"), Mapping)
                and (value := result["measurement"].get(key)) is not None
            },
        }
        history.append(row)
        handles.extend(_output_handles(row["output_evidence"]))
        handles.append(SourceHandle(
            handle=f"artifact:measurement:{action}", kind="experiment_result",
            artifact=ref.path, summary=f"{action}: {status}",
        ))
        if (include_prior_metrics and status == "passed"
                and ref.path != current_ref.path and action != "baseline"):
            metrics.extend(metric.model_copy(update={
                "metric_id": f"metric:history:{action}:{metric.name}",
                "label": action,
            }) for metric in metric_sources_from_execution(result, artifact=ref.path)
                if metric.label == "candidate")
    context.results = {**context.results, "measurement_history": history}
    current = next((row for row in history if row["artifact"] == current_ref.path), None)
    if current is not None:
        context.results["output_evidence"] = current["output_evidence"]
    context.source_handles = handles
    memory.source_handles = list(handles)
    context.metric_sources = metrics
    memory.metric_sources = list(metrics)
    if len(history) > 1:
        memory.key_decisions.append(
            "Measurement history lists each baseline, candidate, and technical remeasurement separately. "
            "Do not infer that no earlier candidate ran from the latest failed result."
        )
    return context, memory


def _citation_source_records(context: ReportContext, documents: DocumentBundle) -> dict[str, DocumentRecord]:
    records = {record.document_id: record for record in documents.records}
    for record in documents.records:
        if record.metadata.get("paper_id"):
            records.setdefault(str(record.metadata["paper_id"]), record)
    # Citation keys may use a derived ID while source access keeps the original
    # document ID. Resolve through the existing handle, never title similarity.
    for handle in context.source_handles:
        document_id = handle.metadata.get("document_id")
        if handle.paper_id and document_id in records:
            records.setdefault(handle.paper_id, records[document_id])
    return records


def apply_report_bibliography(
    context: ReportContext, memory: ReportMemory | None = None, *, documents: DocumentBundle,
    notes: Sequence[Mapping[str, Any]],
) -> tuple[ReportContext, ReportMemory | None]:
    """One source-matched citation projection for reading and material writing.

    Proposals are derived outputs, not changes to frozen input or publication
    identity. The existing bibliography owner decides which fields to accept.
    """
    records = _citation_source_records(context, documents)
    fronts = {section.document_id: section for section in documents.sections if section.section == "front_matter"}
    by_id: dict[str, list[Mapping[str, Any]]] = {}
    for note in notes:
        if isinstance(note, Mapping):
            by_id.setdefault(str(note.get("paper_id")), []).append(note)
    rows, origins_by_id = [], {}
    for original in context.papers:
        record = records.get(original["id"])
        row = dict(original)
        if record is not None:
            origins = {}
            # Writers cite the projected paper ID; historical readers used the
            # storage document ID. Both resolve to this same retained source,
            # and the bibliography owner still checks the exact section/quote.
            source_notes = [note for key in dict.fromkeys((original["id"], record.document_id))
                            for note in by_id.get(key, [])]
            for note in source_notes or [None]:
                row, accepted = apply_bibliographic_note(row, record, fronts.get(record.document_id), note)
                origins.update(accepted)
            if origins:
                origins_by_id[row["id"]] = origins
        rows.append(row)
    papers = {row["id"]: row for row in rows}
    handles = []
    for handle in context.source_handles:
        if handle.paper_id not in origins_by_id:
            handles.append(handle)
            continue
        origins = origins_by_id[handle.paper_id]
        metadata = {**handle.metadata,
                    "bibliographic_sources": {**handle.metadata.get("bibliographic_sources", {}), **origins},
                    **{field: papers[handle.paper_id].get(field) for field in ("authors", "published", "doi", "url")},
                    "bibliography": bibliographic_details(Paper.from_row(papers[handle.paper_id]))}
        if "title" in origins:
            metadata["title_source"] = origins["title"]
        handles.append(handle.model_copy(update={"title": papers[handle.paper_id]["title"], "metadata": metadata}))
    context = context.model_copy(update={"papers": rows, "source_handles": handles})
    if memory is None:
        return context, None
    handles_by_id = {handle.handle: handle for handle in handles}
    memory_ids = {handle.handle for handle in memory.source_handles}
    memory_handles = [handles_by_id.get(handle.handle, handle) for handle in memory.source_handles]
    memory_handles.extend(handle for handle in handles if handle.handle not in memory_ids)
    return (context,
            memory.model_copy(update={"source_handles": memory_handles}))


def bibliography_planning_views(context: ReportContext, documents: DocumentBundle | None) -> list[dict[str, Any]]:
    """Bounded existing front matter for missing local citation fields only."""
    if documents is None:
        return []
    from simple_ar.research.evidence.bibliography import FRONT_MATTER_CHARS
    records = _citation_source_records(context, documents)
    fronts = {section.document_id: section for section in documents.sections if section.section == "front_matter"}
    candidates = [(row, records[row["id"]], fronts[records[row["id"]].document_id], fields)
                  for row in context.papers if row["id"] in records
                  and records[row["id"]].document_id in fronts
                  and (fields := local_citation_fields_to_fill(row, records[row["id"]]))]
    views = []
    remaining = FRONT_MATTER_CHARS
    for index, (paper, record, section, fields) in enumerate(candidates):
        size = remaining // (len(candidates) - index)
        text = section.text[:size]
        remaining -= len(text)
        views.append({"paper_id": record.document_id, "section_id": section.section_id,
                      "recorded_title": paper["title"], "missing_fields": fields,
                      "text": text, "truncated": len(text) < len(section.text),
                      "scope": "supplied source front matter, not publication identity verification"})
    return views


def _attach_linked_document_references(context: ReportContext, documents: DocumentBundle) -> None:
    """Keep linked-document identity separate in fresh and reused reports."""
    for record in documents.records:
        handle = f"material:{record.document_id}"
        if record.metadata.get("kind") != "supporting_material" or not record.url:
            continue
        # Reversible encoding keeps arbitrary document IDs BibTeX-safe; source
        # access still uses metadata.document_id, not this bibliography ID.
        citation_id = "material-" + record.document_id.encode("utf-8").hex()
        if not any(row["id"] == citation_id for row in context.papers):
            context.papers.append(Paper(id=citation_id, title=record.title,
                authors=list(record.authors), abstract="", url=record.url,
                published=record.published, doi=record.doi, source="supporting_material",
                bibliographic_notes=[*record.metadata.get("bibliographic_notes", []),
                    "Retrieved source snapshot; publication identity and claims are not independently verified, and no execution is established."]).to_row())
        paper = next(row for row in context.papers if row["id"] == citation_id)
        citation_key = next((key for key, value in context.citation_key_map.items() if value == citation_id), "")
        if not citation_key:
            number = max((int(key[1:]) for key in context.citation_key_map
                          if key.startswith("P") and key[1:].isdigit()), default=0) + 1
            citation_key = f"P{number}"
            context.citation_key_map[citation_key] = citation_id
        context.source_handles = [row.model_copy(update={"citation_key": citation_key, "paper_id": citation_id,
            "title": paper["title"], "metadata": {**row.metadata,
                **{field: paper.get(field) for field in ("authors", "published", "doi", "url")},
                "bibliography": bibliographic_details(Paper.from_row(paper))}})
            if row.handle == handle else row for row in context.source_handles]


def attach_report_read_evidence(
    context: ReportContext,
    memory: ReportMemory,
    *,
    documents: DocumentBundle,
    read: "ReadResult",
    read_ref: ArtifactRef,
    documents_ref: ArtifactRef | None = None,
) -> tuple[ReportContext, ReportMemory]:
    """Join reading notes by document identity, not title or position."""

    from simple_ar.research.documents.extractors import document_extraction_limitations
    if documents_ref is not None:
        registered = {handle.handle for handle in context.source_handles}
        for record in documents.records:
            handle = f"material:{record.document_id}"
            if record.metadata.get("kind") == "supporting_material" and handle not in registered:
                context.source_handles.append(SourceHandle(handle=handle, kind="material",
                    title=record.title, paper_id=record.document_id, artifact=documents_ref.path,
                    metadata={**record.metadata, "document_id": record.document_id, "url": record.url,
                              "evidence_role": "linked_material_not_paper_or_measured_result"}))
                registered.add(handle)
    _attach_linked_document_references(context, documents)
    for limitation in document_extraction_limitations(documents.records):
        if limitation not in memory.limitations:
            memory.limitations.append(limitation)

    # Local papers use document_id as their citable identity; retrieved papers
    # may additionally carry the original connector's paper_id. Neither titles
    # nor list positions are identities (and source_id can be a file path).
    by_paper = {record.document_id: record for record in documents.records}
    for record in documents.records:
        paper_id = str(record.metadata.get("paper_id") or "")
        if paper_id:
            by_paper.setdefault(paper_id, record)
    notes = {note["paper_id"]: note for note in read.paper_notes}
    # Local inputs initially use a filename as title. Reuse an existing reading
    # proposal only when it occurs verbatim (apart from whitespace) in that
    # same source's front matter. Do not overwrite provider/user metadata or
    # infer dates/bylines from PDF creation properties.
    context, memory = apply_report_bibliography(context, memory, documents=documents, notes=read.paper_notes)
    paper_rows = context.papers
    papers_by_id = {row["id"]: row for row in paper_rows}
    chunks = {chunk.chunk_id: chunk for chunk in documents.chunks}
    statuses = [record.extraction_status for record in documents.records]
    parsed_count = sum(status == "parsed" for status in statuses)
    metadata_count = sum(status == "metadata_only" for status in statuses)
    unavailable_count = sum(status in {"failed", "skipped"} for status in statuses)
    noted_count = sum(record.document_id in notes for record in documents.records)
    coverage_note = (
        f"Source access: {parsed_count} parsed full/local text, {metadata_count} metadata/abstract-only, "
        f"{unavailable_count} unavailable or skipped. Do not describe metadata/abstract-only sources "
        "as full-text reading. "
        f"Bounded model reading notes: {noted_count}/{len(documents.records)} source records. "
        "Parsed access and model notes do not certify full-document comprehension or semantic support."
    )
    if read.question_assessments:
        coverage_note += (
            " Candidate-screening observations in chronological order (not verified answers): "
            + json.dumps(list(read.question_assessments), ensure_ascii=False)
            + ". Reconcile earlier search gaps with later reading and original passages; "
            "a direct candidate does not establish its claims, and a missing candidate does not "
            "prove that no relevant method exists."
        )
    handles: list[SourceHandle] = []
    for handle in context.source_handles:
        record = by_paper.get(handle.metadata.get("document_id") or handle.paper_id)
        if record is None:
            handles.append(handle)
            continue
        note = notes.get(record.document_id)
        metadata = dict(handle.metadata)
        metadata.update(
            document_id=record.document_id,
            extraction_status=record.extraction_status,
            extraction_coverage=dict(record.metadata.get("fulltext_extraction", {}).get("coverage", {})),
            reading_artifact=read_ref.path,
            reading_state="bounded_model_note" if note is not None else "no_model_note",
        )
        if note is not None:
            metadata["reading_notes"] = {
                key: note[key]
                for key in ("problem", "method", "datasets", "metrics", "key_claims", "claim_scopes", "limitations",
                            "open_questions", "confidence", "evidence_refs", "reading_coverage")
                if key in note
            }
            metadata["reading_notes_kind"] = "model_interpretation_not_source_text"
            followup = note.get("reading_followup", {})
            if isinstance(followup, Mapping):
                metadata["reading_notes"]["reading_followup"] = reading_followup_context(followup)
            refs = list(note.get("evidence_refs", []))
            # Claim-local references are also original-source locations. They
            # need not be repeated in the note's global reference list.
            for claim in note.get("claim_scopes", []):
                if isinstance(claim, Mapping):
                    refs.extend(claim.get("evidence_refs", []))
            refs = list(dict.fromkeys(refs))
            supported = [chunks[ref] for ref in refs if ref in chunks
                         and chunks[ref].document_id == record.document_id]
            # Interpretations alone are not a passage-level check. Carry a
            # bounded set of the exact persisted passages alongside the notes.
            # Keep exact query-centered windows, but do not let a full gap-read
            # pool erase every earlier method/condition reference at delivery.
            queried = []
            for row in followup.get("passages", []) if isinstance(followup, Mapping) else []:
                if not isinstance(row, Mapping):
                    continue
                chunk = chunks.get(row.get("chunk_id"))
                start, end = row.get("character_start"), row.get("character_end")
                if (chunk is not None and chunk.document_id == record.document_id
                        and type(start) is int and type(end) is int and 0 <= start < end <= len(chunk.text)
                        and row.get("text") == chunk.text[start:end]):
                    if not any((prior["chunk_id"], prior["character_start"], prior["character_end"])
                               == (row["chunk_id"], start, end) for prior in queried):
                        queried.append(dict(row))
            queried_ids = {row["chunk_id"] for row in queried}
            overview = [chunk for chunk in supported if chunk.chunk_id not in queried_ids]
            # These are already referenced original passages, not an overview
            # requiring another representative sample. Bound their model view
            # once in narrative; do not silently detach notes from their sources.
            passages = [*queried, *[
                {"chunk_id": chunk.chunk_id, "text": chunk.text,
                 "truncated": False}
                for chunk in overview
            ]]
            metadata["evidence_passages"] = passages
            metadata["evidence_passages_truncated"] = len({row["chunk_id"] for row in passages}) < len(
                [chunk for chunk in chunks.values() if chunk.document_id == record.document_id])
        handles.append(
            handle.model_copy(
                update={"summary": record.abstract or handle.summary, "metadata": metadata,
                        "title": papers_by_id.get(handle.paper_id, {}).get("title", handle.title)}
            )
        )
    return (
        context.model_copy(update={"source_handles": handles, "papers": paper_rows,
                                   "evidence_summary": f"{context.evidence_summary} {coverage_note}".strip()}),
        memory.model_copy(update={"source_handles": handles,
                                  "limitations": [*memory.limitations, coverage_note]}),
    )


def metric_sources_from_execution(
    execution: Mapping[str, Any],
    *,
    artifact: str,
    metric_artifacts: Mapping[str, str] | None = None,
) -> list[MetricSource]:
    """Convert values and their registered owners together, without rebinding later.

    Legacy embedded records retain the enclosing artifact. Separate records
    supply their actual owners by label; this does not change values or status.
    """

    owners = metric_artifacts or {}
    rows: list[MetricSource] = []
    for label, values in _metric_groups(execution):
        record = execution["baseline"] if label == "baseline" else execution
        schema = record.get("result_schema") or {}
        directions = schema.get("metric_directions") or {}
        measurement = record.get("measurement") or {}
        protocol = record.get("experiment_contract") or {}
        units = {
            spec["name"]: spec.get("unit", "")
            for spec in protocol.get("metric_specs", [])
        }
        for name, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                continue
            rows.append(
                MetricSource(
                    metric_id=f"metric:{label}:{name}",
                    name=str(name),
                    value=value,
                    artifact=owners.get(label, artifact),
                    label=label,
                    direction=str(
                        directions.get(name)
                        or (
                            schema.get("direction")
                            if name == schema.get("primary_metric")
                            else ""
                        )
                        or ""
                    ),
                    measurement_id=measurement.get("measurement_id"),
                    protocol_id=measurement.get("protocol_id"),
                    protocol_revision=measurement.get("protocol_revision"),
                    protocol_fingerprint=measurement.get("protocol_fingerprint"),
                    condition_id=measurement.get("condition_id"),
                    unit=str(units.get(name, "")),
                    source_kind=str(
                        measurement.get("source_kind") or "legacy_unverified"
                    ),
                )
            )

    comparisons = execution.get("comparisons")
    if isinstance(comparisons, list):
        for index, comparison in enumerate(comparisons):
            if not isinstance(comparison, Mapping):
                continue
            metric_rows = comparison.get("metrics")
            if not isinstance(metric_rows, list):
                continue
            for metric_row in metric_rows:
                if not isinstance(metric_row, Mapping):
                    continue
                name = str(metric_row.get("name") or "").strip()
                value = metric_row.get("delta")
                if not name or isinstance(value, bool) or not isinstance(value, (int, float, str)):
                    continue
                rows.append(
                    MetricSource(
                        metric_id=f"metric:comparison:{index}:{name}",
                        name=name,
                        value=value,
                        artifact=owners.get("comparison_delta", artifact),
                        label="comparison_delta",
                        direction=str(metric_row.get("direction") or ""),
                        source_kind="derived_comparison",
                    )
                )
    return rows


def _metric_groups(execution: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    groups: list[tuple[str, Mapping[str, Any]]] = []
    metrics = execution.get("metrics")
    if isinstance(metrics, Mapping):
        groups.append(("candidate", metrics))
    baseline = execution.get("baseline")
    baseline_metrics = baseline.get("metrics") if isinstance(baseline, Mapping) else None
    if isinstance(baseline_metrics, Mapping):
        groups.append(("baseline", baseline_metrics))
    return groups


def experiment_record_snapshot(context: ReportContext) -> dict[str, Any]:
    """Exact recorded values and roles, not a new interpretation or ledger."""
    return {"metric_sources": [row.model_dump(mode="json") for row in context.metric_sources],
        "experiment_plan": context.experiment_plan, "execution_context": context.execution_context,
        "results": context.results}


def experiment_record_markdown(context: ReportContext, sources: list[dict[str, Any]]) -> str:
    """One deterministic attachment text owner for assembly and audit."""
    markdown = "# Recorded Experiment Evidence\n\n" + _verified_experiment_evidence(context,
        detailed_reference="[Complete recorded context and metric provenance](experiment_evidence.json)")
    if sources:
        markdown += "\n\n## Registered Source Copies\n\n" + "\n".join(
            f"- [Registered source {index}]({row['copied_path']}) — original artifact: `{row['source']['path']}`"
            for index, row in enumerate(sources, 1))
    else:
        markdown += "\n\nOnly the projected records were supplied; original source files were not copied."
    return markdown + "\n"


def _append_verified_experiment_evidence(
    sections: tuple[ReportSectionDraft | Mapping[str, Any], ...],
    context: ReportContext,
    config: ReportRuntimeConfig | None = None,
) -> tuple[ReportSectionDraft | Mapping[str, Any], ...]:
    """Project reader-facing records; legacy direct callers keep full tables."""

    if context.report_mode != "experiment":
        return sections
    evidence = (_verified_experiment_evidence(context,
        detailed_reference="[Complete recorded context and metric provenance](experiment_evidence.json)")
        if config is not None else _verified_experiment_evidence(context))
    if not evidence:
        return sections
    linked = config is not None and config.data_tables == "linked"
    return (
        *sections,
        ReportSectionDraft(
            section_id="verified_experiment_metrics",
            heading="Experiment Records" if linked else "Verified Experiment Metrics",
            draft_markdown=("[Recorded measurements and provenance](experiment_evidence.md). "
                "These records do not independently verify the implementation or scientific conclusions."
                if linked else evidence),
            metric_ids=[metric.metric_id for metric in context.metric_sources],
        ),
    )


def _verified_experiment_evidence(context: ReportContext, *, detailed_reference: str | None = None) -> str:
    """Render a compact measured summary without asking the Writer.

    Per-task measurements remain in the immutable experiment artifacts.  They
    are useful for audit and follow-up analysis, but expanding every value into
    the paper makes the paper unreadable.  Paired sessions therefore expose
    aggregate metrics and a small seed-level primary-metric table here.
    """

    if not context.metric_sources:
        return ""
    primary_metric, declared_metrics = _declared_report_metrics(context)
    intro = (
        "Declared primary and required values below are copied from persisted experiment evidence."
        if declared_metrics is not None
        else "The following values are copied from the persisted experiment evidence."
    )
    lines = [intro + " Full measurements remain available from their source artifacts."]
    comparisons = context.results.get("comparisons") if isinstance(context.results, Mapping) else None
    summaries = context.results.get("paired_summary") if isinstance(context.results, Mapping) else None
    if isinstance(summaries, list) and summaries:
        table = _paired_summary_markdown(summaries, declared_metrics)
        if table:
            lines.extend(["", "### Aggregate Paired Metrics", "", table])
            lines.append(
                "Mean deltas are derived as candidate minus baseline; the reported "
                "sample deviation describes observed paired deltas and is not a significance test."
            )
        seed_table = _paired_primary_metric_markdown(comparisons, primary_metric)
        if seed_table:
            lines.extend(["", "### Seed-Level Primary Metric", "", seed_table])
        detail_reference = detailed_reference if detailed_reference is not None else _detailed_measurement_reference(context)
        if detail_reference:
            lines.extend([
                "",
                "Detailed per-task and per-condition measurements are preserved in "
                f"{detail_reference}; this section shows the declared compact results.",
            ])
        return "\n".join(lines)
    history = context.results.get("measurement_history") if isinstance(context.results, Mapping) else None
    history_table = _paired_history_markdown(history, declared_metrics)
    if history_table:
        lines.extend(["", "### Recorded Paired Conditions", "", history_table])
        lines.append(
            "Rows pair one recorded baseline and one candidate by condition and protocol fingerprint. "
            "Deltas are candidate minus baseline; this table does not certify the underlying assets "
            "or establish statistical significance."
        )
        detail_reference = detailed_reference if detailed_reference is not None else _detailed_measurement_reference(context)
        if detail_reference:
            lines.extend(["", f"Full measurement records are preserved in {detail_reference}."])
        return "\n".join(lines)
    if isinstance(comparisons, list):
        rendered = False
        for comparison in comparisons:
            if not isinstance(comparison, Mapping):
                continue
            table = _comparison_markdown(comparison, declared_metrics)
            if table:
                lines.extend(["", "### Baseline and Candidate Comparison", "", table])
                lines.append(
                    "Derived delta is candidate minus baseline; it is calculated from "
                    "the two measured values, not an independent measurement."
                )
                rendered = True
                break
        if not rendered:
            lines.append(
                "No structured paired-comparison rows are registered; individual recorded values follow."
                if not comparisons
                else (
                    "No valid paired comparison row matched the declared metrics; "
                    "available individual measurements are listed below."
                )
            )

    if isinstance(comparisons, list) and rendered:
        visible_metrics = []
    elif declared_metrics is None:
        visible_metrics = context.metric_sources
    else:
        visible_metrics = [metric for metric in context.metric_sources if metric.name in declared_metrics]
    ledger = _metric_ledger(visible_metrics)
    if ledger:
        lines.extend(["", "### Metric Provenance", "", ledger])
        if any(metric.source_kind.startswith("derived_") for metric in visible_metrics):
            lines.append("Rows with a derived origin are calculated summaries or differences, not direct measurements.")
    detail_reference = detailed_reference if detailed_reference is not None else _detailed_measurement_reference(context)
    if detail_reference and (
        isinstance(comparisons, list) or len(visible_metrics) < len(context.metric_sources)
    ):
        detail_note = (
            "Detailed per-condition and per-task measurements are preserved in"
            if isinstance(comparisons, list)
            else "Other detailed measurements remain in"
        )
        lines.extend(["", f"{detail_note} {detail_reference}."])
    return "\n".join(lines)


def _paired_history_markdown(history: object, declared_metrics: set[str] | None) -> str:
    """Render unambiguous measured pairs, never infer pairs from action names."""

    if not isinstance(history, list):
        return ""
    grouped: dict[tuple[str, str], dict[str, list[Mapping[str, Any]]]] = {}
    for row in history:
        if not isinstance(row, Mapping) or row.get("status") != "passed":
            continue
        measurement = row.get("measurement")
        metrics = row.get("metrics")
        if not isinstance(measurement, Mapping) or not isinstance(metrics, Mapping):
            continue
        condition_id = measurement.get("condition_id")
        fingerprint = measurement.get("protocol_fingerprint")
        if not isinstance(condition_id, str) or not isinstance(fingerprint, str):
            continue
        role, separator, condition = condition_id.partition(":")
        if separator != ":" or not condition or role not in {"baseline", "candidate"}:
            continue
        if measurement.get("source_kind") != "measured":
            continue
        grouped.setdefault((fingerprint, condition), {"baseline": [], "candidate": []})[role].append(row)

    lines: list[str] = []
    complete_pairs = 0
    for (_, condition), roles in grouped.items():
        if len(roles["baseline"]) != 1 or len(roles["candidate"]) != 1:
            continue  # Duplicates or incomplete pairs need explicit analysis.
        complete_pairs += 1
        baseline = roles["baseline"][0]["metrics"]
        candidate = roles["candidate"][0]["metrics"]
        for name in baseline:
            if name not in candidate or (declared_metrics is not None and name not in declared_metrics):
                continue
            b_value, c_value = baseline[name], candidate[name]
            if (isinstance(b_value, bool) or isinstance(c_value, bool)
                    or not isinstance(b_value, (int, float))
                    or not isinstance(c_value, (int, float))):
                continue
            lines.append(
                f"| {condition.replace('|', '/')} | `{str(name).replace('|', '/')}` | "
                f"{_format_report_metric(b_value)} | {_format_report_metric(c_value)} | "
                f"{_format_report_metric(c_value - b_value)} |"
            )
    if not lines or complete_pairs < 2:
        return ""
    return "\n".join([
        "| Condition | Metric | Baseline | Candidate | Delta |",
        "| --- | --- | ---: | ---: | ---: |",
        *lines,
    ])


def _declared_report_metrics(context: ReportContext) -> tuple[str, set[str] | None]:
    """Return the declared primary metric and report-level required metrics."""

    names: list[str] = []
    primary = ""

    def add(value: object) -> None:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple)):
            candidates = value
        else:
            return
        for item in candidates:
            name = str(item).strip()
            if name and name not in names:
                names.append(name)

    sources: list[Mapping[str, Any]] = []
    if isinstance(context.results, Mapping):
        sources.append(context.results)
        for key in ("experiment_contract", "baseline"):
            value = context.results.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
    if isinstance(context.experiment_plan, Mapping):
        sources.append(context.experiment_plan)

    for source in sources:
        schema = source.get("result_schema")
        if isinstance(schema, Mapping):
            if not primary:
                primary = str(schema.get("primary_metric") or "").strip()
            add(schema.get("required_metrics"))
        if not primary:
            primary = str(source.get("primary_metric") or "").strip()
        add(source.get("metrics"))
    if primary:
        add(primary)
    return primary, set(names) if names else None


def _paired_summary_markdown(
    summaries: list[Mapping[str, Any]],
    metric_names: set[str] | None = None,
) -> str:
    """Render only declared paired summaries when a metric scope is provided."""
    rows = [
        row
        for row in summaries
        if isinstance(row, Mapping)
        and str(row.get("metric") or "").strip()
        and (metric_names is None or str(row.get("metric") or "").strip() in metric_names)
    ]
    if not rows:
        return ""
    table = [
        "| Metric | Seeds | Baseline mean | Candidate mean | Mean delta (derived) | Delta std |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        table.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown_cell(str(row['metric']))}`",
                    _format_report_metric(row.get("n")),
                    _format_report_metric(row.get("baseline_mean")),
                    _format_report_metric(row.get("candidate_mean")),
                    _format_report_metric(row.get("delta_mean")),
                    _format_report_metric(row.get("delta_sample_std")),
                )
            )
            + " |"
        )
    return "\n".join(table)


def _paired_primary_metric_markdown(comparisons: object, primary_metric: str) -> str:
    """Render one measured primary-metric row per seed when declared."""
    if not isinstance(comparisons, list) or not primary_metric:
        return ""
    table = [
        "| Seed | Baseline | Candidate | Derived delta (candidate − baseline) |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for comparison in comparisons:
        if not isinstance(comparison, Mapping):
            continue
        metrics = comparison.get("metrics")
        if not isinstance(metrics, list):
            continue
        metric = next(
            (
                row
                for row in metrics
                if isinstance(row, Mapping) and str(row.get("name")) == primary_metric
            ),
            None,
        )
        if not isinstance(metric, Mapping):
            continue
        table.append(
            "| "
            + " | ".join(
                (
                    _format_report_metric(comparison.get("seed")),
                    _format_report_metric(metric.get("baseline")),
                    _format_report_metric(metric.get("candidate")),
                    _format_report_metric(metric.get("delta")),
                )
            )
            + " |"
        )
    return "\n".join(table) if len(table) > 2 else ""


def _comparison_markdown(
    comparison: Mapping[str, Any],
    metric_names: set[str] | None = None,
) -> str:
    metric_rows = comparison.get("metrics")
    if not isinstance(metric_rows, list):
        return ""
    table = [
        "| Metric | Baseline | Candidate | Derived delta (candidate − baseline) | Interpretation |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in metric_rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or "").strip()
        if not name or (metric_names is not None and name not in metric_names):
            continue
        table.append(
            "| "
            + " | ".join(
                (
                    f"`{_markdown_cell(name)}`",
                    _format_report_metric(row.get("baseline")),
                    _format_report_metric(row.get("candidate")),
                    _format_report_metric(row.get("delta")),
                    _markdown_cell(str(row.get("interpretation") or "recorded")),
                )
            )
            + " |"
        )
    return "\n".join(table) if len(table) > 2 else ""


def _detailed_measurement_reference(context: ReportContext) -> str:
    """Link to existing immutable result artifacts without copying their rows."""

    paths: list[str] = []
    collection = context.results.get("collection_ref") if isinstance(context.results, Mapping) else None
    if isinstance(collection, Mapping) and isinstance(collection.get("path"), str):
        paths.append(collection["path"])
    else:
        paths.extend(metric.artifact for metric in context.metric_sources)

    references: list[str] = []
    for path in dict.fromkeys(path for path in paths if path):
        artifact_path = PurePosixPath(path)
        if not artifact_path.parts or artifact_path.is_absolute() or any(
            part in {".", ".."} for part in artifact_path.parts
        ):
            continue
        # Report artifacts live in attempts/<report-attempt>/; result refs are
        # session-root-relative and therefore need a path relative to that dir.
        target = relpath(artifact_path.as_posix(), "attempts/__report_attempt__")
        references.append(f"[`{path}`]({quote(target, safe='/-._~')})")
    return ", ".join(references)


def _metric_ledger(metrics: list[MetricSource]) -> str:
    table = [
        "| Source | Metric | Value | Unit | Condition | Origin |",
        "| --- | --- | ---: | --- | --- | --- |",
    ]
    for metric in metrics:
        table.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(metric.label or "experiment"),
                    f"`{_markdown_cell(metric.name)}`",
                    _format_report_metric(metric.value),
                    _markdown_cell(metric.unit or "not recorded"),
                    _markdown_cell(metric.condition_id or "not recorded"),
                    _markdown_cell(metric.source_kind),
                )
            )
            + " |"
        )
    return "\n".join(table) if len(table) > 2 else ""


def _format_report_metric(value: object) -> str:
    if value is None:
        return "not recorded"
    if isinstance(value, float):
        return f"{value:.6g}"
    return _markdown_cell(str(value))


def _markdown_cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").strip()


def claim_evidence_record_from_analysis(claim: Any) -> ClaimEvidenceRecord:
    verdict = str(getattr(claim, "verdict", "not_evaluated"))
    status = {
        "supported": "supported",
        "partially_supported": "partially_supported",
        "unsupported": "unsupported",
    }.get(verdict, "speculative")
    return ClaimEvidenceRecord(
        claim_id=str(getattr(claim, "claim_id", "claim")),
        claim=str(getattr(claim, "claim", "")),
        status=status,  # type: ignore[arg-type]
        evidence_handles=evidence_handles_from_claim(getattr(claim, "evidence", ())),
        metric_ids=[str(item) for item in getattr(claim, "metric_refs", ())],
        notes=f"Analysis verdict: {verdict}.",
    )


def evidence_handles_from_claim(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    handles: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            for key in ("handle", "ref", "artifact", "source"):
                candidate = str(item.get(key) or "").strip()
                if candidate:
                    handles.append(candidate)
                    break
        elif str(item).strip():
            handles.append(str(item).strip())
    return list(dict.fromkeys(handles))


def _synthesis_markdown(synthesis: Any) -> str:
    # Prefer the persisted evidence synthesis, not the historical research-idea
    # projection. In particular, a survey must not lose reported results merely
    # because no local experiment was requested.
    text = str(getattr(synthesis, "synthesis_markdown", "") or "").strip()
    if text:
        return text
    lines = [f"Gap summary: {synthesis.gap_summary}".strip()]
    for idea in synthesis.ideas:
        lines.append(
            f"{idea.idea_id} {idea.title}: {idea.hypothesis} "
            f"Proposed change: {idea.proposed_change}"
        )
    return "\n".join(line for line in lines if line.strip())


def _research_source_handles(
    *,
    search: SearchResult,
    brief_ref: ArtifactRef,
    execution_ref: ArtifactRef,
    analysis_ref: ArtifactRef,
    design_ref: ArtifactRef | None,
    execution: Mapping[str, Any],
    analysis: AnalysisResult,
) -> list[SourceHandle]:
    handles = [
        SourceHandle(
            handle="artifact:synthesis",
            kind="synthesis",
            artifact=brief_ref.path,
            summary="Evidence-derived research direction and experiment contract.",
        ),
        SourceHandle(
            handle="artifact:experiment_execution",
            kind="experiment",
            artifact=execution_ref.path,
            summary=f"Execution status: {execution.get('status', 'unknown')}.",
        ),
        SourceHandle(
            handle="artifact:result_analysis",
            kind="analysis",
            artifact=analysis_ref.path,
            summary=f"Analysis status: {analysis.status}.",
        ),
    ]
    if design_ref is not None:
        handles.insert(
            1,
            SourceHandle(
                handle="artifact:research_design",
                kind="research_design",
                artifact=design_ref.path,
                summary="Selected research direction and executable contract.",
            ),
        )
    handles.extend(_paper_source_handles(search))
    return handles


def _paper_source_handles(search: SearchResult) -> list[SourceHandle]:
    return [
        SourceHandle(
            handle=f"paper:{paper.id}",
            kind="paper",
            citation_key=f"P{index}",
            paper_id=paper.id,
            title=paper.title,
            summary=paper.abstract,
            metadata={
                "source": paper.source,
                "source_id": paper.source_id,
                "url": paper.url,
                "published": paper.published,
                "authors": list(paper.authors),
                "doi": paper.doi,
                "bibliography": bibliographic_details(paper),
            },
        )
        for index, paper in enumerate(search.selected_papers, start=1)
    ]


def _citation_key_map(papers: Sequence[Paper]) -> dict[str, str]:
    """Assign stable, compact keys for the current selected-paper order."""

    return {f"P{index}": paper.id for index, paper in enumerate(papers, start=1)}


__all__ = [
    "ReportProjectionError",
    "attach_paired_report_measurements",
    "attach_report_read_evidence",
    "build_literature_report_inputs",
    "build_research_report_inputs",
    "claim_evidence_record_from_analysis",
    "evidence_handles_from_claim",
    "metric_sources_from_execution",
]
