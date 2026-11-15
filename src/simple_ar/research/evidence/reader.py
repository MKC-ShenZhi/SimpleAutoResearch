"""Reusable document-to-evidence reading boundary.

The reader owns card derivation from an already ingested ``DocumentBundle``
and can optionally apply the bounded screening/note policy shared with the
legacy facade. It does not search, write files, or require a pipeline Context.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
import math
from threading import Lock
from copy import deepcopy
from typing import Any, Callable, Literal, Mapping

from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.integrations.llm import LLMError, LLMClient
from simple_ar.integrations.model_profiles import model_connections_compatible
from simple_ar.research.contracts import (
    ClaimCard,
    CodeLink,
    DatasetCard,
    DocumentRecord,
    EvidenceRef,
    MethodCard,
    PaperCard,
    TextChunk,
)
from simple_ar.research.documents.ingest import DocumentBundle, merge_document_reports
from simple_ar.research.evidence.bibliography import front_matter_view
from simple_ar.research.store.chunking import DEFAULT_CHUNK_CHARS
from simple_ar.research.store.retrieval import order_source_chunks, rank_source_chunks, source_chunk_views, source_query_terms
from simple_ar.research.evidence.cards import (
    build_code_links,
    build_dataset_cards,
    build_evidence_cards,
    build_method_cards,
)
from simple_ar.research.evidence.screening import (
    _screening_max_shortlist,
    read_paper_notes_with_llm,
    render_paper_notes_markdown,
    screen_papers_with_llm,
)


ReadStatus = Literal["completed", "partial", "empty"]
READ_OVERVIEW_CHUNKS = 12


def query_evidence(
    bundle: DocumentBundle,
    *,
    document_id: str | None = None,
    chunk_ids: tuple[str, ...] | None = None,
    adjacent_chunks: int = 0,
) -> tuple[EvidenceRef, ...]:
    """Resolve real source chunks into stable evidence references.

    ``chunk_ids=None`` selects all chunks, while ``chunk_ids=()`` explicitly
    selects none.  Unknown IDs and duplicate chunk identities are errors so a
    caller cannot silently turn a dangling citation into a different source.
    Neighboring context is restricted to the same document and follows the
    bundle's persisted order.
    """

    if adjacent_chunks < 0:
        raise ValueError("adjacent_chunks must be non-negative.")
    chunks_by_id: dict[str, Any] = {}
    positions_by_document: dict[str, list[Any]] = {}
    for position, chunk in enumerate(bundle.chunks):
        if chunk.chunk_id in chunks_by_id:
            raise ValueError(f"Duplicate evidence chunk ID: {chunk.chunk_id}")
        entry = (position, chunk)
        chunks_by_id[chunk.chunk_id] = entry
        positions_by_document.setdefault(chunk.document_id, []).append(entry)

    records = {record.document_id: record for record in bundle.records}
    if document_id is not None and document_id not in records:
        raise ValueError(f"Unknown evidence document ID: {document_id}")
    if chunk_ids == ():
        return ()
    selected_ids = (
        tuple(chunk.chunk_id for chunk in bundle.chunks
              if document_id is None or chunk.document_id == document_id)
        if chunk_ids is None
        else tuple(dict.fromkeys(chunk_ids))
    )
    missing = [chunk_id for chunk_id in selected_ids if chunk_id not in chunks_by_id]
    if missing:
        raise ValueError("Unknown evidence chunk ID(s): " + ", ".join(missing))

    refs: list[EvidenceRef] = []
    for chunk_id in selected_ids:
        _, chunk = chunks_by_id[chunk_id]
        if document_id is not None and chunk.document_id != document_id:
            raise ValueError(
                f"Evidence chunk {chunk_id} does not belong to document {document_id}."
            )
        record = records.get(chunk.document_id)
        if record is None:
            raise ValueError(f"Missing document record for evidence chunk {chunk_id}.")
        document_entries = positions_by_document[chunk.document_id]
        local_index = next(index for index, (_, item) in enumerate(document_entries) if item.chunk_id == chunk_id)
        start = max(0, local_index - adjacent_chunks)
        end = min(len(document_entries), local_index + adjacent_chunks + 1)
        context_entries = document_entries[start:end]
        context_ids = tuple(item.chunk_id for _, item in context_entries if item.chunk_id != chunk_id)
        context_text = "\n\n".join(item.text for _, item in context_entries if item.text.strip())
        revision = str(
            record.content_hash
            or record.metadata.get("revision")
            or record.metadata.get("document_revision")
            or ""
        ).strip() or None
        refs.append(
            EvidenceRef(
                evidence_id=chunk.chunk_id,
                document_id=record.document_id,
                chunk_id=chunk.chunk_id,
                source=record.source,
                source_id=record.source_id,
                document_revision=revision,
                source_path=chunk.source_path,
                page=chunk.page,
                line_start=chunk.line_start,
                line_end=chunk.line_end,
                extraction_status=record.extraction_status,
                text=chunk.text,
                adjacent_chunk_ids=context_ids,
                context_text=context_text,
            )
        )
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """Input to the reusable document/evidence reader.

    ``None`` means no filtering. An empty tuple is an explicit empty
    selection, which lets a caller preserve the meaning of an empty shortlist.
    ``document_ids`` and ``paper_ids`` are alternative identifier families;
    when both are supplied, either family may select a record.  Model-assisted
    screening and notes are opt-in; the default remains a pure deterministic
    document-to-card transformation.
    """

    bundle: DocumentBundle
    document_ids: tuple[str, ...] | None = None
    paper_ids: tuple[str, ...] | None = None
    required_document_ids: tuple[str, ...] = ()
    topic: str = ""
    problem_markdown: str = ""
    research_plan_json: str = "{}"
    config: Mapping[str, object] = field(default_factory=dict)
    use_llm: bool = False
    llm_client: Any | None = field(default=None, repr=False, compare=False)
    emit: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    previous: ReadResult | None = None
    source_selection: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.use_llm and self.llm_client is None:
            raise ValueError("ReadRequest.llm_client is required when use_llm is true.")
        object.__setattr__(self, "config", dict(self.config))


@dataclass(frozen=True, slots=True)
class ReadResult:
    """Evidence from selected documents, plus optional metadata-only task gaps.

    question_assessments are search proposals, not paper claims or proof that
    a question was answered. Old read_result.v1 handoffs default to no rows.
    """

    status: ReadStatus
    bundle: DocumentBundle
    paper_cards: tuple[PaperCard, ...] = ()
    claim_cards: tuple[ClaimCard, ...] = ()
    method_cards: tuple[MethodCard, ...] = ()
    dataset_cards: tuple[DatasetCard, ...] = ()
    code_links: tuple[CodeLink, ...] = ()
    screening_decisions: tuple[dict[str, Any], ...] = ()
    paper_notes: tuple[dict[str, Any], ...] = ()
    notes_markdown: str = ""
    diagnostics: tuple[str, ...] = ()
    question_assessments: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a compact summary without copying document text."""
        return {
            "schema_version": "read_result.v1",
            "status": self.status,
            "document_count": len(self.bundle.records),
            "chunk_count": len(self.bundle.chunks),
            "paper_card_count": len(self.paper_cards),
            "claim_card_count": len(self.claim_cards),
            "method_card_count": len(self.method_cards),
            "dataset_card_count": len(self.dataset_cards),
            "code_link_count": len(self.code_links),
            "screening_decision_count": len(self.screening_decisions),
            "paper_note_count": len(self.paper_notes),
            "question_assessment_count": len(self.question_assessments),
            "diagnostics": list(self.diagnostics),
        }

    def to_handoff_dict(self) -> dict[str, object]:
        """Return cards and source locations for an explicit downstream handoff.

        The handoff keeps document identity and evidence locations, but does
        not duplicate the source bundle. Notes may retain bounded query
        excerpts and their locations to explain a source-driven correction.
        """
        return {
            "schema_version": "read_result.v1",
            "status": self.status,
            "documents": [_document_handoff_row(record) for record in self.bundle.records],
            "source_spans": [
                {key: value for key, value in ref.to_row().items()
                 if key not in {"text", "context_text"}}
                for ref in query_evidence(self.bundle)
            ],
            "paper_cards": [card.to_row() for card in self.paper_cards],
            "claim_cards": [card.to_row() for card in self.claim_cards],
            "method_cards": [card.to_row() for card in self.method_cards],
            "dataset_cards": [card.to_row() for card in self.dataset_cards],
            "code_links": [link.to_row() for link in self.code_links],
            "screening_decisions": [dict(row) for row in self.screening_decisions],
            "paper_notes": [dict(row) for row in self.paper_notes],
            "question_assessments": [dict(row) for row in self.question_assessments],
            "notes_markdown": self.notes_markdown,
            "diagnostics": list(self.diagnostics),
        }

    @classmethod
    def from_handoff_dict(
        cls,
        data: Mapping[str, Any],
        *,
        bundle: DocumentBundle,
    ) -> "ReadResult":
        """Restore a persisted read handoff without network or parser calls.

        The source bundle is required explicitly because the read handoff
        keeps chunk text in the document artifact rather than copying it a
        second time. Cards and source locations are restored from the handoff.
        """

        if str(data.get("schema_version") or "") != "read_result.v1":
            raise ValueError("Expected a read_result.v1 object.")
        status = str(data.get("status") or "")
        if status not in {"completed", "partial", "empty"}:
            raise ValueError(f"Unsupported read handoff status: {status!r}")
        result = cls(
            status=status,  # type: ignore[arg-type]
            bundle=bundle,
            paper_cards=_card_rows(data.get("paper_cards"), PaperCard),
            claim_cards=_card_rows(data.get("claim_cards"), ClaimCard),
            method_cards=_card_rows(data.get("method_cards"), MethodCard),
            dataset_cards=_card_rows(data.get("dataset_cards"), DatasetCard),
            code_links=_card_rows(data.get("code_links"), CodeLink),
            screening_decisions=tuple(
                dict(row)
                for row in data.get("screening_decisions", [])
                if isinstance(row, Mapping)
            ),
            paper_notes=tuple(
                dict(row)
                for row in data.get("paper_notes", [])
                if isinstance(row, Mapping)
            ),
            notes_markdown=str(data.get("notes_markdown") or ""),
            question_assessments=tuple(dict(row) for row in data.get("question_assessments", [])
                                       if isinstance(row, Mapping)),
            diagnostics=tuple(
                str(item) for item in data.get("diagnostics", [])
            ),
        )
        return _with_evidence_validation(result)


def _source_requests(result: ReadResult) -> tuple[str, ...]:
    from simple_ar.research.evidence.screening import _new_source_queries
    return tuple(dict.fromkeys(q for note in (*result.question_assessments, *result.paper_notes)
                              for q in _new_source_queries(note.get("new_source_queries", []))))


def new_source_queries(result: ReadResult) -> tuple[str, ...]:
    """Order gap requests fairly before the application applies its source budget.

    Candidate metadata is not an answer. Missing/context-only questions get
    first consideration, but one question's second query must not displace
    every other question's first query. Keep all requests for caller-side
    deduplication against search history and explicit limits; URLs retain the
    separate parent-cited acquisition path.
    """
    from itertools import zip_longest
    from simple_ar.research.evidence.screening import _new_source_queries

    priority = {"missing": 0, "context_only": 1, "direct_candidate": 2}
    questions = sorted(result.question_assessments,
                       key=lambda row: priority.get(row.get("status"), 2))
    groups = [tuple(q for q in _new_source_queries(row.get("new_source_queries", []))
                    if not q.lower().startswith(("https://", "http://")))
              for row in questions]
    ordered = [q for round_queries in zip_longest(*groups)
               for q in round_queries if q is not None]
    ordered.extend(q for note in result.paper_notes
                   for q in _new_source_queries(note.get("new_source_queries", []))
                   if not q.lower().startswith(("https://", "http://")))
    return tuple(dict.fromkeys(ordered))


def linked_material_records(
    result: ReadResult, *, bundle: DocumentBundle, limit: int = 2,
) -> tuple[DocumentRecord, ...]:
    """Select explicit parent-cited links; syntax checks only, no DNS or fetching."""
    from simple_ar.research.preparation_assets import validate_public_url
    from uuid import NAMESPACE_URL, uuid5
    if limit <= 0:
        return ()
    seen = {r.url for r in bundle.records}
    parents = {r.document_id: r for r in bundle.records if r.metadata.get("kind") != "supporting_material"}
    chunks = {c.chunk_id: c for c in bundle.chunks}
    records = []
    requested = tuple(q for q in _source_requests(result) if q.lower().startswith(("https://", "http://")))
    # A reading gap can name a data or documentation link instead of searching
    # for another paper. Only exact, parent-cited URLs are eligible. Legacy
    # notes without URL requests retain repository-first acquisition.
    links = ([link for url in requested for link in result.code_links if link.url == url]
             if requested else sorted(result.code_links, key=lambda link: not bool(link.repository)))
    for link in links:
        parent = next((r for r in parents.values() if link.paper_id in
            (r.document_id, r.metadata.get("paper_id"), r.source_id)), None)
        chunk = next((chunks[c] for c in link.evidence_refs if c in chunks and parent is not None
            and chunks[c].document_id == parent.document_id and link.url in chunks[c].text), None)
        if chunk is None or link.url in seen:
            continue
        try:
            validate_public_url(link.url)
        except ValueError:
            continue
        identifier = uuid5(NAMESPACE_URL, parent.document_id + "\n" + link.url).hex
        records.append(DocumentRecord(document_id=f"linked-{identifier}",
            title=f"Linked material for {parent.title}", source="supporting_material", url=link.url,
            metadata={"kind": "supporting_material", "parent_document_id": parent.document_id,
                      "parent_chunk_id": chunk.chunk_id, "parent_quote": chunk.text,
                      "content_scope": "linked_material_not_paper_or_measured_result"}))
        seen.add(link.url)
        if len(records) >= limit:
            break
    return tuple(records)


def _append_unique(previous, fresh, key):
    rows = {}
    for row in (*previous, *fresh):
        identifier = row[key] if isinstance(row, Mapping) else getattr(row, key)
        if identifier in rows and rows[identifier] != row:
            raise ValueError(f"Conflicting read evidence identity: {key}={identifier!r}")
        rows.setdefault(identifier, row)
    return list(rows.values())


def _merge_fulltext(previous: dict, fresh: dict) -> dict:
    """Merge the existing fulltext manifest/extraction formats, not arbitrary keys."""
    if not previous or not fresh or previous == fresh:
        return dict(previous or fresh)
    if 'batches' in previous or 'batches' in fresh:
        return merge_document_reports(previous, fresh)
    rows = _append_unique(previous.get("documents", []), fresh.get("documents", []), "document_id")
    counts = {"document_count", "hint_count", "selected_count", "fetch_attempt_count",
              "cached_count", "parsed_count", "status_counts"}
    metadata = (previous.keys() | fresh.keys()) - counts - {"documents", "notes"}
    merged = {}
    for key in metadata:
        if key == "budget":
            left, right = previous.get(key, {}), fresh.get(key, {})
            for policy in ("max_pdf_mb", "keep_raw_pdf", "parser_backend", "web_extract_backend"):
                if policy in left and policy in right and left[policy] != right[policy]:
                    raise ValueError(f"Conflicting fulltext policy: {policy}")
            merged[key] = dict(left if key in previous else right)
            continue
        if key == "enabled":
            merged[key] = bool(previous.get(key) or fresh.get(key))
            continue
        if key in previous and key in fresh and previous[key] != fresh[key]:
            raise ValueError(f"Conflicting fulltext metadata: {key}")
        merged[key] = previous[key] if key in previous else fresh[key]
    merged.update(documents=rows, notes=list(dict.fromkeys(
        previous.get("notes", []) + fresh.get("notes", []))))
    if "budget" in merged:
        note = ("Cumulative manifest budget retains the original total limits, not the fresh batch's "
                "remaining allowance; each ingest attempt retains its own effective budget.")
        if note not in merged["notes"]:
            merged["notes"].append(note)
    old_ids = {r["document_id"] for r in previous.get("documents", [])}
    new_ids = {r["document_id"] for r in fresh.get("documents", [])}
    summary = fresh if old_ids <= new_ids else previous if new_ids <= old_ids else None
    for key in counts & (previous.keys() | fresh.keys()):
        if key == "document_count":
            merged[key] = len(rows)
        elif summary is not None:
            if old_ids == new_ids and key in previous and key in fresh and previous[key] != fresh[key]:
                raise ValueError(f"Conflicting fulltext summary: {key}")
            merged[key] = summary.get(key, {} if key == "status_counts" else 0)
        else:
            if old_ids & new_ids:
                raise ValueError("Partially overlapping fulltext counters require reconciliation")
            default = {} if key == "status_counts" else 0
            left, right = previous.get(key, default), fresh.get(key, default)
            merged[key] = ({s: left.get(s, 0) + right.get(s, 0) for s in left.keys() | right.keys()}
                           if key == "status_counts" else left + right)
    return merged


def merge_read_results(previous: ReadResult, fresh: ReadResult) -> ReadResult:
    """Append evidence without mutating inputs, replacing notes or clearing old questions."""
    assessments = (*previous.question_assessments, *fresh.question_assessments)
    if not fresh.bundle.records:
        if not fresh.question_assessments:
            return previous
        return replace(previous, status="partial", question_assessments=assessments,
            diagnostics=tuple(dict.fromkeys((*previous.diagnostics, *fresh.diagnostics))))
    bundle = DocumentBundle(
        records=_append_unique(previous.bundle.records, fresh.bundle.records, "document_id"),
        chunks=_append_unique(previous.bundle.chunks, fresh.bundle.chunks, "chunk_id"),
        sections=_append_unique(previous.bundle.sections, fresh.bundle.sections, "section_id"),
        fulltext_manifest=_merge_fulltext(previous.bundle.fulltext_manifest, fresh.bundle.fulltext_manifest),
        fulltext_extraction=_merge_fulltext(previous.bundle.fulltext_extraction, fresh.bundle.fulltext_extraction),
    )
    values = {name: tuple(_append_unique(getattr(previous, name), getattr(fresh, name), key))
              for name, key in (("paper_cards", "paper_id"), ("claim_cards", "claim_id"),
                  ("method_cards", "method_id"), ("dataset_cards", "dataset_id"),
                  ("code_links", "link_id"), ("paper_notes", "paper_id"))}
    # Selection is a batch-scoped judgment, not an immutable source identity.
    # Preserve distinct judgments; the original attempts retain their provenance.
    decisions = []
    for row in (*previous.screening_decisions, *fresh.screening_decisions):
        if row not in decisions:
            decisions.append(row)
    values["screening_decisions"] = tuple(decisions)
    status: ReadStatus = "empty" if not bundle.records else (
        "partial" if "partial" in (previous.status, fresh.status) else "completed")
    notes = previous.notes_markdown
    if fresh.notes_markdown and fresh.notes_markdown not in notes:
        notes = "\n\n".join(text for text in (notes, fresh.notes_markdown) if text)
    return _with_evidence_validation(ReadResult(status=status, bundle=bundle, **values,
        question_assessments=assessments,
        notes_markdown=notes, diagnostics=tuple(dict.fromkeys((*previous.diagnostics, *fresh.diagnostics)))))


def read_documents(request: ReadRequest) -> ReadResult:
    """Read selected documents into evidence cards and optional model notes.

    The function does not search, write files, or inspect paths.  Without
    ``use_llm`` it performs only deterministic card derivation.  With explicit
    model assistance it applies bounded screening and note generation before
    deriving cards from the selected records.
    """
    bundle = _select_bundle(request)
    materials = [r for r in bundle.records
                 if r.metadata.get("kind") == "supporting_material" or not r.is_original_source]
    material_ids = {r.document_id for r in materials}
    screening_decisions: tuple[dict[str, Any], ...] = ()
    paper_notes: tuple[dict[str, Any], ...] = ()
    notes_markdown = ""
    question_assessments: list[dict[str, Any]] = []
    if request.source_selection is not None:
        screening_decisions = tuple(dict(r) for r in request.source_selection.get("decisions", []))
        question_assessments = [dict(r) for r in request.source_selection.get("question_assessments", [])]
    if request.use_llm and bundle.records:
        client = request.llm_client
        if client is None:
            raise ValueError("ReadRequest.llm_client is required when use_llm is true.")
        screening_mode = _read_screening_mode(request.config)
        fixed_sources = screening_mode == "auto" and {
            record.document_id for record in bundle.records
        } <= set(request.required_document_ids)
        if request.source_selection is None and screening_mode != "deterministic" and any(r.document_id not in material_ids for r in bundle.records):
            decisions = [] if fixed_sources else screen_papers_with_llm(
                client,
                topic=request.topic or "research topic",
                problem_markdown=request.problem_markdown,
                research_plan_json=request.research_plan_json or "{}",
                emit=request.emit,
                papers=[record.to_row() for record in bundle.records if record.document_id not in material_ids],
                config=request.config,
                question_assessments=question_assessments,
            )
            decisions = _preserve_required_screening(
                bundle, decisions, request.required_document_ids, request.config,
            )
            screening_decisions = tuple(decisions)
            paper_bundle = replace(bundle, records=[r for r in bundle.records if r.document_id not in material_ids],
                sections=[s for s in bundle.sections if s.document_id not in material_ids],
                chunks=[c for c in bundle.chunks if c.document_id not in material_ids])
            screened = _bundle_for_screening(paper_bundle, decisions)
            bundle = replace(screened, records=[*screened.records, *materials],
                sections=[*screened.sections, *(s for s in bundle.sections if s.document_id in material_ids)],
                chunks=[*screened.chunks, *(c for c in bundle.chunks if c.document_id in material_ids)])
        readable = [record for record in bundle.records if not record.is_prior_draft
                    and (request.source_selection is None or record.extraction_status != "metadata_only")]
        if readable:
            snippets = {record.document_id: format_bundle_evidence_snippets(
                bundle, document_id=record.document_id,
                focus="\n".join((request.topic, request.problem_markdown)),
            ) for record in readable}
            notes = read_paper_notes_with_llm(
                client,
                papers=[record.to_row() for record in readable],
                evidence_snippets_by_document=snippets,
                front_matter_by_document={section.document_id: front_matter_view(section)
                    for section in bundle.sections if section.section == "front_matter"},
                topic=request.topic,
                problem_markdown=request.problem_markdown + (
                    "\nRecords with source=supporting_material or web are webpages, not papers or verified official sources. "
                    "Read them only for the task questions; preserve their own passage citations and distinguish "
                    "author statements from measured cost or verified runnable code. "
                    "Records with source=local_analysis or table_analysis/code_analysis metadata are derived analysis, "
                    "not literature; preserve their recorded evidence role and verification scope."
                    if materials or any(record.source == "web" for record in readable) else ""),
                emit=request.emit,
                config=request.config,
            )
            # Retain what the model actually saw, not just a document-level
            # citation. Sampling/retrieval is not semantic claim verification.
            focus = "\n".join((request.topic, request.problem_markdown))
            for note in notes:
                available = tuple(chunk for chunk in bundle.chunks
                                  if chunk.document_id == note["paper_id"])
                shown = tuple(chunk for chunk in select_reading_chunks(available, max_chunks=READ_OVERVIEW_CHUNKS, focus=focus)
                              if chunk.text.strip())
                note["reading_coverage"] = {
                    "available_chunks": len(available),
                    "shown_chunk_ids": [chunk.chunk_id for chunk in shown],
                    "excerpt_chars": DEFAULT_CHUNK_CHARS,
                    "shortened_chunk_ids": [chunk.chunk_id for chunk in shown
                                            if len(" ".join(chunk.text.split())) > DEFAULT_CHUNK_CHARS],
                    "selection": "overview_and_lexical_focus" if focus.strip() else "overview",
                    "semantic_verification": "not_performed",
                }
            paper_notes = tuple(_refine_reading_gaps(request, bundle, notes, snippets))
            notes_markdown = render_paper_notes_markdown(paper_notes)
    if not bundle.records:
        return ReadResult(
            status="partial" if question_assessments else "empty",
            bundle=bundle,
            screening_decisions=screening_decisions,
            paper_notes=paper_notes,
            notes_markdown=notes_markdown,
            question_assessments=tuple(question_assessments),
            diagnostics=("No documents matched the requested read selection.",),
        )

    papers = [r for r in bundle.records if r.document_id not in material_ids]
    paper_cards, claim_cards = build_evidence_cards(
        documents=papers,
        chunks=bundle.chunks,
    )
    method_cards = build_method_cards(documents=papers, chunks=bundle.chunks)
    dataset_cards = build_dataset_cards(documents=papers, chunks=bundle.chunks)
    code_links = build_code_links(documents=papers, chunks=bundle.chunks)
    diagnostics: list[str] = []
    status: ReadStatus = "completed"
    if request.source_selection is not None and any(r.extraction_status == "metadata_only" for r in bundle.records):
        status = "partial"
        diagnostics.append("Selected sources remain metadata-only; discovery assessments are not original-text reading.")
    if any(row["status"] != "direct_candidate" for row in question_assessments):
        status = "partial"
        diagnostics.append("Required question evidence remains missing or context-only in the candidate assessment.")
    pending = [note["paper_id"] for note in paper_notes
               if note.get("reading_followup", {}).get("pending_queries", note.get("followup_queries"))]
    if pending:
        status = "partial"
        diagnostics.append(f"Source questions remain after one bounded reading followup: {', '.join(pending[:5])}.")
    failed = [note["paper_id"] for note in paper_notes
              if note.get("reading_followup", {}).get("revision_status") == "failed"]
    if failed:
        diagnostics.append(f"Optional source reread failed; prior notes retained without resolving gaps: {', '.join(failed)}.")
    if not bundle.chunks:
        status = "partial"
        diagnostics.append(
            "No text chunks were available; cards may rely on metadata abstracts."
        )
    else:
        covered = {chunk.document_id for chunk in bundle.chunks}
        missing = [record.document_id for record in bundle.records if record.document_id not in covered]
        if missing:
            status = "partial"
            diagnostics.append(
                f"No text chunks for {len(missing)} selected document(s): "
                + ", ".join(missing[:5])
                + (" (and more)" if len(missing) > 5 else "")
                + ". Claims about these documents require further source access."
            )
    return _with_evidence_validation(ReadResult(
        status=status,
        bundle=bundle,
        paper_cards=tuple(paper_cards),
        claim_cards=tuple(claim_cards),
        method_cards=tuple(method_cards),
        dataset_cards=tuple(dataset_cards),
        code_links=tuple(code_links),
        screening_decisions=screening_decisions,
        paper_notes=paper_notes,
        notes_markdown=notes_markdown,
        question_assessments=tuple(question_assessments),
        diagnostics=tuple(diagnostics),
    ))


class _ReadProgressClient:
    """Keep completed model reads in this attempt; failures still propagate.

    This is a read-stage checkpoint, not a shared response cache. Exact prompts,
    registered inputs, action and connection bind reuse to an explicit retry.
    Each worker persists its result before the batch can fail.
    """

    def __init__(self, client: LLMClient, context: CapabilityContext):
        self.client, self.context = client, context
        self.lock = Lock()
        self.binding = client.connection_binding()
        self.rows: list[dict[str, Any]] = []
        self.inputs = [ref.to_dict() for ref in context.inputs]
        root = (context.input_store or context.store).root
        # Inspect only read checkpoints in this session, never project files.
        paths = sorted((root / "attempts").glob("*/read_progress.json"))
        current = context.store.root / "read_progress.json"
        if current.is_file() and current not in paths:
            paths.append(current)
        for path in paths:
            saved = (context.input_store or context.store).read_json(path)
            if (saved.get("schema_version") == "read_progress.v1"
                    and saved.get("inputs") == self.inputs
                    and saved.get("trigger") == context.attempt.trigger
                    and saved.get("model") == client.model
                    and model_connections_compatible(saved.get("connection", {}), self.binding)):
                self.rows.extend(saved.get("completed", []))
        # Later checkpoints already include earlier completions; do not grow
        # the journal by copying those rows again on each recovery.
        self.rows = list({tuple(row["prompt"][key] for key in ("system", "user", "label")): row
                          for row in self.rows}.values())

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, Any]:
        prompt = {"system": system, "user": user, "label": label}
        with self.lock:
            for row in reversed(self.rows):
                if row["prompt"] == prompt:
                    return deepcopy(row["response"])
        response = self.client.ask_json(system, user, label=label)
        with self.lock:
            self.rows.append({"prompt": prompt, "response": deepcopy(response)})
            self.context.store.write_json("read_progress.json", {
                "schema_version": "read_progress.v1", "inputs": self.inputs,
                "trigger": self.context.attempt.trigger, "model": self.client.model,
                "connection": self.binding, "completed": self.rows,
            }, kind="read_progress", schema="read_progress.v1", producer="research.read")
        return response

    def ask_json_many(self, requests, *, max_workers=4):
        # Reuse the existing bounded scheduler, cancellation and ordering.
        return LLMClient._run_many(self, requests,
            lambda request: self.ask_json(request.system, request.user, label=request.label),
            max_workers=max_workers)


def run_read_capability(
    *,
    context: CapabilityContext,
    request: ReadRequest,
) -> CapabilityResult:
    """Persist one read handoff through the session boundary.

    Reading remains a transformation over a caller-provided bundle.  The
    adapter owns only the attempt-local artifact and status mapping; it does
    not fetch documents or silently expand the selection.  Model assistance is
    still explicit on ``ReadRequest``.
    """
    # Named connection snapshots contain no secret values. Legacy/duck-typed
    # clients keep their existing behavior rather than an unbound checkpoint.
    if (request.use_llm and isinstance(request.llm_client, LLMClient)
            and request.llm_client.connection_binding()):
        request = replace(request, llm_client=_ReadProgressClient(request.llm_client, context))
    result = read_documents(request)
    bundle_output = ()
    if request.previous is not None:
        result = merge_read_results(request.previous, result)
        bundle_output = (context.store.write_json("document_bundle.json", result.bundle.to_handoff_dict(),
            kind="document_bundle", schema="document_bundle.v1", producer="research.read"),)
    output = context.store.write_json(
        "read_result.json",
        result.to_handoff_dict(),
        kind="read_result",
        schema="read_result.v1",
        producer="research.read",
    )
    status = {
        "completed": "completed",
        "partial": "partial",
        "empty": "blocked",
    }[result.status]
    return CapabilityResult(
        status=status,  # type: ignore[arg-type]
        artifacts=(output, *bundle_output),
        diagnostics=result.diagnostics,
        usage={
            "documents": len(result.bundle.records),
            "chunks": len(result.bundle.chunks),
            "paper_cards": len(result.paper_cards),
            "screening_decisions": len(result.screening_decisions),
            "paper_notes": len(result.paper_notes),
        },
        provenance={
            "capability": "read",
            "result_schema": "read_result.v1",
            "mode": "llm" if request.use_llm else "deterministic",
            "model": str(getattr(request.llm_client, "model", ""))
            if request.use_llm
            else "",
        },
    )


def _select_bundle(request: ReadRequest) -> DocumentBundle:
    if request.document_ids is None and request.paper_ids is None:
        return request.bundle

    document_ids = set(request.document_ids or ())
    paper_ids = set(request.paper_ids or ())
    records = [
        record
        for record in request.bundle.records
        if _record_matches(record, document_ids=document_ids, paper_ids=paper_ids)
    ]
    selected_document_ids = {record.document_id for record in records}
    return DocumentBundle(
        records=records,
        fulltext_manifest=request.bundle.fulltext_manifest,
        fulltext_extraction=request.bundle.fulltext_extraction,
        sections=[
            section
            for section in request.bundle.sections
            if section.document_id in selected_document_ids
        ],
        chunks=[
            chunk
            for chunk in request.bundle.chunks
            if chunk.document_id in selected_document_ids
        ],
    )


def _record_matches(
    record: DocumentRecord,
    *,
    document_ids: set[str],
    paper_ids: set[str],
) -> bool:
    if record.document_id in document_ids:
        return True
    paper_identifiers = {record.document_id, record.source_id or ""}
    metadata_paper_id = record.metadata.get("paper_id")
    if metadata_paper_id:
        paper_identifiers.add(str(metadata_paper_id))
    return bool({item for item in paper_identifiers if item} & paper_ids)


def _read_screening_mode(config: Mapping[str, object]) -> str:
    value = str(
        config.get("read_screening")
        or config.get("research_read_screening")
        or "auto"
    ).strip().lower()
    return value if value in {"auto", "llm", "deterministic"} else "auto"


def _bundle_for_screening(
    bundle: DocumentBundle,
    decisions: list[dict[str, Any]],
) -> DocumentBundle:
    """Keep model-selected records in priority order without copying text."""

    if not decisions:
        return bundle
    record_by_identifier: dict[str, DocumentRecord] = {}
    for record in bundle.records:
        identifiers = {
            record.document_id,
            record.source_id or "",
            str(record.metadata.get("paper_id") or ""),
        }
        for identifier in identifiers:
            if identifier:
                record_by_identifier.setdefault(identifier, record)
    selected_records: list[DocumentRecord] = []
    selected_ids: set[str] = set()
    kept = [
        row
        for row in decisions
        if str(row.get("decision") or "keep").strip().lower() == "keep"
    ]
    kept.sort(
        key=lambda row: (
            _optional_int(row.get("reading_priority")) or 9999,
            str(row.get("paper_id") or ""),
        )
    )
    for row in kept:
        identifier = str(row.get("paper_id") or "").strip()
        record = record_by_identifier.get(identifier)
        if record is None or record.document_id in selected_ids:
            continue
        selected_records.append(record)
        selected_ids.add(record.document_id)
    return DocumentBundle(
        records=selected_records,
        fulltext_manifest=bundle.fulltext_manifest,
        fulltext_extraction=bundle.fulltext_extraction,
        sections=[
            section for section in bundle.sections if section.document_id in selected_ids
        ],
        chunks=[
            chunk for chunk in bundle.chunks if chunk.document_id in selected_ids
        ],
    )


def _preserve_required_screening(
    bundle: DocumentBundle,
    decisions: list[dict[str, Any]],
    required_document_ids: tuple[str, ...],
    config: Mapping[str, object],
) -> list[dict[str, Any]]:
    """Keep explicitly supplied documents without silently widening the shortlist."""

    required = tuple(dict.fromkeys(required_document_ids))
    if not required:
        return decisions
    records = {record.document_id: record for record in bundle.records}
    missing = set(required) - records.keys()
    if missing:
        raise ValueError("Required read document(s) are absent: " + ", ".join(sorted(missing)))
    limit = _screening_max_shortlist(config, len(bundle.records))
    if len(required) > limit:
        raise ValueError(
            f"{len(required)} supplied documents exceed the read shortlist limit {limit}; "
            "increase research.read_max_shortlist or narrow the supplied documents."
        )

    output = [dict(row) for row in decisions]
    by_id = {str(row.get("paper_id") or ""): row for row in output}
    for index, document_id in enumerate(required):
        record = records[document_id]
        identifiers = (document_id, record.source_id or "", str(record.metadata.get("paper_id") or ""))
        row = next((by_id[identifier] for identifier in identifiers if identifier in by_id), None)
        if row is None:
            row = {"paper_id": document_id}
            output.append(row)
        row.update({
            "decision": "keep",
            "reading_priority": index - len(required),
            "reason": "Explicitly supplied document; retained for evidence-grounded reading.",
        })

    required_set = set(required)
    selected = [row for row in output if str(row.get("decision") or "keep").lower() == "keep"]
    selected.sort(key=lambda row: (
        _optional_int(row.get("reading_priority")) or 9999,
        str(row.get("paper_id") or ""),
    ))
    for row in selected[limit:]:
        identifier = str(row.get("paper_id") or "")
        if identifier in required_set:
            continue
        row["decision"] = "drop"
        row["reason"] = "Displaced by explicitly supplied document within read shortlist limit."
    return output


def _refine_reading_gaps(
    request: ReadRequest, bundle: DocumentBundle,
    notes: list[dict[str, Any]], snippets: Mapping[str, str],
) -> list[dict[str, Any]]:
    """One source-scoped query round, then revise notes only with new text.

    The saved bundle remains the source owner. Query excerpts are bounded
    provenance for this correction, not another index or a support verdict.
    Initial reading errors still fail the Read attempt. Optional model reread
    errors retain prior notes as partial, with unresolved gaps and no retry.
    """
    records = {record.document_id: record for record in bundle.records}
    output = []
    for note_index, note in enumerate(notes):
        queries = list(dict.fromkeys(note.get("followup_queries", [])))[:2]
        if not queries:
            output.append(note)
            continue
        # Overview sampling prioritizes the body; an explicit source question
        # may instead need a cited method's identity or an appendix condition.
        chunks = order_source_chunks([chunk for chunk in bundle.chunks if chunk.document_id == note["paper_id"]])
        shown_ids = set(note["reading_coverage"]["shown_chunk_ids"])
        initial_text = {chunk.chunk_id: " ".join(chunk.text.split()) for chunk in chunks}
        initial_text = {key: text[:DEFAULT_CHUNK_CHARS - 3] if len(text) > DEFAULT_CHUNK_CHARS else text
                        for key, text in initial_text.items()}
        lookups = []
        candidates = []
        for query in queries:
            hits = rank_source_chunks(chunks, query, limit=2)
            # Keep independent appearances before neighbors. A caption and
            # its prose reference can be far apart after PDF extraction.
            positions = [chunks.index(hit) for hit in hits]
            indices = list(dict.fromkeys([*positions, *[
                neighbor for index in positions for neighbor in (index + 1, index - 1)
                if 0 <= neighbor < len(chunks)
            ]]))
            selected = [chunks[index] for index in indices]
            views = source_chunk_views(selected, query=query, max_chars=6 * DEFAULT_CHUNK_CHARS,
                                       max_chunk_chars=DEFAULT_CHUNK_CHARS)
            new_views = [view for view in views if view["chunk_id"] not in shown_ids or
                         " ".join(view["text"].split()) not in
                         initial_text[view["chunk_id"]]]
            candidates.append(new_views)
            lookups.append({"query": query, "status": "matches" if hits else "no_lexical_match",
                            "matched_chunk_ids": [chunk.chunk_id for chunk in hits],
                            "semantic_verification": "not_performed"})
        # Fair sharing between questions without adding another retrieval or
        # model round. Duplicates use no additional window budget.
        passages = {}
        for position in range(max((len(rows) for rows in candidates), default=0)):
            for rows in candidates:
                if position < len(rows) and len(passages) < 6:
                    view = rows[position]
                    passages.setdefault((view["chunk_id"], view["character_start"], view["character_end"]), view)
        for lookup, rows in zip(lookups, candidates):
            included = sum((row["chunk_id"], row["character_start"], row["character_end"]) in passages for row in rows)
            lookup.update(new_window_count=included, omitted_window_count=len(rows) - included)
        trace = {"lookups": lookups, "passages": list(passages.values()),
                 "revision_performed": False, "pending_queries": queries,
                 "scope": "retained_text_only_no_absence_or_support_certification"}
        if passages:
            _emit = request.emit
            if _emit is not None:
                _emit(f"Rereading source gaps for {note['paper_id']} (one bounded round).")
            try:
                revised = read_paper_notes_with_llm(request.llm_client,
                    papers=[records[note["paper_id"]].to_row()], evidence_snippets_by_document=snippets,
                    front_matter_by_document={section.document_id: front_matter_view(section)
                        for section in bundle.sections if section.section == "front_matter"},
                    revision_context_by_document={note["paper_id"]: {"previous_note": note, "source_lookup": trace}},
                    topic=request.topic, problem_markdown=request.problem_markdown,
                    config=request.config, emit=request.emit)[0]
            except LLMError:
                # Initial notes already succeeded. An optional reread must not
                # discard them or trigger more calls after unknown usage/budget
                # failure. Retain unresolved questions and the observed windows;
                # the shared ledger still owns the failed request and its usage.
                trace["revision_status"] = "failed"
                note["reading_followup"] = trace
                if _emit is not None:
                    _emit(f"Source reread failed for {note['paper_id']}; keeping prior notes and stopping optional rereads.")
                output.extend(notes[note_index:])
                return output
            trace.update(revision_performed=True, pending_queries=revised.get("followup_queries", []),
                         prior_note=note)
            revised["reading_coverage"] = {**note["reading_coverage"],
                "followup_shown_chunk_ids": list(dict.fromkeys(view["chunk_id"] for view in trace["passages"]))}
            if not revised.get("bibliographic_fields"):
                revised["bibliographic_fields"] = note.get("bibliographic_fields", [])
            note = revised
        note["reading_followup"] = trace
        output.append(note)
    return output


def reading_followup_context(value: Any, *, include_passages: bool = False) -> dict[str, Any]:
    """Current lookup evidence, not superseded notes from the saved trace.

    The full trace remains in the Read artifact. Downstream synthesis/writing
    must consume the adopted note, not implicitly adopt its historical drafts.
    Preserve unresolved queries and, when requested, the original passages.
    """
    keys = ("lookups", "pending_queries", "revision_performed", "revision_status", "scope")
    if include_passages:
        keys += ("passages",)
    return {key: value[key] for key in keys if key in value} if isinstance(value, Mapping) else {}


def _optional_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _reading_source_order(chunks: tuple[TextChunk, ...] | list[TextChunk]) -> list[TextChunk]:
    """Use comparable retained positions, not ingest priority, within a source."""
    documents: dict[str, list[TextChunk]] = {}
    for chunk in chunks:
        documents.setdefault(chunk.document_id, []).append(chunk)
    return [chunk for rows in documents.values() for chunk in order_source_chunks(rows)]


def select_representative_chunks(
    chunks: tuple[TextChunk, ...] | list[TextChunk], *, max_chunks: int,
    required_chunk_ids: tuple[str, ...] = (),
) -> tuple[TextChunk, ...]:
    """Bound a reading overview while covering documents and observed sections.

    This is a sampling policy, not a claim that the selected text represents
    unseen parts of a paper. Bibliography chunks are excluded when the same
    document has substantive text. Section coverage includes retained boundaries
    before filling interiors; task pins count toward that coverage and the cap.
    """
    if max_chunks < 1:
        return ()
    chunks = _reading_source_order(chunks)
    required = set(required_chunk_ids)
    pinned = [chunk for chunk in chunks
              if chunk.chunk_id in required and chunk.metadata.get("section") != "references"][:max_chunks]
    pinned_ids = {chunk.chunk_id for chunk in pinned}
    documents: dict[str, list[TextChunk]] = {}
    for chunk in chunks:
        documents.setdefault(chunk.document_id, []).append(chunk)
    candidates = {
        document_id: [row for row in rows if row.metadata.get("section") != "references"] or rows
        for document_id, rows in documents.items()
    }
    quotas = {document_id: sum(row.chunk_id in pinned_ids for row in rows)
              for document_id, rows in candidates.items()}
    remaining = max_chunks - len(pinned)
    while remaining:
        available = [document_id for document_id, rows in candidates.items()
                     if quotas[document_id] < len(rows)]
        if not available:
            break
        document_id = min(available, key=quotas.get)
        quotas[document_id] += 1
        remaining -= 1

    selected_ids: set[str] = set(pinned_ids)
    for document_id, rows in candidates.items():
        quota = quotas[document_id]
        if not quota:
            continue
        sections: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            section_id = str(row.metadata.get("section_id") or document_id)
            sections.setdefault(section_id, []).append(index)
        chosen = {index for index, row in enumerate(rows) if row.chunk_id in pinned_ids}
        anchors = [positions[0] for positions in sections.values()
                   if not any(index in chosen for index in positions)]
        if len(anchors) > quota - len(chosen):
            # Section type is useful for an overview, but never substitutes
            # for the actual text. Reserve a few distinct kinds before
            # spreading the remaining windows across the document.
            by_kind: dict[str, list[int]] = {}
            for index in anchors:
                by_kind.setdefault(str(rows[index].metadata.get("section") or "body"), []).append(index)
            represented_kinds = {str(rows[index].metadata.get("section") or "body") for index in chosen}
            for kind in ("abstract", "method", "experiments", "results", "limitations",
                         "discussion", "conclusion", "related_work", "introduction"):
                if len(chosen) == quota:
                    break
                options = by_kind.get(kind, [])
                if options and kind not in represented_kinds:
                    chosen.add(options[len(options) // 2])
        else:
            chosen.update(anchors)
        while len(chosen) < quota:
            unseen_anchors = [index for index in anchors if index not in chosen]
            if unseen_anchors:
                pool = unseen_anchors
                covered = chosen
            else:
                # Global spacing overweights long neighboring sections and
                # ignores definitions/qualifications at section boundaries.
                # Share extra windows across sections, counting task pins;
                # spread within each section instead of across the whole PDF.
                positions = min(
                    (positions for positions in sections.values() if any(index not in chosen for index in positions)),
                    key=lambda positions: sum(index in chosen for index in positions),
                )
                pool = positions
                covered = chosen.intersection(positions)
            next_index = max(
                (index for index in pool if index not in chosen),
                key=lambda index: (min(abs(index - prior) for prior in covered) if covered else 0, -index),
            )
            chosen.add(next_index)
        selected_ids.update(rows[index].chunk_id for index in chosen)
    return tuple(chunk for chunk in chunks if chunk.chunk_id in selected_ids)


def select_reading_chunks(
    chunks: tuple[TextChunk, ...] | list[TextChunk], *, max_chunks: int,
    focus: str = "",
) -> tuple[TextChunk, ...]:
    """Reserve a bounded part of an overview for task-relevant passages.

    Search all retained substantive chunks before sampling. Rare overlapping
    terms rank passages; neighboring retained same-source context shares the reserved
    quota. The remaining budget still covers sections and late text. This is
    lexical retrieval, not a relevance verdict, translation or claim audit.
    """
    chunks = _reading_source_order(chunks)
    terms = source_query_terms(focus)
    if not terms or max_chunks < 3 or len(chunks) <= max_chunks:
        return select_representative_chunks(chunks, max_chunks=max_chunks)
    candidates = [chunk for chunk in chunks if chunk.metadata.get("section") != "references"]
    overlaps = [source_query_terms(chunk.text) & terms for chunk in candidates]
    frequencies = {term: sum(term in match for match in overlaps) for term in terms}
    weights = {term: math.log((len(candidates) + 1) / (count + 1))
               for term, count in frequencies.items() if 0 < count < len(candidates)}
    ranked = sorted(range(len(candidates)),
                    key=lambda index: (-math.fsum(weights.get(term, 0) for term in sorted(overlaps[index])), index))
    pinned: list[str] = []
    quota = max_chunks // 3
    for index in ranked:
        if len(pinned) >= quota:
            break
        if not any(weights.get(term, 0) > 0 for term in overlaps[index]):
            break
        # Retained source order, not body-first storage priority. Positions can
        # have gaps; neighboring retained chunks do not certify continuous text.
        # Keep the hit before neighbors so a tiny budget retains it.
        for position in (index, index - 1, index + 1):
            if len(pinned) >= quota:
                break
            if 0 <= position < len(candidates):
                chunk = candidates[position]
                if (chunk.document_id == candidates[index].document_id
                        and chunk.source_path == candidates[index].source_path
                        and chunk.chunk_id not in pinned):
                    pinned.append(chunk.chunk_id)
    return select_representative_chunks(chunks, max_chunks=max_chunks,
                                         required_chunk_ids=tuple(pinned))


def format_bundle_evidence_snippets(
    bundle: DocumentBundle,
    *,
    max_chunks: int = READ_OVERVIEW_CHUNKS,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    document_id: str | None = None,
    focus: str = "",
) -> str:
    """Render bounded, source-labelled text for model reading prompts."""

    if max_chars < 4:
        raise ValueError("Evidence excerpt max_chars must be at least 4.")
    lines: list[str] = []
    shortened = 0
    available = tuple(
        chunk for chunk in bundle.chunks
        if document_id is None or chunk.document_id == document_id
    )
    selected = select_reading_chunks(available, max_chunks=max_chunks, focus=focus)
    for chunk in selected:
        text = " ".join(chunk.text.split())
        if len(text) > max_chars:
            shortened += 1
            text = text[: max_chars - 3].rstrip() + "..."
        if not text:
            continue
        location = chunk.source_path or chunk.document_id
        if chunk.line_start is not None:
            location += f":{chunk.line_start}"
            if chunk.line_end is not None and chunk.line_end != chunk.line_start:
                location += f"-{chunk.line_end}"
        heading = str(chunk.metadata.get("heading") or chunk.metadata.get("section") or "").strip()
        label = f" ({heading})" if heading else ""
        lines.append(f"[{chunk.chunk_id}] {location}{label}: {text}")
    if len(selected) < len(available):
        lines.append(
            f"[coverage] Bounded overview sampled {len(selected)} of {len(available)} chunks; "
            "unseen details require a targeted source read."
        )
        if focus.strip():
            lines.append("[coverage] Selection combines section overview and lexical task matches; "
                         "it does not establish complete coverage or absence of conflicting evidence.")
    if shortened:
        lines.append(f"[coverage] {shortened} chunk excerpt(s) shortened to {max_chars} characters; "
                     "a missing detail here is not evidence of absence from the retained source.")
    return "\n".join(lines)


def _document_handoff_row(record: DocumentRecord) -> dict[str, object]:
    """Keep document identity and provenance without duplicating full text."""
    return {
        "document_id": record.document_id,
        "source_id": record.source_id,
        "title": record.title,
        "source": record.source,
        "url": record.url,
        "doi": record.doi,
        "published": record.published,
        "extraction_status": record.extraction_status,
        "parser": record.parser,
        "metadata": {key: record.metadata[key] for key in ("kind", "evidence_role") if key in record.metadata},
    }


def validate_read_evidence(result: ReadResult) -> tuple[str, ...]:
    """Return diagnostics for card references absent from the source bundle.

    Cards deliberately point to chunk IDs rather than copying source text. A
    persisted handoff is only trustworthy when those references still resolve
    against the bundle supplied by the caller. The check is intentionally
    narrow: it validates declared references and does not scan files or infer
    semantic correctness from prose.
    """

    chunks = {chunk.chunk_id: chunk.document_id for chunk in result.bundle.chunks}
    records = {record.document_id: record for record in result.bundle.records}
    aliases = {}
    for record in result.bundle.records:
        for alias in (record.document_id, record.source_id, record.metadata.get("paper_id")):
            if alias:
                aliases.setdefault(str(alias), set()).add(record.document_id)
    references: list[str] = []
    for cards in (
        result.paper_cards,
        result.claim_cards,
        result.method_cards,
        result.dataset_cards,
        result.code_links,
    ):
        for card in cards:
            for reference in card.evidence_refs:
                reference = str(reference).strip()
                if reference:
                    references.append(reference)
    missing = sorted({reference for reference in references if reference not in chunks})
    diagnostics = []
    if missing:
        diagnostics.append(
            f"{len(missing)} read evidence reference(s) do not resolve to the document bundle: "
            + ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else "")
        )
    # Notes are model interpretations too. A reference to a real chunk in a
    # different paper is not valid support for this paper; document-level refs
    # remain allowed for historical notes, but are not passage-level evidence.
    for note in result.paper_notes:
        owner = str(note.get("paper_id") or "")
        if owner not in records:
            diagnostics.append(f"Reading note has an unknown document identity: {owner!r}.")
            continue
        invalid = [str(ref) for ref in note.get("evidence_refs", [])
                   if chunks.get(str(ref)) != owner and aliases.get(str(ref)) != {owner}]
        if invalid:
            diagnostics.append(f"Reading note {owner!r} has unresolved or cross-document references: "
                               + ", ".join(invalid[:8]))
        scopes = note.get("claim_scopes", [])
        if not isinstance(scopes, list):
            diagnostics.append(f"Reading note {owner!r} has malformed scoped claims; support is unverified.")
            continue
        for claim in scopes:
            if not isinstance(claim, Mapping):
                diagnostics.append(f"Reading note {owner!r} has a malformed scoped claim; support is unverified.")
                continue
            refs = claim.get("evidence_refs", [])
            if not isinstance(refs, list) or not refs or any(chunks.get(str(ref)) != owner for ref in refs):
                diagnostics.append(f"Scoped reading claim {claim.get('claim_id', owner)!r} lacks "
                                   "same-document passage references; support is unverified.")
    return tuple(diagnostics)


def _with_evidence_validation(result: ReadResult) -> ReadResult:
    from simple_ar.research.documents.extractors import document_extraction_limitations
    diagnostics = (*validate_read_evidence(result), *document_extraction_limitations(result.bundle.records))
    if not diagnostics:
        return result
    status = "partial" if result.status == "completed" else result.status
    return replace(
        result,
        status=status,  # type: ignore[arg-type]
        diagnostics=tuple(dict.fromkeys((*result.diagnostics, *diagnostics))),
    )


def _card_rows(value: object, card_type: type[Any]) -> tuple[Any, ...]:
    if not isinstance(value, list):
        return ()
    allowed = {field.name for field in fields(card_type)}
    return tuple(
        card_type(**{key: row[key] for key in allowed if key in row})
        for row in value
        if isinstance(row, Mapping)
    )


__all__ = [
    "ReadRequest",
    "ReadResult",
    "ReadStatus",
    "new_source_queries",
    "merge_read_results",
    "format_bundle_evidence_snippets",
    "reading_followup_context",
    "select_representative_chunks",
    "read_documents",
    "run_read_capability",
    "validate_read_evidence",
]
