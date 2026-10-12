"""Replaceable boundaries for local document resolution and parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


ResolutionStatus = Literal["available", "missing", "unavailable", "failed"]
TEXT_SUFFIXES = {".md", ".markdown", ".txt"}
HTML_SUFFIXES = {".html", ".htm"}
SUPPORTED_DOCUMENT_SUFFIXES = TEXT_SUFFIXES | HTML_SUFFIXES | {".pdf"}
MATERIAL_TEXT_SUFFIXES = TEXT_SUFFIXES | {".json", ".bib", ".ipynb"}
SUPPORTED_MATERIAL_SUFFIXES = MATERIAL_TEXT_SUFFIXES | HTML_SUFFIXES | {".pdf"}


@dataclass(frozen=True, slots=True)
class DocumentResolution:
    """Result of resolving a document hint to a local resource."""

    document_id: str
    status: ResolutionStatus
    path: Path | None = None
    reason: str = ""


class DocumentResolver(Protocol):
    """Resolve a cached or user-provided document without parsing it."""

    def resolve(
        self,
        *,
        document_id: str,
        local_path: str | None,
        url: str | None,
    ) -> DocumentResolution:
        """Return a local path or an explicit unavailable status."""
        ...


class LocalDocumentResolver:
    """Resolve paths already present on the local machine."""

    def resolve(
        self,
        *,
        document_id: str,
        local_path: str | None,
        url: str | None,
    ) -> DocumentResolution:
        if local_path:
            path = Path(local_path)
            if path.is_file():
                return DocumentResolution(
                    document_id=document_id,
                    status="available",
                    path=path,
                    reason="local_resource_available",
                )
            return DocumentResolution(
                document_id=document_id,
                status="missing",
                reason="cached_path_missing",
            )
        if url:
            return DocumentResolution(
                document_id=document_id,
                status="unavailable",
                reason="remote_resource_not_resolved",
            )
        return DocumentResolution(
            document_id=document_id,
            status="missing",
            reason="resource_location_missing",
        )


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Text and provenance label returned by a document parser."""

    text: str
    parser: str
    # Optional observed parser coverage; an absent value means unknown, not
    # complete. This travels with the existing document provenance.
    coverage: dict[str, object] = field(default_factory=dict)


class DocumentParser(Protocol):
    """Parse one resolved local resource into text."""

    def parse(self, path: Path) -> ParsedDocument:
        """Return extracted text and the parser name."""
        ...


__all__ = [
    "DocumentParser",
    "DocumentResolution",
    "DocumentResolver",
    "LocalDocumentResolver",
    "ParsedDocument",
    "ResolutionStatus",
]
