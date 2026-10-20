"""Literal review anchors into an existing prompt, not another evidence store.

Roles describe record ownership, not truth. A valid quote does not prove that a
reviewer's interpretation follows; absent/truncated evidence is still unknown.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from simple_ar.integrations.llm import LLMResponseError
from simple_ar.report.schema import (
    FACTUAL_REVIEW_FINDING_TYPES, ReportEvidenceQuote, ReportSectionReview, finding_requires_resolution,
)

GAP_EVIDENCE_CONTRACT = "empty_gap_observation.v1"


def _allows_empty_gap(view: Mapping[str, Any], kind: str, action: str | None) -> bool:
    return (view.get("evidence_reference_contract") == GAP_EVIDENCE_CONTRACT
            and kind == "evidence_gap" and action == "verify")


EVIDENCE_QUOTE_SCHEMA = {
    # Show the normal response, not all optional quotation fields at once.
    # Resolution/persistence and strict legacy quotation validation are unchanged.
    "pointer": "copy the exact absolute JSON Pointer value from THIS request's evidence_locator or supplied evidence; omit anchor, quote and role; never invent a path",
}
EVIDENCE_QUOTE_RULES = (
    "Locate an alleged factual error in current draft_quotes and evidence_quotes, not only a free-text opinion. In evidence_quotes return pointer with the exact absolute JSON Pointer to a supplied scalar field, and omit anchor, quote and role. Copy the locator's path VALUE, not its role:index navigation key. The controller records the actual field value and ownership. This is a field reference, not a model quotation or proof of semantic support.",
    "Actual supplied fields omitted from the bounded locator remain directly selectable with pointer. If you additionally supply a quote, supply its role and keep the quote exact. Pointers are not filesystem paths, URLs or paths into an earlier request. For numbers/bools/null quote the complete JSON value. Changed words/numbers/paths still fail; no automatic correction or dehyphenation, and an invalid supplied quote is never replaced by a field reference.",
    "A required factual finding must quote the current draft. A definite metric_mismatch must also locate non-derived counter-evidence. If a needed observation is unavailable, use evidence_gap/verify and request evidence or a bounded qualification instead of inventing a mismatch. Style/omission findings need not fabricate a quotation from text that does not exist.",
    "Roles are record ownership, not scientific certification: objective/experiment_plan/execution_context and execution_evidence.declared_* are declaration; execution_evidence.execution_records are executor_record; execution_evidence.output_evidence are producer_output; metric_sources and verified_execution_results are registered_result. The historical name verified_execution_results does not certify method correctness.",
    "Length observations, delivery_text_observation, narrative context and assembly-owned content are derived_context: they describe the current delivery or its recorded preview, not independent scientific counter-evidence. They may support length/style findings but cannot by themselves establish a definite metric mismatch.",
    "Source and tool passages/metadata are recorded_material, not proof of an independently verified identity. Reading notes, method summaries and synthesis/interpretation text are derived_context. Do not label declaration or producer output as executor_record. Compare the correct roles; disagreement between a requested condition and observed execution is not itself a false description of that observed execution.",
    "A source pointer/quote confirms location, not semantic support. When evidence is missing, truncated or only derived, request the authorized source tool or retain an explicit gap; do not invent counter-evidence or replace an observed value with a declaration.",
    "Recorded source passage text can contain PDF line wrapping. Only ASCII layout whitespace (spaces, tabs, line breaks) may be collapsed for matching within that single passage. Do not change case, punctuation, signs, numbers, ligatures, hyphenation, identities or paths; do not concatenate different fields or excerpt windows. All other evidence scalars remain literal.",
    "evidence_locator is bounded navigation into evidence already supplied here, not new evidence or a complete source inventory. Use its literal path values in pointer; unlisted available fields remain addressable. Omitted paths do not mean missing observations. Listed roles describe ownership, not semantic support or scientific verification.",
    "A draft_quote may quote the complete current supplied heading or a literal body span of its identified section. Use the full heading, not a truncated section ID or a heading prefix that could fabricate a spelling defect. Frozen-plan headings, prior drafts and omitted neighbor text are not current quotations; do not concatenate separate heading/body or excerpt windows into a fabricated continuous quotation.",
)


def _source_passage_path(path: list[str]) -> bool:
    return path[-1] == "text" and any(key in path for key in
        ("evidence_passages", "chunks", "source_front_matter"))


def _layout_text(text: str) -> str:
    # Not Unicode/fuzzy matching or dehyphenation; preserve all non-layout bytes.
    return re.sub(r"[ \t\r\n\f\v]+", " ", text).strip(" ")


def review_evidence_locator(view: Mapping[str, Any], *, max_chars: int = 4000, max_entries: int = 48) -> dict[str, Any]:
    """Bounded navigation to current scalars, never copied evidence or state.

    Use the validator's ownership rule and pointer escaping. Original passages
    precede metadata within each role; round-robin roles keep one large source
    from hiding observed execution or declared requirements. Every omitted path
    remains directly addressable by the validator, never inferred absent.
    """
    roots = {"objective", "experiment_plan", "execution_context", "execution_evidence", "metric_sources",
             "verified_execution_results", "source_evidence", "allowed_sources", "supplementary_evidence",
             "extra_tool_context", "length_observation", "delivery_text_observation", "assembly_owned_content",
             "narrative_context", "edit_scope", "document_length_budget", "revision_context"}
    candidates: dict[str, list[tuple[int, int, str]]] = {}

    def walk(value: Any, path: list[str]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                walk(child, [*path, str(key)])
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, [*path, str(index)])
        elif isinstance(value, (str, int, float, bool)) or value is None:
            try:
                role = _role(view, path)
            except LLMResponseError:
                return  # Not an eligible evidence owner, not a model-visible assertion.
            pointer = "/" + "/".join(key.replace("~", "~0").replace("/", "~1") for key in path)
            passage = _source_passage_path(path)
            candidates.setdefault(role, []).append((0 if passage else 1, len(path), pointer))

    for key, value in view.items():
        if key in roots:
            walk(value, [key])
    for rows in candidates.values():
        # A long argv/list must not hide its sibling cwd/duration/status. Prefer
        # shallower scalar fields after original passages, without interpreting
        # names, values or a particular task; ties keep current source order.
        rows.sort(key=lambda row: row[:2])
    total = sum(len(rows) for rows in candidates.values())
    result = {"scope": "current_prompt_scalar_navigation_not_evidence_or_verification",
        "pointers_by_role": {}, "listed_scalar_count": 0, "eligible_scalar_count": total,
        "omitted_scalar_count": total, "coverage": "partial" if total else "complete"}
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_chars:
        raise ValueError("Evidence locator budget cannot fit its coverage metadata.")
    groups = result["pointers_by_role"]
    roles = list(candidates)
    for index in range(max((len(rows) for rows in candidates.values()), default=0)):
        for role in roles:
            if result["listed_scalar_count"] >= max(0, max_entries):
                return result
            if index >= len(candidates[role]):
                continue
            pointer = candidates[role][index][2]
            # Show the selectable key beside its path, rather than requiring
            # the model to count a JSON array. This replaces the presentation;
            # canonical saved evidence remains the actual pointer and value.
            group = groups.setdefault(role, {})
            anchor = f"{role}:{index}"
            group[anchor] = pointer
            result["listed_scalar_count"] += 1
            result["omitted_scalar_count"] -= 1
            result["coverage"] = "partial" if result["omitted_scalar_count"] else "complete"
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_chars:
                group.pop(anchor)
                if not group:
                    groups.pop(role)
                result["listed_scalar_count"] -= 1
                result["omitted_scalar_count"] += 1
                result["coverage"] = "partial"
    return result


def review_draft_quote_sources(view: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Literal current fields actually shown to this role, without joining them.

    Full-document reviews expose whole bodies; section reviews expose a current
    draft plus bounded neighbor windows. The current candidate overrides any
    same-ID neighbor, and frozen plans/previous drafts never authorize a quote.
    This is a request-local projection, not new persisted evidence or state.
    """
    sources: dict[str, tuple[str, ...]] = {}
    for row in view.get("narrative_context", {}).get("adopted_sections", []):
        values = [row.get("heading", "")]
        values.extend(window.get("text", "") for window in row.get("prose_windows", []))
        values.extend(window.get("text", "") for window in row.get("table_excerpt", {}).get("windows", []))
        sources[row["section_id"]] = tuple(values)
    for row in view.get("sections", []):
        sources[row["section_id"]] = (row.get("heading", ""), row.get("markdown", ""))
    draft = view.get("draft")
    if isinstance(draft, Mapping) and draft.get("section_id"):
        sources[draft["section_id"]] = (draft.get("heading", ""), draft.get("draft_markdown", ""))
    return sources


def draft_quote_present(sources: Mapping[str, str | tuple[str, ...]], section_id: str, quote: str) -> bool:
    values = sources.get(section_id, ())
    if isinstance(values, str):
        return bool(quote) and quote in values  # Legacy body-only callers.
    # Headings are short identity-bearing fields; a prefix must not masquerade
    # as a complete misspelled title. Body/window spans retain literal matching.
    return bool(quote) and bool(values) and (quote == values[0] or any(quote in value for value in values[1:]))


def _resolve_pointer(view: Mapping[str, Any], pointer: str, *, allow_empty: bool = False,
                     allow_list: bool = False) -> tuple[list[str], Any]:
    # Bound producer metadata can opt into lists; review quotes stay scalar.
    def location(value: Any, path: list[str]) -> str:
        parent = "/" + "/".join(key.replace("~", "~0").replace("/", "~1") for key in path)
        if isinstance(value, Mapping):
            keys = list(value)
            return (f"Available keys at {parent}: {json.dumps(keys[:16], ensure_ascii=False)}"
                    + (f" ({len(keys) - 16} additional keys omitted)" if len(keys) > 16 else ""))
        if isinstance(value, list):
            return f"Array at {parent} has {len(value)} elements; use an existing canonical index"
        return f"{parent} is already a scalar, not a child container"

    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise LLMResponseError("Review evidence needs an absolute JSON Pointer into the current prompt.")
    raw = pointer[1:].split("/")
    if any(re.search(r"~(?:[^01]|$)", segment) for segment in raw):
        raise LLMResponseError("Review evidence pointer contains an invalid escape.")
    segments = [segment.replace("~1", "/").replace("~0", "~") for segment in raw]
    value: Any = view
    consumed: list[str] = []
    for segment in segments:
        if isinstance(value, Mapping) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", segment) and int(segment) < len(value):
            value = value[int(segment)]
        else:
            raise LLMResponseError(f"Review evidence pointer is absent from the current prompt: {pointer}. {location(value, consumed)}. No pointer or quote was repaired.")
        consumed.append(segment)
    if isinstance(value, (Mapping, list)) and not (allow_list and isinstance(value, list)) and not (
            allow_empty and not value and view.get("evidence_reference_contract") == GAP_EVIDENCE_CONTRACT):
        raise LLMResponseError(f"Review evidence must locate a scalar field, not a whole container: {pointer}. {location(value, segments)}. Select a supplied scalar explicitly; no pointer or quote was repaired.")
    return segments, value


def _role(view: Mapping[str, Any], path: list[str]) -> str:
    root, *rest = path
    if root in {"objective", "experiment_plan", "execution_context"}:
        return "declaration"
    if root in {"length_observation", "edit_scope", "document_length_budget", "narrative_context", "assembly_owned_content", "delivery_text_observation"}:
        return "derived_context"
    if root == "revision_context" and rest and rest[0] == "length_observation":
        return "derived_context"
    if root == "execution_evidence" and rest:
        key = rest[0]
        if key in {"declared_protocol", "declared_execution_context"}:
            return "declaration"
        if key == "execution_records":
            return "executor_record"
        if key == "output_evidence":
            return "producer_output"
        if key == "implementation_verification":
            return "registered_result"
    if root in {"metric_sources", "verified_execution_results"}:
        return "derived_context" if "interpretation" in rest else "registered_result"
    if root in {"source_evidence", "allowed_sources"}:
        # Summaries/reading cards are not the original cited passage.
        return "derived_context" if any(key in rest for key in (
            "summary", "reading_notes", "derived_reading_notes_on_request", "method", "contribution", "evaluation", "relevance")) else "recorded_material"
    if root in {"supplementary_evidence", "extra_tool_context"} and rest and rest[0].isdigit():
        tool = view[root][int(rest[0])]
        if tool.get("tool_name") == "get_synthesis_brief" and "text" in rest:
            return "derived_context"
        if any(key in rest for key in ("reading_notes", "derived_reading_notes_on_request", "summary", "interpretation")):
            return "derived_context"
        if tool.get("tool_name") == "get_metric_source":
            return "registered_result"
        if tool.get("tool_name") == "get_code_task_result":
            if "results" not in rest and "metric_sources" not in rest:
                return "producer_output"
            return "registered_result"
        return "recorded_material"
    raise LLMResponseError(f"Review evidence pointer does not identify a supplied evidence/requirement field: /{'/'.join(path)}.")


def validate_evidence_quotes(quotes: list[ReportEvidenceQuote], view: Mapping[str, Any], *, allow_empty: bool = False) -> None:
    for reference in quotes:
        path, value = _resolve_pointer(view, reference.pointer, allow_empty=allow_empty)
        quote = reference.quote if reference.mode == "field_reference" else reference.quote.strip()
        expected = _role(view, path)
        present = (quote == value if reference.mode == "field_reference" else quote in value) if isinstance(value, str) else quote == json.dumps(value, ensure_ascii=False)
        if (reference.mode == "quotation" and not present and isinstance(value, str) and expected == "recorded_material"
                and _source_passage_path(path)):
            present = bool(_layout_text(quote)) and _layout_text(quote) in _layout_text(value)
        if (reference.mode == "quotation" and not quote) or not present:
            raise LLMResponseError(f"Review evidence quote is absent or changed at {reference.pointer}.")
        if reference.role != expected:
            raise LLMResponseError(f"Review evidence role is {expected}, not {reference.role}, at {reference.pointer}.")


def resolve_review_evidence(response: Mapping[str, Any], view: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve located field selections before model validation/persistence.

    A short selection uses the visible locator; a direct pointer resolves the
    current view. Never repair a pointer, mutate the raw response, or retain a
    short alias in a checkpoint.
    Persisting the actual value prevents restoration from silently rebinding a
    field reference. An optional model quote remains a strict quotation.
    """
    normalized = dict(response)
    for collection in ("findings", "finding_checks"):
        rows = response.get(collection)
        if not isinstance(rows, list):
            continue  # The existing schema owns malformed review structures.
        normalized_rows = []
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("evidence_quotes"), list):
                normalized_rows.append(row)
                continue
            references = []
            for reference in row["evidence_quotes"]:
                if isinstance(reference, Mapping) and reference.get("mode", "quotation") != "quotation":
                    raise LLMResponseError("Field-reference mode is controller-owned, not a model quotation mode.")
                if not isinstance(reference, Mapping):
                    references.append(reference)
                    continue
                anchor_role = None
                if "anchor" in reference:
                    anchor = reference["anchor"]
                    match = re.fullmatch(r"([a-z_]+):(0|[1-9][0-9]*)", anchor) if isinstance(anchor, str) else None
                    groups = view.get("evidence_locator", {}).get("pointers_by_role", {})
                    paths = groups.get(match[1], {}) if match and isinstance(groups, Mapping) else {}
                    if isinstance(paths, Mapping):
                        pointer = paths.get(anchor)
                    elif isinstance(paths, list) and match and int(match[2]) < len(paths):
                        # Retained requests used zero-based arrays. Resolve that
                        # exact request only; never guess an off-by-one answer.
                        pointer = paths[int(match[2])]
                    else:
                        pointer = None
                    if not match or pointer is None:
                        raise LLMResponseError("Review evidence anchor is not listed in this request.")
                    anchor_role = match[1]
                elif "pointer" in reference and "quote" not in reference:
                    pointer = reference["pointer"]
                else:
                    references.append(reference)
                    continue  # Literal quotations retain their existing schema and checks.
                path, value = _resolve_pointer(view, pointer, allow_empty=(collection == "findings"
                    and _allows_empty_gap(view, row.get("type"), row.get("required_action"))))
                role = _role(view, path)
                if anchor_role is not None and role != anchor_role:
                    raise LLMResponseError("Review evidence locator role does not match the selected field.")
                if "pointer" in reference and reference["pointer"] != pointer:
                    raise LLMResponseError("Review evidence anchor and pointer disagree; neither was repaired.")
                if "role" in reference and reference["role"] != role:
                    raise LLMResponseError(f"Review evidence role is {role}, not {reference['role']}, at {pointer}.")
                selected = {key: val for key, val in reference.items() if key != "anchor"}
                selected.update(pointer=pointer, role=role)
                if "quote" not in reference:
                    selected.update(quote=value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
                                    mode="field_reference")
                references.append(selected)
            normalized_rows.append({**row, "evidence_quotes": references})
        normalized[collection] = normalized_rows
    return normalized


def validate_finding_anchors(review: ReportSectionReview, drafts: Mapping[str, str | tuple[str, ...]], view: Mapping[str, Any]) -> None:
    for finding in review.findings:
        for reference in finding.draft_quotes:
            quote = reference.quote.strip()
            if not draft_quote_present(drafts, reference.section_id, quote):
                raise LLMResponseError("Review finding quoted text absent from the current section.")
        validate_evidence_quotes(finding.evidence_quotes, view, allow_empty=
            _allows_empty_gap(view, finding.type, finding.required_action))
        if finding_requires_resolution(finding) and finding.type in {*FACTUAL_REVIEW_FINDING_TYPES, "evidence_gap"}:
            if not finding.draft_quotes:
                raise LLMResponseError("Required factual review finding needs a current-draft quotation.")
            if finding.type == "metric_mismatch" and not any(
                    quote.role != "derived_context" for quote in finding.evidence_quotes):
                raise LLMResponseError("A definite metric mismatch needs located non-derived counter-evidence; otherwise request verification rather than assert a mismatch.")
