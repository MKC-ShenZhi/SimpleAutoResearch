from __future__ import annotations

"""Deterministic repair-action application for code-task workspaces.

This module is intentionally small and benchmark-agnostic. It turns model
repair intentions into workspace edits with the same safety property as the
existing old/new patch flow: ambiguous edits fail instead of guessing.
"""

import ast
import hashlib
from pathlib import Path
from typing import Any, Mapping

from simple_ar.code_task.analysis.interfaces import public_api
from simple_ar.code_task.generation.common import safe_relative_path


def looks_like_diff_fragment(text: str) -> bool:
    """Detect unified-diff syntax misplaced in exact old/new source replacements."""
    lines = [line for line in text.splitlines() if line.strip()]
    if any(line.startswith(("@@", "--- ", "+++ ")) for line in lines):
        return True
    removed = any(line.startswith("-") for line in lines)
    added = any(line.startswith("+") for line in lines)
    return removed and added


def apply_repair_actions(
    workspace_dir: Path,
    actions: object,
    *,
    allowed_paths: set[str] | None = None,
) -> dict[str, Any]:
    """Apply structured repair actions to a workspace.

    Supported actions are:
    ``no_change`` to explicitly skip a file;
    ``replace_block`` / ``replace_text`` with ``old_string`` + ``new_string``;
    ``rewrite_function`` with ``function_name`` + ``new_source``;
    ``rewrite_file`` with ``content``;
    ``add_file`` with ``content``; and ``remove_file``.

    Args:
        workspace_dir: Root directory to edit.
        actions: Model-returned action list.
        allowed_paths: Optional path allow-list. When provided, actions outside
            this set are rejected.

    Returns:
        An edit-application artifact. ``status`` is ``patched`` when at least
        one action changed a file, ``failed`` when nothing changed and at least
        one action was rejected, and ``skipped`` when no actions were supplied.
    """

    rows = [row for row in actions if isinstance(row, Mapping)] if isinstance(actions, list) else []
    result: dict[str, Any] = {
        "schema_version": "code_task_repair_edit_application.v1",
        "status": "skipped",
        "changed_files": [],
        "applied_actions": [],
        "rejected_actions": [],
    }
    if not rows:
        return result

    workspace = workspace_dir.resolve()
    allowed = _allowed_path_set(workspace_dir, allowed_paths or set())
    allowed.discard("")
    changed: list[str] = []
    for index, row in enumerate(rows, start=1):
        action = _action_type(row)
        rel_path = normalize_action_path(str(row.get("path", "")))
        if action in {"no_change", "skip"} and not rel_path and allowed:
            rel_path = sorted(allowed)[0]
        if not rel_path:
            _reject(result, index, row, "missing_or_unsafe_path")
            continue
        rel_path = _canonical_action_path(workspace, rel_path, allowed)
        if allowed and rel_path not in allowed:
            _reject(result, index, row, "path_not_in_allowed_context")
            continue
        path = _workspace_file(workspace, rel_path)
        if path is None:
            _reject(result, index, row, "path_escapes_workspace")
            continue
        if _has_file_ancestor(path, workspace):
            _reject(result, index, row, "parent_path_is_file")
            continue
        try:
            record = _apply_one_action(path, rel_path, action, row)
        except RepairActionError as exc:
            _reject(result, index, row, str(exc))
            continue
        if record is None:
            _reject(result, index, row, "action_made_no_change")
            continue
        result["applied_actions"].append(record)
        if record.get("changed") is False:
            continue
        if rel_path not in changed:
            changed.append(rel_path)

    result["changed_files"] = changed
    if changed:
        result["status"] = "patched"
    elif result["rejected_actions"]:
        result["status"] = "failed"
    return result


def normalize_action_path(value: str) -> str:
    return safe_relative_path(value)


def _allowed_path_set(workspace_dir: Path, paths: set[str]) -> set[str]:
    workspace_name = workspace_dir.name.replace("\\", "/")
    result: set[str] = set()
    for raw in paths:
        normalized = normalize_action_path(raw)
        if not normalized:
            continue
        result.add(normalized)
        stripped = _strip_known_root_prefix(normalized, workspace_name=workspace_name)
        if stripped:
            result.add(stripped)
    return result


def _canonical_action_path(workspace: Path, rel_path: str, allowed: set[str]) -> str:
    if not rel_path:
        return ""
    if rel_path in allowed or (workspace / rel_path).exists():
        return rel_path
    stripped = _strip_known_root_prefix(rel_path, workspace_name=workspace.name)
    if stripped and (not allowed or stripped in allowed or (workspace / stripped).exists()):
        return stripped
    return rel_path


def _strip_known_root_prefix(value: str, *, workspace_name: str) -> str:
    for prefix in ("generated_project/", f"{workspace_name}/"):
        if value == prefix[:-1]:
            return ""
        if value.startswith(prefix):
            return value[len(prefix) :]
    return ""


class RepairActionError(RuntimeError):
    """Raised when a repair action cannot be applied deterministically."""


def _apply_one_action(
    path: Path,
    rel_path: str,
    action: str,
    row: Mapping[str, Any],
) -> dict[str, Any] | None:
    before_text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    before_hash = _sha256(before_text) if path.exists() else ""
    before_api = public_api(path) if path.exists() and path.suffix == ".py" else []

    if action in {"no_change", "skip"}:
        return {
            "action": "no_change",
            "path": rel_path,
            "before_hash": before_hash,
            "after_hash": before_hash,
            "before_public_api": before_api,
            "after_public_api": before_api,
            "rationale": _string(row.get("rationale")) or _string(row.get("reason")),
            "changed": False,
        }
    if action in {"replace_block", "replace_text"}:
        if not path.is_file():
            raise RepairActionError("target_file_missing")
        old = _string(row.get("old_string")) or _string(row.get("old"))
        new = _string(row.get("new_string")) or _string(row.get("new"))
        if not old:
            raise RepairActionError("old_string_missing")
        if not new:
            raise RepairActionError("new_string_missing")
        occurrences = before_text.count(old)
        if occurrences == 0:
            raise RepairActionError("old_string_not_found")
        if occurrences > 1:
            raise RepairActionError(f"old_string_matched_{occurrences}_times")
        after_text = before_text.replace(old, new, 1)
    elif action == "rewrite_function":
        if not path.is_file():
            raise RepairActionError("target_file_missing")
        function_name = _string(row.get("function_name")) or _function_name_from_new_source(row.get("new_source"))
        new_source = _string(row.get("new_source")) or _string(row.get("content"))
        if not function_name:
            raise RepairActionError("function_name_missing")
        if not new_source:
            raise RepairActionError("new_source_missing")
        after_text = _replace_function_source(before_text, function_name=function_name, new_source=new_source)
    elif action == "rewrite_file":
        content = _string(row.get("content")) or _string(row.get("new_string")) or _string(row.get("new"))
        if not content:
            raise RepairActionError("content_missing")
        after_text = content.rstrip() + "\n"
    elif action == "add_file":
        if path.exists() and (not path.is_file() or path.stat().st_size > 0):
            raise RepairActionError("target_file_already_exists")
        content = _string(row.get("content")) or _string(row.get("new_string")) or _string(row.get("new"))
        if not content:
            raise RepairActionError("content_missing")
        after_text = content.rstrip() + "\n"
    elif action == "remove_file":
        if not path.is_file():
            raise RepairActionError("target_file_missing")
        path.unlink()
        return {
            "action": action,
            "path": rel_path,
            "before_hash": before_hash,
            "after_hash": "",
            "before_public_api": before_api,
            "after_public_api": [],
            "rationale": _string(row.get("rationale")) or _string(row.get("reason")),
        }
    else:
        raise RepairActionError(f"unsupported_action:{action or '<empty>'}")

    if after_text == before_text:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(after_text, encoding="utf-8")
    after_api = public_api(path) if path.suffix == ".py" else []
    return {
        "action": action,
        "path": rel_path,
        "before_hash": before_hash,
        "after_hash": _sha256(after_text),
        "before_public_api": before_api,
        "after_public_api": after_api,
        "public_api_changed": before_api != after_api,
        "rationale": _string(row.get("rationale")) or _string(row.get("reason")),
    }


def _replace_function_source(source: str, *, function_name: str, new_source: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise RepairActionError(f"cannot_parse_target:{exc.msg}") from exc
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            matches.append(node)
    if not matches:
        raise RepairActionError("function_not_found")
    if len(matches) > 1:
        raise RepairActionError(f"function_matched_{len(matches)}_times")
    node = matches[0]
    start = getattr(node, "lineno", 0)
    end = getattr(node, "end_lineno", 0)
    if start <= 0 or end < start:
        raise RepairActionError("function_location_unavailable")
    lines = source.splitlines()
    replacement = _align_replacement_indent(
        new_source.rstrip("\n").splitlines(),
        col_offset=int(getattr(node, "col_offset", 0) or 0),
    )
    return "\n".join([*lines[: start - 1], *replacement, *lines[end:]]) + "\n"


def _align_replacement_indent(lines: list[str], *, col_offset: int) -> list[str]:
    if col_offset <= 0 or not lines:
        return lines
    first = next((line for line in lines if line.strip()), "")
    if first.startswith(" " * col_offset):
        return lines
    return [(" " * col_offset + line if line.strip() else line) for line in lines]


def _function_name_from_new_source(value: object) -> str:
    text = _string(value)
    if not text:
        return ""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return ""


def _workspace_file(workspace: Path, rel_path: str) -> Path | None:
    target = (workspace / rel_path).resolve()
    try:
        target.relative_to(workspace)
    except ValueError:
        return None
    return target


def _has_file_ancestor(path: Path, workspace: Path) -> bool:
    current = path.parent
    while current != workspace:
        if current.exists() and not current.is_dir():
            return True
        if workspace not in current.parents:
            break
        current = current.parent
    return False


def _action_type(row: Mapping[str, Any]) -> str:
    return _string(row.get("action")) or _string(row.get("type"))


def _reject(result: dict[str, Any], index: int, row: Mapping[str, Any], reason: str) -> None:
    result["rejected_actions"].append(
        {
            "index": index,
            "path": normalize_action_path(str(row.get("path", ""))),
            "action": _action_type(row),
            "reason": reason,
            "rationale": _string(row.get("rationale")) or _string(row.get("reason")),
        }
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""
