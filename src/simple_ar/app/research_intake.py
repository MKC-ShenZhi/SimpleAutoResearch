from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def create_research_task(brief: ResearchBrief, *, root: str | Path, services: Any | None = None,
                         model: str | None = None,
                         allow_vision: bool = False,
                         snapshot_sources: bool = False,
                         max_snapshot_bytes: int = 16 * 1024 * 1024):
    """Create a common task from public research inputs without running it.

    This is an input projection, not another planner or lifecycle. Runtime
    clients/providers stay in memory; the native owner retains their identities,
    user obligations, selected evidence and the one resource ledger.
    Image inspection requires explicit allow_vision=True; the native owner
    validates its named vision route and charges the same model budget.
    """
    if type(allow_vision) is not bool:
        raise ValueError('allow_vision must be a bool')

    from simple_ar.app.research_application import ResearchApplicationServices
    from simple_ar.app.tool_application import ToolApplication
    from simple_ar.tools.sources import SourcePolicy
    from simple_ar.tools.writing import freeze_report_requirement

    settings = services or ResearchApplicationServices()
    config = dict(settings.config)
    aliases = {'research_sources': 'providers', 'research_use_fulltext': 'require_fulltext',
        'research_selected_idea_id': 'selected_idea_id', 'feasibility_review_model': 'feasibility_connection'}
    policy_values, unhandled = {}, []
    auxiliary = {'research_task_kind', 'research_materials_only', 'report', 'report_writing_method',
        'data_analysis', 'max_output_tokens', 'execution', 'execution_confirmation'}
    for name, value in config.items():
        target = aliases.get(name, name.removeprefix('research_'))
        if name not in auxiliary and target in SourcePolicy.model_fields:
            policy_values[target] = value
        elif name not in auxiliary:
            unhandled.append(name)
    if unhandled or settings.extraction_dir is not None:
        raise ValueError('These legacy creation controls need their explicit execution/revision owner: '
            + ', '.join(sorted([*unhandled, *(['extraction_dir'] if settings.extraction_dir is not None else [])])))
    source = Path(brief.source_path) if brief.source_path else None
    base = settings.input_base_dir or (source.parent if source and source.suffix else source) or Path.cwd()
    assets = normalize_assets(brief, input_base_dir=base)
    diagnostics = validate_brief(brief, assets)
    if any(item.severity == 'error' for item in diagnostics):
        raise ResearchInputError(item for item in diagnostics if item.severity == 'error')
    mounts, materials = {}, []
    for index, asset in enumerate(assets):
        if _is_remote_locator(asset.locator):
            continue  # Explicit URLs remain in the original request; acquisition needs network permission.
        alias = f'source{index}'
        mounts[alias] = Path(asset.locator)
        if asset.role not in {'dataset', 'code'} and mounts[alias].is_file():
            materials.append(alias)
    providers = (list(settings.search_registry.names()) if settings.search_registry is not None
        else ['openalex', 'semantic_scholar', 'arxiv'])
    local_only = (config.get('research_materials_only') is True
        or policy_values.get('allow_network') is False
        or bool(materials) and config.get('research_materials_only') is not False
            and policy_values.get('allow_network') is not True)
    outputs = brief.requested_outputs or ('research_summary',)
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
            and asset.role not in {'dataset', 'code'} and path.is_file() for asset in assets)]
    if confirm_execution and (projection is None or projection['execution_contract'] is None):
        raise ValueError('execution_confirmation requires a fixed scientific measurement contract')
    policy = SourcePolicy.model_validate({'max_results': settings.max_results,
        'max_documents': settings.max_results, 'max_chunks': settings.max_chunks,
        'idea_limit': settings.idea_limit, 'max_fulltext_documents': min(settings.max_results, 1),
        'cache_dir': str(settings.cache_dir.resolve()) if settings.cache_dir else None,
        'providers': providers, **policy_values,
        'materials': policy_values.get('materials', materials), 'allow_network': not local_only,
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
    client = settings.llm_client
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
    limits = settings.budget_limits
    if selected_model is not None and any(type(limits[name]) is not int or limits[name] < 1
            for name in ('llm_requests', 'total_tokens') if limits.get(name) is not None):
        raise ValueError('Model-assisted creation requires finite positive llm_requests/total_tokens')
    unsupported_limits = {name for name, value in limits.items() if value is not None and name not in {
        'llm_requests', 'total_tokens', 'process_invocations', 'process_wall_seconds', 'source_operations'}}
    if unsupported_limits:
        raise ValueError('Unsupported creation resource controls: ' + ', '.join(sorted(unsupported_limits)))
    source_count = max(1, len(policy.queries)) * len(policy.providers)
    # Preserve the old bounded process allowance independently of native attempts.
    execution_runs = (settings.max_attempts if settings.max_attempts is not None else 16) if 'experiments' in outputs else 0
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
    attempt_limit = settings.max_attempts
    if selected_model is None and attempt_limit is None:
        attempt_limit = 16
    app = ToolApplication.create(root, (brief.objective or brief.request_text)
        + '\nOriginal user request and declared constraints:\n' + json.dumps(brief.to_dict(), ensure_ascii=False),
        model=selected_model, sources=mounts, allow_vision=allow_vision,
        allow_execution='experiments' in outputs or projection is not None,
        allow_network=policy.allow_network if projection is None or research_work else False, max_turns=(limits.get('llm_requests')
            if limits.get('llm_requests') is not None else 30 if selected_model is not None else 0),
        max_tokens=(limits.get('total_tokens') if limits.get('total_tokens') is not None
            else 600000 if selected_model is not None else 0),
        max_attempts=attempt_limit, report_requirement=report, control_protocol='operations.v1',
        context_protocol='conversation.v1',
        max_output_tokens=config.get('max_output_tokens'), table_analysis=table,
        research_policy=policy.model_dump(mode='json') if projection is None or research_work or confirm_execution else None,
        code_task=projection['code_task'] if projection else None,
        execution_contract=projection['execution_contract'] if projection else None,
        process_limits=process_limits,
        source_operations=limits.get('source_operations') if limits.get('source_operations') is not None
            else source_count * (1 + policy.max_iterations) + policy.max_documents * 3,
        services=settings, snapshot_sources=snapshot_sources, max_snapshot_bytes=max_snapshot_bytes)
    with app.session.mutation_scope():
        app.session.manifest.budget.max_no_progress = settings.max_no_progress
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
    "project_execution_creation",
    "UnsupportedExecutionCreation",
    "normalize_assets",
    "parse_research_input",
    "validate_brief",
    "write_intake_artifacts",
]
