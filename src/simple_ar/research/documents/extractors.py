from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from simple_ar.research.contracts import DocumentRecord, SourcePlan
from simple_ar.research.documents.ports import (
    DocumentParser,
    DocumentResolver,
    LocalDocumentResolver,
    ParsedDocument,
    MATERIAL_TEXT_SUFFIXES, HTML_SUFFIXES,
)
from simple_ar.research.documents.sections import abstract_excerpt as _abstract


EXTRACTION_SCHEMA_VERSION = "fulltext_extraction.v1"
MOJIBAKE_SEGMENT_PATTERN = re.compile(r"鈥[\u4e00-\u9fff]")


class LocalDocumentParser:
    """Default local parser used when no external parser is injected."""

    def __init__(self, *, parser_backend: str = "basic", max_pdf_pages: int | None = None) -> None:
        self.parser_backend = str(parser_backend or "basic").strip().lower() or "basic"
        if max_pdf_pages is not None and (isinstance(max_pdf_pages, bool) or int(max_pdf_pages) < 1):
            raise ValueError("max_pdf_pages must be positive when supplied.")
        self.max_pdf_pages = int(max_pdf_pages) if max_pdf_pages is not None else None

    @classmethod
    def from_source_plan(cls, source_plan: SourcePlan) -> "LocalDocumentParser":
        """Build the default parser without exposing extraction internals."""

        return cls(
            parser_backend=str(source_plan.budget.get("parser_backend") or "basic"),
            max_pdf_pages=source_plan.budget.get("max_pdf_pages"),
        )

    def parse(self, path: Path) -> ParsedDocument:
        """Parse one local resource using the existing backend semantics."""

        if self.parser_backend == "unstructured":
            if path.suffix.lower() == ".pdf" and self.max_pdf_pages is not None:
                raise RuntimeError("unstructured PDF parsing cannot enforce max_pdf_pages; use the basic parser or omit the limit")
            return ParsedDocument(_read_unstructured(path), "unstructured")

        suffix = path.suffix.lower()
        if suffix in MATERIAL_TEXT_SUFFIXES:
            text = _read_text(path)
            # Remote text caches do not retain the URL's filename extension.
            # Identify notebook structure, not a particular source or project.
            notebook = None
            if suffix == '.ipynb' or text.lstrip().startswith('{'):
                try:
                    notebook = json.loads(text)
                except ValueError:
                    if suffix == '.ipynb':
                        raise RuntimeError('invalid_notebook_json') from None
            if isinstance(notebook, dict) and 'nbformat' in notebook:
                if type(notebook['nbformat']) is not int or notebook['nbformat'] != 4:
                    raise RuntimeError('unsupported_notebook_version')
                cells = notebook.get('cells')
                if not isinstance(cells, list):
                    raise RuntimeError('invalid_notebook_cells')
                pieces, outputs = [], 0
                for index, cell in enumerate(cells):
                    if not isinstance(cell, dict) or cell.get('cell_type') not in {'code', 'markdown', 'raw'}:
                        raise RuntimeError('invalid_notebook_cell')
                    source = cell.get('source', '')
                    if isinstance(source, list) and all(isinstance(line, str) for line in source):
                        source = ''.join(source)
                    if not isinstance(source, str):
                        raise RuntimeError('invalid_notebook_source')
                    pieces.append(f"# Notebook cell {index + 1} ({cell['cell_type']})\n\n{source}")
                    outputs += bool(cell.get('outputs'))
                return ParsedDocument('\n\n'.join(pieces), 'notebook_cells', coverage={
                    'notebook_cells': len(cells), 'extracted_cells': len(cells),
                    'cells_with_omitted_outputs': outputs,
                    'scope': 'Cell sources only; saved outputs, images and execution status are not read or verified.'})
            if suffix == '.ipynb':
                raise RuntimeError('invalid_notebook_structure')
            return ParsedDocument(text, "plain_text")
        if suffix in HTML_SUFFIXES:
            return ParsedDocument(_html_to_text(_read_text(path)), "basic_html")
        if suffix == ".pdf":
            coverage: dict[str, object] = {}
            return ParsedDocument(
                _read_pdf(path, max_pages=self.max_pdf_pages, coverage=coverage),
                "pypdf_optional",
                coverage=coverage,
            )
        raise RuntimeError(f"unsupported_fulltext_suffix:{suffix or 'none'}")


def document_extraction_limitations(records: list[DocumentRecord]) -> tuple[str, ...]:
    """Observed parser limits for reading/writing, never semantic certification."""
    notes = []
    for record in records:
        coverage = record.metadata.get("fulltext_extraction", {}).get("coverage", {})
        if 'notebook_cells' in coverage:
            notes.append(f"Source {record.document_id} contains {coverage['extracted_cells']} notebook cell sources; "
                         "saved outputs, images and execution status are not read or verified. Inspect the original notebook when they matter.")
        if coverage.get("unpaired_unicode_replacements"):
            notes.append(f"Source {record.document_id} contains {coverage['unpaired_unicode_replacements']} "
                         "unpaired Unicode characters replaced during extraction; affected text needs source inspection.")
        if coverage.get("truncated") is True:
            notes.append(f"Source {record.document_id} extracted {coverage.get('extracted_pages')}/"
                         f"{coverage.get('total_pages')} PDF pages; unextracted pages are unavailable. "
                         "Parsed text is not complete source access.")
        empty = coverage.get("empty_text_pages", [])
        if empty:
            notes.append(f"Source {record.document_id} returned no text on {len(empty)} PDF page(s); "
                         "blank extraction does not establish that those pages contain no information.")
    return tuple(notes)


def apply_fulltext_extraction(
    *,
    records: list[DocumentRecord],
    fulltext_manifest: dict[str, Any],
    source_plan: SourcePlan,
    extraction_dir: Path,
    resolver: DocumentResolver | None = None,
    parser: DocumentParser | None = None,
) -> tuple[list[DocumentRecord], dict[str, Any]]:
    """Parse cached/local full-text hints into document records.

    Args:
        records: Document records built from paper metadata and local inputs.
        fulltext_manifest: Output of ``build_fulltext_manifest``. Only cached
            hints are parsed; blocked or failed fetches remain manifest rows.
        source_plan: Research source plan controlling parser intent and budget.
        extraction_dir: Directory for extracted text when the source is not
            already plain text.
        resolver: Optional resolver for cached or local resources.
        parser: Optional parser port. The configured legacy parser is used when
            omitted.

    Returns:
        A pair of ``(updated_records, extraction_manifest)``. Parsing failures
        are recorded in the manifest and do not fail the search stage.
    """
    if not bool(fulltext_manifest.get("enabled", False)):
        return records, _manifest(
            enabled=False,
            source_plan=source_plan,
            rows=[],
            notes=["Full-text extraction is disabled by research_use_fulltext/use_fulltext."],
        )

    docs_by_id = {
        str(row.get("document_id", "")): row
        for row in _list_of_dicts(fulltext_manifest.get("documents"))
    }
    updated: list[DocumentRecord] = []
    rows: list[dict[str, Any]] = []
    extraction_dir.mkdir(parents=True, exist_ok=True)
    resolver = resolver or LocalDocumentResolver()
    for record in records:
        hint = _first_cached_hint(docs_by_id.get(record.document_id, {}))
        if hint is None:
            updated.append(record)
            rows.append(_row(record, status="skipped", reason="no_cached_fulltext"))
            continue
        parsed_record, row = _parse_hint(
            record=record,
            hint=hint,
            source_plan=source_plan,
            extraction_dir=extraction_dir,
            resolver=resolver,
            parser=parser,
        )
        updated.append(parsed_record)
        rows.append(row)

    return updated, _manifest(enabled=True, source_plan=source_plan, rows=rows, notes=[])


def _parse_hint(
    *,
    record: DocumentRecord,
    hint: dict[str, Any],
    source_plan: SourcePlan,
    extraction_dir: Path,
    resolver: DocumentResolver,
    parser: DocumentParser | None,
) -> tuple[DocumentRecord, dict[str, Any]]:
    try:
        resolution = resolver.resolve(
            document_id=record.document_id,
            local_path=str(hint.get("local_path") or "").strip() or None,
            url=str(hint.get("url") or "").strip() or None,
        )
    except Exception as exc:
        return record, _row(
            record,
            status="failed",
            reason=f"{type(exc).__name__}: {exc}"[:300],
            hint=hint,
        )
    if resolution.status != "available" or resolution.path is None:
        return record, _row(
            record,
            status="failed",
            reason=resolution.reason or resolution.status,
            hint=hint,
        )

    try:
        parsed = (parser or LocalDocumentParser.from_source_plan(source_plan)).parse(resolution.path)
        if not isinstance(parsed, ParsedDocument):
            raise TypeError(
                f"document parser returned {type(parsed).__name__}; "
                "expected ParsedDocument"
            )
        text, parser_name = parsed.text, parsed.parser
        coverage = dict(parsed.coverage)
    except Exception as exc:  # pragma: no cover - parser backend behavior varies by environment.
        return record, _row(
            record,
            status="failed",
            reason=str(exc)[:300] or "extraction_failed",
            source_path=resolution.path,
            hint=hint,
        )

    original_replacements = text.count("\ufffd")
    text = _normalize_text(text)
    if text.count("\ufffd") > original_replacements:
        coverage["unpaired_unicode_replacements"] = text.count("\ufffd") - original_replacements
    if not text.strip():
        return record, _row(
            record,
            status="failed",
            reason="empty_extraction",
            source_path=resolution.path,
            hint=hint,
        )

    text_path = resolution.path if (parser_name == 'plain_text'
        and resolution.path.suffix.lower() in MATERIAL_TEXT_SUFFIXES) else _write_extracted_text(
        extraction_dir=extraction_dir,
        record=record,
        text=text,
    )
    metadata = dict(record.metadata)
    # Parsing establishes usable text, not the extent of the fetched source.
    # Keep the selected hint's qualification with the extracted record so all
    # consumers (including retained bundles) see the same observed coverage.
    if hint.get("content_scope"):
        coverage["content_scope"] = hint["content_scope"]
    elif hint.get("acquisition", {}).get("content_scope"):
        coverage["content_scope"] = hint["acquisition"]["content_scope"]
    if hint.get("original_hint", {}).get("kind"):
        coverage["original_hint_kind"] = hint["original_hint"]["kind"]
    metadata["fulltext_extraction"] = {
        "status": "parsed",
        "parser": parser_name,
        "source_path": str(resolution.path),
        "extracted_text_path": str(text_path),
        "hint_kind": str(hint.get("kind") or ""),
        "chars": len(text),
        "coverage": coverage,
    }
    parsed_record = replace(
        record,
        abstract=_abstract(text) or record.abstract,
        local_path=str(text_path),
        content_hash=_sha256(text_path),
        extraction_status="parsed",
        parser=parser_name,
        metadata=metadata,
    )
    return parsed_record, _row(
        parsed_record,
        status="parsed",
        reason="ok",
        source_path=resolution.path,
        extracted_text_path=text_path,
        hint=hint,
        chars=len(text),
        parser=parser_name,
        coverage=coverage,
    )


def _read_pdf(path: Path, *, max_pages: int | None, coverage: dict[str, object] | None = None) -> str:
    if not _path_looks_like_pdf(path):
        raise RuntimeError("invalid_pdf_header")
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError("pypdf is not installed; install it or keep PDF parsing disabled") from exc
    reader = PdfReader(str(path))
    parts: list[str] = []
    empty_pages: list[int] = []
    for page in reader.pages[:max_pages]:
        text = page.extract_text() or ""
        if not text.strip():
            empty_pages.append(len(parts) + 1)
        # Link annotations carry exact targets that visible text may omit or
        # split across lines. Preserve them as attributed resources, not claims
        # or permission to fetch/execute their contents.
        links = []
        for annotation in page.get("/Annots", []):
            action = annotation.get_object().get("/A")
            if hasattr(action, "get") and action.get("/S") == "/URI":
                uri = str(action.get("/URI") or "").strip()
                if uri.startswith(("https://", "http://")) and uri not in links:
                    links.append(uri)
        if links:
            text += "\nPDF hyperlink targets (not verified contents):\n" + "\n".join(links)
        parts.append(text)
    if coverage is not None:
        coverage.update(total_pages=len(reader.pages), extracted_pages=len(parts), page_limit=max_pages,
                        truncated=len(parts) < len(reader.pages), empty_text_pages=empty_pages)
    return "\n".join(parts)


def _path_looks_like_pdf(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    return header.lstrip().startswith(b"%PDF-")


def _read_unstructured(path: Path) -> str:
    try:
        from unstructured.partition.auto import partition  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "unstructured is not installed; install the optional document parser or use parser_backend='basic'/'pypdf'"
        ) from exc
    elements = partition(filename=str(path))
    return "\n\n".join(str(element).strip() for element in elements if str(element).strip())


class _HTMLTextParser(HTMLParser):
    # Preserve document structure, not DOM text-node boundaries. Inline spans,
    # citations and emphasis must not split a heading or scientific sentence.
    BLOCK_TAGS = {"address", "article", "blockquote", "caption", "dd", "div", "dl", "dt",
                  "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "li",
                  "main", "ol", "p", "pre", "section", "table", "tr", "ul", "title"}
    SKIP_TAGS = {"script", "style", "noscript", "nav", "form", "dialog"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skip_depth = 0
        self.anchor: tuple[str, int] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth:
            if tag == "a":
                self.anchor = (dict(attrs).get("href") or "", len(self.parts))
            if tag in self.BLOCK_TAGS or tag in {"br", "hr"}:
                self.parts.append("\n")
                if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    self.parts.append("#" * int(tag[1]) + " ")
            elif tag in {"td", "th"}:
                self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag == "a" and self.anchor is not None:
            target, start = self.anchor
            label = "".join(self.parts[start:]).strip()
            if target and target != label:
                self.parts.append(f" ({target})")
            elif target.startswith(("https://", "http://")):
                # A displayed URL ends at its anchor, not at the next text node.
                self.parts.append(" ")
            self.anchor = None
        elif not self.skip_depth and tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(re.sub(r"\s+", " ", data))


def _html_to_text(text: str) -> str:
    parser = _HTMLTextParser()
    parser.feed(text)
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in "".join(parser.parts).splitlines()).strip()


def _write_extracted_text(*, extraction_dir: Path, record: DocumentRecord, text: str) -> Path:
    path = extraction_dir / f"{_safe_name(record.document_id)}.txt"
    path.write_text(_normalize_text(text), encoding="utf-8")
    return path


def _first_cached_hint(row: dict[str, Any]) -> dict[str, Any] | None:
    for hint in _list_of_dicts(row.get("hints")):
        if hint.get("status") == "cached" and hint.get("local_path"):
            return hint
    return None


def _manifest(
    *,
    enabled: bool,
    source_plan: SourcePlan,
    rows: list[dict[str, Any]],
    notes: list[str],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "schema_version": EXTRACTION_SCHEMA_VERSION,
        "enabled": enabled,
        "parser_backend": str(source_plan.budget.get("parser_backend") or "basic"),
        "document_count": len(rows),
        "parsed_count": status_counts.get("parsed", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "documents": rows,
        "notes": notes
        or [
            "Only cached or local full-text hints are parsed.",
            "PDF extraction uses optional pypdf when available; failures are recorded without failing the run.",
        ],
    }


def _row(
    record: DocumentRecord,
    *,
    status: str,
    reason: str,
    source_path: Path | None = None,
    extracted_text_path: Path | None = None,
    hint: dict[str, Any] | None = None,
    chars: int | None = None,
    parser: str | None = None,
    coverage: dict[str, object] | None = None,
) -> dict[str, Any]:
    return {
        "document_id": record.document_id,
        "title": record.title,
        "status": status,
        "reason": reason,
        "parser": parser or record.parser or "",
        "hint_kind": str((hint or {}).get("kind") or ""),
        "source_path": str(source_path) if source_path else None,
        "extracted_text_path": str(extracted_text_path) if extracted_text_path else None,
        "chars": chars,
        "coverage": dict(coverage or {}),
    }


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _normalize_text(text: str) -> str:
    # Some PDF backends return UTF-16 surrogate code units instead of Unicode
    # scalars. Preserve valid pairs (including mathematical symbols); malformed
    # units become visible replacement characters, never silently dropped.
    if re.search(r"[\ud800-\udfff]", text):
        text = text.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
    repaired = _repair_common_mojibake(text)
    return re.sub(r"\n{3,}", "\n\n", repaired.replace("\r\n", "\n").replace("\r", "\n")).strip() + "\n"


def _repair_common_mojibake(text: str) -> str:
    """Repair common UTF-8-as-GBK artifacts from lightweight PDF extraction."""
    if "鈥" not in text:
        return text

    def _repair_segment(match: re.Match[str]) -> str:
        segment = match.group(0)
        try:
            return segment.encode("gb18030").decode("utf-8")
        except UnicodeError:
            return segment

    repaired = MOJIBAKE_SEGMENT_PATTERN.sub(_repair_segment, text)
    repaired = re.sub(r"(?<=[A-Za-z])鈥\?(?=[A-Za-z])", "’", repaired)
    repaired = repaired.replace("鈥?", "”").replace("鈥�", "”")
    return repaired


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return safe[:120] or "document"
