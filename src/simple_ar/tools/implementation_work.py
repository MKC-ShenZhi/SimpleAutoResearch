"""Receipt-derived professional continuation; the existing action loop schedules it."""
from __future__ import annotations

import re
import hashlib
from pathlib import Path
from typing import Any

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.implementation_run import candidate_source, execution_binding
# Compatibility export for existing domain callers; stop proof and disposition
# have a single implementation in the common outcome boundary.
from simple_ar.tools.professional_outcomes import stop_decision


PROFESSIONAL_GENERATION = 'professional_decision.v1'
REGISTERED_PROJECT_GENERATION = 'professional_project.v1'


def _repair_view(owner: Any, locator: str, observed: dict,
                 originals: dict, windows: list[dict], *, dynamic_windows: bool = False) -> dict:
    """Project one verified diagnosis; retain the full inspection as authority."""
    import copy
    from simple_ar.tools.writing import read_repair_review

    record = owner.workspace.resolve(locator)
    # Review admission and material handoff must select the same diagnosis.
    # An unadopted candidate can expose a producer defect while the adopted
    # manuscript's own inspection legitimately remains successful and empty.
    inspection = read_repair_review(owner.workspace, observed['data'])['delivery_review']
    defects = copy.deepcopy(inspection['defects'])
    available = [
        {'path': row['path'], 'sha256': row['sha256'], 'text': row['text'],
         'container': 'original_files', 'key': key}
        for key, row in originals.items() if isinstance(row.get('text'), str)
    ]
    available.extend(
        {'path': row['request']['path'], 'sha256': row['sha256'],
         'text': row['text'], 'container': 'source_windows', 'index': index}
        for index, row in enumerate([] if dynamic_windows else windows)
        if 'request' in row and isinstance(row.get('text'), str) and row.get('sha256')
    )
    for defect in defects:
        for quote in defect.get('finding', {}).get('evidence_quotes', []):
            text = quote.get('quote')
            if not isinstance(text, str) or not text:
                continue
            for source in available:
                start = source['text'].find(text)
                if start < 0:
                    continue
                # These are character offsets within the supplied text, not byte
                # offsets in the file. Original review pointers remain untouched.
                quote.pop('quote')
                quote['supplied_text_reference'] = {
                    key: value for key, value in source.items() if key != 'text'}
                quote['supplied_text_reference'].update(
                    start_char=start, end_char=start + len(text),
                    quote_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest())
                break
    return {
        'tool_name': observed['tool_name'],
        'diagnosis_record': {'path': locator,
            'sha256': hashlib.sha256(record.read_bytes()).hexdigest()},
        'delivery_review': {'status': inspection['status'],
            'defects': defects},
    }


def prepare_work(owner: Any, request: Any, store: ArtifactStore, output: Path,
                 project: Path | None, identity: str, repair_observation: dict | None,
                 *, retained_project: dict | None = None) -> Any:
    """Use the existing writer with on-demand reads; no separate selection phase."""
    from simple_ar.code_task.generation.selection import (
        ImplementationSources, bind_implementation_originals, preserved_input_hashes, registered_project_files,
    )
    from simple_ar.code_task.generation.writer import write_implementation_unit
    from simple_ar.tools.materials import freeze_work_context, observed_window_key
    from simple_ar.tools.workspace import FileWindow
    from simple_ar.tools.specs import ToolResult

    request_data = request.model_dump()
    if store.exists('request.json'):
        saved = store.read_json('request.json')
        if saved['generation_method'] not in {PROFESSIONAL_GENERATION, REGISTERED_PROJECT_GENERATION} or saved['request'] != request_data:
            raise ValueError('Professional generation request changed; preserve the original attempt')
        if not store.exists('result.json'):
            raise ValueError('Interrupted professional decision; inspect retained steps and accounting, no blind replay')
        result = store.read_json('result.json')
        hashes = {str(output / row['path']): row['sha256'] for row in result['data']['files']}
        inventory = owner.workspace.bind_inventory(output, identity=identity)
        if inventory['file_hashes'] != hashes:
            raise ValueError('Prepared candidate inventory changed; do not replay the professional decision')
        inputs = {row['path']: row['sha256'] for row in saved.get('original_files', {}).values()}
        inputs.update({str(owner.workspace.resolve(row['request']['path'])): row['sha256']
            for row in saved['source_windows'] if 'request' in row})
        inputs.update(preserved_input_hashes(owner.workspace, saved.get('preserved_files', {}),
            contract=saved.get('preserved_input_contract')))
        if owner.workspace.artifact_hashes(inputs) != inputs:
            raise ValueError('Professional source inputs changed; retain the original attempt')
        if prior := saved.get('prior_candidate'):
            if saved['generation_method'] == REGISTERED_PROJECT_GENERATION:
                registered_project_files(owner.workspace, prior)
            previous = owner.workspace.resolve(prior['result_ref'])
            if (hashlib.sha256(previous.read_bytes()).hexdigest() != prior['result_sha256']
                    or hashlib.sha256((previous.parent / 'request.json').read_bytes()).hexdigest()
                        != prior['source_binding_sha256']):
                raise ValueError('Prior registered source binding changed')
        return ToolResult(tool_name='prepare_implementation', **result)

    for locator in request.material_roots:
        material = owner.workspace.resolve_material_root(locator)
        if material == output or material.is_relative_to(output) or output.is_relative_to(material):
            raise ValueError('Material roots must be separate from the candidate output')
    retained = (retained_project if retained_project is not None else
        owner._retained_candidate(project, identity=identity, complete_project=True) if not request.files else None)
    if (request.repair_execution or request.repair_review) and retained is None:
        raise ValueError('Diagnosed repair requires the unchanged registered candidate')
    originals = (dict(retained['original_files']) if retained else bind_implementation_originals(
        [row.model_dump() for row in request.files], resolve_source=owner.workspace.resolve))
    windows = []
    for source in request.source_windows:
        arguments = source.model_copy(update={'full_digest': True}).model_dump(exclude_unset=True)
        observed = owner.workspace.call('read_file', arguments, identity=identity)
        if not observed.ok:
            raise ValueError(f'Original window unavailable: {observed.error}')
        windows.append({'request': arguments, **observed.data,
            'role': 'supplied_source_or_diagnostic_not_certified_fact'})
    bounded_reuse = request.producer_completion == 'goal_findings.v1'
    inherited, source_owner_paths, inherited_coverage, prior_roots = [], set(), None, []
    prior_binding = None
    if retained:
        previous = owner.workspace.resolve(retained['result_ref'])
        prior_binding = ArtifactStore(previous.parent).read_json('request.json')
    # Completion semantics and shared work ownership do not declare optional
    # source presentation. Only a parent that actually used the working-set
    # protocol can retire its reads into bounded optional inheritance.
    parent_working_set = (prior_binding.get('investigation', {}).get('working_set', {}).get('contract')
        if prior_binding is not None else None)
    if retained and bounded_reuse and parent_working_set == 'professional_working_set.v1':
        source_owner_paths = {str(owner.workspace.resolve(row['path']))
            for row in prior_binding.get('original_files', {}).values()}
        parent_windows = prior_binding['source_windows']
        total = sum('request' in row for row in parent_windows)
        inherited_coverage = {'available': total,
            'details': {'binding': str(previous.parent / 'request.json'),
                'sha256': retained['source_binding_sha256'], 'container': 'source_windows'}}
        prior_roots = list(prior_binding['request'].get('material_roots', []))
        prior_roots.extend(row['path'] for row in prior_binding['request'].get('source_windows', []))
        for index in range(len(parent_windows) - 1, -1, -1):
            row = parent_windows[index]
            if 'request' not in row:
                continue
            inherited.append({'request': row['request'],
                'data': {key: value for key, value in row.items()
                    if key not in {'request', 'role', 'observation_ref'}},
                'source_reference': {'binding': str(previous.parent / 'request.json'),
                    'sha256': retained['source_binding_sha256'], 'source_window_index': index}})
            if len(inherited) == 64:
                break
        inherited.reverse()
    elif retained:
        for row in retained['source_windows']:
            coverage = observed_window_key(owner.workspace, FileWindow.model_validate(row['request']), row)
            arguments = FileWindow(path=row['request']['path'], offset_bytes=coverage[1],
                max_bytes=max(1, coverage[2] - coverage[1]), full_digest=True).model_dump(exclude_unset=True)
            observed = owner.workspace.call('read_file', arguments, identity=identity)
            if (not observed.ok or observed.data['sha256'] != row['sha256']
                    or observed.data['text'] != row['text']):
                raise ValueError('Retained material changed; no automatic revision')
            windows.append({'request': arguments, **observed.data,
                'role': 'retained_observed_material_not_certified_fact'})
    roots = list(dict.fromkeys([*request.material_roots,
        *(row['request']['path'] for row in windows if 'request' in row)]))
    if bounded_reuse:
        # Optional stale/invalid parent references must not become mandatory
        # root admissions. Valid roots preserve on-demand access even when the
        # entire previous working set is larger than the presentation ceiling.
        for locator in [*prior_roots, *(row['request'].get('path') for row in inherited)]:
            try:
                path = owner.workspace.resolve_material_root(locator)
                if path.exists() and str(path) not in roots:
                    roots.append(str(path))
            except (ValueError, KeyError, TypeError, OSError, RuntimeError):
                pass  # The shared seed records the exact refusal and reference.
    if not roots and project is None:
        roots = [str(path) for name, path in owner.workspace.mounts.items()
            if name not in {'workspace', 'records'}]
    context_window = None
    if owner.work_context is not None and bounded_reuse:
        # Required current context spends the same presentation allowance as
        # file windows, before optional reuse reserves room for fresh reads.
        current_project = retained.get('project_root') if retained else str(project) if project else None
        context_window = freeze_work_context(owner.work_context, list(dict.fromkeys([
            *([current_project] if current_project else []), *roots,
            *(row['path'] for row in originals.values())])))
        windows.append(context_window)
    sources = ImplementationSources(workspace=owner.workspace,
        project=Path(retained['project_root']) if retained and 'project_root' in retained else project,
        editable_project=project if retained and 'editable_project' in retained else None, roots=roots,
        windows=windows, originals=originals, identity=identity, retained=retained is not None,
        material_observations=owner.material_observations,
        preserved_files=retained.get('preserved_files') if retained else None,
        registered_project=retained, diagnostic_context=bounded_reuse,
        inherited_observations=inherited, inherited_coverage=inherited_coverage,
        source_owner_paths=source_owner_paths, input_contract=owner.input_contract)
    sources.requested_files = [row.model_dump() for row in request.files]
    if owner.work_context is not None and not bounded_reuse:
        # Historical fresh work keeps its original context placement. Cached
        # preparations returned above keep their saved bytes and contracts.
        context_window = freeze_work_context(owner.work_context,
            list(dict.fromkeys([*sources.roots, *(row['path'] for row in originals.values())])))
        windows.append(context_window)
        sources.windows.append(context_window)
    if request.repair_review and repair_observation is not None:
        repair_observation = _repair_view(
            owner, request.repair_review, repair_observation, originals, sources.windows,
            dynamic_windows=bounded_reuse)
    observations = ([repair_observation] if repair_observation is not None else
        owner.execution_observations() if owner.execution_observations else [])
    binding = {'generation_method': REGISTERED_PROJECT_GENERATION if retained else PROFESSIONAL_GENERATION,
        'request': request_data,
        'argv_semantics': 'candidate_relative.v2',
        'source_windows': windows, 'original_files': originals,
        'execution_observations': observations, 'investigation': sources.context(),
        **({'journal_observations': {'coverage': sources.seed_coverage}}
            if bounded_reuse else {}),
        **({'material_contract': 'authoring_context.v1'}
            if request.producer_completion == 'goal_findings.v1' else {}),
        **({'prior_candidate': {key: value for key, value in retained.items()
            if key not in {'original_files', 'source_windows'}}} if retained else {})}
    advice = None
    if request.candidate_admission:
        from simple_ar.code_task.generation.dependencies import build_dependency_advice, dependency_observation_view
        if retained:
            prior_store = ArtifactStore(owner.workspace.resolve(retained['result_ref']).parent)
            advice = prior_store.read_json('request.json').get('dependency_advice')
        if advice is None:
            advice = build_dependency_advice(request.goal, python_executable=str(owner.workspace.python))
        binding.update(candidate_admission=request.candidate_admission, dependency_advice=advice,
            dependency_observation=dependency_observation_view(advice))
    sources.retain_binding(binding, store)
    result = write_implementation_unit(project_dir=output, goal=request.goal, files=None,
        source_windows=sources.windows, original_files=originals, client=owner._client(identity),
        artifact_dir=store.root, task_python=str(owner.workspace.python),
        execution_observations=observations, execution_requested=request.execution_requested, investigation=sources,
        candidate_admission=bool(request.candidate_admission), dependency_advice=advice,
        command_source_binding=binding,
        validate_destination=lambda name: owner._feedback_destination(output, name, retained))
    sources.validate()
    if retained and owner._retained_candidate(project, identity=identity, complete_project=True) != retained:
        raise ValueError('Retained candidate changed during the professional decision')
    if sources.preserved_files:
        binding['preserved_files'] = sources.preserved_files
    sources.retain_binding(binding, store)
    result['workspace_scope'] = sources.context()['workspace_scope']
    result['investigation'] = {'observed_windows': sum('request' in row for row in windows),
        'record': str(store.root / 'request.json'), 'decision_steps': str(store.root / 'decision-steps')}
    return owner._publish_implementation(identity, store, result)


def _accepted(owner: Any, observed: dict, identity: str) -> ArtifactStore:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
        raise ValueError('Invalid accepted attempt identity')
    store = ArtifactStore(owner.root / 'attempts' / identity)
    ref = owner._registered_ref(store.root / 'native_result.json',
        kind='native_observation', store_root=store.root)
    if store.read_json(ref) != observed:
        raise ValueError('Continuation requires the exact accepted native result')
    if any(row.attempt_id == identity and row.status in {'reserved', 'unknown'}
            for row in owner.ledger.entries):
        raise ValueError('Accepted work still has unsettled physical accounting')
    return store


def _execution(owner: Any, locator: str, *, successful: bool = False) -> tuple[dict, dict]:
    record = owner.workspace.resolve(locator)
    relative = record.relative_to(owner.root / 'attempts').parts
    if len(relative) != 2 or relative[1] != 'candidate_execution.json':
        raise ValueError('Use a registered implementation execution receipt')
    store = ArtifactStore(record.parent)
    ref = owner._registered_ref(record, kind='prepared_implementation_execution', store_root=store.root)
    if ref.schema != 'prepared_implementation_execution.v1':
        raise ValueError('Unknown execution receipt schema')
    saved = store.read_json(ref)
    observed = store.read_json('native_result.json')
    _accepted(owner, observed, relative[0])
    from simple_ar.tools.implementation_run import _result
    if _result(owner, store, saved).model_dump(mode='json') != observed:
        raise ValueError('Execution differs from its accepted native result')
    statuses = {'run_succeeded'} if successful else {'process_failed', 'missing_outputs'}
    if saved['status'] not in statuses:
        raise ValueError('Execution does not have the required settled outcome for this diagnosis')
    if store.read_json('execution_input.json') != saved['approval']:
        raise ValueError('Execution differs from its retained exact approval')
    process_path = owner.root / 'tools' / 'processes' / relative[0] / 'invocation.json'
    if (owner.root / saved['receipt']).resolve() != process_path.resolve():
        raise ValueError('Execution has no original physical receipt')
    process = ArtifactStore(process_path.parent).read_json(process_path.name)
    binding = saved['approval']['binding']
    if saved.get('producer_completion') is not None or binding.get('producer_completion') is not None:
        from simple_ar.tools.implementation_run import candidate_source
        _, prepared, source = candidate_source(owner, saved['prepared_result'])
        completion = source['request'].get('producer_completion')
        investigation = source['request'].get('result_investigation')
        if (saved.get('result_investigation') != investigation
                or binding.get('result_investigation') != investigation
                or prepared['data'].get('result_investigation') != investigation):
            raise ValueError('Result investigation responsibility differs from the approved preparation')
        if (saved.get('producer_completion') != completion or binding.get('producer_completion') != completion
                or saved.get('unresolved') != prepared['data'].get('unresolved', [])
                or binding.get('unresolved') != saved.get('unresolved')):
            raise ValueError('Execution notes or completion semantics differ from the approved preparation')
        from simple_ar.tools.implementation_run import bind_command_input_versions, execution_input_versions, prepared_unit
        unit, candidate, _ = prepared_unit(owner, saved['prepared_result'])
        if ('context_sha256' in binding) != ('context_sha256' in unit):
            raise ValueError('Execution material roles differ from their frozen preparation')
        context = binding.get('context_sha256', {})
        if 'context_sha256' in unit:
            bind_command_input_versions(owner, unit, candidate, binding['argv'])
        if (context != unit.get('context_sha256', {}) or any(
                binding['source_sha256'].get(path) != digest for path, digest in context.items())):
            raise ValueError('Execution context cannot remove a required source dependency')
        if 'context_sha256' in binding:
            actual = (process.get('input_binding') or {}).get('workspace_artifacts') or {}
            expected = execution_input_versions(binding)
            if (actual.get('version') != 1 or owner.workspace.resolved_versions(actual.get('inputs', {}))
                    != owner.workspace.resolved_versions(expected)):
                raise ValueError('Physical execution dependencies differ from their approved material roles')
    if (process['invocation_id'] != relative[0] or process['status'] != 'finished'
            or type(process.get('returncode')) is not int or process.get('stop_reason')
            or successful and process['returncode'] != 0
            or process['argv'] != binding['argv'] or process['cwd'] != binding['cwd']):
        raise ValueError('Physical invocation is interrupted, unknown or differs from approval')
    entries = [row for row in owner.ledger.entries
        if row.reservation_id == f'process-{relative[0]}']
    if (len(entries) != 1 or entries[0].status != 'settled'
            or entries[0].purpose != 'local_process' or entries[0].actual_source != 'executor_wall_clock'
            or entries[0].reserved != {'process_invocations': 1, 'process_wall_seconds': binding['plan']['timeout_sec']}
            or entries[0].actual != {'process_invocations': 1, 'process_wall_seconds': process['duration_sec']}):
        raise ValueError('Physical invocation is not settled in the shared ledger')
    from simple_ar.core.process import process_observation
    actual = process_observation(process, process_path.parent)
    if any(saved.get(key) != actual.get(key) for key in ('returncode', 'stop_reason', 'stdout', 'stderr')):
        raise ValueError('Retained diagnostics differ from the physical receipt')
    if owner.workspace.artifact_hashes(binding['source_sha256']) != binding['source_sha256']:
        raise ValueError('Execution inputs changed; no automatic repair')
    if successful and (not saved['output_sha256']
            or owner.workspace.artifact_hashes(saved['output_sha256']) != saved['output_sha256']):
        raise ValueError('Successful execution outputs changed; no review-based repair')
    return saved, observed


def _request(owner: Any, locator: str) -> tuple[Any, Any]:
    candidate, _, source = candidate_source(owner, locator)
    return _retained_request(source), candidate


def _retained_request(source: dict) -> Any:
    from simple_ar.tools.implementation import ImplementationRequest
    request = dict(source['request'])
    # Before conditional default-one serialization, absent persisted ceilings
    # meant zero. A receipt must never inherit a later public-request default.
    if request.get('complete_work') and 'max_repairs' not in request:
        request['max_repairs'] = 0
    return ImplementationRequest.model_validate(request)




def _same_producer(owner: Any, left: dict | None, right: dict | None) -> bool:
    if left is None or right is None:
        return left is right
    return ({key: value for key, value in left.items() if key not in {'inputs', 'outputs'}}
            == {key: value for key, value in right.items() if key not in {'inputs', 'outputs'}}
        and owner.workspace.resolved_versions(left['inputs']) == owner.workspace.resolved_versions(right['inputs'])
        and owner.workspace.resolved_versions(left['outputs']) == owner.workspace.resolved_versions(right['outputs']))


def _review_execution(owner: Any, locator: str) -> tuple[dict, dict]:
    """Resolve a reviewed successful producer, never change its exit-zero record."""
    record = owner.workspace.resolve(locator)
    relative = record.relative_to(owner.root / 'attempts').parts
    if len(relative) != 2 or relative[1] != 'native_result.json':
        raise ValueError('Review repair needs records/<compose-attempt>/native_result.json')
    observed = ArtifactStore(record.parent).read_json(record.name)
    from simple_ar.tools.writing import (
        accepted_composition_receipt, check_composition_inputs, read_repair_review, read_composition_candidate)
    writing, binding, data = accepted_composition_receipt(root=owner.root, workspace=owner.workspace,
        session_id=owner.session_id, ledger=owner.ledger, identity=relative[0], observed=observed)
    # This existing read-only byte consumer needs only the shared workspace;
    # no writing runtime, client or recovery owner is instantiated here.
    check_composition_inputs(owner.workspace, data['inputs'])
    # A rejected prose candidate may uncover genuine defects in unchanged
    # attachments. Repair authority follows that verified observation, while
    # publication continues to consume only the adopted manuscript's review.
    review = read_repair_review(owner.workspace, data)
    inspection = review['delivery_review']
    if (review.get('review_status') != 'needs_revision'
            or inspection.get('status') != 'needs_revision' or not inspection.get('defects')):
        raise ValueError('Composition has no independently bound delivery defects')
    candidate = read_composition_candidate(owner.workspace, data)
    from simple_ar.report.writing import validate_document_inspection
    validate_document_inspection(writing, binding, {**data, 'delivery_review': inspection},
        review_result=review, reviewed_sections=candidate['sections'] if candidate else None)

    from simple_ar.report.review_evidence import delivery_target_pointer, SCOPED_DELIVERY_INSPECTION
    snapshot = {owner.workspace.resolve(row['path']): (index, row)
        for index, row in enumerate(binding['delivery_review']['artifacts'])}
    producer, targets = None, []
    unit_owners = None
    for defect in inspection['defects']:
        if defect.get('type') != 'delivery_artifact_defect' or not defect.get('targets'):
            raise ValueError('Review repair needs concrete delivery artifact defects')
        for target in defect['targets']:
            path, digest = target['path'], target['sha256']
            resolved = owner.workspace.resolve(path)
            current = snapshot.get(resolved)
            frozen = current[1] if current is not None else None
            if (frozen is None or delivery_target_pointer(frozen, current[0]) is None
                    or digest != frozen['sha256']
                    or not _same_producer(owner, target.get('producer'), frozen.get('producer'))
                    or len(target.get('consumers', [])) != len(frozen.get('consumers', []))
                    or any(not _same_producer(owner, left, right) for left, right in
                        zip(target.get('consumers', []), frozen.get('consumers', [])))):
                raise ValueError('Review target differs from the actual frozen inspection subject')
            from simple_ar.tools.materials import read_bound_material_bytes, inspect_bound_material
            admission = binding['delivery_review'].get('text_admission')
            limit = 32000
            if admission is not None:
                if (admission.get('contract') not in {
                        'professional_unit_text.v1', 'professional_unit_text.v2', SCOPED_DELIVERY_INSPECTION}
                        or admission.get('max_text_bytes') != 256000):
                    raise ValueError('Unknown frozen review text admission')
                limit = 256000
            if frozen['coverage'] == 'table_profile':
                if (not admission or admission.get('contract') != SCOPED_DELIVERY_INSPECTION
                        or admission.get('max_profile_bytes') != 96000
                        or admission.get('table_reader_max_mb') != 20):
                    raise ValueError('Profile repair requires its scoped inspection admission')
                actual_view = inspect_bound_material(owner.workspace, path, digest,
                    text_bytes=0, profile_bytes=admission['max_profile_bytes'])
                if (actual_view.get('coverage') != 'table_profile'
                        or actual_view.get('profile') != frozen['profile']):
                    raise ValueError('Reviewed profile differs from the actual bound table view')
            else:
                raw = read_bound_material_bytes(owner.workspace, path, digest, max_bytes=limit)
                if raw.decode('utf-8') != frozen.get('text'):
                    raise ValueError('Reviewed target text is not the actual complete inspection window')
            actual = owner.workspace.artifact_provenance([path])[path]
            if actual['sha256'] != digest or not _same_producer(owner, actual['producer'], target.get('producer')):
                raise ValueError('Reviewed artifact bytes or actual provenance changed')
            owners = [target['producer']] if target.get('producer') else target.get('consumers', [])
            if not owners and binding.get('document_work', {}).get('producer_units'):
                from simple_ar.tools.producer_declarations import professional_unit_input_owners
                if unit_owners is None:
                    unit_owners = professional_unit_input_owners(owner, binding['document_work']['producer_units'])
                owners = unit_owners.get(str(resolved), [])
            if not owners:
                raise ValueError('Reviewed defect has no registered process producer or consumer')
            for proposed in owners:
                if producer is not None and not _same_producer(owner, producer, proposed):
                    raise ValueError('Delivery defects belong to multiple or inconsistent producers')
                producer = proposed
                outputs = proposed['outputs']
                if not outputs or any(not _same_producer(owner, row['producer'], proposed)
                        for row in owner.workspace.artifact_provenance(outputs).values()):
                    raise ValueError('Defect producer differs from actual output provenance')
                if not target.get('producer') and owner.workspace.resolved_versions(proposed['inputs']).get(resolved) != digest:
                    raise ValueError('Reviewed code is not an actual input of the selected producer')
            targets.append(target)
    if not producer:
        raise ValueError('Delivery inspection has no uniquely bound producer')
    execution, _ = _execution(owner,
        f"records/{producer['invocation_id']}/candidate_execution.json", successful=True)
    approved = execution['approval']['binding']
    from simple_ar.tools.implementation_run import execution_input_versions
    process_path = owner.root / execution['receipt']
    if (producer['receipt'] != execution['receipt']
            or hashlib.sha256(process_path.read_bytes()).hexdigest() != producer['receipt_sha256']
            or owner.workspace.resolved_versions(producer['inputs']) != owner.workspace.resolved_versions(execution_input_versions(approved))
            or owner.workspace.resolved_versions(producer['outputs']) != owner.workspace.resolved_versions(execution['output_sha256'])):
        raise ValueError('Reviewed producer differs from its approved successful execution')
    candidate, payload, _ = candidate_source(owner, execution['prepared_result'])
    sources = {owner.workspace.resolve(str(candidate / row['path'])) for row in payload['data']['files']}
    inputs = owner.workspace.resolved_versions(execution_input_versions(approved))
    outputs = owner.workspace.resolved_versions(execution['output_sha256'])
    for target in targets:
        path = owner.workspace.resolve(target['path'])
        if target.get('producer'):
            if outputs.get(path) != target['sha256']:
                raise ValueError('Reviewed target is not this execution\'s original output')
        elif path not in sources or inputs.get(path) != target['sha256']:
            raise ValueError('Reviewed code is outside the original candidate source inventory')
    return execution, observed


def repair_request(owner: Any, locator: str) -> tuple[dict, dict]:
    """Rebuild repair arguments and diagnostics from accepted lineage only."""
    saved, observed = _execution(owner, locator)
    return _repair_arguments(owner, saved, observed, locator, 'repair_execution')


def review_repair_request(owner: Any, locator: str) -> tuple[dict, dict]:
    """Return strict prepare arguments and the accepted composition diagnosis."""
    saved, observed = _review_execution(owner, locator)
    return _repair_arguments(owner, saved, observed, locator, 'repair_review')


def review_repair_outcome(owner: Any, observed: dict, source: str) -> tuple[dict, dict]:
    """Accept only a writing source-gap receipt pointing to its admitted diagnosis.

    This unwraps evidence, never an action. The original review repair owner
    still proves independent inspection, exact producer/version, permissions,
    accounting and repair lineage before proposing any implementation work.
    """
    if observed.get('status') == 'ok':
        return review_repair_request(owner, 'records/' + source.removeprefix('attempts/'))
    from simple_ar.tools.professional_outcomes import accepted_native_result

    data = observed.get('data') or {}
    diagnosis = data.get('producer_diagnosis') or {}
    if (observed.get('tool_name') != 'compose_document' or observed.get('status') != 'error'
            or data.get('owner_stop_reason') not in {'producer_repair_required', 'attachment_evidence_unresolved'}
            or diagnosis.get('contract') != 'document_source_gap.v1'):
        raise ValueError('Outcome is not a writing producer diagnosis')
    identity = Path(source).parent.name
    if Path(source).as_posix() != f'attempts/{identity}/native_result.json':
        raise ValueError('Producer diagnosis requires its actual native receipt')
    accepted_native_result(owner.root, owner.session_id, observed, identity)
    if any(row.attempt_id == identity and row.status in {'reserved', 'unknown'} for row in owner.ledger.entries):
        raise ValueError('Writing source diagnosis still has unsettled accounting')
    admitted = ArtifactStore(owner.root / 'tools/writing' / identity)
    if hashlib.sha256(admitted.resolve('selection-input.json').read_bytes()).hexdigest() != diagnosis['admission_sha256']:
        raise ValueError('Writing source diagnosis admission changed')
    work = admitted.read_json('selection-input.json')['work_source']
    if work.get('contract') != 'document_source.v1':
        raise ValueError('Producer diagnosis has no admitted document source')
    origin = diagnosis['origin']
    from simple_ar.tools.professional_outcomes import native_result_path
    from simple_ar.tools.writing import accepted_composition_receipt
    prior_path = native_result_path(owner.root, owner.workspace, diagnosis['source_result'])
    prior_id = prior_path.parent.name
    if (prior_id == identity
            or diagnosis['source_result'] != f'records/{prior_id}/native_result.json'):
        raise ValueError('Producer diagnosis has no distinct original composition')
    intent = work['intent']
    if intent.get('prior_result'):
        locator = intent['prior_result']
        selected = owner.workspace.resolve(str(owner.root / locator) if locator.startswith('attempts/') else locator)
        if selected != prior_path:
            raise ValueError('Producer diagnosis differs from its admitted result source')
    elif not intent.get('prior_draft'):
        raise ValueError('Producer diagnosis differs from its admitted draft source')
    prior = ArtifactStore(owner.root / 'attempts' / prior_id).read_json('native_result.json')
    writing, _, saved = accepted_composition_receipt(root=owner.root, workspace=owner.workspace,
        session_id=owner.session_id, ledger=owner.ledger, identity=prior_id, observed=prior)
    if hashlib.sha256(prior_path.read_bytes()).hexdigest() != diagnosis['source_sha256']:
        raise ValueError('Producer diagnosis original native receipt changed')
    # The source locator names the physical native attempt; the frozen origin
    # binds the logical Writer receipts, including after continuation.
    if (origin['owner'] != writing.root.name
            or set(origin['receipt_sha256']) != {'request.json', 'result.json'} or any(
            hashlib.sha256(writing.resolve(name).read_bytes()).hexdigest() != value
            for name, value in origin['receipt_sha256'].items())):
        raise ValueError('Producer diagnosis original writing receipts changed')
    if (not intent.get('prior_result')
            and owner.workspace.resolve(intent['prior_draft']) != owner.workspace.resolve(saved['editable_markdown'])):
        raise ValueError('Producer diagnosis differs from its admitted draft source')
    paths = {saved['editable_markdown']}
    if saved.get('review_path'):
        paths.add(saved['review_path'])
    expected = {path: saved['artifact_sha256'][path] for path in paths}
    if origin['artifact_sha256'] != expected or owner.workspace.artifact_hashes(expected) != expected:
        raise ValueError('Producer diagnosis candidate or inspection version changed')
    return review_repair_request(owner, diagnosis['source_result'])


class WorkStopped(ValueError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def _failure_signature(saved: dict, candidates: list[Path]) -> tuple | None:
    if saved.get('status') not in {'process_failed', 'missing_outputs'}:
        return None
    diagnostic = saved.get('stderr') or saved.get('stdout') or '\n'.join(saved.get('issues', []))
    if not diagnostic:
        return None  # Exit code alone cannot prove repeated identical errors.
    for candidate in sorted(candidates, key=lambda path: len(str(path)), reverse=True):
        for spelling in dict.fromkeys((str(candidate), candidate.as_posix())):
            diagnostic = diagnostic.replace(spelling, '<candidate>')
    return saved['status'], saved.get('returncode'), saved.get('stop_reason'), diagnostic


def _repair_arguments(owner: Any, saved: dict, observed: dict, locator: str, kind: str) -> tuple[dict, dict]:
    candidate, _, diagnosed_source = candidate_source(owner, saved['prepared_result'])
    request = _retained_request(diagnosed_source)
    if not request.complete_work:
        raise ValueError('This candidate did not opt into complete_work')
    current, seen, repairs = request, {(kind, locator)}, 0
    shared = request.work_contract in {'shared.v1', 'shared.v2'}
    outcomes, candidates = [saved], [candidate]
    prior_candidates = {owner.workspace.resolve(saved['prepared_result'])}
    excluded = {'project_path', 'files', 'repair_execution', 'repair_review'}
    while current.repair_execution or current.repair_review:
        mode = 'repair_execution' if current.repair_execution else 'repair_review'
        previous = getattr(current, mode)
        if (mode, previous) in seen or not shared and repairs >= 2:
            raise ValueError('Cyclic or excessive repair lineage')
        seen.add((mode, previous))
        prior, _ = (_execution(owner, previous) if mode == 'repair_execution'
            else _review_execution(owner, previous))
        _, _, source = candidate_source(owner, saved['prepared_result'])
        if source.get('prior_candidate', {}).get('result_ref') != prior['prepared_result']:
            raise ValueError('Repair lineage does not reuse its diagnosed candidate')
        previous_path = owner.workspace.resolve(prior['prepared_result'])
        if previous_path in prior_candidates:
            raise ValueError('Cyclic repair candidate lineage')
        prior_candidates.add(previous_path)
        previous_binding = source['prior_candidate']
        if (previous_binding.get('result_sha256') != hashlib.sha256(previous_path.read_bytes()).hexdigest()
                or previous_binding.get('source_binding_sha256') != hashlib.sha256(
                    (previous_path.parent / 'request.json').read_bytes()).hexdigest()):
            raise ValueError('Repair lineage prior source receipts changed')
        prior_request, prior_candidate = _request(owner, prior['prepared_result'])
        outcomes.append(prior)
        candidates.append(prior_candidate)
        expected_project = owner.workspace.resolve(previous_binding.get('editable_project', str(prior_candidate)))
        if owner.workspace.resolve(current.project_path) != expected_project:
            raise ValueError('Repair lineage uses a different project than its diagnosed candidate')
        current = prior_request
        if (not current.complete_work
                or current.model_dump(exclude=excluded) != request.model_dump(exclude=excluded)):
            raise ValueError('Repair lineage changed the original work contract')
        repairs += 1
        saved = prior
    if not shared and repairs >= request.max_repairs:
        raise WorkStopped('repair_ceiling', 'Technical repair ceiling reached')
    if shared and len(outcomes) >= 2:
        signature = _failure_signature(outcomes[0], candidates)
        if signature is not None and signature == _failure_signature(outcomes[1], candidates):
            raise WorkStopped('repair_stagnated', 'Consecutive verified executions repeated the same failure; no automatic retry')
    if owner._retained_candidate(candidate, identity=candidate.name, complete_project=True) is None:
        raise ValueError('Repair requires unchanged registered sources and materials')
    arguments = request.model_dump()
    arguments.pop('repair_execution', None)
    arguments.pop('repair_review', None)
    prior_scope = diagnosed_source.get('prior_candidate', {})
    editable = candidate
    if 'editable_project' in prior_scope:
        editable = candidate / Path(prior_scope['editable_project']).relative_to(Path(prior_scope['project_root']))
    arguments.update(project_path=str(editable), files=[], **{kind: locator})
    return arguments, observed


def affordable_work(owner: Any) -> None:
    """Reject known finite shortages, without claiming whole-work feasibility."""
    # Generation always needs at least one request and one physical invocation.
    # Unknown/unbounded dimensions retain the shared ledger's normal admission.
    for resource, minimum in (('llm_requests', 1), ('total_tokens', 1),
                              ('process_invocations', 1), ('process_wall_seconds', 1)):
        remaining = owner.ledger.remaining(resource)
        if remaining is not None and remaining < minimum:
            raise WorkStopped('shared_budget_exhausted', f'Complete work lacks minimum remaining {resource} capacity')


def resume_after_budget(owner: Any, observed: dict, attempt_id: str) -> dict:
    """Re-propose only a proved, undispatched preparation under a new identity.

    The dispatcher calls this after a new explicit resource grant. A partially
    performed generation is not a preflight refusal and cannot be replayed by
    replenishing its wallet; its caller must explicitly choose a fresh action.
    """
    from simple_ar.tools.implementation import ImplementationRequest
    try:
        store = _accepted(owner, observed, attempt_id)
        if (owner.exact is not None or observed.get('tool_name') != 'prepare_implementation'
                or observed.get('status') != 'error'
                or observed.get('data', {}).get('owner_stop_reason') != 'shared_budget_exhausted'):
            raise ValueError('Budget resume requires the original accepted preparation refusal')
        if any(row.status in {'reserved', 'unknown'} for row in owner.ledger.entries):
            raise WorkStopped('accounting_unresolved', 'Reconcile unknown or in-flight operations before any new preparation')
        refusal = observed['data'].get('budget_refusal')
        if not refusal:
            raise ValueError('Preparation refusal has no receipt-bound request; explicitly submit the original saved request as a new caller action')
        request = ImplementationRequest.model_validate(refusal['request'])
        if not request.complete_work:
            raise ValueError('Preparation refusal did not enter a complete-work owner')
        source = ArtifactStore(store.root / 'implementation')
        expected_hash = refusal.get('source_binding_sha256')
        if source.exists('request.json'):
            if (hashlib.sha256(source.resolve('request.json').read_bytes()).hexdigest() != expected_hash
                    or source.read_json('request.json')['request'] != request.model_dump()):
                raise ValueError('Refused preparation source contract changed')
            binding = source.read_json('request.json')
            hashes = {row['path']: row['sha256'] for row in binding.get('original_files', {}).values()}
            hashes.update({row['request']['path']: row['sha256']
                for row in binding.get('source_windows', []) if 'request' in row})
            from simple_ar.code_task.generation.selection import preserved_input_hashes
            hashes.update(preserved_input_hashes(owner.workspace, binding.get('preserved_files', {}),
                contract=binding.get('preserved_input_contract')))
            if binding.get('generation_method') == REGISTERED_PROJECT_GENERATION:
                from simple_ar.code_task.generation.selection import registered_project_files
                registered_project_files(owner.workspace, binding['prior_candidate'])
            if owner.workspace.artifact_hashes(hashes) != hashes:
                raise ValueError('Refused preparation materials changed; no automatic restart')
        elif expected_hash is not None:
            raise ValueError('Refused preparation source binding disappeared')
        activity = any(row.attempt_id == attempt_id for row in owner.ledger.entries)
        if type(refusal['physical_activity']) is not bool or refusal['physical_activity'] != activity:
            raise ValueError('Refused preparation accounting differs from its accepted observation')
        if activity:
            action = stop_decision(owner, observed, attempt_id, 'preparation_restart_requires_action',
                'Preparation already performed physical work. Inspect its retained steps, then explicitly call '
                'run(actions=[the requested_action below]) to start a new preparation under the same contract; '
                'all earlier attempts, charges and repair lineage remain. Resource authorization alone cannot replay it.')
            action['stop']['requested_action'] = {'tool_name': 'prepare_implementation',
                'arguments': request.model_dump()}
            return action
        if (source.exists('result.json')
                or owner.workspace.resolve(f'workspace/implementations/{attempt_id}').exists()):
            raise ValueError('Refused preparation has candidate bytes; inspect them before any restart')
        affordable_work(owner)
        return {'tool_name': 'prepare_implementation', 'arguments': request.model_dump()}
    except WorkStopped as exc:
        return stop_decision(owner, observed, attempt_id, exc.reason, str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return stop_decision(owner, observed, attempt_id, 'owner_state_unresolved', str(exc))


def continuation(owner: Any, observed: dict, attempt_id: str) -> dict | None:
    if owner.exact is not None:
        return None
    entered = False
    try:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id):
            return None
        store = ArtifactStore(owner.root / 'attempts' / attempt_id)
        # Determine responsibility before settlement validation. Unsettled work
        # must stop, not fall back to a coordinator repair or physical replay.
        if observed.get('tool_name') == 'prepare_implementation':
            source = ArtifactStore(store.root / 'implementation')
            entered = bool(observed.get('data', {}).get('owner_entered'))
            if source.exists('request.json'):
                entered = bool(source.read_json('request.json')['request'].get('complete_work'))
        elif observed.get('tool_name') == 'execute_implementation':
            prepared = (store.read_json('candidate_execution.json')['prepared_result']
                if store.exists('candidate_execution.json') else
                store.read_json('execution_input.json')['binding']['request']['prepared_result']
                if store.exists('execution_input.json') else None)
            if prepared:
                binding = ArtifactStore(owner.workspace.resolve(prepared).parent).read_json('request.json')
                entered = bool(binding['request'].get('complete_work'))
        store = _accepted(owner, observed, attempt_id)
        if observed['tool_name'] == 'prepare_implementation':
            if observed['status'] != 'ok':
                if entered:
                    return stop_decision(owner, observed, attempt_id,
                        observed.get('data', {}).get('owner_stop_reason', 'owner_failed'), observed.get('error', ''))
                return None  # Invalid/unadmitted tool inputs are still coordinator work.
            locator = f'records/{attempt_id}/implementation/result.json'
            if observed['data']['prepared_result'] != locator:
                if entered:
                    raise ValueError('Accepted preparation locator differs from its owner receipt')
                return None
            request, _ = _request(owner, locator)
            if not request.complete_work:
                return None
            arguments = {'prepared_result': locator}
            execution_binding(owner, arguments, identity=attempt_id)
            return {'tool_name': 'execute_implementation', 'arguments': arguments}
        if observed['tool_name'] == 'execute_implementation':
            locator = f'records/{store.root.name}/candidate_execution.json'
            record = store.read_json('candidate_execution.json') if store.exists('candidate_execution.json') else None
            if record is not None:
                request, _ = _request(owner, record['prepared_result'])
                if not request.complete_work:
                    return None
                entered = True
                if record['status'] == 'run_succeeded':
                    _execution(owner, locator, successful=True)
                    if request.result_investigation is not None:
                        return {'tool_name': 'investigate_implementation',
                            'arguments': {'execution_ref': locator}}
                    # These are inherited model-authored gaps, not a typed
                    # physical failure. Keep them in the immutable execution
                    # observation for writing/review and final delivery gates;
                    # their presence alone cannot prohibit that handoff.
                    return None
            elif store.exists('execution_input.json'):
                prepared = store.read_json('execution_input.json')['binding']['request']['prepared_result']
                request, _ = _request(owner, prepared)
                entered = request.complete_work
                if entered:
                    return stop_decision(owner, observed, attempt_id, 'execution_unresolved',
                        'Execution has no settled owner result; no replay or coordinator repair')
            if observed['data'].get('execution_ref') != locator:
                if entered:
                    raise ValueError('Accepted execution locator differs from its owner receipt')
                return None
            arguments, _ = repair_request(owner, locator)
            affordable_work(owner)
            return {'tool_name': 'prepare_implementation', 'arguments': arguments}
    except WorkStopped as exc:
        return stop_decision(owner, observed, attempt_id, exc.reason, str(exc))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        if entered:
            return stop_decision(owner, observed, attempt_id, 'owner_state_unresolved', str(exc))
        return None
    return None
