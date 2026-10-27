"""Reusable source preparation; no continuation, adoption or execution control."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from simple_ar.core.capabilities import ArtifactStore

PROFESSIONAL_GENERATION = 'professional_decision.v1'
REGISTERED_PROJECT_GENERATION = 'professional_project.v1'
SOURCE_OWNER_CONTRACT = 'source_owner.v1'


def material_observation_metadata(binding: dict) -> dict:
    """Expose retained reader snapshots, never synthesize reads of authored files."""
    from copy import deepcopy

    if binding.get('source_owner_contract') != SOURCE_OWNER_CONTRACT:
        return {}
    return {'material_observations': [
        {'request': deepcopy(row['request']), 'data': deepcopy({key: value
            for key, value in row.items() if key not in {'request', 'role', 'observation_ref'}})}
        for row in binding.get('source_windows', [])
        if 'request' in row and isinstance(row.get('text'), str) and row.get('sha256')
    ]}


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

    contract = getattr(owner, 'source_owner_contract', None)
    request_data = request.model_dump()
    if store.exists('request.json'):
        saved = store.read_json('request.json')
        if saved.get('source_owner_contract') not in (None, SOURCE_OWNER_CONTRACT):
            raise ValueError('Unknown retained source owner contract')
        if saved.get('source_decision_protocol') not in (None, 'native.v1'):
            raise ValueError('Unknown retained source decision protocol')
        if saved.get('source_edit_contract') not in (None, 'bound_content.v1'):
            raise ValueError('Unknown retained source edit contract')
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
        # A goal-only delegation inherits already mounted user-authorized
        # sources, never a scan of the internal workspace or journal tree.
        # No mounts is a valid greenfield request from requirements alone.
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
        source_owner_paths=source_owner_paths, input_contract=owner.input_contract,
        edit_contract=owner.source_edit_contract)
    sources.requested_files = [row.model_dump() for row in request.files]
    if owner.work_context is not None and not bounded_reuse:
        # Historical fresh work keeps its original context placement. Cached
        # preparations returned above keep their saved bytes and contracts.
        context_window = freeze_work_context(owner.work_context,
            list(dict.fromkeys([*sources.roots, *(row['path'] for row in originals.values())])))
        windows.append(context_window)
        sources.windows.append(context_window)
    if request.repair_review and repair_observation is not None:
        from simple_ar.tools.implementation_work import _repair_view
        repair_observation = _repair_view(
            owner, request.repair_review, repair_observation, originals, sources.windows,
            dynamic_windows=bounded_reuse)
    observations = ([repair_observation] if repair_observation is not None else
        owner.execution_observations() if owner.execution_observations else [])
    binding = {'generation_method': REGISTERED_PROJECT_GENERATION if retained else PROFESSIONAL_GENERATION,
        'request': request_data,
        **({'source_owner_contract': contract} if contract else {}),
        **({'source_decision_protocol': owner.decision_protocol} if owner.decision_protocol else {}),
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
        decision_protocol=owner.decision_protocol, source_owner_contract=contract,
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

