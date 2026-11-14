from __future__ import annotations

from typing import Any, Callable

from simple_ar.report.retrieval import ReportSourceResolver
from simple_ar.research.store.retrieval import order_source_chunks, rank_source_chunks, source_chunk_views as _chunk_views
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.report.schema import ReportContext, ReportIterationRecord, ReportToolCall, ReportToolResult, ReportToolSpec, SourceHandle
from simple_ar.report.tools import (
    GetCodeTaskResultArgs,
    GetMetricSourceArgs,
    GetNeighborChunksArgs,
    GetPaperBriefArgs,
    GetSynthesisBriefArgs,
    SearchSourceChunksArgs,
    report_tool_specs,
)


def checkpointed_report_reads(
    gateway: ReportToolGateway, requests: list[ReportToolCall], event: ReportIterationRecord,
    all_results: list[ReportToolResult], checkpoint: Callable[[], None],
) -> None:
    """One read-allocation owner for Writer and Reviewer; recovery never replays.

    An allocated but unconfirmed result stays explicitly unavailable rather
    than becoming evidence or a fresh tool allowance after interruption.
    """
    for index, request in enumerate(requests):
        if index < len(event.tool_results):
            continue
        pending = ReportToolResult(tool_name=request.tool_name, status="blocked",
            summary="Read allocated before checkpoint; no confirmed result was saved. This is not source evidence.",
            metadata={"request": request.model_dump(mode="json"), "lookup_state": "allocated"})
        result_index = len(all_results)
        event.tool_results.append(pending)
        all_results.append(pending)
        checkpoint()
        result = gateway.call(request)
        event.tool_results[index] = result
        all_results[result_index] = result
        checkpoint()


class ReportToolGateway:
    """Local report tool executor with OpenAI-style schema export."""

    def __init__(self, context: ReportContext, *, documents: DocumentBundle | None = None,
                 output_reader: Callable[[str, int, int, str, dict], dict] | None = None,
                 source_reader: Callable[[str], list | None] | None = None) -> None:
        self.set_context(context)
        self.specs = {spec.name: spec for spec in report_tool_specs()}
        self.call_counts = {name: 0 for name in self.specs}
        self.documents = documents
        self.output_reader = output_reader
        self.source_reader = source_reader

    def _source_chunks(self, handle: SourceHandle) -> tuple[list | None, str]:
        document_id = str(handle.metadata.get('document_id') or handle.paper_id)
        original = self.source_reader(document_id) if self.source_reader is not None else None
        if original is not None:
            return original, 'persisted_extracted_text'
        if self.documents is None:
            return None, 'unavailable'
        scope = ('selected_retained_passages' if handle.metadata.get('evidence_selection') == 'explicit_retained_chunks'
                 else 'persisted_extracted_text')
        return [row for row in self.documents.chunks if row.document_id == document_id], scope

    def set_context(self, context: ReportContext) -> None:
        """Reproject accepted citation fields without resetting tool allowance."""
        self.context = context
        self.resolver = ReportSourceResolver(context)

    def list_specs(self) -> list[ReportToolSpec]:
        """Return tool specs."""
        return list(self.specs.values())

    def openai_tools(self) -> list[dict[str, Any]]:
        """Export tool specs in OpenAI-compatible function-tool shape."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in self.list_specs()
        ]

    def call(self, call: ReportToolCall) -> ReportToolResult:
        """Execute a bounded local report tool call."""
        spec = self.specs.get(call.tool_name)
        if spec is None:
            return ReportToolResult(tool_name=call.tool_name, status="blocked", summary="Unknown report tool.")
        self.call_counts[call.tool_name] += 1
        if self.call_counts[call.tool_name] > spec.max_calls:
            return ReportToolResult(
                tool_name=call.tool_name,
                status="blocked",
                summary=f"Tool call budget exceeded for {call.tool_name}.",
            )
        try:
            return self._dispatch(call)
        except Exception as exc:  # pragma: no cover - defensive boundary
            return ReportToolResult(tool_name=call.tool_name, status="error", summary=str(exc))

    def _dispatch(self, call: ReportToolCall) -> ReportToolResult:
        name = call.tool_name
        if name == "search_source_chunks":
            args = SearchSourceChunksArgs.model_validate(call.arguments)
            handle = self.resolver.get(args.handle)
            if handle is None:
                return ReportToolResult(tool_name=name, status="not_found", summary="Registered source handle not found.")
            chunks, scope = self._source_chunks(handle)
            if chunks is None:
                return ReportToolResult(tool_name=name, status="not_found",
                    summary="Persisted text unavailable; no source search was performed.",
                    content={"search_performed": False}, source_handles=[handle.handle])
            selected = rank_source_chunks(chunks, args.query, limit=args.limit)
            return ReportToolResult(tool_name=name, status="ok" if selected else "not_found",
                summary="Lexical matches in retained text; reread neighboring passages before judging support." if selected else
                    "No lexical match in retained text. This is not proof that the original source omits the claim.",
                content={"search_performed": True, "search_scope": scope,
                    "query": args.query, "document_chunk_count": len(chunks),
                    "semantic_verification": "not_performed",
                    "chunks": _chunk_views(selected, query=args.query)}, source_handles=[handle.handle])
        if name == "get_paper_brief":
            args = GetPaperBriefArgs.model_validate(call.arguments)
            if args.handle:
                handle = self.resolver.get(args.handle)
                handles = self.resolver.find_by_paper(handle.paper_id) if handle and handle.paper_id else ([handle] if handle else [])
            elif args.citation_key:
                handles = self.resolver.find_by_citation_key(args.citation_key)
            else:
                handles = self.resolver.find_by_paper(args.paper_id)
            if not handles:
                return ReportToolResult(tool_name=name, status="not_found", summary="No paper handle found.")
            source_label = args.handle or args.citation_key or args.paper_id
            return ReportToolResult(
                tool_name=name,
                summary=f"Found {len(handles)} handle(s) for paper {source_label}.",
                content={"handles": [_tool_handle_view(handle) for handle in handles],
                         "source_front_matter": self._source_front_matter(handles)},
                source_handles=[handle.handle for handle in handles],
            )
        if name == "get_neighbor_chunks":
            args = GetNeighborChunksArgs.model_validate(call.arguments)
            handle = self.resolver.get(args.handle)
            if handle is None:
                return ReportToolResult(tool_name=name, status="not_found", summary="Chunk handle not found.")
            source_chunks, scope = self._source_chunks(handle)
            if source_chunks is not None:
                chunks = order_source_chunks(source_chunks)
                anchor = args.chunk_id or handle.chunk_id
                if not anchor:
                    passages = handle.metadata.get("evidence_passages", [])
                    anchor = next((item.get("chunk_id", "") for item in passages if isinstance(item, dict)), "")
                if scope == 'persisted_extracted_text':
                    anchor = next((item.get('source_chunk_id', anchor) for item in
                        handle.metadata.get('evidence_passages', []) if item.get('chunk_id') == anchor), anchor)
                index = next((i for i, item in enumerate(chunks) if item.chunk_id == anchor), None)
                if index is None:
                    return ReportToolResult(tool_name=name, status="not_found",
                        summary="No persisted anchor in this source. Choose a chunk id; no unrelated passage was substituted.",
                        content={"available_chunk_ids": [item.chunk_id for item in chunks[:12]],
                                 "chunk_ids_truncated": len(chunks) > 12}, source_handles=[handle.handle])
                # A fixed output window, centered on the actual cited passage.
                # Prefer source positions to ingest priority; unknown locations
                # keep legacy retained order. Never cross source identity.
                selected = chunks[max(0, index - args.before):index + args.after + 1]
                rows = _chunk_views(selected)
                return ReportToolResult(tool_name=name, summary=f"Read {len(rows)} retained passages around {anchor}; retained neighbors do not guarantee contiguous original text.",
                    content={"source_kind": scope, "anchor_chunk_id": anchor,
                             "chunks": rows, "document_chunk_count": len(chunks)}, source_handles=[handle.handle])
            related = self.resolver.find_by_paper(handle.paper_id) if handle.paper_id else [handle]
            index = next((i for i, item in enumerate(related) if item.handle == handle.handle), 0)
            selected = related[max(0, index - args.before):index + args.after + 1]
            return ReportToolResult(
                tool_name=name,
                summary="Persisted original text unavailable; returned metadata/previous excerpts, not a fresh source read.",
                content={"source_kind": "metadata_or_cached_excerpt", "handles": [_tool_handle_view(item) for item in selected]},
                source_handles=[item.handle for item in selected],
            )
        if name == "get_metric_source":
            args = GetMetricSourceArgs.model_validate(call.arguments)
            metric = next((item for item in self.context.metric_sources if item.metric_id == args.metric_id), None)
            if metric is None:
                return ReportToolResult(tool_name=name, status="not_found", summary="Metric source not found.")
            return ReportToolResult(
                tool_name=name,
                summary=f"Metric {metric.name}={metric.value} from {metric.artifact}.",
                content=metric.model_dump(mode="json"),
            )
        if name == "get_synthesis_brief":
            from simple_ar.report.narrative import DERIVED_CONTEXT_STATUS
            args = GetSynthesisBriefArgs.model_validate(call.arguments)
            text = self.context.synthesis_markdown or self.context.hypothesis_markdown or self.context.evidence_summary
            if args.query:
                hits = self.resolver.search(args.query, limit=5)
            else:
                hits = []
            return ReportToolResult(
                tool_name=name,
                summary="Returned compact synthesis context.",
                content={
                    "text_status": dict(DERIVED_CONTEXT_STATUS),
                    "text": text[:2400],
                    "truncated": len(text) > 2400,
                    "total_characters": len(text),
                    "matching_handles": [_tool_handle_view(handle) for handle in hits],
                },
                source_handles=[handle.handle for handle in hits],
            )
        if name == "get_code_task_result":
            args = GetCodeTaskResultArgs.model_validate(call.arguments)
            if args.output_handle:
                handle = self.resolver.get(args.output_handle)
                if handle is None or handle.kind != "experiment_output":
                    return ReportToolResult(tool_name=name, status="not_found", summary="Registered output handle not found.")
                if self.output_reader is None:
                    return ReportToolResult(tool_name=name, status="not_found", summary="Output reader unavailable; no file was read.")
                content = self.output_reader(handle.artifact, args.offset, args.limit, args.query, args.record_match)
                found = (content.get("matched_records", 0) > 0 if args.record_match else
                         not args.query or content.get("query_matched"))
                return ReportToolResult(tool_name=name, status="ok" if found else "not_found",
                    summary="Read bounded producer text; not independent verification." if found else
                            "No matching record or literal phrase in this registered file; no semantic absence claim is established.",
                    content=content, source_handles=[handle.handle])
            return ReportToolResult(
                tool_name=name,
                summary="Returned experiment result artifact context.",
                content={
                    "results": self.context.results,
                    "metric_sources": [metric.model_dump(mode="json") for metric in self.context.metric_sources],
                },
            )
        return ReportToolResult(tool_name=name, status="blocked", summary="Unhandled report tool.")

    def _source_front_matter(self, handles: list[SourceHandle]) -> list[dict[str, Any]]:
        """Reread saved source headers by identity, never by title or live paths.

        Headers live in the existing section handoff even when a physical chunk
        cap omits their indexed chunks. Original text and recorded metadata stay
        distinct; this view does not certify identity or change the bibliography.
        """
        document_ids = list(dict.fromkeys(
            str(handle.metadata.get("document_id") or handle.paper_id)
            for handle in handles
        ))
        rows = []
        remaining = 4800
        for document_id in document_ids[:4]:
            sections = [section for section in (self.documents.sections if self.documents else [])
                        if section.document_id == document_id and section.section == "front_matter"]
            passages = []
            for section in sections:
                text = section.text[:min(1600, remaining)]
                if not text:
                    break
                remaining -= len(text)
                passages.append({"section_id": section.section_id, "text": text,
                                 "line_start": section.line_start, "line_end": section.line_end,
                                 "total_characters": len(section.text), "truncated": len(text) < len(section.text)})
            rows.append({"document_id": document_id,
                         "status": "available" if passages else "omitted_from_tool_window" if sections else "not_retained",
                         "source_kind": "persisted_extracted_text",
                         "identity_verification": "not_performed",
                         "passages": passages, "omitted_sections": len(sections) - len(passages)})
        if len(document_ids) > 4:
            rows.append({"omitted_documents": len(document_ids) - 4})
        return rows


def _tool_handle_view(handle: Any) -> dict[str, Any]:
    """Use the same bounded evidence projection as drafting and review.

    A brief must not reinsert entire reading records into every supplementary
    context. Original passages remain available through the anchored read tools.
    """
    from simple_ar.report.narrative import _prompt_handle_view
    return _prompt_handle_view(handle)
