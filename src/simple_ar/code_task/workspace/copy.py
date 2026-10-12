from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable


DEFAULT_IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".simple_ar_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "runs",
    "venv",
}

DEFAULT_IGNORED_FILE_NAMES = {
    ".DS_Store",
    ".env",
}

DEFAULT_IGNORED_SUFFIXES = {
    ".pyc",
    ".pyd",
    ".pyo",
}

DEFAULT_SPARSE_EXCLUDE_PATTERNS = (
    ".git/**",
    ".hg/**",
    ".svn/**",
    ".venv/**",
    "venv/**",
    "__pycache__/**",
    "**/__pycache__/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    ".simple_ar_cache/**",
    "node_modules/**",
    "build/**",
    "dist/**",
    "runs/**",
    "data/**",
    "**/data/**",
    "models/**",
    "**/models/**",
    "cache/**",
    ".cache/**",
    "**/.cache/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*secret*",
    "**/*secret*",
    "*credential*",
    "**/*credential*",
)

DEFAULT_SPARSE_INCLUDE_PATTERNS = (
    "*.py",
    "**/*.py",
    "*.toml",
    "*.cfg",
    "*.ini",
    "*.yaml",
    "*.yml",
    "*.txt",
    "*.md",
    "requirements*.txt",
    "uv.lock",
    "Pipfile",
    "poetry.lock",
    "src/**",
    "tests/**",
    "configs/**",
    "config/**",
    "benchmark.py",
    "**/benchmark.py",
    "bench.py",
    "**/bench.py",
)

MAX_RECORDED_SKIPS = 200


@dataclass(frozen=True)
class CopyReport:
    """Summary of a safe workspace copy operation.

    Args:
        files_copied: Number of regular files copied into the workspace.
        bytes_copied: Total bytes copied.
        skipped_count: Total number of skipped files and directories.
        skipped: Bounded list of skipped files or directories with reasons.
    """

    files_copied: int
    bytes_copied: int
    skipped_count: int
    skipped: tuple[dict[str, Any], ...]

    def to_json(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the copy report."""
        return {
            "files_copied": self.files_copied,
            "bytes_copied": self.bytes_copied,
            "skipped_count": self.skipped_count,
            "skipped_record_limit": MAX_RECORDED_SKIPS,
            "skipped": list(self.skipped),
        }


def empty_copy_report() -> CopyReport:
    """Return an empty copy report for non-copy workspace modes."""
    return CopyReport(
        files_copied=0,
        bytes_copied=0,
        skipped_count=0,
        skipped=(),
    )


def copy_workspace_inputs(
    source: Path, workspace: Path, paths: tuple[str, ...], report: CopyReport,
) -> CopyReport:
    """Materialize explicitly named project data, without linking to originals.

    Size limits for incidental source discovery do not apply to declared inputs.
    Other copy exclusions still apply. Validate the entire selection and disk
    capacity before overlaying inputs, including untracked data in worktrees.
    """
    selected: dict[str, Path] = {}
    for relative in paths:
        name = Path(relative)
        if name.is_absolute() or not name.parts or ".." in name.parts:
            raise ValueError(f"Data input must be project-relative: {relative}")
        target = source / name
        for ancestor in (target, *target.parents):
            if ancestor == source:
                break
            reason = (_skip_dir_reason(ancestor, source, workspace) if ancestor.is_dir()
                      else _skip_file_reason(ancestor, max_file_bytes=0))
            if reason:
                raise ValueError(f"Data input is excluded ({reason}): {relative}")
        if not target.exists():
            raise FileNotFoundError(f"Declared project data not found: {relative}")
        files = [target] if target.is_file() else []
        if target.is_dir():
            for current, directories, filenames in os.walk(target):
                directory = Path(current)
                directories[:] = [name for name in directories
                                  if not _skip_dir_reason(directory / name, source, workspace)]
                files.extend(directory / name for name in filenames
                             if not _skip_file_reason(directory / name, max_file_bytes=0))
        for path in files:
            if not path.is_file():
                raise ValueError(f"Data input is not a regular file: {path}")
            selected[path.relative_to(source).as_posix()] = path
    total = sum(path.stat().st_size for path in selected.values())
    if total > shutil.disk_usage(workspace).free:
        raise OSError(f"Declared project data needs {total} bytes; insufficient workspace disk space.")
    for relative in selected:
        destination = workspace / relative
        for ancestor in (destination, *destination.parents):
            if ancestor == workspace:
                break
            if ancestor.is_symlink():
                raise ValueError(f"Data destination is a symlink: {relative}")
        _safe_destination(workspace, Path(relative))
    added_files = added_bytes = 0
    for relative, path in selected.items():
        destination = _safe_destination(workspace, Path(relative))
        # Ordinary copies already contain smaller inputs. Worktrees may contain
        # a committed version; the explicitly supplied input is authoritative.
        if not destination.exists() or report.files_copied == 0:
            added_files += 1
            added_bytes += path.stat().st_size
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    remaining = tuple(row for row in report.skipped
                      if not (row["kind"] == "file" and row["path"] in selected))
    return CopyReport(report.files_copied + added_files, report.bytes_copied + added_bytes,
                      max(0, report.skipped_count - added_files), remaining)


def copy_code_workspace(
    code_root: Path,
    workspace_dir: Path,
    *,
    max_file_bytes: int = 2_000_000,
    max_files: int | None = None,
    max_total_bytes: int | None = None,
    path_allowed: Callable[[Path], bool] | None = None,
) -> CopyReport:
    """Copy a code root into an isolated, editable workspace.

    The copy is intentionally conservative: it skips common cache/build
    directories, secrets such as ``.env``, symlinks, bytecode, and files above
    ``max_file_bytes``. Later code-task stages should mutate only this copied
    workspace, never the original source directory.

    Args:
        code_root: Existing project or benchmark directory to copy.
        workspace_dir: Destination workspace inside the code-task run.
        max_file_bytes: Maximum size for files to copy. Use ``0`` to disable
            this size guard.
        max_files: Optional bounded selection, checked before copying.
        max_total_bytes: Optional total size bound, checked before copying.
        path_allowed: Consumer access rule, checked before any file is read.

    Returns:
        Summary of copied and skipped paths.

    Raises:
        FileNotFoundError: If ``code_root`` does not exist.
        NotADirectoryError: If ``code_root`` is not a directory.
        FileExistsError: If ``workspace_dir`` already contains files.
    """
    source = Path(code_root).resolve()
    workspace = Path(workspace_dir).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Code root does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Code root is not a directory: {source}")
    if max_file_bytes < 0:
        raise ValueError("max_file_bytes must be >= 0")
    if any(value is not None and (type(value) is not int or value <= 0)
           for value in (max_files, max_total_bytes)):
        raise ValueError("Copy selection limits must be positive integers")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"Workspace already contains files: {workspace}")

    skipped: list[dict[str, Any]] = []
    skipped_count = 0
    files_copied = 0
    bytes_copied = 0
    selected: list[tuple[Path, Path]] = []
    selection_bytes = 0
    entries_seen = 0

    for current, dirnames, filenames in os.walk(source):
        entries_seen += len(dirnames) + len(filenames)
        if max_files is not None and entries_seen > max_files * 16:
            raise ValueError("Project traversal exceeds its bounded selection; choose a smaller source")
        current_path = Path(current)
        filtered_dirnames: list[str] = []
        for dirname in dirnames:
            path = current_path / dirname
            reason = (_skip_dir_reason(path, source, workspace)
                      or ("access_excluded" if path_allowed and not path_allowed(path) else None))
            if reason:
                skipped_count += 1
                _record_skip(skipped, source, path, "dir", reason)
                continue
            filtered_dirnames.append(dirname)
        dirnames[:] = filtered_dirnames

        for filename in filenames:
            path = current_path / filename
            reason = ("access_excluded" if path_allowed and not path_allowed(path)
                      else _skip_file_reason(path, max_file_bytes=max_file_bytes))
            if reason:
                skipped_count += 1
                _record_skip(skipped, source, path, "file", reason)
                continue

            relative_path = path.relative_to(source)
            _safe_destination(workspace, relative_path)
            selected.append((path, relative_path))
            selection_bytes += path.stat().st_size
            if max_files is not None and len(selected) > max_files:
                raise ValueError("Project exceeds max_files; choose a smaller source or an explicit larger bound")
            if max_total_bytes is not None and selection_bytes > max_total_bytes:
                raise ValueError("Project exceeds max_total_bytes; choose a smaller source or an explicit larger bound")

    # One owner supplies legacy initialization and native project preparation.
    # Reject quota overflow before copying, not after publishing a partial tree.
    workspace.mkdir(parents=True, exist_ok=True)
    if max_total_bytes is not None and shutil.disk_usage(workspace).free < selection_bytes:
        raise ValueError("Insufficient disk space for the selected project")
    for path, relative_path in selected:
        if (path.is_symlink() or not path.resolve().is_relative_to(source)
                or (path_allowed and not path_allowed(path))):
            raise ValueError("Project source changed or became inaccessible during copying")
        destination = _safe_destination(workspace, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination, follow_symlinks=False)
        copied_size = destination.stat().st_size
        if destination.is_symlink() or (max_file_bytes > 0 and copied_size > max_file_bytes):
            raise ValueError("Project source changed during copying")
        files_copied += 1
        bytes_copied += copied_size
        if max_total_bytes is not None and bytes_copied > max_total_bytes:
            raise ValueError("Project source grew beyond max_total_bytes during copying")

    return CopyReport(
        files_copied=files_copied,
        bytes_copied=bytes_copied,
        skipped_count=skipped_count,
        skipped=tuple(skipped),
    )


def sparse_copy_code_workspace(
    code_root: Path,
    workspace_dir: Path,
    *,
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    max_file_bytes: int = 2_000_000,
) -> CopyReport:
    """Copy selected source files into an isolated workspace.

    Sparse copy is intentionally experimental. It is useful when the user knows
    the project subset needed for a task, but it can omit runtime dependencies
    in larger projects. Exclude rules always include cache/build/data/model and
    secret-like paths before user-supplied patterns are applied.

    Args:
        code_root: Existing project or benchmark directory to copy from.
        workspace_dir: Destination workspace inside the code-task run.
        include_patterns: POSIX-style glob patterns to copy. When omitted,
            conservative source/config/test defaults are used.
        exclude_patterns: Additional POSIX-style glob patterns to skip.
        max_file_bytes: Maximum size for files to copy. Use ``0`` to disable
            this size guard.

    Returns:
        Summary of copied and skipped paths.
    """
    source = Path(code_root).resolve()
    workspace = Path(workspace_dir).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Code root does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Code root is not a directory: {source}")
    if max_file_bytes < 0:
        raise ValueError("max_file_bytes must be >= 0")
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"Workspace already contains files: {workspace}")

    includes = normalize_copy_patterns(include_patterns or DEFAULT_SPARSE_INCLUDE_PATTERNS)
    excludes = normalize_copy_patterns(DEFAULT_SPARSE_EXCLUDE_PATTERNS + tuple(exclude_patterns))
    workspace.mkdir(parents=True, exist_ok=True)
    skipped: list[dict[str, Any]] = []
    skipped_count = 0
    files_copied = 0
    bytes_copied = 0

    for current, dirnames, filenames in os.walk(source):
        current_path = Path(current)
        filtered_dirnames: list[str] = []
        for dirname in dirnames:
            path = current_path / dirname
            rel_path = _relative_posix(source, path)
            reason = _skip_dir_reason(path, source, workspace) or _sparse_dir_skip_reason(
                rel_path,
                exclude_patterns=excludes,
            )
            if reason:
                skipped_count += 1
                _record_skip(skipped, source, path, "dir", reason)
                continue
            filtered_dirnames.append(dirname)
        dirnames[:] = filtered_dirnames

        for filename in filenames:
            path = current_path / filename
            rel_path = _relative_posix(source, path)
            reason = (
                _skip_file_reason(path, max_file_bytes=max_file_bytes)
                or _sparse_file_skip_reason(
                    rel_path,
                    include_patterns=includes,
                    exclude_patterns=excludes,
                )
            )
            if reason:
                skipped_count += 1
                _record_skip(skipped, source, path, "file", reason)
                continue

            destination = _safe_destination(workspace, Path(rel_path))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            copied_size = destination.stat().st_size
            files_copied += 1
            bytes_copied += copied_size

    return CopyReport(
        files_copied=files_copied,
        bytes_copied=bytes_copied,
        skipped_count=skipped_count,
        skipped=tuple(skipped),
    )


def _skip_dir_reason(path: Path, source: Path, workspace: Path) -> str | None:
    name = path.name
    if path.is_symlink():
        return "symlink_dir"
    if name in DEFAULT_IGNORED_DIR_NAMES or name.startswith("."):
        return "ignored_dir"
    resolved = path.resolve()
    if workspace.is_relative_to(resolved) or resolved.is_relative_to(workspace):
        return "output_workspace"
    if not resolved.is_relative_to(source):
        return "outside_source"
    return None


def _skip_file_reason(path: Path, *, max_file_bytes: int) -> str | None:
    name = path.name
    if path.is_symlink():
        return "symlink_file"
    if name in DEFAULT_IGNORED_FILE_NAMES:
        return "ignored_file"
    if name.startswith(".env") and not name.endswith(".example"):
        return "possible_secret"
    if path.suffix.lower() in DEFAULT_IGNORED_SUFFIXES:
        return "ignored_suffix"
    try:
        size = path.stat().st_size
    except OSError:
        return "stat_failed"
    if max_file_bytes > 0 and size > max_file_bytes:
        return "file_too_large"
    return None


def _safe_destination(workspace: Path, relative_path: Path) -> Path:
    destination = (workspace / relative_path).resolve()
    if not destination.is_relative_to(workspace):
        raise ValueError(f"Refusing to copy outside workspace: {relative_path}")
    return destination


def _record_skip(
    skipped: list[dict[str, Any]],
    source: Path,
    path: Path,
    kind: str,
    reason: str,
) -> None:
    if len(skipped) >= MAX_RECORDED_SKIPS:
        return
    try:
        relative = path.relative_to(source).as_posix()
    except ValueError:
        relative = str(path)
    skipped.append({"path": relative, "kind": kind, "reason": reason})


def normalize_copy_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Normalize sparse-copy filters for both copying and its manifest."""
    result: list[str] = []
    for pattern in patterns:
        text = str(pattern).replace("\\", "/").strip().strip("/")
        if text:
            result.append(text)
    return tuple(dict.fromkeys(result))


def _sparse_dir_skip_reason(
    relative_path: str,
    *,
    exclude_patterns: tuple[str, ...],
) -> str | None:
    rel = relative_path.rstrip("/")
    if _matches_any(rel, exclude_patterns) or _matches_any(rel + "/placeholder", exclude_patterns):
        return "sparse_excluded_dir"
    return None


def _sparse_file_skip_reason(
    relative_path: str,
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> str | None:
    if _matches_any(relative_path, exclude_patterns):
        return "sparse_excluded_file"
    if not _matches_any(relative_path, include_patterns):
        return "sparse_not_included"
    return None


def _matches_any(relative_path: str, patterns: tuple[str, ...]) -> bool:
    path = relative_path.replace("\\", "/").strip("/")
    return any(fnmatchcase(path, pattern) for pattern in patterns)


def _relative_posix(source: Path, path: Path) -> str:
    try:
        return path.relative_to(source).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")
