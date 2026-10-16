from __future__ import annotations

import argparse
from pathlib import Path

from simple_ar.code_task.runtime.config import CodeTaskConfigError, parse_metric_direction_arg


def build_parser(
    *, research_defaults: dict | None = None,
    allow_resume_without_topic: bool = False,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simple-ar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    models = subparsers.add_parser("models", help="Inspect named model connections locally; no API calls.")
    models.add_argument("--config", type=Path, help="Model catalog TOML; otherwise SIMPLE_AR_MODELS_CONFIG or user config.")
    models.add_argument("--profile", help="Inspect one profile name, without exposing its credential.")
    models.add_argument("--purpose", choices=("text", "code", "writing", "vision", "image"), default="text")

    from simple_ar.cli.start import add_start_parser
    add_start_parser(subparsers)
    results = subparsers.add_parser("results", help="List current reusable session deliveries without executing a task.")
    results.add_argument("session", type=Path)
    image = subparsers.add_parser("image", help="Generate or edit one concept image with a named Images profile; preserve each version separately.")
    image.add_argument("--prompt", required=True)
    image.add_argument("--input", type=Path, help="Existing image to edit; the original is never overwritten.")
    image.add_argument("--output", type=Path, required=True, help="New version directory, or identical saved request to recover without another call.")
    image.add_argument("--config", type=Path, help="Named model catalog TOML.")
    image.add_argument("--model", help="Named selector, e.g. profile:illustration; default image route.")
    image_review = subparsers.add_parser("image-review", help="Inspect rendered figures with a named vision model; save actionable feedback without editing originals.")
    image_review.add_argument("--input", type=Path, action="append", required=True, help="Rendered PNG/JPEG/WebP; repeat for up to four images.")
    image_review.add_argument("--goal", required=True, help="What the figure must communicate, including visual constraints.")
    image_review.add_argument("--output", type=Path, required=True, help="New review version directory; identical completed inputs recover without another call.")
    image_review.add_argument("--config", type=Path, help="Named model catalog TOML.")
    image_review.add_argument("--model", help="Named vision selector; default vision route.")

    preparation_parser = subparsers.add_parser("project-info", help="Read project instructions, dependency declarations, entry candidates and named data locations without execution.")
    preparation_parser.add_argument("--project", type=Path, required=True)
    preparation_parser.add_argument("--data-path", action="append", default=[], type=Path, help="Named data location; relative paths use the project root. Contents are not read.")
    preparation_parser.add_argument("--output", type=Path, required=True, help="New directory for editable preparation notes and facts.")

    export_parser = subparsers.add_parser(
        "report-export", help="Export an assembled report as an editable ACM acmart manuscript."
    )
    export_parser.add_argument("--report-dir", type=Path, required=True,
                               help="Directory containing report_body.md and references.bib.")
    export_parser.add_argument("--output", type=Path, required=True, help="New export directory.")
    export_parser.add_argument("--title", help="Optional reader-facing document title.")
    export_parser.add_argument("--compile", action="store_true", help="Also compile with installed pdflatex/bibtex; retain source and logs on failure.")




    session_parser = subparsers.add_parser(
        "research-session",
        aliases=["research-brief"],
        help="Run a bounded literature-only or literature-to-experiment research session.",
    )
    session_parser.add_argument("--config", type=Path, help="Research TOML; explicit CLI options override file values.")
    session_parser.add_argument("--report-writing-method", choices=("article", "sections"), default=None,
        help="New reports use a complete article work unit; explicit section/review controls retain sections. Saved sessions keep their method.")
    session_parser.add_argument("--session-root", type=Path, help="Continue this session; unchanged evidence and measurements are reused, while explicitly revised inputs invalidate only dependent results.")
    session_parser.add_argument(
        "--recover-interrupted", action="store_true",
        help="With --session-root, confirm the prior worker has stopped, close its running attempt with no result, then resume saved work. Never use while that worker is still active.",
    )
    session_parser.add_argument("--reanalyze", action="store_true", help="With --session-root, reconsider existing measurements and resume the research decision; do not request a report or rerun experiments.")
    session_parser.add_argument("--authorization-id", default=None, help="Stable idempotency key required for explicit continuation allowances.")
    session_parser.add_argument("--authorization-reason", default=None, help="Human-readable reason recorded with a continuation allowance.")
    session_parser.add_argument("--authorize-remaining", action="append", default=None, metavar="DIMENSION=AMOUNT", help="Authorize a new remaining resource allowance; repeat for multiple dimensions.")
    session_parser.add_argument("--additional-attempts", type=int, default=0, help="Increase the total attempt cap by this amount; prior attempts are retained.")
    session_parser.add_argument("--additional-no-progress", type=int, default=0, help="Increase the consecutive no-progress cap by this amount.")
    session_parser.add_argument(
        "--interaction", choices=("assisted", "checkpoints", "autonomous"), default=None,
        help="New sessions default to checkpoints; an explicit resume changes only future, unexecuted decisions.",
    )
    session_parser.add_argument("--decision-id", default=None, help="Id of the pending decision shown by research-session.")
    session_parser.add_argument("--decision-response", choices=("accept", "reject", "revise"), default=None)
    session_parser.add_argument("--decision-guidance", default=None, help="User-provided facts or revised direction; valid with --decision-response revise.")
    session_parser.add_argument(
        "--topic",
        required=not (allow_resume_without_topic or bool(research_defaults and research_defaults.get("topic"))),
    )
    session_parser.add_argument(
        "--task-kind", choices=("auto", "survey", "bug_fix", "measurement", "reproduction", "writing", "data_analysis"), default="auto",
        help="Task-driven path: survey reads evidence; bug_fix patches a project; measurement analyzes a command; reproduction reads supplied papers then checks a fixed explicit protocol without innovation.",
    )
    session_parser.add_argument("--outputs", nargs="+", choices=("summary", "report", "experiments", "bug_fix", "code_task", "data_analysis"))
    from simple_ar.cli.start import add_data_options
    add_data_options(session_parser)
    session_parser.add_argument("--material", action="append", default=[],
                                help="Writing: text/PDF or table_analysis.v1 analysis.json with copied data; not a verified experiment or bibliographic paper.")
    session_parser.add_argument("--data-path", action="append", default=[], type=Path,
                                help="Reproduction/measurement: named input data file or directory; records an external read-only asset, without automatic copying or command rewriting.")
    session_parser.add_argument("--total-tokens", type=int, default=None, help="Optional session API token budget; omitted means unlimited.")
    session_parser.add_argument("--llm-requests", type=int, default=None, help="Optional session API request budget; omitted means unlimited.")
    session_parser.add_argument("--max-output-tokens", type=int, default=None)
    session_parser.add_argument(
        "--max-section-tokens",
        type=int,
        default=None,
        help="Report Writer/Reviewer output cap per section; use 0 to omit the cap.",
    )
    session_parser.add_argument("--process-invocations", type=int, default=None)
    session_parser.add_argument("--process-wall-seconds", type=int, default=None)
    session_parser.add_argument(
        "--model",
        default=None,
        help="Optional model override; enables LLM-backed planning, reading, synthesis, and analysis.",
    )
    session_parser.add_argument(
        "--feasibility-review-model",
        default=None,
        help="Optional independent model for source-backed implementation feasibility review.",
    )
    session_parser.add_argument(
        "--output-root",
        default="runs/research-session",
        help="Parent directory for the timestamped research session.",
    )
    session_parser.add_argument(
        "--cache-dir",
        default=None,
        help="Optional shared full-text cache directory; omitted uses the session directory.",
    )
    session_parser.add_argument(
        "--local-document",
        action="append",
        default=[],
        help="Local Markdown or text document; may be repeated.",
    )
    session_parser.add_argument(
        "--query",
        dest="queries",
        action="append",
        default=[],
        help="Search query override; may be repeated.",
    )
    session_parser.add_argument(
        "--provider",
        dest="providers",
        action="append",
        default=[],
        help="Search provider override; may be repeated.",
    )
    session_parser.add_argument("--max-results", type=int, default=10)
    session_parser.add_argument("--max-chunks", type=int, default=300)
    session_parser.add_argument("--max-fulltext-documents", dest="research_max_fulltext_documents", type=int,
                                default=None, help="Maximum remote full-text documents to fetch; omit for no document count cap.")
    session_parser.add_argument("--max-pdf-mb", dest="research_max_pdf_mb", type=int,
                                default=None, help="Maximum size of each fetched PDF in MiB; omit for no PDF size cap.")
    session_parser.add_argument("--web-extract-backend", dest="research_web_extract_backend",
                                choices=("direct", "tavily_basic"), default=None)
    session_parser.add_argument("--idea-limit", type=int, default=3)
    session_parser.add_argument(
        "--max-research-iterations", type=int, default=1,
        help="Maximum evidence-driven research rounds after the first analysis; zero stops after the first analysis.",
    )
    session_parser.add_argument("--cwd", default=".")
    session_parser.add_argument(
        "--timeout-sec",
        type=int,
        default=None,
        help="Experiment timeout; for --code-task-config, overrides [execute].timeout_sec.",
    )
    session_parser.add_argument("--label", default="research-session")
    session_parser.add_argument("--primary-metric", default=None)
    session_parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="Required metric name; may be repeated.",
    )
    session_parser.add_argument(
        "--metric-direction",
        action="append",
        default=[],
        metavar="NAME=DIRECTION",
        help="Metric direction, such as accuracy=higher; may be repeated.",
    )
    session_parser.add_argument(
        "--with-report",
        action="store_true",
        help=(
            "After a passed session, append the existing report and audit attempts. "
            "This is the default when --model is supplied, except data_analysis which requires an explicit report request."
        ),
    )
    session_parser.add_argument(
        "--no-report",
        action="store_true",
        help="Skip report generation for a model-backed session (debug/fast mode).",
    )
    session_parser.add_argument(
        "--report-template",
        default="auto",
        help="Report template; auto selects a structure from the research evidence and goal judgment.",
    )
    session_parser.add_argument(
        "--report-reviewer",
        choices=("llm", "disabled"),
        default="llm",
        help="Report reviewer used with --with-report.",
    )
    session_parser.add_argument(
        "--max-review-iterations",
        type=int,
        default=1,
        help="Maximum report revision cycles per section with --with-report.",
    )
    _add_report_document_args(session_parser, prefix="report-")
    session_parser.add_argument(
        "--code-task-config",
        default=None,
        help=(
            "Optional Code-Task TOML. When supplied, the existing Code-Task "
            "backend implements the selected research direction in this session."
        ),
    )
    session_parser.add_argument(
        "--command",
        dest="command_argv",
        nargs=argparse.REMAINDER,
        required=False,
        help=(
            "Command to execute; place this option last. Omit it for a "
            "literature-only summary/report or when using --code-task-config."
        ),
    )

    if research_defaults:
        session_parser.set_defaults(**research_defaults)

    continuation_parser = subparsers.add_parser(
        "research-session-continue",
        help="Run one bounded recovery experiment in an existing research session.",
    )
    continuation_parser.add_argument(
        "--session-root",
        required=True,
        help="Existing research-session directory with a failed experiment handoff.",
    )
    continuation_parser.add_argument(
        "--parent-attempt-id",
        default=None,
        help="Failed experiment attempt to branch from; defaults to the latest canonical experiment (or experiment-001 for legacy sessions).",
    )
    continuation_parser.add_argument(
        "--model",
        default=None,
        help="Optional model override for the recovery result-analysis step.",
    )
    continuation_parser.add_argument("--cwd", default=".")
    continuation_parser.add_argument("--timeout-sec", type=int, default=300)
    continuation_parser.add_argument("--label", default="research-session-recovery")
    continuation_parser.add_argument("--primary-metric", default=None)
    continuation_parser.add_argument(
        "--metric",
        action="append",
        default=[],
        help="Required metric name; may be repeated.",
    )
    continuation_parser.add_argument(
        "--metric-direction",
        action="append",
        default=[],
        metavar="NAME=DIRECTION",
        help="Metric direction, such as accuracy=higher; may be repeated.",
    )
    continuation_parser.add_argument(
        "--command",
        dest="command_argv",
        nargs=argparse.REMAINDER,
        required=True,
        help="Revised command to execute; place this option last.",
    )

    migration_parser = subparsers.add_parser(
        "research-session-migrate",
        help="Create a canonical successor session from a read-only v1 session.",
    )
    migration_parser.add_argument(
        "--source-root",
        required=True,
        help="Existing session directory containing session_manifest.v1.",
    )
    migration_parser.add_argument(
        "--destination-root",
        required=True,
        help="New, empty directory for the canonical successor session.",
    )
    migration_parser.add_argument(
        "--artifact",
        dest="artifact_names",
        action="append",
        default=[],
        help="State reference to copy; may be repeated. Only explicitly named small files are copied.",
    )
    migration_parser.add_argument(
        "--requested-output",
        dest="requested_outputs",
        action="append",
        default=[],
        help="Requested successor output; may be repeated. Defaults to the legacy brief or research_summary.",
    )
    migration_parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="Maximum size of each explicitly selected imported artifact.",
    )

    report_parser = subparsers.add_parser(
        "research-report",
        help="Generate and audit a report from a completed research session.",
    )
    report_parser.add_argument(
        "--session-root",
        required=True,
        help="Existing research-session directory with a ready-for-report handoff.",
    )
    report_parser.add_argument(
        "--refresh", action="store_true",
        help="Write new report attempts from existing evidence; retain old reports and do not rerun experiments.",
    )
    report_parser.add_argument(
        "--model",
        required=True,
        help="Model used by the existing report Writer/Reviewer agent.",
    )
    report_parser.add_argument(
        "--template",
        default="experiment",
        help="Report template name or Markdown path; defaults to experiment.",
    )
    report_parser.add_argument(
        "--reviewer",
        choices=("llm", "disabled"),
        default="llm",
        help="Keep the existing reviewer loop or disable revision while retaining audit.",
    )
    report_parser.add_argument(
        "--max-review-iterations",
        type=int,
        default=1,
        help="Maximum Writer revision cycles per section.",
    )
    _add_report_document_args(report_parser)
    report_parser.add_argument(
        "--max-section-tokens",
        type=int,
        default=None,
        help="Report Writer/Reviewer output cap per section; use 0 to omit the cap.",
    )



    status_parser = subparsers.add_parser("status", help="Show run status.")
    status_parser.add_argument("run_dir")

    tools_parser = subparsers.add_parser("tools", help="Inspect or serve run-local SimpleAutoResearch tools.")
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_schema = tools_subparsers.add_parser("schema", help="Export OpenAI or MCP tool schemas.")
    tools_schema.add_argument("--format", choices=("mcp", "openai"), default="mcp")
    tools_schema.add_argument("--output", default=None)
    tools_call = tools_subparsers.add_parser("call", help="Call one run-local read-only tool.")
    tools_call.add_argument("run_dir")
    tools_call.add_argument("tool_name")
    tools_call.add_argument("--args-json", default="{}")
    tools_call.add_argument("--args-file", default=None, help="Read tool arguments from a JSON file.")
    tools_call.add_argument("--debug-payloads", action="store_true")
    tools_mcp = tools_subparsers.add_parser("serve-mcp", help="Serve read-only run tools over MCP stdio.")
    tools_mcp.add_argument("run_dir")
    tools_mcp.add_argument("--debug-payloads", action="store_true")

    code_task_parser = subparsers.add_parser(
        "code-task",
        help="Work with an existing codebase in an isolated run workspace.",
    )
    code_task_subparsers = code_task_parser.add_subparsers(
        dest="code_task_command",
        required=True,
    )
    code_task_init = code_task_subparsers.add_parser(
        "init",
        help="Prepare a code-task workspace and build a code index.",
    )
    code_task_init.add_argument(
        "--config",
        default=None,
        help="Optional TOML config file for code-task init settings.",
    )
    code_task_init.add_argument("--code-root", default=None)
    code_task_init.add_argument(
        "--kind",
        choices=("existing_project", "greenfield"),
        default=None,
        help="Explicit structured compatibility: existing_project or greenfield with its saved scope/benchmark/planning contract. For goal-driven creation use start GOAL.",
    )
    code_task_init.add_argument("--task-file", default=None)
    code_task_init.add_argument("--output-root", default=None)
    code_task_init.add_argument("--name", default=None)
    code_task_init.add_argument("--benchmark-command", default=None)
    code_task_init.add_argument(
        "--primary-metric",
        default=None,
        help=(
            "Primary benchmark metric for before/after verdicts, for example "
            "`accuracy` or `macro_f1`."
        ),
    )
    code_task_init.add_argument(
        "--metric-direction",
        action="append",
        default=[],
        type=_metric_direction_arg,
        metavar="METRIC=DIRECTION",
        help=(
            "Metric direction for comparison. Direction aliases include "
            "higher, lower, resource, and ignore. May be repeated."
        ),
    )
    _add_code_task_env_args(code_task_init)
    _add_code_task_workspace_args(code_task_init)
    code_task_init.add_argument(
        "--max-file-bytes",
        type=int,
        default=None,
        help="Maximum file size copied in copy/sparse modes. Use 0 to disable.",
    )
    code_task_probe = code_task_subparsers.add_parser(
        "probe",
        help="Inspect the workspace runtime and project environment.",
    )
    code_task_probe.add_argument("run_dir")
    _add_code_task_env_args(code_task_probe)

    code_task_map = code_task_subparsers.add_parser(
        "map",
        help="Build or refresh layered repo-map artifacts for a code-task run.",
    )
    code_task_map.add_argument("run_dir")
    code_task_map.add_argument(
        "--no-refresh-index",
        action="store_true",
        help="Reuse the existing codebase_index.json instead of scanning the current workspace.",
    )
    code_task_map.add_argument(
        "--show-summary",
        action="store_true",
        help="Print repo_map_summary.md after writing it.",
    )

    code_task_locate = code_task_subparsers.add_parser(
        "locate",
        help="Rank likely editable files and read-only evidence from the repo map.",
    )
    code_task_locate.add_argument("run_dir")
    code_task_locate.add_argument(
        "--query",
        default=None,
        help="Optional locate query. Defaults to code_task/task.md.",
    )
    code_task_locate.add_argument(
        "--top-k",
        type=int,
        default=8,
        help="Maximum candidates kept in each editable/evidence group.",
    )
    code_task_locate.add_argument(
        "--refresh-map",
        action="store_true",
        help="Rebuild codebase_index.json and repo_map.json before locating files.",
    )
    code_task_locate.add_argument(
        "--no-read-only",
        action="store_true",
        help="Omit protected read-only evidence files from locate results.",
    )
    code_task_locate.add_argument(
        "--show-summary",
        action="store_true",
        help="Print locate_results.md after writing it.",
    )

    code_task_context = code_task_subparsers.add_parser(
        "context",
        help="Build a bounded prompt context pack from locate results.",
    )
    code_task_context.add_argument("run_dir")
    code_task_context.add_argument(
        "--query",
        default=None,
        help="Optional locate query. Defaults to code_task/task.md.",
    )
    code_task_context.add_argument(
        "--top-k",
        type=int,
        default=8,
        help="Candidate budget passed to locate for each candidate group.",
    )
    code_task_context.add_argument(
        "--max-files",
        type=int,
        default=8,
        help="Maximum snippets included in the context pack.",
    )
    code_task_context.add_argument(
        "--max-source-chars-per-file",
        type=int,
        default=4000,
        help="Per-file source snippet character budget.",
    )
    code_task_context.add_argument(
        "--max-total-chars",
        type=int,
        default=20000,
        help="Total source snippet character budget.",
    )
    code_task_context.add_argument(
        "--refresh-map",
        action="store_true",
        help="Rebuild codebase_index.json and repo_map.json before locating files.",
    )
    code_task_context.add_argument(
        "--show-prompt",
        action="store_true",
        help="Print prompt_context.md after writing it.",
    )

    code_task_work_plan = code_task_subparsers.add_parser(
        "work-plan",
        help="Generate a batch-oriented implementation work plan for a code-task run.",
    )
    code_task_work_plan.add_argument("run_dir")
    code_task_work_plan.add_argument("--model", default=None)
    code_task_work_plan.add_argument("--no-llm", action="store_true")
    code_task_work_plan.add_argument("--force", action="store_true")
    code_task_work_plan.add_argument(
        "--allow-planning-fallback",
        action="store_true",
        help="Allow offline fallback after a known complete work-plan format failure, not transport failure.",
    )
    code_task_work_plan.add_argument(
        "--llm-retry-attempts",
        type=int,
        default=1,
        help="Completed-response work-plan corrections, including the first attempt; transport retries use the model connection.",
    )
    code_task_work_plan.add_argument("--max-files", type=int, default=8)
    code_task_work_plan.add_argument("--max-source-chars-per-file", type=int, default=2500)

    code_task_batch = code_task_subparsers.add_parser(
        "batch",
        help="Create an attempt/batch state directory for one work-plan item.",
    )
    code_task_batch.add_argument("run_dir")
    code_task_batch.add_argument(
        "--work-item",
        required=True,
        help="Work-plan item id to execute, for example W1.",
    )
    code_task_batch.add_argument(
        "--attempt-id",
        default=None,
        help="Optional attempt id such as attempt-001. Defaults to the active attempt.",
    )
    code_task_batch.add_argument(
        "--force",
        action="store_true",
        help="Create a new batch even if the active attempt already has one for this item.",
    )

    code_task_plan = code_task_subparsers.add_parser(
        "plan",
        help="Generate a human-reviewable patch plan for a code-task run.",
    )
    code_task_plan.add_argument("run_dir")
    code_task_plan.add_argument("--model", default=None)
    code_task_plan.add_argument("--no-llm", action="store_true")
    code_task_plan.add_argument("--force", action="store_true")
    code_task_plan.add_argument(
        "--allow-planning-fallback",
        action="store_true",
        help="Allow offline fallback after a known complete patch-plan format failure, not transport failure.",
    )
    code_task_plan.add_argument(
        "--llm-retry-attempts",
        type=int,
        default=1,
        help="Completed-response patch-plan corrections, including the first attempt; transport retries use the model connection.",
    )
    code_task_plan.add_argument("--max-files", type=int, default=8)
    code_task_plan.add_argument("--max-source-chars-per-file", type=int, default=2500)

    code_task_decide = code_task_subparsers.add_parser(
        "decide-plan",
        help="Record a human decision for the current code-task patch plan.",
    )
    code_task_decide.add_argument("run_dir")
    code_task_decide.add_argument(
        "--decision",
        required=True,
        choices=("approve", "reject", "revise"),
    )
    code_task_decide.add_argument("--note", default="")
    code_task_decide.add_argument("--reviewer", default="user")

    code_task_propose = code_task_subparsers.add_parser(
        "propose-edits",
        help="Ask the model to propose controlled old/new text edits.",
    )
    code_task_propose.add_argument("run_dir")
    code_task_propose.add_argument("--model", default=None)
    code_task_propose.add_argument("--no-llm", action="store_true")
    code_task_propose.add_argument("--force", action="store_true")
    code_task_propose.add_argument("--max-files", type=int, default=8)
    code_task_propose.add_argument("--max-source-chars-per-file", type=int, default=4000)
    code_task_propose.add_argument(
        "--allow-large-edits",
        action="store_true",
        help="Accept proposals that exceed the normal budget but fit the large budget.",
    )

    code_task_apply = code_task_subparsers.add_parser(
        "apply-edits",
        help="Safely apply controlled old/new text edits to the workspace.",
    )
    code_task_apply.add_argument("run_dir")
    code_task_apply.add_argument("--edits-file", default=None)
    code_task_apply.add_argument(
        "--allow-unapproved-plan",
        action="store_true",
        help="Bypass the human approval gate. Intended only for local experiments/tests.",
    )
    code_task_apply.add_argument(
        "--allow-large-edits",
        action="store_true",
        help="Apply a reviewed proposal that requires large-edit approval.",
    )

    code_task_validate = code_task_subparsers.add_parser(
        "validate",
        help="Run lightweight static validation over the code-task workspace.",
    )
    code_task_validate.add_argument("run_dir")
    code_task_validate.add_argument("--strict", action="store_true")
    code_task_validate.add_argument("--max-file-bytes", type=int, default=500_000)

    code_task_baseline = code_task_subparsers.add_parser(
        "baseline",
        help="Run the recorded benchmark before applying a patch.",
    )
    code_task_baseline.add_argument("run_dir")
    code_task_baseline.add_argument("--command", dest="benchmark_command", default=None)
    code_task_baseline.add_argument("--timeout", type=int, default=60)
    code_task_baseline.add_argument("--skip-validation", action="store_true")
    _add_code_task_env_args(code_task_baseline)

    code_task_run = code_task_subparsers.add_parser(
        "run",
        help="Run the recorded benchmark command in the code-task workspace.",
    )
    code_task_run.add_argument("run_dir")
    code_task_run.add_argument("--command", dest="benchmark_command", default=None)
    code_task_run.add_argument("--timeout", type=int, default=60)
    code_task_run.add_argument("--skip-validation", action="store_true")
    _add_code_task_env_args(code_task_run)

    code_task_analyze = code_task_subparsers.add_parser(
        "analyze-failure",
        help="Write a deterministic failure analysis from the latest benchmark run.",
    )
    code_task_analyze.add_argument("run_dir")

    code_task_repair = code_task_subparsers.add_parser(
        "repair",
        help="Propose bounded repair edits from the latest failure analysis.",
    )
    code_task_repair.add_argument("run_dir")
    code_task_repair.add_argument("--model", default=None)
    code_task_repair.add_argument("--no-llm", action="store_true")
    code_task_repair.add_argument("--max-files", type=int, default=8)
    code_task_repair.add_argument("--max-source-chars-per-file", type=int, default=4000)

    code_task_execute = code_task_subparsers.add_parser(
        "execute",
        help="Run a conservative state-aware code-task sequence.",
    )
    code_task_execute.add_argument("run_dir")
    code_task_execute.add_argument(
        "--config",
        default=None,
        help="Optional TOML config for execute model, budget, and runtime settings.",
    )
    code_task_execute.add_argument(
        "--to-step",
        choices=(
            "probe",
            "baseline",
            "work-plan",
            "batch",
            "plan",
            "propose-edits",
            "apply-edits",
            "review",
            "validate",
            "run",
            "analyze-failure",
            "repair",
        ),
        default=None,
        help="Last step execute may attempt.",
    )
    code_task_execute.add_argument("--dry-run", action="store_true")
    code_task_execute.add_argument("--model", default=None)
    code_task_execute.add_argument("--no-llm", action="store_true")
    code_task_execute.add_argument("--timeout", type=int, default=60)
    code_task_execute.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Auto-approve inline review gates in normal execute mode; with "
            "--interactive, auto-continue eligible primitive steps."
        ),
    )
    code_task_execute.add_argument(
        "--interactive",
        action="store_true",
        help="Debug mode: ask before each primitive step instead of running to the next review gate.",
    )
    code_task_execute.add_argument(
        "--no-review-inline",
        action="store_true",
        help="Disable inline review prompts and stop at review gates instead.",
    )
    code_task_execute.add_argument("--skip-validation", action="store_true")
    code_task_execute.add_argument("--strict-validation", action="store_true")
    code_task_execute.add_argument("--validation-max-file-bytes", type=int, default=500_000)
    code_task_execute.add_argument(
        "--apply-proposed-edits",
        action="store_true",
        help="Apply reviewed proposed_edits.json after plan approval.",
    )
    code_task_execute.add_argument(
        "--allow-large-edits",
        action="store_true",
        help="Allow execute to accept/apply proposals that exceed the normal edit budget.",
    )
    code_task_execute.add_argument(
        "--allow-planning-fallback",
        action="store_true",
        help=(
            "Allow deterministic offline work/patch plans if LLM planning fails. "
            "By default execute stops so the same command can retry cleanly."
        ),
    )
    code_task_execute.add_argument(
        "--planning-mode",
        choices=("tool_agent", "compact"),
        default=None,
        help="Greenfield complete architecture work unit (compact); tool_agent is a deprecated alias, with migration receipts for historical plans.",
    )
    code_task_execute.add_argument(
        "--llm-retry-attempts",
        type=int,
        default=None,
        help="Completed-response corrections for planning and greenfield generation; transport retries use the model connection.",
    )
    code_task_execute.add_argument(
        "--planning-review-rounds",
        type=int,
        default=None,
        help="Deprecated: planning review has been retired. Nonzero values emit a migration notice and grant no calls; use 0.",
    )
    code_task_execute.add_argument("--repair-rounds", type=int, default=None)
    code_task_execute.add_argument(
        "--reuse-planning-from",
        type=Path,
        default=None,
        help=(
            "Reuse an immutable greenfield planning snapshot from another run. This imports "
            "only accepted planning artifacts after verifying the task-contract hash; it never "
            "copies generated code, implementation memory, review findings, or repair history."
        ),
    )
    code_task_execute.add_argument(
        "--review-gate",
        choices=("strict", "runtime"),
        default=None,
        help=(
            "Greenfield review gate mode. `strict` blocks on all blocking review findings; "
            "`runtime` only blocks safety/direct executability issues and lets judge score packaging/report gaps."
        ),
    )
    code_task_execute.add_argument(
        "--baseline-policy",
        choices=("auto", "run", "skip", "provided", "none"),
        default=None,
        help=(
            "Existing-project baseline behavior: auto/run executes the unchanged "
            "benchmark, skip/none continues without it, provided records metrics "
            "from --baseline-metrics-file."
        ),
    )
    code_task_execute.add_argument(
        "--baseline-metrics-file",
        default=None,
        help="JSON or metric-line file used when --baseline-policy provided is selected.",
    )
    code_task_execute.add_argument("--max-files", type=int, default=8)
    code_task_execute.add_argument("--max-source-chars-per-file", type=int, default=4000)
    _add_code_task_env_args(code_task_execute)

    inspect_parser = subparsers.add_parser("inspect", help="Index and summarize run artifacts.")
    inspect_parser.add_argument("run_dir")

    search_parser = subparsers.add_parser(
        "search-artifacts",
        help="Search indexed run artifacts with lexical retrieval.",
    )
    search_parser.add_argument("run_dir")
    search_parser.add_argument("query")
    search_parser.add_argument("--top-k", type=int, default=8)
    search_parser.add_argument(
        "--include-operational",
        action="store_true",
        help="Also search runner metadata such as manifests and stage_meta.json.",
    )

    clean_parser = subparsers.add_parser(
        "clean",
        help="Review and clean rebuildable caches for one run or shared index store.",
    )
    clean_parser.add_argument(
        "run_dir",
        nargs="?",
        help="Run directory to clean. Omit when using --shared-index or --shared-cache.",
    )
    clean_parser.add_argument(
        "--yes",
        action="store_true",
        help="Delete the displayed cleanup targets without an interactive confirmation.",
    )
    clean_parser.add_argument(
        "--all-caches",
        action="store_true",
        help="Delete every known rebuildable cache/index/context artifact for this run.",
    )
    clean_parser.add_argument(
        "--shared-index",
        action="store_true",
        help=(
            "Dangerous: clear the shared research index store across runs "
            "(SQLite FTS/LanceDB accelerator data)."
        ),
    )
    clean_parser.add_argument(
        "--shared-cache",
        action="store_true",
        help=(
            "Strongest shared cleanup: clear shared research indexes and "
            "literature provider cache under .simple_ar_cache by default."
        ),
    )
    clean_parser.add_argument(
        "--index-root",
        default=None,
        help=(
            "Shared index root for --shared-index/--shared-cache. Defaults to "
            "SIMPLE_AR_RESEARCH_INDEX_ROOT or .simple_ar_cache/research_index."
        ),
    )
    clean_parser.add_argument(
        "--literature-cache-root",
        default=None,
        help=(
            "Literature cache root for --shared-cache. Defaults to "
            ".simple_ar_cache/literature."
        ),
    )
    clean_parser.add_argument(
        "--allow-external-index-root",
        action="store_true",
        help="Allow shared cleanup to touch a cache/index root outside the current workspace.",
    )

    return parser

def _add_report_document_args(parser: argparse.ArgumentParser, *, prefix: str = "") -> None:
    """Same report controls for new sessions and explicit report generation."""
    options = (
        ("document-review", {"action": argparse.BooleanOptionalAction,
            "help": "Enable bounded whole-document review and targeted revision."}),
        ("review-scope", {"choices": ("section", "document"),
            "help": f"Review each section or the complete body; document requires --{prefix}document-review."}),
        ("draft-scope", {"choices": ("section", "document", "auto"),
            "help": f"Draft by section, jointly, or auto (joint up to 2000 planned words; otherwise section checkpoints). document/auto require --{prefix}review-scope document and --{prefix}document-review."}),
        ("--max-document-review-prompt-chars", {"type": int,
            "help": "Optional complete review request character limit; 0 uses client/session capacity without truncating evidence."}),
        ("outline-strategy", {"choices": ("auto", "template", "adaptive"),
            "help": "Use the template or explicitly plan an evidence-organized outline for this new report."}),
        ("data-tables", {"choices": ("linked", "full"),
            "help": "Link complete copied analysis records (default) or repeat their full tables in the report."}),
    )
    for name, settings in options:
        parser.add_argument(name if name.startswith("--") else f"--{prefix}{name}", default=None, **settings)


def _add_code_task_env_args(parser: argparse.ArgumentParser) -> None:
    """Add shared code-task execution environment policy arguments."""
    parser.add_argument(
        "--env-mode",
        choices=("current", "external"),
        default=None,
        help=(
            "Execution environment mode. `current` uses the active "
            "SimpleAutoResearch Python; `external` uses --python. "
            "No dependencies are installed."
        ),
    )
    parser.add_argument(
        "--python",
        dest="python_executable",
        default=None,
        help="Python executable path or command name for --env-mode external.",
    )

def _add_code_task_workspace_args(parser: argparse.ArgumentParser) -> None:
    """Add shared code-task workspace creation arguments."""
    parser.add_argument(
        "--workspace-mode",
        choices=("auto", "copy", "git_worktree", "sparse_copy"),
        default=None,
        help=(
            "Workspace strategy. `auto` prefers git_worktree for Git projects "
            "and falls back to copy; `copy` copies a guarded source tree; "
            "`git_worktree` creates a detached git worktree; "
            "`sparse_copy` is experimental and copies selected patterns."
        ),
    )
    parser.add_argument(
        "--workspace-include",
        action="append",
        default=None,
        help=(
            "POSIX glob copied by --workspace-mode sparse_copy. Repeatable. "
            "Prefer TOML [workspace].include for multiple patterns."
        ),
    )
    parser.add_argument(
        "--workspace-exclude",
        action="append",
        default=None,
        help=(
            "Additional POSIX glob skipped by --workspace-mode sparse_copy. "
            "Repeatable."
        ),
    )
    parser.add_argument(
        "--workspace-reuse-source-venv",
        action="store_true",
        default=None,
        help=(
            "When a source .venv is detected, record and use its Python "
            "interpreter as the initial external execution policy."
        ),
    )



def _metric_direction_arg(value: str) -> tuple[str, str]:
    """Parse ``--metric-direction metric=direction`` arguments."""
    try:
        return parse_metric_direction_arg(value)
    except CodeTaskConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
