from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, read_text, write_json, write_text
from simple_ar.code_task.runtime.state import code_task_paths, is_relative_to


ATTEMPT_ID_RE = re.compile(r"^attempt-(\d{3,})$")


def archive_run_attempt(
    run_dir: Path,
    *,
    run_label: str,
    stdout: str,
    stderr: str,
    metrics: dict[str, float],
    execution_report: dict[str, Any],
) -> dict[str, Any]:
    """Write an immutable copy of one benchmark execution attempt.

    ``code_task/run/<label>/`` remains the latest-result slot used by existing
    summaries, comparisons, and adapters. The history directory keeps older
    stdout/stderr/report files available for debugging repeated repair loops.
    """

    paths = code_task_paths(Path(run_dir))
    invocation_id = execution_report.get('process', {}).get('invocation_id')
    if invocation_id and execution_report.get('native_binding'):
        for existing in (paths.run_artifact_dir / run_label / 'attempts').glob('*/execution_report.json'):
            saved = read_json(existing)
            if saved.get('process', {}).get('invocation_id') != invocation_id:
                continue
            keys = ('status', 'returncode', 'timed_out', 'command', 'cwd', 'timeout_sec',
                    'native_binding', 'implementation_file_hashes', 'process', 'metric_values')
            if (any(saved.get(key) != execution_report.get(key) for key in keys)
                    or read_text(existing.parent / 'stdout.txt') != (stdout or '')
                    or read_text(existing.parent / 'stderr.txt') != (stderr or '')
                    or read_json(existing.parent / 'metrics.json') != metrics):
                raise ValueError('Archived validator facts disagree with the original invocation; no replacement')
            record = {'id': saved['history_attempt'], 'label': run_label, 'status': saved['status'],
                'run_at': saved['generated_at'], 'returncode': saved['returncode'], 'timed_out': saved['timed_out'],
                'execution_report': existing.relative_to(paths.run_dir).as_posix(),
                **{name: saved[name] for name in ('stdout', 'stderr', 'metrics')}}
            meta = existing.parent / 'attempt_meta.json'
            if meta.is_file():
                if read_json(meta) != record:
                    raise ValueError('Archived validator metadata disagrees with its original report')
            else:
                write_json(meta, record)
            return record
    attempt_id = next_run_attempt_id(paths.run_artifact_dir, run_label)
    attempt_dir = paths.run_artifact_dir / run_label / "attempts" / attempt_id
    rel_base = f"code_task/run/{run_label}/attempts/{attempt_id}"

    write_text(attempt_dir / "stdout.txt", stdout or "")
    write_text(attempt_dir / "stderr.txt", stderr or "")
    write_json(attempt_dir / "metrics.json", metrics)

    history_report = dict(execution_report)
    history_report.update(
        {
            "history_attempt": attempt_id,
            "latest_slot": f"code_task/run/{run_label}",
            "stdout": f"{rel_base}/stdout.txt",
            "stderr": f"{rel_base}/stderr.txt",
            "metrics": f"{rel_base}/metrics.json",
        }
    )
    write_json(attempt_dir / "execution_report.json", history_report)

    record = {
        "id": attempt_id,
        "label": run_label,
        "status": str(execution_report.get("status", "unknown")),
        "run_at": str(execution_report.get("generated_at", "")),
        "returncode": execution_report.get("returncode"),
        "timed_out": bool(execution_report.get("timed_out", False)),
        "execution_report": f"{rel_base}/execution_report.json",
        "stdout": f"{rel_base}/stdout.txt",
        "stderr": f"{rel_base}/stderr.txt",
        "metrics": f"{rel_base}/metrics.json",
    }
    write_json(attempt_dir / "attempt_meta.json", record)
    return record


def archive_failure_artifacts_for_latest_attempt(
    run_dir: Path,
    *,
    run_label: str,
    failure_analysis_path: Path,
    failure_graph_path: Path,
) -> dict[str, str]:
    """Copy failure analysis artifacts into the latest run-attempt directory."""

    attempt_dir = latest_run_attempt_dir(run_dir, run_label)
    if attempt_dir is None:
        return {}
    archived: dict[str, str] = {}
    if failure_analysis_path.is_file():
        target = attempt_dir / "failure_analysis.md"
        write_text(target, read_text(failure_analysis_path))
        archived["failure_analysis"] = _relative_to_run(run_dir, target)
    if failure_graph_path.is_file():
        target = attempt_dir / "failure_graph.json"
        graph = read_json(failure_graph_path)
        write_json(target, graph)
        archived["failure_graph"] = _relative_to_run(run_dir, target)
    if archived:
        meta_path = attempt_dir / "attempt_meta.json"
        meta = read_json(meta_path) if meta_path.is_file() else {}
        if isinstance(meta, dict):
            meta.update(archived)
            write_json(meta_path, meta)
    return archived


def next_run_attempt_id(run_artifact_dir: Path, run_label: str) -> str:
    attempts_dir = Path(run_artifact_dir) / run_label / "attempts"
    max_index = 0
    if attempts_dir.is_dir():
        for child in attempts_dir.iterdir():
            match = ATTEMPT_ID_RE.match(child.name)
            if match:
                max_index = max(max_index, int(match.group(1)))
    return f"attempt-{max_index + 1:03d}"


def latest_run_attempt_dir(run_dir: Path, run_label: str) -> Path | None:
    paths = code_task_paths(Path(run_dir))
    attempts_dir = paths.run_artifact_dir / run_label / "attempts"
    if not attempts_dir.is_dir():
        return None
    candidates: list[tuple[int, Path]] = []
    for child in attempts_dir.iterdir():
        match = ATTEMPT_ID_RE.match(child.name)
        if match and child.is_dir():
            candidates.append((int(match.group(1)), child))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _relative_to_run(run_dir: Path, path: Path) -> str:
    root = Path(run_dir).resolve()
    resolved = Path(path).resolve()
    if is_relative_to(resolved, root):
        return resolved.relative_to(root).as_posix()
    return str(path)
