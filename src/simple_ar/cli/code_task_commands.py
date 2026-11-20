"""CLI command handlers for code-task workflows."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Any

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
    PatchValidationError,
    WorkspaceModeError,
    analyze_code_task_failure,
    apply_patch_edits,
    build_code_task_context_pack,
    build_code_task_repo_map,
    create_code_task_batch,
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
)
from simple_ar.code_task.orchestration.execute import execute_reviewed_code_task
from simple_ar.code_task.runtime.config import (
    CodeTaskConfigError,
    load_code_task_execute_options,
    load_code_task_init_options,
)
from simple_ar.core.artifacts import read_text
from simple_ar.core.console import print_line
from simple_ar.integrations.llm import LLMError


def _main_hook(name: str, default: Any) -> Any:
    """Resolve CLI view callbacks through simple_ar.cli.main when monkeypatched in tests."""
    main_mod = sys.modules.get("simple_ar.cli.main")
    if main_mod is not None and hasattr(main_mod, name):
        return getattr(main_mod, name)
    return default


def _format_status_number(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        sign = "+" if number > 0 else ""
        return f"{sign}{number:.6g}"
    return str(value)


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
    print_line(f"Resource ledger: {run_dir / 'budget_ledger.json'}")



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
    execution = options.execution_kwargs()
    execution.update(
        model=args.model or options.model,
        use_llm=options.use_llm and not args.no_llm,
        timeout_sec=args.timeout if args.timeout != 60 else options.timeout_sec,
        to_step=args.to_step or options.to_step,
        repair_rounds=options.repair_rounds if args.repair_rounds is None else args.repair_rounds,
        planning_review_rounds=(options.planning_review_rounds if args.planning_review_rounds is None
                                else args.planning_review_rounds),
        max_files=args.max_files if args.max_files != 8 else options.max_files,
        max_source_chars_per_file=(args.max_source_chars_per_file if args.max_source_chars_per_file != 4000
                                   else options.max_source_chars_per_file),
        validation_max_file_bytes=(args.validation_max_file_bytes if args.validation_max_file_bytes != 500_000
                                   else options.validation_max_file_bytes),
        env_mode=args.env_mode or options.env_mode,
        python_executable=args.python_executable or options.python_executable,
        allow_planning_fallback=args.allow_planning_fallback or options.allow_planning_fallback,
        planning_mode=args.planning_mode or options.planning_mode,
        llm_retry_attempts=args.llm_retry_attempts or options.llm_retry_attempts,
        baseline_policy=args.baseline_policy or options.baseline_policy,
        baseline_metrics_file=args.baseline_metrics_file or options.baseline_metrics_file,
        review_gate=args.review_gate or options.review_gate,
        skip_validation=args.skip_validation or options.skip_validation,
        strict_validation=args.strict_validation or options.strict_validation,
        direct_edit=False if args.interactive else None,
        approve_direct_edits=not args.interactive and args.yes and not args.no_review_inline,
        planning_snapshot_from=args.reuse_planning_from,
        dry_run=args.dry_run,
        message_callback=render_execute_message,
    )
    to_step = execution['to_step']

    run_dir = Path(args.run_dir)
    _main_hook('render_execute_header', render_execute_header)(
        run_dir,
        to_step=to_step,
        use_llm=execution['use_llm'],
        timeout_sec=execution['timeout_sec'],
        dry_run=args.dry_run,
    )

    def confirm_step(step: str) -> bool:
        render_step_preview(step)
        return _main_hook('confirm_next_step', confirm_next_step)(step)

    def confirm_action(action: str) -> bool:
        title, artifact, description, warning, prompt, stop = {
            "approve-plan": ("Patch Plan Review", "patch_plan.md",
                "approve this patch plan and continue to edit proposal generation",
                "Only approve if the plan matches the task, edit scope, benchmark, and expected files.",
                "Approve the patch plan and continue?", "patch-plan review"),
            "apply-proposal": ("Edit Proposal Review", "meta/proposed_edits.json",
                "apply the reviewed proposal, validate, and run the patched benchmark",
                "Only continue if the exact proposed edits match the approved scope.",
                "Apply the reviewed edit proposal and continue?", "edit-proposal review"),
            "allow-large-edits": ("Large Edit Review", "meta/proposed_edits.json",
                "allow the large reviewed proposal and continue applying it",
                "Large edits have wider blast radius. Continue only if the larger patch is intentional and fully reviewed.",
                "Allow this large edit proposal and continue?", "large-edit review"),
        }[action]
        render_review_gate(title=title, artifact=run_dir / "code_task" / artifact,
            action=description, warning=warning)
        if not _main_hook('confirm_review_gate', confirm_review_gate)(prompt, assume_yes=bool(args.yes)):
            print_line(f"Execute stopped at {stop}.")
            return False
        return True

    execute_reviewed_code_task(run_dir,
        confirm=confirm_action if _inline_review_enabled(args) else None,
        observe=_main_hook('render_execute_result', render_execute_result),
        **{
            **execution,
            'apply_proposed_edits': args.apply_proposed_edits or options.apply_proposed_edits,
            'allow_large_edits': args.allow_large_edits or options.allow_large_edits,
            **({'before_step': confirm_step} if args.interactive and not args.yes and not args.dry_run else {}),
        })



def _inline_review_enabled(args: argparse.Namespace) -> bool:
    """Return true when execute may ask inline review-gate questions."""

    return (
        not bool(args.dry_run)
        and not bool(args.no_review_inline)
        and not bool(args.interactive)
        and (bool(args.yes) or sys.stdin.isatty())
    )

