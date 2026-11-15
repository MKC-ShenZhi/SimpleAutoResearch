from __future__ import annotations

from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, read_jsonl, read_text, write_text
from simple_ar.code_task.editing.scope import is_protected_edit_path
from simple_ar.code_task.memory import code_task_memory_paths, _artifact_ref
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    uses_legacy_greenfield,
    utcnow_iso,
)


def write_code_task_summary(run_dir: Path) -> Path:
    """Write a human-readable summary for the current code-task state.

    Args:
        run_dir: Code-task run directory.

    Returns:
        Path to ``code_task/summary.md``.
    """
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    summary_path = paths.task_dir / "summary.md"

    task = _read_optional(paths.task_dir / "task.md")
    patch_plan = _read_optional(paths.task_dir / "patch_plan.md")
    patch_diff = _read_optional(paths.task_dir / "patch.diff")
    environment = _read_optional_json(paths.meta_dir / "environment_report.json")
    validation = _read_optional_json(paths.meta_dir / "validation_report.json")
    baseline_execution = _read_run_json(paths.run_artifact_dir, "baseline", "execution_report.json")
    baseline_metrics = _read_run_json(paths.run_artifact_dir, "baseline", "metrics.json")
    patched_execution = _read_run_json(paths.run_artifact_dir, "patched", "execution_report.json")
    patched_metrics = _read_run_json(paths.run_artifact_dir, "patched", "metrics.json")
    comparison = _read_optional_json(paths.run_artifact_dir / "comparison.json")
    failure = _read_latest_failure(paths.run_artifact_dir, manifest)
    memory_snapshot = _read_memory_snapshot(run_dir)

    write_text(
        summary_path,
        _render_summary(
            manifest=manifest,
            task=task,
            patch_plan=patch_plan,
            patch_diff=patch_diff,
            environment=environment,
            validation=validation,
            baseline_execution=baseline_execution,
            baseline_metrics=baseline_metrics,
            patched_execution=patched_execution,
            patched_metrics=patched_metrics,
            comparison=comparison,
            failure=failure,
            memory_snapshot=memory_snapshot,
        ),
    )
    _update_manifest(run_dir, manifest)
    return summary_path


def _render_summary(
    *,
    manifest: dict[str, Any],
    task: str,
    patch_plan: str,
    patch_diff: str,
    environment: dict[str, Any],
    validation: dict[str, Any],
    baseline_execution: dict[str, Any],
    baseline_metrics: dict[str, Any],
    patched_execution: dict[str, Any],
    patched_metrics: dict[str, Any],
    comparison: dict[str, Any],
    failure: str,
    memory_snapshot: dict[str, Any],
) -> str:
    changed_files = _changed_files(manifest)
    repair = manifest.get("repair", {})
    active_repair = _active_repair(repair)
    lines = [
        "# Code Task Summary",
        "",
        f"Status: `{manifest.get('status', 'unknown')}`",
        "",
        "## Result",
        "",
        _result_overview(
            manifest=manifest,
            environment=environment,
            validation=validation,
            baseline_execution=baseline_execution,
            patched_execution=patched_execution,
            comparison=comparison,
            failure=failure,
            changed_files=changed_files,
        ),
        "",
        "## Review and Repair Notes",
        "",
        _review_and_repair_notes(manifest, memory_snapshot),
        "",
        "## Task",
        "",
        _clip(_strip_heading(task), max_chars=1200) or "No task text was recorded.",
        "",
        "## Environment",
        "",
        _environment_summary(environment, manifest),
        "",
        "## Plan",
        "",
        _plan_status(manifest, patch_plan),
        "",
        "## Patch",
        "",
        _patch_summary(manifest, patch_diff, changed_files),
        "",
        "## Validation",
        "",
        _validation_summary(validation),
        "",
        "## Benchmark",
        "",
        _benchmark_summary(
            baseline_execution=baseline_execution,
            baseline_metrics=baseline_metrics,
            patched_execution=patched_execution,
            patched_metrics=patched_metrics,
            comparison=comparison,
        ),
    ]
    if failure:
        lines.extend(
            [
                "",
                "## Failure Analysis",
                "",
                _clip(_strip_heading(failure), max_chars=1800),
            ]
        )
    if active_repair:
        lines.extend(
            [
                "",
                "## Repair",
                "",
                _repair_summary(repair),
            ]
        )
    artifact_lines = [
        "- Workspace: `code_task/workspace`",
        "- Task contract: `code_task/meta/task_contract.json`",
        "- Contract coverage: `code_task/meta/task_contract_coverage.json`",
        "- Environment report: `code_task/meta/environment_report.json`",
        "- Analyzer registry: `code_task/meta/analyzer_registry.json`",
        "- Patch plan: `code_task/patch_plan.md`",
        "- Patch diff: `code_task/patch.diff`",
        "- Validation report: `code_task/meta/validation_report.json`",
        "- Baseline execution: `code_task/run/baseline/execution_report.json`",
        "- Patched execution: `code_task/run/patched/execution_report.json`",
        "- Stdout/stderr: `code_task/run/<label>/stdout.txt`, `code_task/run/<label>/stderr.txt`",
        "- Run history: `code_task/run/<label>/attempts/attempt-*/`",
    ]
    if comparison:
        artifact_lines.insert(-1, "- Comparison: `code_task/run/comparison.json`")
    if isinstance(repair, dict) and repair.get("latest_proposed_edits"):
        artifact_lines.insert(-1, f"- Repair proposal: `{repair.get('latest_proposed_edits')}`")
    memory_refs = memory_snapshot.get("artifact_refs")
    if isinstance(memory_refs, dict) and memory_snapshot.get("task_memory"):
        if memory_refs.get("task_memory_markdown"):
            artifact_lines.insert(-1, f"- Task memory: `{memory_refs.get('task_memory_markdown')}`")
        if memory_refs.get("repair_memory"):
            artifact_lines.insert(-1, f"- Repair memory: `{memory_refs.get('repair_memory')}`")
    lines.extend(["", "## Artifacts", "", *artifact_lines, ""])
    return "\n".join(lines)


def _result_overview(
    *,
    manifest: dict[str, Any],
    environment: dict[str, Any],
    validation: dict[str, Any],
    baseline_execution: dict[str, Any],
    patched_execution: dict[str, Any],
    comparison: dict[str, Any],
    failure: str,
    changed_files: list[str],
) -> str:
    lines = [
        f"- Outcome: {_outcome_text(baseline_execution, patched_execution, comparison, validation)}",
        f"- Next step: {_next_step(manifest, environment, validation, baseline_execution, patched_execution, comparison, failure)}",
    ]
    primary = _primary_metric_text(manifest, comparison)
    if primary:
        lines.append(f"- Primary metric: {primary}")
    if baseline_execution:
        lines.append(f"- Baseline status: `{baseline_execution.get('status', 'unknown')}`")
    baseline_policy = _baseline_policy_record(manifest)
    if baseline_policy and not baseline_execution:
        lines.append(
            "- Baseline policy: "
            f"`{baseline_policy.get('policy', 'unknown')}` "
            f"({baseline_policy.get('status', 'unknown')})"
        )
    if patched_execution:
        lines.append(f"- Patched status: `{patched_execution.get('status', 'unknown')}`")
    if changed_files:
        lines.append(f"- Changed files: `{len(changed_files)}`")
    risky_files = _review_sensitive_files(changed_files)
    if risky_files:
        lines.append(
            "- Review risk: patch changed test/benchmark files "
            + ", ".join(f"`{path}`" for path in risky_files)
            + ". Treat the result as requiring extra human review."
        )
    if comparison:
        reasons = comparison.get("reasons", [])
        if isinstance(reasons, list) and reasons:
            lines.append("- Evidence: " + "; ".join(str(item) for item in reasons[:3]))
    return "\n".join(lines)


def _review_and_repair_notes(manifest: dict[str, Any], memory_snapshot: dict[str, Any]) -> str:
    lines: list[str] = []
    repair_line = _attempted_repairs(memory_snapshot, manifest)
    if repair_line:
        lines.append(f"- Attempted repairs: {repair_line}")
    findings = memory_snapshot.get("review_findings")
    if isinstance(findings, list) and findings:
        lines.append("- Recent review findings:")
        for row in findings[-3:]:
            if not isinstance(row, dict):
                continue
            lines.append(
                "  - "
                f"`{row.get('severity', 'info')}` "
                f"{_clip(str(row.get('summary', '')), max_chars=240)}"
            )
    repairs = memory_snapshot.get("repair_memory")
    if isinstance(repairs, list) and repairs:
        latest = repairs[-1] if isinstance(repairs[-1], dict) else {}
        if latest:
            lines.append(
                "- Latest repair memory: "
                f"`{latest.get('status', 'noted')}` "
                f"{_clip(str(latest.get('failure_summary', '')), max_chars=240)}"
            )
    return "\n".join(lines)


def _attempted_repairs(memory_snapshot: dict[str, Any], manifest: dict[str, Any]) -> str:
    repair = manifest.get("repair")
    counts: list[str] = []
    if isinstance(repair, dict):
        for key, label in (
            ("repair_count", "existing-project"),
            ("review_repair_count", "review"),
            ("run_repair_count", "run"),
        ):
            if repair.get(key) is not None:
                counts.append(f"{label}={repair.get(key)}")
    rows = memory_snapshot.get("repair_memory")
    row_count = len(rows) if isinstance(rows, list) else 0
    if row_count:
        counts.append(f"memory_records={row_count}")
    return ", ".join(counts)


def _outcome_text(
    baseline_execution: dict[str, Any],
    patched_execution: dict[str, Any],
    comparison: dict[str, Any],
    validation: dict[str, Any],
) -> str:
    if comparison:
        return f"`{comparison.get('verdict', 'inconclusive')}` from baseline-vs-patched comparison."
    if patched_execution:
        return f"`{patched_execution.get('status', 'unknown')}` patched benchmark run; no comparison is available yet."
    if validation and validation.get("status") == "failed":
        return "`validation_failed`; benchmark should wait until errors are reviewed."
    if baseline_execution:
        return f"`{baseline_execution.get('status', 'unknown')}` baseline run; patch has not been benchmarked yet."
    return "`not_complete`; no benchmark evidence has been collected yet."


def _next_step(
    manifest: dict[str, Any],
    environment: dict[str, Any],
    validation: dict[str, Any],
    baseline_execution: dict[str, Any],
    patched_execution: dict[str, Any],
    comparison: dict[str, Any],
    failure: str,
) -> str:
    plan = manifest.get("plan", {})
    patch = manifest.get("patch", {})
    code_task = manifest.get("code_task", {})
    is_new_project = isinstance(code_task, dict) and str(code_task.get("kind", "")).lower() == "greenfield"
    direct_edit = isinstance(code_task, dict) and code_task.get('auto_edit_policy') == 'direct.v1'
    is_greenfield = uses_legacy_greenfield(manifest)
    plan_status = plan.get("status", "not_started") if isinstance(plan, dict) else "not_started"
    patch_status = patch.get("status", "not_started") if isinstance(patch, dict) else "not_started"
    validation_status = validation.get("status") if validation else ""
    if not environment:
        return "Run `simple-ar code-task probe <run-dir>` to record environment signals."
    if not baseline_execution and not is_new_project and not _baseline_not_required(manifest):
        return "Run `simple-ar code-task baseline <run-dir>` before asking for edits."
    if is_greenfield and patched_execution:
        patched_status = str(patched_execution.get("status", "unknown"))
        if patched_status == "passed":
            return "Review `code_task/summary.md`, `code_task/workspace/generated_project/`, and run artifacts."
        if failure:
            return "Review failure analysis and rerun `simple-ar code-task execute <run-dir> --repair-rounds 1` for a bounded generated-project repair."
        return "Rerun `simple-ar code-task execute <run-dir> --repair-rounds 1` to analyze and repair the generated benchmark failure."
    if is_greenfield and patch_status == "applied" and not validation:
        return "Run `simple-ar code-task execute <run-dir> --to-step run` to validate and benchmark the generated project."
    if is_greenfield and validation_status == "failed":
        return "Review `code_task/meta/validation_report.json`; rerun execute with repair budget if needed."
    if is_greenfield and not patched_execution:
        return "Run `simple-ar code-task execute <run-dir> --to-step run` to benchmark the generated project."
    if plan_status in {"not_started", "unknown"} and direct_edit and patch_status in {"not_started", "unknown"}:
        return "Run `simple-ar code-task execute <run-dir>` to propose the authorized work."
    if plan_status in {"not_started", "unknown"} and not direct_edit:
        return "Run `simple-ar code-task plan <run-dir>` to create a reviewable patch plan."
    if plan_status == "pending_approval":
        return "Review `code_task/patch_plan.md`, then run `simple-ar code-task decide-plan <run-dir> --decision approve|revise|reject`."
    if plan_status == "revision_requested":
        return "Revise the task or regenerate the patch plan with `simple-ar code-task plan <run-dir> --force`."
    if plan_status == "rejected":
        return "Stop this run or revise the task before generating edits."
    if patch_status in {"not_started", "unknown"}:
        return "Run `simple-ar code-task propose-edits <run-dir>` after plan approval."
    if patch_status == "edits_proposed":
        return "Review `code_task/meta/proposed_edits.json`, then run `simple-ar code-task apply-edits <run-dir>`."
    if patch_status == "applied" and not validation:
        return "Run `simple-ar code-task validate <run-dir>` before the patched benchmark."
    if validation_status == "failed":
        return "Review `code_task/meta/validation_report.json`; fix issues or request a repair proposal."
    if not patched_execution:
        return "Run `simple-ar code-task run <run-dir>` to benchmark the patched workspace."
    patched_status = str(patched_execution.get("status", "unknown"))
    if patched_status != "passed":
        if failure:
            if is_greenfield:
                return "Review failure analysis and rerun `simple-ar code-task execute <run-dir> --repair-rounds 1` for a bounded generated-project repair."
            return "Review failure analysis and consider `simple-ar code-task repair <run-dir>`."
        if is_greenfield:
            return "Rerun `simple-ar code-task execute <run-dir> --repair-rounds 1` to analyze and repair the generated benchmark failure."
        return "Run `simple-ar code-task analyze-failure <run-dir>` to summarize the benchmark failure."
    if comparison:
        verdict = str(comparison.get("verdict", "inconclusive"))
        if verdict == "improved":
            return "Review `summary.md`, `patch.diff`, and `comparison.json`; apply the patch to the original project only after manual review."
        if verdict in {"regressed", "mixed"}:
            return "Inspect `comparison.json`; a negative or mixed result is valid evidence, not an execution failure."
        return "Inspect `comparison.json`; add metric directions or a stronger benchmark if the verdict is inconclusive."
    if _baseline_not_required(manifest):
        return "Review patched benchmark artifacts; no baseline comparison was requested for this run."
    return "Run the baseline or patched benchmark again if comparison artifacts are missing."


def _baseline_not_required(manifest: dict[str, Any]) -> bool:
    record = _baseline_policy_record(manifest)
    policy = str(record.get("policy", "")).lower()
    status = str(record.get("status", "")).lower()
    return policy in {"skip", "none"} and status in {"skipped", "recorded"}


def _baseline_policy_record(manifest: dict[str, Any]) -> dict[str, Any]:
    benchmark = manifest.get("benchmark", {})
    if not isinstance(benchmark, dict):
        return {}
    record = benchmark.get("baseline_policy", {})
    return record if isinstance(record, dict) else {}


def _primary_metric_text(manifest: dict[str, Any], comparison: dict[str, Any]) -> str:
    metric_config = comparison.get("metric_config", {}) if isinstance(comparison, dict) else {}
    primary = ""
    if isinstance(metric_config, dict):
        primary = str(metric_config.get("primary_metric") or "").strip()
    benchmark = manifest.get("benchmark", {})
    if not primary and isinstance(benchmark, dict):
        primary = str(benchmark.get("primary_metric") or "").strip()
    if not primary:
        return ""
    direction = _configured_direction_text(primary, metric_config, benchmark)
    suffix = f" ({direction})" if direction else ""
    return f"`{primary}`{suffix}"


def _configured_direction_text(
    metric_name: str,
    metric_config: dict[str, Any],
    benchmark: object,
) -> str:
    for source in (
        metric_config.get("metric_directions") if isinstance(metric_config, dict) else {},
        benchmark.get("metric_directions") if isinstance(benchmark, dict) else {},
    ):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if str(key).lower() == metric_name.lower():
                return str(value)
    return ""


def _environment_summary(environment: dict[str, Any], manifest: dict[str, Any]) -> str:
    policy = _environment_policy(environment, manifest)
    if not environment and not policy:
        return "- Environment has not been probed yet."
    platform_data = environment.get("platform", {})
    python_data = environment.get("python", {})
    project = environment.get("project", {})
    gpu = environment.get("gpu", {})
    lines = [f"- Status: `{environment.get('status', 'not_probed')}`"]
    if policy:
        lines.append(f"- Mode: `{policy.get('mode', 'current')}`")
        lines.append(
            f"- Execution Python: `{policy.get('python_executable', 'unknown')}`"
        )
        lines.append(
            "- Dependency install: "
            f"`{policy.get('dependency_install', 'disabled')}`"
        )
    if isinstance(platform_data, dict):
        system = platform_data.get("system", "unknown")
        release = platform_data.get("release", "")
        machine = platform_data.get("machine", "")
        lines.append(f"- Platform: `{system} {release}` `{machine}`".strip())
    if isinstance(python_data, dict):
        lines.append(
            f"- Python: `{python_data.get('version', 'unknown')}` "
            f"at `{python_data.get('executable', 'unknown')}`"
        )
    if isinstance(gpu, dict):
        lines.append(f"- GPU devices: `{gpu.get('count', 0)}`")
    if isinstance(project, dict):
        dependency_files = project.get("dependency_files", [])
        test_dirs = project.get("test_dirs", [])
        if isinstance(dependency_files, list) and dependency_files:
            lines.append("- Dependency files: " + ", ".join(f"`{item}`" for item in dependency_files))
        if isinstance(test_dirs, list) and test_dirs:
            lines.append("- Test dirs: " + ", ".join(f"`{item}`" for item in test_dirs))
    warnings = environment.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.append("- Warnings: " + ", ".join(f"`{item}`" for item in warnings[:8]))
    return "\n".join(lines)


def _environment_policy(
    environment_report: dict[str, Any],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    environment = manifest.get("environment", {})
    if isinstance(environment, dict):
        policy = environment.get("policy")
        if isinstance(policy, dict):
            return policy
    policy = environment_report.get("execution_policy")
    return policy if isinstance(policy, dict) else {}


def _plan_status(manifest: dict[str, Any], patch_plan: str) -> str:
    plan = manifest.get("plan", {})
    status = plan.get("status", "not_started") if isinstance(plan, dict) else "not_started"
    mode = plan.get("mode", "unknown") if isinstance(plan, dict) else "unknown"
    selected = plan.get("selected_files", []) if isinstance(plan, dict) else []
    lines = [f"- Status: `{status}`", f"- Mode: `{mode}`"]
    if isinstance(selected, list) and selected:
        lines.append("- Context files: " + ", ".join(f"`{path}`" for path in selected))
    if patch_plan:
        lines.append("- Plan file exists: `code_task/patch_plan.md`")
    return "\n".join(lines)


def _patch_summary(
    manifest: dict[str, Any],
    patch_diff: str,
    changed_files: list[str],
) -> str:
    patch = manifest.get("patch", {})
    status = patch.get("status", "not_started") if isinstance(patch, dict) else "not_started"
    lines = [f"- Status: `{status}`"]
    if changed_files:
        lines.append("- Changed files: " + ", ".join(f"`{path}`" for path in changed_files))
        risky_files = _review_sensitive_files(changed_files)
        if risky_files:
            lines.append(
                "- Review risk: test/benchmark files changed: "
                + ", ".join(f"`{path}`" for path in risky_files)
            )
    if patch_diff:
        added = sum(
            1
            for line in patch_diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1
            for line in patch_diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )
        lines.append(f"- Diff size: `{added}` added line(s), `{removed}` removed line(s)")
    return "\n".join(lines)


def _validation_summary(validation: dict[str, Any]) -> str:
    if not validation:
        return "- No validation report has been written yet."
    lines = [
        f"- Status: `{validation.get('status', 'unknown')}`",
        f"- Errors: `{validation.get('error_count', 0)}`",
        f"- Warnings: `{validation.get('warning_count', 0)}`",
    ]
    issues = validation.get("issues", [])
    if isinstance(issues, list) and issues:
        lines.append("- First issues:")
        for issue in issues[:5]:
            if isinstance(issue, dict):
                lines.append(
                    f"  - `{issue.get('severity', 'issue')}` "
                    f"`{issue.get('code', '')}` in `{issue.get('path', '')}`: "
                    f"{issue.get('message', '')}"
                )
    return "\n".join(lines)


def _benchmark_summary(
    *,
    baseline_execution: dict[str, Any],
    baseline_metrics: dict[str, Any],
    patched_execution: dict[str, Any],
    patched_metrics: dict[str, Any],
    comparison: dict[str, Any],
) -> str:
    sections: list[str] = []
    if baseline_execution:
        sections.extend(
            [
                "### Baseline",
                "",
                _execution_summary(baseline_execution, baseline_metrics),
            ]
        )
    if patched_execution:
        if sections:
            sections.append("")
        sections.extend(
            [
                "### Patched",
                "",
                _execution_summary(patched_execution, patched_metrics),
            ]
        )
    if comparison:
        if sections:
            sections.append("")
        sections.extend(
            [
                "### Comparison",
                "",
                _comparison_summary(comparison),
            ]
        )
    if not sections:
        return "- Benchmark has not been executed yet."
    return "\n".join(sections)


def _comparison_summary(comparison: dict[str, Any]) -> str:
    lines = [
        f"- Verdict: `{comparison.get('verdict', 'inconclusive')}`",
        f"- Status: `{comparison.get('status', 'unknown')}`",
    ]
    reasons = comparison.get("reasons", [])
    if isinstance(reasons, list) and reasons:
        lines.append("- Reasons: " + "; ".join(str(item) for item in reasons[:4]))
    rows = comparison.get("metrics", [])
    if isinstance(rows, list) and rows:
        lines.append("")
        lines.append("| Metric | Baseline | Patched | Delta | Interpretation | Direction |")
        lines.append("| --- | ---: | ---: | ---: | --- | --- |")
        for row in rows:
            if not isinstance(row, dict):
                continue
            direction = str(row.get("direction", "unknown"))
            source = str(row.get("direction_source", "none"))
            lines.append(
                "| "
                f"`{row.get('name', '')}` | "
                f"{_number_text(row.get('baseline'))} | "
                f"{_number_text(row.get('patched'))} | "
                f"{_delta_text(row.get('delta'))} | "
                f"`{row.get('interpretation', 'changed')}` | "
                f"`{direction}` ({source}) |"
            )
    return "\n".join(lines)


def _repair_summary(repair: dict[str, Any]) -> str:
    legacy_attempts = repair.get("repair_count")
    review_attempts = repair.get("review_repair_count")
    run_attempts = repair.get("run_repair_count")
    lines = [
        f"- Status: `{repair.get('status', 'unknown')}`",
    ]
    effective_status = repair.get("effective_status")
    if effective_status and effective_status != repair.get("status"):
        lines.append(f"- Effective status: `{effective_status}`")
    if repair.get("review_after_repair_status"):
        lines.append(f"- Review after repair: `{repair.get('review_after_repair_status')}`")
    if legacy_attempts is not None:
        lines.append(f"- Attempts: `{legacy_attempts}`")
    if review_attempts is not None:
        lines.append(f"- Review repair attempts: `{review_attempts}`")
    if run_attempts is not None:
        lines.append(f"- Run repair attempts: `{run_attempts}`")
    if legacy_attempts is None and review_attempts is None and run_attempts is None:
        lines.append("- Attempts: `0`")
    if repair.get("latest_proposed_edits"):
        lines.append(f"- Latest proposal: `{repair.get('latest_proposed_edits')}`")
    if repair.get("latest_review_repair"):
        lines.append(f"- Latest review repair: `{repair.get('latest_review_repair')}`")
    if repair.get("latest_run_repair"):
        lines.append(f"- Latest run repair: `{repair.get('latest_run_repair')}`")
    if repair.get("latest_edit_count") is not None:
        lines.append(f"- Proposed edits: `{repair.get('latest_edit_count')}`")
    selected = repair.get("selected_files")
    if isinstance(selected, list) and selected:
        files = ", ".join(f"`{path}`" for path in selected[:5])
        suffix = " ..." if len(selected) > 5 else ""
        lines.append(f"- Context files: {files}{suffix}")
    return "\n".join(lines)


def _execution_summary(execution: dict[str, Any], metrics: dict[str, Any]) -> str:
    if not execution:
        return "- Benchmark has not been executed yet."
    lines = [
        f"- Status: `{execution.get('status', 'unknown')}`",
        f"- Return code: `{execution.get('returncode')}`",
        f"- Timed out: `{execution.get('timed_out')}`",
        f"- Command: `{execution.get('command_text', '')}`",
        f"- Duration: `{execution.get('duration_sec')}` second(s)",
    ]
    environment = execution.get("environment", {})
    if isinstance(environment, dict) and environment:
        lines.append(f"- Environment mode: `{environment.get('mode', 'current')}`")
        lines.append(
            f"- Execution Python: `{environment.get('python_executable', 'unknown')}`"
        )
    if metrics:
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("| --- | ---: |")
        for key, value in sorted(metrics.items()):
            lines.append(f"| `{key}` | {value} |")
    return "\n".join(lines)


def _number_text(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{float(value):.6g}"
    return ""


def _delta_text(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        sign = "+" if number > 0 else ""
        return f"{sign}{number:.6g}"
    return ""


def _changed_files(manifest: dict[str, Any]) -> list[str]:
    patch = manifest.get("patch", {})
    if isinstance(patch, dict):
        value = patch.get("changed_files")
        if isinstance(value, list):
            return [str(item) for item in value if isinstance(item, str)]
    return []


def _review_sensitive_files(paths: list[str]) -> list[str]:
    """Return changed files that need extra review before trusting results."""
    return [path for path in paths if is_protected_edit_path(path)]


def _strip_heading(text: str) -> str:
    lines = []
    for line in text.strip().splitlines():
        if line.startswith("# "):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _read_optional(path: Path) -> str:
    return read_text(path) if path.exists() else ""


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = read_json(path)
    return value if isinstance(value, dict) else {}


def _read_memory_snapshot(run_dir: Path) -> dict[str, Any]:
    paths = code_task_memory_paths(run_dir)
    task_memory = _read_optional_json(paths.task_memory_json)
    return {
        "task_memory": task_memory,
        "review_findings": _read_jsonl_safe(paths.review_findings_jsonl)[-5:],
        "repair_memory": _read_jsonl_safe(paths.repair_memory_jsonl)[-5:],
        "artifact_refs": {
            "task_memory_markdown": _artifact_ref(run_dir, paths.task_memory_md),
            "repair_memory": _artifact_ref(run_dir, paths.repair_memory_jsonl),
        },
    }


def _read_jsonl_safe(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return [row for row in read_jsonl(path) if isinstance(row, dict)]
    except Exception:
        return []


def _read_run_json(run_dir: Path, label: str, filename: str) -> dict[str, Any]:
    return _read_optional_json(run_dir / label / filename)


def _read_latest_failure(run_dir: Path, manifest: dict[str, Any]) -> str:
    failure = manifest.get("failure_analysis", {})
    if isinstance(failure, dict):
        if failure.get("status") in {"no_failure", "resolved"}:
            return ""
        path = failure.get("analysis")
        if path:
            root = run_dir.parent.parent
            return _read_optional(root / str(path))
    for label in ("patched", "baseline"):
        text = _read_optional(run_dir / label / "failure_analysis.md")
        if text:
            return text
    return _read_optional(run_dir / "failure_analysis.md")


def _active_repair(repair: object) -> bool:
    if not isinstance(repair, dict) or not repair:
        return False
    return repair.get("status") not in {"benchmark_passed", "resolved"}


def _clip(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 18].rstrip() + "... [truncated]"


def _update_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    layout = manifest_section(manifest, "layout")
    layout["summary"] = "code_task/summary.md"
    manifest["layout"] = layout
    manifest["summary"] = {
        "generated_at": utcnow_iso(),
        "path": "code_task/summary.md",
    }
    save_code_task_manifest(run_dir, manifest)
