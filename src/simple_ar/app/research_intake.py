from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import hashlib
import json
import stat
from pathlib import Path
import tomllib
from typing import Any

from simple_ar.core.artifacts import write_json, write_text
from simple_ar.app.research_execution import UnsupportedExecutionCreation, compile_scoped_measurement
from simple_ar.research.workflow_contracts import Diagnostic, ResearchAsset, ResearchBrief


class ResearchInputError(ValueError):
    """Raised when a research input cannot be normalized safely."""

    def __init__(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics = tuple(diagnostics)
        message = "; ".join(item.message for item in self.diagnostics) or "Invalid research input"
        super().__init__(message)


def _legacy_creation_options(services: Any) -> dict:
    """Read old public creation settings only at the compatibility boundary.

    The object itself still carries runtime clients/registry to the native owner.
    Saved-task loading never enters this boundary.
    """
    return {name: getattr(services, name) for name in (
        'config', 'budget_limits', 'input_base_dir', 'max_results', 'max_chunks',
        'idea_limit', 'cache_dir', 'extraction_dir', 'max_attempts', 'max_no_progress')}


_DEFAULT_RESEARCH_PROVIDERS = ('openalex', 'semantic_scholar', 'arxiv')
_OUTPUT_ALIASES = {'experiment': 'experiments', 'code': 'code_task', 'code_patch': 'code_task',
    'patch': 'code_task', 'bug_repair': 'bug_fix'}


def requested_research_outputs(brief: ResearchBrief) -> tuple[str, ...]:
    outputs = (value.strip().lower() for value in brief.requested_outputs)
    return tuple(dict.fromkeys(_OUTPUT_ALIASES.get(value, value) for value in outputs)) or ('research_summary',)


def _creation_model_limits(limits: Mapping, *, model_enabled: bool, preserve_unlimited: bool = False) -> dict:
    if not model_enabled:
        return {'llm_requests': 0, 'total_tokens': 0}
    return {name: limits[name] if name in limits and (limits[name] is not None or preserve_unlimited) else default
        for name, default in (('llm_requests', 30), ('total_tokens', 600000))}


def declared_research_work(outputs: Iterable[str], sources: Iterable[Mapping],
                           research_config: Mapping | None, *, explicit_research: bool) -> bool:
    """Interpret source/design intent once for a caller-declared execution."""
    requested = {_OUTPUT_ALIASES.get(value.strip().lower(), value.strip().lower()) for value in outputs}
    return bool({'summary', 'research_summary'} & requested
        or (research_config is not None and any(row['role'] in {'paper', 'material'} for row in sources))
        or explicit_research or (research_config or {}).get('research_selected_idea_id'))


def create_structured_research_task(brief: ResearchBrief, *, root: str | Path,
                                    config: Mapping, budget_limits: Mapping,
                                    model: str | None, max_results: int,
                                    timeout_sec: int | None = None, research_work: bool | None = None,
                                    queries: Iterable[str] | None = None,
                                    providers: Iterable[str] | None = None,
                                    interaction: str | None = None, **creation_options):
    """Preserve structured-entry admission defaults without legacy service boxes.

    Source collection and declared execution had distinct resource contracts.
    Resolve them here before the common creation compiler; this adds no runtime
    or saved state. Callers retain their original input and cache path bases.
    """
    values, limits = dict(config), dict(budget_limits)
    runtime = creation_options.get('services')
    model_enabled = bool(model) or getattr(runtime, 'llm_client', None) is not None
    if values.get('execution') is None:
        defaults = dict(research_use_fulltext=False, research_allow_pdf_download=False,
            research_keep_raw_pdf=False, research_max_pdf_mb=20, research_max_pdf_pages=100,
            research_read_max_shortlist=max_results, research_max_iterations=0)
        values = {**defaults, **values}
        values['research_execution_boundary'] = {'timeout_sec': timeout_sec if timeout_sec is not None else 300}
        attempts = max(30, _creation_model_limits(limits, model_enabled=model_enabled)['llm_requests'] * 10)
    else:
        values['interaction'] = interaction or values.get('interaction') or 'checkpoints'
        if research_work:
            values.update(queries=list(queries or ()), providers=list(providers or _DEFAULT_RESEARCH_PROVIDERS))
        # Declared caller-directed execution never authorizes a model by budget alone.
        limits.update(_creation_model_limits(limits if model_enabled else {}, model_enabled=model_enabled))
        attempts = max(20, limits['llm_requests'] * 10 + 16)
    return create_research_task(brief, root=root, model=model, config=values, budget_limits=limits,
        _research_work=research_work if values.get('execution') is not None else None,
        max_results=max_results, max_attempts=attempts, max_no_progress=6, **creation_options)


def create_research_task(brief: ResearchBrief, *, root: str | Path, services: Any | None = None,
                         model: str | None = None, allow_vision: bool = False,
                         snapshot_sources: bool = False,
                         max_snapshot_bytes: int = 16 * 1024 * 1024,
                         _research_work: bool | None = None,
                         **creation_options):
    """Create without running; intake owns configuration and domain admission.

    Creation keywords are config, budget_limits, input_base_dir, max_results,
    max_chunks, idea_limit, cache_dir, extraction_dir, max_attempts and max_no_progress.
    Services supply runtime bindings only. Historical ResearchApplicationServices
    settings remain supported, but cannot be mixed with direct creation keywords.
    Image inspection requires explicit allow_vision and the native vision route.
    """
    if services is not None and hasattr(services, 'config'):
        if creation_options:
            raise ValueError('Choose direct creation settings or legacy services settings, not both')
        creation_options = _legacy_creation_options(services)
    return _create_research_task(brief, root=root, services=services, model=model,
        allow_vision=allow_vision, snapshot_sources=snapshot_sources,
        max_snapshot_bytes=max_snapshot_bytes, research_work=_research_work, **creation_options)


def revise_research_task(app: Any, *, research_settings: Mapping[str, Any] | None = None,
                         materials: Iterable[tuple[str | Path, str]] = (),
                         report_settings: Mapping[str, Any] | None = None,
                         report_requested: bool | None = None, report_method: str | None = None,
                         independent_review_required: bool | None = None,
                         resource_allowances: Mapping[str, int | float] | None = None,
                         additional_attempts: int | None = None, additional_no_progress: int | None = None,
                         authorization_id: str = '', authorization_reason: str = '') -> bool:
    """Apply explicit research revisions to the original common task and ledger."""
    from simple_ar.tools.sources import SourcePolicy
    from simple_ar.tools.workspace import resolve_source_mount

    saved = app.state['config']
    mounts = dict(saved['sources'])
    policy = dict(saved.get('research_policy') or {'allow_network': saved['allow_network']})
    selected = list(policy.get('materials', ()))
    has_source_work = bool(research_settings)
    confirm_protocol = bool((research_settings or {}).get('interaction') in {'assisted', 'checkpoints'}
        and saved.get('execution_contract') is not None and not policy.get('require_design'))
    if confirm_protocol:
        policy['require_design'] = True
    for locator, role in materials:
        path = str(resolve_source_mount(Path(locator)))
        alias = next((name for name, value in mounts.items() if value == path), None)
        if alias is None:
            index = 0
            while f'source{index}' in mounts:
                index += 1
            alias = f'source{index}'
            mounts[alias] = path
        if role in {'paper', 'material'}:
            if not Path(path).is_file():
                raise ValueError('Research material must be a file')
            if alias not in selected:
                selected.append(alias)
                has_source_work = True
    changes: dict[str, Any] = {}
    if mounts != saved['sources']:
        changes['sources'] = mounts
    aliases = {'research_use_fulltext': 'require_fulltext',
        'max_research_iterations': 'max_iterations'}
    for name, value in (research_settings or {}).items():
        if name == 'research_materials_only':
            policy['allow_network'] = not value
            changes['allow_network'] = not value
        else:
            target = aliases.get(name, name.removeprefix('research_'))
            if target == 'cache_dir' and value is not None:
                value = str(Path(value).expanduser().resolve())
            if target not in SourcePolicy.model_fields:
                raise ValueError('Unknown research revision: ' + name)
            policy[target] = value
    if has_source_work:
        policy['materials'] = selected
        normalized = SourcePolicy.model_validate(policy).model_dump(mode='json')
        if normalized != saved.get('research_policy'):
            changes['research_policy'] = normalized
    if changes.get('allow_network') == saved.get('allow_network'):
        changes.pop('allow_network')
    if independent_review_required is not None:
        if type(independent_review_required) is not bool:
            raise ValueError('Independent review selection must be a boolean')
        if independent_review_required != saved.get('independent_review_required', False):
            changes['independent_review_required'] = independent_review_required
    if report_requested is False:
        if saved.get('report_requirement') is not None or app._report_projection()['kind'] == 'report':
            changes['report_requirement'] = None
    elif report_requested or report_settings is not None or report_method is not None:
        if (report_requested is not True and saved.get('report_requirement') is None
                and app._report_projection()['kind'] != 'report'):
            raise ValueError('Report settings require an existing or explicitly requested report')
        requirement = app.report_revision(dict(report_settings or {}), method=report_method)
        if requirement is not None:
            changes['report_requirement'] = requirement
    allowances = dict(resource_allowances or {})
    capacity = bool(allowances or additional_attempts or additional_no_progress)
    if capacity and (not authorization_id.strip() or not authorization_reason.strip()):
        raise ValueError('A resource revision requires an authorization id and reason')
    if not changes:
        if not capacity:
            return False
        # The ordinary continuation owns standalone grants too. Stop before
        # action selection so changing capacity cannot start a model or tool.
        app.run(resource_allowances=allowances, additional_attempts=additional_attempts,
            additional_no_progress=additional_no_progress, authorization_id=authorization_id,
            authorization_reason=authorization_reason, should_stop=lambda: True, progress=lambda _: None)
        return True
    app.supply(supply_id=authorization_id or f"research-revision-{len(app.state.get('supplies', [])) + 1}",
        reason=authorization_reason or 'Revise the supplied research materials and settings.',
        changes=changes, replace_existing=True, resource_allowances=allowances,
        additional_attempts=additional_attempts, additional_no_progress=additional_no_progress)
    if confirm_protocol:
        app.request_execution_confirmation()
    return True


def start_research_task(app: Any, *, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Start structured research through the saved common task and its budget.

    With no model, collect the explicitly supplied materials or run the declared
    table analysis; later authored or experimental work stays pending. The
    normal task action journal owns execution, confirmation and recovery.
    """
    progress = progress or getattr(app.services, 'message_callback', None) or print
    saved = app.state['config']
    if saved['model']:
        return app.run(progress=progress)
    if saved.get('execution_contract') is not None and saved.get('research_policy') is None:
        return start_declared_measurements(app, progress=progress)
    brief = ResearchBrief.from_dict(app.session.store.read_json(app.session.manifest.state_refs['brief']))
    policy = saved.get('research_policy') or {}
    action = ({'tool_name': 'analyze_table', 'arguments': saved['table_analysis']}
        if saved.get('table_analysis') is not None else
        {'tool_name': 'research_collect' if saved['allow_network'] else 'research', 'arguments': {
            'operation': 'summary', 'question': brief.objective or brief.request_text,
            'materials': policy.get('materials', []), 'use_llm': False}})
    status = app.run(actions=[action], progress=progress)
    turn = app.state['turns'][-1]
    result = app.session.store.read_json(turn['tools'][-1]['result']) if turn['tools'] else {}
    if result.get('status') != 'ok' or not result.get('artifacts'):
        return status
    if saved.get('execution_contract'):
        confirm = bool(policy.get('require_design'))
        if confirm:
            app.request_execution_confirmation()
        return start_declared_measurements(app, request=brief.request_text, confirm=confirm, progress=progress)
    produced = {'data_analysis'} if saved.get('table_analysis') is not None else {'summary', 'research_summary'}
    missing = [name for name in requested_research_outputs(brief) if name not in produced]
    deferred = bool(missing or saved.get('report_requirement') or policy.get('require_design'))
    completion = ({'tool_name': 'ask_user', 'arguments': {'question':
        'Completed materials and results are retained, but requested work remains unfinished: '
        + ', '.join(missing or ['research design']) + '. Continue the same task with the required '
        'inputs, authorized model or execution approval as appropriate.'}}
        if deferred else {'tool_name': 'deliver_result', 'arguments': {
        'summary': brief.request_text, 'artifacts': result['artifacts'],
        'unresolved': result.get('data', {}).get('unresolved', [])}})
    return app.run(actions=[completion], progress=progress)


def start_declared_measurements(app: Any, *, request: str | None = None, confirm: bool = False,
                                progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Run the caller's initial literal conditions with one-use authorization.

    A bound protocol confirmation remains separate from process approval. New
    proposals, retries and resumed commands use the ordinary approval boundary.
    """
    from simple_ar.tools.scientific import measurement_conditions

    progress = progress or getattr(app.services, 'message_callback', None) or print
    brief = ResearchBrief.from_dict(app.session.store.read_json(app.session.manifest.state_refs['brief']))
    if request is None:
        request = brief.request_text
    saved = app.state['config']
    contract = saved['execution_contract']
    conditions = [row for row in measurement_conditions(contract) if not row.get('reuse_ref')]
    grouped = 'conditions' in contract
    actions = [{'tool_name': 'measure_experiment', 'arguments':
        {**row['execution'], 'condition_id': row['id']} if grouped else
        row['execution'] if (saved['model'] or saved.get('report_requirement')
            or set(requested_research_outputs(brief)) != {'experiments'}) else
        {**row['execution'], 'delivery': {'summary': request, 'artifacts': []}}}
        for row in conditions]
    turn_index = len(app.state['turns'])
    request_count = len(app.state['requests'])
    remaining = {f'caller-{turn_index}-{index}': row['arguments'] for index, row in enumerate(actions)}

    def approve(call: dict, token: str) -> bool:
        progress(f"Execution proposal: {call['function']['arguments']}\nApproval token: {token}")
        arguments = json.loads(call['function']['arguments'])
        if (len(app.state['turns']) != turn_index + 1
                or app.state['turns'][-1].get('origin') != 'caller'
                or app.state['turns'][-1].get('request_count') != request_count
                or call['id'] not in remaining or call['function']['name'] != 'measure_experiment'
                or arguments != remaining[call['id']]):
            return False
        if arguments.get('environment') is not None:
            binding = app.scientific.approval_binding(arguments, identity=app.state['pending']['attempt'])
            if preparation := binding.get('environment_preparation'):
                steps = preparation['steps']
                wall = len(steps) * preparation['profile']['timeout_sec'] + sum(
                    row['execution']['timeout_sec'] for row in conditions)
                progress('Environment preparation steps (literal argv): ' + json.dumps(steps, ensure_ascii=False))
                progress(f"Preparation once + {len(conditions)} formal measurements: {len(steps) + len(conditions)} invocations; <= {wall}s declared wall reservation. "
                         f"Shared process limits: {json.dumps(binding['limits'], sort_keys=True)}")
            else:
                progress('Reuse verified shared environment: ' + json.dumps(binding['shared_environment'], sort_keys=True))
        del remaining[call['id']]
        return True

    status = app.run(actions=actions, approve=None if confirm else approve, progress=progress)
    if grouped and not saved['model']:
        completion = app.measurement_completion_actions()
        if completion:
            status = app.run(actions=completion, progress=progress)
    return status


def _create_research_task(brief: ResearchBrief, *, root: str | Path, services: Any | None,
                          model: str | None, allow_vision: bool, snapshot_sources: bool,
                          max_snapshot_bytes: int, config: Mapping | None = None,
                          budget_limits: Mapping | None = None, input_base_dir: str | Path | None = None,
                          max_results: int = 10, max_chunks: int | None = 300, idea_limit: int = 3,
                          cache_dir: str | Path | None = None, extraction_dir: str | Path | None = None,
                          research_work: bool | None = None,
                          max_attempts: int | None = None,
                          max_no_progress: int = 3):
    from simple_ar.app.tool_application import ToolApplication
    from simple_ar.tools.sources import SourcePolicy
    from simple_ar.report.requirements import freeze_report_requirement

    if type(allow_vision) is not bool:
        raise ValueError('allow_vision must be a bool')
    if max_attempts is not None and (type(max_attempts) is not int or max_attempts < 1):
        raise ValueError('max_attempts must be a positive integer')
    if max_results < 1 or idea_limit < 1:
        raise ValueError('Research application numeric settings must be positive.')
    if max_chunks is not None and max_chunks < 1:
        raise ValueError('max_chunks must be positive when provided.')
    if max_no_progress < 1:
        raise ValueError('Research application budgets must be positive.')
    config = dict(config or {})
    limits = dict(budget_limits or {})
    aliases = {'research_sources': 'providers', 'research_use_fulltext': 'require_fulltext',
        'research_selected_idea_id': 'selected_idea_id', 'feasibility_review_model': 'feasibility_connection'}
    policy_values, unhandled = {}, []
    auxiliary = {'research_task_kind', 'research_materials_only', 'research_local_documents', 'report', 'report_writing_method',
        'independent_review_required',
        'data_analysis', 'max_output_tokens', 'execution', 'execution_confirmation'}
    for name, value in config.items():
        target = aliases.get(name, name.removeprefix('research_'))
        if name not in auxiliary and target in SourcePolicy.model_fields:
            policy_values[target] = value
        elif name not in auxiliary:
            unhandled.append(name)
    if unhandled:
        raise ValueError('These legacy creation controls need their explicit execution/revision owner: '
            + ', '.join(sorted(unhandled)))
    if extraction_dir is not None:
        directory = str(Path(extraction_dir).expanduser().resolve())
        if policy_values.get('extraction_dir') not in (None, directory):
            raise ValueError('Conflicting explicit extraction directories')
        policy_values['extraction_dir'] = directory
    base = brief_input_base(brief, input_base_dir)
    assets = normalize_assets(brief, input_base_dir=base)
    local_documents = config.get('research_local_documents', ())
    if local_documents is None:
        local_documents = ()
    if not isinstance(local_documents, (list, tuple)) or any(
            not isinstance(path, (str, Path)) or not str(path).strip() or _is_remote_locator(str(path))
            for path in local_documents):
        raise ValueError('research_local_documents must contain explicit local paths')
    declared_paths = set()
    if local_documents:
        declared = ResearchBrief(request_text=brief.request_text, asset_requests=tuple(
            {'locator': str(path), 'role': 'paper'} for path in local_documents))
        declared_assets = normalize_assets(declared, input_base_dir=base)
        declared_paths = {asset.locator for asset in declared_assets}
        present = {asset.locator for asset in assets}
        for asset in declared_assets:
            if asset.locator not in present:
                assets = (*assets, asset)
                present.add(asset.locator)
    diagnostics = validate_brief(brief, assets)
    if any(item.severity == 'error' for item in diagnostics):
        raise ResearchInputError(item for item in diagnostics if item.severity == 'error')
    mounts, materials = {}, []
    for index, asset in enumerate(assets):
        if _is_remote_locator(asset.locator):
            continue  # Explicit URLs remain in the original request; acquisition needs network permission.
        alias = f'source{index}'
        mounts[alias] = Path(asset.locator)
        if (asset.role not in {'dataset', 'code'} or asset.locator in declared_paths) and mounts[alias].is_file():
            materials.append(alias)
    registry = getattr(services, 'search_registry', None)
    providers = (list(registry.names()) if registry is not None
        else list(_DEFAULT_RESEARCH_PROVIDERS))
    local_only = (config.get('research_materials_only') is True
        or policy_values.get('allow_network') is False
        or bool(materials) and config.get('research_materials_only') is not False
            and policy_values.get('allow_network') is not True)
    outputs = requested_research_outputs(brief)
    supported = {'summary', 'research_summary', 'report', 'paper', 'full_paper', 'assessment',
        'idea_assessment', 'idea_comparison', 'design', 'research_design', 'data_analysis', 'experiments',
        'bug_fix', 'code_task'}
    if set(outputs) - supported:
        raise ValueError('Use explicit native execution contracts for these outputs: '
            + ', '.join(sorted(set(outputs) - supported)))
    execution = config.get('execution')
    if execution is not None and not isinstance(execution, Mapping):
        raise ValueError('execution must be an object containing the declared domain contract')
    if {'bug_fix', 'code_task'} & set(outputs) and (execution is None or not execution.get('code_task')):
        raise ValueError('Code deliverables require an explicit execution.code_task contract')
    if research_work is None:
        research_work = (execution is None or bool({'summary', 'research_summary'} & set(outputs)
            or materials or any(policy_values.get(name) for name in (
                'require_summary', 'require_design', 'materials', 'source_workspace', 'queries', 'providers', 'allow_network'))))
    design_required = bool(policy_values.get('require_design', research_work and 'experiments' in outputs))
    confirm_execution = config.get('execution_confirmation', False)
    if type(confirm_execution) is not bool or confirm_execution and (execution is None or design_required):
        raise ValueError('execution_confirmation requires a caller-directed fixed execution')
    projection = (project_execution_creation(execution, request=brief.request_text, outputs=tuple(outputs),
        sources=tuple(asset.to_dict() for asset in assets), input_base_dir=base, require_design=design_required)
        if execution is not None else None)
    if projection is not None:
        policy_values = _remap_policy_mounts(policy_values, mounts, projection['sources'])
        mounts = projection['sources']
        materials = [name for name, path in mounts.items() if any(
            not _is_remote_locator(asset.locator) and Path(asset.locator) == path
            and (asset.role not in {'dataset', 'code'} or asset.locator in declared_paths)
            and path.is_file() for asset in assets)]
    if confirm_execution and (projection is None or projection['execution_contract'] is None):
        raise ValueError('execution_confirmation requires a fixed scientific measurement contract')
    selected_materials = policy_values.get('materials', materials)
    if local_documents and 'materials' in policy_values and isinstance(selected_materials, (list, tuple)):
        selected_materials = list(dict.fromkeys([*selected_materials, *(alias for alias, path in mounts.items()
            if str(path) in declared_paths)]))
    policy = SourcePolicy.model_validate({'max_results': max_results,
        'max_documents': max_results, 'max_chunks': max_chunks,
        'idea_limit': idea_limit,
        'cache_dir': str(Path(cache_dir).resolve()) if cache_dir else None,
        'providers': providers, **policy_values,
        'materials': selected_materials, 'allow_network': not local_only,
        'require_design': bool(confirm_execution or design_required),
        'require_summary': bool(policy_values.get('require_summary') or {'summary', 'research_summary'} & set(outputs))})
    report = (freeze_report_requirement(dict(config.get('report') or {}),
        method=config.get('report_writing_method')) if {'report', 'paper', 'full_paper'} & set(outputs) else None)
    table = config.get('data_analysis')
    if table is not None:
        path = (Path(base) / table['file']).resolve()
        alias = next((name for name, item in mounts.items() if item.resolve() == path), None)
        if alias is None:
            index = 0
            while f'source{index}' in mounts:
                index += 1
            alias = f'source{index}'
            mounts[alias] = path
        table = {'path': alias, 'spec': {name: value for name, value in table.items() if name != 'file'}}
    client = getattr(services, 'llm_client', None)
    if model is not None and client is not None:
        raise ValueError('Choose a named model or an injected client, not both')
    selected_model = model if model else 'runtime' if client is not None else None
    if projection is not None and projection['code_task'] is not None and selected_model is None:
        raise ValueError('Code preparation requires a model')
    if confirm_execution and not research_work:
        policy = policy.model_copy(update={'allow_network': False})
    if projection is not None and research_work:
        boundary = projection['execution_boundary']
        project_alias = (projection['code_task'] or {}).get('project_path')
        if project_alias and policy.source_workspace:
            from simple_ar.tools.workspace import resolve_mounted_path
            if resolve_mounted_path(mounts, policy.source_workspace).resolve() != mounts[project_alias].resolve():
                raise ValueError('Explicit research source_workspace conflicts with the declared code project')
        policy = policy.model_copy(update={'execution_boundary': boundary,
            'execution_schema': execution.get('result_schema', {}),
            'source_workspace': project_alias or policy.source_workspace})
    if selected_model is not None and any(type(limits[name]) is not int or limits[name] < 1
            for name in ('llm_requests', 'total_tokens') if limits.get(name) is not None):
        raise ValueError('Model-assisted creation requires finite positive llm_requests/total_tokens')
    unsupported_limits = {name for name, value in limits.items() if value is not None and name not in {
        'llm_requests', 'total_tokens', 'process_invocations', 'process_wall_seconds', 'source_operations'}}
    if unsupported_limits:
        raise ValueError('Unsupported creation resource controls: ' + ', '.join(sorted(unsupported_limits)))
    model_limits = _creation_model_limits(limits, model_enabled=selected_model is not None,
        preserve_unlimited=services is not None and hasattr(services, 'config'))
    source_count = max(1, len(policy.queries)) * len(policy.providers)
    # Preserve the old bounded process allowance independently of native attempts.
    execution_runs = (max_attempts if max_attempts is not None else 16) if 'experiments' in outputs else 0
    network_runs = source_count if policy.allow_network and (projection is None or research_work) else 0
    process_defaults = (projection['process_limits'] if projection is not None else {
        'process_invocations': execution_runs,
        'process_wall_seconds': execution_runs * policy.execution_boundary.get('timeout_sec', 300)})
    if projection is not None and projection['execution_contract'] is None and 'experiments' in outputs:
        process_defaults = {'process_invocations': process_defaults['process_invocations'] + execution_runs,
            'process_wall_seconds': process_defaults['process_wall_seconds']
                + execution_runs * policy.execution_boundary.get('timeout_sec', 300)}
    process_limits = {name: limits[name] if limits.get(name) is not None else default + extra
        for name, default, extra in (
            ('process_invocations', process_defaults['process_invocations'], network_runs * 4),
            ('process_wall_seconds', process_defaults['process_wall_seconds'], network_runs * 120))}
    root = Path(root).expanduser().resolve()
    # Caller-directed work has no model-turn budget from which to size attempts.
    attempt_limit = max_attempts
    if selected_model is None and attempt_limit is None:
        attempt_limit = 16
    request_context = brief.to_dict()
    task_kind = str(config.get('research_task_kind') or '').strip()
    if task_kind and task_kind.lower() != 'auto':
        request_context['research_task_kind'] = task_kind
    app = ToolApplication.create(root, (brief.objective or brief.request_text)
        + '\nOriginal user request and declared constraints:\n' + json.dumps(request_context, ensure_ascii=False),
        model=selected_model, sources=mounts, allow_vision=allow_vision,
        allow_execution='experiments' in outputs or projection is not None,
        allow_network=policy.allow_network if projection is None or research_work else False,
        max_turns=model_limits['llm_requests'], max_tokens=model_limits['total_tokens'],
        max_attempts=attempt_limit, report_requirement=report, control_protocol='operations.v1',
        independent_review_required=config.get('independent_review_required', False),
        max_output_tokens=config.get('max_output_tokens'), table_analysis=table,
        research_policy=policy.model_dump(mode='json') if projection is None or research_work or confirm_execution else None,
        code_task=projection['code_task'] if projection else None,
        execution_contract=projection['execution_contract'] if projection else None,
        process_limits=process_limits,
        source_operations=limits.get('source_operations') if limits.get('source_operations') is not None
            else source_count * (1 + policy.max_iterations) + policy.max_documents * 3,
        services=services, snapshot_sources=snapshot_sources, max_snapshot_bytes=max_snapshot_bytes)
    with app.session.mutation_scope():
        app.session.manifest.budget.max_no_progress = max_no_progress
        app.session.manifest.state_refs['brief'] = app.session.store.write_json('inputs/research_brief.json',
            brief.to_dict(), kind='research_input', schema=brief.schema_version, producer='research_intake')
        app.session.manifest.state_refs['assets'] = app.session.store.write_json('inputs/research_assets.json',
            {'assets': [asset.to_dict() for asset in assets]}, kind='research_assets', producer='research_intake')
        app.session.save()
    if confirm_execution:
        app.request_execution_confirmation()
    return app


def _remap_policy_mounts(values: dict, original: dict[str, Path], projected: dict[str, Path]) -> dict:
    """Preserve the original asset alias identity when execution adds mounts."""
    def remap(locator: str) -> str:
        if not locator or Path(locator).is_absolute():
            return locator
        alias, separator, suffix = locator.partition('/')
        if alias not in original:
            raise ValueError(f'Explicit source policy locator has no original asset mount: {locator}')
        path = original[alias].resolve()
        target = next((name for name, mounted in projected.items() if mounted.resolve() == path), None)
        if target is None:
            raise ValueError(f'Execution projection cannot preserve source policy locator: {locator}')
        return target + (separator + suffix if separator else '')
    updated = dict(values)
    if 'materials' in values:
        updated['materials'] = [remap(value) for value in values['materials']]
    if 'source_workspace' in values:
        updated['source_workspace'] = remap(values['source_workspace'])
    return updated


def _execution_path(value: str | Path, base: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def _bind_baseline_references(execution: dict, mounts: list[Path], base: Path) -> dict:
    values = execution.get('baseline_ref')
    if execution.get('baseline_policy') != 'reuse':
        return execution
    if values is None:
        raise UnsupportedExecutionCreation('Historical baseline reuse requires its saved owner')
    from simple_ar.tools.scientific import _reject_links
    refs = []
    for value in values if isinstance(values, (list, tuple)) else [values]:
        named = Path(value).expanduser()
        named = named if named.is_absolute() else base / named
        _reject_links(named)
        path = named.resolve()
        if path not in mounts:
            mounts.append(path)
        refs.append(f'source{mounts.index(path)}')
    return {**execution, 'baseline_ref': refs if isinstance(values, (list, tuple)) else refs[0]}


def _exact_code_contract(execution: dict, task: dict, project: Path | None,
                         mounts: list[Path], sources: tuple, request: str,
                         formal: dict | None) -> dict:
    from simple_ar.tools.implementation import ExactCodeTask
    from simple_ar.tools.scientific import measurement_conditions
    preparation_names = ('kind', 'workspace_mode', 'max_file_bytes', 'workspace_include', 'workspace_exclude',
        'workspace_reuse_source_venv', 'env_mode', 'python_executable', 'max_repairs')
    scientific = formal is not None
    conditions = [row for row in measurement_conditions(formal) if not row.get('reuse_ref')] if scientific else []
    if scientific:
        if project is not None and (not project.is_dir() or any(
                Path(row['execution']['cwd']).resolve() != project for row in conditions)):
            raise ValueError('Reproduction measurement cwd must match the original code_root')
        if project is None and (task['kind'] != 'greenfield' or task['workspace_mode'] != 'empty'):
            raise ValueError('Source-less reproduction requires empty greenfield preparation')
    # Exact argv explicitly names these file inputs. Mount only those files,
    # never their parent directories or arbitrary directory-valued arguments.
    # The implementation owner binds their bytes as protected external inputs.
    commands = [task.get('validation_command') or execution['command'], execution['command']]
    if (execution.get('environment') or {}).get('check_command'):
        commands.append(execution['environment']['check_command'])
    if formal is not None:
        commands.extend(row['execution']['command'] for row in conditions)
    for command in commands:
        for argument in command[1:]:
            value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
            try:
                named = (Path(value) if Path(value).is_absolute() else Path(execution['cwd']) / value).absolute()
                path = named.resolve()
                if path.is_file() and (project is None or not path.is_relative_to(project)) and path not in mounts:
                    from simple_ar.tools.scientific import _reject_links
                    _reject_links(named)
                    mounts.append(path)
            except (OSError, ValueError):
                pass  # Literal argv need not represent filesystem paths.
    data = [Path(row['locator']).resolve().relative_to(project).as_posix() for row in sources
            if project is not None and row['role'] == 'dataset' and Path(row['locator']).resolve().is_relative_to(project)]
    if sources:
        request += '\n\nSupplied material roles (read-only user declarations, not verified evidence):\n' + json.dumps(
            [{'path': row['locator'], 'role': row['role']} for row in sources], ensure_ascii=False)
    if scientific:
        request += ('\n\nRun validation and formal measurements as separate physical calls, even when '
            'their argv match; validator results are not measurements.')
        if any(row.get('before_implementation') for row in conditions):
            request += (' First prepare the unchanged source, then complete all declared baseline measurements '
                'before implement(prepared_result). Pass the actual successful baseline facts to implementation; '
                'failed or missing baselines do not authorize generation.')
    preparation = {name: task[name] for name in preparation_names if name in task}
    new_files = task.get('initial_files', [])
    protected = task.get('protected_patterns', [])
    if 'initial_files' in task:
        if (task['kind'] != 'existing_project' or project is None
                or task['workspace_mode'] not in {'auto', 'copy', 'git_worktree'}):
            raise ValueError('initial_files requires an isolated existing project with auto/copy/git_worktree preparation')
        if (not isinstance(new_files, (list, tuple)) or not new_files
                or any(not isinstance(name, str) or Path(name).suffix != '.py'
                       or name not in task.get('allowed_patterns', []) for name in new_files)):
            raise ValueError('initial_files requires explicit exact allowed new Python paths')
        from simple_ar.code_task.editing.scope import default_edit_scope
        protected = default_edit_scope(protected_patterns=protected)['protected_patterns']
    exact = ExactCodeTask(project_path=f'source{mounts.index(project)}' if project is not None else '', goal=request,
        allowed_patterns=task['allowed_patterns'] or ['**'], protected_patterns=protected, new_files=list(new_files),
        validation_command=list(task.get('validation_command') or execution['command']),
        timeout_sec=task.get('validation_timeout_sec') or execution['timeout_sec'],
        preparation={**preparation, 'data_inputs': list(dict.fromkeys(data))},
        environment=execution.get('environment'),
        edit_policy={name: task[name] for name in ('budget_profile', 'edit_budget_overrides', 'allow_large_edits') if name in task})
    for name in exact.new_files:
        target = project / name
        if target.exists():
            raise ValueError('initial_files must not replace an original project path')
        for part in (target, *target.parents):
            if part == project:
                break
            reparse = part.exists() and (getattr(part.lstat(), 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
            if part.is_symlink() or reparse:
                raise ValueError('initial_files cannot traverse links')

    return exact.model_dump()


def _normalize_creation_code_task(task: dict, base: Path) -> dict:
    """Use the standalone CodeTask configuration owners for public partial inputs."""
    from simple_ar.app.research_execution import code_task_execution_options
    from simple_ar.code_task.runtime.config import load_code_task_init_options, load_code_task_execute_options
    values = dict(task)
    if values.get('code_root'):
        values['code_root'] = str(_execution_path(values['code_root'], base))
    if values.get('python_executable'):
        python = Path(values['python_executable']).expanduser()
        values['python_executable'] = str((python if python.is_absolute() else base / python).absolute())
    names = ('kind', 'code_root', 'workspace_mode', 'max_file_bytes', 'workspace_include',
        'workspace_exclude', 'workspace_reuse_source_venv', 'env_mode', 'python_executable')
    init = load_code_task_init_options(require_task_file=False,
        **{name: values[name] for name in names if name in values})
    return {**code_task_execution_options(init, load_code_task_execute_options()), **values}


def project_execution_creation(execution: Mapping[str, Any], *, request: str, outputs: tuple,
                               sources: tuple, input_base_dir: str | Path,
                               require_design: bool = False) -> dict[str, Any]:
    """Compile declared domain data for both Python and CLI creation; never run or approve."""
    from copy import deepcopy
    from simple_ar.tools.scientific import freeze_measurement_set, measurement_contract, measurement_conditions
    base = Path(input_base_dir).resolve()
    execution = deepcopy(dict(execution))
    flat = {'command', 'cwd', 'timeout_sec', 'label', 'result_schema', 'protocol', 'environment'}
    controls = {'baseline', 'baseline_policy', 'baseline_ref', 'pairs', 'seeds', 'seed_count', 'seed_flag'}
    if set(execution) - (flat | controls | {'code_task'}):
        raise UnsupportedExecutionCreation('Unsupported explicit execution controls: '
            + ', '.join(sorted(set(execution) - (flat | controls | {'code_task'}))))
    task = execution.get('code_task')
    if task is not None and (not isinstance(task, dict) or 'run_dir' in task):
        raise UnsupportedExecutionCreation('Initialized CodeTask adoption requires its saved owner')
    scientific = 'experiments' in outputs
    if set(outputs) - {'experiments', 'report', 'summary', 'research_summary', 'bug_fix', 'code_task'}:
        raise UnsupportedExecutionCreation('Unsupported execution outputs')
    if not scientific and task is None:
        raise UnsupportedExecutionCreation('Execution needs a measurement or explicit code deliverable')
    open_design = scientific and require_design and not {'baseline_policy', 'baseline', 'pairs'}.intersection(execution)
    execution['cwd'] = str(_execution_path(execution.get('cwd', '.'), base))
    if 'baseline' in execution and isinstance(execution['baseline'], dict) and 'cwd' in execution['baseline']:
        execution['baseline']['cwd'] = str(_execution_path(execution['baseline']['cwd'], base))
    project = None
    if task is not None:
        from simple_ar.tools.implementation import ExactCodePreparation
        preparation_names = {'kind', 'workspace_mode', 'max_file_bytes', 'workspace_include', 'workspace_exclude',
            'workspace_reuse_source_venv', 'env_mode', 'python_executable', 'max_repairs'}
        if set(task) - (preparation_names | {'code_root', 'allowed_patterns', 'protected_patterns', 'approval_note',
                'budget_profile', 'edit_budget_overrides', 'allow_large_edits', 'validation_command',
                'validation_timeout_sec', 'initial_files'}):
            raise UnsupportedExecutionCreation('Unsupported explicit CodeTask controls')
        task = _normalize_creation_code_task(task, base)
        execution['code_task'] = task
        project = Path(task['code_root']) if task.get('code_root') else None
        task.update(ExactCodePreparation.model_validate({name: task[name]
            for name in preparation_names if name in task}).model_dump(exclude={'data_inputs'}))
        if not scientific and (set(execution) - (flat | {'code_task', 'baseline_policy'})
                or execution.get('baseline_policy', 'skip') != 'skip' or 'protocol' in execution):
            raise UnsupportedExecutionCreation('Implementation-only creation cannot discard scientific controls')
    sources = tuple({**row, 'locator': str(_execution_path(row['locator'], base))}
        for row in sources if not _is_remote_locator(str(row['locator'])))
    mounts = list(dict.fromkeys([*([project] if project else []), *(Path(row['locator']) for row in sources)]))
    declaration = {key: value for key, value in execution.items() if key != 'code_task'}
    declaration = _bind_baseline_references(declaration, mounts, base)
    contract = None
    if scientific and not open_design:
        extended = set(declaration) - flat
        if not require_design and extended == {'baseline_policy'} and declaration['baseline_policy'] == 'skip':
            declaration = {key: value for key, value in declaration.items() if key in flat}
            extended = set()
        if task is not None:
            contract, declaration = compile_scoped_measurement(execution, task, declaration)
        else:
            contract = freeze_measurement_set(declaration) if extended else measurement_contract(declaration)
    if open_design and task is not None:
        from simple_ar.code_task.execution.environment import resolve_code_task_command
        from simple_ar.code_task.workspace.modes import _source_venv_python
        reused = (_source_venv_python(project) if project and task.get('workspace_reuse_source_venv')
            and not task.get('python_executable') else None)
        declaration['command'] = (list(declaration['command']) if (execution.get('environment') or {}).get('mode') == 'venv'
            else resolve_code_task_command(declaration['command'], env_mode='external' if reused else task.get('env_mode', 'current'),
                python_executable=str(reused) if reused else task.get('python_executable')))
        if project is not None and Path(declaration['cwd']) != project:
            raise ValueError('Reproduction measurement cwd must match the original code_root')
    if open_design:
        measurement_contract({key: value for key, value in declaration.items() if key in flat})
    exact = _exact_code_contract(execution, task, project, mounts, sources, request, contract) if task is not None else None
    conditions = [row for row in measurement_conditions(contract) if not row.get('reuse_ref')] if contract else []
    validators = 1 + (exact.get('preparation') or {}).get('max_repairs', 0) if exact else 0
    required = {'process_invocations': len(conditions) + validators,
        'process_wall_seconds': sum(row['execution']['timeout_sec'] for row in conditions)
            + validators * (exact['timeout_sec'] if exact else 0)}
    environment = execution.get('environment')
    if environment is not None:
        from simple_ar.research.project_environment import project_environment_plan
        command = conditions[0]['execution']['command'] if conditions else exact['validation_command'] if exact else declaration['command']
        plan = project_environment_plan(environment, project or Path(execution['cwd']),
            Path('__attempt__') / 'environment', command)
        required['process_invocations'] += len(plan['steps'])
        required['process_wall_seconds'] += len(plan['steps']) * plan['profile']['timeout_sec']
    return {'sources': {f'source{i}': path for i, path in enumerate(mounts)},
        'code_task': exact, 'execution_contract': contract, 'execution_boundary': {**declaration,
            **({'code_task': task} if task is not None else {})}, 'process_limits': required}


def parse_research_input(
    request_text: str | Path,
    *,
    config_path: str | Path | None = None,
    asset_paths: Iterable[str | Path] = (),
) -> ResearchBrief:
    """Parse plain text, one Markdown/text file, and optional TOML metadata.

    Parsing is intentionally side-effect-light: it reads only the named input
    and TOML file. It does not call a model, download documents, scan a
    repository, install dependencies, or execute an asset.
    """

    text, source_path, inferred_config = _read_request(request_text)
    selected_config = Path(config_path) if config_path is not None else inferred_config
    config: dict[str, Any] = {}
    if selected_config is not None:
        config = _load_toml(selected_config)
        source_path = source_path or selected_config

    values = _input_values(config)
    if not text.strip():
        text = _text_value(values, "request_text", "topic")
    if not text.strip():
        raise ResearchInputError(
            (Diagnostic("request_text_required", "A research question or request is required."),)
        )

    requests = _collect_asset_requests(values, asset_paths)
    try:
        return ResearchBrief(
            request_text=text,
            brief_id=_text_value(values, "brief_id") or _stable_brief_id(text, requests),
            revision=_positive_int(values.get("revision", values.get("brief_revision")), 1),
            parent_revision=_optional_positive_int(values.get("parent_revision")),
            objective=_text_value(values, "objective"),
            intents=_strings(values.get("intents", values.get("intent"))),
            requested_outputs=_strings(
                values.get("requested_outputs", values.get("outputs"))
            ),
            asset_ids=_strings(values.get("asset_ids")),
            user_hypotheses=_strings(values.get("user_hypotheses", values.get("hypotheses"))),
            hard_constraints=_strings(values.get("hard_constraints", values.get("constraints"))),
            preferences=_strings(values.get("preferences")),
            open_questions=_strings(values.get("open_questions")),
            accepted_assumptions=_strings(values.get("accepted_assumptions")),
            asset_requests=tuple(requests),
            source_path=str(source_path.resolve()) if source_path is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise ResearchInputError(
            (Diagnostic("brief_invalid", str(exc), field="brief"),)
        ) from exc


def brief_input_base(brief: ResearchBrief, input_base_dir: str | Path | None = None) -> Path:
    """Resolve relative material and execution inputs for both research entries."""
    if input_base_dir is not None:
        return Path(input_base_dir)
    if brief.source_path:
        source = Path(brief.source_path)
        return source.parent if source.suffix else source
    return Path.cwd()


def normalize_assets(
    brief: ResearchBrief,
    *,
    input_base_dir: str | Path,
) -> tuple[ResearchAsset, ...]:
    """Resolve named assets and collect only lightweight identity metadata."""

    base_dir = Path(input_base_dir).resolve()
    assets: list[ResearchAsset] = []
    for request in brief.asset_requests:
        locator = str(request["locator"])
        kind = str(request.get("kind") or "file")
        role = str(request.get("role") or "input")
        mutability = str(request.get("mutability") or "read_only")
        requested_id = str(request.get("asset_id") or "")
        allowed_uses = tuple(str(item) for item in request.get("allowed_uses", ()))
        diagnostics: list[Diagnostic] = []

        if _is_remote_locator(locator):
            canonical_locator = locator
            identity = {"locator": locator, "fingerprint_strength": "none"}
            availability = "unknown"
            understanding = "unknown"
            resolved_path: Path | None = None
        else:
            candidate = Path(locator)
            if not candidate.is_absolute():
                candidate = base_dir / candidate
            try:
                resolved_path = candidate.resolve(strict=False)
            except OSError as exc:
                resolved_path = candidate.absolute()
                diagnostics.append(
                    Diagnostic(
                        "asset_path_unresolved",
                        f"Could not fully resolve asset path {locator!r}: {exc}",
                        "warning",
                        "locator",
                    )
                )
            canonical_locator = str(resolved_path)
            if not resolved_path.exists():
                availability = "missing"
                understanding = "unavailable"
                identity = {
                    "locator": canonical_locator,
                    "fingerprint_strength": "none",
                }
                diagnostics.append(
                    Diagnostic(
                        "asset_missing",
                        f"Asset does not exist: {canonical_locator}",
                        "warning",
                        "locator",
                    )
                )
            else:
                availability = "available"
                understanding = "not_inspected"
                identity, stat_diagnostic = _light_identity(resolved_path)
                if stat_diagnostic is not None:
                    diagnostics.append(stat_diagnostic)
                if resolved_path.is_file() and resolved_path.suffix.lower() not in _KNOWN_SUFFIXES:
                    diagnostics.append(
                        Diagnostic(
                            "asset_format_unrecognized",
                            f"No built-in reader is selected for {resolved_path.suffix or 'this'} asset.",
                            "warning",
                            "locator",
                        )
                    )

        if kind == "file":
            kind = _infer_kind(resolved_path, locator)
        asset_id = requested_id or _stable_asset_id(canonical_locator)
        if not allowed_uses:
            allowed_uses = ("read", "reference") if role in {"paper", "dataset", "baseline"} else ("read",)
        assets.append(
            ResearchAsset(
                asset_id=asset_id,
                kind=kind,
                role=role,
                locator=canonical_locator,
                identity=identity,
                availability=availability,
                understanding=understanding,
                mutability=mutability,
                allowed_uses=allowed_uses,
                diagnostics=tuple(diagnostics),
            )
        )
    return tuple(assets)


def validate_brief(
    brief: ResearchBrief,
    assets: Iterable[ResearchAsset],
) -> tuple[Diagnostic, ...]:
    """Validate explicit input facts without judging scientific quality."""

    diagnostics: list[Diagnostic] = []
    if not brief.request_text.strip():
        diagnostics.append(Diagnostic("request_text_required", "A research request is required.", field="request_text"))

    seen: dict[str, str] = {}
    for asset in assets:
        previous = seen.get(asset.asset_id)
        if previous is not None and previous != asset.locator:
            diagnostics.append(
                Diagnostic(
                    "asset_identity_conflict",
                    f"Asset id {asset.asset_id!r} refers to more than one locator.",
                    "error",
                    "asset_ids",
                )
            )
        seen[asset.asset_id] = asset.locator
        diagnostics.extend(asset.diagnostics)

    if brief.asset_ids:
        missing_ids = sorted(set(brief.asset_ids) - set(seen))
        if missing_ids:
            diagnostics.append(
                Diagnostic(
                    "asset_not_normalized",
                    f"Brief references assets that were not normalized: {', '.join(missing_ids)}",
                    "warning",
                    "asset_ids",
                )
            )
    if not brief.requested_outputs:
        diagnostics.append(
            Diagnostic(
                "outputs_unspecified",
                "No requested output was declared; the application must confirm the deliverable.",
                "info",
                "requested_outputs",
            )
        )
    return tuple(diagnostics)


def write_intake_artifacts(
    brief: ResearchBrief,
    assets: Iterable[ResearchAsset],
    output_dir: str | Path,
    *, revision: int | None = None,
) -> dict[str, Path]:
    """Write inspectable brief, asset, and deterministic Markdown summaries."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    asset_rows = [asset.to_dict() for asset in assets]
    suffix = f"-r{revision:04d}" if revision is not None else ""
    brief_path = root / f"brief{suffix}.json"
    assets_path = root / f"assets{suffix}.json"
    markdown_path = root / f"brief{suffix}.md"
    write_json(brief_path, brief.to_dict())
    write_json(assets_path, {"schema_version": "research_assets.v1", "assets": asset_rows})
    write_text(markdown_path, _brief_markdown(brief, asset_rows))
    return {"brief": brief_path, "assets": assets_path, "markdown": markdown_path}




def _read_request(value: str | Path) -> tuple[str, Path | None, Path | None]:
    if isinstance(value, Path):
        candidate = value
    else:
        candidate = _existing_file(value)
        if candidate is None:
            return value, None, None
    if candidate.suffix.lower() == ".toml":
        return "", None, candidate
    if candidate.suffix.lower() not in {".md", ".markdown", ".txt"}:
        raise ResearchInputError(
            (
                Diagnostic(
                    "request_file_format",
                    "Request file must be Markdown, TXT, or TOML.",
                    "error",
                    "request_text",
                ),
            )
        )
    try:
        return candidate.read_text(encoding="utf-8"), candidate, None
    except OSError as exc:
        raise ResearchInputError(
            (Diagnostic("request_file_unreadable", str(exc), "error", "request_text"),)
        ) from exc


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ResearchInputError(
            (Diagnostic("config_missing", f"TOML config does not exist: {path}", field="config_path"),)
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ResearchInputError(
            (Diagnostic("config_invalid", f"Could not parse TOML config: {exc}", field="config_path"),)
        ) from exc
    if not isinstance(data, dict):
        raise ResearchInputError(
            (Diagnostic("config_shape", "TOML root must be a table.", field="config_path"),)
        )
    return data


def _input_values(config: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("brief", "research"):
        section = config.get(name)
        if isinstance(section, Mapping):
            values.update(section)
    values.update({key: value for key, value in config.items() if key not in {"brief", "research"}})
    return values


def _collect_asset_requests(
    values: Mapping[str, Any],
    asset_paths: Iterable[str | Path],
) -> list[dict[str, Any]]:
    raw_assets = values.get("assets", values.get("asset_requests", []))
    if raw_assets is None:
        raw_assets = []
    if not isinstance(raw_assets, list):
        raise ResearchInputError(
            (Diagnostic("assets_shape", "assets must be a TOML array of tables.", field="assets"),)
        )
    requests: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_assets):
        if isinstance(raw, str):
            raw = {"locator": raw}
        if not isinstance(raw, Mapping):
            raise ResearchInputError(
                (Diagnostic("asset_invalid", f"Asset {index} must be a table or path.", field="assets"),)
            )
        locator = str(raw.get("locator") or raw.get("path") or "").strip()
        if not locator:
            raise ResearchInputError(
                (Diagnostic("asset_locator_required", f"Asset {index} has no locator or path.", field="assets"),)
            )
        requests.append(
            {
                "asset_id": str(raw.get("asset_id") or _stable_asset_id(locator)),
                "locator": locator,
                "kind": str(raw.get("kind") or _infer_kind(None, locator)),
                "role": str(raw.get("role") or "input"),
                "mutability": str(raw.get("mutability") or "read_only"),
                "allowed_uses": list(_strings(raw.get("allowed_uses"))),
            }
        )

    local_documents = values.get("local_documents", values.get("documents", [])) or []
    if isinstance(local_documents, str):
        local_documents = [local_documents]
    if not isinstance(local_documents, (list, tuple)):
        raise ResearchInputError(
            (Diagnostic("local_documents_shape", "local_documents must be a list of paths.", field="local_documents"),)
        )
    requests.extend(
        {
            "asset_id": _stable_asset_id(str(path)),
            "locator": str(path),
            "kind": _infer_kind(None, str(path)),
            "role": "paper",
            "mutability": "read_only",
            "allowed_uses": ["read", "reference"],
        }
        for path in local_documents
        if str(path).strip()
    )
    requests.extend(
        {
            "asset_id": _stable_asset_id(str(path)),
            "locator": str(path),
            "kind": _infer_kind(None, str(path)),
            "role": "input",
            "mutability": "read_only",
            "allowed_uses": ["read"],
        }
        for path in asset_paths
        if str(path).strip()
    )
    return requests


def _light_identity(path: Path) -> tuple[dict[str, Any], Diagnostic | None]:
    try:
        stat = path.stat()
        identity: dict[str, Any] = {
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "file_type": "directory" if path.is_dir() else "file",
            "fingerprint_strength": "metadata",
        }
        if path.is_file() and stat.st_size <= 1_048_576:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            identity["sha256"] = digest
            identity["fingerprint_strength"] = "content"
        return identity, None
    except OSError as exc:
        return (
            {"fingerprint_strength": "none"},
            Diagnostic("asset_identity_failed", f"Could not inspect asset identity: {exc}", "warning", "locator"),
        )


def _brief_markdown(brief: ResearchBrief, assets: list[dict[str, Any]]) -> str:
    lines = [
        "# Research input",
        "",
        f"- Brief: `{brief.brief_id}` revision {brief.revision}",
        f"- Objective: {brief.objective or '(not explicitly declared)'}",
        f"- Intents: {', '.join(brief.intents) or '(not explicitly declared)'}",
        f"- Requested outputs: {', '.join(brief.requested_outputs) or '(not explicitly declared)'}",
        "",
        "## Request",
        "",
        brief.request_text.rstrip(),
        "",
        "## Assets",
        "",
    ]
    if assets:
        lines.extend(
            f"- `{asset['asset_id']}` ({asset['role']}, {asset['kind']}): {asset['locator']}"
            for asset in assets
        )
    else:
        lines.append("- None declared.")
    if brief.open_questions:
        lines.extend(["", "## Open questions", "", *[f"- {item}" for item in brief.open_questions]])
    return "\n".join(lines) + "\n"


def _existing_file(value: str) -> Path | None:
    try:
        candidate = Path(value)
        return candidate if candidate.is_file() else None
    except (OSError, ValueError):
        return None


def _text_value(values: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = values.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("revision must be a positive integer") from exc
    if parsed < 1:
        raise ValueError("revision must be a positive integer")
    return parsed


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    return _positive_int(value, 1)


def _stable_brief_id(text: str, requests: list[dict[str, Any]]) -> str:
    payload = json.dumps({"text": text, "assets": requests}, sort_keys=True, default=str)
    return f"brief-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _stable_asset_id(locator: str) -> str:
    return f"asset-{hashlib.sha256(locator.encode('utf-8')).hexdigest()[:12]}"


def _is_remote_locator(locator: str) -> bool:
    return "://" in locator


def _infer_kind(path: Path | None, locator: str) -> str:
    if path is not None and path.is_dir():
        return "repository" if (path / ".git").exists() else "directory"
    suffix = Path(locator).suffix.lower()
    if suffix in {".md", ".markdown", ".txt"}:
        return "document"
    if suffix == ".pdf":
        return "paper"
    if suffix in {".csv", ".json", ".jsonl", ".parquet", ".npz", ".npy"}:
        return "dataset"
    return "file"


_KNOWN_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".pdf",
    ".csv",
    ".json",
    ".jsonl",
    ".parquet",
    ".npz",
    ".npy",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}


__all__ = [
    "ResearchInputError",
    "create_research_task",
    "create_structured_research_task",
    "start_research_task",
    "start_declared_measurements",
    "project_execution_creation",
    "UnsupportedExecutionCreation",
    "normalize_assets",
    "parse_research_input",
    "validate_brief",
    "write_intake_artifacts",
]
