"""Receipt proofs and stop dispositions shared by professional result owners.

These functions only read existing native attempts. They do not settle physical
accounting, authorize consumers, schedule actions, or create lifecycle state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from simple_ar.core.capabilities import ArtifactRef, ArtifactStore


def native_result_digest(observed: dict) -> str:
    """Digest the original structured observation, independent of JSON formatting."""
    return hashlib.sha256(json.dumps(observed, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def related_feedback(workspace: Any, store: ArtifactStore, session_id: str, turns: list[dict],
                     targets: list[str], owners: dict[str, Any], *, copy_lineage: bool) -> list[dict]:
    """Project accepted domain reviews onto the requested materials.

    Domain owners interpret and validate their own receipts. This shared view
    relates reviewed versions to current materials; it neither re-runs reviews
    nor turns an earlier opinion into acceptance of a later version.
    """
    roots = [workspace.resolve(path) for path in targets]
    selected_hashes = None
    reviews, covered = [], set()
    for turn in reversed(turns):
        calls = {row['id']: row for row in store.read_json(turn['reply']).get('tool_calls', [])}
        for item in reversed(turn['tools']):
            call = calls[item['id']]
            owner = owners.get(call['function']['name'])
            if owner is None:
                continue
            result = store.read_json(item['result'])
            if result.get('status') != 'ok':
                continue
            arguments = json.loads(call['function']['arguments'])
            inputs = owner.retained_feedback_inputs(arguments, result.get('data') or {})
            if not inputs:
                continue
            if selected_hashes is None:
                selected_hashes = {path: workspace.artifact_hashes([str(path)])[str(path)]
                                   for path in roots if path.is_file()}
            relevant = {}
            for locator, digest in inputs.items():
                try:
                    subject = workspace.resolve(locator)
                except ValueError:
                    continue
                if copy_lineage:
                    from simple_ar.tools.materials import _recorded_copy_source
                    selected = next((path for path, value in selected_hashes.items()
                        if value == digest and (path == subject or _recorded_copy_source(
                            workspace, str(path), digest, str(subject)))), None)
                else:
                    selected = next((path for path, value in selected_hashes.items() if value == digest), None)
                if subject not in covered and (selected is not None or any(subject == root or root.is_dir()
                        and subject.is_relative_to(root) for root in roots)):
                    relevant[locator] = (subject, selected or subject, digest)
            if not relevant:
                continue
            identity = Path(item['result']).parent.name
            attempt = ArtifactStore(store.root / 'attempts' / identity)
            if (attempt.exists('capability_result.json')
                    and attempt.read_capability_result().provenance.get('native_result_sha256')):
                accepted_native_result(store.root, session_id, result, identity, require_content_binding=True)
            owner.validate_retained_feedback(store.root, workspace, identity, arguments, result)
            subjects = []
            for locator, (subject, selected, digest) in relevant.items():
                current = workspace.artifact_hashes([str(selected)])[str(selected)] if selected.is_file() else None
                subjects.append({'path': locator, 'reviewed_sha256': digest,
                    'selected_path': str(selected), 'current_sha256': current,
                    'current_matches_review': current == digest})
                covered.add(subject)
            reviews.append({'record': 'records/' + item['result'].removeprefix('attempts/'),
                'arguments': arguments, 'result': result, 'subjects': subjects})
    return reviews


def accepted_review_journal(workspace: Any, store: Any, item: dict, receipt: dict) -> dict:
    """Resolve the separate accepted review without adopting its opinion.

    This explicitly proves registration and parent scope; journal reads alone
    do neither. Accounting checks remain with the consumers that require them.
    """
    session_id = store.read_json('session_manifest.json')['session_id']
    parent = Path(item['result']).parent.name
    accepted_native_result(store.root, session_id, receipt, parent)
    path, review, digest = accepted_scoped_opinion(
        store.root, workspace, session_id, item['review_result'], parent)
    return {'record': 'attempts/' + path.relative_to((store.root / 'attempts').resolve()).as_posix(),
            'receipt': review, 'sha256': digest}


def native_result_path(root: Path, workspace: Any, locator: str) -> Path:
    """Resolve the journal or mounted spelling of one same-task native result.

    This is address admission only; callers still prove registration, bytes,
    settlement and the domain-specific meaning of the original receipt.
    """
    path = workspace.resolve(str(root / locator) if locator.startswith('attempts/') else locator)
    relative = path.relative_to((root / 'attempts').resolve())
    if (len(relative.parts) != 2 or relative.name != 'native_result.json'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', relative.parts[0])):
        raise ValueError('Use a same-task native result receipt')
    return path


def registered_native_output(root: Path, session_id: str, record: Path, *,
                             kind: str, store_root: Path | None = None) -> ArtifactRef:
    """One registration proof for native receipts and declared domain outputs."""
    store = ArtifactStore(store_root or record.parent)
    relative = record.relative_to(store.root).as_posix()
    attempt = store.read_attempt_manifest()
    capability = store.read_capability_result()
    session = ArtifactStore(root).read_json('session_manifest.json')
    if (attempt.attempt_id != store.root.name or attempt.capability not in {'native_tool', 'native_action'}
            or attempt.status != 'completed' or capability.status != 'completed'
            or session['session_id'] != session_id
            or attempt.attempt_id not in session['budget']['recorded_attempts']):
        if attempt.capability == 'native_action':
            raise ValueError('Source is not a completed native_action attempt in this task')
        raise ValueError('Source is not a completed native_tool attempt in this task')
    if attempt.capability == 'native_action':
        bindings = [ref for ref in attempt.outputs if ref.path == 'action.json']
        if (len(bindings) != 1 or bindings[0].kind != 'operation_binding'
                or bindings[0].status != 'available'
                or [ref for ref in capability.artifacts if ref.path == 'action.json'] != bindings):
            raise ValueError('Native action lacks its registered action binding')
        action = store.read_json(bindings[0])
        if (not isinstance(action, dict) or action.get('attempt') != attempt.attempt_id
                or action.get('kind') not in ('tool', 'opinion', 'receipt')):
            raise ValueError('Native action binding is not an accepted tool or opinion')
        current = action
        seen = set()
        while current.get('recovered_from') is not None:
            previous_id = current['recovered_from']
            if (not isinstance(previous_id, str) or previous_id in seen
                    or current.get('attempt') != previous_id + '-receipt'):
                raise ValueError('Native action recovery chain is invalid')
            seen.add(previous_id)
            previous = ArtifactStore(Path(root) / 'attempts' / previous_id).read_json('action.json')
            expected = {key: value for key, value in previous.items()
                        if key not in {'result', 'execution_approval'}}
            expected.update(attempt=previous_id + '-receipt',
                kind='opinion' if previous.get('kind') == 'opinion' else 'receipt',
                recovered_from=previous_id)
            if current != expected:
                raise ValueError('Native action recovery differs from its original binding')
            current = previous
        if current.get('kind') not in ('tool', 'opinion'):
            raise ValueError('Native action recovery has no original tool or opinion')
        if action['kind'] == 'opinion':
            result = store.read_json('native_result.json')
            digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if action.get('result_sha256') != digest:
                raise ValueError('Native opinion result differs from its accepted action binding')
    registered = [ref for ref in attempt.outputs if ref.path == relative
        and ref.kind == kind and ref.status == 'available'
        and ref in capability.artifacts]
    if len(registered) != 1:
        raise ValueError('Source is not an actual registered output')
    if attempt.capability == 'native_action' and (
            [ref for ref in attempt.outputs if ref.path == relative] != registered
            or [ref for ref in capability.artifacts if ref.path == relative] != registered):
        raise ValueError('Native action output registration is ambiguous')
    return registered[0]


def accepted_native_result(root: Path, session_id: str, observed: dict,
                           attempt_id: str, *, ledger: Any = None,
                           require_content_binding: bool = False) -> tuple[ArtifactStore, str]:
    """Check registration and any producer-time content binding.

    Historical registration remains usable for its original contracts; callers
    claiming a file's provenance must require a producer-time binding.
    """
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id):
        raise ValueError('Invalid accepted attempt identity')
    store = ArtifactStore(Path(root) / 'attempts' / attempt_id)
    ref = registered_native_output(root, session_id, store.root / 'native_result.json',
        kind='native_observation', store_root=store.root)
    raw = store.resolve(ref).read_bytes()
    if json.loads(raw.decode('utf-8')) != observed:
        raise ValueError('Professional outcome requires the exact accepted native observation')
    binding = store.read_capability_result().provenance.get('native_result_sha256')
    if binding is None and store.read_attempt_manifest().capability == 'native_action':
        action = store.read_json('action.json')
        if action.get('kind') == 'opinion':
            binding = action.get('result_sha256')
    if binding is None and require_content_binding:
        raise ValueError('Accepted native observation has no original content binding')
    if binding is not None and binding != native_result_digest(observed):
        raise ValueError('Accepted native observation changed since production')
    if ledger is not None:
        from simple_ar.core.budget import completed_llm_usage_is_accounted
        if not completed_llm_usage_is_accounted(row.to_dict() for row in ledger.entries
                if row.attempt_id == attempt_id):
            raise ValueError('Professional outcome still has unsettled physical accounting')
    return store, hashlib.sha256(raw).hexdigest()


def accepted_scoped_opinion(root: Path, workspace: Any, session_id: str, locator: str,
                            scope: str, *, expected: dict | None = None) -> tuple[Path, dict, str]:
    """Prove an opinion belongs to one parent action without adopting its judgment."""
    path = native_result_path(root, workspace, locator)
    result = ArtifactStore(path.parent).read_json('native_result.json')
    if expected is not None and result != expected:
        raise ValueError('Opinion differs from the accepted native observation')
    store, digest = accepted_native_result(root, session_id, result, path.parent.name)
    action = store.read_json('action.json')
    if (store.read_attempt_manifest().capability != 'native_action'
            or not isinstance(action, dict) or action.get('kind') != 'opinion'
            or not isinstance(action.get('origin'), dict) or action['origin'].get('scope') != scope):
        raise ValueError('Opinion is not the accepted action for this scope')
    return path, result, digest


def stop_decision(owner: Any, observed: dict, attempt_id: str, reason: str, detail: str) -> dict:
    """Describe an accepted outcome without changing its historical stop semantics."""
    _, digest = accepted_native_result(owner.root, owner.session_id, observed, attempt_id)
    data = observed.get('data') or {}
    return {'stop': {'contract': 'professional_stop.v1', 'owner': observed['tool_name'],
        'reason': reason, 'detail': detail, 'source_result': f'records/{attempt_id}/native_result.json',
        'source_sha256': digest,
        'diagnostics': {key: data[key] for key in ('status', 'prepared_result', 'execution_ref',
            'returncode', 'stop_reason', 'stderr', 'stdout', 'issues', 'unresolved') if key in data},
        'error': observed.get('error', ''), 'completed': False,
        'verification': 'Professional work stopped; existing candidates and failures are retained, not certified or replayed.'}}


def check_professional_stop(root: Path, session_id: str, source: str, stop: dict) -> None:
    """Validate a retained stop against its registered, current native receipt."""
    attempt_id = Path(source).parent.name
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id)
            or Path(source).as_posix() != f'attempts/{attempt_id}/native_result.json'
            or stop.get('contract') != 'professional_stop.v1'
            or stop.get('completed') is not False
            or stop.get('source_result') != f'records/{attempt_id}/native_result.json'):
        raise ValueError('Professional stop differs from its accepted source observation')
    store = ArtifactStore(Path(root) / 'attempts' / attempt_id)
    _, digest = accepted_native_result(root, session_id, store.read_json('native_result.json'), attempt_id)
    if digest != stop.get('source_sha256'):
        raise ValueError('Professional stop differs from its accepted source observation')
