"""Ordinary task creation inputs shared by terminal and graphical clients."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import tomllib

from simple_ar.app.reuse import ReuseAccess, select_reuse


FUNCTION_LABELS = {
    "survey": "Direction survey / 方向调研 — compare sources and deliver a report",
    "bug_fix": "Code repair / 修改代码 — describe the fix and supply a read-only project",
    "reproduction": "Finite reproduction / 有限复现 — describe the conclusion and supply paper/project/data",
    "writing": "Material writing / 材料写作 — use notes, drafts or an analysis package",
    "data_analysis": "Data and plots / 分析绘图 — investigate supplied data; explicit table columns retain the no-API shortcut",
    "figure": "Editable method figure / 方法图 — describe components and relationships; deliver code, SVG and PNG",
    "code": "Code project / 代码项目 — create from a goal or modify a project, with authorized validation",
}


FIELDS = {
    'request': str, 'model': str, 'source': list, 'output_root': str,
    'kind': str, 'project': str, 'document': list, 'material': list,
    'extensions': list, 'from_session': str, 'reuse': list, 'reuse_version': int,
    'data_file': str, 'data_path': list,
    'project_python': str, 'allow_execution': bool, 'sandbox_execution': bool, 'sandbox_gpu': bool, 'allow_network': bool,
    'allow_vision': bool, 'independent_review_required': bool,
    'max_images': int, 'max_turns': int, 'max_tokens': int,
    'max_processes': int, 'max_process_seconds': (int, float),
}
PATHS = {'output_root', 'project_python', 'project', 'data_file', 'from_session'}
PATH_LISTS = {'source', 'document', 'material', 'data_path', 'extensions'}


@dataclass
class NativeTaskInputs:
    request: str | None = None
    goal: str | None = None
    model: str = 'env'
    task_config: Path | None = None
    output_root: Path | None = None
    kind: str | None = None
    project: Path | None = None
    data_file: Path | None = None
    project_python: Path | None = None
    from_session: Path | None = None
    reuse_version: int | None = None
    source: list[Path] = field(default_factory=list)
    document: list[Path] = field(default_factory=list)
    material: list[Path] = field(default_factory=list)
    data_path: list[Path] = field(default_factory=list)
    extensions: list[Path] = field(default_factory=list)
    reuse: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    validate: str | None = None
    timeout_sec: int | None = None
    with_report: bool = False
    allow_execution: bool = False
    sandbox_execution: bool = False
    sandbox_gpu: bool = False
    allow_network: bool = False
    allow_vision: bool = False
    independent_review_required: bool | None = None
    max_images: int = 0
    max_turns: int = 30
    max_tokens: int = 1_200_000
    max_processes: int | None = None
    max_process_seconds: float | None = None
    _explicit_start_destinations: set[str] = field(default_factory=set)


def native_process_limits(args: NativeTaskInputs) -> dict | None:
    limits = dict(getattr(args, '_process_limits', None) or {})
    for name, dimension in (('max_processes', 'process_invocations'),
                            ('max_process_seconds', 'process_wall_seconds')):
        value = getattr(args, name, None)
        if value is not None:
            if value < 0 or not math.isfinite(value):
                raise ValueError(f'{name} must be finite and nonnegative')
            if dimension in limits and limits[dimension] != value:
                raise ValueError(f'Conflicting {dimension} in task and structured contract')
            limits[dimension] = value
    return limits or None


def read_task_config_bytes(path: Path) -> bytes:
    with path.open('rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Task configuration must be at most 64 KiB')
    return raw


def apply_task_config(args: NativeTaskInputs) -> str | None:
    """Apply one snapshot of the TOML inputs and return its creation binding."""
    path = getattr(args, 'task_config', None)
    if path is None:
        return None
    path = path.expanduser().resolve()
    raw = read_task_config_bytes(path)
    document = tomllib.loads(raw.decode('utf-8'))
    if set(document) != {'task'} or not isinstance(document['task'], dict):
        raise ValueError('Task configuration requires one [task] table')
    unknown = set(document['task']) - FIELDS.keys()
    if unknown:
        raise ValueError('Unknown task settings: ' + ', '.join(sorted(unknown)))
    explicit = args._explicit_start_destinations
    configured = []
    for name, value in document['task'].items():
        types = FIELDS[name] if isinstance(FIELDS[name], tuple) else (FIELDS[name],)
        if type(value) not in types or isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f'Invalid task setting type: {name}')
        if name == 'kind':
            if value not in FUNCTION_LABELS:
                raise ValueError('Unknown task outcome preference: ' + value)
        if name == 'reuse' and (any(type(item) is not str or not item.strip() for item in value)
                               or len(set(value)) != len(value)):
            raise ValueError('Task reuse must list distinct nonempty registered result names')
        if name in PATH_LISTS:
            if any(type(item) is not str or not item.strip() for item in value):
                raise ValueError(f'Task {name} must list nonempty file/directory paths')
            value = [(path.parent / Path(item).expanduser()).resolve() for item in value]
        elif name in PATHS:
            candidate = path.parent / Path(value).expanduser()
            # A venv executable's symlink target belongs to the base runtime;
            # resolving it silently selects a different Python environment.
            value = candidate.absolute() if name == 'project_python' else candidate.resolve()
        if name not in explicit and not (name == 'request' and (args.request is not None or args.goal)):
            setattr(args, name, value)
            explicit.add(name)
            configured.append(name)
    args._task_config_source = str(path)
    args._task_config_fields = sorted(configured)
    native_process_limits(args)
    return hashlib.sha256(raw).hexdigest()


def _creation_source_locator(path: Path, sources: list[Path]) -> str:
    """Name an already admitted source without changing its access boundary."""
    if path in sources:
        return f'source{sources.index(path)}'
    locators = [f'source{index}/{path.relative_to(source).as_posix()}'
                for index, source in enumerate(sources)
                if source.is_dir() and path.is_relative_to(source)]
    if not locators:
        raise ValueError('Creation declaration requires an existing source mount')
    return min(locators, key=lambda value: (len(value), value))


def _native_material_roles(args: NativeTaskInputs) -> dict[str, list[str]]:
    """Resolve creation and feedback aliases through the common mount guard."""
    from simple_ar.tools.workspace import resolve_source_mount
    roles: dict[str, list[str]] = {}
    for name, values in (("source", args.source), ("document", args.document), ("material", args.material),
                         ("project", [args.project] if args.project is not None else []), ("data_path", args.data_path),
                         ("data_file", [args.data_file] if args.data_file is not None else [])):
        for value in values:
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                raise ValueError("--document requires local mounted material for native creation; put public URLs in the goal and authorize --allow-network separately.")
            path = resolve_source_mount(Path(value))
            if name == 'project' and not path.is_dir():
                raise ValueError('--project must be a read-only project directory.')
            if name == 'data_file' and not path.is_file():
                raise ValueError('--data-file must be a regular file.')
            roles.setdefault(name, []).append(str(path))
    return roles


def reuse_session_materials(args: NativeTaskInputs, *, native: bool = False,
                            access: ReuseAccess | None = None, notify: Callable[[str], None] = lambda _: None) -> None:
    root = getattr(args, "from_session", None)
    selected = getattr(args, "reuse", [])
    if root is None:
        if selected or getattr(args, 'reuse_version', None) is not None:
            raise ValueError("--reuse requires --from-session.")
        return
    root = root.resolve()
    if not selected:
        raise ValueError("Select registered result names with --reuse; inspect with results SESSION.")
    if 'code_project' in selected and (args.project or args.validate or args.allow):
        raise ValueError("Select code_project or an explicit project/scope/checker; do not override the selected project's proof in the same request.")
    selection = select_reuse(root, selected, access=access, version=getattr(args, 'reuse_version', None))
    args._reuse_origin = selection.origin
    if native:
        args.source = list(dict.fromkeys([*args.source, *selection.package_mounts]))
    available = selection.groups

    def append_materials():
        args.material = list(dict.fromkeys([*args.material, *selection.materials]))
        if selection.prior_drafts:
            args._reuse_report_drafts = selection.prior_drafts

    if "code_project" in selected:
        if selection.version is not None:
            if not native:
                raise ValueError('Retained project files require an ordinary native task')
            append_materials()
            args.from_session, args.reuse = None, []
            notify('Reuse only delivered project files with their directory layout; missing dependencies and data remain separate inputs.')
            return
        project = selection.project
        if project is None:
            raise ValueError("No current reusable code_project; complete and validate the original project first.")
        workspace, manifest, policy = project
        if manifest is None:
            if not native:
                raise ValueError('Native project reuse requires ordinary start without --chat or a structured execution protocol')
            append_materials()
            args.project = workspace
            args.from_session, args.reuse = None, []
            notify('Reuse the delivered native project as read-only material. New work uses its own budgets, interpreter and permissions; saved execution is not replayed.')
            return
        if ((not native or args.kind == 'reproduction') and selected != ["code_project"]
                or args.kind not in {None, "code", "bug_fix", "figure", "data_analysis", "reproduction"}):
            raise ValueError("Reuse one code_project independently for code/figure revision; use code_analysis for writing.")
        reproduction = args.kind == "reproduction"
        if reproduction:
            if (any(getattr(args, name, None) for name in (
                    "cwd", "hypothesis", "dataset", "expected_outcome", "metric", "output_files",
                    "metric_sources", "requirements", "install_project", "project_python"))
                    or getattr(args, "environment", "current") != "current"
                    or any(getattr(args, name, None) is not None for name in ("run_argv", "check_argv"))):
                raise ValueError("Reusing reproduction retains its measured protocol, checker and interpreter; start a separate task to change execution conditions.")
            from simple_ar.core.capabilities import CapabilityRegistry
            from simple_ar.core.session import SessionController
            session = SessionController.load(root, registry=CapabilityRegistry())
            prepared = session.manifest.state_refs.get("preparation")
            from simple_ar.app.tool_application import ToolApplication
            if session.manifest.transition_recipe == ToolApplication.RECIPE:
                execution = ToolApplication.load(root).reusable_execution() or {}
            else:
                execution = session.store.read_json(prepared).get("execution", {}) if prepared and prepared.status == "available" else {}
            if (not isinstance(execution, dict) or not execution.get("command")
                    or Path(execution.get("cwd", "")).resolve() != workspace
                    or not isinstance(execution.get("protocol"), dict)
                    or any(not execution["protocol"].get(key) for key in ("hypothesis", "dataset", "expected_outcome"))):
                raise ValueError("No prepared reproduction protocol for this validated project.")
            if (execution.get("baseline_policy", "skip") != "skip"
                    or any(execution.get(key) for key in ("pairs", "baseline", "seeds", "seed_count", "seed_flag"))
                    or set(execution["protocol"]) - {"hypothesis", "dataset", "expected_outcome", "metrics"}):
                raise ValueError("This entry repeats single-command finite protocols only; resume the original session for paired or extended protocols.")
            args.run_argv = list(execution["command"])
            args.cwd = workspace
            for key in ("hypothesis", "dataset", "expected_outcome"):
                setattr(args, key, execution["protocol"][key])
            from simple_ar.app.research_execution import execution_request
            schema = execution_request(execution).result_schema
            args.metric = list(schema.get("required_metrics", []))
            args.output_files = dict(schema.get("output_files", {}))
            args.metric_sources = dict(schema.get("metric_sources", {}))
            primary = schema.get("primary_metric")
            if primary in args.metric:
                args.metric = [primary, *[name for name in args.metric if name != primary]]
            if args.timeout_sec is None:
                args.timeout_sec = execution.get("timeout_sec", 300)
        args.kind, args.scripted, args.project = "reproduction" if reproduction else "bug_fix", False, workspace
        args.validate = manifest["benchmark"]["command"]
        args.allow = list(manifest["edit_scope"]["allowed_patterns"])
        args._reuse_protected = list(manifest["edit_scope"].get("protected_patterns", []))
        args._reuse_edit_policy = policy
        args._analysis_project = "code_analysis" in available
        if native:
            append_materials()
        python = manifest.get("environment", {}).get("policy", {}).get("python_executable")
        if reproduction:
            python = execution.get("code_task", {}).get("python_executable") or python
        if python and not args.project_python:
            args.project_python = Path(python)
        if (workspace / "data").is_dir():
            args.data_path = list(dict.fromkeys([*args.data_path, workspace / "data"]))
        args.from_session, args.reuse = None, []
        notify("Reuse a validated project in a new isolated task with its registered edit scope and checker." +
                   (" Repeat the saved reproduction protocol; no environment installation is requested." if reproduction else ""))
        return
    if args.kind == "survey" and not native:
        if selected != ["report"] or "report" not in available:
            raise ValueError("Survey follow-up reuses --reuse report's source bundle, not its prose or analysis results.")
        bundles = [path for path in available["report"] if path.suffix == ".json"
                   and json.loads(path.read_text(encoding="utf-8")).get("schema_version") == "document_bundle.v1"]
        if not bundles:
            raise ValueError("The saved report has no reusable original-source bundle.")
        # Older sessions may have untyped drafts/notes in their source bundle.
        # Recover ownership from explicit input roles, never filenames or prose.
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController
        from simple_ar.research.documents.ingest import retained_document_materials
        session = SessionController.load(root, registry=CapabilityRegistry())
        assets_ref = session.manifest.state_refs.get("assets")
        assets = session.store.read_json(assets_ref).get("assets", []) if assets_ref and assets_ref.status == "available" else []
        material_inputs = {row["locator"] for row in assets if row.get("role") == "material" and row.get("locator")}
        filtered = {}
        for path, bundle in retained_document_materials(bundles, original_sources_only=True).items():
            excluded = {row.document_id for row in bundle.records
                        if row.source == "local_files" and row.source_id in material_inputs}
            if excluded:
                payload = bundle.to_handoff_dict()
                for key in ("documents", "sections", "chunks"):
                    payload[key] = [row for row in payload[key] if row["document_id"] not in excluded]
                if not payload["documents"]:
                    raise ValueError("The saved report contains only derived material; supply original sources for the survey.")
                filtered[str(path)] = payload
        args._survey_source_bundles = filtered
        args.material.extend(bundles)
        args.material = list(dict.fromkeys(args.material))
        args.sources = args.sources or "search"
        args.from_session, args.reuse = None, []
        return
    if not native and args.kind not in {None, "writing"}:
        raise ValueError("Registered deliveries currently enter writing; use their source data for a new analysis.")
    append_materials()
    if not native:
        args.kind = "writing"
    args.from_session = None
    args.reuse = []


def prepare_native_inputs(args: NativeTaskInputs, *, goal: str, reuse_origin: dict | None = None,
                          declarations: dict | None = None, exact_code: bool = False) -> None:
    """Bind role declarations and exact code scope to the admitted mounts."""
    from simple_ar.tools.workspace import resolve_source_mount
    roles = _native_material_roles(args)
    sources = list(dict.fromkeys(Path(value)
        for name in ("source", "document", "material", "data_path", "project", "data_file")
        for value in roles.get(name, ())))
    if exact_code:
        from simple_ar.tools.implementation import ExactCodeTask
        if args.project is None or not args.allow or not args.validate:
            raise ValueError('Exact bug_fix requires --project, --allow and --validate together')
        command = shlex.split(args.validate, posix=os.name != 'nt')
        command = [value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in {'"', "'"}
                   else value for value in command]
        project = resolve_source_mount(args.project)
        code_goal = goal
        if getattr(args, '_analysis_project', False):
            from simple_ar.result_analysis.script_project import SCRIPT_RESULT_REQUIREMENTS
            code_goal += '\n\n' + SCRIPT_RESULT_REQUIREMENTS
        policy = getattr(args, '_reuse_edit_policy', None)
        args._code_task = ExactCodeTask(project_path=f'source{sources.index(project)}', goal=code_goal,
            allowed_patterns=args.allow, validation_command=command,
            protected_patterns=list(getattr(args, '_reuse_protected', []) or []),
            timeout_sec=args.timeout_sec or 300,
            **({'edit_policy': policy} if policy is not None else {})).model_dump()
        args.allow_execution = True
    from simple_ar.research.documents.ingest import analysis_material_paths
    analysis_material_paths(path.expanduser().resolve() for path in args.material if path.is_file())
    if args.data_file is not None:
        data = args.data_file.expanduser().absolute()
        from simple_ar.result_analysis.script_project import MAX_DATA_BYTES
        if not data.is_file() or data.stat().st_size > MAX_DATA_BYTES:
            raise ValueError("Creation --data-file must be a regular file of at most 20 MiB; use --source for larger bounded investigation.")
    payload = {}
    if roles:
        payload['material_roles'] = {name: [_creation_source_locator(Path(value), sources)
            for value in values] for name, values in roles.items()}
    if declarations:
        payload['data_meanings'] = declarations
    if args.kind is not None:
        payload['outcome_preference'] = args.kind
    if args.with_report:
        payload['with_report'] = True
    if reuse_origin:
        payload['reuse_origin'] = {**reuse_origin, 'prior_drafts': [
            _creation_source_locator(Path(path).resolve(), sources)
            for path in getattr(args, '_reuse_report_drafts', [])]}
    args._creation_declarations = payload
    args.request, args.goal, args.source = goal, None, sources


def prepare_client_inputs(args: NativeTaskInputs, *, access: ReuseAccess) -> None:
    """Prepare the settings offered by graphical clients without terminal prompts."""
    if args.from_session is not None or args.reuse or args.reuse_version is not None:
        reuse_session_materials(args, native=True, access=access)
    goal = args.request or args.goal
    if not goal or not goal.strip():
        raise ValueError('Supply a nonempty task goal')
    reuse_origin = getattr(args, '_reuse_origin', None)
    if reuse_origin or args.kind or args.project or args.document or args.material or args.data_file or args.data_path:
        prepare_native_inputs(args, goal=goal, reuse_origin=reuse_origin,
            exact_code=args.kind == 'bug_fix' and bool(args.allow or args.validate))
    else:
        args.request = goal


def native_creation_options(args: NativeTaskInputs) -> dict:
    """One projection of native creation access, frozen contracts and budgets."""
    return dict(model=args.model or None, control_protocol='operations.v1',
        creation_declarations=getattr(args, '_creation_declarations', {}),
        extensions=getattr(args, 'extensions', []),
        context_protocol='auto',
        sources={f'source{index}': path for index, path in enumerate(args.source)},
        allow_execution=args.allow_execution, sandbox_execution=getattr(args, 'sandbox_execution', False),
        sandbox_gpu=getattr(args, 'sandbox_gpu', False),
        allow_network=args.allow_network, allow_vision=args.allow_vision,
        independent_review_required=getattr(args, 'independent_review_required', None),
        max_turns=args.max_turns, max_tokens=args.max_tokens, max_images=args.max_images,
        project_python=args.project_python, code_task=getattr(args, '_code_task', None),
        process_limits=native_process_limits(args),
        execution_contract=getattr(args, '_execution_contract', None),
        report_requirement=getattr(args, '_report_requirement', None),
        research_policy=getattr(args, '_research_policy', None),
        table_analysis=getattr(args, '_table_analysis', None),
        source_operations=getattr(args, '_source_operations', None),
        max_attempts=getattr(args, '_task_attempt_limit', None),
        max_output_tokens=getattr(args, '_task_output_limit', None))

