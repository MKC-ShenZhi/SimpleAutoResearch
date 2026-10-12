"""Layered code-task review indexing and context selection.

This module is intentionally benchmark-agnostic. It builds a local project
index, groups files into review clusters, and prepares bounded snippets for
LLM review. Deterministic checks can inspect the whole project; LLM reviewers
should receive only the relevant cluster context.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from simple_ar.code_task.analysis.interfaces import public_api_from_source, render_source_snippets, _module_name, _resolve_import_module
from simple_ar.code_task.analysis.index import IGNORED_FILE_NAMES, is_python_environment, read_code_task_text
from simple_ar.code_task.analysis.source_context import diff_source_anchors, source_context_for_files
from simple_ar.reviewing.schema import ReviewFinding


SKIP_PARTS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    "dist",
    "build",
    ".ipynb_checkpoints",
}


def build_review_index(
    project_dir: Path,
    *,
    result_schema: Mapping[str, Any] | None = None,
    contract: Mapping[str, Any] | None = None,
    excluded_names: frozenset[str] = frozenset(),
    path_filter: Callable[[Path], bool] | None = None,
) -> dict[str, Any]:
    """Build a complete local review index without calling an LLM."""

    files: list[dict[str, Any]] = []
    notebooks: list[dict[str, Any]] = []
    required_metrics = _required_metrics(result_schema or {})
    for path in sorted(_project_files(project_dir, excluded_names=excluded_names, path_filter=path_filter)):
        if not path.is_file() or path.is_symlink():
            continue
        rel = _safe_rel(project_dir, path)
        if not rel:
            continue
        if path.suffix.lower() == ".ipynb":
            # A notebook can be an experiment entry but is not a CLI script.
            # Keep its identity without loading cell outputs into code review.
            notebooks.append({"path": rel, "size_bytes": path.stat().st_size,
                              "inspection": "path_only_cells_and_outputs_unread"})
            continue
        if path.name in IGNORED_FILE_NAMES or path.name.startswith(".env") or path.name.endswith(".lock") or path.stat().st_size > 1_000_000:
            continue
        text = read_code_task_text(path)
        if text is None:
            continue
        role = classify_review_role(rel)
        row: dict[str, Any] = {
            "path": rel,
            "suffix": path.suffix.lower(),
            "role": role,
            "size_bytes": path.stat().st_size,
            "line_count": max(1, len(text.splitlines())) if text else 0,
            "mentions_required_metrics": [metric for metric in required_metrics if metric and metric in text],
        }
        if path.suffix == ".py":
            row["public_api"] = public_api_from_source(text)
            row["imports"] = _python_imports(text, current=_module_name(project_dir, path),
                                             is_package=path.name == "__init__.py")
            row["entrypoint_candidate"] = _is_entrypoint_candidate(rel, text)
        files.append(row)

    return {
        "schema_version": "code_task_review_index.v1",
        "project_dir": str(project_dir),
        "file_count": len(files),
        "python_file_count": sum(1 for row in files if row.get("suffix") == ".py"),
        "required_metrics": required_metrics,
        "entrypoints": [row["path"] for row in files if row.get("entrypoint_candidate")],
        "notebooks": notebooks,
        "roles": _role_counts(files),
        "task_markers": _task_markers(contract or {}),
        "files": files,
    }


def _project_files(project_dir: Path, *, excluded_names: frozenset[str] = frozenset(),
                   path_filter: Callable[[Path], bool] | None = None):
    """Prune dependency/cache trees before traversal, not after visiting them."""
    for directory, children, names in os.walk(project_dir, followlinks=False):
        parent = Path(directory)
        children[:] = [name for name in children
                       if name not in SKIP_PARTS and name not in excluded_names and not name.startswith(".env")
                       and not name.endswith((".egg-info", ".dist-info"))
                       and not is_python_environment(parent / name)
                       and not (parent / name).is_symlink()
                       and (path_filter is None or path_filter(parent / name))]
        for name in names:
            if name not in excluded_names and (path_filter is None or path_filter(parent / name)):
                yield parent / name


def build_review_clusters(
    review_index: Mapping[str, Any],
    *,
    deterministic_findings: Sequence[ReviewFinding] = (),
    max_clusters: int = 6,
    max_files_per_cluster: int = 5,
    relevant_paths: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Select bounded semantic clusters for LLM review."""

    if max_clusters <= 0 or max_files_per_cluster <= 0:
        return []
    files = [row for row in review_index.get("files", []) if isinstance(row, Mapping)]
    cluster_defs = [
        ("entrypoint", "Entrypoint and orchestration", ["entrypoint", "orchestration"]),
        ("data_flow", "Data loading and preprocessing", ["data", "preprocess"]),
        ("core_logic", "Core algorithm and model logic", ["core", "model"]),
        ("metrics_artifacts", "Metrics, result schema, and artifact writers", ["metrics", "reporting"]),
        ("config_docs", "Configuration, documentation, and user-facing surface", ["config", "docs"]),
        ("support", "Support modules and remaining project surface", ["support"]),
    ]
    clusters: list[dict[str, Any]] = []
    used: set[str] = set()
    known = {str(row.get("path")) for row in files if row.get("path")}
    relevant = list(dict.fromkeys(path for path in relevant_paths if path in known))
    for offset in range(0, len(relevant), max_files_per_cluster):
        paths = relevant[offset:offset + max_files_per_cluster]
        clusters.append({
            "cluster_id": "changes" if offset == 0 else f"changes_{len(clusters) + 1}",
            "title": "Changed source and local integration",
            "objective": "Review the actual changed behavior and surrounding source, not only the patch diff.",
            "roles": sorted({str(row.get("role") or "support") for row in files if row.get("path") in paths}),
            "files": paths,
            "deterministic_findings": _findings_for_paths(deterministic_findings, paths),
        })
        used.update(paths)
        if len(clusters) >= max_clusters:
            return clusters
    # A local patch needs its actual dependencies/callers, not arbitrary large
    # data or documents sharing a filename role. Historical indexes without
    # import observations and whole-project reviews retain their broad scope.
    if relevant and all(path.endswith(".py") for path in relevant) and any("imports" in row for row in files):
        modules = {str(row["path"]): str(PurePosixPath(str(row["path"])).with_suffix(""))
                   .replace("/", ".").removesuffix(".__init__")
                   for row in files if str(row.get("path", "")).endswith(".py")}
        related = set(relevant)
        for row in files:
            imports = row.get("imports", [])
            targets = {path for path, module in modules.items()
                       if any(name == alias or name.startswith(alias + ".")
                              for name in imports for alias in {module, module.removeprefix("src.")})}
            if row.get("path") in relevant:
                related.update(targets)
            elif targets.intersection(relevant):
                related.add(str(row["path"]))
        files = [row for row in files if row.get("path") in related]
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    for row in files:
        by_role.setdefault(str(row.get("role") or "support"), []).append(row)
    for cluster_id, title, roles in cluster_defs:
        selected: list[Mapping[str, Any]] = []
        for role in roles:
            selected.extend(by_role.get(role, []))
        selected = _rank_cluster_files(selected)
        paths = [str(row.get("path")) for row in selected if row.get("path") and str(row.get("path")) not in used]
        paths = paths[:max_files_per_cluster]
        if not paths:
            continue
        used.update(paths)
        findings = _findings_for_paths(deterministic_findings, paths)
        clusters.append(
            {
                "cluster_id": cluster_id,
                "title": title,
                "objective": _cluster_objective(cluster_id),
                "roles": roles,
                "files": paths,
                "deterministic_findings": findings,
            }
        )
        if len(clusters) >= max_clusters:
            break

    remaining = [
        str(row.get("path"))
        for row in _rank_cluster_files(files)
        if row.get("path") and str(row.get("path")) not in used
    ][:max_files_per_cluster]
    if remaining and len(clusters) < max_clusters:
        clusters.append(
            {
                "cluster_id": "remaining",
                "title": "Remaining high-signal files",
                "objective": "Look for integration risks not covered by earlier clusters.",
                "roles": ["support"],
                "files": remaining,
                "deterministic_findings": _findings_for_paths(deterministic_findings, remaining),
            }
        )
    return clusters


def compact_review_index(review_index: Mapping[str, Any], *, max_files: int = 40) -> dict[str, Any]:
    files = review_index.get("files")
    rows = [row for row in files if isinstance(row, Mapping)] if isinstance(files, list) else []
    return {
        "schema_version": review_index.get("schema_version", "code_task_review_index.v1"),
        "file_count": review_index.get("file_count", 0),
        "python_file_count": review_index.get("python_file_count", 0),
        "required_metrics": review_index.get("required_metrics", []),
        "entrypoints": review_index.get("entrypoints", []),
        "roles": review_index.get("roles", {}),
        "task_markers": review_index.get("task_markers", {}),
        "files": [
            {
                "path": row.get("path", ""),
                "role": row.get("role", "support"),
                "line_count": row.get("line_count", 0),
                "public_api": row.get("public_api", [])[:16] if isinstance(row.get("public_api"), list) else [],
                "mentions_required_metrics": row.get("mentions_required_metrics", []),
            }
            for row in rows[:max_files]
        ],
    }


def snippets_for_cluster(
    project_dir: Path,
    cluster: Mapping[str, Any],
    *,
    chars_per_file: int = 4_000,
    patch_diff: str = "",
) -> list[str]:
    paths = cluster.get("files")
    rows = source_context_for_files(project_dir,
        [str(rel) for rel in paths] if isinstance(paths, list) else [],
        max_chars_per_file=chars_per_file, anchors=diff_source_anchors(patch_diff),
        editable_files=[], include_unanchored_tail=True)
    # Keep the existing list-of-blocks interface for generated-project review.
    return [render_source_snippets([row]) for row in rows]


def classify_review_role(path: str) -> str:
    lowered = path.lower()
    name = PurePosixPath(path).name.lower()
    stem = PurePosixPath(path).stem.lower()
    if name in {"main.py", "__main__.py", "cli.py", "app.py"} or stem in {"main", "cli", "app"}:
        return "entrypoint"
    if _contains_any(lowered, ("runner", "run_", "execute", "executor", "orchestr", "workflow", "pipeline", "experiment", "train", "eval")):
        return "orchestration"
    if _contains_any(lowered, ("input", "data", "dataset", "loader", "source", "ingest", "feature", "label")):
        return "data"
    if _contains_any(lowered, ("process", "preprocess", "transform", "prepare", "clean", "split")):
        return "preprocess"
    if _contains_any(lowered, ("core", "model", "algorithm", "logic", "method", "estimator", "classif", "regress")):
        return "model"
    if _contains_any(lowered, ("analysis", "metric", "score", "report", "artifact", "output", "result", "summary", "writer")):
        return "metrics"
    if _contains_any(lowered, ("config", "setting", "schema", "option")):
        return "config"
    if name.lower() in {"readme.md", "usage.md"} or lowered.endswith((".md", ".txt")):
        return "docs"
    return "support"


def _rank_cluster_files(files: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(
        files,
        key=lambda row: (
            _role_rank(str(row.get("role") or "support")),
            -int(row.get("line_count") or 0),
            str(row.get("path") or ""),
        ),
    )


def _role_rank(role: str) -> int:
    ranks = {
        "entrypoint": 0,
        "orchestration": 1,
        "data": 2,
        "preprocess": 3,
        "model": 4,
        "core": 4,
        "metrics": 5,
        "reporting": 5,
        "config": 6,
        "docs": 7,
    }
    return ranks.get(role, 100)


def _findings_for_paths(findings: Sequence[ReviewFinding], paths: Sequence[str]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    path_set = set(paths)
    for finding in findings:
        blob = " ".join([finding.summary, finding.recommendation, *finding.evidence])
        if any(path in blob for path in path_set):
            selected.append(finding.model_dump(mode="json"))
    return selected[:12]


def _cluster_objective(cluster_id: str) -> str:
    objectives = {
        "entrypoint": "Verify the command path, orchestration, and metric printing contract.",
        "data_flow": "Verify data loading, preprocessing, leakage boundaries, and record schemas.",
        "core_logic": "Verify core implementation logic and dependency boundaries.",
        "metrics_artifacts": "Verify metric computation, required outputs, and artifact writers.",
        "config_docs": "Verify configuration/documentation match the executable project surface.",
    }
    return objectives.get(cluster_id, "Review this cluster for local correctness and integration risk.")


def _task_markers(contract: Mapping[str, Any]) -> dict[str, Any]:
    text = "\n".join(str(contract.get(key) or "") for key in ("objective", "task")).lower()
    criteria = contract.get("success_criteria")
    if isinstance(criteria, list):
        text += "\n" + "\n".join(str(item).lower() for item in criteria)
    return {
        "mentions_readme": "readme" in text,
        "mentions_results_json": "results.json" in text,
        "mentions_report": "report.md" in text or "report" in text,
        "mentions_multiple_tasks": any(word in text for word in ("multiple", "several", "suite", "tasks")),
    }


def _required_metrics(schema: Mapping[str, Any]) -> list[str]:
    value = schema.get("required_metrics")
    metrics = [str(item) for item in value if str(item).strip()] if isinstance(value, list) else []
    primary = str(schema.get("primary_metric") or "").strip()
    if primary and primary not in metrics:
        metrics.insert(0, primary)
    return metrics


def _python_imports(source: str, *, current: str = "", is_package: bool = False) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = _resolve_import_module(current, node.module, node.level, is_package)
            imports.append(prefix)  # Includes star imports as a module dependency.
            imports.extend(f"{prefix}.{alias.name}".strip(".") for alias in node.names if alias.name != "*")
    return sorted(set(imports))[:40]


def _is_entrypoint_candidate(path: str, source: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if name in {"main.py", "__main__.py", "cli.py", "app.py"}:
        return True
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for test in ast.walk(node.test):
            if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
                continue
            a, b = test.left, test.comparators[0]
            for variable, literal in ((a, b), (b, a)):
                if isinstance(variable, ast.Name) and variable.id == "__name__" and isinstance(literal, ast.Constant) and literal.value == "__main__":
                    return True
    return False


def _role_counts(files: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in files:
        role = str(row.get("role") or "support")
        counts[role] = counts.get(role, 0) + 1
    return counts


def _safe_rel(root: Path, path: Path) -> str:
    try:
        return _safe_path(path.relative_to(root).as_posix())
    except ValueError:
        return ""


def _safe_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/").strip().lstrip("/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)
