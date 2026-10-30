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
                or action.get('kind') not in ('tool', 'opinion')):
            raise ValueError('Native action binding is not an accepted tool or opinion')
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
                           attempt_id: str, *, ledger: Any = None) -> tuple[ArtifactStore, str]:
    """Prove registration and hash the very bytes compared with the observation."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id):
        raise ValueError('Invalid accepted attempt identity')
    store = ArtifactStore(Path(root) / 'attempts' / attempt_id)
    ref = registered_native_output(root, session_id, store.root / 'native_result.json',
        kind='native_observation', store_root=store.root)
    raw = store.resolve(ref).read_bytes()
    if json.loads(raw.decode('utf-8')) != observed:
        raise ValueError('Professional outcome requires the exact accepted native observation')
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


def journal_input_dependencies(root: Path, workspace: Any, locator: str, *,
                               cache: dict | None = None,
                               producer_index_cache: dict | None = None) -> dict[str, str]:
    """Check declared input versions in existing same-task operation receipts.

    The index is temporary and contains no new facts or task state. Results
    lacking an input binding stay unknown; a file read never certifies them.
    Process outputs remain the workspace owner's responsibility.
    """
    store = ArtifactStore(root)
    cache = {} if cache is None else cache
    index = cache.get('journal_input_owners')
    if index is None:
        index = {}
        for turn in store.read_json('actions.json').get('turns', []):
            for item in turn.get('tools', []):
                try:
                    result = store.read_json(item['result'])
                    inputs = (result.get('data') or {}).get('input_sha256')
                    if result.get('status') != 'ok' or not isinstance(inputs, dict):
                        continue
                    for artifact in result.get('artifacts', []):
                        try:
                            index[str(workspace.resolve(artifact))] = item['result']
                        except (ValueError, OSError):
                            continue  # Unrelated inaccessible artifacts do not displace current ones.
                except (ValueError, TypeError, KeyError, OSError, RuntimeError):
                    continue
        cache['journal_input_owners'] = index

    session_id = store.read_json('session_manifest.json')['session_id']
    dependencies, active, completed = {}, set(), set()

    def visit(name: str) -> None:
        path = str(workspace.resolve(name))
        if path in active:
            raise ValueError('Recorded material dependencies form a cycle')
        if path in completed:
            return
        record = index.get(path)
        if record is None:
            return  # No recorded producer is not a proof of no dependencies.
        result = store.read_json(record)
        accepted_native_result(root, session_id, result, Path(record).parent.name)
        inputs = (result.get('data') or {}).get('input_sha256')
        if result.get('status') != 'ok' or not isinstance(inputs, dict):
            raise ValueError('Recorded material producer no longer has its input binding')
        if workspace.artifact_hashes(inputs) != inputs:
            raise ValueError('Recorded material producer input version changed')
        active.add(path)
        try:
            workspace.check_artifact_producers(inputs, cache=cache, **(
                {'producer_index_cache': producer_index_cache} if producer_index_cache is not None else {}))
            for name, digest in inputs.items():
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError('Recorded material producer has an invalid input version')
                canonical = str(workspace.resolve(name))
                if canonical in dependencies and dependencies[canonical] != digest:
                    raise ValueError('Recorded material producers have conflicting input versions')
                dependencies[canonical] = digest
                visit(name)
        finally:
            active.remove(path)
        completed.add(path)

    visit(locator)
    return dependencies


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
