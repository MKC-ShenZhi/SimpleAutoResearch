"""Fill missing citation fields from the same retained source's front matter.

This is a pure projection of a reading proposal, not a metadata service or an
identity certificate. Provider/user fields, source identity and saved documents
are never overwritten. Quotes are retained with their existing section locator.
"""
from collections.abc import Mapping
from pathlib import PurePosixPath
import re
from typing import Any
from urllib.parse import urlsplit

from simple_ar.literature.models import Paper, bibliographic_details, _arxiv_identifier
from simple_ar.research.contracts import DocumentRecord, DocumentSection

FRONT_MATTER_CHARS = 12000
CITATION_FIELD_VALUE_RULE = (
    "Field values contain data, not source labels: title is the publication title; authors is an array of names "
    "without affiliation markers; published is only the visible publication date/year, without labels such as "
    "'Published online:', 'Received:' or 'Accepted:'; doi is the identifier and url is the full public URL. "
    "Use ISO dates/years or a complete written-month date, not ambiguous numeric dates. The quote preserves "
    "the original label and punctuation. Each value must occur in that exact same-source quote apart from whitespace. "
    "Do not infer missing metadata or substitute received/accepted dates for publication."
)


def _placeholder_title(record: DocumentRecord) -> str:
    if record.source == "supporting_material" and record.metadata.get("kind") == "supporting_material":
        return str(record.url or "")
    return PurePosixPath(str(record.source_id or "").replace("\\", "/")).stem.replace("_", " ").replace("-", " ").strip()


def _can_fill_source_fields(record: DocumentRecord) -> bool:
    return (not record.metadata.get("paper_id") and (record.source == "local_files"
        or (record.source == "supporting_material" and record.metadata.get("kind") == "supporting_material")))


def local_citation_fields_to_fill(paper: Mapping[str, Any], record: DocumentRecord) -> list[str]:
    """Request fields using the same placeholder and availability rules as acceptance."""
    if not _can_fill_source_fields(record):
        return []
    details = bibliographic_details(Paper.from_row(dict(paper)))
    return [field for field, missing in (
        ("title", bool(_placeholder_title(record)) and paper.get("title") == _placeholder_title(record)),
        ("authors", not details["authors"]), ("published", not details["year"]),
        ("doi", not details["doi"]), ("url", not details["url"])) if missing]


def front_matter_view(section: DocumentSection) -> dict[str, Any]:
    return {"section_id": section.section_id, "text": section.text[:FRONT_MATTER_CHARS],
            "truncated": len(section.text) > FRONT_MATTER_CHARS,
            "scope": "supplied front matter, not independent publication metadata"}


def apply_bibliographic_note(paper: Mapping[str, Any], record: DocumentRecord,
                            section: DocumentSection | None, note: Mapping[str, Any] | None,
                            ) -> tuple[dict[str, Any], dict[str, Any]]:
    row, origins = dict(paper), {}
    if (not _can_fill_source_fields(record) or section is None
            or section.document_id != record.document_id or section.section != "front_matter"):
        return row, origins
    note = note or {}
    text = " ".join(section.text[:FRONT_MATTER_CHARS].split())
    placeholder = _placeholder_title(record)
    proposal = note.get("bibliographic_fields", [])
    fields = list(proposal[:5]) if isinstance(proposal, list) else []
    # Legacy notes proposed only a title. Retain that narrow, source-matched
    # behavior; no inferred authors, year, or identifier is added on recovery.
    if not any(isinstance(item, Mapping) and item.get("field") == "title" for item in fields):
        fields.append({"field": "title", "value": note.get("title"), "quote": note.get("title"),
                       "section_id": section.section_id})
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        field = item.get("field")
        quote = " ".join(str(item.get("quote") or "").split())
        if (not isinstance(field, str) or field not in {"title", "authors", "published", "doi", "url"} or field in origins
                or item.get("section_id") != section.section_id or not quote or len(quote) > FRONT_MATTER_CHARS
                or quote not in text):
            continue
        value = item.get("value")
        if field == "authors":
            if (row.get(field) or not isinstance(value, list) or not value or len(value) > 200
                    or any(not isinstance(name, str) or not name.strip() or len(name) > 160 for name in value)):
                continue
            value = [" ".join(name.split()) for name in value]
            if len(set(value)) != len(value) or any(name not in quote for name in value):
                continue
        else:
            if not isinstance(value, str) or not value.strip():
                continue
            value = " ".join(value.split())
            if value not in quote or len(value) > (240 if field == "title" else 480):
                continue
            if field == "title":
                if not placeholder or row.get(field) != placeholder or value == placeholder:
                    continue
            elif field == "url":
                if str(row.get(field) or "").lower().startswith(("http://", "https://")):
                    continue
                try:
                    url = urlsplit(value)
                except ValueError:
                    continue
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    continue
            elif row.get(field):
                continue
        candidate = {**row, field: value}
        details = bibliographic_details(Paper.from_row(candidate))
        if field == "published" and not details["year"]:
            continue
        if field == "published":
            # Match the saved source line, not just a model-selected date substring.
            # A real date can still describe a manuscript lifecycle event rather
            # than publication. Do not promote an explicitly labelled such event.
            date_lines = [line for line in section.text[:FRONT_MATTER_CHARS].splitlines()
                          if value in " ".join(line.split())]
            if date_lines and all(re.search(
                r"\b(?:draft|received|accepted|revised|submitted|updated|copyright)\b", line, re.I
            ) for line in date_lines):
                continue
        if field == "doi" and not details["doi"]:
            continue
        row = candidate
        origins[field] = {"section_id": section.section_id, "quote": quote,
                          "scope": "same_source_front_matter_text_match_not_identity_verification"}
        if field == "authors" and item.get("complete") is not True:
            row["bibliographic_notes"] = [*(row.get("bibliographic_notes") or []),
                "Author names were extracted from a bounded front-matter view; completeness is not established."]
    # arXiv documents explicitly stamp their identifier in front matter.
    # The registry documents /abs/<identifier>; mapping that observed label is
    # not a title search, filename guess, publication-year inference or request.
    # https://github.com/arXiv/arxiv-docs/blob/develop/source/help/find/index.md
    stamps = [(match.group(0).strip(), _arxiv_identifier(match.group(1))) for match in
        re.finditer(r"(?im)^\s*arxiv:\s*([^\s]+)", section.text[:FRONT_MATTER_CHARS])]
    stamps = [(quote, identifier) for quote, identifier in stamps if identifier]
    if not str(row.get("url") or "").lower().startswith(("http://", "https://")) and len({identifier for _, identifier in stamps}) == 1:
        row["url"] = "https://arxiv.org/abs/" + stamps[0][1]
        origins["url"] = {"section_id": section.section_id, "quote": " ".join(stamps[0][0].split()),
                          "scope": "observed_arxiv_label_registry_locator_mapping_not_identity_verification"}
    if origins:
        row["bibliographic_notes"] = list(dict.fromkeys([*(row.get("bibliographic_notes") or []),
            "Missing citation fields filled from supplied front matter and matched to saved source quotes; publication identity is not independently verified."]))
    return row, origins
