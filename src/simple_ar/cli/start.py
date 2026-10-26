"""Input adapters for native tasks and explicit structured compatibility.

This module adapts inputs without inferring execution authority or owning task
state. Native creation delegates to main; structured setup exports editable
TOML and supplies the same arguments directly to the shared session entry.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys

from simple_ar.app.session_roots import new_research_session_root
from simple_ar.cli.research_config import data_options_supplied, data_settings, setup_option_contract
from simple_ar.core.console import print_line
from simple_ar.result_analysis.table import TABLE_CHOICES, TABLE_MODE_DESCRIPTIONS, TABLE_PLOT_DESCRIPTIONS, TableSpec


FUNCTION_LABELS = {
    "survey": "Direction survey / 方向调研 — compare sources and deliver a report",
    "bug_fix": "Code repair / 修改代码 — describe the fix and supply a read-only project",
    "reproduction": "Finite reproduction / 有限复现 — describe the conclusion and supply paper/project/data",
    "writing": "Material writing / 材料写作 — use notes, drafts or an analysis package",
    "data_analysis": "Data and plots / 分析绘图 — investigate supplied data; explicit table columns retain the no-API shortcut",
    "figure": "Editable method figure / 方法图 — describe components and relationships; deliver code, SVG and PNG",
    "code": "Code project / 代码项目 — create from a goal or modify a project, with authorized validation",
}


def select_start_model(args: argparse.Namespace, *, interactive: bool) -> None:
    """Resolve and display the ordinary catalog before setup or task calls."""
    if getattr(args, "_model_confirmed", False) or getattr(args, "resume_setup", None):
        return
    from dotenv import load_dotenv
    from rich.table import Table
    from rich.text import Text
    from simple_ar.core.console import make_console
    from simple_ar.integrations.model_profiles import catalog_path, load_model_catalog
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    catalog = load_model_catalog()
    if catalog is None:
        print_line("Model catalog not found. Using legacy environment settings; configure ~/.config/simple-ar/models.toml or SIMPLE_AR_MODELS_CONFIG.")
        print_line(f"Legacy model: {os.environ.get('SIMPLE_AR_MODEL', 'gpt-4o-mini') if args.model == 'env' else args.model}; timeout: {os.environ.get('SIMPLE_AR_LLM_TIMEOUT_SEC', '180')}s. Use simple-ar models to inspect catalog connections.")
        args._model_confirmed = True
        return
    available = [(name, profile) for name, profile in catalog.profiles.items() if "text" in profile.capabilities]
    if not available:
        raise ValueError("The model catalog has no text connection for task setup.")
    selected = args.model if args.model != "env" else None
    if selected is None and interactive and not (catalog.routes.get("text") or catalog.routes.get("default")):
        default_name = available[0][0]
    else:
        default_name, _ = catalog.select(selected, purpose="text")
    table = Table(title="Model connections / 模型连接", show_lines=False)
    for label in ("#", "Connection", "Model", "Endpoint", "Stream", "Timeout", "Key"):
        table.add_column(label)
    for index, (name, profile) in enumerate(available, 1):
        table.add_row(*(Text(value) for value in (str(index), name + (" (default)" if name == default_name else ""),
                      profile.model, profile.base_url, "yes" if profile.stream else "no",
                      f"{profile.request_timeout_sec:g}s", "set" if os.environ.get(profile.api_key_env, "").strip() else "missing")))
    make_console().print(table)
    print_line(f"Catalog: {catalog_path()}")
    if interactive and args.model == "env":
        answer = input(f"Model number or connection name / 模型编号或名称 [{default_name}]: ").strip()
        if answer:
            choices = {str(index): name for index, (name, _) in enumerate(available, 1)}
            default_name = choices.get(answer, answer.removeprefix("profile:"))
    name, profile = catalog.select(f"profile:{default_name}", purpose="text")
    args.model = f"profile:{name}"
    args._model_confirmed = True
    print_line(f"Selected: {args.model} → {profile.model}; stream={profile.stream}; timeout={profile.request_timeout_sec:g}s")


def _document_input(value: str) -> Path | str:
    """Keep URL spelling intact; retain the existing local Path contract."""
    return value if value.startswith(("https://", "http://")) else Path(value)


def add_data_options(parser: argparse.ArgumentParser) -> None:
    defaults = TableSpec.defaults()
    parser.add_argument("--data-file", type=Path, help="Data analysis: UTF-8 CSV/TSV or JSON records.")
    parser.add_argument("--data-attribution", default="", help="Optional user-declared data source/credit/version. Preserved in analysis and writing; not downloaded or independently verified.")
    parser.add_argument("--value-column", action="append", default=[], help="Explicit numeric column; repeat for separate metrics or selected matrix/coordinate columns.")
    parser.add_argument("--group-column", default="", help="Category column: unique row labels for values bars/heatmaps, or distinct coordinate series for line/scatter.")
    parser.add_argument("--observation-unit", default="", help="What one row represents, e.g. one independent run or one supplied summary.")
    parser.add_argument("--value-unit", default="", help="Unit shared by selected value columns; omitted is recorded as unknown.")
    parser.add_argument("--paired-baseline", default="", help="Explicit same-row paired comparison: selected baseline column; other selected observation columns are candidates with a common quantity/unit.")
    parser.add_argument("--data-association", choices=TABLE_CHOICES["association"], default=defaults["association"],
                        help="Opt-in descriptive Pearson r for supplied line/scatter x/y pairs in each group; not paired differences, significance or causal inference.")
    parser.add_argument("--data-mode", choices=TABLE_CHOICES["mode"], default=defaults["mode"],
                        help="; ".join(f"{mode}: {meaning}" for mode, meaning in TABLE_MODE_DESCRIPTIONS.items()))
    parser.add_argument("--data-missing", choices=TABLE_CHOICES["missing"], default=defaults["missing"])
    parser.add_argument("--figure-width", choices=TABLE_CHOICES["width"], default=defaults["width"], help="Generic 3.5/7-inch figure target, not a conference-specific size.")
    parser.add_argument("--data-max-mb", type=int, default=defaults["max_mb"], help="Physical input size limit, MiB.")
    parser.add_argument("--data-max-figures", type=int, default=defaults["max_figures"], help="Physical SVG page limit; overflow fails without dropping categories.")
    parser.add_argument("--data-plot", choices=TABLE_CHOICES["plot"], default=defaults["plot"],
                        help="; ".join(f"{plot}: {meaning}" for plot, meaning in TABLE_PLOT_DESCRIPTIONS.items()))
    parser.add_argument("--x-column", default="", help="line/scatter: explicit numeric x column, distinct from value columns.")
    parser.add_argument("--x-unit", default="", help="Unit of the x coordinate; omission is recorded as unknown.")
    parser.add_argument("--data-max-points", type=int, default=defaults["max_points"], help="Physical points per coordinate figure; excess fails without sampling.")
    parser.add_argument("--series-layout", choices=TABLE_CHOICES["series_layout"], default=defaults["series_layout"],
                        help="line/scatter: separate axes by default, or explicitly share axes for columns with a common declared unit.")


def add_start_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("start", help="Start a native goal-driven task; structured protocols retain explicit compatibility.")
    native = parser.add_argument_group("Native task")
    native.add_argument("request", nargs="?", help="Natural-language task using the native action loop; no function selection.")
    native.add_argument("--resume-task", type=Path, help="Resume a native task directory, or an exact displayed name with --tasks-in; never replays unknown operations.")
    native.add_argument("--tasks-in", type=Path, help="Read-only native task listing in PATH and its immediate children (at most 256 entries); no recursive search.")
    native.add_argument("--select-task", action="store_true", help="With --tasks-in, interactively select an existing native task; blank cancels.")
    native.add_argument("--task-config", type=Path, help="Create a native task from an editable TOML configuration; explicit CLI values override it.")
    native.add_argument("--write-task-config", type=Path, help="Export editable native task TOML and exit without creating a task.")
    native.add_argument("--max-processes", type=int, default=None, help="Native creation: cumulative process invocation cap; zero disables processes.")
    native.add_argument("--max-process-seconds", type=float, default=None, help="Native creation: cumulative process wall-time cap in seconds.")
    native.add_argument("--supply-task", type=Path, help="With --resume-task, confirm caller TOML contracts or first model binding. replace_existing=true explicitly revises saved contracts without resetting evidence or ledger. Records without execution; not a model retry or model replacement.")
    native.add_argument("--retry-model", action="store_true", help="With --resume-task, explicitly continue a terminated failed native model decision using a new attempt and remaining budget. Optionally select --model profile:NAME/route:NAME to record a compatible Chat connection change without clearing history or charges. Cached replies keep their saved connection; tools, in-flight requests and whole tasks are not resubmitted. Use separately from feedback or authorization changes.")
    native.add_argument("--switch-model", metavar="profile:NAME", help="With --resume-task, record a named text connection change only (profile:NAME or route:NAME). Requires --switch-id and --switch-reason. No model call, process or feedback; saved delivery and budgets remain valid. Reconcile pending operations first.")
    native.add_argument("--switch-id", help="Stable id for an idempotent --switch-model record in the original task journal.")
    native.add_argument("--switch-reason", help="Explicit human-readable reason for --switch-model.")
    native.add_argument("--authorize-remaining", action="append", default=None, metavar="DIMENSION=AMOUNT", help="Native resume: explicitly authorize a new remaining resource allowance after confirmation; repeat for multiple dimensions.")
    native.add_argument("--authorization-id", default=None, help="Native resume: stable idempotency key required for explicit continuation allowances.")
    native.add_argument("--authorization-reason", default=None, help="Native resume: human-readable reason recorded with a continuation allowance.")
    native.add_argument("--source", action="append", default=[], type=Path, help="Native task: read-only file/directory mount, repeatable; on resume, explicitly add materials after confirmation.")
    native.add_argument("--allow-execution", action="store_true", help="Native task: allow proposing process commands; each command still needs approval. Not an OS sandbox.")
    native.add_argument("--allow-network", action="store_true", help="Native task: authorize public source search/acquisition, including bounded PDFs; no installation or command authorization.")
    native.add_argument("--allow-vision", action="store_true", help="Native task: authorize sending mounted rendered images to the named TOML vision route, sharing the task model budget.")
    native.add_argument("--max-images", type=int, default=0, help="Native task: authorize this many image generation/edit submissions through the named image route (default: disabled); retained on resume.")
    native.add_argument("--approve-execution", default="", help="Native resume: exact approval token printed for its pending process.")
    native.add_argument("--decision-id", default=None, help="Exact pending research design/protocol decision; requires --resume-task.")
    native.add_argument("--decision-response", choices=("accept", "reject", "revise"), default=None)
    native.add_argument("--decision-guidance", default=None, help="Facts or direction for a revise response; not execution authorization.")
    native.add_argument("--max-turns", type=int, default=30, help="Native task: model request allowance, retained across feedback/resume.")
    native.add_argument("--max-tokens", type=int, default=600000, help="Native task: shared model token allowance, retained across feedback/resume.")
    parser.set_defaults(_native_start_destinations={action.dest for action in native._group_actions} | {'extensions'},
        extensions=[],
        _shared_start_destinations={'goal', 'model', 'output_root', 'prepare_only', 'yes', 'project_python'},
        _data_declaration_destinations={'value_column', 'group_column', 'observation_unit', 'value_unit',
            'data_attribution', 'paired_baseline', 'x_column', 'x_unit', 'data_association',
            'data_mode', 'data_missing', 'data_plot', 'series_layout', 'figure_width'})
    parser.add_argument("--guided", action="store_true", help="Choose an outcome preference for the native task; explicit protocol controls retain structured compatibility.")
    parser.add_argument("--kind", choices=tuple(FUNCTION_LABELS), help="Starting preference for native creation, or explicit structured protocol. Does not grant execution or network access.")
    parser.add_argument("--from-session", type=Path, help="Reuse registered results or a validated project from an existing session.")
    parser.add_argument("--reuse", action="append", default=[],
                        help="Registered result name shown by results SESSION; repeatable. Materials can serve any goal. code_project retains its saved edit scope and checker.")
    parser.add_argument("--chat", action="store_true", help="Opt-in model-assisted technical setup for explicit structured protocols, not the ordinary native preference entry. Requires a terminal and model connection.")
    parser.add_argument("--resume-setup", type=Path, help="Resume a saved natural-language setup directory, retaining replies and setup model accounting; not execution recovery.")
    parser.add_argument("--with-report", action="store_true", help="Data analysis: also write and review a report from the saved analysis and figures in the same session; requires a model. Default: analysis only, no task API calls.")
    parser.add_argument("--scripted", action="store_true", help="Data analysis: native goal-driven code/analysis task, with read-only data. Use --allow-execution for individually approved commands; no scaffold or preset restriction.")
    add_data_options(parser)
    parser.add_argument("--data-path", action="append", default=[], type=Path,
                        help="Reproduction/code fix: existing data file/directory, repeatable. Project inputs enter isolated code workspaces; external paths never rewrite execution commands.")
    parser.add_argument("--goal", help="Describe the question or desired fix in your own words.")
    parser.add_argument("--asset-max-mb", type=int,
                        help="Chat setup: explicit per-project/data download capacity in MiB (default 20); project ZIP expansion is limited to four times this. Does not grant download or execution permission.")
    parser.add_argument("--document", action="append", default=[], type=_document_input,
                        help="Literature tasks: local paper/reference; reproduction --chat also accepts a public HTTPS paper URL, acquired only after confirmation. Use --material for notes/drafts/analysis packages.")
    parser.add_argument("--material", action="append", default=[], type=Path, help="Writing or reproduction preparation: notes, drafts, ordinary JSON or a table_analysis.v1 analysis package. Ordinary materials are unverified; analysis packages recheck copied data when imported for writing. Not a bibliographic paper.")
    parser.add_argument("--template", help="Writing: built-in report template or Markdown template path; default material_report. Use experiment for an honest paper-style draft.")
    parser.add_argument("--sources", choices=setup_option_contract("survey")["sources"]["allowed_values"], help="Use only supplied documents, or allow online search.")
    parser.add_argument("--fulltext", action="store_true", help="Allow online survey full text, or PDF download/retention for explicitly confirmed reproduction --chat paper URLs; otherwise PDF permission is requested separately.")
    parser.add_argument("--web-extract-backend", choices=("direct", "tavily_basic"),
                        help="Explicit public-page acquisition backend; separate from PDF downloads.")
    parser.add_argument("--max-cited-sources", type=int, help="Optional maximum number of distinct sources cited in the final report.")
    parser.add_argument("--project", type=Path, help="Existing project: bug_fix edits an isolated copy; reproduction inspects it, with optional --allow/--validate for isolated result-adapter preparation.")
    parser.add_argument("--validate", help="Explicit code validation command; reproduction with --allow uses this independent check before formal measurement.")
    parser.add_argument("--allow", action="append", default=[], help="Editable project-relative path/glob; repeat as needed.")
    parser.add_argument("--project-python", type=Path,
                        help="Existing Python for native process execution or code repair/reproduction; preserves a venv entry. With reproduction --environment venv, selects the base Python for the explicitly authorized task environment.")
    parser.add_argument("--hypothesis", help="Prepared reproduction: the published conclusion to check.")
    parser.add_argument("--dataset", help="Prepared reproduction: data and any accepted adaptation.")
    parser.add_argument("--expected-outcome", help="Prepared reproduction: comparison criteria, not invented results.")
    parser.add_argument("--metric", action="append", default=[], help="Prepared reproduction: metric emitted by the command; repeat as needed.")
    parser.add_argument("--output-files", type=json.loads, default={}, help='Reproduction: JSON name-to-relative-file mapping written under SIMPLE_AR_OUTPUT_DIR, for example {"raw":"measurements.json"}. No directory scan or stdout path inference.')
    parser.add_argument("--metric-sources", type=json.loads, default={}, help='Reproduction: explicit JSON/CSV metric selectors for registered outputs; for example {"coverage":{"output":"raw","path":["coverage"]}}.')
    parser.add_argument("--cwd", type=Path, help="Prepared reproduction: existing execution directory; defaults to the current directory.")
    parser.add_argument("--timeout-sec", type=int, help="Prepared reproduction: one process limit in seconds; defaults to 300.")
    parser.add_argument("--environment", choices=setup_option_contract("reproduction")["environment"]["allowed_values"], default="current",
                        help="Reproduction: use the current environment, or explicitly create a task-local venv and install selected requirements before the declared command.")
    parser.add_argument("--requirements", action="append", default=[],
                        help="Venv: project-relative requirements file; repeatable. Defaults to requirements.txt when present, otherwise a bare venv. Installation can run build code; not an OS sandbox.")
    parser.add_argument("--install-project", action="store_true",
                        help="Reproduction venv: also install the execution project as a package, not editable. Requires a packaging declaration; may run build code/network and write source build metadata. Default: no project install.")
    parser.add_argument("--check-argv", type=json.loads, help="Reproduction: explicitly authorized short check argv as JSON, run before measurement. No inferred command or scientific metrics; uses the preparation timeout.")
    parser.add_argument("--model", default="env", help="Default: use the configured model catalog route, or legacy .env connection.")
    parser.add_argument("--interaction", choices=("assisted", "checkpoints", "autonomous"), default="checkpoints")
    parser.add_argument("--output-root", type=Path, default=Path("runs/assistant"))
    parser.add_argument("--prepare-only", action="store_true", help="Save inspectable input files without model or process calls.")
    parser.add_argument("--yes", action="store_true", help="Accept the displayed task summary; does not broaden execution authority.")
    parser.add_argument("--command", dest="run_argv", nargs=argparse.REMAINDER, help="Prepared reproduction: explicit argv. Put this option last; no shell interpretation.")


def _answer(prompt: str, current: str | None, *, interactive: bool) -> str:
    if current and current.strip():
        return current.strip()
    if not interactive:
        raise ValueError(f"Missing input: {prompt}. Supply the corresponding option or run start in a terminal.")
    answer = input(prompt + ": ").strip()
    if not answer:
        raise ValueError(f"A value is required: {prompt}")
    return answer


def _select_start_preference(args: argparse.Namespace, *, interactive: bool) -> None:
    if interactive and args.kind is None:
        from rich.panel import Panel
        from rich.table import Table
        from simple_ar.core.console import make_console
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column(style="cyan", no_wrap=True)
        table.add_column()
        for index, (name, label) in enumerate(FUNCTION_LABELS.items(), 1):
            table.add_row(str(index), label)
        make_console().print(Panel(table, title="SimpleAutoResearch · 科研助手", border_style="cyan",
                                  subtitle="Choose an outcome preference / 选择成果偏好"))
        print_line("For explicit technical protocol setup: simple-ar start --chat.")
    kind = _answer("Preference number or name / 偏好编号或名称", args.kind, interactive=interactive)
    numbers = {str(index): name for index, name in enumerate(FUNCTION_LABELS, 1)}
    args.kind = numbers.get(kind, kind)
    if args.kind not in FUNCTION_LABELS:
        raise ValueError("Choose a displayed preference number or name.")


def migrate_new_start(args: argparse.Namespace) -> bool:
    """Translate ordinary preferences, never a saved or explicit protocol.

    This only adapts inputs to _run_native_start. The native application remains
    the sole task/configuration/authorization/ledger owner. Explicit unsupported
    controls are rejected before any model selection or session creation.
    """
    # On an existing task, material aliases are added access through the same
    # confirmation/journal as --source. They must not start a new preference or
    # reinterpret feedback as a creation goal.
    material_aliases = {'project', 'document', 'material', 'data_file', 'data_path'}
    if args.resume_task and not (args.kind or args.guided or args.chat or args.resume_setup
                                 or args.scripted or args.from_session or args.reuse):
        from simple_ar.cli.parser import build_parser
        defaults = build_parser().parse_args(['start'])
        supplied = set(getattr(args, '_explicit_start_destinations', ()))
        supplied.update(name for name, value in vars(defaults).items()
                        if not name.startswith('_') and getattr(args, name, value) != value)
        if supplied & material_aliases:
            unsupported = supplied - (args._native_start_destinations
                | args._shared_start_destinations | material_aliases)
            if unsupported:
                raise ValueError('Resume cannot preserve these structured controls: '
                                 + ', '.join(sorted(unsupported)))
            roles = _native_material_roles(args)
            args.source = list(dict.fromkeys(Path(value) for values in roles.values() for value in values))
            return True
    reuse_inputs = bool(args.from_session or args.reuse)
    registered_origin = getattr(args, '_reuse_origin', None)
    if reuse_inputs and "code_project" in args.reuse:
        if args.chat or args.resume_setup or hasattr(args, '_setup_state'):
            return False  # Scientific/confirmed technical protocols keep their original owner.
        registered_origin = {'session': str(args.from_session.resolve()) if args.from_session else None,
                             'results': list(args.reuse)}
        reuse_session_materials(args, native=True)
        if getattr(args, '_material_authorize', None) is None:
            args.source.extend(path.parent for path in args.material)
        reuse_inputs = False
    if hasattr(args, '_reuse_edit_policy') and args.kind == 'reproduction':
        return False
    if args.chat or args.resume_setup or hasattr(args, "_setup_state"):
        if args.kind == "figure" or args.scripted:
            raise ValueError("Native creation cannot preserve chat/resume_setup structured controls; use ordinary preferences or an explicit supported protocol.")
        return False  # Technical setup retains its confirmed structured protocol.
    if args.kind is None:
        if not args.guided:
            if args.scripted:
                raise ValueError("--scripted requires --kind data_analysis.")
            if not (reuse_inputs or args.project or args.document or args.material
                    or args.data_file or args.data_path or args.with_report):
                return False  # Bare start and plain requests already use native main.
        if args.resume_task:
            raise ValueError("Creation preferences cannot change a saved task; use --resume-task without --guided.")
        if args.guided and args.kind is None:
            _select_start_preference(args, interactive=sys.stdin.isatty())
    scripted = args.scripted
    figure = args.kind == "figure"
    exact_code = args.kind == 'bug_fix' and bool(args.allow or args.validate)
    if scripted and args.kind != "data_analysis":
        raise ValueError("--scripted requires --kind data_analysis.")
    if args.resume_task:
        raise ValueError("Creation preferences cannot change a saved task; use --resume-task without --kind/--scripted.")
    if args.approve_execution:
        raise ValueError("--approve-execution requires --resume-task and its saved pending command.")
    if args.retry_model:
        raise ValueError("--retry-model requires --resume-task and its saved failed model decision.")
    if args.switch_model or args.switch_id is not None or args.switch_reason is not None:
        raise ValueError("--switch-model requires --resume-task, --switch-id and --switch-reason.")
    from simple_ar.cli.parser import build_parser
    defaults = build_parser().parse_args(["start"])
    explicit = set(getattr(args, "_explicit_start_destinations", ()))
    explicit.update(name for name, value in vars(defaults).items()
                    if not name.startswith("_") and getattr(args, name, value) != value)
    supported = (args._native_start_destinations | args._shared_start_destinations
                 | {"kind", "guided", "scripted", "project", "data_file", "data_path", "document", "material", "with_report", "from_session", "reuse"}
                 | args._data_declaration_destinations)
    if exact_code:
        supported |= {'allow', 'validate', 'timeout_sec'}
    protocol_controls = explicit - supported
    # Explicit value/coordinate/baseline selectors request the table shortcut.
    # Scripted declarations remain exact user intent for the native analysis.
    table_protocol = args.kind == "data_analysis" and not scripted and bool(
        explicit & {"value_column", "x_column", "paired_baseline"})
    if protocol_controls or table_protocol:
        if registered_origin and not hasattr(args, '_reuse_edit_policy'):
            raise ValueError('Native project reuse accepts goals and materials, not frozen execution controls: '
                             + ', '.join(sorted(protocol_controls or explicit & {'value_column', 'x_column', 'paired_baseline'})))
        if hasattr(args, '_reuse_edit_policy'):
            return False  # Explicit extra controls still require the confirmed structured protocol.
        if not (figure or scripted or exact_code):
            return False
        raise ValueError("Native creation cannot preserve these structured controls: "
                         + ", ".join(sorted(protocol_controls))
                         + ". Use the explicit research-session/code-task protocol when required; no options were applied.")
    reuse_origin = registered_origin
    if reuse_inputs:
        reuse_origin = {"session": str(args.from_session.resolve()) if args.from_session else None,
                        "results": list(args.reuse)}
        reuse_session_materials(args, native=True)
        # Mount complete registered result packages, not just their JSON index
        # or Markdown body. Their source data/figures must remain available to
        # the common inventory and writing owners. Never mount the whole old
        # session or import its execution state/allowances into a new task.
        if getattr(args, '_material_authorize', None) is None:
            args.source.extend(path.parent for path in args.material)
    if args.goal and args.request:
        raise ValueError("Use either the positional request or --goal, not both.")
    goal = args.request if args.request is not None else args.goal
    if goal is None:
        goal = _answer("Goal / 目标", None, interactive=sys.stdin.isatty())
    if not goal.strip():
        raise ValueError("Supply a nonempty task goal.")
    interactive = sys.stdin.isatty()
    if interactive and not (args.source or args.document or args.material or args.project or args.data_file or args.data_path):
        print_line("Materials are read-only. Supply common file/project/data paths, one per line; blank finishes.")
        while value := input("Material path / 材料路径（可留空）: ").strip():
            args.source.append(Path(value))
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
    declarations = {name: getattr(args, name) for name in sorted(args._data_declaration_destinations)
                    if name in explicit or getattr(args, name) != getattr(defaults, name)}
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
    if interactive and not args.yes and not args.prepare_only and not (args.allow_network or args.allow_execution):
        print_line("If needed for your goal, authorize public source acquisition or process proposals. Each process command still needs its own approval; this is not an OS sandbox.")
        access = input("Access / 所需访问 [none / network / process / both; default none]: ").strip().lower()
        if access not in {"", "none", "network", "process", "both"}:
            raise ValueError("Choose none, network, process or both; no access was granted.")
        args.allow_network = access in {"network", "both"}
        args.allow_execution = access in {"process", "both"}
    args.request = goal
    args.goal = None
    args.source = sources
    print_line("Ordinary preferences use the native task owner. Materials remain read-only; --allow-execution permits proposals only, and --yes accepts the summary only.")
    return True


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


def _native_material_roles(args: argparse.Namespace) -> dict[str, list[str]]:
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


def _quote(value: str) -> str:
    # JSON strings are valid TOML basic strings for these ordinary text values.
    return json.dumps(value, ensure_ascii=False)


def native_creation_options(args: argparse.Namespace) -> dict:
    """One projection of native creation access, frozen contracts and budgets."""
    from simple_ar.cli.task_options import native_process_limits
    return dict(model=args.model or None, control_protocol='operations.v1',
        creation_declarations=getattr(args, '_creation_declarations', {}),
        extensions=getattr(args, 'extensions', []),
        context_protocol='auto',
        sources={f'source{index}': path for index, path in enumerate(args.source)},
        allow_execution=args.allow_execution, allow_network=args.allow_network, allow_vision=args.allow_vision,
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




def migrate_structured_creation(args: argparse.Namespace, *, request: str, outputs: tuple,
                                sources: tuple, limits: dict, root: Path, research_config: dict,
                                report_config: dict, writing_method: str | None,
                                data_analysis: dict | None, review_model: str | None = None) -> None:
    """Project domain contracts into one task; modules own their local work."""
    from simple_ar.app.research_application import ResearchApplicationServices
    from simple_ar.app.research_intake import create_research_task
    from simple_ar.research.workflow_contracts import ResearchBrief
    model = args.model if data_analysis is None or 'report' in outputs else None
    # CLI defaults remain CLI defaults. The common intake owns normalization,
    # source aliases, report/table contracts and admission for all consumers.
    config = dict(research_use_fulltext=False, research_allow_pdf_download=False,
        research_keep_raw_pdf=False, research_max_pdf_mb=20, research_max_pdf_pages=100,
        research_read_max_shortlist=args.max_results, research_max_iterations=0,
        research_max_fulltext_documents=min(research_config.get('research_max_documents', args.max_results), args.max_results),
        research_materials_only=data_analysis is not None or args.task_kind == 'writing')
    config.update(research_config)
    config.update(research_task_kind=args.task_kind, report=report_config,
        report_writing_method=writing_method, data_analysis=data_analysis,
        max_output_tokens=getattr(args, 'max_output_tokens', None) if model else None,
        research_execution_boundary={'timeout_sec': args.timeout_sec if args.timeout_sec is not None else 300})
    if review_model:
        config['feasibility_review_model'] = review_model
    budgets = {**limits, **{name: getattr(args, name, None)
        for name in ('process_invocations', 'process_wall_seconds')}}
    services = ResearchApplicationServices(max_results=args.max_results, max_chunks=args.max_chunks,
        idea_limit=args.idea_limit, cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
        input_base_dir=Path.cwd(), config=config, budget_limits=budgets,
        max_attempts=max(30, (limits.get('llm_requests') if limits.get('llm_requests') is not None else 30 if model else 0) * 10),
        max_no_progress=6)
    app = create_research_task(ResearchBrief(request_text=request, objective=args.topic,
        requested_outputs=outputs, asset_requests=sources), root=root, services=services, model=model)
    _run_created_research(app, request=request, root=root, outputs=outputs,
        question=args.topic, data_analysis=data_analysis)


def _run_created_research(app, *, request: str, root: Path, outputs: tuple,
                          question: str, data_analysis: dict | None = None) -> None:
    """Run or collect for the same task without inventing completion of later work."""
    from simple_ar.cli.main import _render_native_result, _ensure_research_cli_success
    saved = app.state['config']
    print_line(f'Structured inputs use the common task owner. Task session: {root}')
    print_line(f'Resume: simple-ar start --resume-task "{root}"')
    deferred = False
    if saved['model']:
        status = app.run(progress=print_line)
    else:
        action = ({'tool_name': 'analyze_table', 'arguments': saved['table_analysis']} if data_analysis is not None else
            {'tool_name': 'research_collect' if saved['allow_network'] else 'research', 'arguments': {
                'operation': 'summary', 'question': question,
                'materials': saved['research_policy']['materials'], 'use_llm': False}})
        actions = [action]
        if saved.get('report_protocol') == 'canonical_report.v1' and not saved.get('report_requirement'):
            # Structured outputs are already the caller's explicit contract;
            # deterministic analysis/material owners need no model classifier.
            actions.insert(0, {'tool_name': 'update_work_plan', 'arguments': {'report_intent': {
                'kind': 'none', 'request_index': len(app.state['requests']) - 1,
                'quote': app.state['requests'][-1][:8000]}}})
        status = app.run(actions=actions, progress=print_line)
        turn = app.state['turns'][-1]
        result = app.session.store.read_json(turn['tools'][-1]['result']) if turn['tools'] else {}
        if result.get('status') == 'ok' and result.get('artifacts'):
            deferred = bool('experiments' in outputs or saved.get('report_requirement')
                or saved['research_policy']['require_design'])
            contract = saved.get('execution_contract')
            if contract is not None:
                from simple_ar.tools.scientific import measurement_conditions
                app.request_execution_confirmation()
                actions = [{'tool_name': 'measure_experiment', 'arguments': {**row['execution'],
                    **({'condition_id': row['id']} if 'conditions' in contract else {}),
                    **({'delivery': {'summary': request, 'artifacts': []}}
                        if 'conditions' not in contract and not saved.get('report_requirement') else {})}}
                    for row in measurement_conditions(contract) if not row.get('reuse_ref')]
                status = app.run(actions=actions, progress=print_line)
            else:
                completion = ({'tool_name': 'ask_user', 'arguments': {'question':
                    'Materials are retained, but the requested design, execution or authored report is not complete. '
                    'Continue this task with an authorized model and execution contract/approval as needed.'}}
                    if deferred else {'tool_name': 'deliver_result', 'arguments': {
                    'summary': request, 'artifacts': result['artifacts'],
                    'unresolved': result.get('data', {}).get('unresolved', [])}})
                status = app.run(actions=[completion], progress=print_line)
    _render_native_result(app, status)
    _ensure_research_cli_success(status['status'], operation='Research task', root=root)


def resume_structured_task(args: argparse.Namespace) -> None:
    """Compatibility spelling selects the saved common owner, never legacy load."""
    from simple_ar.cli.parser import build_parser
    from simple_ar.cli.main import _run_native_start
    native = build_parser().parse_args(['start'])
    supported = {'session_root', 'topic', 'model', 'config', 'decision_id', 'decision_response',
        'decision_guidance', 'authorization_id', 'authorization_reason', 'authorize_remaining'}
    unsupported = set(getattr(args, '_explicit_resume_destinations', ())) - supported
    if unsupported:
        raise ValueError('This common task retains frozen domain contracts. Use start --resume-task for feedback; '
                         'cannot reinterpret saved controls: ' + ', '.join(sorted(unsupported)))
    native.resume_task, native.yes = Path(args.session_root), True
    if 'topic' in getattr(args, '_explicit_resume_destinations', ()):
        native.request = args.topic
    native._explicit_start_destinations = set(getattr(args, '_explicit_resume_destinations', ())) & {'model'}
    for name in ('model', 'decision_id', 'decision_response', 'decision_guidance',
                 'authorization_id', 'authorization_reason', 'authorize_remaining'):
        setattr(native, name, getattr(args, name, None))
    _run_native_start(native)


def _fixed_execution_research(args: argparse.Namespace, sources: tuple, outputs: tuple,
                              research_config: dict | None, research_fields: set[str]) -> bool:
    """Detect source/design obligations from inputs, independently of CLI labels."""
    # Bare fixed-run mounts can carry notes without requesting source work.
    # A supplied research configuration makes those material roles actionable.
    return ('summary' in outputs
        or (research_config is not None
            and any(row['role'] in {'paper', 'material'} for row in sources))
        or bool(set(getattr(args, '_explicit_resume_destinations', ()))
                & ((research_fields - {'interaction'}) | {'selected_idea_id', 'feasibility_review_model'}))
        or bool((research_config or {}).get('research_selected_idea_id')))


def _create_declared_execution(args: argparse.Namespace, *, execution: dict, request: str,
                               outputs: tuple, sources: tuple, limits: dict, root: Path,
                               research_config: dict | None, report_config: dict | None,
                               writing_method: str | None, research_work: bool,
                               confirm_protocol: bool = False):
    """CLI defaults and domain inputs only; intake owns all creation contracts."""
    from simple_ar.app.research_application import ResearchApplicationServices
    from simple_ar.app.research_intake import create_research_task
    from simple_ar.research.workflow_contracts import ResearchBrief
    config = {**(research_config or {}), 'execution': execution,
        'interaction': getattr(args, 'interaction', None) or (research_config or {}).get('interaction') or 'checkpoints',
        'require_design': (research_config or {}).get('require_design', research_work and 'experiments' in outputs),
        'execution_confirmation': confirm_protocol,
        'report': report_config or {}, 'report_writing_method': writing_method,
        'max_output_tokens': getattr(args, 'max_output_tokens', None) if args.model else None}
    if research_work:
        config.update(queries=list(args.queries or ()),
            providers=list(args.providers or ('openalex', 'semantic_scholar', 'arxiv')))
    budgets = {**limits}
    for name, default in (('llm_requests', 30), ('total_tokens', 600000)):
        if budgets.get(name) is None:
            budgets[name] = default if args.model else 0
        elif not args.model:
            budgets[name] = 0
    services = ResearchApplicationServices(config=config, budget_limits=budgets,
        input_base_dir=Path(args.config).resolve().parent if getattr(args, 'config', None) else Path.cwd(),
        max_results=args.max_results, max_chunks=args.max_chunks, idea_limit=args.idea_limit,
        cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
        max_attempts=max(20, budgets['llm_requests'] * 10 + 16), max_no_progress=6)
    return create_research_task(ResearchBrief(request_text=request, objective=args.topic,
        requested_outputs=outputs, asset_requests=sources), root=root, services=services,
        model=args.model or None)


def _print_execution_confirmation(app, root: Path) -> None:
    """Display the caller protocol already saved by common creation."""
    decision = app.sources.pending_decision()
    print_line('Pending protocol decision: ' + json.dumps(decision, ensure_ascii=False))
    print_line(f'Confirm protocol: simple-ar start --resume-task "{root}" '
        f'--decision-id {decision["decision_id"]} --decision-response accept')


def create_declared_research(args: argparse.Namespace, *, execution: dict,
                             request: str, outputs: tuple, limits: dict, root: Path,
                             sources: tuple = (), report_config: dict | None = None,
                             writing_method: str | None = None,
                             research_config: dict | None = None) -> bool:
    """Create every declared CLI task through the common domain owner.

    Merged CLI fields have already been validated. Domain admission either
    preserves the declared contract or raises before creation; it never sends
    a different request to a second application.
    """
    from simple_ar.cli.research_config import FIELDS
    from simple_ar.cli.main import _render_native_result, _ensure_research_cli_success
    from simple_ar.tools.scientific import measurement_conditions

    scientific = 'experiments' in outputs
    research_fields = {destination for destination, _ in FIELDS['research'].values()}
    research_work = _fixed_execution_research(args, sources, outputs, research_config, research_fields)
    if scientific and execution.get('code_task') is not None and args.task_kind == 'reproduction':
        from simple_ar.app.research_execution import code_task_validation
        code_task_validation(execution, required=True)
    confirm = (scientific and not research_work
        and 'interaction' in set(getattr(args, '_explicit_resume_destinations', ()))
        and getattr(args, 'interaction', None) in {'assisted', 'checkpoints'})
    app = _create_declared_execution(args, execution=execution, request=request,
        outputs=outputs, sources=sources, limits=limits, root=root,
        research_config=research_config, report_config=report_config,
        writing_method=writing_method, research_work=research_work, confirm_protocol=confirm)
    if confirm:
        _print_execution_confirmation(app, root)
    if execution.get('code_task') is not None or research_work:
        _run_created_research(app, request=request, root=root, outputs=outputs, question=args.topic)
        return True

    # Only this caller's initial literal conditions carry one-use execution
    # intent. A model proposal, retry or later identical command still needs
    # separate approval. This is not an OS sandbox.
    contract = app.state['config']['execution_contract']
    conditions = [row for row in measurement_conditions(contract) if not row.get('reuse_ref')]
    grouped = 'conditions' in contract
    actions = [{'tool_name': 'measure_experiment', 'arguments':
        {**row['execution'], 'condition_id': row['id']} if grouped else
        row['execution'] if args.model else {**row['execution'], 'delivery': {'summary': request, 'artifacts': []}}}
        for row in conditions]
    if (not args.model and app.state['config'].get('report_protocol') == 'canonical_report.v1'
            and not app.state['config'].get('report_requirement')):
        actions.insert(0, {'tool_name': 'update_work_plan', 'arguments': {'report_intent': {
            'kind': 'none', 'request_index': len(app.state['requests']) - 1,
            'quote': app.state['requests'][-1][:8000]}}})
    print_line('Declared measurement uses the common task owner and original cwd/argv. '
        + ('Accept the bound protocol, then approve each process separately.' if confirm else
           'Each initial literal condition carries one-use caller intent; later proposals require approval.'))
    print_line(f'Task session: {root}\nResume: simple-ar start --resume-task "{root}" --approve-execution TOKEN')
    remaining = {f'caller-0-{index}': row['arguments'] for index, row in enumerate(actions)
        if row['tool_name'] == 'measure_experiment'}

    def approve(call: dict, token: str) -> bool:
        print_line(f"Execution proposal: {call['function']['arguments']}\nApproval token: {token}")
        arguments = json.loads(call['function']['arguments'])
        if (len(app.state['turns']) != 1 or app.state['turns'][0].get('origin') != 'caller'
                or call['id'] not in remaining or call['function']['name'] != 'measure_experiment'
                or arguments != remaining[call['id']]):
            return False
        if arguments.get('environment') is not None:
            binding = app.scientific.approval_binding(arguments, identity=app.state['pending']['attempt'])
            if preparation := binding.get('environment_preparation'):
                steps = preparation['steps']
                wall = len(steps) * preparation['profile']['timeout_sec'] + sum(
                    row['execution']['timeout_sec'] for row in conditions)
                print_line('Environment preparation steps (literal argv): ' + json.dumps(steps, ensure_ascii=False))
                print_line(f"Preparation once + {len(conditions)} formal measurements: {len(steps) + len(conditions)} invocations; <= {wall}s declared wall reservation. "
                           f"Shared process limits: {json.dumps(binding['limits'], sort_keys=True)}")
            else:
                print_line('Reuse verified shared environment: ' + json.dumps(binding['shared_environment'], sort_keys=True))
        del remaining[call['id']]
        return True

    status = app.run(actions=actions, approve=None if confirm else approve, progress=print_line)
    if grouped and not args.model:
        completion = app.measurement_completion_actions()
        if completion:
            status = app.run(actions=completion, progress=print_line)
    _render_native_result(app, status)
    _ensure_research_cli_success(status['status'], operation='Measurement', root=root)
    return True


def _array(values: list[str]) -> str:
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _inline_value(value) -> str:
    """Serialize validated selectors (tables, lists and ordinary scalars)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_quote(key)} = {_inline_value(item)}" for key, item in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_inline_value(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _command(arguments: list[str]) -> str:
    if sys.platform == "win32":
        import subprocess
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _export_start_config(options: dict, config: Path) -> None:
    """Export declared options only; execution never reads this projection back."""
    from simple_ar.cli.research_config import FIELDS
    rows = []
    for section, fields in FIELDS.items():
        values = {name: options[destination] for name, (destination, _) in fields.items()
                  if destination in options}
        if section == "task":
            values["output_root"] = "sessions"
        if section == "execution":
            values.update(options.get("execution_details", {}))
            if "code_task_config" in values:
                values["code_task_config"] = "code_task.toml"
        if values:
            rows.extend(["", f"[{section}]", *[f"{name} = {_inline_value(value)}"
                                               for name, value in values.items()]])
    config.write_text("\n".join(rows).lstrip() + "\n", encoding="utf-8")


def _start_session_arguments(options: dict, config: Path, *, explicit: set[str] | None = None) -> argparse.Namespace:
    """Project generated values with the config owner's types and path rules.

    These are constructed start values, not arbitrary configuration sections.
    Choice/value semantics come from start's input owners and the shared
    validate_session_arguments call in _print_research_session. Edited files
    must first pass research_defaults, including its full declaration checks.
    """
    from simple_ar.cli.parser import build_parser
    from simple_ar.cli.research_config import FIELDS, PATHS
    values = dict(options)
    unknown = set(values) - {destination for fields in FIELDS.values() for destination, _ in fields.values()} - {"execution_details"}
    if unknown:
        raise ValueError(f"Unknown generated configuration fields: {sorted(unknown)}")
    for fields in FIELDS.values():
        for destination, expected in fields.values():
            if destination not in values:
                continue
            value = values[destination]
            if type(value) is not expected or (expected is list and any(type(item) is not str for item in value)):
                raise ValueError(f"Invalid generated configuration type: {destination}")
            if destination in PATHS:
                def resolve(item):
                    target = Path(item).expanduser()
                    return str(target if target.is_absolute() else (config.resolve().parent / target).resolve())
                values[destination] = [resolve(item) for item in value] if expected is list else resolve(value)
    if "execution_details" in values:
        from simple_ar.app.research_execution import validate_execution_declaration
        validate_execution_declaration(values["execution_details"])
    result = build_parser(research_defaults=values).parse_args(["research-session"])
    result.config = config
    result._explicit_resume_destinations = (set(options) - {"execution_details"} if explicit is None else explicit) | {"config"}
    return result


def _code_task_files(goal: str, project: Path | None, validation: str, allowed: list[str],
                     python_executable: Path | None = None, *, generated_script: bool = False,
                     protected: list[str] | None = None, edit_policy: dict | None = None,
                     timeout_sec: int = 300) -> dict[str, str]:
    environment = ('mode = "current"\n' if python_executable is None else
                   'mode = "external"\n' + f'python = {_quote(str(python_executable))}\n')
    policy = {"budget_profile": "large", "allow_large_edits": True} if generated_script else dict(edit_policy or {})
    from simple_ar.code_task.editing.budget import validated_edit_budget_overrides
    overrides = validated_edit_budget_overrides(policy.pop("edit_budget_overrides", {}))
    policy_rows = ''.join(f'{key} = {str(value).lower() if type(value) is bool else _quote(value)}\n'
                          for key, value in policy.items() if value is not None)
    # New programs need complete source blocks, unlike local repository fixes.
    # Keep the large profile's total/file limits; use the existing absolute
    # per-block ceiling rather than a task-specific or unlimited exception.
    if generated_script:
        from simple_ar.code_task.editing.budget import edit_budget_for_profile
        overrides["max_new_chars"] = edit_budget_for_profile("absolute").max_new_chars
    budget_rows = ("\n[budget]\n" + ''.join(f"{key} = {value}\n" for key, value in overrides.items())) if overrides else ""
    return {
        "task.md": goal + "\n",
        "code_task.toml": "[code_task]\n" + (f"code_root = {_quote(str(project))}\n" if project else 'kind = "greenfield"\n')
        + 'task_file = "{config_dir}/task.md"\noutput_root = "{config_dir}/sessions"\n'
        + '\n[workspace]\nmode = ' + _quote("auto" if project else "empty") + '\n\n[environment]\n' + environment
        + "\n[edit_scope]\n" + f"allowed_patterns = {_array(allowed)}\n"
        + 'protected_patterns = ' + _array(list(dict.fromkeys([
            "tests/**", "**/tests/**", "test_*.py", "**/test_*.py", ".env", ".env.*", *(protected or [])]))) + '\n'
        + "\n[benchmark]\n" + f"command = {_quote(validation)}\n"
        + f'\n[execute]\nuse_llm = true\nbaseline_policy = "skip"\ntimeout_sec = {timeout_sec}\nrepair_rounds = 1\n'
        + policy_rows + budget_rows,
    }


def _reproduction_options(args: argparse.Namespace, *, interactive: bool) -> dict:
    from simple_ar.experiment.execution.outputs import metric_sources, output_files
    attachments = output_files({"output_files": args.output_files})
    selectors = metric_sources({"output_files": attachments, "metric_sources": args.metric_sources})
    protocol = {key: _answer(prompt, getattr(args, key), interactive=interactive) for key, prompt in (
        ("hypothesis", "Published conclusion / 要检查的论文结论"),
        ("dataset", "Data and adaptation / 数据及条件偏离"),
        ("expected_outcome", "Comparison criteria / 预期及判断标准"),
    )}
    argv = args.run_argv
    if argv is None:
        raw = _answer('Command argv as JSON / 命令参数列表（如 ["python", "run.py"]）', None, interactive=interactive)
        argv = json.loads(raw)
    if not isinstance(argv, list) or not argv or any(not isinstance(item, str) or not item.strip() for item in argv):
        raise ValueError("Reproduction requires a nonempty command argument list of nonempty strings.")
    python = getattr(args, "project_python", None)
    if python is not None and args.environment == "current":
        from simple_ar.code_task.execution.environment import resolve_code_task_command
        argv = resolve_code_task_command(argv, env_mode="external", python_executable=python)
        if getattr(args, "check_argv", None):
            args.check_argv = resolve_code_task_command(args.check_argv, env_mode="external", python_executable=python)
    args.run_argv = argv
    metrics = args.metric or list(selectors) or [_answer("Measured metric / stdout 或登记文件中的指标名", None, interactive=interactive)]
    if any(not value.strip() for value in metrics) or len(set(metrics)) != len(metrics):
        raise ValueError("Metric names must be nonempty and distinct.")
    cwd = (args.cwd or args.project or Path.cwd()).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"Execution directory not found: {cwd}")
    timeout = args.timeout_sec if args.timeout_sec is not None else 300
    if timeout < 1:
        raise ValueError("--timeout-sec must be positive.")
    environment = {}
    setup_steps = 0
    if getattr(args, "check_argv", None) is not None and not args.check_argv:
        raise ValueError("--check-argv must name a nonempty check command; omit it to skip the check.")
    if args.environment == "venv" or getattr(args, "check_argv", None) is not None:
        from simple_ar.research.project_environment import project_environment_plan
        requirements = (args.requirements or (["requirements.txt"] if (cwd / "requirements.txt").is_file() else [])) if args.environment == "venv" else []
        # Planning-only path: export the caller's original argv, never this future interpreter.
        plan = project_environment_plan({"mode": args.environment, "requirements": requirements,
                                       **({"python_executable": str(python)} if python is not None and args.environment == "venv" else {}),
                                       "install_project": args.install_project, "timeout_sec": timeout,
                                       "check_command": getattr(args, "check_argv", None) or []},
                                       project=cwd, directory=Path('__attempt__') / 'environment', command=argv)
        profile = plan['profile']
        setup_steps = len(plan['steps'])
        environment = profile
        if profile.get("check_command"):
            print_line(f"Preparation check: {_command(profile['check_command'])}; failure or timeout stops measurement. Check outputs are not scientific results.")
        print_line(f"Preparation: {args.environment}; requirements {profile['requirements']}; install project: {profile['install_project']}. {setup_steps} invocations, each <= {timeout}s. Venv installation may run build code/network; current mode does not install. Not an OS sandbox.")
    print_line("\n".join(f"{key}: {value}" for key, value in protocol.items()))
    print_line(f"Execution: {_command(argv)}\nDirectory: {cwd}\nOne invocation; timeout: {timeout}s; metrics: {', '.join(metrics)}")
    if not setup_steps:
        print_line("Uses the current environment, not an OS sandbox. No automatic installs, baseline training, or method changes are authorized.")
    adaptation = bool(args.validate or args.allow)
    initial_files = []
    state = getattr(args, "_setup_state", {})
    if state.get("status") == "accepted" and state.get("proposals"):
        proposal = state["proposals"][-1]
        creation = (proposal.get("execution_proposal") or {}).get("code_preparation")
        if creation:
            if (proposal.get("kind") != "reproduction" or not adaptation
                    or args.allow != creation["allowed_paths"]
                    or shlex.split(args.validate) != creation["validation_argv"]):
                raise ValueError("Confirmed adapter creation scope/checker no longer matches setup.")
            initial_files = list(creation["allowed_paths"])
            # A confirmed legacy adapter is whole-file creation, not a small
            # patch to author algorithms. Retain its bounded creation policy;
            # do not broaden
            # editable paths or apply this to ordinary repository repairs.
            args._generated_script = True
            print_line("Authorized new adapter creation uses the bounded script edit policy; author files remain read-only.")
    invocations = (3 if adaptation else 1) + setup_steps
    return {"command_argv": argv, "cwd": str(cwd), "timeout_sec": timeout,
            "primary_metric": metrics[0], "metric": metrics,
            "execution_details": {"baseline_policy": "skip", "protocol": {**protocol, "metrics": metrics},
                **({"initial_files": initial_files} if initial_files else {}),
                **({"output_files": attachments} if attachments else {}),
                **({"metric_sources": selectors} if selectors else {}),
                **({"environment": environment} if environment else {})},
            "process_invocations": invocations, "process_wall_seconds": timeout * invocations}


def _session_project(session) -> tuple[Path, dict | None, dict] | None:
    """Resolve an owner-proved project and its optional exact contract."""
    from simple_ar.app.tool_application import ToolApplication
    if session.manifest.transition_recipe == ToolApplication.RECIPE:
        return ToolApplication.load(session.store.root).reusable_code_project()
    from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
    ref = session.manifest.state_refs.get("implementation")
    if ref is None or ref.status != "available":
        return None
    result = session.store.read_json(ref)
    if result.get("status") != "validated" or not result.get("workspace_dir") or not result.get("code_task_run_dir"):
        return None
    workspace, run = Path(result["workspace_dir"]).resolve(), Path(result["code_task_run_dir"]).resolve()
    if (not workspace.is_relative_to(session.store.root) or not run.is_relative_to(session.store.root)
            or not workspace.is_dir() or code_task_paths(run).workspace_dir.resolve() != workspace):
        # A retired or relocated workspace is not an editable offering. Its
        # absence must not block independently registered portable deliveries.
        return None
    manifest = load_code_task_manifest(run)
    if not manifest.get("benchmark", {}).get("command") or not manifest.get("edit_scope", {}).get("allowed_patterns"):
        return None
    policy = {}
    for prepared in session.manifest.state_refs.values():
        if prepared.kind != "prepared_execution" or prepared.status != "available":
            continue
        execution = session.store.read_json(prepared).get("execution", {})
        if execution.get("cwd") and Path(execution["cwd"]).resolve() == workspace:
            task = execution.get("code_task", {})
            policy = {key: task[key] for key in ("budget_profile", "allow_large_edits", "edit_budget_overrides") if key in task}
            break
    return workspace, manifest, policy


def session_materials(root: Path, *, authorize=None, include_project: bool = True) -> dict[str, list[Path]]:
    """Resolve current registered deliveries, not arbitrary historical files."""
    root = root.resolve()
    if authorize is not None:
        # UI reuse uses the same artifact owner and admits each exact path
        # before reads; executable project assembly needs broader authority.
        from simple_ar.tools.workspace import RetainedDelivery
        available = RetainedDelivery(root, authorize=authorize).reusable_materials()
        available.pop('code_project', None)
        return available
    from simple_ar.core.capabilities import CapabilityRegistry
    from simple_ar.core.session import SessionController
    session = SessionController.load(root, registry=CapabilityRegistry())
    from simple_ar.app.tool_application import ToolApplication
    if session.manifest.transition_recipe == ToolApplication.RECIPE:
        # A previous delivery remains on disk during feedback. It is not the
        # current completion while the revised task is active or paused.
        if session.manifest.status not in {"completed", "partial"}:
            return {}
        from simple_ar.tools.workspace import RetainedDelivery
        available = RetainedDelivery(root).reusable_materials()
        available.pop('code_project', None)  # Complete project proof is separate from portable file groups.
        if not include_project:
            return available
        try:
            project = _session_project(session)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Portable files retain their own proof even when the complete
            # executable project is ambiguous, unavailable or changed.
            print_line(f'code_project unavailable: {exc}')
            project = None
        if project is not None:
            available['code_project'] = [project[0]]
        return available
    available: dict[str, list[Path]] = {}
    project = _session_project(session)
    if project is not None:
        available["code_project"] = [project[0]]
    for name in ("data_analysis", "report", "summary"):
        ref = session.manifest.state_refs.get(name)
        if ref is None or ref.status != "available":
            continue
        path = session.store.resolve(ref).resolve()
        if not path.is_relative_to(session.store.root) or not path.is_file():
            raise ValueError(f"Registered {name} result is missing or outside its session.")
        if name == "report":
            body = path.parent / "report_body.md"
            if not body.is_file():
                continue
            # Keep the draft and its evidence together; the draft alone is not
            # independently verified literature or a measured result.
            paths = [body]
            paths.extend(p for p in (path.parent / "experiment_evidence.json",
                                    path.parent / "citation_map.json") if p.is_file())
            documents = session.manifest.state_refs.get("documents")
            # Reuse the evidence actually consumed by this draft, including
            # later reading/linked documents, not the initial ingest alone.
            writer = session.manifest.state_refs.get("writer")
            if writer is not None and writer.status == "available":
                payload = session.store.read_json(writer)
                snapshot = payload.get("input_snapshot", {}).get("path")
                if snapshot:
                    snapshot_ref = session.store.ref(Path(writer.path).parent / snapshot)
                    sources = session.store.read_json(snapshot_ref).get("sources", [])
                    bundles = [row for row in sources if row.get("schema") == "document_bundle.v1"]
                    if len(bundles) > 1:
                        raise ValueError("Registered report has ambiguous source bundles.")
                    if bundles:
                        from simple_ar.core.capabilities import ArtifactRef
                        documents = ArtifactRef.from_dict(bundles[0])
            if documents is not None and documents.status == "available":
                retained = session.store.resolve(documents).resolve()
                if not retained.is_relative_to(session.store.root) or not retained.is_file():
                    raise ValueError("Registered report sources are missing or outside their session.")
                paths.append(retained)
        else:
            paths = [path]
        available[name] = paths
    implementation = session.manifest.state_refs.get("implementation")
    if implementation is not None and implementation.status == "available":
        payload = session.store.read_json(implementation)
        delivery = payload.get("artifact_refs", {}).get("code_analysis")
        if payload.get("status") == "validated" and isinstance(delivery, dict):
            path = (session.store.resolve(implementation).parent / delivery["path"]).resolve()
            if not path.is_relative_to(session.store.root) or not path.is_file():
                raise ValueError("Registered code analysis package is missing or outside its session.")
            available["code_analysis"] = [path]
            return available
        if payload.get("status") == "validated" and payload.get("workspace_dir"):
            workspace = Path(payload["workspace_dir"]).resolve()
            files = [workspace / "outputs/report.md", workspace / "outputs/results.json"]
            if workspace.is_relative_to(session.store.root) and all(path.is_file() for path in files):
                available["code_analysis"] = files
    return available


def reuse_session_materials(args: argparse.Namespace, *, native: bool = False) -> None:
    root = getattr(args, "from_session", None)
    selected = getattr(args, "reuse", [])
    if root is None:
        if selected:
            raise ValueError("--reuse requires --from-session.")
        return
    root = root.resolve()
    if not selected:
        raise ValueError("Select registered result names with --reuse; inspect with results SESSION.")
    args._reuse_origin = {'session': str(root), 'results': list(selected)}
    def materials():
        return session_materials(root, authorize=getattr(args, '_material_authorize', None),
                                 include_project='code_project' in selected)

    if "code_project" in selected:
        if getattr(args, '_material_authorize', None) is not None:
            raise ValueError('code_project proof requires executable-owner assembly unavailable in UI retained-material grants; use portable delivery groups or the original CLI')
        if args.project or args.validate or args.allow:
            raise ValueError("Select code_project or an explicit project/scope/checker; do not override the selected project's proof in the same request.")
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController
        session = SessionController.load(root, registry=CapabilityRegistry())
        project = _session_project(session)
        if project is None:
            raise ValueError("No current reusable code_project; complete and validate the original project first.")
        workspace, manifest, policy = project
        if manifest is None:
            if not native:
                raise ValueError('Native project reuse requires ordinary start without --chat or a structured execution protocol')
            available = materials()
            _append_reused_materials(args, root, [name for name in selected if name != 'code_project'], available)
            args.project = workspace
            args.from_session, args.reuse = None, []
            print_line('Reuse the delivered native project as read-only material. New work uses its own budgets, interpreter and permissions; saved execution is not replayed.')
            return
        if ((not native or args.kind == 'reproduction') and selected != ["code_project"]
                or args.kind not in {None, "code", "bug_fix", "figure", "data_analysis", "reproduction"}):
            raise ValueError("Reuse one code_project independently for code/figure revision; use code_analysis for writing.")
        reproduction = args.kind == "reproduction"
        if reproduction:
            if any((args.run_argv is not None, args.cwd, args.hypothesis, args.dataset,
                    args.expected_outcome, args.metric, args.output_files, args.metric_sources,
                    args.environment != "current", args.requirements, args.install_project,
                    args.check_argv is not None, args.project_python)):
                raise ValueError("Reusing reproduction retains its measured protocol, checker and interpreter; start a separate task to change execution conditions.")
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
        available = materials()
        args._analysis_project = "code_analysis" in available
        if native:
            _append_reused_materials(args, root, [name for name in selected if name != 'code_project'], available)
        python = manifest.get("environment", {}).get("policy", {}).get("python_executable")
        if reproduction:
            python = execution.get("code_task", {}).get("python_executable") or python
        if python and not args.project_python:
            args.project_python = Path(python)
        if (workspace / "data").is_dir():
            args.data_path = list(dict.fromkeys([*args.data_path, workspace / "data"]))
        args.from_session, args.reuse = None, []
        print_line("Reuse a validated project in a new isolated task with its registered edit scope and checker." +
                   (" Repeat the saved reproduction protocol; no environment installation is requested." if reproduction else ""))
        return
    if args.kind == "survey" and not native:
        available = materials()
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
    _append_reused_materials(args, root, selected, materials())
    if not native:
        args.kind = "writing"
    args.from_session = None
    args.reuse = []


def _append_reused_materials(args: argparse.Namespace, root: Path, selected: list[str],
                             available: dict[str, list[Path]]) -> None:
    """Import declared result groups for both scoped code and material goals."""
    for name in selected:
        if name not in available:
            raise ValueError(f"No current reusable {name} delivery in {root}.")
        preflight = getattr(args, '_material_preflight', None)
        if preflight is not None:
            preflight(available[name])
        args.material.extend(available[name])
        if name == "report":
            args._reuse_report_drafts = [str(available[name][0])]
    args.material = list(dict.fromkeys(args.material))


def prepare_start(args: argparse.Namespace, *, for_execution: bool = False) -> Path | argparse.Namespace | None:
    """Prepare/export inputs; return shared session arguments only after approval.

    Path-returning callers retain the configuration-export interface. The CLI
    requests arguments for direct execution, without loading the exported TOML.
    """
    interactive = sys.stdin.isatty()
    migrated = migrate_new_start(args)
    native_destinations = getattr(args, "_native_start_destinations", None)
    if native_destinations is None:
        # Saved technical setup excludes parser-private metadata. Reconstruct
        # the option contract from its existing owner, not from saved guesses.
        from simple_ar.cli.parser import build_parser
        contract = build_parser().parse_args(["start"])
    else:
        contract = args
    supplied = set(getattr(args, "_explicit_start_destinations", ()))
    native_options = supplied & contract._native_start_destinations
    legacy = supplied - contract._native_start_destinations - contract._shared_start_destinations
    if not migrated and legacy and (args.request or native_options):
        raise ValueError("Do not mix native task and legacy setup options: " + ", ".join(sorted(legacy)))
    if migrated or (not hasattr(args, "_setup_state") and
                    (args.request or args.resume_task or (not legacy and args.kind is None
                        and not (args.chat or args.resume_setup or args.from_session or args.reuse)))):
        from simple_ar.cli.main import _run_native_start
        _run_native_start(args)
        return None
    if native_options:
        raise ValueError("Do not mix native access/budget options with a structured protocol: "
                         + ", ".join(sorted(native_options)))
    reuse_session_materials(args)
    if (args.chat or args.resume_setup) and not hasattr(args, "_setup_state"):
        from simple_ar.cli.intake_dialogue import discuss_start
        select_start_model(args, interactive=interactive)
        args = discuss_start(args)
        if args is None:
            return None
    if getattr(args, "asset_max_mb", None) is not None and not hasattr(args, "_setup_state"):
        raise ValueError("--asset-max-mb applies to confirmed --chat downloads, not ordinary task preparation.")
    _select_start_preference(args, interactive=interactive)
    kind = args.kind
    if kind == "figure" or args.scripted:
        raise ValueError("Figure/scripted preferences require native entry; technical setup cannot preserve their controls as a structured protocol.")
    if kind == "code" and args.project is None:
        print_line("Structured code creation compatibility: retaining the explicit edit scope/validator and CodeTask protocol. Use a natural-language start goal for native creation.")
    if kind != "data_analysis" or args.with_report or args.chat:
        select_start_model(args, interactive=interactive)
    project_python = getattr(args, "project_python", None)
    if project_python is not None:
        if kind not in {"bug_fix", "code", "reproduction"}:
            raise ValueError("--project-python requires --kind bug_fix; reproduction uses its command/environment options.")
        from simple_ar.code_task.execution.environment import _resolve_external_python
        try:
            project_python = Path(_resolve_external_python(str(project_python)))
        except (FileNotFoundError, ValueError) as exc:
            raise ValueError(f"Project Python executable not found or invalid: {project_python}") from exc
    if args.environment != "current" or args.requirements or args.install_project:
        if kind != "reproduction" or args.environment != "venv":
            raise ValueError("Environment/requirements/project installation requires --kind reproduction --environment venv.")
    if getattr(args, "check_argv", None) is not None and kind != "reproduction":
        raise ValueError("--check-argv requires --kind reproduction.")
    data_paths = [path.expanduser().resolve() for path in getattr(args, "data_path", [])]
    if data_paths and kind not in {"reproduction", "bug_fix", "code"}:
        raise ValueError("--data-path requires --kind reproduction or bug_fix; table analysis uses --data-file.")
    for path in data_paths:
        if not path.exists():
            raise ValueError(f"Reproduction data location not found: {path}")
    goal = _answer("Goal / 目标", args.goal, interactive=interactive)
    analysis = None
    if kind == "data_analysis":
        if interactive:
            if args.data_file is None:
                args.data_file = Path(_answer("Data file / 数据路径", None, interactive=True))
            if not args.value_column:
                from simple_ar.result_analysis.table import read_table_source
                _, rows = read_table_source(args.data_file.expanduser().resolve(), max_mb=args.data_max_mb)
                columns = list(rows[0])
                print_line(f"Data preview / 数据预览: {len(rows)} rows; values below are examples, not inferred semantics.")
                for number, column in enumerate(columns, 1):
                    samples = [str(row[column])[:70] for row in rows[:3]]
                    print_line(f"{number}. {column}: {samples}")
                selected = _answer("Columns to analyze: names or comma-separated numbers / 选择分析列", None, interactive=True)
                if selected in columns:
                    args.value_column = [selected]
                else:
                    tokens = [item.strip() for item in selected.split(",")]
                    args.value_column = [columns[int(item) - 1] if item.isdecimal() and 1 <= int(item) <= len(columns) else item
                                         for item in tokens]
            args.observation_unit = _answer("What one row represents / 每行代表什么", args.observation_unit, interactive=True)
        analysis = data_settings(args)
    elif data_options_supplied(args):
        raise ValueError("Data options require --kind data_analysis.")
    if any(isinstance(path, str) for path in args.document):
        raise ValueError("Paper URLs require reproduction --chat acquisition and confirmation before ordinary preparation; --prepare-only does not fetch.")
    documents = [path.expanduser().resolve() for path in args.document]
    materials = [path.expanduser().resolve() for path in args.material]
    if args.template and kind != "writing":
        raise ValueError("--template requires --kind writing.")
    if materials and kind == "survey":
        from simple_ar.research.documents.ingest import retained_document_materials
        if any(path.suffix.lower() != ".json" for path in materials) or len(retained_document_materials(materials, original_sources_only=True)) != len(materials):
            raise ValueError("Survey materials must be saved original-source document bundles; use --document for ordinary source files.")
    elif materials and kind not in {"writing", "reproduction"} and not (kind == "data_analysis" and args.with_report):
        raise ValueError("--material requires writing, reproduction preparation or data_analysis --with-report.")
    if kind == "writing" and interactive and not (documents or materials):
        materials.append(Path(_answer("Draft, notes or results / 草稿、笔记或结果说明路径", None,
                                      interactive=interactive)).expanduser().resolve())
    if kind in {"survey", "reproduction"} and interactive and not documents and args.sources is None:
        prompt = "Local document path / 本地材料" + ("（必需）" if kind == "reproduction" else "（可留空在线检索）")
        supplied = input(prompt + ": ").strip()
        if supplied:
            documents.append(Path(supplied).expanduser().resolve())
    for path in [*documents, *materials]:
        if not path.is_file():
            raise ValueError(f"Document not found: {path}")
    if kind == "writing":
        if not (materials or documents):
            raise ValueError("Writing requires at least one --material or --document.")
        if len(set([*documents, *materials])) != len(documents) + len(materials):
            raise ValueError("Each writing file must have one role; do not repeat papers as material.")
        from simple_ar.research.documents.ports import SUPPORTED_DOCUMENT_SUFFIXES, SUPPORTED_MATERIAL_SUFFIXES
        if any(path.suffix.lower() not in SUPPORTED_DOCUMENT_SUFFIXES for path in documents) or any(
            path.suffix.lower() not in SUPPORTED_MATERIAL_SUFFIXES for path in materials):
            raise ValueError("Writing material must be text/HTML/PDF/JSON or a table_analysis.v1 package; use data_analysis to compute raw tables.")
        from simple_ar.research.documents.ingest import analysis_material_paths
        analysis_material_paths(materials)
    project = None
    validation = None
    allowed = list(args.allow)
    sources = args.sources
    reproduction_options = {}
    from simple_ar.report.templates import BUILTIN_TEMPLATE_NAMES, MATERIAL_REPORT_TEMPLATE
    writing_template = args.template or MATERIAL_REPORT_TEMPLATE
    if kind == "writing":
        from simple_ar.report.schema import ReportRuntimeConfig
        from simple_ar.report.templates import ReportTemplateError, load_report_template_bundle
        if Path(writing_template).suffix.lower() in {".md", ".markdown"}:
            writing_template = str(Path(writing_template).expanduser().resolve())
        try:
            load_report_template_bundle(report_mode="supplied_materials",
                                        config=ReportRuntimeConfig(template=writing_template))
        except ReportTemplateError as exc:
            raise ValueError(f"Invalid writing template or review criteria: {exc}") from exc
    if kind != "reproduction" and any((args.run_argv is not None, args.hypothesis, args.dataset,
            args.expected_outcome, args.metric, args.output_files, args.metric_sources, args.cwd is not None, args.timeout_sec is not None)):
        raise ValueError("Execution/protocol options require --kind reproduction; use --validate for bug_fix.")
    if kind == "data_analysis":
        if any((args.fulltext, args.project, args.validate, allowed, args.max_cited_sources)) or sources not in {None, "materials"}:
            raise ValueError("Data analysis does not authorize online search, remote retrieval or project editing.")
        if (documents or materials or sources) and not args.with_report:
            raise ValueError("Data documentation and references require --with-report; analysis alone reads only its table.")
        if any(path.suffix.lower() not in {".md", ".markdown", ".txt", ".pdf"} for path in [*documents, *materials]):
            raise ValueError("Data report documentation must be local text/PDF; supply the table through --data-file.")
        if len(set([*documents, *materials])) != len(documents) + len(materials):
            raise ValueError("Each supplied file must have one role; use --material for data documentation and --document for references.")
        sources = "materials"
    elif kind in {"survey", "reproduction", "writing"}:
        if args.max_cited_sources is not None and args.max_cited_sources < 1:
            raise ValueError("--max-cited-sources must be positive when specified.")
        if (args.validate or allowed or args.project) and kind != "reproduction":
            raise ValueError(f"{kind} does not accept project editing or validation options; choose bug_fix.")
        if args.project and not args.project.expanduser().resolve().is_dir():
            raise ValueError(f"Project directory not found: {args.project}")
        if kind in {"reproduction", "writing"}:
            paper_url_prepared = kind == "reproduction" and any(
                entry.get("adopted") and entry.get("receipt", {}).get("status") == "completed"
                and entry.get("allow_pdf_download")
                and Path(entry["receipt"]["path"]).resolve() in documents
                for entry in getattr(args, "_setup_state", {}).get("paper_acquisitions", []))
            if sources not in {None, "materials"} or (args.fulltext and not paper_url_prepared):
                raise ValueError("Prepared reproduction/writing uses supplied local material only; online preparation is not supported by this entry.")
            sources = "materials"
        else:
            sources = _answer("Source scope / 来源 [materials / search]", sources, interactive=interactive)
        if sources not in {"materials", "search"}:
            raise ValueError("Source scope must be materials or search.")
        if sources == "materials" and not (documents or materials):
            raise ValueError("materials requires at least one --document; no online search will be inferred.")
        if args.fulltext and sources != "search" and not (kind == "reproduction" and paper_url_prepared):
            raise ValueError("--fulltext enables remote retrieval and requires --sources search; local documents are already read in materials mode.")
        if kind == "reproduction":
            if args.validate or allowed:
                if not (args.validate and allowed and args.project):
                    raise ValueError("Reproduction code preparation requires --project, explicit --allow paths and an independent --validate command.")
                if args.check_argv is not None:
                    raise ValueError("Code preparation's independent validation replaces --check-argv; environment installation is authorized separately.")
                if args.environment == "venv" and shlex.split(args.validate)[0] not in {"python", "python3"}:
                    raise ValueError("Code preparation venv requires a python/python3 checker alias.")
                project = args.project.expanduser().resolve()
                if args.cwd and args.cwd.expanduser().resolve() != project:
                    raise ValueError("Reproduction code preparation must measure in its isolated project; --cwd must match --project.")
                validation = args.validate
            reproduction_options = _reproduction_options(args, interactive=interactive)
    else:
        if sources not in {None, "materials", "search"}:
            raise ValueError("Source scope must be materials or search.")
        if sources is None and documents:
            sources = "materials"
        if args.fulltext and sources != "search":
            raise ValueError("--fulltext requires explicit --sources search.")
        if kind == "code" and args.project is None:
            project = None
            if data_paths:
                raise ValueError("A new code project does not copy external data; provide a scaffold with --project to preserve declared data paths.")
        else:
            project = Path(_answer("Project path / 项目路径", str(args.project) if args.project else None,
                                   interactive=interactive)).expanduser().resolve()
        if project is not None and not project.is_dir():
            raise ValueError(f"Project directory not found: {project}")
        validation = _answer("Validation command / 验证命令", args.validate, interactive=interactive)
        if not allowed:
            allowed = [_answer("Editable path/glob / 允许修改的路径（例如 src/**）", None, interactive=interactive)]
    if project or kind == "code":
        for pattern in allowed:
            if not pattern.strip() or Path(pattern).is_absolute() or ".." in pattern.replace("\\", "/").split("/"):
                raise ValueError("Editable paths must be nonempty project-relative patterns without '..'.")
    if args.with_report and kind not in {"data_analysis", "bug_fix", "code"}:
        raise ValueError("start --with-report requires data_analysis or bug_fix; survey, reproduction and writing already request reports.")
    if args.with_report and not args.model:
        raise ValueError("--with-report requires a model connection; analysis alone needs no model.")
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from simple_ar.core.console import make_console
    summary = Table.grid(padding=(0, 2))
    for label, value in (("Task", kind), ("Goal", goal),
                         ("Model", "not used (descriptive analysis)" if kind == "data_analysis" and not args.with_report else args.model),
                         ("Interaction", args.interaction)):
        summary.add_row(Text(label, style="cyan"), Text(value))
    make_console().print(Panel(summary, title="Task preview / 任务确认", border_style="cyan"))
    if project:
        print_line(f"Project: {project}\nEdit scope: {', '.join(allowed)}\nValidation: {validation}")
        print_line("The validation command is authorized in an isolated auto-selected worktree/copy using "
                   + ("the approved task venv after installation" if kind == "reproduction" and args.environment == "venv"
                      else f"project Python {project_python}" if project_python else "the current Python environment")
                   + "; workspace isolation is not an OS sandbox.")
        if data_paths:
            print_line("Declared data inputs: " + ", ".join(str(path) for path in dict.fromkeys(data_paths)))
            print_line("Project data is copied at the same relative location and protected from automated edits; external data paths are not copied or injected into commands.")
    elif kind == "code":
        print_line(f"New empty CodeTask workspace; edit scope: {', '.join(allowed)}; validation: {validation}")
        print_line("Generated project files live under generated_project/. Validation is authorized; no dependency installation or network access is implied. Workspace isolation is not an OS sandbox.")
    elif kind == "data_analysis":
        print_line(f"Data: {analysis['file']}; values: {', '.join(args.value_column)}; mode: {args.data_mode}; missing: {args.data_missing}")
        print_line("The analysis itself makes no model/API calls or project-code execution and does not verify experiments. Descriptive statistics only; copied data may be sensitive. Optional chat setup has separate model usage.")
        if args.with_report:
            print_line("A model will write and review a report from this session's copied data, computed results and figures; no new experiments or online search.")
    else:
        print_line(f"Sources: {sources}; papers: {len(documents)}; materials: {len(materials)}. " +
                   ("One declared reproduction command requested." if kind == "reproduction" else "No code execution requested."))
        if data_paths:
            print_line("External read-only data locations (not copied or split-verified): " + ", ".join(str(path) for path in dict.fromkeys(data_paths)))
        if args.max_cited_sources is not None:
            print_line(f"Final report source limit: {args.max_cited_sources} distinct cited source(s).")
        if sources == "search":
            print_line("Reading: remote full-text/PDF retrieval enabled (best effort)." if args.fulltext else
                       "Reading: abstracts and supplied local materials only. Use --fulltext for remote full-text/PDF retrieval.")
        if kind == "writing":
            print_line("Writes from supplied material; analysis packages copy data and recheck arithmetic, not data collection or scientific validity. No experiment, synthesis, or online search is requested.")

    # Persist before the final confirmation: an EOF/decline here loses no inputs.
    root = getattr(args, "_start_root", None) or new_research_session_root(args.output_root, goal)
    for index, (source, payload) in enumerate(getattr(args, "_survey_source_bundles", {}).items(), 1):
        from simple_ar.core.artifacts import write_json
        package = root / f"reused-sources-{index}.json"
        write_json(package, payload)
        materials = [package if str(path) == source else path for path in materials]
    drafts = getattr(args, "_reuse_report_drafts", [])
    if drafts:
        from simple_ar.research.documents.ingest import build_draft_document_bundle
        from simple_ar.core.artifacts import write_json
        for index, draft in enumerate(drafts, 1):
            draft = Path(draft)
            if draft not in materials:
                continue
            package = root / f"reused-draft-{index}.json"
            bundle = build_draft_document_bundle(draft, extraction_dir=root / f"draft-extraction-{index}",
                                                  identity=f"reused-draft-{index}")
            write_json(package, bundle.to_handoff_dict())
            materials = [package if path == draft else path for path in materials]
    config = root / "research.toml"
    outputs = {"survey": ["report"], "bug_fix": ["bug_fix"], "code": ["code_task"], "reproduction": ["experiments", "report"], "writing": ["report"], "data_analysis": ["data_analysis"]}
    if args.with_report:
        outputs[kind].append("report")
    model = "" if kind == "data_analysis" and not args.with_report else args.model
    options = {"topic": goal, "task_kind": "auto" if kind == "code" else kind,
               "outputs": outputs[kind], "output_root": str((root / "sessions").resolve()),
               "model": model, "interaction": args.interaction, **reproduction_options}
    if kind in {"survey", "reproduction", "writing"} or (kind == "data_analysis" and args.with_report) or (kind in {"bug_fix", "code"} and (sources or documents)):
        options.update(research_materials_only=sources == "materials", research_use_fulltext=args.fulltext,
                       research_allow_pdf_download=args.fulltext, research_keep_raw_pdf=args.fulltext,
                       local_document=[str(path) for path in documents])
        if getattr(args, "web_extract_backend", None):
            options["research_web_extract_backend"] = args.web_extract_backend
        if args.fulltext:
            options.update(research_max_fulltext_documents=6, research_max_pdf_mb=20)
        if data_paths:
            options["data_path"] = [str(path) for path in dict.fromkeys(data_paths)]
        if kind == "reproduction" and (args.project or args.cwd):
            from simple_ar.research.preparation import inspect_project_preparation, project_preparation_markdown
            from simple_ar.core.artifacts import write_json
            facts = inspect_project_preparation(args.project or args.cwd, data_paths=tuple(data_paths),
                read_paths=tuple(getattr(args, "_setup_state", {}).get("project_read_paths", [])))
            write_json(root / "preparation.json", facts)
            (root / "preparation.md").write_text(project_preparation_markdown(facts), encoding="utf-8")
        if kind in {"writing", "data_analysis", "survey", "reproduction"}:
            options["material"] = [str(path) for path in materials]
    if kind in {"survey", "reproduction", "writing"} or args.with_report:
        options["report_document_review"] = True
        if kind in {"survey", "reproduction", "data_analysis"} or (kind == "writing" and writing_template in {
            MATERIAL_REPORT_TEMPLATE, "analysis_report", "source_review",
        }):
            # Keep the established entry default until automatic granularity has
            # a real same-material comparison; auto remains an explicit choice.
            options.update(report_draft_scope="document", report_review_scope="document")
        if args.max_cited_sources is not None:
            options["report_max_cited_sources"] = args.max_cited_sources
        if kind == "reproduction":
            options["report_template"] = "reproduction"
        elif kind in {"writing", "data_analysis", "bug_fix", "code"}:
            options["report_template"] = writing_template
        # New built-in tasks share evidence planning; custom templates and
        # existing TOML/checkpoints retain their author's structure.
        if kind in {"survey", "reproduction"} or writing_template in {"auto", *BUILTIN_TEMPLATE_NAMES}:
            options["report_outline_strategy"] = "adaptive"
    if kind == "data_analysis":
        from simple_ar.cli.research_config import FIELDS
        options.update({FIELDS["analysis"][key][0]: list(value) if isinstance(value, tuple) else value
                        for key, value in analysis.items()})
    if project or kind == "code":
        if data_paths and kind in {"bug_fix", "code"}:
            options["data_path"] = [str(path) for path in dict.fromkeys(data_paths)]
        implementation_goal = getattr(args, "_code_task_text", goal)
        if getattr(args, "_analysis_project", False):
            from simple_ar.result_analysis.script_project import SCRIPT_RESULT_REQUIREMENTS
            implementation_goal += "\n\n" + SCRIPT_RESULT_REQUIREMENTS
        if kind == "reproduction":
            implementation_goal += ("\n\nPrepare only the authorized result adapter or execution glue. "
                "Reuse the inspected author implementation; do not alter methods, splits, evaluation, "
                "data or declared measurement conditions. Preserve raw results and export the declared "
                "metrics with their identity and provenance. The independent validator is not a scientific measurement.\n"
                + "Formal measurement argv: " + json.dumps(args.run_argv, ensure_ascii=False)
                + "\nRegistered outputs: " + json.dumps(args.output_files, ensure_ascii=False)
                + "\nMetric selectors: " + json.dumps(args.metric_sources, ensure_ascii=False))
        for name, text in _code_task_files(implementation_goal, project, validation, allowed,
                                         None if kind == "reproduction" and args.environment == "venv" else project_python,
                                         generated_script=getattr(args, "_generated_script", False),
                                         protected=getattr(args, "_reuse_protected", None),
                                         edit_policy=getattr(args, "_reuse_edit_policy", None),
                                         timeout_sec=args.timeout_sec or 300).items():
            (root / name).write_text(text, encoding="utf-8")
        options["code_task_config"] = str((root / "code_task.toml").resolve())
    session_args = _start_session_arguments(options, config)
    _export_start_config(options, config)
    exported_digest = hashlib.sha256(config.read_bytes()).digest()
    if hasattr(args, "_setup_state"):
        from simple_ar.core.artifacts import write_json
        state = {**args._setup_state, "status": "configured", "configuration": str(config)}
        write_json(root / "setup.json", state)
    print_line(f"Saved task configuration: {config}")
    print_line("Run: " + _command(["simple-ar", "research-session", "--config", str(config)]))
    print_line("After a session starts, use the resume command printed by the task owner. "
               "The setup command starts a new task; do not rerun this setup to resume.")
    if args.prepare_only:
        return config
    if not args.yes:
        if not interactive:
            raise ValueError(f"Task saved at {config}. Use --yes to start non-interactively, or --prepare-only to save only.")
        if input("Start now / 现在执行? [y/N]: ").strip().lower() not in {"y", "yes"}:
            print_line("Saved without execution. Edit the files or use the printed run command later.")
            return None
    if for_execution and hashlib.sha256(config.read_bytes()).digest() != exported_digest:
        from simple_ar.cli.research_config import research_defaults
        explicit: set[str] = set()
        edited = research_defaults(["research-session", "--config", str(config)], explicit_destinations=explicit)
        session_args = _start_session_arguments(edited, config, explicit=explicit)
    return session_args if for_execution else config
