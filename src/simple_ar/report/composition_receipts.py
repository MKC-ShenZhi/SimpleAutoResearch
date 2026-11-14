"""Read the historical composition receipts used by retained task consumers.

This module interprets saved writing outcomes; it does not schedule a writer,
create a task, or replace the original session and accounting.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.professional_outcomes import accepted_native_result
from simple_ar.tools.specs import tool_result_payload


class RetainedDocumentReview:
    """Read an old document review without creating its writing gateway."""

    @staticmethod
    def retained_feedback_inputs(arguments: dict, data: dict) -> dict:
        return {row['path']: row['sha256'] for row in data.get('inputs', [])
                if row.get('path') == arguments.get('prior_draft') and row.get('sha256')}

    @staticmethod
    def validate_retained_feedback(root: Path, workspace: Any, identity: str,
                                   arguments: dict, result: dict) -> None:
        data = result.get('data') or {}
        owner = ArtifactStore(root / 'tools/writing' / identity)
        if (owner.read_json('result.json').get('data') != data
                or workspace.artifact_hashes(result['artifacts']) != data.get('artifact_sha256')):
            raise ValueError('Retained document feedback changed')


def composition_owner(*, root: Path, workspace: Any, session_id: str, identity: str) -> ArtifactStore:
    """Resolve an admitted physical continuation, including stopped operations."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
        raise ValueError('Invalid composition identity')
    store = ArtifactStore(root/'tools/writing'/identity)
    if not store.exists('resume.json'):
        return store
    link = store.read_json('resume.json')
    owner = link['owner']
    if owner == identity or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', owner):
        raise ValueError('Invalid logical document owner')
    from simple_ar.tools.professional_outcomes import native_result_path
    path = native_result_path(root, workspace, link['prepared_result'])
    prepared = json.loads(path.read_bytes())
    accepted_native_result(root, session_id, prepared, path.parent.name)
    source = prepared.get('data', {}).get('report_identity', {})
    if (hashlib.sha256(path.read_bytes()).hexdigest() != link['prepared_sha256']
            or source.get('kind') != 'publication_candidate' or source.get('composition') != owner
            or source.get('receipt_sha256') != link['stage_sha256']):
        raise ValueError('Continuation publication source changed')
    result = ArtifactStore(root/'tools/writing'/owner)
    if result.exists('resume.json') or not result.exists('request.json'):
        raise ValueError('Continuation must retain one original Writer owner')
    return result


def accepted_composition_receipt(*, root: Path, workspace: Any, session_id: str,
        ledger: Any, observed: dict, identity: str | None = None) -> tuple[ArtifactStore, dict, dict]:
    """Prove the physical observation before resolving its logical document owner.

    Journal annotations are not native receipt bytes. Historical observations
    without a record retain their original, noncontinued manuscript identity;
    a continuation must provide its actual physical identity or record.
    Review disposition and delivery suitability remain consuming policies.
    """
    from simple_ar.tools.professional_outcomes import native_result_path
    raw = tool_result_payload(observed)
    if raw.get('tool_name') != 'compose_document':
        raise ValueError('Expected an accepted composition observation')
    if 'record' in observed:
        record = observed['record']
        if not isinstance(record, str) or not record:
            raise ValueError('Composition observation has no native result record')
        physical = native_result_path(root, workspace, record).parent.name
        if identity is not None and identity != physical:
            raise ValueError('Composition identity differs from its physical record')
        identity = physical
    if identity is None:
        identity = workspace.resolve(raw['data']['editable_markdown']).parent.name
    accepted_native_result(root, session_id, raw, identity, ledger=ledger)
    return read_composition_receipt(root=root, workspace=workspace,
        session_id=session_id, identity=identity, observed=raw)


def read_composition_receipt(*, root: Path, workspace: Any, session_id: str, identity: str, observed: dict | None = None) -> tuple[ArtifactStore, dict, dict]:
    """Validate retained composition identity, not its review disposition.

    Negative reviews remain valid receipts. Source windows, producer proof,
    review completion and delivery acceptance belong to the consuming policy.
    Missing historical result status retains its original successful default.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
        raise ValueError('Invalid composition identity')
    store = ArtifactStore(root / 'tools/writing' / identity)
    if store.exists('resume.json'):
        saved = store.read_json('result.json')
        if observed is not None and {key: value for key, value in observed.items() if key != 'tool_name'} != saved:
            raise ValueError('Physical continuation result changed')
        owner = composition_owner(root=root, workspace=workspace, session_id=session_id, identity=identity)
        return read_composition_receipt(root=root, workspace=workspace, session_id=session_id,
            identity=owner.root.name, observed={'tool_name': 'compose_document', **saved})
    binding, saved = store.read_json('request.json'), store.read_json('result.json')
    data = saved['data']
    prefix = f'workspace/manuscripts/{identity}'
    hashes = data.get('artifact_sha256')
    work = (data.get('editable_markdown') == f'{prefix}/candidate.md'
        and binding.get('work_result_contract') == 'checked_candidate.v1')
    if (saved.get('status', 'ok') != 'ok' or saved.get('error')
            or observed is not None and (
                observed.get('tool_name') != 'compose_document'
                or observed.get('status') != 'ok' or observed.get('error')
                or observed.get('data') != data or observed.get('artifacts') != saved['artifacts'])
            or data.get('version') != prefix
            or not work and workspace.resolve(data['editable_markdown']) != workspace.resolve(f'{prefix}/manuscript.md')
            or data.get('inputs') != binding['inputs']
            or data.get('report_binding') != binding.get('report_binding')
            or not hashes or data['editable_markdown'] not in hashes
            or set(saved['artifacts']) != set(hashes)
            or workspace.artifact_hashes(saved['artifacts']) != hashes):
        raise ValueError('Canonical report receipt, source binding or authoring artifacts changed')
    if work:
        check_work_receipt(root=root, workspace=workspace, session_id=session_id,
            store=store, binding=binding, data=data)
    return store, binding, data


def check_work_receipt(*, root: Path, workspace: Any, session_id: str, store: ArtifactStore, binding: dict, data: dict) -> dict:
    """Prove negative work against the actual author and inspection owner."""
    from simple_ar.report.writing import checked_writing_candidate
    from simple_ar.report.schema import ReviewerFinding, finding_requires_resolution
    if binding.get('continued_source_review') == 'checked_work.v1':
        prior = binding['prior_input_reuse']
        physical = prior.get('native_owner', prior['owner'])
        native = ArtifactStore(root / 'attempts' / physical)
        accepted_native_result(root, session_id, native.read_json('native_result.json'), physical)
    candidate = read_composition_candidate(workspace, data)
    expected = ([finding for row in candidate['independent_review']['section_reviews'] for finding in row['findings']
        if finding_requires_resolution(ReviewerFinding.model_validate(finding))]
        + candidate['independent_review'].get('delivery_review', {}).get('defects', [])) if candidate else []
    if (candidate is None or not data.get('unresolved') or data.get('incomplete')
            or candidate != checked_writing_candidate(store, binding)
            or data['unresolved'] != expected
            or read_composition_review(workspace, data) != candidate['independent_review']
            or data.get('retained_candidate', {}).get('status') != 'checked_not_adopted'
            or data.get('retained_candidate', {}).get('adopted') is not False
            or data.get('retained_candidate', {}).get('markdown') != f"{data['version']}/candidate.md"
            or workspace.resolve(data['retained_candidate']['markdown']).read_text(encoding='utf-8')
                != candidate['report_body_markdown']):
        raise ValueError('Negative work receipt differs from its complete checked candidate')
    return candidate


def read_composition_review(workspace: Any, data: dict) -> dict:
    """Read the final Writer review, preserving legacy inline receipts.

    A published review locator is authoritative only at its retained version
    and current byte hash. Inline and artifact forms must never disagree.
    This reads evidence only; callers still enforce their required status.
    """
    review = data.get('independent_review')
    review_path = data.get('review_path')
    if review_path is not None:
        hashes = data.get('artifact_sha256') or {}
        if (review_path != f"{data.get('version')}/review.json"
                or review_path not in hashes
                or workspace.artifact_hashes([review_path])[review_path] != hashes[review_path]):
            raise ValueError('Composition has no current version-bound independent review artifact')
        retained = json.loads(workspace.resolve(review_path).read_text(encoding='utf-8'))
        if review is not None and review != retained:
            raise ValueError('Inline independent review differs from its retained artifact')
        review = retained
    if not isinstance(review, dict):
        raise ValueError('Composition has no retained independent review')
    if 'delivery_review' in data and review.get('delivery_review') != data['delivery_review']:
        raise ValueError('Final independent review differs from the delivery inspection')
    return review


def read_composition_candidate(workspace: Any, data: dict) -> dict | None:
    """Read a retained candidate for repair; never confer adoption authority."""
    locator = data.get('pending_candidate')
    if locator is None:
        return None
    if locator != f"{data['version']}/pending_candidate.json":
        raise ValueError('Pending candidate is outside its composition')
    expected = data.get('artifact_sha256', {}).get(locator)
    path = workspace.resolve(locator)
    if not expected or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError('Pending candidate changed after composition')
    candidate = json.loads(path.read_bytes())
    review = candidate['independent_review']
    identity = candidate.get('assembly_identity') or review['assembly_identity']
    if (review['review_status'] != 'needs_revision'
            or not (review.get('delivery_review', {}).get('defects')
                or any(row.get('verdict') in {'revise_required', 'fail'} or row.get('findings')
                    for row in review.get('section_reviews', [])))
            or identity['body_sha256'] != hashlib.sha256(
                candidate['report_body_markdown'].encode('utf-8')).hexdigest()):
        raise ValueError('Pending candidate lacks a bound negative delivery review')
    sections = hashlib.sha256(json.dumps(candidate['sections'], sort_keys=True,
        ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
    if identity['sections_sha256'] != sections:
        raise ValueError('Pending candidate sections differ from their review')
    return candidate


def read_repair_review(workspace: Any, data: dict) -> dict:
    candidate = read_composition_candidate(workspace, data)
    return candidate['independent_review'] if candidate else read_composition_review(workspace, data)


def check_composition_inputs(workspace: Any, inputs: list[dict[str, Any]]) -> None:
    for row in inputs:
        if row.get('sha256') is not None:
            if workspace.artifact_hashes([row['path']])[row['path']] != row['sha256']:
                raise ValueError('Report source changed; compose a new current version')
        else:
            selection = row['selection']
            read = workspace.call('read_file', {'path': row['path'],
                'offset_bytes': selection['offset_bytes'],
                'max_bytes': selection['next_offset_bytes'] - selection['offset_bytes']}, identity='report-delivery')
            if (read.status != 'ok' or read.data['size_bytes'] != selection['size_bytes']
                    or hashlib.sha256(read.data['text'].encode('utf-8')).hexdigest() != selection['window_sha256']):
                raise ValueError('Report source window changed; compose a new current version')
