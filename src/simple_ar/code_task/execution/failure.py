from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, read_text, write_json, write_text
from simple_ar.code_task.analysis.index import project_python_files
from simple_ar.code_task.execution.artifact_contract import compact_artifact_scan
from simple_ar.code_task.execution.failure_graph import build_failure_graph
from simple_ar.code_task.execution.run_history import archive_failure_artifacts_for_latest_attempt
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    is_relative_to,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.execution.summary import write_code_task_summary


TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\):[\s\S]*", re.MULTILINE)
FILE_LINE_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')
@dataclass(frozen=True)
class FailureAnalysisResult:
    """Result returned after writing deterministic failure analysis.

    Args:
        run_dir: Code-task run directory.
        analysis_path: Markdown failure analysis path.
        status: ``needs_repair`` when a failed execution is present,
            otherwise ``no_failure``.
        implicated_files: Workspace-relative files mentioned by tracebacks.
        source: Evidence source used for the diagnosis.
    """

    run_dir: Path
    analysis_path: Path
    status: str
    implicated_files: tuple[str, ...]
    source: str = "benchmark"


def analyze_code_task_failure(run_dir: Path) -> FailureAnalysisResult:
    """Create a compact Markdown diagnosis from the latest benchmark run.

    Args:
        run_dir: Code-task run directory.

    Returns:
        Failure analysis metadata.

    Raises:
        FileNotFoundError: If no execution report exists.
        RuntimeError: If ``run_dir`` is not a code-task run.
    """
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    artifacts = _latest_failure_artifacts(paths, manifest)
    report_path = artifacts.get("execution_report")
    report: dict[str, Any] = {}
    if isinstance(report_path, Path) and report_path.exists():
        report_value = read_json(report_path)
        if not isinstance(report_value, dict):
            raise RuntimeError(f"Expected JSON object in {report_path}")
        report = report_value
    artifact_scan = _read_optional_json(artifacts.get("artifact_scan")) if artifacts.get("artifact_scan") else {}

    stdout = _read_optional(artifacts["stdout"])
    stderr = _read_optional(artifacts["stderr"])
    validation = _read_optional_json(artifacts["validation_report"])
    if not report and not _validation_failed(validation):
        raise FileNotFoundError(
            "No failed benchmark execution or validation report was found. "
            "Run `simple-ar code-task validate` or `simple-ar code-task run` first."
        )

    changed_files = _changed_files(manifest)
    graph = build_failure_graph(
        workspace_dir=paths.workspace_dir,
        stdout=stdout,
        stderr=stderr,
        validation=validation,
        changed_files=changed_files,
    )
    if artifact_scan:
        graph["artifact_scan"] = compact_artifact_scan(artifact_scan)
    if isinstance(report.get("runtime_watchdog"), dict):
        graph["runtime_watchdog"] = report["runtime_watchdog"]
    traceback_block = str(graph.get("traceback") or _traceback_block(stderr))
    runtime_implicated = [
        str(path) for path in graph.get("traceback_files", []) if isinstance(path, str) and path
    ]
    validation_implicated = _validation_issue_files(validation, paths.workspace_dir)
    signal_matched_files = [
        str(path) for path in graph.get("signal_matched_files", []) if isinstance(path, str) and path
    ]
    if not runtime_implicated and signal_matched_files:
        runtime_implicated = signal_matched_files
    implicated_files = _dedupe(
        runtime_implicated + ([] if (stderr or stdout or traceback_block) else validation_implicated)
    )
    signal_lines = [
        str(line) for line in graph.get("runtime_signals", []) if isinstance(line, str) and line
    ]
    if not signal_lines and stderr.strip():
        signal_lines = _stderr_fallback_signal_lines(stderr)
    if not signal_lines:
        signal_lines = [
            str(line) for line in graph.get("validation_signals", []) if isinstance(line, str) and line
        ] or _validation_signal_lines(validation)

    status = "no_failure" if report.get("status") == "passed" else "needs_repair"
    markdown = _render_failure_analysis(
        report=report,
        validation=validation,
        source=str(artifacts["source"]),
        traceback_block=traceback_block,
        signal_lines=signal_lines,
        implicated_files=implicated_files,
        signal_matched_files=signal_matched_files,
        failure_graph=graph,
        changed_files=changed_files,
        status=status,
    )
    analysis_path = artifacts["failure_analysis"]
    graph_path = analysis_path.with_name("failure_graph.json")
    write_json(graph_path, graph)
    write_text(analysis_path, markdown)
    history_failure: dict[str, str] = {}
    run_label = str(report.get("label") or "").strip()
    if run_label:
        history_failure = archive_failure_artifacts_for_latest_attempt(
            run_dir,
            run_label=run_label,
            failure_analysis_path=analysis_path,
            failure_graph_path=graph_path,
        )
    _update_manifest_after_failure_analysis(
        run_dir,
        manifest,
        analysis_path=analysis_path,
        status=status,
        source=str(artifacts["source"]),
        implicated_files=implicated_files,
        signal_matched_files=signal_matched_files,
        failure_graph_path=graph_path,
        history_failure=history_failure,
    )
    write_code_task_summary(run_dir)
    return FailureAnalysisResult(
        run_dir=paths.run_dir,
        analysis_path=analysis_path,
        status=status,
        implicated_files=tuple(implicated_files),
        source=str(artifacts["source"]),
    )


def _render_failure_analysis(
    *,
    report: dict[str, Any],
    validation: dict[str, Any],
    source: str,
    traceback_block: str,
    signal_lines: list[str],
    implicated_files: list[str],
    signal_matched_files: list[str],
    failure_graph: dict[str, Any],
    changed_files: list[str],
    status: str,
) -> str:
    status_line = str(report.get("status", "unknown"))
    sections = [
        "# Failure Analysis",
        "",
        f"Status: `{status}`",
        f"Source: `{source}`",
        "",
        "## Execution",
        "",
        _execution_summary(report, status_line),
        "",
        "## Validation",
        "",
        _validation_summary(validation),
        "",
        "## Likely Cause",
        "",
        _likely_cause(report, traceback_block, signal_lines, failure_graph=failure_graph),
        "",
        "## Failure Graph",
        "",
        f"- Primary signal: `{failure_graph.get('primary_signal', '')}`",
        "- Candidate repair files: "
        + (
            ", ".join(f"`{path}`" for path in failure_graph.get("candidate_files", [])[:8])
            if isinstance(failure_graph.get("candidate_files"), list)
            else "none"
        ),
        "",
        "## Runtime Contracts",
        "",
        _artifact_scan_summary(failure_graph.get("artifact_scan")),
        "",
        _runtime_watchdog_summary(failure_graph.get("runtime_watchdog")),
        "",
        "## Implicated Files",
        "",
        _bullet_list([f"`{path}`" for path in implicated_files]) or "- None found in traceback.",
        "",
        "## Signal-Matched Files",
        "",
        _bullet_list([f"`{path}`" for path in signal_matched_files])
        or "- No source files matched extracted error tokens.",
        "",
        "## Recently Changed Files",
        "",
        _bullet_list([f"`{path}`" for path in changed_files]) or "- No patch metadata found.",
        "",
        "## Error Signals",
        "",
        _bullet_list([f"`{line}`" for line in signal_lines[:12]]) or "- No concise error lines found.",
        "",
    ]
    if traceback_block:
        sections.extend(
            [
                "## Traceback",
                "",
                "```text",
                _clip(traceback_block, max_chars=6000).rstrip(),
                "```",
                "",
            ]
        )
    sections.extend(
        [
            "## Repair Guidance",
            "",
            "- Keep the repair limited to implicated or recently changed files unless the traceback points elsewhere.",
            "- Re-run validation before re-running the benchmark.",
            "- Prefer fixing the smallest failing behavior before broad refactors.",
            "",
        ]
    )
    return "\n".join(sections)


def _execution_summary(report: dict[str, Any], status_line: str) -> str:
    if not report:
        return "- Benchmark was not launched; diagnosis is based on validation evidence."
    return "\n".join(
        [
            f"- Benchmark status: `{status_line}`",
            f"- Return code: `{report.get('returncode')}`",
            f"- Timed out: `{report.get('timed_out')}`",
            f"- Command: `{report.get('command_text', '')}`",
        ]
    )


def _artifact_scan_summary(value: object) -> str:
    if not isinstance(value, dict):
        return "- Artifact scan: not available."
    lines = [f"- Artifact scan status: `{value.get('status', 'unknown')}`"]
    findings = value.get("findings")
    for finding in findings if isinstance(findings, list) else []:
        if not isinstance(finding, dict):
            continue
        lines.append(
            "- "
            f"{finding.get('code', 'artifact_issue')} at "
            f"`{finding.get('expected_path', '')}`: "
            f"{finding.get('message', '')}"
        )
        candidates = finding.get("candidate_paths")
        if isinstance(candidates, list) and candidates:
            lines.append("  candidates: " + ", ".join(f"`{item}`" for item in candidates[:4]))
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts[:4]:
            if not isinstance(artifact, dict):
                continue
            lines.append(
                "- "
                f"`{artifact.get('expected_path', '')}`: "
                f"{artifact.get('parse_status', 'unknown')} "
                f"({artifact.get('size', 0)} bytes)"
            )
    return "\n".join(lines)


def _runtime_watchdog_summary(value: object) -> str:
    if not isinstance(value, dict):
        return "- Runtime watchdog: not triggered."
    lines = [
        f"- Runtime watchdog: `{value.get('reason', 'triggered')}`",
        f"- Detail: {value.get('detail', '')}",
        f"- Elapsed seconds: `{value.get('elapsed_sec', '')}`",
        f"- Warning-like lines: `{value.get('warning_line_count', 0)}`",
    ]
    samples = value.get("sample_lines")
    if isinstance(samples, list) and samples:
        lines.append("- Samples: " + " | ".join(f"`{str(item)[:160]}`" for item in samples[:4]))
    return "\n".join(lines)


def _validation_summary(validation: dict[str, Any]) -> str:
    if not validation:
        return "- No validation report found."
    lines = [
        f"- Status: `{validation.get('status', 'unknown')}`",
        f"- Errors: `{validation.get('error_count', 0)}`",
        f"- Warnings: `{validation.get('warning_count', 0)}`",
    ]
    issues = validation.get("issues", [])
    if isinstance(issues, list):
        for issue in issues[:5]:
            if not isinstance(issue, dict):
                continue
            lines.append(
                "- "
                f"{issue.get('severity', 'issue')} "
                f"{issue.get('code', '')} "
                f"in `{issue.get('path', '')}`: "
                f"{issue.get('message', '')}"
            )
    return "\n".join(lines)


def _likely_cause(
    report: dict[str, Any],
    traceback_block: str,
    signal_lines: list[str],
    *,
    failure_graph: dict[str, Any] | None = None,
) -> str:
    if not report:
        return "Static validation failed before a benchmark execution report was available."
    if report.get("status") == "passed":
        return "No failure detected in the latest execution report."
    if report.get("status") == "blocked_by_validation":
        return "The benchmark was not launched because static validation reported errors."
    if report.get("timed_out") is True:
        return "The benchmark exceeded the configured timeout."
    if isinstance(report.get("runtime_watchdog"), dict):
        watchdog = report["runtime_watchdog"]
        return (
            "The benchmark was stopped by the runtime output watchdog: "
            f"`{watchdog.get('reason', 'triggered')}` ({watchdog.get('detail', '')})."
        )
    quality_guard = report.get("quality_guard")
    if isinstance(quality_guard, dict) and quality_guard.get("reason") == "artifact_path_mismatch":
        return "The run wrote a required artifact to the wrong workspace path; repair the artifact output path contract."
    if failure_graph and failure_graph.get("primary_signal"):
        return f"The strongest execution signal is: `{failure_graph.get('primary_signal')}`"
    if traceback_block:
        last = _last_nonempty_line(traceback_block)
        return f"The latest traceback ends with: `{last}`"
    if signal_lines:
        return f"The strongest error signal is: `{signal_lines[0]}`"
    return "The benchmark returned a non-zero status without a concise traceback."


def _traceback_block(stderr: str) -> str:
    match = TRACEBACK_RE.search(stderr)
    return match.group(0).strip() if match else ""


def _implicated_files(text: str, workspace_dir: Path) -> list[str]:
    workspace = workspace_dir.resolve()
    files: list[str] = []
    for match in FILE_LINE_RE.finditer(text):
        path = Path(match.group(1))
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not is_relative_to(resolved, workspace):
            continue
        rel = resolved.relative_to(workspace).as_posix()
        if rel not in files:
            files.append(rel)
    return files


def _stderr_fallback_signal_lines(stderr: str) -> list[str]:
    lines: list[str] = []
    for line in stderr.splitlines():
        stripped = " ".join(line.strip().split())
        if stripped:
            lines.append(_clip(stripped, max_chars=240))
    return lines[:3]


def _changed_files(manifest: dict[str, Any]) -> list[str]:
    patch = manifest.get("patch", {})
    if isinstance(patch, dict):
        value = patch.get("changed_files")
        if isinstance(value, list):
            return [str(item) for item in value if isinstance(item, str)]
    return []


def _validation_failed(validation: dict[str, Any]) -> bool:
    return validation.get("status") == "failed" or _int_value(validation.get("error_count")) > 0


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _validation_issue_files(validation: dict[str, Any], workspace_dir: Path) -> list[str]:
    issues = validation.get("issues", [])
    if not isinstance(issues, list):
        return []
    workspace = workspace_dir.resolve()
    files: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        value = issue.get("path")
        if not isinstance(value, str) or not value:
            continue
        rel = Path(value)
        if rel.is_absolute() or ".." in rel.parts:
            continue
        resolved = (workspace / rel).resolve()
        if not is_relative_to(resolved, workspace):
            continue
        normalized = rel.as_posix()
        if normalized not in files:
            files.append(normalized)
    return files


def _validation_signal_lines(validation: dict[str, Any]) -> list[str]:
    issues = validation.get("issues", [])
    if not isinstance(issues, list):
        return []
    lines: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        severity = issue.get("severity", "issue")
        code = issue.get("code", "")
        path = issue.get("path", "")
        message = issue.get("message", "")
        lines.append(_clip(f"{severity} {code} in {path}: {message}", max_chars=240))
    return lines


def _source_signal_files(workspace_dir: Path, signal_text: str) -> list[str]:
    terms = _failure_terms(signal_text)
    if not terms or not workspace_dir.is_dir():
        return []
    ranked: list[tuple[int, str]] = []
    workspace = workspace_dir.resolve()
    for path in project_python_files(workspace):
        try:
            rel = path.resolve().relative_to(workspace).as_posix()
            source = path.read_text(encoding="utf-8", errors="ignore").lower()
        except (OSError, ValueError):
            continue
        score = 0
        for term in terms:
            if term in source:
                score += 1
                if re.search(rf"\b{re.escape(term)}\b", source):
                    score += 1
        if score:
            ranked.append((score, rel))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [rel for _, rel in ranked[:8]]


def _failure_terms(text: str) -> list[str]:
    lowered = text.lower()
    terms: list[str] = []
    for pattern in (
        r"has no attribute ['\"]([^'\"]+)['\"]",
        r"name ['\"]([^'\"]+)['\"] is not defined",
        r"no module named ['\"]([^'\"]+)['\"]",
        r"unexpected keyword argument ['\"]([^'\"]+)['\"]",
        r"keyerror:\s*['\"]([^'\"]+)['\"]",
    ):
        terms.extend(match.group(1).lower() for match in re.finditer(pattern, lowered))
    for quoted in re.findall(r"'([^']{3,80})'|\"([^\"]{3,80})\"", text):
        value = next((part for part in quoted if part), "")
        if value:
            terms.append(value.lower())
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", text):
        terms.append(token.lower())
    stop = {
        "attributeerror",
        "benchmark",
        "cannot",
        "error",
        "experiment",
        "failed",
        "failure",
        "file",
        "float",
        "line",
        "module",
        "object",
        "python",
        "return",
        "status",
        "traceback",
        "typeerror",
        "valueerror",
        "with",
    }
    filtered = [term for term in terms if term not in stop and len(term) >= 3]
    return list(dict.fromkeys(filtered))[:24]


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        if item not in deduped:
            deduped.append(item)
    return deduped


def _read_optional(path: Path) -> str:
    return read_text(path) if path.exists() else ""


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = read_json(path)
    return value if isinstance(value, dict) else {}


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return _clip(stripped, max_chars=240)
    return ""


def _clip(text: str, *, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 18].rstrip() + "... [truncated]"


def _update_manifest_after_failure_analysis(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    analysis_path: Path,
    status: str,
    source: str,
    implicated_files: list[str],
    signal_matched_files: list[str],
    failure_graph_path: Path,
    history_failure: dict[str, str] | None = None,
) -> None:
    layout = manifest_section(manifest, "layout")
    benchmark = manifest.get("benchmark", {})
    latest_label = "patched"
    if isinstance(benchmark, dict):
        latest_label = str(benchmark.get("latest_label") or latest_label)
    analysis_rel = analysis_path.relative_to(run_dir).as_posix()
    graph_rel = failure_graph_path.relative_to(run_dir).as_posix()
    layout["failure_analysis"] = analysis_rel
    layout["failure_graph"] = graph_rel
    failure = manifest_section(manifest, "failure_analysis")
    failure.update(
        {
            "status": status,
            "source": source,
            "generated_at": utcnow_iso(),
            "analysis": analysis_rel,
            "failure_graph": graph_rel,
            "run_label": latest_label if source != "validation" else "",
            "implicated_files": implicated_files,
            "signal_matched_files": signal_matched_files,
        }
    )
    if history_failure:
        failure["history_failure_analysis"] = history_failure.get("failure_analysis", "")
        failure["history_failure_graph"] = history_failure.get("failure_graph", "")
        benchmark = manifest_section(manifest, "benchmark")
        runs = benchmark.get("runs")
        run_record = runs.get(latest_label) if isinstance(runs, dict) else None
        attempts = run_record.get("attempts") if isinstance(run_record, dict) else None
        if isinstance(attempts, list):
            for row in reversed(attempts):
                if isinstance(row, dict) and row.get("id") == run_record.get("latest_attempt"):
                    row.update(history_failure)
                    break
            run_record["attempts"] = attempts
            if history_failure.get("failure_analysis"):
                run_record["latest_failure_analysis"] = history_failure["failure_analysis"]
            if history_failure.get("failure_graph"):
                run_record["latest_failure_graph"] = history_failure["failure_graph"]
            runs[latest_label] = run_record
            benchmark["runs"] = runs
            manifest["benchmark"] = benchmark
    manifest["layout"] = layout
    manifest["failure_analysis"] = failure
    if status == "needs_repair":
        manifest["status"] = "failure_analyzed"
    save_code_task_manifest(run_dir, manifest)


def _latest_failure_artifacts(paths: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    validation_report = paths.meta_dir / "validation_report.json"
    benchmark = manifest.get("benchmark", {})
    if isinstance(benchmark, dict):
        report_rel = benchmark.get("execution_report")
        stdout_rel = benchmark.get("stdout")
        stderr_rel = benchmark.get("stderr")
        if report_rel and stdout_rel and stderr_rel:
            report = paths.run_dir / str(report_rel)
            run_dir = report.parent
            source = "benchmark"
            report_value = _read_optional_json(report)
            if report_value.get("status") == "blocked_by_validation":
                source = "validation"
            artifact_scan_rel = report_value.get("artifact_scan")
            artifact_scan = (
                paths.run_dir / str(artifact_scan_rel)
                if isinstance(artifact_scan_rel, str) and artifact_scan_rel
                else run_dir / "artifact_scan.json"
            )
            return {
                "source": source,
                "execution_report": report,
                "stdout": paths.run_dir / str(stdout_rel),
                "stderr": paths.run_dir / str(stderr_rel),
                "validation_report": validation_report,
                "failure_analysis": run_dir / "failure_analysis.md",
                "artifact_scan": artifact_scan,
            }
    return {
        "source": "validation",
        "execution_report": None,
        "stdout": paths.meta_dir / "validation_stdout.txt",
        "stderr": paths.meta_dir / "validation_stderr.txt",
        "validation_report": validation_report,
        "failure_analysis": paths.meta_dir / "failure_analysis.md",
    }
