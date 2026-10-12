"""Native-tool adapters over existing source search and document ingestion."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.artifacts import write_json
from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.research.contracts import DocumentRecord, SourcePlan
from simple_ar.research.documents.ingest import DocumentBundle, build_document_bundle, build_supporting_material_bundle
from simple_ar.research.store.retrieval import rank_source_chunks, source_chunk_views
from simple_ar.research.preparation_assets import validate_public_url
from simple_ar.research.sources.capability import SearchRequest, SearchResult, search_sources
from simple_ar.research.sources.registry import default_search_provider_registry
from simple_ar.tools.specs import CommonToolSpec, ToolResult


class _Search(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    providers: list[Literal["arxiv", "openalex", "semantic_scholar", "web"]] = Field(
        default_factory=lambda: ["arxiv", "openalex"], min_length=1, max_length=3)
    max_results: int = Field(default=5, ge=1, le=20)


class _Acquire(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    search_record: str = Field(default="", description="Saved evidence/.../search.json returned by search_sources.")
    paper_id: str = Field(default="", description="Exact ID in that saved search record, not a guessed citation.")
    url: str = Field(default="", description="Alternatively, an explicit public HTTPS document URL; no credentials.")


class _Passages(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    document_record: str = Field(description="DocumentBundle record in saved evidence or a supplied read-only source mount.")
    query: str = Field(default="", max_length=2000,
        description="Question for lexical passage lookup; omit to inspect source identity and retained-text coverage without reading the full JSON.")
    limit: int = Field(default=5, ge=1, le=8)
    max_chars: int = Field(default=6000, ge=500, le=12000)


def source_tool_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name=name, description=description, input_schema=model.model_json_schema(),
        permission_level=permission, risk_level="medium" if permission == "network" else "low", domain="sources")
        for name, model, permission, description in (
            ("search_sources", _Search, "network", "Search selected scholarly/web providers. Save metadata and failure history; excerpts are not original full text."),
            ("acquire_source", _Acquire, "network", "Acquire one saved search result or explicit public HTTPS source using existing bounded document parsers. Save original evidence and expose readable chunks; retrieval may fail."),
            ("find_source_passages", _Passages, "read_only", "Query retained text for bounded lexical matches together with source titles, URLs and retained coverage. Omit the query only for identity/coverage inspection; a separate identity lookup is not needed after querying this record. Lexical selection is not exhaustive reading; no new network or model call."))]


class SourceToolGateway:
    @staticmethod
    def inventory_source(path: Path) -> dict[str, Any] | None:
        """Describe a bound original bundle for professional material selection.

        The caller owns path authorization and inventory digests. This is a
        read-only view of declared provenance, not source verification or a
        second index. Ordinary JSON and oversized files remain ordinary inputs.
        """
        if path.suffix.lower() != '.json' or path.stat().st_size > 8_000_000:
            return None
        try:
            payload = json.loads(path.read_bytes())
        except (ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict) or payload.get('schema_version') != 'document_bundle.v1':
            return None
        bundle = DocumentBundle.from_handoff_dict(payload)
        return {'document_record': str(path),
            'documents': [{'id': row.document_id, 'title': row.title, 'url': row.url,
                'status': row.extraction_status} for row in bundle.records[:20]],
            'documents_truncated': len(bundle.records) > 20,
            'retained_chunks': len(bundle.chunks),
            'retained_preview': source_chunk_views(bundle.chunks[:1], max_chars=1200),
            'scope': 'Declared source identity and bounded retained preview, not verified claims or exhaustive reading.'}

    @staticmethod
    def history_identity(name: str, data: dict[str, Any]) -> dict[str, Any]:
        """Keep acquisition identity, not source prose or a second evidence store.

        A successful adapter receipt can describe metadata-only/failed retrieval.
        Preserve those observed statuses with the bound record so compaction does
        not turn every source into an indistinguishable successful tool call.
        """
        if name != 'acquire_source':
            return {}
        return {key: data[key] for key in ('document_record', 'documents', 'text_bytes')
                if key in data}

    def __init__(self, root: Path, workspace: Any, ledger: BudgetLedger) -> None:
        self.root, self.workspace, self.ledger = root, workspace, ledger
        self.registry = default_search_provider_registry()

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid source invocation identity")
            if name == "find_source_passages":
                request = _Passages.model_validate(arguments)
                path = self.workspace.resolve_source_record(request.document_record)
                bundle = DocumentBundle.from_handoff_dict(json.loads(path.read_text()))
                identity = {
                    "document_record": request.document_record,
                    "documents": [{"id": record.document_id, "title": record.title,
                        "url": record.url, "status": record.extraction_status}
                        for record in bundle.records[:20]],
                    "documents_truncated": len(bundle.records) > 20,
                    "retained_chunks": len(bundle.chunks), "retained_sections": len(bundle.sections),
                }
                if not request.query.strip():
                    return ToolResult(tool_name=name, data={
                        **identity,
                        "retained_preview": source_chunk_views(bundle.chunks[:1], max_chars=1200),
                        "scope": "Source identity, retained coverage and a bounded retained-text preview; not full reading. Query passages for consequential claims. Supplied source metadata is not independently verified."})
                selected = rank_source_chunks(bundle.chunks, request.query, limit=request.limit)
                return ToolResult(tool_name=name, data={
                    **identity,
                    "matches": source_chunk_views(selected, query=request.query, max_chars=request.max_chars),
                    "selected_chunks": len(selected),
                    "scope": "Lexical lookup in retained text; not full reading or proof of absence"})
            request = (_Search if name == "search_sources" else _Acquire).model_validate(arguments)
            directory = self.root / "evidence" / identity
            if name == "search_sources":
                operation = lambda: self._search(request, directory)
                count = len(set(request.providers))
            else:
                papers = []
                records = []
                if request.search_record:
                    if request.url or not request.paper_id:
                        raise ValueError("Choose a saved search result OR an explicit URL")
                    path = self.workspace.resolve(request.search_record)
                    if not path.is_relative_to(self.root / "evidence"):
                        raise ValueError("Use a saved search result from the task evidence directory")
                    result = SearchResult.from_handoff_dict(json.loads(path.read_text()))
                    papers = [paper for paper in result.papers if paper.id == request.paper_id]
                    if not papers:
                        raise ValueError("Paper ID is absent from this saved search result")
                    for paper in papers:
                        validate_public_url(paper.fulltext_url or paper.url)
                else:
                    if request.paper_id:
                        raise ValueError("Paper ID requires its saved search record")
                    validate_public_url(request.url)
                    records = [DocumentRecord(document_id=identity, title=request.url,
                        source="supporting_material", url=request.url,
                        metadata={"parent_document_id": "explicit_task_source"})]
                operation = lambda: self._acquire(papers, records, directory)
                count = 1
            # Counts adapter/provider operations, not physical HTTP requests or
            # vendor credits. Existing acquisition owns redirect/byte limits.
            reservation = f"source:{identity}"
            if any(entry.reservation_id == reservation for entry in self.ledger.entries):
                raise ValueError("Source invocation already recorded; inspect its result before any replay")
            entry = self.ledger.reserve(reservation, {"source_operations": count}, purpose=name)
            if entry.status != "reserved":
                raise ValueError("Source operation already accounted; cannot replay")
            directory.mkdir(parents=True, exist_ok=True)
            try:
                data = operation()
            except Exception:
                self.ledger.settle(reservation, {"source_operations": count}, actual_source="adapter",
                    reason="Source adapter attempted; operation failed")
                raise
            except BaseException:
                self.ledger.mark_unknown(reservation, reason="Source operation interrupted; inspect saved records before replay")
                raise
            self.ledger.settle(reservation, {"source_operations": count}, actual_source="adapter")
            return ToolResult(tool_name=name, data=data)
        except (ValueError, KeyError, OSError, BudgetError) as exc:
            return ToolResult(tool_name=name, status="blocked", error=str(exc))

    def _search(self, request: _Search, directory: Path) -> dict[str, Any]:
        result = search_sources(SearchRequest(queries=(request.query,),
            providers=tuple(dict.fromkeys(request.providers)), max_results_per_query=request.max_results,
            cache_dir=self.root / "source_cache"), registry=self.registry)
        write_json(directory / "search.json", result.to_handoff_dict())
        return {"status": result.status, "search_record": f"evidence/{directory.name}/search.json",
            "responses": result.to_dict()["responses"], "diagnostics": list(result.diagnostics),
            "papers": [{**paper.to_row(), "abstract": paper.abstract[:1500]}
                       for paper in {paper.id: paper for paper in result.papers}.values()],
            "scope": "Provider metadata/excerpts; acquire originals for consequential claims"}

    def _acquire(self, papers: list, records: list, directory: Path) -> dict[str, Any]:
        plan = SourcePlan(queries=[], require_fulltext=True, allow_pdf_download=True,
            budget={"max_fulltext_documents": 1, "max_fulltext_fetch_attempts": 3,
                    "max_pdf_mb": 8, "keep_raw_pdf": True, "parser_backend": "basic",
                    "max_pdf_pages": 60,
                    "web_extract_backend": "direct", "public_network_only": True})
        options = dict(source_plan=plan, cache_dir=directory / "raw", extraction_dir=directory / "parsed")
        bundle = (build_document_bundle(papers=papers, **options) if papers else
                  build_supporting_material_bundle(records=records, **options))
        write_json(directory / "document.json", bundle.to_handoff_dict())
        # Raw extraction and canonical provenance stay outside writable output.
        # Model text windows are served by the same mounted file reader.
        text = "\n\n".join(f"[{chunk.chunk_id}; document={chunk.document_id}; page={chunk.page}]\n{chunk.text}"
                           for chunk in bundle.chunks)
        (directory / "passages.txt").write_text(text, encoding="utf-8")
        return {"document_record": f"evidence/{directory.name}/document.json",
            "passages": f"evidence/{directory.name}/passages.txt", "text_bytes": len(text.encode()),
            "documents": [{"id": record.document_id, "title": record.title,
                "url": record.url, "status": record.extraction_status} for record in bundle.records],
            "acquisition": bundle.fulltext_manifest, "extraction": bundle.fulltext_extraction,
            "scope": "Only acquired passages support original-text claims; metadata is not full text"}
