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


def execution_input_versions(binding: dict) -> dict[str, str]:
    """Runtime edges, distinct from the complete approval/context byte guard."""
    versions, context = binding['source_sha256'], binding.get('context_sha256', {})
    if not isinstance(context, dict) or any(versions.get(path) != digest for path, digest in context.items()):
        raise ValueError('Execution context is not part of the approved source versions')
    return {path: digest for path, digest in versions.items() if path not in context}


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
    completion = source['request'].get('producer_completion')
    if completion not in {None, 'goal_findings.v1'} or data.get('producer_completion') != completion:
        raise ValueError('Prepared completion semantics differ from the retained request')
    from simple_ar.tools.implementation_work import REGISTERED_PROJECT_GENERATION
    if source.get('generation_method') == REGISTERED_PROJECT_GENERATION:
        from simple_ar.code_task.generation.selection import registered_project_files
        registered_project_files(owner.workspace, source['prior_candidate'])
    if source.get('project_snapshot'):
        from simple_ar.code_task.generation.selection import registered_project_files
        registered_project_files(owner.workspace, source['project_snapshot'])
    if preserved := source.get('preserved_files'):
        from simple_ar.code_task.generation.selection import preserved_input_hashes

        preserved_input_hashes(owner.workspace, preserved)
        files = {row['path']: row['sha256'] for row in data['files']}
        if any(files.get(name) != binding['sha256'] for name, binding in preserved.items()):
            raise ValueError('Preserved inputs differ from the registered candidate inventory')
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
    plan.validate_command_identity(candidate_root=candidate, source_binding=source,
        candidate_paths=[row['path'] for row in data['files']])
    files = {str(owner.workspace.resolve(str(candidate / row['path']), write=True)): row['sha256']
        for row in data['files']}
    binding_path = record.parent / 'request.json'
    # Reuse actual owner-computed identities, never model-written digests.
    inputs = dict(files)
    inputs[str(record)] = _hash(record)
    inputs[str(binding_path)] = _hash(binding_path)
    if source.get('project_snapshot'):
        from simple_ar.code_task.generation.selection import registered_project_files
        snapshot = source['project_snapshot']
        inputs.update({str(Path(snapshot['project_root']) / name): digest for name, digest in
            registered_project_files(owner.workspace, snapshot).items()})
    for item in source.get('original_files', {}).values():
        inputs[str(owner.workspace.resolve(item['path']))] = item['sha256']
    if source.get('preserved_files'):
        from simple_ar.code_task.generation.selection import preserved_input_hashes

        inputs.update(preserved_input_hashes(owner.workspace, source['preserved_files']))
    if prior := source.get('prior_candidate'):
        previous = owner.workspace.resolve(prior['result_ref'])
        inputs[str(previous)] = prior['result_sha256']
        inputs[str(previous.parent / 'request.json')] = prior['source_binding_sha256']
        from simple_ar.tools.implementation_work import REGISTERED_PROJECT_GENERATION
        if source.get('generation_method') == REGISTERED_PROJECT_GENERATION:
            from simple_ar.code_task.generation.selection import registered_project_files
            inputs.update({str(Path(prior['project_root']) / name): digest for name, digest in
                registered_project_files(owner.workspace, prior).items()})
            from simple_ar.code_task.generation.selection import preserved_input_hashes
            for name, binding in prior.get('preserved_files', {}).items():
                inputs.update(preserved_input_hashes(owner.workspace, {name: binding}))
    contract = source.get('material_contract')
    if contract not in {None, 'authoring_context.v1'}:
        raise ValueError('Unknown retained implementation material roles')
    runtime_inputs = set(inputs)
    inputs.update(source.get('selection', {}).get('input', {}).get('file_hashes', {}))
    for window in source['source_windows']:
        if 'request' in window:
            if not window.get('sha256'):
                raise ValueError('Execution needs full material identities; inspect a full-digest window first')
            inputs[str(owner.workspace.resolve(window['request']['path']))] = window['sha256']
    semantics = source.get('argv_semantics')
    if semantics not in {None, 'candidate_relative.v1', 'candidate_relative.v2'}:
        raise ValueError('Unknown retained command path semantics')
    snapshot_contract = source.get('execution_snapshot')
    if snapshot_contract not in {None, 'implementation_inputs.v1'}:
        raise ValueError('Unknown implementation input snapshot contract')
    return {'prepared_result': locator, 'plan': plan.model_dump(),
        'source_sha256': inputs,
        **({'execution_snapshot': snapshot_contract} if snapshot_contract else {}),
        **({'context_sha256': {path: digest for path, digest in inputs.items() if path not in runtime_inputs}}
            if contract == 'authoring_context.v1' else {}),
        **({'argv_semantics': semantics} if semantics else {})}, candidate, payload


def resolve_execution_snapshot(workspace: Any, reference: dict, *,
                               process: dict | None = None, identity: str | None = None) -> dict[str, str]:
    """Resolve an internal execution-owner reference, never a model input map.

    The existing approval and preparation receipts own every member. Rebuild
    them and stream-check their bytes at each process-owner use; the compact
    reference is not a cached permission or a second snapshot state. A process
    consumer supplies its normalized request and invocation identity; these
    must name this approval's command and execution attempt. Omitting them is
    only a read-only version query, never authorization to launch a command.
    """
    import re
    from types import SimpleNamespace
    from simple_ar.tools.implementation import ImplementationToolGateway

    if (not isinstance(reference, dict) or set(reference) != {'record', 'sha256'}
            or not isinstance(reference['record'], str)
            or not isinstance(reference['sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', reference['sha256'])):
        raise ValueError('Invalid implementation input snapshot reference')
    record = workspace.resolve(reference['record'])
    relative = record.relative_to(workspace.root / 'attempts').parts
    if (len(relative) != 2 or relative[1] != 'execution_input.json'
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', relative[0])
            or _hash(record) != reference['sha256']):
        raise ValueError('Implementation input snapshot escaped its owner or changed')
    if identity is not None and identity != relative[0]:
        raise ValueError('Implementation input snapshot belongs to a different execution attempt')
    attempt = ArtifactStore(record.parent).read_attempt_manifest()
    if attempt.capability != 'native_tool' or attempt.attempt_id != relative[0]:
        raise ValueError('Implementation snapshot requires its native execution attempt')
    approval = ArtifactStore(record.parent).read_json(record.name)
    binding = approval['binding']
    request = ExecuteImplementationRequest.model_validate(binding['request'])
    if not approval.get('token') or binding.get('execution_snapshot') != 'implementation_inputs.v1':
        raise ValueError('Historical approval cannot acquire an input snapshot')
    if process is not None:
        if (not isinstance(process, dict) or process.get('inputs')
                or process.get('input_snapshot') != reference or process.get('delivery') is not None):
            raise ValueError('Implementation snapshot requires its exact owner process request')
        argv = workspace.process_argv(process['argv'], resolve_mounts=not process.get('literal_argv', False))
        cwd = workspace.resolve(process.get('cwd', 'workspace'), write=True)
        outputs = {workspace.resolve(path, write=True) for path in process.get('outputs', [])}
        expected_outputs = {workspace.resolve(path, write=True) for path in binding['outputs']}
        if (argv != binding['argv'] or str(cwd) != binding['cwd']
                or type(process.get('timeout_sec')) is not int
                or process['timeout_sec'] != binding['plan']['timeout_sec']
                or outputs != expected_outputs
                or bool(process.get('literal_argv', False)) != (binding.get('argv_semantics')
                    in {'candidate_relative.v1', 'candidate_relative.v2'})):
            raise ValueError('Implementation snapshot process differs from its approved command or outputs')
    session = ArtifactStore(workspace.root).read_json('session_manifest.json')
    proof = SimpleNamespace(root=workspace.root, workspace=workspace, session_id=session['session_id'])
    proof._registered_ref = lambda path, **kwargs: ImplementationToolGateway._registered_ref(proof, path, **kwargs)
    unit, candidate, payload = prepared_unit(proof, request.prepared_result)
    argv = workspace.process_argv(unit['plan']['argv'], resolve_mounts=unit.get('argv_semantics') is None)
    bind_command_input_versions(proof, unit, candidate, argv)
    if (any(binding.get(key) != unit.get(key) for key in unit)
            or binding.get('argv') != argv or binding.get('cwd') != str(candidate)
            or binding.get('outputs') != [str(workspace.resolve(str(candidate / name), write=True))
                for name in unit['plan']['outputs']]):
        raise ValueError('Implementation snapshot differs from its approved preparation')
    versions = binding['source_sha256']
    if workspace.artifact_hashes(versions) != versions:
        raise ValueError('Implementation snapshot source or context changed')
    # Before launch the candidate must still be exactly the approved source
    # tree. Once a physical invocation exists, additional runtime files are not
    # source members; all registered source bytes remain guarded above.
    physical = workspace.root / 'tools/processes' / relative[0] / 'invocation.json'
    if not physical.exists():
        if any(Path(path).exists() or Path(path).is_symlink() for path in binding['outputs']):
            raise ValueError('Implementation snapshot outputs appeared before execution')
        inventory = workspace.bind_inventory(candidate, identity=relative[0])['file_hashes']
        expected = {str(candidate / row['path']): row['sha256'] for row in payload['data']['files']}
        if inventory != expected:
            raise ValueError('Implementation snapshot candidate membership changed')
    return execution_input_versions(binding)


def bind_command_input_versions(owner: Any, unit: dict, candidate: Path, argv: list[str]) -> None:
    """Bind direct command files and promote them out of observed context."""
    for index, argument in enumerate(argv):
        value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
        try:
            path = (candidate / value).resolve()
            exists = path.is_file()
        except (OSError, ValueError):
            continue  # An argument may be inline program text, not a path.
        if (unit.get('execution_snapshot') == 'implementation_inputs.v1'
                and str(path) in {str((candidate / name).resolve()) for name in unit['plan']['outputs']}):
            continue  # A new output appearing after execution is never an input.
        if index == 0 and str(path) not in unit.get('context_sha256', {}):
            continue  # The interpreter retains its existing execution policy.
        if exists:
            path = owner.workspace.resolve_material_root(str(path))
            digest = _hash(path)
            if str(path) in unit['source_sha256'] and unit['source_sha256'][str(path)] != digest:
                raise ValueError('Direct command input differs from its observed version')
            unit['source_sha256'][str(path)] = digest
            unit.get('context_sha256', {}).pop(str(path), None)


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
    # An implementation plan executes in its candidate, unlike an arbitrary
    # workspace command whose whole mount locators are explicitly resolved.
    # Retained preparations without this binding keep their original behavior.
    argv = owner.workspace.process_argv(unit['plan']['argv'],
        resolve_mounts=unit.get('argv_semantics') is None)
    if unit.get('argv_semantics') == 'candidate_relative.v2':
        # Interpreter alias binding must obey the same source identity rule.
        from simple_ar.code_task.generation.run_plan import ImplementationRunPlan
        source = ArtifactStore(owner.workspace.resolve(request.prepared_result).parent).read_json('request.json')
        ImplementationRunPlan.model_validate({**unit['plan'], 'argv': argv}).validate_command_identity(
            candidate_root=candidate, source_binding=source,
            candidate_paths=[row['path'] for row in payload['data']['files']])
    bind_command_input_versions(owner, unit, candidate, argv)
    return {**unit, 'request': request.model_dump(), 'cwd': str(candidate),
        'argv': argv, 'process_artifacts': 1,
        **({'producer_completion': payload['data']['producer_completion']}
            if 'producer_completion' in payload['data'] else {}),
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
    else:
        current_binding = execution_binding(owner, arguments, identity=identity)
        if 'process_artifacts' not in binding:
            # A historical pending approval retains its original proof contract.
            # Still compare every previously approved field before executing.
            current_binding.pop('process_artifacts', None)
        if binding != current_binding:
            raise ValueError('Prepared execution inputs changed since approval')
    recovering = owner.workspace.has_started(identity)
    if recovering and not owner.workspace.can_reconcile('run_process', identity):
        raise ValueError('Prepared process is live or unknown; no replay')
    store.write_json('execution_input.json', approval)
    if owner.workspace.artifact_hashes(binding['source_sha256']) != binding['source_sha256']:
        raise ValueError('Approved implementation source/context changed before execution')
    snapshot = ({'record': f'records/{identity}/execution_input.json',
        'sha256': _hash(store.resolve('execution_input.json'))}
        if binding.get('execution_snapshot') == 'implementation_inputs.v1' else None)
    process_request = {'argv': binding['argv'], 'cwd': binding['cwd'],
        'timeout_sec': binding['plan']['timeout_sec'],
        **({'literal_argv': True} if binding.get('argv_semantics') in {'candidate_relative.v1', 'candidate_relative.v2'} else {}),
        **({**({'input_snapshot': snapshot} if snapshot is not None else
               {'inputs': list(execution_input_versions(binding))}),
            'outputs': binding['outputs']}
           if binding.get('process_artifacts') == 1 else {})}
    if snapshot is not None:
        resolve_execution_snapshot(owner.workspace, snapshot, process=process_request, identity=identity)
    process = owner.workspace.call('run_process', process_request, identity=identity)
    if process.status != 'ok' and not (process.data.get('receipt')
            and type(process.data.get('returncode')) is int):
        return ToolResult(tool_name='execute_implementation', status='blocked', data=process.data,
            error=process.error, artifacts=process.artifacts)
    data = process.data
    status, outputs, issues = 'run_succeeded', {}, []
    if recovering:
        status = 'unbound_outputs_after_interruption'
        issues.append('Recovered physical process without owner-bound output bytes; no replay or output certification')
    if not recovering and (data.get('returncode') != 0 or data.get('stop_reason')):
        status = 'process_failed'
        issues.append('Process failed or stopped; inspect its actual stdout/stderr before preparing a repair')
    if binding.get('process_artifacts') == 1:
        # The process owner already checked all input/output bytes. Consume its
        # receipt rather than independently certifying the same execution again.
        physical = ArtifactStore(owner.root).read_json(data['receipt'])
        if physical.get('output_binding_error'):
            status = 'changed_inputs' if physical.get('output_binding_error_kind') == 'inputs' else 'missing_outputs'
            issues.append(physical['output_binding_error'])
        elif status == 'run_succeeded':
            recorded = {owner.workspace.resolve(path): digest
                for path, digest in physical.get('output_binding', {}).get('outputs', {}).items()}
            if set(recorded) != {Path(path) for path in binding['outputs']}:
                status = 'missing_outputs'
                issues.append('Process receipt does not bind the declared output set')
            else:
                outputs = {path: recorded[Path(path)] for path in binding['outputs']}
    else:
        # Historical approvals retain their original owner and proof strength.
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
                    owner.workspace.resolve_material_root(path)
                outputs = owner.workspace.artifact_hashes(binding['outputs'])
            except (OSError, ValueError):
                status = 'missing_outputs'
                issues.append('Successful exit did not produce every declared regular output file')
    try:
        context_unchanged = (owner.workspace.artifact_hashes(binding.get('context_sha256', {}))
            == binding.get('context_sha256', {}))
    except (OSError, ValueError):
        context_unchanged = False
    if not context_unchanged:
        status, outputs = 'changed_inputs', {}
        issues.append('Observed implementation context changed during execution; no owner certification')
    saved = {**data, 'argv': binding['argv'], 'cwd': binding['cwd'],
        'schema_version': 'prepared_implementation_execution.v1',
        'prepared_result': request.prepared_result, 'approval': approval, 'status': status,
        **({'producer_completion': binding['producer_completion']} if 'producer_completion' in binding else {}),
        'unresolved': binding['unresolved'],
        'output_sha256': outputs, 'issues': issues,
        'verification': 'Physical invocation and byte versions only; no independent scientific or visual validation.'}
    if status == 'run_succeeded' and request.delivery is not None:
        delivery = (request.delivery if binding.get('producer_completion') == 'goal_findings.v1'
            else request.delivery.model_copy(update={'unresolved': list(dict.fromkeys([
                *request.delivery.unresolved, *binding['unresolved']]))}))
        saved['delivery'] = owner.workspace.resolve_delivery(delivery)
        saved['delivery_sha256'] = owner.workspace.artifact_hashes(saved['delivery']['artifacts'])
    store.write_json('candidate_execution.json', saved,
        kind='prepared_implementation_execution', schema='prepared_implementation_execution.v1')
    return _result(owner, store, saved)


def delivery_issues(owner: Any, observations: list[dict], *, delivery: dict | None = None,
                    declaration_adoptions: list[dict] | None = None) -> list[str]:
    # A later successful version supersedes an earlier failed candidate for
    # delivery, without rewriting its ledger or declaring the failure solved.
    executions = [row for row in observations if row.get('tool_name') == 'execute_implementation']
    scoped = getattr(owner, 'declaration_review', False)
    if scoped:
        from simple_ar.tools.producer_declarations import delivery_evidence
        from simple_ar.tools.implementation_work import _accepted
        try:
            preparations, executions = delivery_evidence(owner, observations, delivery)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ['Delivery producer/dependency evidence is unavailable or changed: ' + str(exc)]
        for prepared in preparations:
            if prepared.get('status') != 'ok':
                continue
            try:
                locator = prepared['data']['prepared_result']
                preparation = owner.workspace.resolve(locator).parent.parent
                _accepted(owner, {key: value for key, value in prepared.items()
                    if key not in {'arguments', 'record', 'current_request'}}, preparation.name)
                candidate, payload, source = candidate_source(owner, locator)
                hashes = {str(candidate / row['path']): row['sha256'] for row in payload['data']['files']}
                if owner.workspace.artifact_hashes(hashes) != hashes:
                    return ['Delivered preparation source versions changed after generation']
                if not any(row.get('data', {}).get('prepared_result')
                        and owner.workspace.resolve(row['data']['prepared_result']) == owner.workspace.resolve(locator)
                        for row in executions):
                    if (source['request'].get('complete_work')
                            or source['request'].get('producer_completion') == 'goal_findings.v1'
                                and source['request'].get('execution_requested')):
                        return ['Prepared complete work has no accepted execution; independent prose review cannot replace execution']
                    if (payload['data'].get('unresolved')
                            and source['request'].get('producer_completion') != 'goal_findings.v1'):
                        return ['Source-only candidate retains unresolved work without an applicable independent adoption: '
                            + '; '.join(payload['data']['unresolved'])]
            except (OSError, ValueError, KeyError, TypeError):
                return ['Delivered preparation lacks current, settled bound evidence']
        if delivery and any(row.get('data', {}).get('producer_completion') == 'goal_findings.v1'
                for row in preparations):
            # A source-only delivery cannot hide a previously inspected defect
            # by omitting the report. Only explicit targets at the same bytes
            # carry this obligation; notes and evidence quotes do not accuse files.
            try:
                from simple_ar.report.writing import validate_document_inspection
                from simple_ar.tools.writing import read_composition_review
                delivered = owner.workspace.resolve_delivery(DeliveryRequest.model_validate(delivery))['artifacts']
                hashes = owner.workspace.artifact_hashes(delivered)
                current = {owner.workspace.resolve(path): digest for path, digest in hashes.items()}
                for observed in observations:
                    data = observed.get('data') or {}
                    if observed.get('tool_name') != 'compose_document' or observed.get('status') != 'ok':
                        continue
                    defects = data.get('delivery_review', {}).get('defects', [])
                    if not any(current.get(owner.workspace.resolve(target['path'])) == target['sha256']
                            for defect in defects for target in defect['targets']):
                        continue
                    identity = owner.workspace.resolve(data['version']).name
                    store = ArtifactStore(owner.root / 'tools/writing' / identity)
                    _accepted(owner, {key: value for key, value in observed.items()
                        if key not in {'arguments', 'record', 'current_request'}}, identity)
                    if store.read_json('result.json')['data'] != data:
                        raise ValueError('Retained attachment findings differ from the accepted composition')
                    validate_document_inspection(store, store.read_json('request.json'), data,
                        review_result=read_composition_review(owner.workspace, data))
                    return ['Delivered attachment bytes retain an independently evidenced required finding; '
                        'repair and inspect the actual candidate, or disclose partial delivery']
            except (OSError, ValueError, KeyError, TypeError):
                return ['Current attachment finding evidence is unavailable or changed']
    current = [row for row in executions if row.get('current_request')]
    latest = executions if scoped else (current or executions)[-1:]
    # Historical contracts retain last-execution semantics. New contracts check
    # every owner actually reached from this delivery, not whichever ran last.
    for observed in latest:
        try:
            data = observed['data']
            store = ArtifactStore(owner.workspace.resolve(data['execution_ref']).parent)
            saved = store.read_json('candidate_execution.json')
            if observed.get('status') != 'ok' or saved['status'] != 'run_succeeded':
                return ['Prepared execution has unresolved failures; inspect its original process receipt']
            if scoped:
                from simple_ar.tools.implementation_work import _execution
                checked, _ = _execution(owner, data['execution_ref'], successful=True)
                if checked != saved:
                    return ['Delivered execution differs from its accepted physical receipt']
            if saved.get('unresolved'):
                from simple_ar.tools.implementation_work import _request
                request, _ = _request(owner, saved['prepared_result'])
                from simple_ar.tools.producer_declarations import declaration_group
                if (request.producer_completion != 'goal_findings.v1' and (request.work_contract != 'shared.v2'
                        or declaration_group(owner, data['execution_ref']) not in (declaration_adoptions or []))):
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
