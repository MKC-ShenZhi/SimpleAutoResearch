"""Execute a registered code unit through the existing process/attempt owner."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.workspace import DeliveryRequest


class ExecuteImplementationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    prepared_result: str = Field(min_length=1, max_length=500,
        description='Registered records/<attempt>/implementation/result.json from prepare_implementation with an execution_plan. No hand-transcribed command, hashes or arbitrary directory.')
    delivery: DeliveryRequest | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional explicit final delivery after this run succeeds and its actual outputs are version-bound. Execution is not independent scientific or visual validation.')


def _hash(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def candidate_source(owner: Any, locator: str) -> tuple[Path, dict, dict]:
    """Resolve an accepted source unit, not an arbitrary project or proposal."""
    from simple_ar.code_task.generation.common import safe_relative_path

    record = owner.workspace.resolve(locator)
    relative = record.relative_to(owner.root / 'attempts').parts
    if len(relative) != 3 or relative[1:] != ('implementation', 'result.json'):
        raise ValueError('Use a registered prepared implementation result, not a directory or proposal')
    attempt = ArtifactStore(record.parent.parent)
    ref = owner._registered_ref(record, kind='prepared_implementation', store_root=attempt.root)
    if ref.schema != 'prepared_implementation.v1':
        raise ValueError('Unknown prepared implementation schema')
    payload = attempt.read_json(ref)
    observed = attempt.read_json('native_result.json')
    if (observed.get('tool_name') != 'prepare_implementation' or observed.get('status') != 'ok'
            or observed.get('data') != payload['data'] or observed.get('artifacts') != payload['artifacts']):
        raise ValueError('Prepared result differs from its accepted native observation')
    data = payload['data']
    candidate = owner.workspace.resolve(f'workspace/implementations/{relative[0]}', write=True)
    if Path(data['project_dir']).resolve() != candidate:
        raise ValueError('Prepared candidate root differs from its producing attempt')
    paths = set()
    for row in data['files']:
        path = safe_relative_path(row['path'])
        if not path or path in paths:
            raise ValueError('Invalid prepared code inventory')
        owner.workspace.resolve(str(candidate / path), write=True)
        paths.add(path)
    binding_path = record.parent / 'request.json'
    if Path(data['source_binding']).resolve() != binding_path.resolve():
        raise ValueError('Prepared source binding is not its producing request')
    source = ArtifactStore(record.parent).read_json('request.json')
    return candidate, payload, source


def prepared_unit(owner: Any, locator: str) -> tuple[dict, Path, dict]:
    """Only an accepted source unit with a valid plan can authorize execution."""
    from simple_ar.code_task.generation.run_plan import ImplementationRunPlan

    candidate, payload, source = candidate_source(owner, locator)
    record = owner.workspace.resolve(locator)
    data = payload['data']
    if data.get('execution_plan') is None:
        raise ValueError('Registered candidate has no execution plan; prepare one before requesting execution')
    plan = ImplementationRunPlan.model_validate(data['execution_plan'])
    plan.validate_planned_paths([row['path'] for row in data['files']])
    files = {str(owner.workspace.resolve(str(candidate / row['path']), write=True)): row['sha256']
        for row in data['files']}
    binding_path = record.parent / 'request.json'
    # Reuse actual owner-computed identities, never model-written digests.
    inputs = dict(files)
    inputs[str(record)] = _hash(record)
    inputs[str(binding_path)] = _hash(binding_path)
    for item in source.get('original_files', {}).values():
        inputs[str(owner.workspace.resolve(item['path']))] = item['sha256']
    if prior := source.get('prior_candidate'):
        previous = owner.workspace.resolve(prior['result_ref'])
        inputs[str(previous)] = prior['result_sha256']
        inputs[str(previous.parent / 'request.json')] = prior['source_binding_sha256']
    inputs.update(source.get('selection', {}).get('input', {}).get('file_hashes', {}))
    for window in source['source_windows']:
        if 'request' in window:
            if not window.get('sha256'):
                raise ValueError('Execution needs full material identities; inspect a full-digest window first')
            inputs[str(owner.workspace.resolve(window['request']['path']))] = window['sha256']
    return {'prepared_result': locator, 'plan': plan.model_dump(),
        'source_sha256': inputs}, candidate, payload


def execution_binding(owner: Any, arguments: dict, *, identity: str) -> dict:
    request = ExecuteImplementationRequest.model_validate(arguments)
    unit, candidate, payload = prepared_unit(owner, request.prepared_result)
    if owner.workspace.artifact_hashes(unit['source_sha256']) != unit['source_sha256']:
        raise ValueError('Prepared implementation or its consumed materials changed before execution')
    # A new physical run must not certify pre-existing figures/results.
    outputs = [str(owner.workspace.resolve(str(candidate / path), write=True)) for path in unit['plan']['outputs']]
    if any(Path(path).exists() or Path(path).is_symlink() for path in outputs):
        raise ValueError('Expected outputs already exist; use the original execution receipt or a new candidate')
    inventory = owner.workspace.bind_inventory(candidate, identity=identity)
    generated = {str(candidate / row['path']): row['sha256'] for row in payload['data']['files']}
    if inventory['file_hashes'] != generated:
        raise ValueError('Prepared candidate inventory changed before approval')
    argv = owner.workspace.process_argv(unit['plan']['argv'])
    for argument in argv[1:]:
        # Literal command text is pinned as argv, while directly named files
        # need byte identities too, even when not selected during generation.
        value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
        try:
            path = (candidate / value).resolve()
            exists = path.is_file()
        except (OSError, ValueError):
            continue  # An argument may be inline program text, not a path.
        if exists:
            path = owner.workspace.resolve_material_root(str(path))
            unit['source_sha256'][str(path)] = _hash(path)
    return {**unit, 'request': request.model_dump(), 'cwd': str(candidate),
        'argv': argv,
        'unresolved': payload['data'].get('unresolved', []),
        'environment': owner.workspace.process_environment(), 'outputs': outputs}


def execute_prepared(owner: Any, context: Any, arguments: dict,
                     *, approval: dict) -> ToolResult:
    """One approved physical invocation; never an implicit repair/retry loop."""
    request = ExecuteImplementationRequest.model_validate(arguments)
    identity, store = context.attempt.attempt_id, context.store
    if not approval.get('token') or not isinstance(approval.get('binding'), dict):
        raise ValueError('Prepared execution needs explicit task-owned approval')
    binding = approval['binding']
    if binding.get('request') != request.model_dump():
        raise ValueError('Prepared execution approval does not match the requested work')
    if store.exists('candidate_execution.json'):
        saved = store.read_json('candidate_execution.json')
        if saved['approval'] != approval:
            raise ValueError('Retained candidate execution has different approval')
        if saved['status'] == 'run_succeeded':
            hashes = {**binding['source_sha256'], **saved['output_sha256']}
            if owner.workspace.artifact_hashes(hashes) != hashes:
                raise ValueError('Executed source or outputs changed; no replay or retroactive delivery')
        return _result(owner, store, saved)
    if store.exists('execution_input.json'):
        if store.read_json('execution_input.json') != approval:
            raise ValueError('Interrupted execution has a different input binding')
    elif binding != execution_binding(owner, arguments, identity=identity):
        raise ValueError('Prepared execution inputs changed since approval')
    recovering = owner.workspace.has_started(identity)
    if recovering and not owner.workspace.can_reconcile('run_process', identity):
        raise ValueError('Prepared process is live or unknown; no replay')
    store.write_json('execution_input.json', approval)
    process = owner.workspace.call('run_process', {'argv': binding['argv'], 'cwd': binding['cwd'],
        'timeout_sec': binding['plan']['timeout_sec']}, identity=identity)
    if process.status != 'ok':
        return ToolResult(tool_name='execute_implementation', status='blocked', data=process.data,
            error=process.error, artifacts=process.artifacts)
    data = process.data
    status, outputs, issues = 'run_succeeded', {}, []
    if recovering:
        status = 'unbound_outputs_after_interruption'
        issues.append('Recovered physical process without owner-bound output bytes; no replay or output certification')
    if data.get('returncode') != 0 or data.get('stop_reason'):
        status = 'process_failed'
        issues.append('Process failed or stopped; inspect its actual stdout/stderr before preparing a repair')
    try:
        unchanged = owner.workspace.artifact_hashes(binding['source_sha256']) == binding['source_sha256']
    except (OSError, ValueError):
        unchanged = False
    if not unchanged:
        status = 'changed_inputs'
        issues.append('Source or consumed material changed during execution')
    if status == 'run_succeeded':
        try:
            for path in binding['outputs']:
                owner.workspace.resolve_material_root(path)  # Never certify an old file through a new output link.
            outputs = owner.workspace.artifact_hashes(binding['outputs'])
        except (OSError, ValueError):
            status = 'missing_outputs'
            issues.append('Successful exit did not produce every declared regular output file')
    saved = {**data, 'argv': binding['argv'], 'cwd': binding['cwd'],
        'schema_version': 'prepared_implementation_execution.v1',
        'prepared_result': request.prepared_result, 'approval': approval, 'status': status,
        'unresolved': binding['unresolved'],
        'output_sha256': outputs, 'issues': issues,
        'verification': 'Physical invocation and byte versions only; no independent scientific or visual validation.'}
    if status == 'run_succeeded' and request.delivery is not None:
        delivery = request.delivery.model_copy(update={'unresolved': list(dict.fromkeys([
            *request.delivery.unresolved, *binding['unresolved']]))})
        saved['delivery'] = owner.workspace.resolve_delivery(delivery)
        saved['delivery_sha256'] = owner.workspace.artifact_hashes(saved['delivery']['artifacts'])
    store.write_json('candidate_execution.json', saved,
        kind='prepared_implementation_execution', schema='prepared_implementation_execution.v1')
    return _result(owner, store, saved)


def delivery_issues(owner: Any, observations: list[dict]) -> list[str]:
    # A later successful version supersedes an earlier failed candidate for
    # delivery, without rewriting its ledger or declaring the failure solved.
    executions = [row for row in observations if row.get('tool_name') == 'execute_implementation']
    current = [row for row in executions if row.get('current_request')]
    latest = (current or executions)[-1:]  # Do not make every historical run a permanent obligation.
    for observed in latest:
        try:
            data = observed['data']
            store = ArtifactStore(owner.workspace.resolve(data['execution_ref']).parent)
            saved = store.read_json('candidate_execution.json')
            if observed.get('status') != 'ok' or saved['status'] != 'run_succeeded':
                return ['Prepared execution has unresolved failures; inspect its original process receipt']
            if saved.get('unresolved'):
                return ['Prepared implementation still reports unresolved work: ' + '; '.join(saved['unresolved'])]
            hashes = {**saved['approval']['binding']['source_sha256'], **saved['output_sha256']}
            for path in saved['output_sha256']:
                owner.workspace.resolve_material_root(path)
            if owner.workspace.artifact_hashes(hashes) != hashes:
                return ['Prepared execution source or output version changed after its run']
        except (OSError, ValueError, KeyError, TypeError):
            return ['Prepared execution lacks current byte-bound evidence; no success certification']
    return []


def _result(owner: Any, store: Any, saved: dict) -> ToolResult:
    ref = store.ref('candidate_execution.json', kind='prepared_implementation_execution',
        schema='prepared_implementation_execution.v1')
    data = {key: value for key, value in saved.items() if key != 'approval'}
    data['execution_ref'] = f'records/{store.root.name}/candidate_execution.json'
    metadata = {'capability_artifacts': [ref.to_dict()]}
    if saved.get('delivery'):
        metadata['delivery_hashes'] = saved['delivery_sha256']
    return ToolResult(tool_name='execute_implementation',
        status='ok' if saved['status'] == 'run_succeeded' else 'blocked', data=data,
        artifacts=list(saved['output_sha256']), metadata=metadata,
        error='; '.join(saved['issues']),
        summary='Executed the registered candidate; retained original process and version evidence. '
            'Scientific and visual quality remain unverified.')
