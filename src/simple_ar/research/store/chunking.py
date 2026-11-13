from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

from simple_ar.research.contracts import DocumentRecord, DocumentSection, TextChunk


DEFAULT_CHUNK_CHARS = 1400
DEFAULT_OVERLAP_CHARS = 180


def build_text_chunks(
    records: Iterable[DocumentRecord],
    *,
    sections: Iterable[DocumentSection] | None = None,
    max_chunks: int | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> list[TextChunk]:
    """Build bounded text chunks from document records.

    Args:
        records: Document records produced by the search/document-store stage.
        sections: Optional section-aware text spans. When provided, chunks are
            built from these spans and carry section metadata.
        max_chunks: Optional global cap. Keep this small for early V2.3 runs.
        chunk_chars: Target maximum characters per chunk.
        overlap_chars: Character overlap between adjacent long chunks.

    Returns:
        Ordered chunks with stable ids and source provenance. Metadata-only
        records contribute abstract chunks; parsed local text records contribute
        local-file text when available.
    """
    if max_chunks is not None and max_chunks < 1:
        raise ValueError("max_chunks must be positive when provided.")
    if sections is not None:
        return _chunks_from_sections(
            sections,
            max_chunks=max_chunks,
            chunk_chars=chunk_chars,
            overlap_chars=overlap_chars,
        )

    chunks: list[TextChunk] = []
    for record in records:
        text, source_path = _record_text(record)
        if not text.strip():
            continue
        for index, span in enumerate(_split_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars), start=1):
            if max_chunks is not None and index > max_chunks:
                break
            chunks.append(
                TextChunk(
                    chunk_id=f"{record.document_id}#chunk-{index:03d}",
                    document_id=record.document_id,
                    text=span,
                    source_path=source_path,
                    line_start=_line_start(text, span),
                    line_end=None,
                    token_estimate=max(1, len(span) // 4),
                    metadata={
                        "title": record.title,
                        "source": record.source,
                        "extraction_status": record.extraction_status,
                        "parser": record.parser or "",
                    },
                )
            )
    return bounded_document_coverage(chunks, max_chunks)


def _chunks_from_sections(
    sections: Iterable[DocumentSection],
    *,
    max_chunks: int | None,
    chunk_chars: int,
    overlap_chars: int,
) -> list[TextChunk]:
    # With a physical cap, title/byline pages must not crowd out all of a
    # document's actual evidence. Keep their source spans but budget body first.
    if max_chunks is not None:
        sections = sorted(sections, key=lambda row: row.section == "front_matter")
    chunks: list[TextChunk] = []
    per_document: dict[str, int] = {}
    for section in sections:
        text = section.text
        if not text.strip():
            continue
        if max_chunks is not None and per_document.get(section.document_id, 0) >= max_chunks:
            continue
        for index, span in enumerate(_split_text(text, chunk_chars=chunk_chars, overlap_chars=overlap_chars), start=1):
            if max_chunks is not None and per_document.get(section.document_id, 0) >= max_chunks:
                break
            chunks.append(
                TextChunk(
                    chunk_id=f"{section.section_id}#chunk-{index:03d}",
                    document_id=section.document_id,
                    text=span,
                    source_path=section.source_path,
                    line_start=_section_line_start(section, text, span),
                    line_end=None,
                    token_estimate=max(1, len(span) // 4),
                    metadata={
                        **section.metadata,
                        "section_id": section.section_id,
                        "section": section.section,
                        "heading": section.heading,
                    },
                )
            )
            per_document[section.document_id] = per_document.get(section.document_id, 0) + 1
    return bounded_document_coverage(chunks, max_chunks)


def bounded_document_coverage(chunks: list[TextChunk], max_chunks: int | None) -> list[TextChunk]:
    """Keep source order when possible; otherwise share the cap across documents.

    A long first PDF must not silently consume the entire reading budget while
    later selected documents receive no source text at all. Within each
    document, chunk order and source IDs remain unchanged from construction;
    bounded section construction budgets body before front matter.
    """
    if max_chunks is None or len(chunks) <= max_chunks:
        return chunks
    by_document: dict[str, list[TextChunk]] = {}
    for chunk in chunks:
        by_document.setdefault(chunk.document_id, []).append(chunk)
    selected: list[TextChunk] = []
    offset = 0
    while len(selected) < max_chunks:
        advanced = False
        for document_chunks in by_document.values():
            if len(document_chunks) > offset:
                selected.append(document_chunks[offset])
                advanced = True
                if len(selected) == max_chunks:
                    break
        if not advanced:
            break
        offset += 1
    return selected


def _record_text(record: DocumentRecord) -> tuple[str, str | None]:
    if record.extraction_status == "parsed" and record.local_path:
        path = Path(record.local_path)
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
            return _read_text(path), str(path)
    return record.abstract or "", record.local_path or record.url


def _split_text(text: str, *, chunk_chars: int, overlap_chars: int) -> Iterator[str]:
    compact = text.strip()
    if not compact:
        return
    if len(compact) <= chunk_chars:
        yield compact
        return
    start = 0
    step = max(1, chunk_chars - max(0, overlap_chars))
    while start < len(compact):
        end = min(len(compact), start + chunk_chars)
        chunk = compact[start:end].strip()
        if chunk:
            yield chunk
        if end >= len(compact):
            break
        start += step


def _line_start(text: str, span: str) -> int | None:
    if not span:
        return None
    index = text.find(span[: min(len(span), 40)])
    if index < 0:
        return None
    return text[:index].count("\n") + 1


def _section_line_start(section: DocumentSection, text: str, span: str) -> int | None:
    local = _line_start(text, span)
    if local is None:
        return section.line_start
    if section.line_start is None:
        return local
    return section.line_start + local - 1


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
