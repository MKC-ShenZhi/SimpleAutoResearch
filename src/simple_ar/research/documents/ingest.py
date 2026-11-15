"""Deterministic document-ingest composition for the research pipeline.

This module owns the handoff between paper metadata, optional full text, and
the derived section/chunk records consumed by reading and evidence code.  It
does not write stage artifacts or call an LLM; callers decide how to persist
the returned bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.core.artifacts import read_json
from simple_ar.literature.models import Paper
from simple_ar.research.contracts import DocumentRecord, DocumentSection, SourcePlan, TextChunk
from simple_ar.research.documents.extractors import apply_fulltext_extraction
from simple_ar.research.documents.fulltext import build_fulltext_manifest
from simple_ar.research.documents.ports import DocumentParser, DocumentResolver
from simple_ar.research.documents.records import build_document_records
from simple_ar.research.documents.sections import build_document_sections
from simple_ar.research.store.chunking import build_text_chunks


@dataclass(frozen=True)
class DocumentBundle:
    """Ingested document data before stage-specific serialization.

    The bundle is deliberately limited to data already represented by the
    existing research contracts.  Index persistence and JSON/JSONL projection
    stay outside this boundary so the same ingest result can serve a future
    standalone reader without taking on the Search stage's output layout.
    """

    records: list[DocumentRecord]
    fulltext_manifest: dict[str, Any]
    fulltext_extraction: dict[str, Any]
    sections: list[DocumentSection]
    chunks: list[TextChunk]

    def to_handoff_dict(self) -> dict[str, Any]:
        """Return one canonical, restorable document handoff."""

        return {
            "schema_version": "document_bundle.v1",
            "documents": [record.to_row() for record in self.records],
            "fulltext_manifest": dict(self.fulltext_manifest),
            "fulltext_extraction": dict(self.fulltext_extraction),
            "sections": [section.to_row() for section in self.sections],
            "chunks": [chunk.to_row() for chunk in self.chunks],
        }

    @classmethod
    def from_rows(
        cls,
        *,
        documents: Iterable[dict[str, Any]],
        chunks: Iterable[dict[str, Any]],
        sections: Iterable[dict[str, Any]] = (),
        fulltext_manifest: dict[str, Any] | None = None,
        fulltext_extraction: dict[str, Any] | None = None,
    ) -> "DocumentBundle":
        """Hydrate the bundle from persisted, schema-compatible rows."""
        return cls(
            records=[_from_row(DocumentRecord, row) for row in documents],
            fulltext_manifest=dict(fulltext_manifest or {}),
            fulltext_extraction=dict(fulltext_extraction or {}),
            sections=[_from_row(DocumentSection, row) for row in sections],
            chunks=[_from_row(TextChunk, row) for row in chunks],
        )

    @classmethod
    def from_handoff_dict(cls, data: Mapping[str, Any]) -> "DocumentBundle":
        """Restore a ``document_bundle.v1`` without network or parser calls."""

        if str(data.get("schema_version") or "") != "document_bundle.v1":
            raise ValueError("Expected a document_bundle.v1 object.")
        return cls.from_rows(
            documents=_mapping_rows(data.get("documents")),
            sections=_mapping_rows(data.get("sections")),
            chunks=_mapping_rows(data.get("chunks")),
            fulltext_manifest=(
                dict(data["fulltext_manifest"])
                if isinstance(data.get("fulltext_manifest"), Mapping)
                else {}
            ),
            fulltext_extraction=(
                dict(data["fulltext_extraction"])
                if isinstance(data.get("fulltext_extraction"), Mapping)
                else {}
            ),
        )


def merge_document_reports(previous: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Keep independent acquisition/extraction reports and their document rows."""
    if not previous or not fresh or previous == fresh:
        return dict(previous or fresh)
    batches = []
    for report in (previous, fresh):
        for batch in report.get('batches', [report]):
            if batch not in batches:
                batches.append(batch)
    documents = []
    for batch in batches:
        for row in batch.get('documents', []):
            if row not in documents:
                documents.append(row)
    # Budgets and fetch counts belong to their original attempts, not to this
    # newly assembled view. Keep them in the unmodified individual reports.
    return {'schema_version': previous.get('schema_version') or fresh.get('schema_version'),
        'document_count': len({row.get('document_id') for row in documents}),
        'documents': documents, 'batches': batches}


def merge_document_bundles(previous: DocumentBundle | None, fresh: DocumentBundle) -> DocumentBundle:
    """Combine retained source content with its actual acquisition provenance."""
    if previous is None:
        return fresh
    return DocumentBundle(records=[*previous.records, *fresh.records],
        sections=[*previous.sections, *fresh.sections], chunks=[*previous.chunks, *fresh.chunks],
        fulltext_manifest=merge_document_reports(previous.fulltext_manifest, fresh.fulltext_manifest),
        fulltext_extraction=merge_document_reports(previous.fulltext_extraction, fresh.fulltext_extraction))


def analysis_material_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    """Recognize declared analysis packages, not every JSON writing material.

    Ordinary JSON remains unverified source text. A declared table-analysis
    family must still pass its existing loader; corrupt packages never fall
    through into supposedly usable prose evidence.
    """
    from simple_ar.result_analysis.table import load_analysis_package
    packages = []
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        payload = read_json(path)
        if isinstance(payload, dict) and str(payload.get("schema_version", "")).startswith("table_analysis."):
            load_analysis_package(path)
            packages.append(path)
        elif isinstance(payload, dict) and str(payload.get("schema_version", "")).startswith("code_analysis."):
            from simple_ar.result_analysis.script_project import copy_code_analysis_package
            copy_code_analysis_package(path, None)
            packages.append(path)
    return tuple(packages)


def retained_document_materials(paths: Iterable[Path], *, original_sources_only: bool = False) -> dict[Path, DocumentBundle]:
    """Restore declared source bundles, not serialized prose or old judgments.

    Embedded records/sections/chunks are reused. Source paths are provenance,
    never instructions to open files or fetch remote content. Declared analysis
    attachments are separately rechecked by the existing package owner.
    """
    retained = {}
    for path in paths:
        if path.suffix.lower() != ".json":
            continue
        payload = read_json(path)
        if not isinstance(payload, Mapping) or not str(payload.get("schema_version", "")).startswith("document_bundle."):
            continue
        bundle = DocumentBundle.from_handoff_dict(payload)
        identifiers = [record.document_id for record in bundle.records]
        if not identifiers or len(set(identifiers)) != len(identifiers):
            raise ValueError("Retained document bundle requires distinct source identities.")
        if any(row.document_id not in identifiers for row in [*bundle.sections, *bundle.chunks]):
            raise ValueError("Retained source text refers to an unknown document.")
        if original_sources_only:
            identifiers = {row.document_id for row in bundle.records if row.is_original_source}
            if not identifiers:
                raise ValueError("Retained document bundle has no original sources for a survey.")
            bundle = replace(bundle, records=[row for row in bundle.records if row.document_id in identifiers],
                sections=[row for row in bundle.sections if row.document_id in identifiers],
                chunks=[row for row in bundle.chunks if row.document_id in identifiers])
        retained[path.resolve()] = bundle
    return retained


@dataclass(frozen=True, slots=True)
class DocumentIngestRequest:
    """Inputs for one reusable document-ingest attempt."""

    papers: tuple[Paper, ...]
    source_plan: SourcePlan
    extraction_dir: Path
    cache_dir: Path | None = None
    max_chunks: int | None = None
    resolver: DocumentResolver | None = None
    parser: DocumentParser | None = None
    analysis_paths: tuple[Path, ...] = ()
    supporting_records: tuple[DocumentRecord, ...] = ()
    original_sources_only: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "papers", tuple(self.papers))
        object.__setattr__(self, "supporting_records", tuple(self.supporting_records))
        if self.max_chunks is not None and self.max_chunks < 1:
            raise ValueError("DocumentIngestRequest.max_chunks must be positive.")


def build_document_bundle(
    *,
    papers: list[Paper],
    source_plan: SourcePlan,
    cache_dir: Path | None,
    extraction_dir: Path,
    max_chunks: int | None = None,
    resolver: DocumentResolver | None = None,
    parser: DocumentParser | None = None,
    supporting_records: Iterable[DocumentRecord] = (),
) -> DocumentBundle:
    """Build the reusable document/section/chunk handoff.

    Full-text fetch and parsing retain their existing permission and budget
    behavior.  In particular, failures remain rows in the extraction manifest
    and do not turn a document-ingest failure into a pipeline exception.
    """

    supporting = _supporting_records(supporting_records)
    exhausted = supporting and any(type(source_plan.budget.get(key)) is not int or source_plan.budget[key] <= 0
        for key in ("max_fulltext_documents", "max_fulltext_fetch_attempts", "max_pdf_mb"))
    source_plan = replace(source_plan, require_fulltext=source_plan.require_fulltext and not exhausted)
    records = [*supporting, *build_document_records(papers=papers, source_plan=source_plan)]
    return build_record_bundle(records=records, source_plan=source_plan, cache_dir=cache_dir,
                               extraction_dir=extraction_dir, max_chunks=max_chunks,
                               resolver=resolver, parser=parser)


def build_supporting_material_bundle(
    *, records: list[DocumentRecord], source_plan: SourcePlan,
    cache_dir: Path, extraction_dir: Path, max_chunks: int | None = None,
    resolver: DocumentResolver | None = None, parser: DocumentParser | None = None,
) -> DocumentBundle:
    """Ingest explicit webpage records, never Paper rows or guessed URLs.

    Callers provide independent document_ids and metadata.parent_document_id.
    Budgets are REMAINING allowances: zero/missing documents, attempts or
    max_pdf_mb denies acquisition (including cache reuse). Original records
    survive failures; the returned manifest is the attempt/cache history.
    No network occurs without require_fulltext. No recursive link traversal.
    """
    return build_document_bundle(papers=[], supporting_records=records, source_plan=source_plan,
        cache_dir=cache_dir, extraction_dir=extraction_dir, max_chunks=max_chunks,
        resolver=resolver, parser=parser)


def _supporting_records(records: Iterable[DocumentRecord]) -> list[DocumentRecord]:
    from simple_ar.research.preparation_assets import validate_public_url
    material_records = []
    for record in records:
        if (record.local_path or not record.url or record.metadata.get("fulltext_hints")
                or not record.metadata.get("parent_document_id")
                or record.document_id == record.metadata.get("parent_document_id")):
            raise ValueError("Explicit independent webpage record with parent_document_id required.")
        validate_public_url(record.url)  # Syntax only; DNS occurs after budget/permission checks.
        material_records.append(replace(record, source="supporting_material", metadata={
            **record.metadata, "kind": "supporting_material",
            "content_scope": "linked_material_not_paper_or_measured_result",
        }))
    return material_records


def build_record_bundle(
    *, records: list[DocumentRecord], source_plan: SourcePlan, cache_dir: Path | None,
    extraction_dir: Path, max_chunks: int | None = None,
    resolver: DocumentResolver | None = None, parser: DocumentParser | None = None,
) -> DocumentBundle:
    """Shared records→fetch history→extraction→sections/chunks composition."""
    fulltext_manifest = build_fulltext_manifest(
        records=records,
        source_plan=source_plan,
        cache_dir=cache_dir,
    )
    records, fulltext_extraction = apply_fulltext_extraction(
        records=records,
        fulltext_manifest=fulltext_manifest,
        source_plan=source_plan,
        extraction_dir=extraction_dir,
        resolver=resolver,
        parser=parser,
    )
    sections = build_document_sections(records)
    chunks = build_text_chunks(records, sections=sections, max_chunks=max_chunks)
    return DocumentBundle(
        records=records,
        fulltext_manifest=fulltext_manifest,
        fulltext_extraction=fulltext_extraction,
        sections=sections,
        chunks=chunks,
    )


def build_local_document_bundle(
    paths: Iterable[str | Path],
    *,
    extraction_dir: Path,
    max_chunks: int | None = None,
    parser_backend: str = "basic",
    resolver: DocumentResolver | None = None,
    parser: DocumentParser | None = None,
) -> DocumentBundle:
    """Build a document bundle from local files without running Search."""
    local_documents = [str(path) for path in paths]
    source_plan = SourcePlan(
        queries=["local documents"],
        sources=["local_files"],
        max_results_per_query=max(1, len(local_documents)),
        require_fulltext=True,
        allow_pdf_download=False,
        local_documents=local_documents,
        budget={"parser_backend": parser_backend},
    )
    return build_document_bundle(
        papers=[],
        source_plan=source_plan,
        cache_dir=None,
        extraction_dir=extraction_dir,
        max_chunks=max_chunks,
        resolver=resolver,
        parser=parser,
    )


def build_draft_document_bundle(path: Path, *, extraction_dir: Path, identity: str) -> DocumentBundle:
    """Keep an explicitly selected draft readable but separate from evidence.

    The caller owns draft selection; neither filenames nor prose determine its
    role. Use the ordinary parser and retained-bundle handoff, not a new store.
    """
    bundle = build_local_document_bundle([path], extraction_dir=extraction_dir)
    identifiers = {row.document_id: f"{identity}:{row.document_id}" for row in bundle.records}
    bundle.records[:] = [replace(row, document_id=identifiers[row.document_id], metadata={
        **row.metadata, "kind": "prior_draft", "evidence_role": "prior_draft_not_primary_evidence",
    }) for row in bundle.records]
    bundle.sections[:] = [replace(row, document_id=identifiers[row.document_id],
        section_id=f"{identity}:{row.section_id}") for row in bundle.sections]
    bundle.chunks[:] = build_text_chunks(bundle.records, sections=bundle.sections)
    return bundle


def run_document_ingest_capability(
    *,
    context: CapabilityContext,
    request: DocumentIngestRequest,
) -> CapabilityResult:
    """Persist one document bundle as an explicit session handoff.

    Fetching, parsing, and cache policy remain owned by the existing ingest
    implementation. This adapter only adds attempt-local persistence and a
    stable status mapping for downstream Read capabilities.
    """

    packages = () if request.original_sources_only else analysis_material_paths(request.analysis_paths)
    retained = retained_document_materials(request.analysis_paths, original_sources_only=request.original_sources_only)
    package_paths = list(packages)
    for path, saved in list(retained.items()):
        analyses = [record for record in saved.records
                    if record.metadata.get("table_analysis") is not None or record.metadata.get("code_analysis") is not None]
        for record in analyses:
            package = record.metadata.get("table_analysis") or record.metadata["code_analysis"]
            source = (path.parent / package["artifact"]).resolve()
            if not source.is_relative_to(path.parent):
                raise ValueError("Retained analysis must stay inside the selected document package.")
            package_paths.append(source)
        if analyses:
            ids = {record.document_id for record in analyses}
            retained[path] = replace(saved, records=[row for row in saved.records if row.document_id not in ids],
                sections=[row for row in saved.sections if row.document_id not in ids],
                chunks=[row for row in saved.chunks if row.document_id not in ids])
    packages = tuple(dict.fromkeys(package_paths))
    analysis_paths = {str(path.resolve()) for path in packages} | {str(path) for path in retained}
    source_plan = replace(request.source_plan, local_documents=[path for path in request.source_plan.local_documents
                         if str(Path(path).resolve()) not in analysis_paths])
    bundle = build_document_bundle(
        papers=list(request.papers),
        supporting_records=request.supporting_records,
        source_plan=source_plan,
        cache_dir=request.cache_dir,
        extraction_dir=request.extraction_dir,
        max_chunks=request.max_chunks,
        resolver=request.resolver,
        parser=request.parser,
    )
    imported = []
    for index, path in enumerate(packages, start=1):
        from simple_ar.result_analysis.table import copy_analysis_package, table_markdown
        prefix = f"analyses/analysis-{index:03d}"
        code_package = read_json(path).get("schema_version") == "code_analysis.v1"
        if code_package:
            from simple_ar.result_analysis.script_project import copy_code_analysis_package
            result = copy_code_analysis_package(path, context.store.root / prefix)
            text = (context.store.root / prefix / "outputs/report.md").read_text(encoding="utf-8")
        else:
            result = copy_analysis_package(path, context.store.root / prefix)
            text = table_markdown({**result, "figures": []})
        document_id = f"supplied-analysis-{index:03d}"
        # Package-local image links are not report-local paths. Keep the
        # numerical material readable; registered figures travel separately
        # to planning and assembly rather than being copied into writer prose.
        artifact = f"{prefix}/analysis.json"
        if code_package:
            bundle.records.append(DocumentRecord(document_id=document_id, title=f"Supplied code analysis {index}",
                source="local_analysis", source_id=str(path.resolve()), extraction_status="parsed", parser="code_analysis.v1",
                metadata={"code_analysis": {**result, "artifact": artifact},
                          "evidence_role": "validated_script_output_not_independently_recomputed"}))
            bundle.sections.append(DocumentSection(section_id=f"{document_id}:results", document_id=document_id,
                section="results", heading="Supplied script results", text=text, source_path=f"{prefix}/outputs/report.md"))
            # Numeric outputs are structured source material, not one long
            # narrative line. Keep field identity on every derived chunk so
            # later groups remain discoverable after the initial excerpt.
            values = result["results"]
            fields = values.items() if isinstance(values, dict) else [(None, values)]
            for field_index, (key, value) in enumerate(fields, 1):
                bundle.sections.append(DocumentSection(
                    section_id=f"{document_id}:output-{field_index}", document_id=document_id,
                    section="results", heading="Recorded script outputs" + (f": {key}" if key is not None else ""),
                    text=json.dumps({key: value} if key is not None else value, ensure_ascii=False, indent=2),
                    source_path=f"{prefix}/outputs/results.json"))
            imported.extend(context.store.ref(item.relative_to(context.store.root), kind="code_analysis_attachment")
                for item in (context.store.root / prefix).rglob("*") if item.is_file())
            continue
        bundle.records.append(DocumentRecord(document_id=document_id, title=f"Descriptive analysis: {result['source_name']}",
            source="local_analysis", source_id=str(path.resolve()), extraction_status="parsed", parser="table_analysis.v1",
            metadata={"table_analysis": {"artifact": artifact, "records": result["records"], "spec": result["spec"],
                                        "row_count": result["row_count"], "figures": result["figures"],
                                        "coordinate_summaries": result.get("coordinate_summaries", []),
                                        "observation_summaries": result.get("observation_summaries", []),
                                        "paired_comparisons": result.get("paired_comparisons", [])},
                      "evidence_role": "recomputed_from_user_supplied_data"}))
        bundle.sections.append(DocumentSection(section_id=f"{document_id}:results", document_id=document_id,
            section="results", heading="Rechecked descriptive data", text=text, source_path=f"{prefix}/analysis.md"))
        imported.extend(context.store.ref(item.relative_to(context.store.root), kind="table_analysis" if item.name == "analysis.json" else "analysis_attachment",
            schema="table_analysis.v1" if item.name == "analysis.json" else None)
            for item in (context.store.root / prefix).rglob("*") if item.is_file())
    if packages:
        bundle.chunks[:] = build_text_chunks(bundle.records, sections=bundle.sections, max_chunks=request.max_chunks)
    for path, saved in retained.items():
        existing = {record.document_id: record for record in bundle.records}
        # A follow-up naturally retrieves previously read sources. Choose one
        # complete text version; never mix old chunks with new sections.
        keep_saved = set()
        for record in saved.records:
            current = existing.get(record.document_id)
            if current is not None:
                if (current.source != record.source or current.is_original_source != record.is_original_source
                        or any(getattr(current, key) and getattr(record, key)
                               and getattr(current, key) != getattr(record, key) for key in ("source_id", "doi"))):
                    raise ValueError("Retained document identities conflict with another supplied source.")
                current_text = any(row.document_id == record.document_id for row in bundle.chunks)
                saved_text = any(row.document_id == record.document_id for row in saved.chunks)
                if current_text and (current.extraction_status == "parsed" or not saved_text):
                    continue
            keep_saved.add(record.document_id)
        bundle.records[:] = [row for row in bundle.records if row.document_id not in keep_saved]
        bundle.sections[:] = [row for row in bundle.sections if row.document_id not in keep_saved]
        bundle.chunks[:] = [row for row in bundle.chunks if row.document_id not in keep_saved]
        bundle.records.extend(replace(record, metadata={**record.metadata,
            "retained_bundle": str(path),
            "retained_source_role": "paper" if record.metadata.get("paper_id") else "material",
            "evidence_role": record.metadata.get("evidence_role") or "retained_source_text_not_reverified"})
            for record in saved.records if record.document_id in keep_saved)
        bundle.sections.extend(row for row in saved.sections if row.document_id in keep_saved)
        bundle.chunks.extend(row for row in saved.chunks if row.document_id in keep_saved)
    output = context.store.write_json(
        "document_bundle.json",
        bundle.to_handoff_dict(),
        kind="document_bundle",
        schema="document_bundle.v1",
        producer="research.documents",
    )
    diagnostics: list[str] = []
    failed_count = int(bundle.fulltext_extraction.get("failed_count") or 0)
    if not bundle.records:
        status = "blocked"
        diagnostics.append("No documents were available for ingest.")
    elif not bundle.chunks:
        status = "blocked"
        diagnostics.append("Ingest produced no text chunks.")
    elif failed_count:
        status = "partial"
        diagnostics.append(f"{failed_count} document extraction(s) failed.")
    else:
        status = "completed"
    return CapabilityResult(
        status=status,  # type: ignore[arg-type]
        artifacts=(output, *imported),
        diagnostics=tuple(diagnostics),
        usage={
            "documents": len(bundle.records),
            "sections": len(bundle.sections),
            "chunks": len(bundle.chunks),
            "extraction_failures": failed_count,
        },
        provenance={
            "capability": "document_ingest",
            "result_schema": "document_bundle.v1",
        },
    )


def _from_row(type_hint: type[Any], row: dict[str, Any]) -> Any:
    """Instantiate a persisted dataclass row while ignoring unknown fields."""
    allowed = {field.name for field in fields(type_hint)}
    return type_hint(**{key: value for key, value in row.items() if key in allowed})


def _mapping_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]
