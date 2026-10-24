"""Literal review anchors into an existing prompt, not another evidence store.

Roles describe record ownership, not truth. A valid quote does not prove that a
reviewer's interpretation follows; absent/truncated evidence is still unknown.
"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from collections.abc import Mapping
from typing import Any

from simple_ar.integrations.llm import LLMResponseError
from simple_ar.report.schema import (
    FACTUAL_REVIEW_FINDING_TYPES, ReportEvidenceQuote, ReportSectionReview, finding_requires_resolution,
)

GAP_EVIDENCE_CONTRACT = "empty_gap_observation.v1"
DRAFT_REFERENCE_CONTRACT = "current_draft_paragraphs.v1"
COMPARISON_VERIFICATION_CONTRACT = "unproven_comparison_requires_verification.v1"
LOCATOR_REFERENCE_CONTRACT = "current_scalar_locator.v1"
SHARED_PRODUCER_LOCATOR_CONTRACT = "current_scalar_locator.v2"
EVIDENCE_NAVIGATION_CONTRACTS = (None, LOCATOR_REFERENCE_CONTRACT, SHARED_PRODUCER_LOCATOR_CONTRACT)
DRAFT_QUOTE_SCHEMA = {"section_id": "current supplied section id", "paragraph": "pN from draft_locator"}


def draft_paragraph_spans(markdown: str) -> list[tuple[int, int]]:
    """Offsets of the stripped v1 paragraphs in the untouched current body."""
    spans = []
    start = 0
    for boundary in [*re.finditer(r"\r?\n[ \t]*\r?\n", markdown), None]:
        end = boundary.start() if boundary is not None else len(markdown)
        part = markdown[start:end]
        if part.strip():
            spans.append((start + len(part) - len(part.lstrip()),
                          end - len(part) + len(part.rstrip())))
        start = boundary.end() if boundary is not None else end
    return spans


def _draft_paragraphs(view: Mapping[str, Any]) -> dict[str, list[str]]:
    """v1: nonempty, stripped blank-line-separated Markdown spans, numbered from 1.

    Only the frozen request's full current sections own these identities. Never
    number historical quotations, plans, headings or bounded neighbor windows.
    """
    return {row["section_id"]: [row.get("markdown", "")[start:end] for start, end in
            draft_paragraph_spans(row.get("markdown", ""))]
            for row in view.get("sections", [])}


def review_draft_locator(view: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded navigation only; exact text remains in the supplied sections."""
    return {sid: {"paragraph_count": len(parts),
                  "previews": {f"p{i}": part[:80] for i, part in enumerate(parts[:24], 1)},
                  "omitted_preview_count": max(0, len(parts) - 24)}
            for sid, parts in _draft_paragraphs(view).items()}


def _resolve_draft_quotes(row: Mapping[str, Any], view: Mapping[str, Any]) -> dict[str, Any]:
    if "draft_reference_contract" not in view:
        return dict(row)  # Frozen old requests retain their original semantics.
    if view["draft_reference_contract"] != DRAFT_REFERENCE_CONTRACT:
        raise LLMResponseError("Unknown current draft reference contract.")
    quotes = row.get("draft_quotes")
    if not isinstance(quotes, list):
        return dict(row)  # Existing schema owns malformed/missing lists.
    paragraphs = _draft_paragraphs(view)
    resolved = []
    for reference in quotes:
        if not isinstance(reference, Mapping):
            resolved.append(reference)
            continue
        keys = set(reference)
        if "paragraph" not in reference:
            if keys - {"section_id", "quote"}:
                raise LLMResponseError("Unknown draft quotation selector.")
            resolved.append(reference)  # Literal quotations are never repaired.
            continue
        if keys != {"section_id", "paragraph"}:
            raise LLMResponseError("Draft paragraph selection cannot mix quotation or other selectors.")
        selector = reference["paragraph"]
        match = re.fullmatch(r"p([1-9][0-9]*)", selector) if isinstance(selector, str) else None
        sid = reference["section_id"]
        parts = paragraphs.get(sid, []) if isinstance(sid, str) else []
        if not match or int(match[1]) > len(parts):
            raise LLMResponseError("Draft paragraph must select an existing current section paragraph.")
        resolved.append({"section_id": sid, "quote": parts[int(match[1]) - 1]})
    return {**row, "draft_quotes": resolved}


DELIVERY_REVIEW_CONTRACT = 'delivery_artifacts.v1'
SCOPED_DELIVERY_INSPECTION = 'delivery_inspection.v1'
_PROFILE_SCOPE = ('Complete bounded-table reader statistics for shape, missingness and raw-value '
    'cardinality of displayed columns, plus bounded examples. This is not full text or a '
    'value-by-value review; values, meanings, units, pairing and statistical conclusions are not verified.')
DECLARATION_ADOPTION_CONTRACT = 'producer_declarations.v1'
CLAIM_ADOPTION_V2 = 'producer_declarations.v2'
DECLARATION_ADOPTION_CONTRACTS = {DECLARATION_ADOPTION_CONTRACT, CLAIM_ADOPTION_V2}


def declaration_adoption_contract(snapshot: Mapping[str, Any]) -> str:
    contract = snapshot.get('declaration_adoption_contract', DECLARATION_ADOPTION_CONTRACT)
    if contract not in DECLARATION_ADOPTION_CONTRACTS:
        raise ValueError('Unknown producer declaration adoption contract')
    return contract
DELIVERY_COPY_CONTRACT = 'delivery_copies.v1'


def project_delivery_producers(artifacts: list[dict], *,
                               producer_table_pointer: str = '/delivery_producers') -> dict[str, Any]:
    """Frozen current_scalar_locator.v2 wire view, local to one request.

    Preserve row order and every field except replacing each present producer
    with {"$ref": "<producer_table_pointer>/N"} (default /delivery_producers).
    The caller places the table at that physical JSON pointer. It holds complete JSON
    records in first-occurrence order, equal by canonical JSON (not file SHA,
    path, role or a partial producer identity). Missing and false producers stay
    distinct. Public pointers address the physical view; no virtual aliases.
    This is presentation only, never a replacement for frozen repair subjects.
    """
    producers: list[Any] = []
    indices: dict[str, int] = {}
    rows = []
    for artifact in artifacts:
        row = deepcopy(artifact)
        if 'producer' in row:
            key = json.dumps(row['producer'], sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False)
            if key not in indices:
                indices[key] = len(producers)
                producers.append(row['producer'])
            row['producer'] = {'$ref': f'{producer_table_pointer}/{indices[key]}'}
        rows.append(row)
    return {'delivery_artifacts': rows, 'delivery_producers': producers}


def project_document_feedback(diagnosis: dict) -> dict:
    """Lossless model view, embedded as {'source_feedback': returned_view}.

    Only delivery artifact producers become references to complete shared JSON
    records. All other diagnosis fields remain literal, including unknowns and
    findings. This grants no inspection or adoption authority. Callers own the
    version gate for saved model snapshots; the original diagnosis is untouched.
    """
    view = deepcopy(diagnosis)
    if not isinstance(view, dict):
        raise ValueError('Document feedback diagnosis must be an object')
    review = view.get('review')
    if review is None:
        return view
    if not isinstance(review, dict):
        raise ValueError('Document feedback review must be an object or null')
    delivery = review.get('delivery_review')
    if delivery is None:
        return view
    if not isinstance(delivery, dict):
        raise ValueError('Document feedback delivery review must be an object or null')
    if 'artifacts' not in delivery:
        return view
    if not isinstance(delivery['artifacts'], list) or any(
            not isinstance(row, dict) for row in delivery['artifacts']):
        raise ValueError('Document feedback artifacts must be a list of objects')
    if 'delivery_producers' in view:
        raise ValueError('Document feedback producer table would overwrite an original field')
    projection = project_delivery_producers(delivery['artifacts'],
        producer_table_pointer='/source_feedback/delivery_producers')
    delivery['artifacts'] = projection['delivery_artifacts']
    view['delivery_producers'] = projection['delivery_producers']
    return view


def _delivery_producer(view: Mapping[str, Any], row: Mapping[str, Any]) -> Any:
    if (view.get('evidence_navigation_contract') != SHARED_PRODUCER_LOCATOR_CONTRACT
            or 'producer' not in row):
        return row.get('producer')
    reference = row['producer']
    pointer = reference.get('$ref') if isinstance(reference, Mapping) else None
    match = re.fullmatch(r'/delivery_producers/(0|[1-9][0-9]*)', pointer) if isinstance(pointer, str) else None
    producers = view.get('delivery_producers')
    if (not match or set(reference) != {'$ref'} or not isinstance(producers, list)
            or int(match[1]) >= len(producers)):
        raise LLMResponseError('Invalid current delivery producer reference; no identity was repaired.')
    return producers[int(match[1])]


def restore_delivery_producers(view: Mapping[str, Any]) -> list[dict]:
    """Losslessly expand v2 rows; legacy requests retain their literal records."""
    rows = []
    for artifact in view.get('delivery_artifacts', []):
        row = deepcopy(artifact)
        if 'producer' in row:
            row['producer'] = deepcopy(_delivery_producer(view, artifact))
        rows.append(row)
    return rows


def delivery_target_pointer(row: Mapping[str, Any], index: int) -> str | None:
    """Select only the actual frozen inspection view; old rows remain text-only.

    The byte owner binds the profile to the full original SHA. Here we validate
    that binding and the shared reader's bounded data shape, never reread files
    or treat a matching digest as scientific verification.
    """
    if row.get('coverage') == 'complete' and isinstance(row.get('text'), str):
        return f'/delivery_artifacts/{index}/text'
    if (row.get('inspection_contract') != SCOPED_DELIVERY_INSPECTION
            or row.get('coverage') != 'table_profile'):
        return None
    profile = row.get('profile')
    required = {'source_sha256', 'row_count', 'columns', 'column_summary', 'example_rows',
        'columns_omitted', 'column_names_truncated', 'cell_values_truncated', 'status'}
    if not isinstance(profile, dict) or set(profile) != required:
        return None
    sha = row.get('sha256')
    if (not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha)
            or profile['source_sha256'] != sha):
        return None
    count, columns, summaries, examples = (profile[key] for key in
        ('row_count', 'columns', 'column_summary', 'example_rows'))
    if (type(count) is not int or count < 1 or not isinstance(columns, list)
            or not 1 <= len(columns) <= 40
            or any(not isinstance(name, str) or len(name) > 200 for name in columns)
            or not isinstance(summaries, list) or len(summaries) != len(columns)
            or not isinstance(examples, list) or len(examples) != min(count, 5)
            or type(profile['columns_omitted']) is not int or profile['columns_omitted'] < 0
            or (profile['columns_omitted'] > 0 and len(columns) != 40)
            or type(profile['column_names_truncated']) is not bool
            or type(profile['cell_values_truncated']) is not bool
            or not isinstance(profile['status'], str) or not profile['status'].strip()):
        return None
    for name, summary in zip(columns, summaries):
        if (not isinstance(summary, dict)
                or set(summary) != {'column', 'column_name_truncated', 'missing', 'distinct_nonmissing'}
                or summary['column'] != name or type(summary['column_name_truncated']) is not bool
                or (summary['column_name_truncated'] and len(name) != 200)
                or type(summary['missing']) is not int or not 0 <= summary['missing'] <= count
                or type(summary['distinct_nonmissing']) is not int
                or not 0 <= summary['distinct_nonmissing'] <= count - summary['missing']
                or ((summary['distinct_nonmissing'] == 0) != (summary['missing'] == count))):
            return None
    if (profile['column_names_truncated'] != any(s['column_name_truncated'] for s in summaries)
            or (len(set(columns)) != len(columns) and not profile['column_names_truncated'])
            or any(not isinstance(example, dict) or set(example) != set(columns)
                or any(not isinstance(value, str) or len(value) > 200 for value in example.values())
                for example in examples)):
        return None
    return f'/delivery_artifacts/{index}/profile'


def delivery_subject_view(row: Mapping[str, Any], *, coverage: bool = False) -> dict:
    """Project frozen subjects, retaining only explicitly bound copy aliases.

    An alias shares the inspected bytes, never the original's producer role.
    Filesystem/version admission belongs to the writing byte owner.
    """
    keys = (('path', 'sha256', 'coverage', 'producer', 'consumers', 'visual_reviewed') if coverage
        else ('path', 'sha256', 'producer', 'consumers'))
    result = {key: row[key] for key in keys if key in row}
    if coverage and row.get('inspection_contract') == SCOPED_DELIVERY_INSPECTION:
        result['inspection_contract'] = SCOPED_DELIVERY_INSPECTION
        if delivery_target_pointer(row, 0) == '/delivery_artifacts/0/profile':
            result['profile_scope'] = {'scope': _PROFILE_SCOPE,
                'displayed_column_count': len(row['profile']['columns']), **{key: row['profile'][key]
                for key in ('row_count', 'columns_omitted', 'column_names_truncated', 'cell_values_truncated')}}
    if 'copies' in row or 'copy_contract' in row:
        if row.get('copy_contract') != DELIVERY_COPY_CONTRACT or not isinstance(row.get('copies'), list):
            raise ValueError('Unknown or incomplete delivery copy contract')
        seen = {row['path']}
        for copy in row['copies']:
            if (not isinstance(copy, dict) or set(copy) != {'path', 'sha256'}
                    or not isinstance(copy['path'], str) or not copy['path']
                    or copy['path'] in seen or copy['sha256'] != row['sha256']):
                raise ValueError('Delivery copy differs from its inspected source version')
            seen.add(copy['path'])
        result.update(copy_contract=DELIVERY_COPY_CONTRACT, copies=row['copies'],
            copy_scope=row.get('copy_scope', 'Byte-equivalent delivery copies; producer and execution roles belong only to the original subject.'))
    return result


def attachment_review_blocks_editing(inspection: dict) -> bool:
    """Prose may fix disclosure, never bad/unobserved attachment bytes."""
    contract = inspection.get('declaration_adoption', {}).get('contract')
    if contract is not None and contract not in DECLARATION_ADOPTION_CONTRACTS:
        raise ValueError('Unknown producer declaration adoption contract')
    if contract is None:
        scoped = (inspection.get('inspection_contract') == SCOPED_DELIVERY_INSPECTION
            or any(row.get('inspection_contract') == SCOPED_DELIVERY_INSPECTION
                   for row in inspection.get('artifacts', [])))
        # Restored historical inspections keep their status-only admission,
        # including the original missing-status default. New scope is opt-in.
        return (scoped and bool(inspection.get('defects') or inspection.get('uninspected'))
            or inspection.get('status', 'reviewed') != 'reviewed')
    return (bool(inspection.get('defects') or inspection.get('uninspected'))
        or inspection.get('status') not in {'reviewed', 'needs_revision'})


def delivery_finding_targets(finding: Any, artifacts: list[dict]) -> list[dict]:
    """Resolve explicit accused subjects, never infer them from evidence quotes."""
    targets = []
    pointers = getattr(finding, 'delivery_targets', [])
    if len(pointers) != len(set(pointers)):
        raise LLMResponseError('Delivery defect targets must be distinct current inspection pointers.')
    for pointer in pointers:
        match = re.fullmatch(r'/delivery_artifacts/(0|[1-9][0-9]*)/(text|profile)', pointer)
        if match is None or int(match[1]) >= len(artifacts):
            raise LLMResponseError('Delivery defect target must select an existing frozen attachment text or profile view.')
        row = artifacts[int(match[1])]
        if delivery_target_pointer(row, int(match[1])) != pointer:
            raise LLMResponseError('Delivery defect target has no valid current inspection view.')
        targets.append(row)
    return targets


def delivery_review_result(snapshot: dict, reviews: list[ReportSectionReview]) -> dict:
    """Bind validated findings to the exact frozen attachment versions."""
    contract = declaration_adoption_contract(snapshot)
    artifacts = snapshot['artifacts']
    defects = []
    for review in reviews:
        for finding in review.findings:
            targets = delivery_finding_targets(finding, artifacts)
            if targets and finding_requires_resolution(finding):
                defects.append({'type': 'delivery_artifact_defect',
                    'finding': finding.model_dump(mode='json'),
                    'targets': [delivery_subject_view(row) for row in targets]})
    current = {row['path']: row['sha256'] for row in artifacts}
    for row in artifacts:
        projected = delivery_subject_view(row)
        for copy in projected.get('copies', []):
            if copy['path'] in current and current[copy['path']] != copy['sha256']:
                raise ValueError('Delivery aliases disagree on a file version')
            current[copy['path']] = copy['sha256']
    for previous in snapshot.get('prior_defects', []):
        if not previous.get('finding', {}).get('delivery_targets'):
            continue  # Old inferred defects never gain explicit-target repair authority.
        # A different manuscript or an omitted reviewer opinion cannot repair
        # unchanged defective attachment bytes. New versions need a new review.
        if any(current.get(row['path']) == row['sha256'] for row in previous['targets']):
            if previous not in defects:
                defects.append(previous)
    missing = [{key: row[key] for key in ('path', 'sha256', 'coverage')} for i, row in enumerate(artifacts)
               if row['coverage'] != 'visual_uninspected' and not delivery_target_pointer(row, i)
               and not (row.get('inspection_contract') != SCOPED_DELIVERY_INSPECTION
                        and row['coverage'] == 'complete')]
    result = {'contract': DELIVERY_REVIEW_CONTRACT,
        'status': 'needs_revision' if defects else 'incomplete' if missing else 'reviewed',
        'artifacts': [delivery_subject_view(row, coverage=True) for row in artifacts],
        'defects': defects, 'uninspected': missing,
        'visual_pending': [{key: row[key] for key in ('path', 'sha256')} for row in artifacts
                           if row['coverage'] == 'visual_uninspected'],
        'scope': 'Text attachments and prose only. Figure versions are bound for the separate visual owner; reviewed does not certify images or scientific validity.'}
    if (snapshot.get('text_admission', {}).get('contract') == SCOPED_DELIVERY_INSPECTION
            or any(row.get('inspection_contract') == SCOPED_DELIVERY_INSPECTION for row in artifacts)):
        result['inspection_contract'] = SCOPED_DELIVERY_INSPECTION
        result['scope_limits'] = [_PROFILE_SCOPE,
            'Reviewed means inspection of the supplied admissible views is complete; '
            'unsupported, incomplete or oversized attachments remain uninspected. '
            'Figure inspection belongs to the separate visual owner.']
        result['profiled_artifacts'] = [
            {'path': row['path'], 'sha256': row['sha256'],
             'reader_coverage': delivery_subject_view(row, coverage=True)['profile_scope'],
             'unverified': ['all individual values', 'column meanings', 'units', 'pairing',
                            'independence', 'statistical conclusions']}
            for i, row in enumerate(artifacts)
            if delivery_target_pointer(row, i) == f'/delivery_artifacts/{i}/profile']
        result['scope'] = ('Inspection of supplied full text and SHA-bound table profile views only. '
            'Reviewed means the supplied views were checked, not all table values or scientific '
            'conclusions. See scope_limits and profiled_artifacts; figures have a separate visual owner.')
    if snapshot.get('producer_completion') == 'goal_findings.v1':
        result['producer_completion'] = 'goal_findings.v1'
        result['producer_notes'] = snapshot['producer_notes']
    if 'prior_defect_refs' in snapshot:
        result['prior_defect_refs'] = snapshot['prior_defect_refs']
    if 'producer_declarations' in snapshot:
        groups = snapshot['producer_declarations']
        ids = [row['id'] for group in groups for row in group['declarations']]
        checks = [check.model_dump(mode='json') for review in reviews for check in review.producer_claim_checks]
        result['declaration_adoption'] = {'contract': contract,
            'producers': groups, 'checks': checks}
        if any(row['status'] == 'unresolved_work' for row in checks):
            result['status'] = 'needs_revision'
        elif (len(checks) != len(ids) or {row['declaration_id'] for row in checks} != set(ids)):
            result['status'] = 'incomplete'
    return result


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
             "narrative_context", "edit_scope", "document_length_budget", "revision_context", "delivery_artifacts",
             "producer_declarations", "producer_notes"}
    if view.get('evidence_navigation_contract') == SHARED_PRODUCER_LOCATOR_CONTRACT:
        roots.add('delivery_producers')
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
    if root == 'producer_notes' and view.get('producer_completion') == 'goal_findings.v1':
        return 'executor_record' if len(rest) > 1 and rest[1] == 'execution' else 'declaration'
    if root == 'delivery_producers' and view.get('evidence_navigation_contract') == SHARED_PRODUCER_LOCATOR_CONTRACT:
        return 'derived_context'
    if root == 'producer_declarations' and view.get('declaration_adoption_contract') in DECLARATION_ADOPTION_CONTRACTS:
        return 'executor_record' if len(rest) > 1 and rest[1] == 'execution' else 'declaration'
    if root == 'delivery_artifacts' and view.get('delivery_review_contract') == DELIVERY_REVIEW_CONTRACT:
        if len(rest) == 2 and rest[-1] == 'text':
            return 'producer_output' if _delivery_producer(view, view[root][int(rest[0])]) else 'recorded_material'
        if (len(rest) >= 3 and rest[1] == 'profile'
                and delivery_target_pointer(view[root][int(rest[0])], int(rest[0]))
                    == f'/delivery_artifacts/{rest[0]}/profile'
                and (rest[2] in {'row_count', 'columns', 'example_rows'}
                     or (rest[2] == 'column_summary' and len(rest) == 5
                         and rest[4] in {'column', 'missing', 'distinct_nonmissing'}))):
            return 'registered_result'
        return 'derived_context'
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
    if 'declaration_adoption_contract' in view:
        declaration_adoption_contract(view)
    if ('draft_reference_contract' in view
            and view['draft_reference_contract'] != DRAFT_REFERENCE_CONTRACT):
        raise LLMResponseError("Unknown current draft reference contract.")
    if view.get('comparison_verification_contract') not in (None, COMPARISON_VERIFICATION_CONTRACT):
        raise LLMResponseError('Unknown comparison verification contract.')
    if view.get('evidence_navigation_contract') not in EVIDENCE_NAVIGATION_CONTRACTS:
        raise LLMResponseError('Unknown evidence navigation contract.')
    normalized = dict(response)
    for collection in ("findings", "finding_checks", "producer_claim_checks"):
        rows = response.get(collection)
        if not isinstance(rows, list):
            continue  # The existing schema owns malformed review structures.
        normalized_rows = []
        for row in rows:
            if isinstance(row, Mapping):
                row = _resolve_draft_quotes(row, view)
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
            resolved = {**row, "evidence_quotes": references}
            if (collection == 'findings' and row.get('type') == 'metric_mismatch'
                    and view.get('comparison_verification_contract') == COMPARISON_VERIFICATION_CONTRACT):
                quotes = [ReportEvidenceQuote.model_validate(item) for item in references]
                # An invalid pointer/quotation still fails. A valid located
                # allegation without counter-evidence is work to investigate,
                # never an established error, permission to patch, or a pass.
                validate_evidence_quotes(quotes, view)
                if quotes and not any(quote.role != 'derived_context'
                        and not _targets_contain_evidence(row.get('delivery_targets', []), quote.pointer)
                        for quote in quotes):
                    resolved.update(type='evidence_gap', required_action='verify',
                        message='Unverified metric comparison (no located independent counter-evidence): '
                            + str(row.get('message', '')),
                        suggested_action='Investigate the retained allegation against original records and '
                            'actual producer outputs. Do not accept it as fact, dismiss it, or merely '
                            'qualify the prose without checking. ' + str(row.get('suggested_action', '')))
            normalized_rows.append(resolved)
        normalized[collection] = normalized_rows
    return normalized


def _targets_contain_evidence(targets: list[str], pointer: str) -> bool:
    return any(pointer == target or pointer.startswith(target + '/') for target in targets)


def validate_finding_anchors(review: ReportSectionReview, drafts: Mapping[str, str | tuple[str, ...]], view: Mapping[str, Any]) -> None:
    for finding in review.findings:
        material_targets = []
        if finding.delivery_targets:
            if view.get('delivery_review_contract') != DELIVERY_REVIEW_CONTRACT:
                raise LLMResponseError('Delivery targets require the current frozen attachment inspection contract.')
            material_targets = delivery_finding_targets(finding, view.get('delivery_artifacts', []))
            # The central selector validates text scalars and complete profile
            # objects. Evidence quotations still must select scalar fields.
        for reference in finding.draft_quotes:
            quote = reference.quote.strip()
            if not draft_quote_present(drafts, reference.section_id, quote):
                raise LLMResponseError("Review finding quoted text absent from the current section.")
        validate_evidence_quotes(finding.evidence_quotes, view, allow_empty=
            _allows_empty_gap(view, finding.type, finding.required_action))
        if finding_requires_resolution(finding) and finding.type in {*FACTUAL_REVIEW_FINDING_TYPES, "evidence_gap"}:
            if not finding.draft_quotes and not material_targets:
                raise LLMResponseError("Required factual review finding needs a current-draft quotation.")
            if finding.type == "metric_mismatch" and not any(
                    quote.role != "derived_context"
                    and not _targets_contain_evidence(finding.delivery_targets, quote.pointer)
                    for quote in finding.evidence_quotes):
                raise LLMResponseError("A definite metric mismatch needs located non-derived counter-evidence; otherwise request verification rather than assert a mismatch.")


def validate_producer_claim_checks(reviews: list[ReportSectionReview], drafts: Mapping,
                                   view: Mapping[str, Any], *, complete: bool = True) -> None:
    if view.get('producer_completion') == 'goal_findings.v1':
        if 'producer_declarations' not in view:
            if any(review.producer_claim_checks for review in reviews):
                raise LLMResponseError('Goal/findings review does not accept declaration adoption checks.')
            return
        # Imported historical producers retain their own explicit v1/v2 gate.
    groups = view.get('producer_declarations', [])
    contract = declaration_adoption_contract(view)
    declarations = {row['id']: (index, row) for index, group in enumerate(groups)
                    for row in group['declarations']}
    if contract == CLAIM_ADOPTION_V2 and len(declarations) != sum(len(group['declarations']) for group in groups):
        raise LLMResponseError('Producer declaration identities must be unique.')
    seen = set()
    for review in reviews:
        for check in review.producer_claim_checks:
            if (view.get('declaration_adoption_contract') not in DECLARATION_ADOPTION_CONTRACTS
                    or check.declaration_id not in declarations or check.declaration_id in seen):
                raise LLMResponseError('Producer declaration check targets an unknown or repeated original statement.')
            seen.add(check.declaration_id)
            index, statement = declarations[check.declaration_id]
            for quote in check.draft_quotes:
                if not draft_quote_present(drafts, quote.section_id, quote.quote):
                    raise LLMResponseError('Producer declaration adoption quotes text absent from the current manuscript.')
            validate_evidence_quotes(check.evidence_quotes, view)
            if not check.explanation.strip():
                raise LLMResponseError('Producer declaration adoption requires an evidence-based explanation.')
            if check.status == 'accepted_limitation' and not check.draft_quotes:
                raise LLMResponseError('Adopting a research limitation requires its disclosure in the current manuscript.')
            if contract == DECLARATION_ADOPTION_CONTRACT and not any(quote.pointer.startswith(f'/producer_declarations/{index}/execution/')
                       and quote.role == 'executor_record' for quote in check.evidence_quotes):
                raise LLMResponseError('Producer declaration check must locate facts from its own execution, not another producer.')
            if (contract == CLAIM_ADOPTION_V2 and check.status == 'satisfied_by_execution'
                    and not any(quote.role == 'executor_record' for quote in check.evidence_quotes)):
                raise LLMResponseError('Execution satisfaction requires actual supplied executor-record support.')
            if check.status == 'unresolved_work' and (review.verdict not in {'revise_required', 'fail'}
                    or not any(finding_requires_resolution(row) for row in review.findings)):
                raise LLMResponseError('Unfinished producer work requires an actionable finding in the same independent review.')
    if complete and set(declarations) != seen:
        raise LLMResponseError('Independent joint review must check every original producer declaration; missing checks never mean adoption.')
