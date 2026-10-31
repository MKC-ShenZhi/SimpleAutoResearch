from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Callable

from simple_ar.code_task.analysis.index import _iter_files, _sha256, is_private_dotenv, is_python_environment


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
    selected = _select_data_inputs(source, workspace, paths)
    total = sum(path.stat().st_size for path in selected.values())
    if total > shutil.disk_usage(workspace).free:
        raise OSError(f"Declared project data needs {total} bytes; insufficient workspace disk space.")
    for relative in selected:
        destination = workspace / relative
        for ancestor in (destination, *destination.parents):
            if ancestor == workspace:
                break
            if _is_link_or_reparse(ancestor):
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
        _validate_source_file(source, path)
        shutil.copy2(path, destination, follow_symlinks=False)
    remaining = tuple(row for row in report.skipped
                      if not (row["kind"] == "file" and row["path"] in selected))
    return CopyReport(report.files_copied + added_files, report.bytes_copied + added_bytes,
                      max(0, report.skipped_count - added_files), remaining)


def _select_data_inputs(source: Path, workspace: Path, paths: tuple[str, ...]) -> dict[str, Path]:
    """Shared declaration validation; intentionally no incidental size ceiling."""
    _validate_root_boundary(source)
    selected: dict[str, Path] = {}
    for relative in paths:
        name = Path(relative)
        if name.anchor or not name.parts or ".." in name.parts:
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
    return selected


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
    selection = _select_workspace_files(
        code_root, workspace_dir, max_file_bytes=max_file_bytes,
        max_files=max_files, max_total_bytes=max_total_bytes, path_allowed=path_allowed,
    )
    return _materialize_selection(selection)


@dataclass(frozen=True)
class _CopySelection:
    source: Path
    workspace: Path
    files: tuple[tuple[Path, Path], ...]
    report: CopyReport
    max_file_bytes: int
    max_total_bytes: int | None
    path_allowed: Callable[[Path], bool] | None


def _select_workspace_files(
    code_root: Path, workspace_dir: Path, *, max_file_bytes: int = 2_000_000,
    max_files: int | None = None, max_total_bytes: int | None = None,
    path_allowed: Callable[[Path], bool] | None = None,
    sparse: bool = False, include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    check_destination: bool = True,
) -> _CopySelection:
    """Select once for materialization and approval; never create a destination."""
    _validate_root_boundary(Path(code_root))
    if check_destination:
        _validate_root_boundary(Path(workspace_dir))
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
    if check_destination and workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"Workspace already contains files: {workspace}")

    includes = normalize_copy_patterns(include_patterns or DEFAULT_SPARSE_INCLUDE_PATTERNS)
    excludes = normalize_copy_patterns(DEFAULT_SPARSE_EXCLUDE_PATTERNS + tuple(exclude_patterns))
    skipped: list[dict[str, Any]] = []
    skipped_count = 0
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
                      or ("access_excluded" if path_allowed and not path_allowed(path) else None)
                      or (_sparse_dir_skip_reason(_relative_posix(source, path), exclude_patterns=excludes)
                          if sparse else None))
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
            reason = reason or (_sparse_file_skip_reason(
                _relative_posix(source, path), include_patterns=includes, exclude_patterns=excludes,
            ) if sparse else None)
            if reason:
                skipped_count += 1
                _record_skip(skipped, source, path, "file", reason)
                continue

            relative_path = path.relative_to(source)
            if check_destination:
                _safe_destination(workspace, relative_path)
            selected.append((path, relative_path))
            selection_bytes += path.stat().st_size
            if max_files is not None and len(selected) > max_files:
                raise ValueError("Project exceeds max_files; choose a smaller source or an explicit larger bound")
            if max_total_bytes is not None and selection_bytes > max_total_bytes:
                raise ValueError("Project exceeds max_total_bytes; choose a smaller source or an explicit larger bound")

    return _CopySelection(source, workspace, tuple(selected),
                          CopyReport(0, 0, skipped_count, tuple(skipped)),
                          max_file_bytes, max_total_bytes, path_allowed)


def _materialize_selection(selection: _CopySelection) -> CopyReport:
    """The sole source-copy writer for both full and sparse policies."""
    source, workspace = selection.source, selection.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    selection_bytes = sum(path.stat().st_size for path, _ in selection.files)
    if selection.max_total_bytes is not None and shutil.disk_usage(workspace).free < selection_bytes:
        raise ValueError("Insufficient disk space for the selected project")
    files_copied = bytes_copied = 0
    for path, relative_path in selection.files:
        _validate_source_file(source, path)
        if selection.path_allowed and not selection.path_allowed(path):
            raise ValueError("Project source became inaccessible during copying")
        destination = _safe_destination(workspace, relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination, follow_symlinks=False)
        copied_size = destination.stat().st_size
        if (_is_link_or_reparse(destination)
                or (selection.max_file_bytes > 0 and copied_size > selection.max_file_bytes)):
            raise ValueError("Project source changed during copying")
        files_copied += 1
        bytes_copied += copied_size
        if selection.max_total_bytes is not None and bytes_copied > selection.max_total_bytes:
            raise ValueError("Project source grew beyond max_total_bytes during copying")
    return CopyReport(files_copied, bytes_copied, selection.report.skipped_count,
                      selection.report.skipped)


class _SourceIndexBoundaryError(ValueError):
    """The unchanged copy policy cannot be projected through the source index."""


def _selection_snapshot(selection: _CopySelection, data_inputs: tuple[str, ...] = ()) -> dict[str, str]:
    """Hash the selected inventory with the index's own boundary and hash owner."""
    selected = {relative.as_posix(): path for path, relative in selection.files}
    selected.update(_select_data_inputs(selection.source, selection.workspace, data_inputs))
    for path in selected.values():
        for parent in path.parents:
            if parent == selection.source:
                break
            marker = parent / "pyvenv.cfg"
            if is_python_environment(parent) and marker.relative_to(selection.source).as_posix() not in selected:
                raise _SourceIndexBoundaryError(
                    "Copy selection omits a pyvenv.cfg marker while retaining environment files; "
                    "the source index cannot prove the resulting inventory without materialization."
                )
    hashes: dict[str, str] = {}
    for path in _iter_files(selection.source):
        relative = path.relative_to(selection.source).as_posix()
        if relative in selected:
            _validate_source_file(selection.source, path)
            hashes[relative] = _sha256(path)
    return dict(sorted(hashes.items()))


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
    return _materialize_selection(_select_workspace_files(
        code_root, workspace_dir, max_file_bytes=max_file_bytes, sparse=True,
        include_patterns=include_patterns, exclude_patterns=exclude_patterns,
    ))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        return (path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    except FileNotFoundError:
        return False


def _validate_root_boundary(path: Path) -> None:
    for ancestor in (path.absolute(), *path.absolute().parents):
        if _is_link_or_reparse(ancestor):
            raise ValueError(f"Linked or reparse source is not supported: {ancestor}")


def _validate_source_file(source: Path, path: Path) -> None:
    for ancestor in (path, *path.parents):
        if _is_link_or_reparse(ancestor):
            raise ValueError(f"Project source became linked: {path}")
        if ancestor == source:
            break
    if not path.resolve().is_relative_to(source) or not path.is_file():
        raise ValueError(f"Project source changed or is not a regular file: {path}")


def _skip_dir_reason(path: Path, source: Path, workspace: Path) -> str | None:
    name = path.name
    if _is_link_or_reparse(path):
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
    if _is_link_or_reparse(path):
        return "symlink_file"
    if name in DEFAULT_IGNORED_FILE_NAMES:
        return "ignored_file"
    if is_private_dotenv(name):
        return "possible_secret"
    if path.suffix.lower() in DEFAULT_IGNORED_SUFFIXES:
        return "ignored_suffix"
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return "not_regular_file"
        size = info.st_size
    except OSError:
        return "stat_failed"
    if max_file_bytes > 0 and size > max_file_bytes:
        return "file_too_large"
    return None


def _safe_destination(workspace: Path, relative_path: Path) -> Path:
    candidate = workspace / relative_path
    for ancestor in (candidate, *candidate.parents):
        if _is_link_or_reparse(ancestor):
            raise ValueError(f"Linked or reparse destination: {relative_path}")
        if ancestor == workspace:
            break
    destination = candidate.resolve()
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
