from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
import tempfile
import uuid
from typing import Any, Callable, Iterator, Mapping


@contextmanager
def generation_candidate(destination: Path, *, validate: Callable[[Path], None] | None = None) -> Iterator[Path]:
    """Publish completed files, retaining old projects and failed candidates.

    The caller owns the exact output directory. This is not a session transaction
    or a lock: its existing task owner must serialize generation and metadata.
    """
    destination = Path(destination).absolute()
    if (destination == Path(destination.anchor) or destination == Path.cwd().absolute()
            or any(path.is_symlink() for path in (destination, *destination.parents))
            or (destination.exists() and not destination.is_dir())):
        raise ValueError("Generation requires a concrete, non-symlink output directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=f".{destination.name}.candidate-", dir=destination.parent))
    try:
        yield candidate
        if validate is not None:
            validate(candidate)
    except BaseException as exc:
        exc.add_note(f"Generated candidate retained at {candidate}; previous output was not replaced")
        raise
    previous = None
    if destination.exists():
        previous = destination.with_name(f".{destination.name}.previous-{uuid.uuid4().hex}")
        destination.rename(previous)
    try:
        candidate.rename(destination)
    except BaseException:
        if previous is not None:
            previous.rename(destination)
        raise


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
