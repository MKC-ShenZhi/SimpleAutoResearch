from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Sequence

from simple_ar.core.artifacts import read_json, read_text
from simple_ar.cli.research_view import ResearchConsole, report_audit_line
from simple_ar.cli.code_task_view import (
    confirm_next_step,
    confirm_review_gate,
    render_execute_header,
    render_execute_message,
    render_execute_result,
    render_init_result,
    render_review_gate,
    render_step_preview,
)
from simple_ar.code_task import (
    analyze_code_task_failure,
    apply_patch_edits,
    build_code_task_context_pack,
    build_code_task_repo_map,
    create_code_task_batch,
    execute_code_task,
    generate_code_task_work_plan,
    generate_patch_plan,
    initialize_code_task,
    locate_code_task_context,
    probe_code_task_environment,
    propose_patch_edits,
    propose_repair_edits,
    record_plan_decision,
    run_code_task_baseline,
    run_code_task_benchmark,
    validate_code_task,
    PatchValidationError,
    WorkspaceModeError,
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
from simple_ar.code_task.runtime.config import EXECUTE_STEPS
from simple_ar.core.console import print_line
from simple_ar.integrations.llm import LLMClient, LLMError
from simple_ar.retrieval.index import build_artifact_index
from simple_ar.retrieval.search import search_artifacts
from simple_ar.cli.parser import build_parser
from simple_ar.tools.cli import call_tool, print_tool_schema, serve_mcp


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
            result = export_acm_report(args.report_dir, args.output, title=args.title, compile_pdf=args.compile)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Report export failed: {exc}") from exc
        print_line(f"ACM project: {args.output.resolve()}; status: {result['status']}")
        if result.get("compile_note"):
            print_line(result["compile_note"])
        if args.compile and not result["compiled"]:
            raise SystemExit(1)
        return
    if args.command == "image-review":
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.code_task.reviewing import run_visual_review
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
            deliveries = session_materials(args.session)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Cannot read deliveries: {exc}") from exc
        for name, paths in deliveries.items():
            print_line(f"{name}: " + ", ".join(str(path) for path in paths))
        if not deliveries:
            print_line("No current reusable delivery. The session may be incomplete.")
        if "code_project" in deliveries:
            print_line("Revise code/figure: simple-ar start --from-session SESSION --reuse code_project --goal 'Your changes'")
        if session.manifest.transition_recipe == ToolApplication.RECIPE:
            print_line(f"Status: {session.manifest.status}")
            if session.manifest.status in {"completed", "partial"}:
                delivery = session.store.read_json("delivery.json")
                print_line(delivery["summary"])
                for limitation in delivery.get("unresolved", []):
                    print_line(f"Unresolved: {limitation}")
            print_line(f"Continue: simple-ar start --resume-task '{args.session}' 'Your changes'")
            return
        print_line("Continue: simple-ar start --kind writing --from-session SESSION --reuse NAME --goal 'Your changes'")
        return
    if args.command == "start":
        args._explicit_start_destinations = _explicit_session_option_destinations(parser, arguments, set(), command="start")
        from simple_ar.cli.start import prepare_start
        from simple_ar.core.locking import SessionLockError
        try:
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
        if args.code_task_command == "work-plan":
            _print_code_task_work_plan(args)
            return
        if args.code_task_command == "batch":
            _print_code_task_batch(args)
            return
        if args.code_task_command == "plan":
            _print_code_task_plan(args)
            return
        if args.code_task_command == "decide-plan":
            _print_code_task_decision(args)
            return
        if args.code_task_command == "propose-edits":
            _print_code_task_propose_edits(args)
            return
        if args.code_task_command == "apply-edits":
            _print_code_task_apply_edits(args)
            return
        if args.code_task_command == "validate":
            _print_code_task_validate(args)
            return
        if args.code_task_command == "baseline":
            _print_code_task_baseline(args)
            return
        if args.code_task_command == "run":
            _print_code_task_run(args)
            return
        if args.code_task_command == "analyze-failure":
            _print_code_task_analyze_failure(args)
            return
        if args.code_task_command == "repair":
            _print_code_task_repair(args)
            return
        if args.code_task_command == "execute":
            _print_code_task_execute(args)
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

    from simple_ar.app.research_application import (
        ResearchApplicationError,
        ResearchApplicationServices,
        create_session,
    )
    from simple_ar.app.research_intake import ResearchInputError
    from simple_ar.app.session_roots import new_research_session_root
    from simple_ar.research.workflow_contracts import ResearchBrief

    saved_outputs = ()
    if getattr(args, "session_root", None) is not None:
        manifest = Path(args.session_root) / 'session_manifest.json'
        if manifest.is_file() and read_json(manifest).get('transition_recipe') == 'native-tools-v1':
            from simple_ar.cli.start import resume_structured_task
            resume_structured_task(args)
            return
        from simple_ar.app.research_application import load_session

        try:
            saved_app = load_session(args.session_root)
            saved_outputs = saved_app.brief.requested_outputs
            if not getattr(args, "topic", None):
                args.topic = saved_app.brief.objective
            if getattr(args, "task_kind", "auto") == "auto" and saved_app.task_kind == "data_analysis":
                args.task_kind = "data_analysis"
        except (OSError, RuntimeError, ValueError) as exc:
            raise SystemExit(f"Could not restore the saved session goal: {exc}") from exc

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
        ("report" in (outputs if outputs is not None else saved_outputs) and not args.no_report))
    llm_client = None if analysis_only else _optional_research_llm_client(args.model, "research session", max_output_tokens=getattr(args, "max_output_tokens", None))
    review_model = (getattr(args, "feasibility_review_model", None) or "").strip()
    if review_model and llm_client is None:
        raise SystemExit("--feasibility-review-model requires --model for research design.")
    feasibility_client = None if task_kind == "data_analysis" else _optional_research_llm_client(
        review_model or None, "feasibility review", max_output_tokens=getattr(args, "max_output_tokens", None),
    )
    if task_kind == "data_analysis":
        report_requested = not analysis_only
    else:
        report_requested = bool(args.with_report or (task_kind != "bug_fix" and
                                (task_kind == "writing" or (llm_client is not None and not args.no_report))))
    if outputs is not None:
        report_requested = "report" in outputs
    if report_requested and llm_client is None:
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
            "result_schema": _merge_result_schemas(
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
            "locator": str(Path(path)),
            "kind": "file",
            "role": "paper",
            "mutability": "read_only",
            "allowed_uses": ["read", "reference"],
        }
        for path in args.local_document
    ]
    asset_requests.extend({"locator": str(Path(path)), "kind": "file", "role": "material",
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
    display = ResearchConsole()
    validation_runs = 1 + execute_options.repair_rounds if task_kind == "bug_fix" and code_task_spec is not None else 1
    process_count = validation_runs if task_kind == 'bug_fix' else 8 if execution is not None else 0
    process_wall = (max(30, timeout_sec) * validation_runs if task_kind == 'bug_fix'
        else max(60, timeout_sec * 8) if execution is not None else 0)
    if (resume_root is None and execution is not None and execution.get('environment') is not None
            and code_task_spec is None and not execution.get('pairs')):
        from simple_ar.research.project_environment import project_environment_plan
        plan = project_environment_plan(execution['environment'], Path(execution['cwd']),
            Path('__attempt__') / 'environment', list(execution['command']))
        process_count = max(process_count, len(plan['steps']) + 1)
        process_wall = max(process_wall, len(plan['steps']) * plan['profile']['timeout_sec'] + timeout_sec)
    services = ResearchApplicationServices(
        llm_client=llm_client,
        feasibility_llm_client=feasibility_client,
        max_results=args.max_results,
        max_chunks=args.max_chunks,
        idea_limit=args.idea_limit,
        cache_dir=Path(args.cache_dir).resolve() if args.cache_dir else None,
        input_base_dir=Path.cwd(),
        config=config,
        budget_limits={
            "llm_requests": args.llm_requests,
            "total_tokens": args.total_tokens,
            "process_invocations": args.process_invocations if getattr(args, "process_invocations", None) is not None else process_count,
            "process_wall_seconds": args.process_wall_seconds if getattr(args, "process_wall_seconds", None) is not None else process_wall,
        },
        max_attempts=32 if code_task_spec is not None else 20,
        message_callback=display.message,
    )
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
        if resume_root is None:
            if execution is None and (not experiment_requested or llm_client is not None):
                from simple_ar.cli.start import migrate_structured_creation
                migrate_structured_creation(args, request=brief.request_text,
                    outputs=brief.requested_outputs, sources=brief.asset_requests,
                    limits=services.budget_limits, root=session_root, research_config=config,
                    report_config=report_config, writing_method=writing_method,
                    data_analysis=data_analysis, review_model=review_model or None)
                return
            if execution is not None:
                from simple_ar.cli.start import migrate_exact_code_creation, migrate_measurement_creation
                if migrate_measurement_creation(args, execution=execution, request=brief.request_text,
                        outputs=brief.requested_outputs, root=session_root, limits=services.budget_limits,
                        sources=brief.asset_requests, report_config=report_config, writing_method=writing_method,
                        research_config=config):
                    return
                if migrate_exact_code_creation(args, execution=execution, request=brief.request_text,
                        outputs=brief.requested_outputs, sources=brief.asset_requests,
                        limits=services.budget_limits, root=session_root,
                        report_config=report_config, writing_method=writing_method, research_config=config):
                    return
            if report_requested:
                _print_report_writing_plan(writing_method, report_config)
            app = create_session(brief, root=session_root, services=services)
        else:
            from simple_ar.app.research_application import load_session
            from dataclasses import replace
            app = load_session(session_root, services=replace(services, config={}))
            saved_review_model = str(app.services.config.get("feasibility_review_model") or "")
            if saved_review_model and app.services.feasibility_llm_client is None:
                reviewer = _optional_research_llm_client(
                    saved_review_model, "feasibility review",
                    max_output_tokens=getattr(args, "max_output_tokens", None),
                )
                app.services = replace(app.services, feasibility_llm_client=reviewer.with_budget(
                    app.budget_ledger, session_id=app.controller.manifest.session_id,
                ))
            from simple_ar.cli.research_config import report_settings
            explicit = getattr(args, "_explicit_resume_destinations", set())
            revised_brief = _merge_resume_brief(
                app.brief, brief, task_text=task_text, outputs=outputs,
                with_report=args.with_report, no_report=args.no_report,
            )
            report_overrides = report_settings(args, explicit_destinations=explicit)
            app.continue_session(
                reason="Resume from research-session with explicit input/budget revision.",
                resume_if_needed=True,
                revised_brief=revised_brief if revised_brief != app.brief else None,
                revised_execution=execution,
                interaction=args.interaction if "interaction" in explicit else None,
                task_kind=task_kind,
                report_writing_method=args.report_writing_method if "report_writing_method" in explicit else None,
                feasibility_review_model=review_model,
                research_settings=_resume_research_settings(args),
                reanalyze=getattr(args, "reanalyze", False),
                recover_interrupted=getattr(args, "recover_interrupted", False),
                decision_id=args.decision_id,
                decision_response=args.decision_response,
                decision_guidance=args.decision_guidance,
                revised_report_config=report_overrides if report_overrides else None,
                **{**continuation, "authorize_remaining": continuation["authorize_remaining"]
                   if "authorize_remaining" in explicit else None,
                   "additional_attempts": continuation["additional_attempts"] if "additional_attempts" in explicit else None,
                   "additional_no_progress": continuation["additional_no_progress"] if "additional_no_progress" in explicit else None},
            )
        view = app.view()
        display.start(
            view,
            model=getattr(llm_client, "model", "configured") if llm_client is not None else "deterministic",
            topic=app.brief.objective,
        )
        for _ in range(app.controller.manifest.budget.max_attempts + 8):
            if view.next_action is None:
                view = app.advance(max_actions=1)
                break
            with display.action(view.next_action):
                view = app.advance(max_actions=1)
            display.state(view)
            if view.status in {"completed", "paused", "blocked", "failed"}:
                break
        app.export_session()
    except (ResearchApplicationError, ResearchInputError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print_line(f"Research session: {view.session_root}")
    print_line(f"Status: {view.status}")
    print_line(f"Mode: {'llm' if llm_client is not None else 'deterministic'}")
    print_line(f"Next action: {view.next_action or 'none'}")
    print_line(f"Artifacts: {len(view.state_refs)}; attempts: {len(view.attempts)}")
    # A bare --session-root restores the saved execution; do not describe a
    # code/experiment session as literature-only just because no config was
    # repeated on the resume command line.
    display_execution = app.services.config.get("execution") if resume_root is not None else execution
    display_outputs = set(app.brief.requested_outputs if resume_root is not None else requested_outputs)
    display_task_kind = app.task_kind if resume_root is not None else task_kind
    print_line(
        "Implementation: "
        + ("bug-fix CodeTask" if display_task_kind == "bug_fix"
           else "existing Code-Task backend" if isinstance(display_execution, dict) and isinstance(display_execution.get("code_task"), dict)
           else "explicit command" if display_execution is not None
           else "preparation required" if "experiments" in display_outputs
           else "not requested (supplied-material writing)" if display_task_kind == "writing"
           else "not requested (descriptive data analysis)" if display_task_kind == "data_analysis"
           else "not requested (literature-only)")
    )
    display.finish(view)
    summary_ref = view.state_refs.get("summary")
    if summary_ref is not None:
        print_line(f"summary: {view.session_root / summary_ref.path}")
    _ensure_research_cli_success(
        view.status,
        operation="Research session",
        root=view.session_root,
    )


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


def _merge_result_schemas(
    configured: dict[str, object], cli_schema: dict[str, object]
) -> dict[str, object]:
    """Let explicit CLI metric flags override configured Code-Task defaults."""

    merged = dict(configured)
    if cli_schema.get("primary_metric"):
        merged["primary_metric"] = cli_schema["primary_metric"]
    if cli_schema.get("required_metrics"):
        merged["required_metrics"] = cli_schema["required_metrics"]
    if cli_schema.get("metric_directions"):
        merged["metric_directions"] = cli_schema["metric_directions"]
    return merged


def _print_research_session_continue(args: argparse.Namespace) -> None:
    """Retry an experiment through the canonical application lifecycle."""

    session_root = Path(args.session_root)
    from simple_ar.app.research_application import (
        ResearchApplicationError,
        load_session,
    )

    try:
        view = load_session(session_root).retry_experiment(
            command=tuple(args.command_argv or ()),
            cwd=Path(args.cwd),
            timeout_sec=args.timeout_sec,
            result_schema=_experiment_result_schema(args),
            label=args.label,
            parent_attempt_id=args.parent_attempt_id,
            reason="Retry the failed canonical experiment from research-session-continue.",
        )
    except (
        ResearchApplicationError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
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


def _report_config_overrides(args: argparse.Namespace, app: Any) -> dict[str, object]:
    """Compatibility projection for research-report; semantics belong to the application."""
    from simple_ar.cli.research_config import report_settings
    from simple_ar.app.research_application import ResearchApplication

    supplied = report_settings(args, explicit_destinations=getattr(args, "_explicit_resume_destinations", set()))
    return ResearchApplication.report_config_overrides(
        app, supplied, session_resume=args.command == "research-session",
    )


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


def _print_research_report(args: argparse.Namespace) -> None:
    """Generate a report from an existing research-session handoff."""

    if args.max_review_iterations < 0:
        raise SystemExit("--max-review-iterations cannot be negative.")
    from simple_ar.cli.research_config import report_settings
    report_settings(args)
    session_root = Path(args.session_root)
    client = _optional_research_llm_client(args.model, "research report")
    if client is None:
        raise SystemExit("--model is required for research-report.")

    # Report generation belongs to the canonical application lifecycle.
    from simple_ar.app.research_application import (
        ResearchApplicationError,
        ResearchApplicationServices,
        load_session,
    )

    app = None
    try:
        app = load_session(
            session_root,
            services=ResearchApplicationServices(
                llm_client=client,
            ),
        )
    except ResearchApplicationError as exc:
        raise SystemExit(
            f"Cannot continue this report session: {exc}. Historical sessions remain readable; "
            "new research uses research-session. No legacy workflow was executed."
        ) from exc
    try:
        view = app.view()
        report_overrides = _report_config_overrides(args, app)
        if args.refresh:
            view = app.request_report(
                refresh=True,
                report_config=report_overrides,
                reason="Explicitly regenerate the report from existing research evidence.",
            )
        elif report_overrides:
            view = app.request_report(
                report_config=report_overrides,
                reason="Apply explicit report settings and rebuild only report deliverables from existing evidence.",
            )
        elif not ({"report", "paper", "full_paper"} & {
            item.strip().lower() for item in app.brief.requested_outputs
        }):
            view = app.request_report()
        elif view.status in {"paused", "blocked"} and view.next_action is not None:
            view = app.continue_session(
                reason="Resume the canonical report lifecycle within the persisted session budget.",
            )
        _print_report_writing_plan(app.services.config.get("report_writing_method", "sections"),
                                   app.services.config.get("report", {}))
        for _ in range(app.controller.manifest.budget.max_attempts + 8):
            if view.next_action is None and view.status != "running":
                break
            if view.next_action is not None:
                print_line(f"Action: {view.next_action}")
            view = app.advance(max_actions=1)
            print_line(
                f"Status: {view.status}; next: {view.next_action or 'none'}"
                + (f"; {view.status_reason}" if view.status_reason else "")
            )
            if view.status in {"completed", "paused", "blocked", "failed"}:
                break
        app.export_session()
    except (ResearchApplicationError, OSError, RuntimeError, TypeError, ValueError) as exc:
        guidance = _report_budget_authorization_guidance(app) if app is not None else None
        if guidance is not None:
            raise SystemExit(f"{exc}\n{guidance}") from exc
        raise SystemExit(str(exc)) from exc
    print_line(f"Research report session: {view.session_root}")
    print_line(f"Status: {view.status}")
    audit_line = report_audit_line(view)
    if audit_line is not None:
        print_line(audit_line)
    for name in ("report", "report_audit"):
        if name in view.state_refs:
            print_line(f"{name}: {view.session_root / view.state_refs[name].path}")
    if view.status in {"paused", "blocked"}:
        guidance = _report_budget_authorization_guidance(app)
        if guidance is not None:
            print_line(guidance)
    _ensure_research_cli_success(
        view.status,
        operation="Research report",
        root=view.session_root,
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


def _print_code_task_status(run_dir: Path, manifest: dict[str, object]) -> None:
    """Print status for a code-task workflow manifest."""
    print_line(f"Run: {run_dir}")
    print_line(f"Workflow: {manifest.get('workflow', 'code_task')}")
    print_line(f"Status: {manifest.get('status', 'unknown')}")
    objective = manifest.get("objective", {})
    if isinstance(objective, dict) and objective:
        print_line(f"Objective: {objective.get('status', 'unknown')}")

    layout = manifest.get("layout", {})
    if isinstance(layout, dict):
        print_line("Layout:")
        for key in (
            "summary",
            "task",
            "workspace",
            "meta",
            "codebase_index",
            "work_plan",
            "attempts",
        ):
            value = layout.get(key)
            if value:
                print_line(f"- {key}: {run_dir / str(value)}")

    workspace = manifest.get("workspace", {})
    if isinstance(workspace, dict) and workspace:
        print_line("Workspace:")
        print_line(f"- mode: {workspace.get('mode', 'copy')}")
        cleanup = workspace.get("cleanup_hint")
        if cleanup:
            print_line(f"- cleanup: {cleanup}")

    codebase = manifest.get("codebase", {})
    if isinstance(codebase, dict):
        print_line("Codebase:")
        print_line(f"- files: {codebase.get('file_count', 0)}")
        print_line(f"- python files: {codebase.get('python_file_count', 0)}")
        print_line(f"- test files: {codebase.get('test_file_count', 0)}")

    plan = manifest.get("plan", {})
    if isinstance(plan, dict) and plan:
        print_line("Plan:")
        print_line(f"- status: {plan.get('status', 'unknown')}")
        print_line(f"- mode: {plan.get('mode', 'unknown')}")
        if plan.get("patch_plan"):
            print_line(f"- patch plan: {run_dir / str(plan.get('patch_plan'))}")

    work_plan = manifest.get("work_plan", {})
    if isinstance(work_plan, dict) and work_plan:
        print_line("Work Plan:")
        print_line(f"- status: {work_plan.get('status', 'unknown')}")
        print_line(f"- mode: {work_plan.get('mode', 'unknown')}")
        print_line(f"- items: {work_plan.get('item_count', 0)}")
        if work_plan.get("path"):
            print_line(f"- json: {run_dir / str(work_plan.get('path'))}")
        if work_plan.get("markdown"):
            print_line(f"- markdown: {run_dir / str(work_plan.get('markdown'))}")

    attempts = manifest.get("attempts", {})
    if isinstance(attempts, dict) and attempts:
        print_line("Attempts:")
        print_line(f"- active: {attempts.get('active', '')}")
        if attempts.get("latest_batch"):
            print_line(f"- latest batch: {run_dir / str(attempts.get('latest_batch'))}")

    environment = manifest.get("environment", {})
    if isinstance(environment, dict) and environment:
        print_line("Environment:")
        print_line(f"- status: {environment.get('status', 'unknown')}")
        policy = environment.get("policy", {})
        if isinstance(policy, dict):
            print_line(f"- mode: {policy.get('mode', 'current')}")
            if policy.get("python_executable"):
                print_line(f"- python: {policy.get('python_executable')}")
        report = environment.get("report")
        if report:
            print_line(f"- report: {run_dir / str(report)}")
        platform_data = environment.get("platform", {})
        if isinstance(platform_data, dict):
            system = platform_data.get("system")
            release = platform_data.get("release")
            if system:
                print_line(f"- platform: {system} {release or ''}".rstrip())
        gpu = environment.get("gpu", {})
        if isinstance(gpu, dict):
            print_line(f"- gpu: {gpu.get('count', 0)} device(s)")

    patch = manifest.get("patch", {})
    if isinstance(patch, dict) and patch:
        print_line("Patch:")
        print_line(f"- status: {patch.get('status', 'unknown')}")
        editor = patch.get("editor")
        backend = patch.get("editor_backend")
        if not backend and isinstance(editor, dict):
            backend = editor.get("backend")
        if backend:
            print_line(f"- editor backend: {backend}")
        if patch.get("proposed_edits"):
            print_line(f"- proposed edits: {run_dir / str(patch.get('proposed_edits'))}")
        if patch.get("patch_diff"):
            print_line(f"- patch diff: {run_dir / str(patch.get('patch_diff'))}")
        changed_files = patch.get("changed_files")
        if isinstance(changed_files, list) and changed_files:
            print_line(f"- changed files: {', '.join(str(path) for path in changed_files)}")

    validation = manifest.get("validation", {})
    if isinstance(validation, dict) and validation:
        print_line("Validation:")
        print_line(f"- status: {validation.get('status', 'unknown')}")
        print_line(f"- errors: {validation.get('error_count', 0)}")
        print_line(f"- warnings: {validation.get('warning_count', 0)}")
        if validation.get("report"):
            print_line(f"- report: {run_dir / str(validation.get('report'))}")

    benchmark = manifest.get("benchmark", {})
    if isinstance(benchmark, dict) and benchmark.get("command"):
        print_line("Benchmark:")
        print_line(f"- command: {benchmark.get('command')}")
        if benchmark.get("primary_metric"):
            print_line(f"- primary metric: {benchmark.get('primary_metric')}")
        metric_directions = benchmark.get("metric_directions", {})
        if isinstance(metric_directions, dict) and metric_directions:
            direction_text = ", ".join(
                f"{name}={direction}"
                for name, direction in sorted(metric_directions.items())
            )
            print_line(f"- metric directions: {direction_text}")
        print_line(f"- executed: {benchmark.get('executed', False)}")
        if benchmark.get("last_status"):
            print_line(f"- last status: {benchmark.get('last_status')}")
        if benchmark.get("latest_label"):
            print_line(f"- latest label: {benchmark.get('latest_label')}")
        runs = benchmark.get("runs", {})
        if isinstance(runs, dict) and runs:
            for label in ("baseline", "patched"):
                row = runs.get(label)
                if isinstance(row, dict):
                    print_line(f"- {label}: {row.get('status', 'unknown')}")
        comparison = benchmark.get("comparison", {})
        if isinstance(comparison, dict) and comparison:
            print_line(f"- comparison: {comparison.get('verdict', 'inconclusive')}")
            deltas = comparison.get("deltas", {})
            if isinstance(deltas, dict) and deltas:
                delta_text = ", ".join(
                    f"{name}={_format_status_number(value)}"
                    for name, value in sorted(deltas.items())[:5]
                )
                print_line(f"- comparison deltas: {delta_text}")
            if comparison.get("path"):
                print_line(f"- comparison report: {run_dir / str(comparison.get('path'))}")
        if benchmark.get("execution_report"):
            print_line(f"- execution report: {run_dir / str(benchmark.get('execution_report'))}")

    failure = manifest.get("failure_analysis", {})
    if isinstance(failure, dict) and failure:
        if failure.get("status") not in {"no_failure", "resolved"}:
            print_line("Failure Analysis:")
            print_line(f"- status: {failure.get('status', 'unknown')}")
            if failure.get("source"):
                print_line(f"- source: {failure.get('source')}")
            if failure.get("analysis"):
                print_line(f"- analysis: {run_dir / str(failure.get('analysis'))}")

    repair = manifest.get("repair", {})
    if isinstance(repair, dict) and repair:
        if repair.get("status") not in {"benchmark_passed", "resolved"}:
            print_line("Repair:")
            print_line(f"- status: {repair.get('status', 'unknown')}")
            print_line(f"- attempts: {repair.get('repair_count', 0)}")
            if repair.get("latest_proposed_edits"):
                print_line(f"- latest proposal: {run_dir / str(repair.get('latest_proposed_edits'))}")


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


def _print_code_task_init(args: argparse.Namespace) -> None:
    """Initialize a code-task run and print the resulting workspace summary."""
    try:
        options = load_code_task_init_options(
            config_path=args.config,
            kind=args.kind,
            code_root=args.code_root,
            task_file=args.task_file,
            output_root=args.output_root,
            name=args.name,
            benchmark_command=args.benchmark_command,
            max_file_bytes=args.max_file_bytes,
            workspace_mode=args.workspace_mode,
            workspace_include=args.workspace_include,
            workspace_exclude=args.workspace_exclude,
            workspace_reuse_source_venv=args.workspace_reuse_source_venv,
            env_mode=args.env_mode,
            python_executable=args.python_executable,
            primary_metric=args.primary_metric,
            metric_directions=args.metric_direction or [],
        )
    except CodeTaskConfigError as exc:
        raise SystemExit(str(exc)) from exc
    code_root = Path(options.code_root) if options.code_root else None
    if options.task_file is None:
        raise SystemExit("Missing task file. Pass --task-file or set [code_task].task_file.")
    task_file = Path(options.task_file)
    name = options.name or (
        f"code-task-{code_root.resolve().name}" if code_root is not None else "greenfield-code-task"
    )
    from simple_ar.app.session_roots import new_research_session_root

    try:
        run_dir = new_research_session_root(Path(options.output_root), name)
        result = initialize_code_task(
            run_dir=run_dir,
            code_root=code_root,
            task_file=task_file,
            kind=options.kind,
            benchmark_command=options.benchmark_command,
            max_file_bytes=options.max_file_bytes,
            workspace_mode=options.workspace_mode,
            workspace_include=options.workspace_include,
            workspace_exclude=options.workspace_exclude,
            workspace_reuse_source_venv=options.workspace_reuse_source_venv,
            env_mode=options.env_mode,
            python_executable=options.python_executable,
            primary_metric=options.primary_metric,
            metric_directions=options.metric_directions,
            edit_scope_mode=options.edit_scope_mode,
            edit_scope_allowed_patterns=options.edit_scope_allowed_patterns,
            edit_scope_protected_patterns=options.edit_scope_protected_patterns,
        )
    except (
        FileExistsError,
        FileNotFoundError,
        NotADirectoryError,
        ValueError,
        WorkspaceModeError,
    ) as exc:
        raise SystemExit(_code_task_init_error_message(exc, options=options)) from exc
    render_init_result(
        result,
        config_path=options.config_path,
        benchmark_command=options.benchmark_command,
        primary_metric=options.primary_metric,
        metric_directions=options.metric_directions,
    )


def _code_task_init_error_message(
    exc: Exception,
    *,
    options: object,
) -> str:
    """Return a user-facing init error with likely next steps."""
    workspace_mode = getattr(options, "workspace_mode", "copy")
    code_root = getattr(options, "code_root", "")
    task_file = getattr(options, "task_file", "")
    lines = [f"Could not initialize code task: {exc}"]
    if isinstance(exc, FileNotFoundError) and "Task file" in str(exc):
        lines.extend(
            [
                "",
                "Check the task file path:",
                f"- configured task_file: {task_file or '(missing)'}",
                "- Pass --task-file path/to/task.md, or set [code_task].task_file in TOML.",
                "- Windows-style backslashes are accepted, but forward slashes are portable across Linux/macOS/Windows.",
                "- For embedded 8-stage code_task_project runs, omit task_file only if you want 05-design to generate one.",
            ]
        )
    elif isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        lines.extend(
            [
                "",
                "Check the code root path:",
                f"- configured code_root: {code_root or '(missing)'}",
                "- It should point to the baseline project directory, not the task file.",
                "- If you use git_worktree, code_root may be the git repository root or a project subdirectory inside it.",
            ]
        )
    elif isinstance(exc, WorkspaceModeError) and workspace_mode == "git_worktree":
        lines.extend(
            [
                "",
                "git_worktree quick checklist:",
                "- code_root should be inside the baseline git repository.",
                "- The repository needs at least one local commit.",
                "- GitHub or any remote is not required.",
                "- If code_root is a monorepo subdirectory, SimpleAutoResearch will use the matching worktree subdirectory as project root.",
                "- Use --workspace-mode copy when the baseline is not a git repository.",
            ]
        )
    return "\n".join(lines)


def _print_code_task_probe(args: argparse.Namespace) -> None:
    """Probe the workspace environment and print a compact summary."""
    result = probe_code_task_environment(
        Path(args.run_dir),
        env_mode=args.env_mode,
        python_executable=args.python_executable,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Environment report: {result.report_path}")
    print_line(f"Status: {result.status}")
    gpu_count = result.gpu.get("count", 0) if isinstance(result.gpu, dict) else 0
    print_line(f"GPU devices: {gpu_count}")
    available_tools = [
        name
        for name, data in result.tools.items()
        if isinstance(data, dict) and data.get("available") is True
    ]
    if available_tools:
        print_line("Available tools: " + ", ".join(sorted(available_tools)))
    if result.warnings:
        print_line("Warnings:")
        for warning in result.warnings:
            print_line(f"- {warning}")


def _print_code_task_map(args: argparse.Namespace) -> None:
    """Build repo-map artifacts and print the resulting project summary."""
    result = build_code_task_repo_map(
        Path(args.run_dir),
        refresh_index=not args.no_refresh_index,
    )
    project = result.repo_map.get("project", {})
    if not isinstance(project, dict):
        project = {}
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Repo map: {result.repo_map_path}")
    print_line(f"Summary: {result.summary_path}")
    print_line(f"Index: {result.codebase_index_path}")
    print_line(f"Index refreshed: {result.refreshed_index}")
    print_line(
        "Mapped: "
        f"{project.get('file_count', 0)} file(s), "
        f"{project.get('directory_count', 0)} directory group(s), "
        f"{project.get('symbol_count', 0)} symbol(s)"
    )
    print_line(
        "Roles: "
        f"{project.get('test_file_count', 0)} test file(s), "
        f"{project.get('benchmark_file_count', 0)} benchmark file(s), "
        f"{project.get('config_file_count', 0)} config file(s)"
    )
    entrypoints = result.repo_map.get("entrypoints", [])
    if isinstance(entrypoints, list) and entrypoints:
        print_line("Entrypoints:")
        for item in entrypoints[:8]:
            if isinstance(item, dict):
                symbol = item.get("symbol")
                suffix = f"::{symbol}" if symbol else ""
                print_line(f"- {item.get('path', '')}{suffix}")
    if args.show_summary:
        print_line("")
        print_line(read_text(result.summary_path))


def _print_code_task_locate(args: argparse.Namespace) -> None:
    """Rank likely code-task context files and print a compact summary."""
    result = locate_code_task_context(
        Path(args.run_dir),
        query=args.query,
        top_k=args.top_k,
        refresh_map=args.refresh_map,
        include_read_only=not args.no_read_only,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Locate results: {result.results_path}")
    print_line(f"Summary: {result.summary_path}")
    print_line(f"Editable targets: {len(result.editable_targets)}")
    for row in result.editable_targets[:8]:
        print_line(f"- {row.get('path', '')} (score {row.get('score', 0)})")
    print_line(f"Read-only evidence: {len(result.read_only_evidence)}")
    for row in result.read_only_evidence[:8]:
        print_line(f"- {row.get('path', '')} (score {row.get('score', 0)})")
    if args.show_summary:
        print_line("")
        print_line(read_text(result.summary_path))


def _print_code_task_context(args: argparse.Namespace) -> None:
    """Build a bounded code-task context pack and print artifact paths."""
    result = build_code_task_context_pack(
        Path(args.run_dir),
        query=args.query,
        top_k=args.top_k,
        max_files=args.max_files,
        max_source_chars_per_file=args.max_source_chars_per_file,
        max_total_chars=args.max_total_chars,
        refresh_map=args.refresh_map,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Context directory: {result.context_dir}")
    print_line(f"Context pack: {result.context_pack_path}")
    print_line(f"Prompt context: {result.prompt_context_path}")
    print_line(f"Snippets: {result.snippets_path}")
    print_line(f"Locate results: {result.locate_results_path}")
    print_line(f"Selected files: {len(result.selected_files)}")
    for path in result.selected_files[:12]:
        print_line(f"- {path}")
    if args.show_prompt:
        print_line("")
        print_line(read_text(result.prompt_context_path))


def _print_code_task_work_plan(args: argparse.Namespace) -> None:
    """Generate a batch-oriented work plan and print artifact paths."""
    try:
        result = generate_code_task_work_plan(
            Path(args.run_dir),
            model=args.model,
            use_llm=not args.no_llm,
            allow_llm_fallback=args.allow_planning_fallback,
            llm_retry_attempts=args.llm_retry_attempts,
            force=args.force,
            max_files=args.max_files,
            max_source_chars_per_file=args.max_source_chars_per_file,
            message_callback=lambda message: print_line(f"  - {message}"),
        )
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Work plan: {result.work_plan_path}")
    print_line(f"Work plan markdown: {result.work_plan_markdown_path}")
    print_line(f"Mode: {result.mode}")
    print_line(f"Items: {result.item_count}")
    print_line(f"Pending approval: {result.pending_approval}")
    print_line(f"Context files: {len(result.selected_files)}")
    for path in result.selected_files:
        print_line(f"- {path}")


def _print_code_task_batch(args: argparse.Namespace) -> None:
    """Create attempt/batch state for a reviewed work item."""
    result = create_code_task_batch(
        Path(args.run_dir),
        work_item_id=args.work_item,
        attempt_id=args.attempt_id,
        force=args.force,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Attempt: {result.attempt_id}")
    print_line(f"Batch: {result.batch_id}")
    print_line(f"Work item: {result.work_item_id}")
    print_line(f"State: {result.state}")
    print_line(f"Attempt state: {result.attempt_state_path}")
    print_line(f"Batch state: {result.batch_state_path}")


def _print_code_task_plan(args: argparse.Namespace) -> None:
    """Generate a code-task patch plan and print a compact summary."""
    try:
        result = generate_patch_plan(
            Path(args.run_dir),
            model=args.model,
            use_llm=not args.no_llm,
            allow_llm_fallback=args.allow_planning_fallback,
            llm_retry_attempts=args.llm_retry_attempts,
            force=args.force,
            max_files=args.max_files,
            max_source_chars_per_file=args.max_source_chars_per_file,
            message_callback=lambda message: print_line(f"  - {message}"),
        )
    except LLMError as exc:
        raise SystemExit(str(exc)) from exc
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Patch plan: {result.patch_plan_path}")
    print_line(f"Mode: {result.mode}")
    print_line(f"Pending approval: {result.pending_approval}")
    print_line(f"Context files: {len(result.selected_files)}")
    for path in result.selected_files:
        print_line(f"- {path}")


def _print_code_task_decision(args: argparse.Namespace) -> None:
    """Record and print the human decision for a patch plan."""
    row = record_plan_decision(
        Path(args.run_dir),
        decision=args.decision,
        note=args.note,
        reviewer=args.reviewer,
    )
    print_line(f"Code task run: {args.run_dir}")
    print_line(f"Decision: {row['decision']}")
    print_line("Decision log: code_task/meta/hitl_decisions.jsonl")


def _print_code_task_propose_edits(args: argparse.Namespace) -> None:
    """Generate controlled edits and print a compact proposal summary."""
    result = propose_patch_edits(
        Path(args.run_dir),
        model=args.model,
        use_llm=not args.no_llm,
        force=args.force,
        max_files=args.max_files,
        max_source_chars_per_file=args.max_source_chars_per_file,
        allow_large_edits=args.allow_large_edits,
        message_callback=lambda message: print_line(f"  - {message}"),
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Proposed edits: {result.proposal_path}")
    print_line(f"Mode: {result.mode}")
    print_line(f"Edit count: {result.edit_count}")
    print_line(f"Context files: {len(result.selected_files)}")
    for path in result.selected_files:
        print_line(f"- {path}")


def _print_code_task_apply_edits(args: argparse.Namespace) -> None:
    """Safely apply controlled edits and print changed files."""
    try:
        result = apply_patch_edits(
            Path(args.run_dir),
            edits_file=Path(args.edits_file) if args.edits_file else None,
            allow_unapproved_plan=args.allow_unapproved_plan,
            allow_large_edits=args.allow_large_edits,
        )
    except PatchValidationError as exc:
        print_line("Patch validation failed; no workspace files were changed.")
        print_line(str(exc))
        raise SystemExit(1) from exc
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Patch diff: {result.patch_diff_path}")
    print_line(f"Applied edits: {result.applied_edits_path}")
    print_line(f"Changed files: {len(result.changed_files)}")
    for path in result.changed_files:
        print_line(f"- {path}")


def _print_code_task_validate(args: argparse.Namespace) -> None:
    """Run static validation and print a compact issue summary."""
    result = validate_code_task(
        Path(args.run_dir),
        strict=args.strict,
        max_file_bytes=args.max_file_bytes,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Validation report: {result.report_path}")
    print_line(f"Status: {result.status}")
    print_line(f"Errors: {result.error_count}")
    print_line(f"Warnings: {result.warning_count}")


def _print_code_task_run(args: argparse.Namespace) -> None:
    """Run a code-task benchmark and print execution artifacts."""
    result = run_code_task_benchmark(
        Path(args.run_dir),
        command=args.benchmark_command,
        timeout_sec=args.timeout,
        skip_validation=args.skip_validation,
        env_mode=args.env_mode,
        python_executable=args.python_executable,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Run label: {result.label}")
    print_line(f"Execution report: {result.report_path}")
    print_line(f"Status: {result.status}")
    print_line(f"Return code: {result.returncode}")
    print_line(f"Timed out: {result.timed_out}")
    print_line(f"Stdout: {result.stdout_path}")
    print_line(f"Stderr: {result.stderr_path}")
    if result.metrics:
        print_line("Metrics:")
        for key, value in result.metrics.items():
            print_line(f"- {key}: {value}")


def _print_code_task_baseline(args: argparse.Namespace) -> None:
    """Run the pre-patch benchmark and print execution artifacts."""
    result = run_code_task_baseline(
        Path(args.run_dir),
        command=args.benchmark_command,
        timeout_sec=args.timeout,
        skip_validation=args.skip_validation,
        env_mode=args.env_mode,
        python_executable=args.python_executable,
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Baseline report: {result.report_path}")
    print_line(f"Status: {result.status}")
    print_line(f"Return code: {result.returncode}")
    print_line(f"Timed out: {result.timed_out}")
    print_line(f"Stdout: {result.stdout_path}")
    print_line(f"Stderr: {result.stderr_path}")
    if result.metrics:
        print_line("Metrics:")
        for key, value in result.metrics.items():
            print_line(f"- {key}: {value}")


def _print_code_task_analyze_failure(args: argparse.Namespace) -> None:
    """Write failure analysis and print the implicated files."""
    result = analyze_code_task_failure(Path(args.run_dir))
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Failure analysis: {result.analysis_path}")
    print_line(f"Status: {result.status}")
    print_line(f"Source: {result.source}")
    print_line(f"Implicated files: {len(result.implicated_files)}")
    for path in result.implicated_files:
        print_line(f"- {path}")


def _print_code_task_repair(args: argparse.Namespace) -> None:
    """Generate a bounded repair proposal and print the review path."""
    result = propose_repair_edits(
        Path(args.run_dir),
        model=args.model,
        use_llm=not args.no_llm,
        max_files=args.max_files,
        max_source_chars_per_file=args.max_source_chars_per_file,
        message_callback=lambda message: print_line(f"  - {message}"),
    )
    print_line(f"Code task run: {result.run_dir}")
    print_line(f"Repair directory: {result.repair_dir}")
    print_line(f"Proposed edits: {result.proposal_path}")
    print_line(f"Mode: {result.mode}")
    print_line(f"Edit count: {result.edit_count}")
    print_line(f"Context files: {len(result.selected_files)}")
    for path in result.selected_files:
        print_line(f"- {path}")


def _print_code_task_execute(args: argparse.Namespace) -> None:
    """Run the state-aware code-task orchestrator and render progress."""
    try:
        options = load_code_task_execute_options(config_path=args.config)
    except CodeTaskConfigError as exc:
        raise SystemExit(str(exc)) from exc
    model = args.model or options.model
    use_llm = False if args.no_llm else options.use_llm
    timeout = args.timeout if args.timeout != 60 else options.timeout_sec
    to_step = args.to_step or options.to_step
    repair_rounds = args.repair_rounds if args.repair_rounds is not None else options.repair_rounds
    planning_review_rounds = (
        args.planning_review_rounds
        if args.planning_review_rounds is not None
        else options.planning_review_rounds
    )
    max_files = args.max_files if args.max_files != 8 else options.max_files
    max_source_chars = (
        args.max_source_chars_per_file
        if args.max_source_chars_per_file != 4000
        else options.max_source_chars_per_file
    )
    validation_max_file_bytes = (
        args.validation_max_file_bytes
        if args.validation_max_file_bytes != 500_000
        else options.validation_max_file_bytes
    )
    env_mode = args.env_mode or options.env_mode
    python_executable = args.python_executable or options.python_executable
    allow_planning_fallback = args.allow_planning_fallback or options.allow_planning_fallback
    planning_mode = args.planning_mode or options.planning_mode
    llm_retry_attempts = args.llm_retry_attempts or options.llm_retry_attempts
    baseline_policy = args.baseline_policy or options.baseline_policy
    baseline_metrics_file = args.baseline_metrics_file or options.baseline_metrics_file
    review_gate = args.review_gate or options.review_gate
    inline_apply_proposed_edits = False
    inline_allow_large_edits = False

    run_dir = Path(args.run_dir)
    render_execute_header(
        run_dir,
        to_step=to_step,
        use_llm=use_llm,
        timeout_sec=timeout,
        dry_run=args.dry_run,
    )

    def run_execute(target_step: str, *, dry_run_override: bool | None = None):
        return execute_code_task(
            run_dir,
            to_step=target_step,
            dry_run=args.dry_run if dry_run_override is None else dry_run_override,
            model=model,
            planner_model=options.planner_model,
            writer_model=options.writer_model,
            reviewer_model=options.reviewer_model,
            editor_model=options.editor_model,
            repair_model=options.repair_model,
            use_llm=use_llm,
            timeout_sec=timeout,
            skip_validation=args.skip_validation or options.skip_validation,
            env_mode=env_mode,
            python_executable=python_executable,
            strict_validation=args.strict_validation or options.strict_validation,
            validation_max_file_bytes=validation_max_file_bytes,
            stream_benchmark_output=options.stream_benchmark_output,
            baseline_policy=baseline_policy,
            baseline_metrics_file=baseline_metrics_file,
            apply_proposed_edits=(
                args.apply_proposed_edits
                or options.apply_proposed_edits
                or inline_apply_proposed_edits
            ),
            allow_large_edits=(
                args.allow_large_edits
                or options.allow_large_edits
                or inline_allow_large_edits
            ),
            allow_planning_fallback=allow_planning_fallback,
            planning_mode=planning_mode,
            planning_review_rounds=planning_review_rounds,
            llm_retry_attempts=llm_retry_attempts,
            repair_rounds=repair_rounds,
            planning_snapshot_from=args.reuse_planning_from,
            review_gate=review_gate,
            budget_profile=options.budget_profile,
            edit_budget_overrides=options.edit_budget_overrides,
            max_batches=options.max_batches,
            cost_cap_usd=options.cost_cap_usd,
            max_files=max_files,
            max_source_chars_per_file=max_source_chars,
            max_generated_lines=options.max_generated_lines,
            implementation_provider=options.implementation_provider,
            implementation_agent_mode=options.implementation_agent_mode,
            implementation_allow_external_agent=options.implementation_allow_external_agent,
            implementation_agent_model=options.implementation_agent_model,
            implementation_agent_binary=options.implementation_agent_binary,
            implementation_agent_args=options.implementation_agent_args,
            implementation_agent_timeout_sec=options.implementation_agent_timeout_sec,
            message_callback=render_execute_message,
        )

    staged_mode = bool(args.interactive)
    if not staged_mode:
        result = run_execute(to_step)
        while True:
            render_execute_result(result)
            if not _inline_review_enabled(args):
                return
            action = _inline_review_action(result, to_step)
            if action == "approve-plan":
                render_review_gate(
                    title="Patch Plan Review",
                    artifact=run_dir / "code_task" / "patch_plan.md",
                    action="approve this patch plan and continue to edit proposal generation",
                    warning=(
                        "Only approve if the plan matches the task, edit scope, "
                        "benchmark, and expected files."
                    ),
                )
                if not confirm_review_gate(
                    "Approve the patch plan and continue?",
                    assume_yes=bool(args.yes),
                ):
                    print_line("Execute stopped at patch-plan review.")
                    return
                record_plan_decision(
                    run_dir,
                    decision="approve",
                    note="Approved from inline execute review.",
                    reviewer="execute",
                )
                result = run_execute(to_step)
                continue
            if action == "apply-proposal":
                render_review_gate(
                    title="Edit Proposal Review",
                    artifact=run_dir / "code_task" / "meta" / "proposed_edits.json",
                    action="apply the reviewed proposal, validate, and run the patched benchmark",
                    warning=(
                        "Only continue after checking every old/new replacement, "
                        "target path, and proposal warning."
                    ),
                )
                if not confirm_review_gate(
                    "Apply the reviewed edit proposal and continue?",
                    assume_yes=bool(args.yes),
                ):
                    print_line("Execute stopped at edit-proposal review.")
                    return
                inline_apply_proposed_edits = True
                result = run_execute(to_step)
                continue
            if action == "allow-large-edits":
                render_review_gate(
                    title="Large Edit Review",
                    artifact=run_dir / "code_task" / "meta" / "proposed_edits.json",
                    action="allow the large reviewed proposal and continue applying it",
                    warning=(
                        "Large edits have wider blast radius. Continue only if "
                        "the larger patch is intentional and fully reviewed."
                    ),
                )
                if not confirm_review_gate(
                    "Allow this large edit proposal and continue?",
                    assume_yes=bool(args.yes),
                ):
                    print_line("Execute stopped at large-edit review.")
                    return
                inline_apply_proposed_edits = True
                inline_allow_large_edits = True
                result = run_execute(to_step)
                continue
            return
        return

    target_index = EXECUTE_STEPS.index(to_step)
    rendered_steps: set[str] = set()
    for step in EXECUTE_STEPS[: target_index + 1]:
        if (step in {"work-plan", "batch"} and to_step not in {"work-plan", "batch"}
                and not (Path(args.run_dir) / "code_task/work_plan.json").is_file()):
            continue
        preview = run_execute(step, dry_run_override=True)
        current_preview = next((record for record in reversed(preview.steps) if record.step == step), None)
        if current_preview is not None and current_preview.status == "skipped":
            render_execute_result(preview, steps=(current_preview,))
            rendered_steps.add(current_preview.step)
            if preview.stop_reason != "stop_point":
                return
            continue
        if preview.stop_reason != "dry_run":
            unseen_steps = tuple(record for record in preview.steps if record.step not in rendered_steps)
            render_execute_result(preview, steps=unseen_steps)
            return
        if not args.yes:
            render_step_preview(step)
            if not confirm_next_step(step, assume_yes=False):
                print_line(f"Execute stopped before step: {step}")
                return
        result = run_execute(step)
        current = next((record for record in reversed(result.steps) if record.step == step), None)
        render_execute_result(result, steps=(current,) if current is not None else ())
        if current is not None:
            rendered_steps.add(current.step)
        if result.stop_reason not in {"stop_point", "completed"}:
            return


def _inline_review_enabled(args: argparse.Namespace) -> bool:
    """Return true when execute may ask inline review-gate questions."""

    return (
        not bool(args.dry_run)
        and not bool(args.no_review_inline)
        and not bool(args.interactive)
        and (bool(args.yes) or sys.stdin.isatty())
    )


def _inline_review_action(result: object, to_step: str) -> str | None:
    """Map an execute stop reason to an optional inline review action."""

    target_index = EXECUTE_STEPS.index(to_step)
    stop_reason = getattr(result, "stop_reason", "")
    if stop_reason == "approval_required" and target_index > EXECUTE_STEPS.index("plan"):
        return "approve-plan"
    if stop_reason == "proposal_review_required" and target_index > EXECUTE_STEPS.index("propose-edits"):
        return "apply-proposal"
    if stop_reason == "large_edit_approval_required" and target_index > EXECUTE_STEPS.index("apply-edits"):
        return "allow-large-edits"
    return None


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


def _render_native_result(app: Any, status: dict[str, Any]) -> None:
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

    pending = app.state.get("pending") or {}
    reason = str(status.get("status_reason") or "")
    # Exception messages can contain provider URLs, headers or credentials.
    if reason.startswith("BudgetExceededError:") or (
        reason.startswith("RuntimeError: Operation failed; inspect saved diagnostics: LLMError: ")
        and "LLM budget prevented provider attempt " in reason
    ):
        reason = "Insufficient remaining budget before dispatch / 发送前剩余额度不足"
    elif reason.startswith("BudgetUnknownError:"):
        reason = "Prior usage is unknown / 先前用量未核清"
    elif ":" in reason:
        reason = "Saved stop detail withheld; inspect session_manifest.json locally"
    elif reason.startswith("Process requires explicit approval"):
        reason = "Process awaits explicit approval / 执行等待明确授权"
    elif reason not in {
        "", "Delivery manifest saved; scientific quality requires independent checking",
        "Model returned prose without a delivery or question; inspect the saved reply before continuing",
        "Shared model request allowance exhausted, including professional calls; existing artifacts are retained, not marked complete",
    }:
        reason = "See saved status_reason in session_manifest.json / 原因见已保存状态记录"
    lines = [f"Status: {status['status']}", f"Reason / 原因: {reason or 'not recorded'}",
             f"Saved session / 会话: {root}", "Locations below are relative to this session unless absolute.",
             f"Workspace / 工作区: {location(app.workspace.workspace)}", "Attempts / 尝试记录: attempts"]
    if pending.get("kind") in {"model", "tool"}:
        lines.append(f"Pending action / 待处理动作: {pending['kind']}")
    attempt = pending.get("attempt") or status.get("current_attempt")
    if isinstance(attempt, str) and attempt not in {".", ".."} and Path(attempt).name == attempt and "\\" not in attempt:
        lines.append(f"Selected attempt / 当前尝试: attempts/{attempt}")
    if status['status'] not in {"completed", "partial"}:
        lines.append("Current request has no confirmed delivery / 当前请求未确认交付")
    else:
        lines.append("Session status is terminal; partial retains unresolved work. Delivery binding and quality are not certified.")
    if app.session.store.exists("delivery.json"):
        lines.append("Saved delivery record / 已保存交付记录: delivery.json")
        lines.append("Request/version binding not checked here; this record may precede the current feedback.")
        try:
            delivery = app.session.store.read_json("delivery.json")
            artifacts = delivery["artifacts"]
            if not isinstance(artifacts, list):
                raise ValueError("Invalid saved artifact list")
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
    else:
        lines.append("No saved delivery record / 无已保存交付记录")

    budget = app.ledger.snapshot()
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


def _run_native_start(args: argparse.Namespace) -> None:
    from simple_ar.app.session_roots import new_research_session_root
    from simple_ar.app.tool_application import ToolApplication
    from simple_ar.cli.start import native_creation_options, select_start_model
    from simple_ar.tools.workspace import resolve_source_mount
    from simple_ar.core.console import make_console, print_line
    from rich.panel import Panel
    from rich.text import Text
    interactive = sys.stdin.isatty()
    try:
        decision = {name: getattr(args, name, None) for name in
                    ('decision_id', 'decision_response', 'decision_guidance')}
        if any(value is not None for value in decision.values()):
            if not args.resume_task or not decision['decision_id'] or not decision['decision_response']:
                raise ValueError('A design reply requires --resume-task, --decision-id and --decision-response')
        else:
            decision = None
        continuation = _continuation_parameters(args)
        if args.approve_execution and not args.resume_task:
            raise ValueError("--approve-execution requires --resume-task and its saved pending command")
        allowances = continuation["authorize_remaining"]
        authorization_id = continuation["authorization_id"] or ""
        authorization_reason = continuation["authorization_reason"] or ""
        if args.retry_model:
            if not args.resume_task:
                raise ValueError('--retry-model requires --resume-task')
            if (args.request or args.goal or args.approve_execution or args.source
                    or args.allow_execution or args.allow_network or args.allow_vision
                    or allowances or authorization_id or authorization_reason):
                raise ValueError('Use --retry-model separately from feedback, access, resource or tool authorization')
        if allowances or continuation["authorization_id"] is not None or continuation["authorization_reason"] is not None:
            if not args.resume_task:
                raise ValueError("Resource authorization requires --resume-task")
            if not allowances:
                raise ValueError("--authorization-id/--authorization-reason require --authorize-remaining")
            if not authorization_id.strip() or not authorization_reason.strip():
                raise ValueError("--authorize-remaining requires --authorization-id and --authorization-reason")
            if args.approve_execution:
                raise ValueError("Confirm resource authorization separately from --approve-execution")
        if args.resume_task:
            if args.max_images or args.project_python:
                raise ValueError("Resume keeps the saved image allowance and interpreter; it cannot silently change them")
            app = ToolApplication.load(args.resume_task.resolve())
            feedback = args.request or args.goal or ""
            explicit_model = 'model' in getattr(args, '_explicit_start_destinations', set())
            replacement_model = args.model if args.retry_model and explicit_model else ''
            if explicit_model and not args.retry_model and args.model != app.state['config']['model']:
                raise ValueError('Resume retains its saved model. Change a terminated failed decision '
                                 'explicitly with --retry-model --model profile:NAME or route:NAME')
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
                if not args.prepare_only and not args.yes:
                    if not interactive or input('Confirm connection replacement / 确认连接替换? [y/N]: ').strip().lower() not in {'y', 'yes'}:
                        return
            if args.retry_model:
                print_line("Explicit model continuation: one new decision attempt, not a task or tool replay. "
                           "Old failures and unknown charges remain; original budgets, access and output cap are retained.")
            if allowances:
                entries = app.ledger.entries
                recorded_usage = {dimension: sum(entry.actual.get(dimension, 0) for entry in entries
                    if entry.status in {"settled", "unknown"}) for dimension in allowances}
                make_console().print(Panel(Text(
                    f"Authorization ID: {authorization_id}\nReason: {authorization_reason}\n"
                    f"Recorded cumulative usage (requested dimensions): {recorded_usage}\n"
                    f"Unknown usage dimensions: {app.ledger.unknown_dimensions()}\n"
                    f"New remaining allowance: {allowances}\n"
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
            make_console().print(Panel(f"Goal: {args.request}\nRead-only sources: {args.source}\n"
                f"Model: {args.model}\nAllowance: {args.max_turns} requests / {args.max_tokens} tokens\n"
                f"Public source access: {args.allow_network}\n"
                f"Rendered-image inspection: {args.allow_vision} (named vision route)\n"
                f"Image submissions: {args.max_images} (named image route)\n"
                f"Process proposals: {args.allow_execution}; commands need approval, not an OS sandbox.",
                title="Task / 任务"))
            if not args.yes and not args.prepare_only:
                if not interactive or input("Start / 开始? [y/N]: ").strip().lower() not in {"y", "yes"}:
                    return
            root = getattr(args, '_native_root', None) or new_research_session_root(args.output_root, args.request)
            app = ToolApplication.create(root, args.request, **native_creation_options(args))
            feedback = ""
        print_line(f"Task session: {app.session.store.root}")
        print_line(f"Saved process allowance (including search backends): {app.ledger.limits.get('process_invocations')}; "
                   f"wall seconds: {app.ledger.limits.get('process_wall_seconds')}. Resume does not reset it.")
        print_line("Resume: simple-ar start --resume-task SESSION [--approve-execution TOKEN]; append a request for feedback.")
        if args.prepare_only:
            return

        def approve(call: dict[str, Any], token: str) -> bool:
            print_line(f"Execution proposal ({call['function']['name']}): {call['function']['arguments']}")
            if call['function']['name'] in {'prepare_code_task', 'implement_code_task'}:
                print_line(f"Frozen CodeTask contract: {app.implementation.exact.model_dump()}")
                exact = app.implementation.exact
                repairs = exact.preparation.max_repairs if exact.preparation is not None else 1
                print_line('Approval binds the project inventory, protected validator files and process environment. '
                    + ('Preparation only; implementation and each formal measurement require separate approval.'
                       if call['function']['name'] == 'prepare_code_task' else f'Includes at most {repairs} technical repairs.'))
            print_line("Execution is not sandboxed. Inspect the generated code before approving.")
            return interactive and input("Execute this command / 执行此命令? [y/N]: ").strip().lower() in {"y", "yes"}

        status = app.run(feedback=feedback, approved_execution=args.approve_execution,
                         retry_model=args.retry_model,
                         retry_model_selector=replacement_model if args.resume_task else '',
                         read_only_sources=tuple(args.source) if args.resume_task else (),
                         allow_execution=args.allow_execution if args.resume_task else False,
                         allow_network=args.allow_network if args.resume_task else False,
                         allow_vision=args.allow_vision if args.resume_task else False,
                         resource_allowances=allowances or None,
                         authorization_id=authorization_id, authorization_reason=authorization_reason,
                         decision=decision,
                         approve=approve if interactive else None, progress=print_line)
        if (args.resume_task and app.state['config'].get('model') is None
                and not feedback and not args.retry_model and not allowances
                and not (args.source or args.allow_execution or args.allow_network or args.allow_vision)):
            completion = app.measurement_completion_actions()
            if completion:
                status = app.run(actions=completion, progress=print_line)
        _render_native_result(app, status)
    except (OSError, ValueError, RuntimeError, LLMError) as exc:
        raise SystemExit(f"Native task stopped; saved operations are not replayed: {exc}") from exc


if __name__ == "__main__":
    main()
