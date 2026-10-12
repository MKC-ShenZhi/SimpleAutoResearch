from __future__ import annotations

from contextlib import suppress
import codecs
from dataclasses import replace
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
import urllib.error
import urllib.request

from simple_ar.literature.models import Paper
from simple_ar.research.contracts import DocumentRecord, FulltextHint, SourcePlan
from simple_ar.research.documents.ports import TEXT_SUFFIXES, MATERIAL_TEXT_SUFFIXES, HTML_SUFFIXES


PDF_SUFFIX = ".pdf"
_FETCH_TIMEOUT_SEC = 20


def fulltext_hints_for_paper(paper: Paper, *, document_id: str) -> list[FulltextHint]:
    """Return non-destructive full-text hints derived from paper metadata.

    Args:
        paper: Normalized metadata row from a source connector.
        document_id: Document id that will own the hint.

    Returns:
        Candidate full-text resources. The function only infers hints; it does
        not fetch remote content.
    """
    hints: list[FulltextHint] = []
    arxiv_id = _arxiv_id(paper.source_id or "") or _arxiv_id(paper.url)
    if paper.source == "arxiv" and arxiv_id:
        hints.append(
            FulltextHint(
                document_id=document_id,
                kind="pdf",
                source="arxiv",
                url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                access="open",
                reason="arxiv_pdf_url",
            )
        )
    if paper.fulltext_url:
        hints.append(_hint_from_url(document_id=document_id, url=paper.fulltext_url, source=paper.source, reason="provider_fulltext_url"))
    elif paper.url and not (paper.source == "arxiv" and arxiv_id):
        hints.append(_hint_from_url(document_id=document_id, url=paper.url, source=paper.source, reason="metadata_url"))
    return _deduplicate_hints(hints)


def build_fulltext_manifest(
    *,
    records: list[DocumentRecord],
    source_plan: SourcePlan,
    cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Build the full-text hint, fetch-budget, and cache manifest.

    Args:
        records: Search document records.
        source_plan: Search source plan with full-text intent and budgets.
        cache_dir: Optional run-local cache directory for selected local or
            remote full-text resources.

    Returns:
        A JSON-friendly manifest describing full-text hints, selected candidates,
        cached resources, and skipped/failed reasons. Remote downloads are only
        attempted when full-text intent and permissions allow them.
        The legacy max_pdf_mb budget limits each remote PDF, HTML or text
        resource, including cache reuse; zero/unset retains the unlimited default.
    """
    max_documents = _positive_int(source_plan.budget.get("max_fulltext_documents"), default=0)
    max_fetch_attempts = _fulltext_fetch_attempt_cap(source_plan.budget, target=max_documents)
    reserve = min(_positive_int(source_plan.budget.get("reserved_fulltext_documents"), default=0),
                  max(0, max_documents - 1))
    document_limit = max_documents - reserve if max_documents else 0
    attempt_limit = max(1, max_fetch_attempts - reserve) if reserve and max_fetch_attempts else max_fetch_attempts
    max_pdf_bytes = _positive_int(source_plan.budget.get("max_pdf_mb"), default=0) * 1024 * 1024
    keep_raw_pdf = bool(source_plan.budget.get("keep_raw_pdf")) if isinstance(source_plan.budget.get("keep_raw_pdf"), bool) else False
    parser_backend = str(source_plan.budget.get("parser_backend") or "basic")
    page_backend = str(source_plan.budget.get("web_extract_backend") or "direct")
    if page_backend not in {"direct", "tavily_basic"}:
        raise ValueError("web_extract_backend must be direct or tavily_basic")

    rows: list[dict[str, Any]] = []
    fetch_attempt_count = 0
    selected_count = 0
    cached_count = 0
    remote_cached_count = 0
    hint_count = 0
    def account_redirect() -> None:
        nonlocal fetch_attempt_count
        if attempt_limit and fetch_attempt_count >= attempt_limit:
            raise RuntimeError("max_fulltext_fetch_attempts_reached")
        fetch_attempt_count += 1
    for record in records:
        hints = _hints_for_record(record)
        hint_count += len(hints)
        selected_for_fetch = False
        row_hints: list[dict[str, Any]] = []
        for hint in hints:
            planned = _plan_hint(
                hint,
                require_fulltext=source_plan.require_fulltext,
                allow_pdf_download=source_plan.allow_pdf_download,
                fetch_attempt_count=fetch_attempt_count,
                cached_count=remote_cached_count,
                max_documents=document_limit,
                max_fetch_attempts=attempt_limit,
                max_pdf_bytes=max_pdf_bytes,
            )
            if planned.status == "selected":
                selected_count += 1
                if planned.local_path:
                    planned = _cache_local_hint(planned)
                else:
                    fetch_attempt_count += 1
                    selected_for_fetch = True
                    if cache_dir is not None:
                        planned = _cache_selected_hint(
                            planned,
                            cache_dir=cache_dir,
                            max_pdf_bytes=max_pdf_bytes,
                            keep_raw_pdf=keep_raw_pdf,
                            allow_pdf_download=source_plan.allow_pdf_download,
                            on_redirect=account_redirect,
                            page_backend=page_backend,
                            public_only=bool(source_plan.budget.get("public_network_only", False)),
                        )
            if planned.status == "cached":
                cached_count += 1
                if not hint.local_path:
                    remote_cached_count += 1
            hint_row = planned.to_row()
            if hint.kind == "landing":
                hint_row.update(original_hint=hint.to_row(),
                                content_scope="source_webpage_not_verified_paper_fulltext")
            row_hints.append(hint_row)
        rows.append(
            {
                "document_id": record.document_id,
                "title": record.title,
                "source": record.source,
                "extraction_status": record.extraction_status,
                "selected_for_fetch": selected_for_fetch,
                "hints": row_hints,
            }
        )

    status_counts = Counter(
        str(hint.get("status", "unknown"))
        for row in rows
        for hint in row.get("hints", [])
        if isinstance(hint, dict)
    )
    return {
        "schema_version": "research_fulltext_manifest.v1",
        "enabled": source_plan.require_fulltext or any(
            hint.get("status") == "cached" and hint.get("kind") in {"pdf", "html"} and hint.get("local_path")
            for row in rows for hint in row["hints"]
        ),
        "allow_pdf_download": source_plan.allow_pdf_download,
        "cache_dir": str(cache_dir) if cache_dir else None,
        "budget": {
            "max_fulltext_documents": max_documents,
            "max_fulltext_fetch_attempts": max_fetch_attempts,
            "max_pdf_mb": _positive_int(source_plan.budget.get("max_pdf_mb"), default=0),
            "keep_raw_pdf": keep_raw_pdf,
            "parser_backend": parser_backend,
            "web_extract_backend": page_backend,
        },
        "document_count": len(records),
        "hint_count": hint_count,
        "selected_count": selected_count,
        "fetch_attempt_count": fetch_attempt_count,
        "cached_count": cached_count,
        "status_counts": dict(sorted(status_counts.items())),
        "documents": rows,
        "notes": [
            f"This acquisition leaves {reserve} document slots for later evidence; total limits remain unchanged.",
            "Full-text fetching is permissioned and failure-safe; failed fetches do not fail the search stage.",
            "Fetch failures are replenished from later candidates up to the bounded fetch-attempt cap.",
            "Remote PDFs are selected only when both use_fulltext and allow_pdf_download are enabled.",
            "max_pdf_mb limits bytes per remote resource for PDF, HTML and text, including reused remote cache files; zero/unset means no byte limit.",
            "Local files can be used as full-text inputs without network access.",
            "Landing URLs are detected by response type/content; webpage access does not establish paper methods or complete paper access. Original hints retain provenance.",
        ],
    }


def _hints_for_record(record: DocumentRecord) -> list[FulltextHint]:
    hints: list[FulltextHint] = []
    if record.local_path:
        local_hint = _hint_from_local_path(record)
        if local_hint:
            hints.append(local_hint)
    raw_hints = record.metadata.get("fulltext_hints")
    if isinstance(raw_hints, list):
        for row in raw_hints:
            if isinstance(row, dict):
                hint = _hint_from_row(record.document_id, row)
                if hint:
                    hints.append(hint)
    elif record.url and not record.local_path:
        hints.append(_hint_from_url(document_id=record.document_id, url=record.url, source=record.source, reason="document_url"))
    return _deduplicate_hints(hints)


def _plan_hint(
    hint: FulltextHint,
    *,
    require_fulltext: bool,
    allow_pdf_download: bool,
    fetch_attempt_count: int,
    cached_count: int,
    max_documents: int,
    max_fetch_attempts: int,
    max_pdf_bytes: int,
) -> FulltextHint:
    if hint.local_path:
        if not require_fulltext and hint.kind not in {"pdf", "html"}:
            return _replace_hint(hint, status="hint_only", reason="fulltext_disabled")
        if hint.kind == "pdf" and max_pdf_bytes and hint.size_bytes and hint.size_bytes > max_pdf_bytes:
            return _replace_hint(hint, status="skipped", reason="local_pdf_exceeds_max_pdf_mb")
        return _replace_hint(hint, status="selected", reason="local_fulltext_available")
    if not require_fulltext:
        return _replace_hint(hint, status="hint_only", reason="fulltext_disabled")
    if hint.kind == "pdf" and not allow_pdf_download:
        return _replace_hint(hint, status="blocked", reason="pdf_download_disabled")
    if max_documents and cached_count >= max_documents:
        return _replace_hint(hint, status="skipped", reason="max_fulltext_documents_reached")
    if max_fetch_attempts and fetch_attempt_count >= max_fetch_attempts:
        return _replace_hint(hint, status="skipped", reason="max_fulltext_fetch_attempts_reached")
    if hint.kind in {"pdf", "html", "text", "landing"}:
        return _replace_hint(hint, status="selected", reason="within_fulltext_budget")
    return _replace_hint(hint, status="hint_only", reason="unsupported_remote_fulltext_kind")


def _fulltext_fetch_attempt_cap(budget: dict[str, Any], *, target: int) -> int:
    configured = _positive_int(budget.get("max_fulltext_fetch_attempts"), default=0)
    if configured:
        return configured
    if target <= 0:
        return 0
    return max(target, target * 2)


def _cache_selected_hint(
    hint: FulltextHint,
    *,
    cache_dir: Path,
    max_pdf_bytes: int,
    keep_raw_pdf: bool,
    allow_pdf_download: bool,
    on_redirect: Callable[[], None] | None = None,
    page_backend: str = "direct",
    public_only: bool = False,
) -> FulltextHint:
    if hint.local_path:
        return _cache_local_hint(hint)
    if not hint.url:
        return _replace_hint(hint, status="failed", reason="missing_fulltext_url")
    if hint.kind == "pdf" and not keep_raw_pdf:
        return _replace_hint(hint, status="skipped", reason="raw_pdf_retention_disabled")
    try:
        cached = _fetch_remote_hint(hint, cache_dir=cache_dir, max_bytes=max_pdf_bytes,
                                    page_backend=page_backend,
                                    public_only=public_only,
                                    allow_pdf_download=allow_pdf_download, keep_raw_pdf=keep_raw_pdf,
                                    on_redirect=on_redirect)
    except Exception as exc:
        return _replace_hint(hint, status="fetch_failed", reason=str(exc)[:300])
    return cached


def _cache_local_hint(hint: FulltextHint) -> FulltextHint:
    path = Path(hint.local_path or "")
    if not path.exists() or not path.is_file():
        return _replace_hint(hint, status="failed", reason="local_file_not_found")
    return replace(hint, local_path=str(path), status="cached", reason="local_fulltext_available",
                   size_bytes=path.stat().st_size)


def _remote_content_kind(content_type: str, head: bytes, *, fallback: str) -> str:
    """Detect supported bytes, never infer a PDF permission from its URL."""
    mime = content_type.partition(";")[0].strip().lower()
    if _bytes_look_like_pdf(head):
        return "pdf"
    if "pdf" in mime:
        raise RuntimeError("remote_content_not_pdf")
    if any(value < 32 and value not in (9, 10, 13) for value in head):
        raise RuntimeError("unsupported_remote_content")
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mime == "text/plain":
        return "text"
    if mime and mime not in {"application/octet-stream", "binary/octet-stream"}:
        raise RuntimeError("unsupported_remote_content_type")
    if re.search(br"<(?:!doctype\s+html|html|head|body|article|main|h[1-6]|p|div)(?:\s|>)", head.lower()):
        return "html"
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        raise RuntimeError("unsupported_remote_content") from None
    return fallback if fallback in {"html", "text"} else "text"


def _check_remote_pdf_policy(kind: str, *, allow_pdf_download: bool, keep_raw_pdf: bool) -> None:
    if kind == "pdf":
        if not allow_pdf_download:
            raise RuntimeError("pdf_download_disabled")
        if not keep_raw_pdf:
            raise RuntimeError("raw_pdf_retention_disabled")


def _fetch_remote_hint(hint: FulltextHint, *, cache_dir: Path, max_bytes: int,
                       allow_pdf_download: bool, keep_raw_pdf: bool,
                       on_redirect: Callable[[], None] | None = None,
                       page_backend: str = "direct", public_only: bool = False) -> FulltextHint:
    """Enforce the shared max_pdf_mb byte budget for every remote content kind."""
    if page_backend == "tavily_basic" and hint.kind in {"html", "text", "landing"}:
        return _fetch_tavily_hint(hint, cache_dir=cache_dir, max_bytes=max_bytes)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{_safe_name(hint.document_id)}-{_safe_name(hint.source)}"
    def path_for_kind(kind: str) -> Path:
        # Keep legacy explicit-kind caches; detected types belong to this URL.
        name = stem if kind == hint.kind else stem + "-" + hashlib.sha256(str(hint.url).encode()).hexdigest()[:16]
        return cache_dir / (name + _cache_suffix(replace(hint, kind=kind)))
    kinds = ("pdf",) if hint.kind == "pdf" else tuple(dict.fromkeys((hint.kind, "html", "text", "pdf")))
    kinds = tuple(kind for kind in kinds if kind != "landing")
    for kind in kinds:
        cache_path = path_for_kind(kind)
        if not _usable_cache_file(cache_path, kind=kind):
            continue
        size = cache_path.stat().st_size
        if max_bytes and size > max_bytes:
            raise RuntimeError("remote_file_exceeds_max_pdf_mb")
        with cache_path.open("rb") as handle:
            detected = _remote_content_kind("", handle.read(1024), fallback=kind)
        _check_remote_pdf_policy(detected, allow_pdf_download=allow_pdf_download, keep_raw_pdf=keep_raw_pdf)
        if detected != kind:
            raise RuntimeError("remote_cached_content_kind_mismatch")
        return replace(hint, kind=detected, local_path=str(cache_path), status="cached",
                       reason="cache_hit", size_bytes=size)
    request = urllib.request.Request(
        str(hint.url),
        headers={"User-Agent": "SimpleAutoResearch/0.1"},
    )
    bytes_written = 0
    cache_path = None  # Only remove a file opened by this attempt, not prior cache.
    try:
        if public_only or hint.source == "supporting_material":
            from simple_ar.research.preparation_assets import public_document_response
            response_context = public_document_response(str(hint.url), max_bytes=max_bytes,
                                                        on_redirect=on_redirect)
        else:
            response_context = urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SEC)
        with response_context as response:
            length = response.headers.get("Content-Length")
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if hint.kind == "pdf" and content_type and not _content_type_may_be_pdf(content_type):
                raise RuntimeError(f"remote_content_type_not_pdf:{content_type}")
            if "pdf" in content_type:
                _check_remote_pdf_policy("pdf", allow_pdf_download=allow_pdf_download, keep_raw_pdf=keep_raw_pdf)
            if max_bytes and length:
                try:
                    if int(length) > max_bytes:
                        raise RuntimeError("remote_file_exceeds_max_pdf_mb")
                except ValueError:
                    pass
            first_chunk = response.read(1024)
            if not first_chunk:
                raise RuntimeError("empty_fulltext_fetch")
            kind = _remote_content_kind(content_type, first_chunk, fallback=hint.kind)
            if hint.kind == "pdf" and kind != "pdf":
                raise RuntimeError("remote_content_not_pdf")
            _check_remote_pdf_policy(kind, allow_pdf_download=allow_pdf_download, keep_raw_pdf=keep_raw_pdf)
            cache_path = path_for_kind(kind)
            with cache_path.open("wb") as handle:
                bytes_written = len(first_chunk)
                if max_bytes and bytes_written > max_bytes:
                    raise RuntimeError("remote_file_exceeds_max_pdf_mb")
                handle.write(first_chunk)
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if max_bytes and bytes_written > max_bytes:
                        raise RuntimeError("remote_file_exceeds_max_pdf_mb")
                    handle.write(chunk)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, RuntimeError):
        if cache_path is not None:
            with suppress(OSError):
                cache_path.unlink()
        raise
    return FulltextHint(
        document_id=hint.document_id,
        kind=kind,
        source=hint.source,
        url=hint.url,
        local_path=str(cache_path),
        access=hint.access,
        status="cached",
        reason="remote_fulltext_cached",
        size_bytes=bytes_written,
    )


def _fetch_tavily_hint(hint: FulltextHint, *, cache_dir: Path, max_bytes: int) -> FulltextHint:
    """Cache extracted public-page text separately from direct downloads."""
    from simple_ar.research.connectors.web import WebConnector
    from simple_ar.core.artifacts import write_json
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{_safe_name(hint.document_id)}-tavily-basic.txt"
    receipt = path.with_suffix(".json")
    if _usable_cache_file(path, kind="text") and receipt.is_file():
        provenance = json.loads(receipt.read_text(encoding="utf-8"))
        if provenance.get("source_url") != hint.url:
            raise RuntimeError("page_cache_source_mismatch")
        size = path.stat().st_size
        if max_bytes and size > max_bytes:
            raise RuntimeError("remote_file_exceeds_max_pdf_mb")
        return replace(hint, kind="text", local_path=str(path), status="cached", reason="cache_hit",
                       size_bytes=size, acquisition={**provenance, "provider_requested": False})
    text, provenance = WebConnector().extract_page(str(hint.url))
    provenance["provider_requested"] = True
    if not text.strip():
        return replace(hint, status="fetch_failed", reason="page_extraction_empty", acquisition=provenance)
    payload = text.encode("utf-8")
    if max_bytes and len(payload) > max_bytes:
        return replace(hint, status="fetch_failed", reason="remote_file_exceeds_max_pdf_mb", acquisition=provenance)
    path.write_bytes(payload)
    write_json(receipt, provenance)
    return replace(hint, kind="text", local_path=str(path), status="cached", reason="tavily_basic_page_cached",
                   size_bytes=len(payload), acquisition=provenance)


def _usable_cache_file(path: Path, *, kind: str) -> bool:
    """Accept a prior cache entry without adding a content-hash requirement."""

    try:
        if not path.is_file() or path.stat().st_size == 0:
            return False
        if kind == "pdf":
            with path.open("rb") as handle:
                return _bytes_look_like_pdf(handle.read(1024))
        return True
    except OSError:
        return False


def _content_type_may_be_pdf(content_type: str) -> bool:
    lowered = content_type.lower()
    return "pdf" in lowered or "octet-stream" in lowered or "binary" in lowered


def _bytes_look_like_pdf(data: bytes) -> bool:
    return data.lstrip().startswith(b"%PDF-")


def _cache_suffix(hint: FulltextHint) -> str:
    if hint.kind == "pdf":
        return ".pdf"
    if hint.kind == "html":
        return ".html"
    if hint.kind == "text":
        return ".txt"
    return ".bin"


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("._")[:120] or "resource"


def _hint_from_row(document_id: str, row: dict[str, Any]) -> FulltextHint | None:
    url = str(row.get("url") or "").strip() or None
    local_path = str(row.get("local_path") or "").strip() or None
    if not url and not local_path:
        return None
    return FulltextHint(
        document_id=document_id,
        kind=str(row.get("kind") or _kind_from_url(url or local_path or "")),
        source=str(row.get("source") or "metadata"),
        url=url,
        local_path=local_path,
        access=str(row.get("access") or "unknown"),
        status=str(row.get("status") or "hint_only"),
        reason=str(row.get("reason") or ""),
        size_bytes=row.get("size_bytes") if isinstance(row.get("size_bytes"), int) else None,
        acquisition=dict(row.get("acquisition") or {}),
    )


def _hint_from_url(*, document_id: str, url: str, source: str, reason: str) -> FulltextHint:
    kind = _kind_from_url(url)
    access = "open" if kind in {"pdf", "html", "text"} and url.startswith(("http://", "https://")) else "unknown"
    return FulltextHint(
        document_id=document_id,
        kind=kind,
        source=source,
        url=url,
        access=access,
        reason=reason,
    )


def _hint_from_local_path(record: DocumentRecord) -> FulltextHint | None:
    if not record.local_path:
        return None
    path = Path(record.local_path)
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        kind = "pdf"
    elif suffix in MATERIAL_TEXT_SUFFIXES:
        kind = "text"
    elif suffix in HTML_SUFFIXES:
        kind = "html"
    else:
        return None
    return FulltextHint(
        document_id=record.document_id,
        kind=kind,
        source="local_files",
        local_path=str(path),
        access="local",
        reason="local_file",
        size_bytes=path.stat().st_size if path.exists() and path.is_file() else None,
    )


def _replace_hint(hint: FulltextHint, *, status: str, reason: str) -> FulltextHint:
    return replace(hint, status=status, reason=reason)


def _kind_from_url(url: str) -> str:
    lower = url.lower()
    parsed = urlparse(lower)
    path = parsed.path or lower
    if path.endswith(PDF_SUFFIX) or path.rstrip("/").endswith("/pdf") or "/pdf/" in path or _looks_like_pdf_download_path(path):
        return "pdf"
    if any(path.endswith(suffix) for suffix in TEXT_SUFFIXES):
        return "text"
    if any(path.endswith(suffix) for suffix in HTML_SUFFIXES):
        return "html"
    if parsed.scheme in {"http", "https"}:
        return "landing"
    return "unknown"


def _looks_like_pdf_download_path(path: str) -> bool:
    """Return true for common scholarly PDF download paths without .pdf suffix."""

    normalized = path.rstrip("/")
    if "/article/download/" in normalized:
        return True
    if "/download/" in normalized and re.search(r"/\d+(?:/\d+)?$", normalized):
        return True
    return False


def _arxiv_id(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    for marker in ("/abs/", "/pdf/"):
        if marker in value:
            value = value.split(marker, maxsplit=1)[1]
            break
    value = value.removesuffix(".pdf")
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", value):
        return value
    if re.match(r"^[a-z-]+(\.[A-Z]{2})?/\d{7}(v\d+)?$", value):
        return value
    return ""


def _deduplicate_hints(hints: list[FulltextHint]) -> list[FulltextHint]:
    seen: set[tuple[str, str, str | None, str | None]] = set()
    unique: list[FulltextHint] = []
    for hint in hints:
        key = (hint.document_id, hint.kind, hint.url, hint.local_path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(hint)
    return unique


def _positive_int(value: object, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default
