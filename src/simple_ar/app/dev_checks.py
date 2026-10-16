from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from typing import Sequence

from simple_ar.core.console import print_line


@dataclass(frozen=True)
class CheckGroup:
    """A named unittest target group used by the developer check runner.

    Args:
        description: Human-readable guidance shown by ``--list``.
        targets: Arguments passed after ``python -m unittest``.
    """

    description: str
    targets: tuple[str, ...]


CHECK_GROUPS: dict[str, CheckGroup] = {
    "core": CheckGroup(
        description="Core artifact, capability, attempt, and handoff-package contract tests.",
        targets=(
            "tests.test_budget",
            "tests.test_capabilities",
            "tests.test_capability_package_example",
            "tests.test_session_locking",
            "tests.test_session_transitions",
            "tests.test_public_api",
        ),
    ),
    "quick": CheckGroup(
        description="Fast sanity checks for contracts, config loading, metrics, prompts, and CLI parsing.",
        targets=(
            "tests.test_dev_checks",
            "tests.test_run_config",
            "tests.test_metrics",
            "tests.test_prompts",
            "tests.test_cli",
            "tests.test_structure_cleanup",
            "tests.test_architecture_boundaries",
        ),
    ),
    "code-task": CheckGroup(
        description="Code-task workflow and source-context behavior; select other affected modules for focused changes.",
        targets=("tests.test_code_task", "tests.test_code_source_views"),
    ),
    "code-task-examples": CheckGroup(
        description="Realistic bundled code-task example tests. Run after changing examples or benchmark behavior.",
        targets=("tests.test_code_task_examples",),
    ),
    "pipeline": CheckGroup(
        description="Experiment/template execution checks (legacy group name; requires the examples extra).",
        targets=(
            "tests.test_experiment_execution",
            "tests.test_experiment_runner",
        ),
    ),
    "research": CheckGroup(
        description="Literature, retrieval, evidence, LLM adapter, and report tests.",
        targets=(
            "tests.test_research_foundation",
            "tests.test_document_ingest",
            "tests.test_research_intake",
            "tests.test_read_boundary",
            "tests.test_search_registry",
            "tests.test_literature",
            "tests.test_retrieval",
            "tests.test_llm",
            "tests.test_idea_assessment",
            "tests.test_report",
            "tests.test_report_checkpoints",
            "tests.test_report_measurement_audit",
            "tests.test_search_capability",
            "tests.test_research_registry",
            "tests.test_document_ports",
            "tests.test_synthesis_capability",
            "tests.test_research_brief",
            "tests.test_research_application",
            "tests.test_research_session_application",
            "tests.test_research_report_application",
            "tests.test_research_code_task_application",
            "tests.test_analysis_capability",
            "tests.test_experiment_capability",
            "tests.test_report_ports",
            "tests.test_report_capability",
            "tests.test_vertical_capability_flow",
            "tests.test_literature_session_flow",
        ),
    ),
    "all": CheckGroup(
        description="Full discovery for shared-interface/architecture checkpoints and release candidates.",
        targets=("discover", "-s", "tests", "-t", "."),
    ),
    "application": CheckGroup(
        description="Research intake, application advancement, candidate assessment and recovery.",
        targets=("tests.test_research_intake", "tests.test_research_application", "tests.test_idea_assessment", "tests.test_research_design", "tests.test_session_migration", "tests.test_tool_agent_handoff"),
    ),
    "llm": CheckGroup(
        description="LLM transport, retries and budget accounting; no live provider calls.",
        targets=("tests.test_llm", "tests.test_budget"),
    ),
    "report": CheckGroup(
        description="Report assembly, writing/revision, audit, recovery and application smoke tests.",
        targets=("tests.test_report", "tests.test_report_checkpoints", "tests.test_report_measurement_audit", "tests.test_report_ports", "tests.test_report_capability", "tests.test_research_report_application",
                 "tests.test_report_document_first", "tests.test_report_narrative"),
    ),
    "intake": CheckGroup(
        description="Guided/chat setup and the shared CLI/TOML configuration, without live model calls.",
        targets=("tests.test_cli_start", "tests.test_intake_dialogue", "tests.test_research_config"),
    ),
    "data": CheckGroup(
        description="Table ingest, descriptive/paired/coordinate analysis, figures and writing handoff smoke tests.",
        targets=("tests.test_table_analysis", "tests.test_table_paired",
                 "tests.test_coordinate_figures", "tests.test_table_figures",
                 "tests.test_analysis_writing", "tests.test_report_data_delivery"),
    ),
    "execution": CheckGroup(
        description="Short real processes, experiment result boundaries and CodeTask progress relay.",
        targets=("tests.test_process_control", "tests.test_experiment_runner", "tests.test_experiment_execution", "tests.test_experiment_capability"),
    ),
}


def build_unittest_command(
    group_name: str | Sequence[str],
    *,
    verbose: bool = False,
    failfast: bool = False,
) -> list[str]:
    """Build one command, deduplicating modules across selected groups."""

    names = [group_name] if isinstance(group_name, str) else list(group_name)
    if "all" in names:
        targets = list(CHECK_GROUPS["all"].targets)
        if verbose:
            targets.append("-v")
        if failfast:
            targets.append("-f")
    else:
        targets = list(dict.fromkeys(target for name in names for target in CHECK_GROUPS[name].targets))
        prefix: list[str] = []
        if verbose:
            prefix.append("-v")
        if failfast:
            prefix.append("-f")
        targets = prefix + targets
    return [sys.executable, "-m", "unittest", *targets]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments for the developer check runner."""

    parser = argparse.ArgumentParser(
        prog="simple-ar-checks",
        description="Run layered SimpleAutoResearch developer test groups.",
    )
    parser.add_argument(
        "groups",
        nargs="*",
        choices=tuple(CHECK_GROUPS),
        help="Check group(s) to run. Use --list to see guidance.",
    )
    parser.add_argument("--list", action="store_true", help="List available check groups.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--verbose", action="store_true", help="Pass -v to unittest.")
    parser.add_argument("--failfast", action="store_true", help="Pass -f to unittest.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run one or more layered unittest groups."""

    args = parse_args(argv)
    if args.list:
        _print_groups()
        return 0
    groups = list(args.groups or ["quick"])
    command = build_unittest_command(
        groups, verbose=bool(args.verbose), failfast=bool(args.failfast),
    )
    print_line(f"[{', '.join(groups)}] {' '.join(command)}")
    if args.dry_run:
        return 0
    return int(subprocess.run(command, check=False).returncode)


def _print_groups() -> None:
    print_line("Available check groups:")
    for name, group in CHECK_GROUPS.items():
        print_line(f"- {name}: {group.description}")


if __name__ == "__main__":
    raise SystemExit(main())
