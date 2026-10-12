from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Mapping

from simple_ar.code_task.generation.common import safe_relative_path, scalar_list, text


SOURCE_FILE_KINDS = {"source", "doc", "config", "data"}
RUNTIME_FILE_KINDS = {"runtime_dir", "artifact_placeholder", "output_placeholder"}
RUNTIME_DIRECTORY_NAMES = {
    "artifact",
    "artifacts",
    "figure",
    "figures",
    "log",
    "logs",
    "output",
    "outputs",
    "plot",
    "plots",
    "report",
    "reports",
    "result",
    "results",
    "submission",
    "submissions",
    "table",
    "tables",
}


def normalize_plan_path(value: object) -> str:
    """Return a safe path relative to the generated project root."""

    return safe_relative_path(_strip_generated_root_prefix(str(value or "")))


def normalize_dependency_paths(value: object, *, limit: int) -> list[str]:
    return [path for path in (normalize_plan_path(item) for item in scalar_list(value)[:limit]) if path]


def is_deliverable_project_file(value: str) -> bool:
    relative = safe_relative_path(value)
    if not relative:
        return False
    path = PurePosixPath(relative)
    return not (
        "__pycache__" in path.parts
        or any(part.startswith(".") and part != ".env.example" for part in path.parts)
        or relative.lower().endswith((".pyc", ".pyo", ".log", ".tmp"))
        or path.name in {"agent_result.json", "ingestion.json", "review.md"}
    )


def dedupe_file_rows(
    files: list[dict[str, Any]],
    *,
    dependency_limit: int,
    public_api_limit: int,
    acceptance_limit: int,
) -> list[dict[str, Any]]:
    by_path: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in files:
        path = normalize_plan_path(row.get("path"))
        if not path:
            continue
        row = dict(row)
        row["path"] = path
        if path not in by_path:
            by_path[path] = row
            order.append(path)
            continue
        by_path[path] = _merge_file_rows(
            by_path[path],
            row,
            dependency_limit=dependency_limit,
            public_api_limit=public_api_limit,
            acceptance_limit=acceptance_limit,
        )
    return [by_path[path] for path in order if path in by_path]


def entrypoint_first(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entrypoints = [row for row in files if row.get("entrypoint") or row.get("path") == "main.py"]
    entrypoint_ids = {id(row) for row in entrypoints}
    rest = [row for row in files if id(row) not in entrypoint_ids]
    return entrypoints + rest


def _merge_file_rows(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    *,
    dependency_limit: int,
    public_api_limit: int,
    acceptance_limit: int,
) -> dict[str, Any]:
    primary = right if _file_row_score(right) >= _file_row_score(left) else left
    secondary = left if primary is right else right
    return {
        "path": str(primary.get("path", "")),
        "purpose": text(primary.get("purpose")) or text(secondary.get("purpose")),
        "dependencies": _merge_unique(
            scalar_list(primary.get("dependencies")),
            scalar_list(secondary.get("dependencies")),
            limit=dependency_limit,
        ),
        "public_api": _merge_unique(
            scalar_list(primary.get("public_api")),
            scalar_list(secondary.get("public_api")),
            limit=public_api_limit,
        ),
        "acceptance_criteria": _merge_unique(
            scalar_list(primary.get("acceptance_criteria")),
            scalar_list(secondary.get("acceptance_criteria")),
            limit=acceptance_limit,
        ),
        "contract_obligations": _merge_unique(
            scalar_list(primary.get("contract_obligations") or primary.get("obligation_ids")),
            scalar_list(secondary.get("contract_obligations") or secondary.get("obligation_ids")),
            limit=40,
        ),
        "entrypoint": bool(primary.get("entrypoint")) or bool(secondary.get("entrypoint")),
        "kind": infer_file_kind(primary.get("path"), primary.get("kind") or secondary.get("kind")),
    }


def _file_row_score(row: Mapping[str, Any]) -> int:
    return (
        len(text(row.get("purpose")))
        + 30 * len(scalar_list(row.get("acceptance_criteria")))
        + 20 * len(scalar_list(row.get("public_api")))
        + 10 * len(scalar_list(row.get("dependencies")))
        + 10 * len(scalar_list(row.get("contract_obligations") or row.get("obligation_ids")))
    )


def _merge_unique(first: list[str], second: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in [*first, *second]:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _strip_generated_root_prefix(value: str) -> str:
    text = value.replace("\\", "/").strip()
    for prefix in ("generated_project/",):
        if text == prefix[:-1]:
            return ""
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


def infer_file_kind(path_value: object, raw_kind: object = "") -> str:
    """Classify planned files without depending on any benchmark convention."""

    kind = text(raw_kind).strip().lower().replace("-", "_")
    path = normalize_plan_path(path_value)
    suffix = PurePosixPath(path).suffix.lower()
    name = PurePosixPath(path).name.lower()
    # Executable Python files remain source even when they live below a
    # runtime-named package such as ``artifacts`` or ``report``.
    if suffix in {".py", ".pyi"}:
        return "source"
    if kind in SOURCE_FILE_KINDS | RUNTIME_FILE_KINDS:
        return kind
    if name in {".gitkeep", ".keep"}:
        return "artifact_placeholder"
    parts = tuple(part.lower() for part in PurePosixPath(path).parts)
    if suffix == "" and name in RUNTIME_DIRECTORY_NAMES:
        return "runtime_dir"
    if any(part in RUNTIME_DIRECTORY_NAMES for part in parts[:-1]):
        return "output_placeholder"
    if suffix in {".md", ".txt", ".rst"}:
        return "doc"
    if suffix in {".json", ".toml", ".yaml", ".yml", ".csv"}:
        return "config"
    return "data"


def is_model_generated_file(row: Mapping[str, Any]) -> bool:
    return infer_file_kind(row.get("path"), row.get("kind")) in SOURCE_FILE_KINDS


def is_runtime_placeholder(row: Mapping[str, Any]) -> bool:
    return infer_file_kind(row.get("path"), row.get("kind")) in RUNTIME_FILE_KINDS

