from __future__ import annotations

import argparse
import os
import shlex
import shutil
import signal
import sys
from threading import current_thread, main_thread
from pathlib import Path
from typing import Any, Callable, Sequence
from uuid import uuid4

from simple_ar.core.artifacts import read_json
from simple_ar.cli.code_task_commands import (
    _print_code_task_analyze_failure,
    _print_code_task_apply_edits,
    _print_code_task_baseline,
    _print_code_task_batch,
    _print_code_task_context,
    _print_code_task_decision,
    _print_code_task_execute,
    _print_code_task_init,
    _print_code_task_locate,
    _print_code_task_map,
    _print_code_task_plan,
    _print_code_task_probe,
    _print_code_task_propose_edits,
    _print_code_task_repair,
    _print_code_task_run,
    _print_code_task_status,
    _print_code_task_validate,
    _print_code_task_work_plan,
)
from simple_ar.cli.code_task_view import (
    confirm_next_step,
    confirm_review_gate,
    render_execute_header,
    render_execute_result,
)
from simple_ar.code_task.runtime.config import (
    CodeTaskConfigError,
    code_task_metric_schema,
    load_code_task_init_options,
    load_code_task_execute_options,
)
from simple_ar.app.cleanup import (
    CleanError,
    apply_clean_plan,
    build_clean_plan,
    build_shared_cache_clean_plan,
    build_shared_index_clean_plan,
    confirm_clean_plan,
    render_clean_plan,
)
from simple_ar.app.research_execution import merge_result_schema
from simple_ar.core.console import print_line
from simple_ar.integrations.llm import LLMCallStopped, LLMClient, LLMError
from simple_ar.retrieval.index import build_artifact_index
from simple_ar.retrieval.search import search_artifacts
from simple_ar.cli.parser import build_parser
from simple_ar.tools.cli import call_tool, print_tool_schema, serve_mcp

# Re-exported view hooks kept on cli.main for test monkeypatching compatibility.
_CODE_TASK_VIEW_HOOKS = (
    confirm_next_step,
    confirm_review_gate,
    render_execute_header,
    render_execute_result,
)


def main(argv: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    brief_alias = arguments[:1] == ["research-brief"]
    if brief_alias:
        # Compatibility spelling only: one public parser/control/ledger owner.
        # Keep the old default output directory and summary-only behavior.
        arguments = ["research-session", "--output-root", "runs/research-brief",
                     "--outputs", "summary", "--interaction", "autonomous", *arguments[1:]]
    if arguments and arguments[0] in {"run", "resume", "research-code-task", "research-experiment"}:
        raise SystemExit(
            f"The legacy command {arguments[0]} has been retired. "
            "Use research-session for new research and research-session-continue "
            "for canonical sessions. Historical outputs remain readable with status; "
            "legacy stage options are not silently translated or executed."
        )
    from simple_ar.cli.research_config import research_defaults
    explicit_config_destinations: set[str] = set()
    session_options = arguments[:arguments.index("--command")] if "--command" in arguments else arguments
    allow_resume_without_topic = bool(arguments and arguments[0] == "research-session") and any(
        option == "--session-root" or option.startswith("--session-root=")
        for option in session_options
    )
    try:
        parser = build_parser(
            research_defaults=research_defaults(
                arguments, explicit_destinations=explicit_config_destinations,
            ),
            allow_resume_without_topic=allow_resume_without_topic,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Invalid research configuration: {exc}") from exc
    args = parser.parse_args(arguments)
    if brief_alias and (args.outputs != ["summary"] or args.session_root is not None):
        raise SystemExit("research-brief creates a summary only; use research-session for other outputs or historical continuation.")
    if args.command == "models":
        from dotenv import load_dotenv
        from simple_ar.integrations.model_profiles import load_model_catalog, ModelConfigError
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
        try:
            catalog = load_model_catalog(args.config)
            if catalog is None:
                print_line("No named catalog configured; existing .env connection remains available.")
            else:
                selected = [catalog.select(f"profile:{args.profile}", purpose=args.purpose)] if args.profile else list(catalog.profiles.items())
                for name, profile in selected:
                    try:
                        profile.credential()
                        credential = "set"
                    except ModelConfigError:
                        credential = "missing"
                    print_line(f"{name}: {profile.api} / {profile.model} / {profile.base_url}; "
                               f"capabilities={','.join(profile.capabilities)}; credential={credential}; "
                               f"stream={profile.stream}; http2={profile.http2}; timeout={profile.request_timeout_sec}s")
                print_line(f"Routes: {dict(catalog.routes)}")
                print_line("Local configuration only; provider capabilities have not been probed.")
        except ModelConfigError as exc:
            raise SystemExit(str(exc)) from None
        return
    if args.command == "project-info":
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.research.preparation import inspect_project_preparation, project_preparation_markdown
        try:
            if args.output.exists():
                raise ValueError("Choose a new --output directory; existing preparation notes are not overwritten.")
            facts = inspect_project_preparation(args.project, data_paths=tuple(args.data_path))
            store = ArtifactStore(args.output)
            store.write_json("preparation.json", facts, kind="project_preparation", schema="project_preparation.v1")
            store.write_text("preparation.md", project_preparation_markdown(facts), kind="project_preparation", schema="markdown.v1")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Project inspection failed: {exc}") from exc
        print_line(f"Preparation notes: {args.output.resolve() / 'preparation.md'}")
        print_line("Read-only inspection; nothing installed or executed. Use these notes as --material for writing, or choose and confirm a documented reproduction command.")
        return
    if args.command == "report-export":
        from simple_ar.report.export import export_acm_report
        try:
            if args.layout is not None and args.latex_template is not None:
                raise ValueError('--layout and --latex-template are alternatives')
            if not args.latex_template and (args.template_asset or args.tex_engine is not None
                    or args.top_level_division is not None):
                raise ValueError('--template-asset, --tex-engine and --top-level-division require --latex-template')
            options = {}
            if args.layout is not None:
                options['layout'] = args.layout
            elif args.latex_template is None:
                options['technical_rendering'] = 'natural'
            if args.latex_template is not None:
                options['template'] = {'path': args.latex_template, 'assets': args.template_asset,
                                       'engine': args.tex_engine or 'pdflatex'}
                if args.top_level_division is not None:
                    options['template']['top_level_division'] = args.top_level_division
            if args.citation_style is not None:
                options['citation_style'] = args.citation_style
            if args.caption_source is not None:
                if args.caption_source == 'manifest' and not (args.report_dir / 'figures/figures_manifest.json').is_file():
                    raise ValueError('The selected caption manifest is not present in the report directory')
                options['caption_owner'] = 'markdown' if args.caption_source == 'adjacent' else args.caption_source
                if args.caption_source == 'adjacent':
                    options['adjacent_captions'] = True
            result = export_acm_report(args.report_dir, args.output, title=args.title, compile_pdf=args.compile,
                                       **options)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Report export failed: {exc}") from exc
        label = 'Publication project' if args.latex_template else 'Article project' if args.layout == 'article' else 'ACM project'
        print_line(f"{label}: {args.output.resolve()}; status: {result['status']}")
        if result.get("compile_note"):
            print_line(result["compile_note"])
        if args.compile and not result["compiled"]:
            raise SystemExit(1)
        return
    if args.command == "image-review":
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.reviewing.visual import run_visual_review
        from simple_ar.integrations.usage import record_usage
        try:
            output = args.output.expanduser().resolve()
            ledger_path = output.with_name(output.name + ".budget.json")
            ledger = BudgetLedger.load(ledger_path) if ledger_path.exists() else BudgetLedger(
                {"llm_requests": 1, "total_tokens": 40000}, storage_path=ledger_path)
            client = LLMClient.from_env(model=args.model, models_config=str(args.config) if args.config else None,
                purpose="vision", budget_ledger=ledger, budget_session_id=str(output),
                usage_callback=lambda usage: record_usage(output, usage, stage="visual_review"))
            result = run_visual_review(client=client,
                image_paths=tuple(path.expanduser().resolve() for path in args.input),
                goal=args.goal, output_dir=output)
        except (OSError, ValueError, LLMError) as exc:
            raise SystemExit(f"Visual inspection stopped: {exc}") from None
        print_line(f"Visual feedback: {output / 'feedback.md'}; model opinion: {result['status']}")
        print_line("Scientific correctness was not assessed. Revise the original source project or use image --input in a new version.")
        return
    if args.command == "image":
        from dotenv import load_dotenv
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.integrations.images import ImagesError, run_image
        from simple_ar.integrations.model_profiles import load_model_catalog
        load_dotenv()
        try:
            catalog = load_model_catalog(args.config)
            if catalog is None:
                raise ValueError("Configure a named Images profile and image route first.")
            output = args.output.expanduser().resolve()
            ledger_path = output.with_name(output.name + ".budget.json")
            ledger = BudgetLedger.load(ledger_path) if ledger_path.exists() else BudgetLedger(
                {"image_requests": 1}, storage_path=ledger_path)
            source = args.input.expanduser().resolve() if args.input else None
            source_store = ArtifactStore(source.parent) if source else None
            result = run_image(catalog, args.prompt, store=ArtifactStore(output), ledger=ledger,
                reservation_id="image", reserves={"image_requests": 1}, selector=args.model,
                input_ref=source_store.ref(source, kind="image") if source_store else None,
                input_store=source_store)
        except (OSError, ValueError, ImagesError) as exc:
            raise SystemExit(f"Image request stopped: {exc}") from None
        for ref in result.artifacts:
            print_line(f"{ref.kind}: {output / ref.path}")
        if result.status != "completed":
            raise SystemExit("; ".join(result.diagnostics))
        print_line("Image delivered; this raster is not measured scientific data or editable vector layers.")
        return
    if args.command == "results":
        from simple_ar.cli.start import session_materials
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.core.session import SessionController
        from simple_ar.core.capabilities import CapabilityRegistry
        try:
            session = SessionController.load(args.session, registry=CapabilityRegistry())
            deliveries = session_materials(args.session, version=args.version)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot read deliveries: {exc}") from exc
        for name, paths in deliveries.items():
            print_line(f"{name}: " + ", ".join(str(path) for path in paths))
        if not deliveries:
            print_line("No current reusable delivery. The session may be incomplete." if args.version is None
                else "No portable files in the selected retained delivery.")
        if "code_project" in deliveries:
            print_line("Ordinary project reuse includes only delivered files; supply missing data and dependencies separately.")
            print_line("Revise code/figure: simple-ar start --from-session SESSION --reuse code_project --goal 'Your changes'")
        if session.manifest.transition_recipe == ToolApplication.RECIPE:
            print_line(f"Status: {session.manifest.status}")
            for line in _delivery_version_lines(args.session):
                print_line(line)
            if args.version is not None or session.manifest.status in {"completed", "partial"}:
                if args.version is not None:
                    from simple_ar.tools.delivery_versions import DeliveryVersions
                    delivery = DeliveryVersions(args.session).select(args.version)['delivery']
                    print_line(f"Historical delivery version: {args.version}; current request status is unchanged")
                else:
                    delivery = session.store.read_json("delivery.json")
                print_line(delivery["summary"])
                for limitation in delivery.get("unresolved", []):
                    print_line(f"Unresolved: {limitation}")
            from simple_ar.cli.task_options import resume_command
            print_line(f"Continue: {resume_command(args.session)} 'Your changes'")
            return
        print_line("Continue: simple-ar start --kind writing --from-session SESSION --reuse NAME --goal 'Your changes'")
        return
    if args.command == "start":
        args._explicit_start_destinations = _explicit_session_option_destinations(parser, arguments, set(), command="start")
        from simple_ar.cli.start import prepare_start
        from simple_ar.core.locking import SessionLockError
        try:
            from simple_ar.cli.task_options import validate_interactive_options
            validate_interactive_options(args)
            from simple_ar.cli.project_tasks import handle_project_tasks
            if handle_project_tasks(args, interactive=sys.stdin.isatty()):
                return
            from simple_ar.cli.task_options import apply_task_config, write_task_config
            apply_task_config(args)
            if write_task_config(args):
                return
            prepared = prepare_start(args, for_execution=True)
        except LLMError as exc:
            raise SystemExit(f"Setup model call failed; the printed setup draft is retained: {exc}") from exc
        except (OSError, ValueError, EOFError, KeyboardInterrupt, SessionLockError) as exc:
            raise SystemExit(f"Task setup stopped: {exc}") from exc
        if isinstance(prepared, argparse.Namespace):
            _print_research_session(prepared)
        return
    if args.command == "research-session":
        args._explicit_resume_destinations = _explicit_session_option_destinations(
            parser, arguments, explicit_config_destinations,
        )
        if brief_alias and 'model' not in args._explicit_resume_destinations:
            args.model = None  # The summary alias keeps its optional-model contract.
    elif args.command == "research-report":
        args._explicit_resume_destinations = _explicit_session_option_destinations(
            parser, arguments, set(), command="research-report",
        )


    if args.command == "research-session":
        _print_research_session(args)
        return

    if args.command == "research-session-continue":
        _print_research_session_continue(args)
        return

    if args.command == "research-session-migrate":
        _print_research_session_migrate(args)
        return

    if args.command == "research-report":
        _print_research_report(args)
        return



    if args.command == "status":
        _print_status(Path(args.run_dir))
        return

    if args.command == "tools":
        if args.tools_command == "schema":
            print_tool_schema(schema_format=args.format, output=args.output)
            return
        if args.tools_command == "call":
            call_tool(
                Path(args.run_dir),
                args.tool_name,
                args_json=args.args_json,
                args_file=args.args_file,
                debug_payloads=args.debug_payloads,
            )
            return
        if args.tools_command == "serve-mcp":
            serve_mcp(Path(args.run_dir), debug_payloads=args.debug_payloads)
            return
        parser.error(f"Unknown tools command: {args.tools_command}")

    if args.command == "code-task":
        if args.code_task_command == "init":
            _print_code_task_init(args)
            return
        accounted = {
            'work-plan': _print_code_task_work_plan, 'plan': _print_code_task_plan,
            'propose-edits': _print_code_task_propose_edits, 'repair': _print_code_task_repair,
            'baseline': _print_code_task_baseline, 'run': _print_code_task_run,
            'execute': _print_code_task_execute,
        }
        if handler := accounted.get(args.code_task_command):
            handler(args)
            return
        if args.code_task_command == "probe":
            _print_code_task_probe(args)
            return
        if args.code_task_command == "map":
            _print_code_task_map(args)
            return
        if args.code_task_command == "locate":
            _print_code_task_locate(args)
            return
        if args.code_task_command == "context":
            _print_code_task_context(args)
            return
        if args.code_task_command == "batch":
            _print_code_task_batch(args)
            return
        if args.code_task_command == "decide-plan":
            _print_code_task_decision(args)
            return
        if args.code_task_command == "apply-edits":
            _print_code_task_apply_edits(args)
            return
        if args.code_task_command == "validate":
            _print_code_task_validate(args)
            return
        if args.code_task_command == "analyze-failure":
            _print_code_task_analyze_failure(args)
            return
        parser.error(f"Unknown code-task command: {args.code_task_command}")

    if args.command == "inspect":
        _print_inspect(Path(args.run_dir))
        return

    if args.command == "search-artifacts":
        _print_artifact_search(
            Path(args.run_dir),
            args.query,
            top_k=args.top_k,
            include_operational=args.include_operational,
        )
        return

    if args.command == "clean":
        _print_clean(args)
        return

    parser.error(f"Unknown command: {args.command}")






def _experiment_result_schema(args: argparse.Namespace) -> dict[str, object]:
    """Normalize the few metric controls exposed by the small CLI."""

    primary = str(args.primary_metric or "").strip()
    metrics = [str(item).strip() for item in args.metric if str(item).strip()]
    required = list(dict.fromkeys(([primary] if primary else []) + metrics))
    directions: dict[str, str] = {}
    for item in args.metric_direction:
        name, separator, direction = str(item).partition("=")
        if not separator or not name.strip() or not direction.strip():
            raise SystemExit(f"Invalid --metric-direction value: {item!r}")
        directions[name.strip()] = direction.strip()
    schema: dict[str, object] = {"required_metrics": required}
    if primary:
        schema["primary_metric"] = primary
    if directions:
        schema["metric_directions"] = directions
    return schema


def _print_research_session(args: argparse.Namespace) -> None:
    """Run the canonical bounded research-to-report application."""

    from simple_ar.app.research_intake import is_common_research_session
    from simple_ar.app.session_roots import new_research_session_root
    from simple_ar.research.workflow_contracts import ResearchBrief

    if getattr(args, "session_root", None) is not None:
        if is_common_research_session(args.session_root):
            from simple_ar.cli.start import resume_structured_task
            try:
                resume_structured_task(args)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                raise SystemExit(f"Cannot continue this research task: {exc}") from exc
            return
        if 'independent_review_required' in getattr(args, '_explicit_resume_destinations', ()):
            raise SystemExit('This option requires an existing common task; historical research sessions retain their saved review contract.')
        raise SystemExit(
            f"Cannot continue historical pre-v2.10 session {args.session_root}: "
            "historical sessions remain readable, but execution now requires a native task created with `simple-ar start`."
        )

    from simple_ar.cli.research_config import report_settings, report_writing_method, validate_session_arguments

    inputs = validate_session_arguments(args)
    report_config = report_settings(args)
    writing_method = None
    if getattr(args, "session_root", None) is None:
        try:
            writing_method, report_config = report_writing_method(args, report_config)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    task_kind, command = inputs.task_kind, inputs.command
    execution_details, outputs = inputs.execution_details, inputs.outputs
    materials, data_analysis = inputs.materials, inputs.data_analysis
    code_task_spec = None
    code_task_baseline_policy = "auto"
    code_task_config = getattr(args, "code_task_config", None)
    if code_task_config:
        if command and task_kind != "reproduction":
            raise SystemExit(
                "Use either --command or --code-task-config for research-session, not both."
            )
        if not args.model:
            raise SystemExit("--code-task-config requires --model for Code-Task generation.")
        try:
            code_task_spec, execute_options = _load_code_task_options_for_cli(
                _resolve_cli_path(code_task_config)
            )
        except (CodeTaskConfigError, RuntimeError, TypeError, ValueError) as exc:
            raise SystemExit(f"Invalid research Code-Task configuration: {exc}") from exc
        missing_inputs = []
        code_root = Path(code_task_spec.code_root).absolute() if code_task_spec.code_root is not None else None
        task_file = Path(code_task_spec.task_file).absolute() if code_task_spec.task_file is not None else None
        if code_root is not None and not code_root.is_dir():
            missing_inputs.append(f"project directory: {code_root}")
        if task_file is not None and not task_file.is_file():
            missing_inputs.append(f"task file: {task_file}")
        if code_task_spec.env_mode == "external":
            python = code_task_spec.python_executable
            if not python:
                missing_inputs.append("[environment].python_executable (required for external mode)")
            elif not Path(python).is_file() and shutil.which(python) is None:
                missing_inputs.append(f"external Python executable: {python}")
        if missing_inputs:
            raise SystemExit("Code-Task input(s) not found: " + "; ".join(missing_inputs))
        if execute_options.use_llm is not True:
            raise SystemExit(
                "--code-task-config requires [execute].use_llm = true because "
                "the existing Code-Task backend generates the implementation."
            )
        code_task_baseline_policy = "skip" if task_kind in {"bug_fix", "reproduction"} or (outputs and "bug_fix" in outputs and "experiments" not in outputs) else execute_options.baseline_policy
        timeout_sec = (
            args.timeout_sec
            if args.timeout_sec is not None
            else execute_options.timeout_sec
        )
    else:
        # No command is the explicit literature-only shape of the canonical
        # application.  It must not manufacture an execution request: the
        # application will stop at its evidence summary, or continue to a
        # research-only report when a model is supplied.
        timeout_sec = args.timeout_sec if args.timeout_sec is not None else 300
    if args.with_report and args.no_report:
        raise SystemExit("Use either --with-report or --no-report for research-session, not both.")
    analysis_only = task_kind == "data_analysis" and not (args.with_report or
        ("report" in (outputs or ()) and not args.no_report))
    model_enabled = bool(args.model) and (not analysis_only or getattr(args, 'independent_review_required', False))
    review_model = (getattr(args, "feasibility_review_model", None) or "").strip()
    if review_model and not model_enabled:
        raise SystemExit("--feasibility-review-model requires --model for research design.")
    if task_kind == "data_analysis":
        report_requested = not analysis_only
    else:
        report_requested = bool(args.with_report or (task_kind != "bug_fix" and
                                (task_kind == "writing" or (model_enabled and not args.no_report))))
    if outputs is not None:
        report_requested = "report" in outputs
    if report_requested and not model_enabled:
        raise SystemExit("--with-report requires --model for report generation.")
    resume_root = getattr(args, "session_root", None)
    session_root = resume_root or new_research_session_root(args.output_root, args.topic)
    execution: dict[str, object] | None = None
    task_text = ""
    if code_task_spec is not None:
        if not code_task_spec.benchmark_command:
            raise SystemExit(
                "--code-task-config requires [benchmark].command for the canonical research-session."
            )
        validation_command = _split_cli_command(code_task_spec.benchmark_command)
        if not validation_command:
            raise SystemExit("The configured Code-Task benchmark command is empty.")
        if task_kind != "reproduction":
            command = validation_command
        if task_file is not None:
            try:
                task_text = task_file.read_text(encoding="utf-8")
            except OSError as exc:
                raise SystemExit(f"Could not read Code-Task task file: {exc}") from exc
        from simple_ar.app.research_execution import code_task_execution_options
        task = code_task_execution_options(code_task_spec, execute_options)
        if ((execution_details.get("environment") or {}).get("mode") == "venv"
                and code_task_spec.env_mode != "current"):
            raise SystemExit("Venv preparation requires an initially current CodeTask interpreter; explicit external Python is not silently replaced.")
        if task_kind == "reproduction":
            if code_root is not None and args.cwd is not None and Path(args.cwd).resolve() != code_root.resolve():
                raise SystemExit("Reproduction measurement cwd must match the isolated CodeTask project.")
            if code_task_spec.env_mode not in {"current", "external"}:
                raise SystemExit("Reproduction code preparation requires an existing current/external interpreter.")
            task.update(validation_command=validation_command,
                        validation_timeout_sec=execute_options.timeout_sec)
        execution = {
            "command": command,
            "cwd": str(code_root.resolve()) if code_root is not None else str(
                Path(args.cwd).resolve() if task_kind == 'reproduction' and args.cwd is not None
                else Path(code_task_spec.config_path).parent.resolve()),
            "timeout_sec": timeout_sec,
            "label": args.label,
            "result_schema": merge_result_schema(
                code_task_metric_schema(code_task_spec.primary_metric, code_task_spec.metric_directions), _experiment_result_schema(args)
            ),
            "code_task": task,
        }
        if code_task_baseline_policy == "run":
            execution["baseline"] = {"command": command, "label": "baseline"}
        elif code_task_baseline_policy in {"skip", "none"}:
            execution["baseline_policy"] = "skip"
        elif code_task_baseline_policy == "provided":
            raise SystemExit(
                "The canonical research-session does not import provided baseline metrics; "
                "use baseline_policy=run/auto or skip for this entrypoint."
            )
    elif command or execution_details.get("pairs"):
        execution = {
            "command": list(command),
            "cwd": str(Path(args.cwd).resolve()),
            "timeout_sec": timeout_sec,
            "label": args.label,
            "result_schema": _experiment_result_schema(args),
        }
    if execution_details:
        if execution is None:
            raise SystemExit("execution.protocol requires a command, pairs or code_task_config.")
        execution_details = dict(execution_details)
        if "initial_files" in execution_details:
            if code_task_spec is None:
                raise SystemExit("Required file creation requires explicit CodeTask preparation.")
            execution["code_task"]["initial_files"] = execution_details.pop("initial_files")
        for field in ("output_files", "metric_sources"):
            if field in execution_details:
                execution["result_schema"][field] = execution_details.pop(field)
        execution.update(execution_details)
        if execution_details.get("pairs"):
            execution.pop("baseline", None)  # Pair rows own both commands.
        from simple_ar.app.research_execution import execution_request
        execution_request(execution)
    request_text = args.topic.strip()
    from simple_ar.research.task_plan import implementation_only
    code_delivery = task_kind == "bug_fix" or implementation_only(execution, outputs or ())
    experiment_requested = not code_delivery and (execution is not None or bool(outputs and "experiments" in outputs))
    if task_text.strip():
        request_text += "\n\n## Implementation task\n\n" + task_text.strip()
    from simple_ar.report.templates import MATERIAL_REPORT_TEMPLATE
    writing_template = args.report_template if "report_template" in getattr(args, "_explicit_resume_destinations", set()) else MATERIAL_REPORT_TEMPLATE
    report_config.update(
        mode="experiment" if experiment_requested else "research_only",
        template=("reproduction" if task_kind == "reproduction" and args.report_template == "experiment" else args.report_template) if experiment_requested else (
            writing_template if task_kind == "writing" else "survey" if args.report_template == "experiment" else args.report_template
        ),
    )
    if code_delivery and report_requested:
        report_config["mode"] = "supplied_materials"
        if args.report_template == "experiment":
            report_config["template"] = MATERIAL_REPORT_TEMPLATE
    config: dict[str, object] = {
        "research_max_documents": args.max_results,
        "research_max_iterations": args.max_research_iterations,
        "report": report_config,
    }
    if getattr(args, 'independent_review_required', None) is not None:
        config['independent_review_required'] = args.independent_review_required
    if writing_method is not None:
        config["report_writing_method"] = writing_method
    if review_model and resume_root is None:
        config["feasibility_review_model"] = review_model
    if getattr(args, "read_max_shortlist", None) is not None:
        config["research_read_max_shortlist"] = args.read_max_shortlist
    if getattr(args, "research_max_pdf_pages", None) is not None:
        config["research_max_pdf_pages"] = args.research_max_pdf_pages
    for name in ("research_max_fulltext_documents", "research_max_pdf_mb"):
        value = getattr(args, name, None)
        if value is not None:
            config[name] = value
    if resume_root is None:
        config["interaction"] = getattr(args, "interaction", None) or "checkpoints"
    if task_kind != "auto":
        config["research_task_kind"] = task_kind
    if data_analysis is not None:
        config["data_analysis"] = data_analysis
    selected_idea_id = str(getattr(args, "selected_idea_id", "") or "").strip()
    if selected_idea_id:
        config["research_selected_idea_id"] = selected_idea_id
    if execution is not None:
        config["execution"] = execution
    if args.queries:
        config["research_queries"] = list(args.queries)
    if args.providers:
        config["research_sources"] = list(args.providers)
    for name in ("research_use_fulltext", "research_allow_pdf_download", "research_keep_raw_pdf", "research_materials_only", "research_web_extract_backend"):
        value = getattr(args, name, None)
        if value is not None:
            config[name] = value
    asset_requests = [
        {
            "locator": str(Path(path).expanduser().resolve()),
            "kind": "file",
            "role": "paper",
            "mutability": "read_only",
            "allowed_uses": ["read", "reference"],
        }
        for path in args.local_document
    ]
    asset_requests.extend({"locator": str(Path(path).expanduser().resolve()), "kind": "file", "role": "material",
                           "mutability": "read_only", "allowed_uses": ["read", "reference"]}
                          for path in materials)
    asset_requests.extend({"locator": str(Path(path).expanduser().resolve()), "kind": "dataset", "role": "dataset",
                           "mutability": "read_only", "allowed_uses": ["read"]}
                          for path in dict.fromkeys(getattr(args, "data_path", [])))
    if data_analysis is not None:
        asset_requests.append({"locator": data_analysis["file"], "kind": "file", "role": "dataset",
                               "mutability": "read_only", "allowed_uses": ["read"]})
    if code_delivery and code_task_spec is not None and code_task_spec.code_root is not None:
        asset_requests.append({
            "asset_id": "code_project",
            "locator": str(code_root.resolve()),
            "kind": "code",
            "role": "code",
            "mutability": "isolated_edit",
            "allowed_uses": ["read", "isolated_edit", "validate"],
        })
    assets = tuple(asset_requests)
    limits = {name: getattr(args, name, None) for name in (
        'llm_requests', 'total_tokens', 'process_invocations', 'process_wall_seconds')}
    intents = (
        ("bug_fix",) if task_kind == "bug_fix"
        else ("survey",) if task_kind == "survey"
        else ("measurement",) if task_kind == "measurement"
        else (("data_analysis", "report") if report_requested else ("data_analysis",)) if task_kind == "data_analysis"
        else ("research", "experiment") if experiment_requested else ("research",)
    )
    requested_outputs = (
        tuple(outputs) if outputs is not None
        else ("data_analysis",) if task_kind == "data_analysis"
        else (("bug_fix", "report") if report_requested else ("bug_fix",)) if task_kind == "bug_fix"
        else ("experiments", "report") if execution is not None and report_requested
        else ("experiments",) if execution is not None
        else ("report",) if report_requested
        else ("summary",)
    )
    brief = ResearchBrief(
        request_text=request_text,
        objective=args.topic.strip(),
        intents=intents,
        requested_outputs=requested_outputs,
        asset_requests=assets,
    )
    continuation = _continuation_parameters(args)
    decision_fields = (args.decision_id, args.decision_response, args.decision_guidance)
    if resume_root is None and any(decision_fields):
        raise SystemExit("A decision reply requires --session-root.")
    if resume_root is None and (
        continuation["authorization_id"] or continuation["authorization_reason"]
        or continuation["authorize_remaining"] or continuation["additional_attempts"]
        or continuation["additional_no_progress"]
    ):
        raise SystemExit("Continuation allowances require --session-root.")
    try:
        if execution is None:
            from simple_ar.cli.start import migrate_structured_creation
            migrate_structured_creation(args, request=brief.request_text,
                outputs=brief.requested_outputs, sources=brief.asset_requests,
                limits=limits, root=session_root, research_config=config,
                report_config=report_config, writing_method=writing_method,
                data_analysis=data_analysis, review_model=review_model or None)
            return
        from simple_ar.cli.start import create_declared_research
        create_declared_research(args, execution=execution, request=brief.request_text,
            outputs=brief.requested_outputs, sources=brief.asset_requests,
            limits=limits, root=session_root,
            report_config=report_config, writing_method=writing_method, research_config=config)
        return
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _split_cli_command(value: str) -> list[str]:
    """Parse the legacy Code-Task command string into explicit argv."""

    parts = shlex.split(value, posix=os.name != "nt")
    if os.name == "nt":
        parts = [
            item[1:-1] if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'" else item
            for item in parts
        ]
    return parts


def _continuation_parameters(args: argparse.Namespace) -> dict[str, object]:
    raw = getattr(args, "authorize_remaining", None)
    allowances: dict[str, int | float] = {}
    if isinstance(raw, dict):
        rows = raw.items()
    elif isinstance(raw, (list, tuple)):
        parsed: list[tuple[str, str]] = []
        for item in raw:
            name, separator, amount = str(item).partition("=")
            if not separator or not name.strip() or not amount.strip():
                raise SystemExit("--authorize-remaining must use DIMENSION=AMOUNT.")
            parsed.append((name.strip(), amount.strip()))
        rows = parsed
    elif raw is None:
        rows = ()
    else:
        raise SystemExit("continuation.remaining must be a table or repeated --authorize-remaining values.")
    for name, raw_amount in rows:
        try:
            amount = float(raw_amount)
        except (OverflowError, TypeError, ValueError) as exc:
            raise SystemExit(f"Invalid continuation allowance for {name!r}.") from exc
        normalized_name = str(name).strip()
        if not normalized_name:
            raise SystemExit("Continuation resource dimensions cannot be blank.")
        if normalized_name in allowances:
            raise SystemExit(f"Duplicate continuation resource dimension: {name}")
        allowances[normalized_name] = int(amount) if amount.is_integer() else amount
    return {
        "authorization_id": getattr(args, "authorization_id", None),
        "authorization_reason": getattr(args, "authorization_reason", None),
        "authorize_remaining": allowances,
        "additional_attempts": getattr(args, "additional_attempts", 0),
        "additional_no_progress": getattr(args, "additional_no_progress", 0),
    }


def _merge_resume_brief(
    existing: Any,
    proposed: Any,
    *,
    task_text: str,
    outputs: Sequence[str] | None,
    with_report: bool,
    no_report: bool,
) -> Any:
    """Apply only supplied CLI/TOML inputs; omitted resume fields stay unchanged."""

    from dataclasses import replace

    objective = proposed.objective.strip()
    if task_text.strip() or objective != existing.objective:
        request_text = proposed.request_text
        if objective != existing.objective and not task_text.strip():
            old_goal = existing.objective.strip()
            suffix = existing.request_text[len(old_goal):] if old_goal and existing.request_text.startswith(old_goal) else ""
            request_text = objective + suffix
    else:
        request_text = existing.request_text

    requested_outputs = existing.requested_outputs
    if outputs is not None:
        requested_outputs = tuple(outputs)
    elif with_report:
        requested_outputs = tuple(dict.fromkeys((*requested_outputs, "report")))
    elif no_report:
        requested_outputs = tuple(item for item in requested_outputs if item not in {"report", "paper", "full_paper"})

    asset_requests = list(existing.asset_requests)
    positions: dict[tuple[str, ...], int] = {}

    def asset_key(item: dict[str, Any]) -> tuple[str, ...]:
        asset_id = str(item.get("asset_id") or "").strip()
        if asset_id:
            return ("id", asset_id)
        locator = str(item.get("locator") or item.get("path") or "").strip()
        if "://" not in locator:
            locator = str(Path(locator).expanduser().resolve())
        return ("locator", locator, str(item.get("role") or "input"))

    for index, item in enumerate(asset_requests):
        positions[asset_key(dict(item))] = index
    for item in proposed.asset_requests:
        row = dict(item)
        key = asset_key(row)
        if key in positions:
            asset_requests[positions[key]] = row
        else:
            positions[key] = len(asset_requests)
            asset_requests.append(row)

    return replace(
        existing,
        request_text=request_text,
        objective=objective,
        requested_outputs=requested_outputs,
        asset_ids=(),
        asset_requests=tuple(asset_requests),
    )


def _print_research_session_continue(args: argparse.Namespace) -> None:
    """Retry an experiment through the canonical application lifecycle."""

    session_root = Path(args.session_root)
    from simple_ar.app.research_intake import retry_research_experiment
    try:
        result = retry_research_experiment(session_root,
            command=tuple(args.command_argv or ()), cwd=Path(args.cwd) if args.cwd else None, timeout_sec=args.timeout_sec,
            result_schema=_experiment_result_schema(args), label=args.label,
            parent_attempt_id=args.parent_attempt_id, progress=print_line)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if isinstance(result, dict):
        observed = result['observation'] or {}
        data = observed.get('data') or {}
        print_line(f'Research session: {session_root}')
        print_line(f"Status: {result['task']['status']}")
        print_line('Mode: deterministic')
        print_line(f"Recovery parent: {result['parent_attempt_id']}")
        if data.get('result_ref'):
            print_line(f"Execution: {data['result_ref']}")
        print_line(f"Execution status: {data.get('execution_status', observed.get('status', 'pending'))}")
        analysis = result.get('analysis') or {}
        if analysis.get('data', {}).get('analysis_ref'):
            print_line(f"Analysis: {analysis['data']['analysis_ref']}")
        if result.get('missing_conditions'):
            print_line('Current comparison lacks applicable measurements: ' + ', '.join(result['missing_conditions'])
                + '. Earlier results remain in task history.')
        elif data.get('execution_status') == 'passed' and not analysis:
            print_line('Measurements are retained; deterministic analysis remains pending.')
        if observed.get('status') != 'ok' or data.get('execution_status') != 'passed':
            raise SystemExit(observed.get('error') or 'Experiment retry did not pass; inspect the retained task evidence.')
        return
    view = result
    print_line(f"Research session: {view.session_root}")
    print_line(f"Status: {view.status}")
    print_line("Mode: deterministic")
    print_line(f"Recovery parent: {args.parent_attempt_id or 'latest experiment'}")
    if "experiment" in view.state_refs:
        print_line(f"Execution: {view.session_root / view.state_refs['experiment'].path}")
    print_line(f"Next action: {view.next_action or 'none'}")
    _ensure_research_cli_success(
        view.status,
        operation="Research session recovery",
        root=view.session_root,
        accepted={"completed", "running"},
    )
    return



def _print_research_session_migrate(args: argparse.Namespace) -> None:
    """Create and report a canonical successor for a legacy v1 session."""

    from simple_ar.app.session_migration import (
        SessionMigrationError,
        import_legacy_session,
    )

    if args.max_artifact_bytes < 1:
        raise SystemExit("--max-artifact-bytes must be positive.")
    try:
        result = import_legacy_session(
            args.source_root,
            args.destination_root,
            artifact_names=args.artifact_names,
            requested_outputs=args.requested_outputs or None,
            max_artifact_bytes=args.max_artifact_bytes,
        )
    except (
        FileExistsError,
        OSError,
        SessionMigrationError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print_line(f"Legacy session: {result.source_root}")
    print_line(f"Successor session: {result.destination_root}")
    print_line(f"Parent session: {result.parent_session}")
    print_line(f"Migration: {result.destination_root / result.migration_ref.path}")
    print_line(f"Imported artifacts: {len(result.imported)}")
    print_line(f"Skipped artifacts: {len(result.skipped)}")
    print_line(f"Historical budget: {result.budget_status}")
    print_line(f'Resume: simple-ar start --resume-task "{result.destination_root}"')


def _optional_research_llm_client(
    model: str | None,
    purpose: str,
    *, max_output_tokens: int | None = None,
) -> LLMClient | None:
    """Create the shared client only when a research command opts into LLMs."""

    if not model:
        return None
    try:
        return LLMClient.from_env(model=None if model == "env" else model, **({"max_output_tokens": max_output_tokens} if max_output_tokens is not None else {}))
    except LLMError as exc:
        raise SystemExit(f"Cannot enable LLM-backed {purpose}: {exc}") from exc


def _explicit_session_option_destinations(
    parser: argparse.ArgumentParser,
    arguments: Sequence[str],
    configured: set[str],
    *,
    command: str = "research-session",
) -> set[str]:
    """Track only config fields supplied by this CLI/TOML invocation."""

    from argparse import _SubParsersAction

    subparsers = next(
        action for action in parser._actions if isinstance(action, _SubParsersAction)
    )
    selected = subparsers.choices[command]
    options = arguments[:arguments.index("--command")] if "--command" in arguments else arguments
    provided = {item.partition("=")[0] for item in options if item.startswith("-")}
    destinations = set(configured)
    for action in selected._actions:
        if provided.intersection(action.option_strings):
            destinations.add(action.dest)
    return destinations


def _resume_research_settings(args: argparse.Namespace) -> dict[str, object]:
    """Translate only explicitly supplied search/ingestion settings."""
    explicit = getattr(args, "_explicit_resume_destinations", set())
    names = (
        "providers", "queries", "max_results", "max_chunks", "read_max_shortlist",
        "research_max_pdf_pages", "research_max_fulltext_documents", "research_max_pdf_mb",
        "idea_limit", "max_research_iterations", "research_use_fulltext",
        "research_materials_only", "research_allow_pdf_download", "research_keep_raw_pdf",
        "research_web_extract_backend", "cache_dir",
    )
    return {name: getattr(args, name, None) for name in names if name in explicit}


def _print_report_writing_plan(method: str, settings: dict) -> None:
    if method == "article":
        rounds = settings.get("max_review_iterations", 0)
        if settings.get("reviewer") != "llm" or not settings.get("document_review"):
            plan = "one complete article, deterministic assembly/audit; no model review."
        elif rounds == 0:
            plan = "one complete article with one independent document inspection; no automatic revision."
        else:
            plan = f"one complete article with an initial inspection; at most {rounds} candidate proposals, independently checked before adoption."
    else:
        plan = "explicit section/planning/review compatibility controls retained."
    print_line(f"Report writing: {method}; {plan}")


def _print_common_research_report(args: argparse.Namespace) -> None:
    """Continue the saved shared task using its existing Writer and budget."""
    from simple_ar.research import load_research_task
    from simple_ar.cli.research_config import report_settings

    root = Path(args.session_root)
    try:
        app = load_research_task(root)
        if args.model is not None and args.model != app.state['config'].get('model'):
            raise ValueError('The common session retains its saved model; research-report cannot switch it.')
        saved = app.current_report_requirement()
        explicit = getattr(args, '_explicit_resume_destinations', set())
        status = app.session.manifest.status
        settings = report_settings(args, explicit_destinations=explicit if saved is not None else None)
        request_report = app.request_report(settings, refresh=args.refresh)
        result = (_run_terminal_task(app.run, progress=print_line)
                  if request_report or status not in {'completed', 'partial'} else app.result_snapshot())
        _render_native_result(app, result)
        _ensure_research_cli_success(result['status'], operation='Research report', root=root)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


def _print_research_report(args: argparse.Namespace) -> None:
    """Generate a report from an existing research-session handoff."""

    if args.max_review_iterations < 0:
        raise SystemExit("--max-review-iterations cannot be negative.")
    from simple_ar.cli.research_config import report_settings
    explicit = getattr(args, '_explicit_resume_destinations', set())
    report_settings(args, explicit_destinations=explicit)
    from simple_ar.app.research_intake import is_common_research_session
    if is_common_research_session(args.session_root):
        _print_common_research_report(args)
        return
    raise SystemExit(
        f"Cannot continue historical pre-v2.10 session {args.session_root} with research-report: "
        "historical sessions remain readable; new research uses `simple-ar start`. No legacy workflow was executed."
    )
    return


def _report_budget_authorization_guidance(app: Any) -> str | None:
    """Show the official bounded continuation when the persisted cap is spent."""

    budget = app.controller.manifest.budget
    # A refresh needs writer, assembly and audit, not just one attempt. For
    # paused reports count only unfinished delivery actions in the saved view.
    view = app.view()
    report_states = ("writer", "report", "report_audit")
    needed_attempts = 3 if view.status == "completed" else sum(
        name not in view.state_refs for name in report_states
    )
    extra_attempts = max(0, needed_attempts - (budget.max_attempts - budget.attempts))
    extra_no_progress = int(budget.no_progress >= budget.max_no_progress)
    exhausted_resources = [
        name for name in ("llm_requests", "total_tokens")
        if app.budget_ledger.remaining(name) == 0
    ]
    if not (extra_attempts or extra_no_progress or exhausted_resources):
        return None
    session_root = Path(app.controller.store.root)
    objective = str(app.brief.objective)
    session_id = str(app.controller.manifest.session_id)
    revision = int(app.controller.manifest.revision)
    authorization_id = f"report-recovery-{session_id}-r{revision}"

    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(value)

    command = [
        "simple-ar research-session",
        "--session-root", quote(str(session_root)),
        "--topic", quote(objective),
        "--authorization-id", quote(authorization_id),
        "--authorization-reason", quote("Authorize one bounded report recovery after the persisted session budget was exhausted."),
    ]
    client = getattr(getattr(app, "services", None), "llm_client", None)
    model = getattr(client, "model", None)
    if isinstance(model, str) and model and model != "env":
        command.extend(("--model", quote(model)))
    if extra_attempts:
        command.extend(("--additional-attempts", str(extra_attempts)))
    if extra_no_progress:
        command.extend(("--additional-no-progress", str(extra_no_progress)))
    for name in sorted(exhausted_resources):
        command.extend(("--authorize-remaining", quote(f"{name}=<finite-amount>")))
    guidance = "To continue through the supported bounded entrypoint, review and run: " + " ".join(command)
    guidance += " The attempt allowance covers remaining report stages, not additional failed retries."
    if view.status == "completed":
        guidance += " This only authorizes capacity and keeps the session completed; then repeat your original research-report --refresh or report-configuration command."
    if exhausted_resources:
        guidance += (
            " Resource allowance is also exhausted for " + ", ".join(sorted(exhausted_resources))
            + "; replace each <finite-amount> with an explicitly chosen positive finite amount before running."
        )
    return guidance




def _resolve_cli_path(value: str | Path) -> Path:
    """Resolve a user-supplied path without changing artifact-relative paths."""

    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def _ensure_research_cli_success(
    status: str,
    *,
    operation: str,
    root: Path,
    accepted: set[str] | None = None,
) -> None:
    """Make non-success application results visible to shell callers."""

    allowed = accepted or {"completed"}
    if status not in allowed:
        raise SystemExit(
            f"{operation} ended with status {status!r}; inspect {root}."
        )


def _load_code_task_options_for_cli(config_path: Path):
    """Consume canonical init/execute options, not a second experiment spec."""
    execute_options = load_code_task_execute_options(config_path=str(config_path))
    return load_code_task_init_options(config_path=str(config_path), require_task_file=False), execute_options


def _print_clean(args: argparse.Namespace) -> None:
    """Preview and clean rebuildable run caches after confirmation."""
    try:
        if args.shared_cache:
            plan = build_shared_cache_clean_plan(
                index_root=args.index_root,
                literature_cache_root=args.literature_cache_root,
                allow_external_index_root=bool(args.allow_external_index_root),
            )
        elif args.shared_index:
            plan = build_shared_index_clean_plan(
                index_root=args.index_root,
                allow_external_index_root=bool(args.allow_external_index_root),
            )
        else:
            if not args.run_dir:
                raise CleanError("Missing run directory. Pass RUN_DIR or use --shared-index.")
            plan = build_clean_plan(Path(args.run_dir), all_caches=bool(args.all_caches))
    except CleanError as exc:
        raise SystemExit(str(exc)) from exc
    render_clean_plan(plan)
    if not plan.targets:
        print_line("Nothing to clean.")
        return
    if not confirm_clean_plan(plan, assume_yes=args.yes):
        print_line("Clean cancelled.")
        return
    result = apply_clean_plan(plan)
    print_line(f"Cleaned targets: {result.deleted_targets}")
    print_line(f"Deleted bytes: {_format_bytes(result.deleted_bytes)}")
    if result.deleted_sqlite_rows:
        print_line(f"Deleted shared SQLite index rows: {result.deleted_sqlite_rows}")
    if result.deleted_lancedb_rows:
        print_line(f"Deleted shared LanceDB index rows: {result.deleted_lancedb_rows}")






def _print_status(run_dir: Path) -> None:
    session_manifest_path = run_dir / "session_manifest.json"
    if session_manifest_path.exists():
        _print_research_session_status(run_dir)
        return

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"Missing manifest.json in {run_dir}")
    manifest = read_json(manifest_path)
    if manifest.get("workflow") == "code_task":
        _print_code_task_status(run_dir, manifest)
        return

    state_path = run_dir / "pipeline_state.json"
    state = read_json(state_path) if state_path.exists() else {}

    print_line(f"Run: {run_dir}")
    print_line(f"Topic: {manifest.get('topic', '')}")
    if state:
        print_line(
            "Pipeline: "
            f"{state.get('status', 'unknown')} "
            f"(last={state.get('last_stage', 'none')}, next={state.get('next_stage', 'none')})"
        )

    print_line("Stages:")
    for item in manifest.get("stages", []):
        marker = _stage_status(item)
        outputs = item.get("outputs", [])
        output_text = ", ".join(str(name) for name in outputs) if isinstance(outputs, list) else ""
        suffix = f" -> {output_text}" if output_text else ""
        print_line(f"- {item['stage_number']:02d} {item['stage']}: {marker}{suffix}")

    report_dir = run_dir / "08-report"
    report_path = report_dir / "report.md"
    report_manifest_path = report_dir / "manifest.json"
    if report_path.exists() or report_manifest_path.exists():
        print_line("Report:")
        if report_path.exists():
            print_line(f"- report.md: {report_path}")
        if report_manifest_path.exists():
            print_line(f"- manifest.json: {report_manifest_path}")


def _print_research_session_status(run_dir: Path) -> None:
    """Print the persisted checkpoint for a capability-oriented session."""

    from simple_ar.core.capabilities import CapabilityRegistry
    from simple_ar.core.session import SessionController

    try:
        controller = SessionController.load(
            run_dir,
            registry=CapabilityRegistry(),
        )
        snapshot = controller.status_snapshot()
        attempts = controller.list_attempts()
    except (FileNotFoundError, KeyError, OSError, TypeError, ValueError) as exc:
        raise SystemExit(f"Could not read research session {run_dir}: {exc}") from exc

    print_line(f"Session: {run_dir}")
    print_line(f"Session ID: {snapshot['session_id']}")
    print_line(f"Topic: {snapshot['topic']}")
    print_line(f"Profile: {snapshot.get('profile') or 'none'}")
    print_line(
        "Status: "
        f"{snapshot['status']} "
        f"(current={snapshot.get('current_attempt') or 'none'}, "
        f"attempts={snapshot['attempt_count']})"
    )
    if controller.manifest.transition_recipe == 'native-tools-v1':
        for line in _delivery_version_lines(controller.store.root):
            print_line(line)

    # Historical sessions recorded next-step decisions. Display those facts
    # without executing the retired workflow or rewriting its manifest.
    decision = snapshot.get("last_decision")
    if (
        snapshot.get("status") == "running"
        and snapshot.get("running_attempts") == 0
        and isinstance(decision, dict)
        and decision.get("action") == "accept"
        and decision.get("result_status") == "completed"
        and decision.get("next_capability") == "report"
    ):
        print_line("Handoff: ready_for_report (next=report)")
    elif (
        snapshot.get("status") == "running"
        and snapshot.get("running_attempts") == 0
        and isinstance(decision, dict)
        and decision.get("action") in {"repair", "revise"}
        and decision.get("next_capability")
    ):
        print_line(
            "Continuation: explicit "
            f"{decision['action']} -> {decision['next_capability']}"
        )

    budget = snapshot["budget"]
    print_line(
        "Budget: "
        f"{budget['attempts']}/{budget['max_attempts']} attempts, "
        f"{budget['no_progress']}/{budget['max_no_progress']} no-progress"
    )

    print_line(
        "Attempt summary: "
        f"{snapshot['completed_attempts']} completed, "
        f"{snapshot['failed_attempts']} failed, "
        f"{snapshot['blocked_attempts']} blocked, "
        f"{snapshot['running_attempts']} running"
    )
    print_line("Attempts:")
    if not attempts:
        print_line("- none")
    else:
        for attempt in attempts:
            capability = attempt.capability or "unknown"
            parent = f", parent={attempt.parent_attempt}" if attempt.parent_attempt else ""
            print_line(
                f"- {attempt.attempt_id}: {capability} {attempt.status}{parent}"
            )

    if isinstance(decision, dict):
        target = decision.get("next_capability") or "stop"
        print_line(
            "Last decision: "
            f"{decision.get('action', 'unknown')} -> {target} "
            f"({decision.get('result_status', 'unknown')})"
        )


def _print_inspect(run_dir: Path) -> None:
    """Build an artifact index and print a compact run summary."""
    index = build_artifact_index(run_dir)
    artifacts = _artifact_rows(index)
    print_line(f"Run: {run_dir}")
    print_line(f"Artifacts: {len(artifacts)}")
    print_line(f"Index: {run_dir / 'artifact_index.json'}")

    by_kind = _count_by(artifacts, "kind")
    if by_kind:
        print_line("Kinds:")
        for name, count in by_kind.items():
            print_line(f"- {name}: {count}")

    by_stage = _count_by(artifacts, "stage")
    if by_stage:
        print_line("Stages:")
        for name, count in by_stage.items():
            print_line(f"- {name}: {count}")

    if artifacts:
        print_line("Largest artifacts:")
        for artifact in sorted(artifacts, key=lambda item: int(item.get("bytes", 0)), reverse=True)[:5]:
            size = _format_bytes(int(artifact.get("bytes", 0)))
            print_line(
                f"- {artifact.get('path', '')} "
                f"({artifact.get('kind', 'unknown')}, {size})"
            )


def _print_artifact_search(
    run_dir: Path,
    query: str,
    *,
    top_k: int,
    include_operational: bool = False,
) -> None:
    """Search run artifacts and print top snippets with source provenance."""
    results = search_artifacts(
        run_dir,
        query,
        top_k=top_k,
        include_operational=include_operational,
    )
    matches = results.get("matches", [])
    print_line(f"Run: {run_dir}")
    print_line(f"Query: {query}")
    print_line(f"Chunks searched: {results.get('chunk_count', 0)}")
    for skipped in results.get("skipped_files", []):
        print_line(f"Not searched: {skipped['path']} ({skipped['reason']}, {_format_bytes(skipped['bytes'])})")
    print_line(f"Matches: {len(matches)}")
    print_line(f"Operational metadata included: {include_operational}")
    print_line(f"Results: {run_dir / 'artifact_search_results.json'}")
    for match in matches:
        path = match.get("path", "")
        line_start = match.get("line_start", "")
        line_end = match.get("line_end", "")
        score = match.get("score", "")
        snippet = str(match.get("snippet", "")).strip()
        print_line(f"- {path}:{line_start}-{line_end} score={score}")
        if snippet:
            print_line(f"  {snippet}")


def _artifact_rows(index: dict[str, object]) -> list[dict[str, object]]:
    rows = index.get("artifacts", [])
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _count_by(rows: list[dict[str, object]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field)
        key = str(value) if value else "root"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _format_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"


def _format_status_number(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        sign = "+" if number > 0 else ""
        return f"{sign}{number:.6g}"
    return str(value)


def _stage_status(item: dict[str, object]) -> str:
    """Return a readable stage status for old and new manifests."""
    value = item.get("status")
    if isinstance(value, str) and value:
        return value
    return "done" if item.get("completed") else "pending"


def _delivery_version_lines(root: Path, versions: list[dict] | None = None) -> list[str]:
    """Discover retained bytes from original attempts, without changing state."""
    from simple_ar.tools.delivery_versions import DeliveryVersions

    if versions is None:
        if not (root / 'session_manifest.json').is_file():
            return []
        try:
            versions = DeliveryVersions(root).catalog()
        except (OSError, ValueError, KeyError, TypeError):
            return ['Retained byte versions unavailable / 保留字节版本不可用；请检查原始交付回执。']
    retained = sum(version['status'] == 'retained' for version in versions)
    lines = [f'Retained byte versions / 已保留字节版本: {retained}']
    if not retained and (root / 'delivery.json').is_file():
        lines.append('Historical delivery bytes unavailable / 历史交付缺少可还原字节；不会从当前文件补建。')
    for version in versions:
        number = version['version']
        if version['status'] != 'retained':
            lines.append(f"- version={number}; request={version['request_count']}; "
                f"unavailable / 历史字节不可还原; observation={version['observation']}")
            continue
        lines.append(f"- version={number}; request={version['request_count']}; files={version['artifact_count']}; "
            f"binding={version['binding']}; receipt={version['receipt']}")
        if version['method_paths']:
            lines.append('  Project methods / 项目方法: ' + ', '.join(version['method_paths']))
    if retained:
        lines.append('Reuse: simple-ar start --from-session SESSION --reuse artifacts --reuse-version N --goal "Your changes"; '
            'new work snapshots the selected version. / 新任务按原布局保全所选历史版本，不续行旧任务。')
        lines.append('Retained bytes do not establish current completion or scientific acceptance. / '
            '保留字节不代表当前请求完成或科研验收。')
    return lines


def _render_native_result(app: Any, status: dict[str, Any], *, task_view: dict | None = None) -> None:
    """Read the existing owners; never commit, reconcile or certify a result."""
    from simple_ar.core.console import make_console
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    root = app.session.store.root
    def location(path: Path) -> str:
        try:
            return str(path.relative_to(root))
        except ValueError:
            return str(path)

    if task_view is None:
        task_view = status.get('task_view')
    if task_view is not None:
        status = {**status, **task_view['manifest']}
    pending = (task_view.get('pending') if task_view is not None else app.state.get('pending')) or {}
    reason = str(status.get("status_reason") or "")
    # Exception messages can contain provider URLs, headers or credentials.
    if reason.startswith("BudgetExceededError:") or (
        reason.startswith("RuntimeError: Operation failed; inspect saved diagnostics: LLMError: ")
        and "LLM budget prevented provider attempt " in reason
    ):
        reason = "Insufficient remaining budget before dispatch / 发送前剩余额度不足"
    elif reason.startswith("BudgetUnknownError:"):
        reason = "Prior usage is unknown / 先前用量未核清"
    elif reason.startswith("Process requires explicit approval"):
        reason = "Process awaits explicit approval / 执行等待明确授权"
    elif reason == _TERMINAL_STOP_REASON:
        reason = "Paused at a work boundary / 已在工作边界暂停"
    elif reason.startswith(('LLM', 'Connect', 'Timeout')):
        reason = "Model connection or response failed / 模型连接或响应失败；已保存调用记录"
    elif ":" in reason:
        reason = "Saved stop detail withheld; inspect session_manifest.json locally"
    elif reason not in {
        "", "Delivery manifest saved; scientific quality requires independent checking",
        "Model returned prose without a delivery or question; inspect the saved reply before continuing",
        "Shared model request allowance exhausted, including professional calls; existing artifacts are retained, not marked complete",
    }:
        reason = "See saved status_reason in session_manifest.json / 原因见已保存状态记录"
    lines = [f"Status: {status['status']}", f"Reason / 原因: {reason or 'not recorded'}",
             f"Saved session / 会话: {root}", "Locations below are relative to this session unless absolute.",
             f"Workspace / 工作区: {location(app.workspace.workspace)}", "Attempts / 尝试记录: attempts"]
    if task_view is not None:
        if task_view['requests']:
            lines.append('Request / 当前请求: ' + task_view['requests'][-1])
        for row in task_view.get('activity', [])[:6]:
            lines.append(f"Recent action / 最近动作: {row.get('tool', 'action')} | "
                         f"{row.get('status', 'unavailable')} | {row['record']}")
        if task_view.get('approval_unavailable'):
            lines.append(task_view['approval_unavailable'])
        approval = task_view.get('approval')
        if approval:
            import json
            lines.append('Pending approval / 待审批: ' + json.dumps(approval, ensure_ascii=False))
    if pending.get("kind") in {"model", "tool"}:
        lines.append(f"Pending action / 待处理动作: {pending['kind']}")
    attempt = pending.get("attempt") or status.get("current_attempt")
    if isinstance(attempt, str) and attempt not in {".", ".."} and Path(attempt).name == attempt and "\\" not in attempt:
        lines.append(f"Selected attempt / 当前尝试: attempts/{attempt}")
    if status['status'] not in {"completed", "partial"}:
        lines.append("Current request has no confirmed delivery / 当前请求未确认交付")
        lines.append(f'Continue / 继续: simple-ar start --resume-task "{root}"')
        if status.get('status_reason') != _TERMINAL_STOP_REASON:
            lines.append('Resolve the stop condition first; approvals, unavailable services and exhausted budgets require action. / 先处理授权、服务或额度等停止条件，再继续原任务。')
        retained = status.get('retained_work') or {}
        last = retained.get('last_operation') or {}
        failure = {'image_reference': 'Image references are missing or outside this manuscript / 正文图片缺失或引用超出稿件目录',
            'review_incomplete': 'Independent inspection did not complete / 独立审阅未完成，有效意见如已保存可继续使用',
            'composition_incomplete': 'Document preparation did not complete / 文稿处理未完成，已有候选如已保存可继续修改'}.get(last.get('failure_kind'))
        if failure:
            lines.append(f'Issue / 问题: {failure}')
        for locator in retained.get('artifacts', [])[:6]:
            try:
                path = app.workspace.resolve(locator)
                if path.is_file() and path.is_relative_to(app.workspace.workspace):
                    lines.append(f'Retained work, not accepted / 已保留，未验收: {location(path)}')
            except (OSError, ValueError, TypeError):
                continue
    else:
        lines.append("Session status is terminal; partial retains unresolved work. Delivery binding and quality are not certified.")
    if app.session.store.exists("delivery.json"):
        lines.append("Saved delivery record / 已保存交付记录: delivery.json")
        lines.append("Request/version binding not checked here; this record may precede the current feedback.")
        try:
            delivery = app.session.store.read_json("delivery.json")
            if task_view is not None:
                lines.append('Recorded summary / 交付说明: ' + delivery.get('summary', ''))
            artifacts = delivery["artifacts"]
            if not isinstance(artifacts, list):
                raise ValueError("Invalid saved artifact list")
            for unresolved in delivery.get('unresolved', []):
                lines.append(f"Unfinished / 未完成: {unresolved}")
            for limitation in delivery.get('limitations', []):
                lines.append(f"Delivery limitation / 交付局限: {limitation}")
            for locator in artifacts[:4]:
                try:
                    path = app.workspace.resolve(locator)
                    lines.append(f"Recorded artifact / 记录位置: {location(path)}" +
                                 ("" if path.is_file() else " (missing or not a file)"))
                except (OSError, ValueError, TypeError):
                    lines.append("Recorded artifact location unavailable or outside allowed paths")
            if len(artifacts) > 4:
                lines.append(f"Other recorded locations: {len(artifacts) - 4}; see delivery.json")
        except (OSError, ValueError, KeyError, TypeError):
            lines.append("Saved delivery record unreadable; no delivery inferred")
        if status['status'] in {'completed', 'partial'}:
            try:
                from simple_ar.tools.delivery_review import recorded_delivery_review
                from simple_ar.tools.workspace import RetainedDelivery
                review = recorded_delivery_review(RetainedDelivery(root))
                if review:
                    lines.append('Recorded independent model opinion / 独立模型检查记录；不证明科研正确性:')
                    lines.extend(f"- {row['message']}" for row in review['findings'])
                    lines.extend(f"- {limit}" for limit in review['limitations'])
                    lines.append(f"Opinion record / 意见记录: {review['record']}")
                else:
                    lines.append('No independent opinion available in this view / 当前视图没有可展示的独立检查意见，不应据此认为已核查')
            except (OSError, ValueError, KeyError, TypeError):
                lines.append('Independent opinion unavailable / 检查意见记录不可核对')
    else:
        lines.append("No saved delivery record / 无已保存交付记录")

    if status.get('delivery_versions_error'):
        lines.append('Retained byte versions unavailable / 保留字节版本不可用；请检查原始交付回执。')
    else:
        lines.extend(_delivery_version_lines(root, status.get('delivery_versions')))
    budget = task_view['resources'] if task_view is not None else app.ledger.snapshot()
    entries = app.ledger.entries
    table = Table("Resource", "Cumulative accounting / 累计账目", "Remaining / 剩余", expand=True)
    for dimension, remaining in budget["remaining"].items():
        settled = sum(entry.actual.get(dimension, 0) for entry in entries if entry.status == "settled")
        held = sum(entry.reserved.get(dimension, 0) for entry in entries if entry.status == "reserved")
        unknown = [entry for entry in entries if entry.status == "unknown" and dimension in entry.reserved]
        accounting = f"settled={settled}; reserved={held}"
        if unknown:
            accounting += (f"; unreconciled={len(unknown)} "
                           f"(known={sum(entry.actual.get(dimension, 0) for entry in unknown)}, "
                           f"reservation={sum(entry.reserved[dimension] for entry in unknown)})")
        available = str(remaining) if remaining is not None else (
            "unknown" if dimension in budget["unknown_dimensions"] else "no finite limit")
        table.add_row(Text(dimension), Text(accounting), Text(available))
    note = Text("Settled includes recorded estimates. Unreconciled reservations are not measured usage. "
                "Remaining follows the saved ledger allowance; it does not promise another request will fit. "
                "Saved artifacts and successful tools do not establish acceptance.")
    make_console().print(Panel(Group(Text("\n".join(lines)), table, note), title="Task result / 任务读出"))


_TERMINAL_STOP_REASON = 'Caller requested boundary stop'


def _run_terminal_task(run: Callable[..., dict[str, Any]], *args, approve=None, **options) -> dict[str, Any]:
    """Translate terminal interrupt into the shared application's pause request."""
    if current_thread() is not main_thread():
        if approve is not None:
            options['approve'] = approve
        return run(*args, **options)
    requested = False
    waiting_for_approval = False

    def request_stop(_signum, _frame):
        nonlocal requested
        requested = True
        if waiting_for_approval:
            raise LLMCallStopped()

    def prompt(call, token):
        nonlocal waiting_for_approval
        waiting_for_approval = True
        try:
            if requested:
                raise LLMCallStopped()
            return approve(call, token)
        finally:
            waiting_for_approval = False

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        if approve is not None:
            options['approve'] = prompt
        return run(*args, should_stop=lambda: requested, **options)
    finally:
        signal.signal(signal.SIGINT, previous)


def _run_native_conversation(app: Any, initial_run: Callable[[], dict[str, Any]],
                             approve: Callable[[dict[str, Any], str], bool]) -> None:
    """Terminal input adapter; the existing app owns feedback, stops and recovery."""
    from simple_ar.cli.task_options import resume_command
    from simple_ar.core.locking import SessionBusyError

    def retained() -> None:
        print_line(f'Task retained / 任务已保留: {app.session.store.root}')
        print_line(f'Resume: {resume_command(app.session.store.root)} --interactive')

    def display(snapshot: dict | None = None) -> None:
        try:
            if snapshot is None or 'task_view' not in snapshot:
                snapshot = app.result_snapshot()
            _render_native_result(app, snapshot, task_view=snapshot['task_view'])
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, LLMError) as exc:
            print_line(f'Task view unavailable: {type(exc).__name__}: {str(exc)[:300]}')
            print_line(f'Diagnostics: {app.session.store.root / "session_manifest.json"}; {app.session.store.root / "attempts"}')

    def run(feedback: str | None = None, *, require_review: bool = False) -> bool:
        submission_id = uuid4().hex if feedback is not None else None
        snapshot = None
        try:
            if feedback is None:
                snapshot = initial_run()
            else:
                snapshot = _run_terminal_task(app.run, feedback=feedback, submission_id=submission_id,
                    approve=approve, progress=print_line,
                    **({'independent_review_required': True} if require_review else {}))
        except SessionBusyError:
            if submission_id is None:
                raise
            print_line(f'Feedback {submission_id} received. The running task will adopt it at a safe boundary.')
        except (EOFError, KeyboardInterrupt):
            return False
        except (OSError, ValueError, RuntimeError, LLMError) as exc:
            # Never retry, replenish resources, or resubmit this input. The app
            # may already have persisted feedback or started an operation.
            print_line(f'Task action stopped: {type(exc).__name__}: {str(exc)[:300]}')
            print_line(f'Diagnostics: {app.session.store.root / "session_manifest.json"}; {app.session.store.root / "attempts"}')
            retained()
        display(snapshot)
        return True

    try:
        if not run():
            return
        print_line('Feedback / 反馈; /review FEEDBACK requires independent review; /status reads saved facts; /exit leaves this task saved.')
        while True:
            feedback = input('Task / 任务> ')
            command = feedback.strip()
            if not command:
                continue
            if command == '/exit':
                return
            if command == '/status':
                display()
                continue
            if command == '/review' or command.startswith('/review '):
                revision = command[len('/review'):].strip()
                if not revision:
                    print_line('Use /review followed by feedback; no feedback submitted.')
                    continue
                if not run(revision, require_review=True):
                    return
                continue
            if command.startswith('/'):
                print_line('Unknown command; use /status or /exit. No feedback submitted.')
                continue
            if not run(feedback):
                return
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        retained()


def _run_native_start(args: argparse.Namespace) -> None:
    from simple_ar.app.session_roots import new_research_session_root
    from simple_ar.app.tool_application import ToolApplication
    from simple_ar.cli.start import native_creation_options, select_start_model
    from simple_ar.tools.workspace import resolve_source_mount
    from simple_ar.core.console import make_console, print_line
    from simple_ar.core.locking import SessionBusyError
    from rich.panel import Panel
    from rich.text import Text
    interactive = sys.stdin.isatty()
    app = None
    try:
        from simple_ar.cli.task_options import validate_interactive_options
        validate_interactive_options(args)
        conversation = getattr(args, 'interactive', False)
        submission_id = getattr(args, 'submission_id', None)
        if submission_id is not None and (not args.resume_task or not submission_id.strip()
                or args.supply_task or args.switch_model or args.prepare_only):
            raise ValueError('--submission-id requires --resume-task and a separate feedback operation')
        if conversation and any(getattr(args, name, None) is not None for name in
                                ('_code_task', '_execution_contract', '_research_policy')):
            raise ValueError('--interactive supports ordinary native tasks only, not structured contracts')
        decision = {name: getattr(args, name, None) for name in
                    ('decision_id', 'decision_response', 'decision_guidance')}
        if any(value is not None for value in decision.values()):
            if not args.resume_task or not decision['decision_id'] or not decision['decision_response']:
                raise ValueError('A design reply requires --resume-task, --decision-id and --decision-response')
        else:
            decision = None
        continuation = _continuation_parameters(args)
        if args.supply_task and not args.resume_task:
            raise ValueError('--supply-task requires --resume-task and its retained task')
        if getattr(args, 'remember_execution', False) and not (args.resume_task and args.approve_execution):
            raise ValueError('--remember-execution requires --resume-task and --approve-execution')
        if args.approve_execution and not args.resume_task:
            raise ValueError("--approve-execution requires --resume-task and its saved pending command")
        allowances = continuation["authorize_remaining"]
        additional_attempts = continuation["additional_attempts"]
        additional_no_progress = continuation["additional_no_progress"]
        if min(additional_attempts, additional_no_progress) < 0:
            raise ValueError('Additional attempt allowances must be non-negative')
        capacity = bool(allowances or additional_attempts or additional_no_progress)
        authorization_id = continuation["authorization_id"] or ""
        authorization_reason = continuation["authorization_reason"] or ""
        switching = bool(args.switch_model)
        if switching or args.switch_id is not None or args.switch_reason is not None:
            if (not switching or not args.resume_task or not (args.switch_id or '').strip()
                    or not (args.switch_reason or '').strip()):
                raise ValueError('--switch-model requires --resume-task, --switch-id and --switch-reason')
            if not args.switch_model.startswith(('profile:', 'route:')):
                raise ValueError('--switch-model requires a named profile:NAME or route:NAME')
            if (args.request or args.goal or args.retry_model or args.supply_task or decision
                    or args.approve_execution or args.source or args.allow_execution or args.allow_network
                    or args.allow_vision or capacity or authorization_id or authorization_reason
                    or 'model' in getattr(args, '_explicit_start_destinations', set())):
                raise ValueError('Use --switch-model separately from feedback, retry, supply, --model or authorization')
        if args.retry_model:
            if not args.resume_task:
                raise ValueError('--retry-model requires --resume-task')
            if (args.request or args.goal or args.approve_execution or args.source
                    or args.allow_execution or args.allow_network or args.allow_vision
                    or capacity or authorization_id or authorization_reason):
                raise ValueError('Use --retry-model separately from feedback, access, resource or tool authorization')
        if capacity or continuation["authorization_id"] is not None or continuation["authorization_reason"] is not None:
            if not args.resume_task:
                raise ValueError("Resource authorization requires --resume-task")
            if not capacity:
                raise ValueError("--authorization-id/--authorization-reason require an explicit allowance")
            if not authorization_id.strip() or not authorization_reason.strip():
                raise ValueError("Continuation allowances require --authorization-id and --authorization-reason")
            if args.approve_execution:
                raise ValueError("Confirm resource authorization separately from --approve-execution")
        if args.resume_task:
            from simple_ar.cli.task_options import validate_resume_options
            validate_resume_options(args)
            app = ToolApplication.load(args.resume_task.resolve())
            if conversation and (app.state['config'].get('control_protocol') != 'operations.v1'
                    or any(app.state['config'].get(name) is not None for name in
                           ('code_task', 'execution_contract', 'research_policy', 'report_protocol'))):
                raise ValueError('--interactive supports ordinary native tasks only, not saved structured contracts')
            if args.supply_task:
                if (args.request or args.goal or args.retry_model or args.approve_execution or decision
                        or args.source or args.allow_execution or args.allow_network or args.allow_vision
                        or capacity or authorization_id or authorization_reason
                        or 'model' in getattr(args, '_explicit_start_destinations', set())):
                    raise ValueError('Use --supply-task separately; declare new contracts and resources in its TOML')
                import tomllib
                path = args.supply_task.resolve()
                with path.open('rb') as stream:
                    supply = tomllib.load(stream)
                if (set(supply) - {'supply_id', 'reason', 'changes', 'resource_allowances', 'replace_existing',
                                   'additional_attempts', 'additional_no_progress'}
                        or not {'supply_id', 'reason', 'changes'}.issubset(supply)
                        or not isinstance(supply['changes'], dict)):
                    raise ValueError('Supply TOML requires supply_id, reason and [changes]; optional [resource_allowances]')
                sources = supply['changes'].get('sources', {})
                if not isinstance(sources, dict) or any(not isinstance(value, str) or not value.strip()
                        for value in sources.values()):
                    raise ValueError('Supply [changes.sources] must map aliases to read-only paths')
                if sources:
                    supply['changes']['sources'] = {name: str((path.parent / value).resolve())
                        for name, value in sources.items()}
                make_console().print(Panel(Text(
                    f"Supply ID: {supply['supply_id']}\nReason: {supply['reason']}\n"
                    f"Contract fields: {', '.join(sorted(supply['changes']))}\n"
                    f"Replace existing contracts: {supply.get('replace_existing', False)}\n"
                    f"New remaining allowances: {supply.get('resource_allowances', {})}\n"
                    f"Additional attempts: {supply.get('additional_attempts', 0)}; "
                    f"additional no-progress: {supply.get('additional_no_progress', 0)}\n"
                    "Original records and usage remain. Replacement retires old authority, not old evidence; "
                    "no model call or process runs, and commands still require separate approval."),
                    title='Confirm task supply / 确认补齐任务条件'))
                if args.prepare_only or (not args.yes and (not interactive or input(
                        'Record these contracts / 保存这些条件? [y/N]: ').strip().lower() not in {'y', 'yes'})):
                    return
                app.supply(**supply)
                _render_native_result(app, app.session.status_snapshot())
                print_line(f'Supply saved without execution. Continue: simple-ar start --resume-task "{app.session.store.root}"')
                return
            feedback = args.request or args.goal or ""
            explicit_model = 'model' in getattr(args, '_explicit_start_destinations', set())
            replacement_model = args.switch_model or (args.model if args.retry_model and explicit_model else '')
            if explicit_model and not args.retry_model and args.model != app.state['config']['model']:
                raise ValueError('Resume retains its saved model. Change a terminated failed decision '
                                 'with --retry-model --model profile:NAME, or record only a connection change '
                                 'with --switch-model profile:NAME --switch-id ID --switch-reason REASON')
            if replacement_model:
                target = LLMClient.from_env(replacement_model).connection_binding()
                settings = target.get('settings') or {}
                print_line(f"Explicit text connection replacement: {app.state['config']['model']} → "
                           f"{replacement_model}; endpoint={settings.get('base_url')}; model={settings.get('model')}. "
                           "Resolved code/writing routes are also pinned; vision, access and budgets remain unchanged.")
                for purpose in ('code', 'writing'):
                    route = target.get(f'{purpose}_route') or {}
                    connection = route.get('connection') or settings
                    print_line(f"{purpose}: {route.get('profile') or replacement_model}; "
                               f"endpoint={connection.get('base_url')}; model={connection.get('model')}")
                if switching:
                    print_line(f"Switch ID: {args.switch_id}; reason: {args.switch_reason}. "
                               "Records the connection only; no model request, process or feedback runs. "
                               "Saved delivery remains valid. Submit new feedback separately.")
                if not args.prepare_only and not args.yes:
                    if not interactive or input('Confirm connection replacement / 确认连接替换? [y/N]: ').strip().lower() not in {'y', 'yes'}:
                        return
            if switching:
                if args.prepare_only:
                    return
                app.switch_model(args.switch_model, switch_id=args.switch_id, reason=args.switch_reason,
                                 expected_connection=target)
                _render_native_result(app, app.session.status_snapshot())
                print_line(f"Switch record retained; active text connection: {app.state['config']['model']}. "
                           "No execution. Continue the original task separately; replaying an old id does not revert later changes.")
                return
            if args.retry_model:
                print_line("Explicit model continuation: one new decision attempt, not a task or tool replay. "
                           "Old failures and unknown charges remain; original budgets, access and output cap are retained.")
            if capacity:
                entries = app.ledger.entries
                recorded_usage = {dimension: sum(entry.actual.get(dimension, 0) for entry in entries
                    if entry.status in {"settled", "unknown"}) for dimension in allowances}
                make_console().print(Panel(Text(
                    f"Authorization ID: {authorization_id}\nReason: {authorization_reason}\n"
                    f"Recorded cumulative usage (requested dimensions): {recorded_usage}\n"
                    f"Unknown usage dimensions: {app.ledger.unknown_dimensions()}\n"
                    f"New remaining allowance: {allowances}\n"
                    f"Additional attempts: {additional_attempts}; additional no-progress: {additional_no_progress}\n"
                    "Original cumulative usage is not cleared / 原累计用量不清零. "
                    "New remaining is not a total historical limit / 新 remaining 不是总历史限额.\n"
                    "Reusing the same authorization ID does not replenish spent capacity. "
                    "Model binding and permissions remain unchanged; commands still need exact approval. "
                    "Closed text calls with finite retained charges stay unknown in the original ledger. "
                    "In-flight, unbounded or unconfirmed side-effect operations require reconciliation."),
                    title="Confirm resource continuation / 确认资源续接"))
                if not args.prepare_only and not args.yes:
                    if not interactive or input("Authorize these resources / 授权这些资源? [y/N]: ").strip().lower() not in {"y", "yes"}:
                        return
            if args.source or args.allow_execution or args.allow_network or args.allow_vision:
                if args.approve_execution:
                    raise ValueError('Confirm added access separately from approving an existing command')
                make_console().print(Panel(f'Additional read-only materials: {args.source}\n'
                    f'Process proposals: {args.allow_execution}; public sources: {args.allow_network}; '
                    f'vision: {args.allow_vision}\nExisting access remains; these access grants do not increase budgets. '
                    'Commands still require individual approval; execution is not sandboxed.', title='Confirm added access'))
                if not args.prepare_only and not args.yes:
                    if not interactive or input('Grant this access / 授权这些访问? [y/N]: ').strip().lower() not in {'y', 'yes'}:
                        return
        else:
            if args.goal and args.request:
                raise ValueError("Use the positional request for a native task, not both request and --goal")
            if not args.request:
                args.request = args.goal or (input("Goal / 想完成什么任务: ").strip() if interactive else "")
                args.goal = None
            if not args.request:
                raise ValueError("Provide a task request or --goal; use --source PATH for read-only materials")
            args.source = [resolve_source_mount(path) for path in args.source]
            select_start_model(args, interactive=interactive)
            creation_options = native_creation_options(args)

            def show_creation(prepared: tuple) -> None:
                config, _, limits, _ = prepared
                file_fields = ', '.join(getattr(args, '_task_config_fields', []))
                make_console().print(Panel(Text(f"Goal: {args.request}\nRead-only sources: {args.source}\n"
                    f"Model: {config['model']}\nAllowance: {limits['llm_requests']} requests / {limits['total_tokens']} tokens\n"
                    f"Process allowance: {limits['process_invocations']} invocations / {limits['process_wall_seconds']} wall seconds\n"
                    f"Task settings: {getattr(args, '_task_config_source', 'command line / defaults')}"
                    f"{f' ({file_fields})' if file_fields else ''}\n"
                    f"Public sources: {config['allow_network']}; rendered-image inspection: {config['allow_vision']}\n"
                    f"Image submissions: {limits['image_requests']}; independent review: {config.get('independent_review_required', False)}\n"
                    f"Process proposals: {config['allow_execution']}; workspace execution: {config.get('sandbox_execution', False)}"),
                    title="Task / 任务"))

            verify_creation = show_creation
            if not args.yes and not args.prepare_only:
                preview_root = Path(args.output_root).expanduser() / ('.preview-' + uuid4().hex)
                prepared = ToolApplication.preview_creation(preview_root, args.request, **creation_options)
                show_creation(prepared)
                if not interactive or input("Start / 开始? [y/N]: ").strip().lower() not in {"y", "yes"}:
                    return
                approved = ToolApplication.creation_binding(prepared)

                def verify_confirmed_creation(actual: tuple) -> None:
                    if ToolApplication.creation_binding(actual) != approved:
                        raise ValueError('Effective task configuration changed after preview')

                verify_creation = verify_confirmed_creation
            root = getattr(args, '_native_root', None) or new_research_session_root(args.output_root, args.request)
            app = ToolApplication.create(root, args.request, verify_creation=verify_creation, **creation_options)
            feedback = ""
        print_line(f"Task session: {app.session.store.root}")
        print_line(f"Saved process allowance (including search backends): {app.ledger.limits.get('process_invocations')}; "
                   f"wall seconds: {app.ledger.limits.get('process_wall_seconds')}. Resume does not reset it.")
        from simple_ar.cli.task_options import resume_command
        print_line(f'Resume: {resume_command(app.session.store.root)}')
        print_line('Append a request for feedback, or --approve-execution TOKEN for the exact pending command.')
        if args.prepare_only:
            return

        def approve(call: dict[str, Any], token: str):
            print_line(f"Execution proposal ({call['function']['name']}): {call['function']['arguments']}")
            if call['function']['name'] == 'execute_implementation':
                import json
                projection = app.execution_approval(call)
                binding = projection['binding']
                if projection['token'] != token:
                    raise ValueError('Prepared execution changed since approval was requested; inspect the current proposal')
                print_line('Prepared command: ' + json.dumps({'argv': binding['argv'],
                    'cwd': binding['cwd'], 'timeout_sec': binding['plan']['timeout_sec']},
                    ensure_ascii=False))
            if call['function']['name'] in {'prepare_code_task', 'implement_code_task'}:
                print_line(f"Frozen CodeTask contract: {app.implementation.exact.model_dump()}")
                exact = app.implementation.exact
                repairs = exact.preparation.max_repairs if exact.preparation is not None else 1
                print_line('Approval binds the project inventory, protected validator files and process environment. '
                    + ('Preparation only; implementation and each formal measurement require separate approval.'
                       if call['function']['name'] == 'prepare_code_task' else f'Includes at most {repairs} technical repairs.'))
            print_line("Execution is not sandboxed. Inspect the generated code before approving.")
            if not interactive:
                return False
            answer = input("Execute, defer, or reject / 执行、暂缓或拒绝? [y/N/r]: ").strip().lower()
            if answer == 'r':
                from simple_ar.app.tool_application import ExecutionRejection
                reason = input('Reason for rejection / 拒绝原因: ').strip()
                return ExecutionRejection(reason) if reason else False
            return answer in {"y", "yes"}

        def run_initial():
            feedback_id = submission_id or (uuid4().hex if conversation and feedback else None)
            finish_declared_work = (not conversation and bool(args.resume_task)
                and app.state['config'].get('model') is None and not feedback and feedback_id is None
                and not args.retry_model and not capacity
                and not (args.source or args.allow_execution or args.allow_network or args.allow_vision))
            try:
                return _run_terminal_task(app.run, feedback=feedback, approved_execution=args.approve_execution,
                    **({'submission_id': feedback_id} if feedback_id is not None else {}),
                    **({'independent_review_required': args.independent_review_required}
                       if args.resume_task and getattr(args, 'independent_review_required', None) is not None else {}),
                    remember_execution=getattr(args, 'remember_execution', False),
                    retry_model=args.retry_model,
                    retry_model_selector=replacement_model if args.resume_task else '',
                    read_only_sources=tuple(args.source) if args.resume_task else (),
                    allow_execution=args.allow_execution if args.resume_task else False,
                    allow_network=args.allow_network if args.resume_task else False,
                    allow_vision=args.allow_vision if args.resume_task else False,
                    resource_allowances=allowances or None,
                    additional_attempts=additional_attempts if 'additional_attempts' in getattr(args, '_explicit_start_destinations', ()) else None,
                    additional_no_progress=additional_no_progress if 'additional_no_progress' in getattr(args, '_explicit_start_destinations', ()) else None,
                    authorization_id=authorization_id, authorization_reason=authorization_reason,
                    decision=decision, approve=approve if interactive else None, progress=print_line,
                    **({'finish_declared_work': True} if finish_declared_work else {}))
            except SessionBusyError:
                if feedback_id is None:
                    raise
                receipt = ToolApplication.feedback_status(app.session.store.root, submission_id=feedback_id)
                print_line(f'Feedback {feedback_id}: {receipt["status"]}. The running task owns adoption.')
                return app.result_snapshot()
        if conversation:
            _run_native_conversation(app, run_initial, approve)
        else:
            _render_native_result(app, run_initial())
    except (EOFError, KeyboardInterrupt):
        if not getattr(args, 'interactive', False) or app is None:
            raise
        from simple_ar.cli.task_options import resume_command
        print_line(f'Task retained: {app.session.store.root}\nResume: {resume_command(app.session.store.root)} --interactive')
    except (OSError, ValueError, RuntimeError, LLMError) as exc:
        if getattr(args, 'interactive', False) and app is not None:
            from simple_ar.cli.task_options import resume_command
            print_line(f'Task retained: {app.session.store.root}\nResume: {resume_command(app.session.store.root)} --interactive')
        raise SystemExit(f"Native task stopped; saved operations are not replayed: {exc}") from exc


if __name__ == "__main__":
    main()
