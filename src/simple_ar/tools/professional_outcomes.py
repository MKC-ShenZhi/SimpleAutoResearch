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
    if (attempt.attempt_id != store.root.name or attempt.capability != 'native_tool'
            or attempt.status != 'completed' or capability.status != 'completed'
            or session['session_id'] != session_id
            or attempt.attempt_id not in session['budget']['recorded_attempts']):
        raise ValueError('Source is not a completed native_tool attempt in this task')
    registered = [ref for ref in attempt.outputs if ref.path == relative
        and ref.kind == kind and ref.status == 'available'
        and ref in capability.artifacts]
    if len(registered) != 1:
        raise ValueError('Source is not an actual registered output')
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
