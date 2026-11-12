from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable


PRIVATE_PATH_COMPONENTS = frozenset({'.git', '.aws', '.ssh', '.codex', '.agents', '__pycache__', '.venv', 'venv'})


def file_stat_version(info: os.stat_result) -> tuple[int, int, int, int, int]:
    """Snapshot a file using timestamps from the same stat source."""
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def is_private_path_name(name: str, *, allow_env_example: bool = False) -> bool:
    """Identify task-private path components without opening their contents."""
    name = name.casefold()
    if name.startswith('.env'):
        return not (allow_env_example and name.endswith('.example'))
    return name in PRIVATE_PATH_COMPONENTS


def replace_exact_spans(text: str, replacements: Iterable[tuple[str, str]]) -> str:
    """Apply unique, non-overlapping replacements against one original text."""
    spans = []
    for number, (old, new) in enumerate(replacements, 1):
        if not old:
            raise ValueError('Replacement text must not be empty')
        start = text.find(old)
        second = text.find(old, start + 1) if start >= 0 else -1
        if start < 0 or second >= 0:
            locations = [text.count('\n', 0, offset) + 1
                         for offset in (start, second) if offset >= 0]
            raise ValueError(f'Replacement {number} must match exactly once; '
                f'first matching lines: {locations}. Include unique context; no edits were written.')
        spans.append((start, start + len(old), new))
    if not spans:
        raise ValueError('Supply at least one replacement')
    spans.sort()
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError('Edit spans overlap; use one replacement for their combined context')
    for start, end, replacement in reversed(spans):
        text = text[:start] + replacement + text[end:]
    return text


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: Any) -> None:
    """Write one JSON artifact without exposing a partially written file."""

    write_atomically(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_atomically(path: Path, content: str | bytes, *, overwrite: bool = True) -> None:
    """Publish a complete sibling file, optionally refusing to replace an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb" if isinstance(content, bytes) else "w",
            encoding=None if isinstance(content, bytes) else "utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
        if overwrite:
            _replace_file(temporary_path, path)
            temporary_path = None
        elif os.name == "nt":
            # Windows rename refuses an existing destination; unlike a hard
            # link it also works on common Windows filesystems without links.
            os.rename(temporary_path, path)
            temporary_path = None
        else:
            # A same-directory hard link publishes fully written bytes in one
            # operation and fails if the destination appeared concurrently.
            os.link(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _replace_file(source: Path, target: Path) -> None:
    """Replace a file, tolerating short Windows antivirus/indexer locks."""

    for attempt in range(4):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


def read_json(path: Path) -> Any:
    return json.loads(read_text(path))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    write_text(path, text)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # JSONL records are LF-delimited. Unicode separators are legal inside JSON
    # strings and must not be interpreted as additional record boundaries.
    for line in read_text(path).split("\n"):
        stripped = line.strip()
        if stripped:
            rows.append(json.loads(stripped))
    return rows


def relative_to_run(run_dir: Path, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(run_dir)).replace("\\", "/")
    except ValueError:
        return str(path)
