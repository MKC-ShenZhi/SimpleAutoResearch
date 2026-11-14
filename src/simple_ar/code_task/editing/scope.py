from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any, Iterable


_PRIVATE_EDIT_PATTERNS = (
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*secret*",
    "**/*secret*",
    "*credential*",
    "**/*credential*",
)

DEFAULT_PROTECTED_EDIT_PATTERNS = (
    "tests/**",
    "**/tests/**",
    "test_*.py",
    "**/test_*.py",
    "*_test.py",
    "**/*_test.py",
    "conftest.py",
    "**/conftest.py",
    "benchmark.py",
    "**/benchmark.py",
    "bench.py",
    "**/bench.py",
    "*benchmark*.py",
    "**/*benchmark*.py",
    *_PRIVATE_EDIT_PATTERNS,
)

DEFAULT_EDIT_SCOPE_MODE = "source_only_default"
DEFAULT_ALLOWED_EDIT_PATTERNS: tuple[str, ...] = ()


def default_edit_scope(
    *,
    mode: str | None = None,
    allowed_patterns: Iterable[str] | None = None,
    protected_patterns: Iterable[str] | None = None,
    respect_allowed_paths: bool = False,
) -> dict[str, Any]:
    """Return the default code-task edit policy stored in manifests.

    Standalone and historical tasks retain the default read-only test and
    benchmark policy. A task with its own explicit edit authority may instead
    allow those paths; its caller still supplies protected validation inputs.
    """

    allowed = _normalized_unique(allowed_patterns or DEFAULT_ALLOWED_EDIT_PATTERNS)
    defaults = _PRIVATE_EDIT_PATTERNS if respect_allowed_paths else DEFAULT_PROTECTED_EDIT_PATTERNS
    protected = _normalized_unique([*defaults, *(protected_patterns or ())])
    return {
        "mode": mode or DEFAULT_EDIT_SCOPE_MODE,
        "allowed_patterns": list(allowed),
        "protected_patterns": list(protected),
        "notes": [
            "When allowed_patterns is empty, any normalized workspace path may be edited unless protected.",
            "When allowed_patterns is set, patch proposals must match one allowed pattern.",
            "Protected files may be indexed and read as context.",
            "Patch proposals and apply-edits must not modify protected files.",
        ],
    }


def allowed_patterns_from_manifest(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Resolve edit allowlist patterns from a code-task manifest.

    An empty tuple means the run uses the historical behavior: all normalized
    workspace paths are candidates unless they match a protected pattern.
    """

    edit_scope = manifest.get("edit_scope", {})
    if not isinstance(edit_scope, dict):
        return DEFAULT_ALLOWED_EDIT_PATTERNS
    patterns = edit_scope.get("allowed_patterns")
    if not isinstance(patterns, list):
        return DEFAULT_ALLOWED_EDIT_PATTERNS
    return _normalized_unique(patterns)


def protected_patterns_from_manifest(manifest: dict[str, Any]) -> tuple[str, ...]:
    """Resolve protected edit patterns from a code-task manifest.

    Older runs do not have an ``edit_scope`` section, so callers fall back to
    the default source-only policy.
    """

    edit_scope = manifest.get("edit_scope", {})
    if not isinstance(edit_scope, dict):
        return DEFAULT_PROTECTED_EDIT_PATTERNS
    patterns = edit_scope.get("protected_patterns")
    if not isinstance(patterns, list):
        return DEFAULT_PROTECTED_EDIT_PATTERNS
    normalized = _normalized_unique(patterns)
    return normalized or DEFAULT_PROTECTED_EDIT_PATTERNS


def is_protected_edit_path(
    relative_path: str,
    *,
    protected_patterns: Iterable[str] | None = None,
) -> bool:
    """Return whether a workspace-relative path is read-only for patches."""

    normalized = normalize_workspace_path(relative_path)
    if not normalized:
        return True
    patterns = tuple(protected_patterns or DEFAULT_PROTECTED_EDIT_PATTERNS)
    lower_path = normalized.lower()
    return any(fnmatchcase(lower_path, pattern.lower()) for pattern in patterns)


def is_edit_allowed_path(
    relative_path: str,
    *,
    allowed_patterns: Iterable[str] | None = None,
    protected_patterns: Iterable[str] | None = None,
) -> bool:
    """Return whether a workspace-relative path may be modified."""

    return edit_scope_rejection_reason(
        relative_path,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
    ) is None


def edit_scope_rejection_reason(
    relative_path: str,
    *,
    allowed_patterns: Iterable[str] | None = None,
    protected_patterns: Iterable[str] | None = None,
) -> str | None:
    """Return why a path is not editable, or ``None`` when it is allowed."""

    normalized = normalize_workspace_path(relative_path)
    if not normalized:
        return "invalid_workspace_path"
    allowed = tuple(allowed_patterns or DEFAULT_ALLOWED_EDIT_PATTERNS)
    if allowed:
        lower_path = normalized.lower()
        if not any(fnmatchcase(lower_path, pattern.lower()) for pattern in allowed):
            return "outside_allowed_patterns"
    if is_protected_edit_path(normalized, protected_patterns=protected_patterns):
        return "protected_path"
    return None


def editable_paths(
    paths: Iterable[str],
    *,
    allowed_patterns: Iterable[str] | None = None,
    protected_patterns: Iterable[str] | None = None,
) -> list[str]:
    """Return paths that may be used as editable patch context."""

    result: list[str] = []
    for path in paths:
        normalized = normalize_workspace_path(path)
        if normalized and is_edit_allowed_path(
            normalized,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        ):
            result.append(normalized)
    return result


def normalize_workspace_path(path: str) -> str:
    """Normalize a user or model supplied path to a POSIX relative form."""

    text = str(path).replace("\\", "/").strip()
    if not text:
        return ""
    pure = PurePosixPath(text)
    if pure.is_absolute() or ".." in pure.parts:
        return ""
    return pure.as_posix()


def editable_context_files(
    index: dict[str, Any], selected_files: list[str], *,
    allowed_patterns: tuple[str, ...], protected_patterns: tuple[str, ...],
    max_files: int,
) -> list[str]:
    """Select editable context, using indexed source files only as a fallback."""
    limit = max(1, max_files)
    selected = editable_paths(
        selected_files, allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
    )
    if selected:
        return selected[:limit]
    files = index.get("files", [])
    if not isinstance(files, list):
        return []
    fallback: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("path")
        path = raw_path.strip() if isinstance(raw_path, str) else ""
        if not path or not is_edit_allowed_path(
            path, allowed_patterns=allowed_patterns, protected_patterns=protected_patterns,
        ):
            continue
        raw_kind = item.get("kind")
        kind = raw_kind.strip() if isinstance(raw_kind, str) else ""
        role_tags = [tag for tag in item.get("role_tags", []) if isinstance(tag, str)]
        if kind == "python" or "source" in role_tags:
            fallback.append(path)
        if len(fallback) >= limit:
            break
    return fallback


def _normalize_pattern(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\\", "/").strip()


def prompt_file_inventory(
    index: dict[str, Any], *, allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...], selected_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Keep every indexed path without repeating all repository signatures.

    Discovery uses the complete role-separated path lists. Short indexed hints
    are useful for editable files and selected evidence; exact API contracts
    belong to the supplied source snippets, not an unbounded AST dump. This is
    a prompt projection only: the stored index and enforcement stay unchanged.
    """
    selected = set(selected_paths)
    inventory: dict[str, Any] = {
        "project": index.get("project", {}), "editable_files": [],
        "read_only_files": [], "file_details": [],
    }
    for item in index.get("files", []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", ""))
        editable = is_edit_allowed_path(
            path, allowed_patterns=allowed_patterns, protected_patterns=protected_patterns,
        )
        inventory["editable_files" if editable else "read_only_files"].append(path)
        if editable or path in selected:
            inventory["file_details"].append({
                "path": path, "kind": item.get("kind"),
                "role_tags": item.get("role_tags", []), "summary": item.get("summary", ""),
            })
    return inventory


def _normalized_unique(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        pattern = _normalize_pattern(value)
        if pattern and pattern not in result:
            result.append(pattern)
    return tuple(result)
