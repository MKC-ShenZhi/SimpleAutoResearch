from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

from simple_ar.core.artifacts import generation_candidate

__all__ = [
    "generation_candidate",
    "safe_relative_path",
    "text",
    "string_list",
    "mapping_list",
    "scalar_list",
]


def safe_relative_path(value: str) -> str:
    """Return a normalized relative POSIX path, or an empty string if unsafe."""

    text = value.replace("\\", "/").strip()
    path = PurePosixPath(text)
    windows = PureWindowsPath(text)
    if (not path.parts or path.is_absolute() or windows.drive or windows.root
            or any(part == ".." or ":" in part or "\0" in part for part in path.parts)):
        return ""
    return path.as_posix()


def text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def string_list(value: Any, *, limit: int | None = None, tail: bool = False) -> list[str]:
    rows = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
    if limit is None:
        return rows
    return rows[-limit:] if tail else rows[:limit]


def mapping_list(value: Any, *, limit: int | None = None, tail: bool = False) -> list[dict[str, Any]]:
    rows = [dict(row) for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []
    if limit is None:
        return rows
    return rows[-limit:] if tail else rows[:limit]


def scalar_list(value: object, *, limit: int | None = None, tail: bool = False) -> list[str]:
    if isinstance(value, list):
        rows = [str(item).strip() for item in value if str(item).strip()]
    elif isinstance(value, str) and value.strip():
        rows = [value.strip()]
    else:
        rows = []
    if limit is None:
        return rows
    return rows[-limit:] if tail else rows[:limit]
