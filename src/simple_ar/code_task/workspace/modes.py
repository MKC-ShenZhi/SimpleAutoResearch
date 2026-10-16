from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from simple_ar.code_task.workspace.copy import (
    CopyReport,
    copy_code_workspace,
    copy_workspace_inputs,
    empty_copy_report,
    normalize_copy_patterns,
    sparse_copy_code_workspace,
    _select_workspace_files,
    _selection_snapshot,
    _select_data_inputs,
    _validate_root_boundary,
    _validate_source_file,
    _SourceIndexBoundaryError,
)
from simple_ar.code_task.analysis.index import IGNORED_DIR_NAMES, IGNORED_FILE_NAMES, _sha256
from simple_ar.code_task.runtime.state import utcnow_iso as _utcnow_iso


SUPPORTED_WORKSPACE_MODES = {"auto", "copy", "empty", "git_worktree", "sparse_copy"}
DEPENDENCY_FILE_NAMES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "environment.yaml",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
)


class WorkspaceModeError(RuntimeError):
    """Raised when a code-task workspace mode cannot be created safely."""


@dataclass(frozen=True)
class WorkspaceSpec:
    """Configuration for creating an editable code-task workspace.

    Args:
        code_root: Source project directory provided by the user. Greenfield
            runs may leave this empty when ``mode`` is ``empty``.
        task_dir: Code-task artifact directory.
        mode: Workspace strategy. Existing-project runs default to ``auto``,
            which prefers ``git_worktree`` for committed Git projects and
            falls back to ``copy`` with a recorded reason. ``sparse_copy`` is
            experimental; ``empty`` is used by greenfield code-task runs.
        max_file_bytes: Maximum copied file size for ``copy`` and
            ``sparse_copy`` modes.
        include: POSIX glob patterns copied by ``sparse_copy``.
        exclude: Additional POSIX glob patterns skipped by ``sparse_copy``.
        reuse_source_venv: Whether a detected source ``.venv`` may be recorded
            and selected as the initial execution interpreter.
        data_inputs: Explicit project-relative runtime inputs copied without the
            incidental size limit; source/secret/symlink exclusions still apply.
    """

    code_root: Path | None
    task_dir: Path
    mode: str = "copy"
    max_file_bytes: int = 2_000_000
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    reuse_source_venv: bool = False
    data_inputs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.max_file_bytes) is not int or self.max_file_bytes < 0:
            raise ValueError("max_file_bytes must be a non-negative integer")
        if type(self.reuse_source_venv) is not bool:
            raise ValueError("reuse_source_venv must be a boolean")
        for name in ("include", "exclude"):
            values = getattr(self, name)
            if not isinstance(values, (list, tuple)) or any(not isinstance(p, str) for p in values):
                raise ValueError(f"{name} must be a list of workspace pattern strings")


@dataclass(frozen=True)
class WorkspaceResult:
    """Result returned by workspace creation.

    Args:
        mode: Workspace strategy used.
        source_root: Resolved user source directory.
        workspace_dir: Isolated workspace root. In git worktree mode this is
            the repository worktree root.
        project_root: Project root consumed by downstream code-task stages. It
            may be a subdirectory inside ``workspace_dir`` for monorepo-style
            code roots.
        writable_root: Root that patching and benchmark execution may mutate.
        read_only_roots: Additional source roots used for provenance only.
        created_at: UTC creation timestamp.
        copy_report: Copy summary for ``copy`` mode; empty for worktrees.
        git: Git provenance for ``git_worktree`` mode.
        environment_mapping: Dependency and interpreter mapping notes.
        patterns: Include/exclude patterns used by sparse workspace modes.
        cleanup_hint: Human-facing cleanup note for this mode.
    """

    mode: str
    requested_mode: str
    selected_mode: str
    source_root: Path
    workspace_dir: Path
    project_root: Path
    writable_root: Path
    read_only_roots: tuple[Path, ...]
    created_at: str
    copy_report: CopyReport
    git: dict[str, Any] | None
    environment_mapping: dict[str, Any]
    patterns: dict[str, Any]
    cleanup_hint: str
    fallback_reason: str = ""
    warnings: tuple[str, ...] = ()
    user_next_steps: tuple[str, ...] = ()

    def to_manifest(self, *, run_dir: Path) -> dict[str, Any]:
        """Return a compact manifest section for this workspace."""
        return {
            "schema_version": 1,
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "selected_mode": self.selected_mode,
            "fallback_reason": self.fallback_reason,
            "source_root": str(self.source_root),
            "workspace_dir": _relative_or_string(run_dir, self.workspace_dir),
            "project_root": _relative_or_string(run_dir, self.project_root),
            "project_relative_path": _project_relative_path(
                self.workspace_dir,
                self.project_root,
            ),
            "writable_root": _relative_or_string(run_dir, self.writable_root),
            "read_only_roots": [
                _relative_or_string(run_dir, path) for path in self.read_only_roots
            ],
            "created_at": self.created_at,
            "copy_report": self.copy_report.to_json(),
            "git": self.git or {},
            "environment_mapping": self.environment_mapping,
            "patterns": self.patterns,
            "cleanup_hint": self.cleanup_hint,
            "warnings": list(self.warnings),
            "user_next_steps": list(self.user_next_steps),
        }


def workspace_source_binding(spec: WorkspaceSpec) -> dict:
    """Describe acceptable initialization inventories without materializing anything.

    Freeze this entire value at approval. After initialization compare the actual
    index hashes against source_snapshots[workspace.selected_mode]; a missing key
    is an unsupported binding, never permission to reinterpret auto as copy.
    This describes task isolation, not an OS sandbox or a race-free filesystem.
    """
    mode = _normalize_mode(spec.mode)
    if spec.data_inputs and mode in {"empty", "sparse_copy"}:
        raise WorkspaceModeError("Declared project data requires auto, copy or git_worktree mode.")
    binding: dict[str, Any] = {
        "schema_version": 1,
        "source_root": None,
        "requested_mode": mode,
        "input_kind": {"git_worktree": "git_head", "auto": "auto", "empty": "empty"}.get(mode, "filesystem"),
        "policy": {
            "owner": "workspace.copy",
            "max_file_bytes": spec.max_file_bytes,
            "include": list(normalize_copy_patterns(spec.include)),
            "exclude": list(normalize_copy_patterns(spec.exclude)),
            "data_inputs": list(spec.data_inputs),
            "index_boundary": "simple_ar.code_task.analysis.index",
        },
        "source_snapshots": {},
        "git": None,
        "blocked_modes": {},
        "omission_boundary": [
            "Only indexed initialization files are bound; this is not an inventory of every repository byte.",
            "Copy uses the shared full/sparse selection, size ceiling (0 disables it), and output-workspace exclusion.",
            "Index exclusions, including hidden directories, .env files, links/reparse points and Python environments, apply.",
            "Declared data bypasses incidental size ceilings and overlays filesystem bytes, including in Git mode.",
            "Recheck after initialization; source mutation and checkout behavior can invalidate this read-only observation.",
        ],
    }
    snapshots = binding["source_snapshots"]
    if mode == "empty":
        snapshots["empty"] = {}
        return binding
    if spec.code_root is None:
        raise WorkspaceModeError(f"{mode} workspace mode requires code_root")
    _validate_root_boundary(Path(spec.code_root))
    source = _validated_source_root(spec.code_root)
    binding["source_root"] = str(source)
    _validate_root_boundary(spec.task_dir / "workspace")
    workspace = (spec.task_dir / "workspace").resolve()
    if mode in {"copy", "sparse_copy", "auto"}:
        copy_mode = "sparse_copy" if mode == "sparse_copy" else "copy"
        selection = _select_workspace_files(
            source, workspace, max_file_bytes=spec.max_file_bytes,
            sparse=copy_mode == "sparse_copy", include_patterns=spec.include,
            exclude_patterns=spec.exclude,
            check_destination=False,
        )
        try:
            snapshots[copy_mode] = _selection_snapshot(selection, spec.data_inputs)
        except _SourceIndexBoundaryError as exc:
            binding["blocked_modes"][copy_mode] = str(exc)
    if mode in {"git_worktree", "auto"}:
        try:
            repo = _git_repo_root(source)
            commit = _git_head_commit(repo)
            relative = _relative_path_or_dot(source, repo).as_posix()
            dirty = bool(_git_output(repo, "status", "--short").strip())
            binding["git"] = {
                "repo_root": str(repo), "commit": commit,
                "project_relative_path": relative, "origin_dirty": dirty,
                "input_kind": "committed_head_with_declared_filesystem_data",
            }
            if mode == "auto" and dirty:
                raise WorkspaceModeError("Auto uses copy for uncommitted source changes.")
            snapshots["git_worktree"] = _git_source_snapshot(repo, commit, relative, source, workspace, spec)
        except (WorkspaceModeError, UnicodeError) as exc:
            binding["blocked_modes"]["git_worktree"] = str(exc)
    return binding


def _read_git_bytes(repo: Path, *args: str, missing_ok: bool = False) -> bytes:
    """Only used for immutable tree/blob reads and read-only Git configuration."""
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "safe.directory=*", "-C", str(repo), *args],
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceModeError(f"Cannot read Git snapshot: {exc}") from exc
    if result.returncode == 1 and missing_ok:
        return b""
    if result.returncode:
        raise WorkspaceModeError(f"Cannot read Git snapshot ({args[0]}): {result.stderr.decode('utf-8', errors='replace')}")
    return result.stdout


def _git_source_snapshot(
    repo: Path, commit: str, project: str, source: Path, workspace: Path, spec: WorkspaceSpec,
) -> dict[str, str]:
    """Project-scoped HEAD inventory, with the existing index's name boundaries.

    No checkout is simulated. Ambiguous checkout transformations are explicitly
    unsupported, leaving the existing initializer untouched.
    """
    settings = {
        key: _read_git_bytes(repo, "config", "--null", "--get", key, missing_ok=True)
        .rstrip(b"\0\n").decode("utf-8", errors="strict")
        for key in ("core.autocrlf", "core.eol")
    }
    if settings["core.autocrlf"].lower() not in {"", "false", "0", "off", "no"}:
        raise WorkspaceModeError("Git checkout newline conversion prevents proving blob/index equivalence (core.autocrlf).")
    if settings.get("core.eol", "").lower() == "crlf":
        raise WorkspaceModeError("Git core.eol=crlf prevents proving blob/index equivalence.")
    # Ancestor attributes can affect the requested monorepo project too.
    repository_tree = _read_git_bytes(repo, "ls-tree", "-r", "-z", "--name-only", commit)
    if any(Path(name.decode("utf-8")).name == ".gitattributes" for name in repository_tree.split(b"\0") if name):
        raise WorkspaceModeError("Committed .gitattributes requires checkout/filter verification by the caller.")
    treeish = commit if project == "." else f"{commit}:{project}"
    raw = _read_git_bytes(repo, "ls-tree", "-r", "-z", treeish)
    entries: dict[str, str] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        file_mode, kind, oid = metadata.decode("ascii").split()
        name = encoded_path.decode("utf-8", errors="strict")
        parts = Path(name).parts
        if (not parts or Path(name).is_absolute() or ".." in parts
                or "\\" in name or ":" in name):
            raise WorkspaceModeError("Git tree contains a path not safely representable as a workspace path.")
        if file_mode not in {"100644", "100755"} or kind != "blob":
            raise WorkspaceModeError("Git symlinks/submodules require checkout-specific index verification.")
        entries[name] = oid
    overlays = _select_data_inputs(source, workspace, spec.data_inputs)
    names = set(entries) | set(overlays)
    if os.name == "nt":
        spellings: dict[str, str] = {}
        for name in names:
            path = Path(name)
            for prefix in (path, *path.parents):
                spelling = prefix.as_posix()
                folded = spelling.casefold()
                if folded in spellings and spellings[folded] != spelling:
                    raise WorkspaceModeError("Git tree/data contains case-colliding paths on Windows.")
                spellings[folded] = spelling
    environments = {str(Path(name).parent).replace("\\", "/")
                    for name in names if Path(name).name == "pyvenv.cfg"}
    hashes: dict[str, str] = {}
    for name in sorted(names):
        path = Path(name)
        # These are precisely the name/marker rules in index._iter_files;
        # links are refused above rather than guessed from the dirty checkout.
        if (path.name in IGNORED_FILE_NAMES or path.name.startswith(".env")
                or any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in path.parts[:-1])
                or any(parent.as_posix() in environments for parent in path.parents if parent != Path("."))):
            continue
        full_name = name if project == "." else f"{project}/{name}"
        attributes = _read_git_bytes(repo, "check-attr", "--all", "-z", "--", full_name)
        if attributes:
            raise WorkspaceModeError("Active Git attributes require checkout/filter verification by the caller.")
        if name in overlays:
            _validate_source_file(source, overlays[name])
            hashes[name] = _sha256(overlays[name])
        else:
            hashes[name] = hashlib.sha256(_read_git_bytes(repo, "cat-file", "blob", entries[name])).hexdigest()
    return hashes


def create_workspace(spec: WorkspaceSpec) -> WorkspaceResult:
    """Create the editable workspace requested by ``spec``."""
    mode = _normalize_mode(spec.mode)
    if spec.data_inputs and mode in {"empty", "sparse_copy"}:
        raise WorkspaceModeError("Declared project data requires auto, copy or git_worktree mode.")
    builders = {"auto": _create_auto_workspace, "empty": _create_empty_workspace,
                "copy": _create_copy_workspace, "git_worktree": _create_git_worktree_workspace,
                "sparse_copy": _create_sparse_copy_workspace}
    result = builders[mode](spec)
    if not spec.data_inputs:
        return result
    report = copy_workspace_inputs(result.source_root, result.project_root, spec.data_inputs, result.copy_report)
    return replace(result, copy_report=report,
                   patterns={**result.patterns, "data_inputs": list(spec.data_inputs)})


def _create_auto_workspace(spec: WorkspaceSpec) -> WorkspaceResult:
    """Prefer git worktree for existing projects, then fall back to copy."""
    if spec.code_root is None:
        raise WorkspaceModeError("auto workspace mode requires code_root")
    try:
        return _create_git_worktree_workspace(spec, requested_mode="auto")
    except WorkspaceModeError as exc:
        result = _create_copy_workspace(
            spec,
            requested_mode="auto",
            fallback_reason=str(exc),
            warnings=(
                "Auto workspace mode fell back to copy because git worktree "
                "could not be prepared.",
            ),
            user_next_steps=_worktree_failure_next_steps(str(exc)),
        )
        return result


def suggested_python_executable(result: WorkspaceResult) -> str | None:
    """Return a mapped Python executable when workspace options request one."""
    value = result.environment_mapping.get("python_executable")
    return value if isinstance(value, str) and value else None


def _create_copy_workspace(
    spec: WorkspaceSpec,
    *,
    requested_mode: str = "copy",
    fallback_reason: str = "",
    warnings: tuple[str, ...] = (),
    user_next_steps: tuple[str, ...] = (),
) -> WorkspaceResult:
    if spec.code_root is None:
        raise WorkspaceModeError("copy workspace mode requires code_root")
    source = _validated_source_root(spec.code_root)
    workspace = (spec.task_dir / "workspace").resolve()
    copy_report = copy_code_workspace(
        source,
        workspace,
        max_file_bytes=spec.max_file_bytes,
    )
    return WorkspaceResult(
        mode="copy",
        requested_mode=requested_mode,
        selected_mode="copy",
        source_root=source,
        workspace_dir=workspace,
        project_root=workspace,
        writable_root=workspace,
        read_only_roots=(),
        created_at=_utcnow_iso(),
        copy_report=copy_report,
        git=None,
        environment_mapping=_environment_mapping(
            source_root=source,
            workspace_dir=workspace,
            reuse_source_venv=spec.reuse_source_venv,
            mode="copy",
        ),
        patterns={},
        cleanup_hint="Delete the run directory to remove this copied workspace.",
        fallback_reason=fallback_reason,
        warnings=warnings,
        user_next_steps=user_next_steps,
    )


def _create_sparse_copy_workspace(spec: WorkspaceSpec) -> WorkspaceResult:
    if spec.code_root is None:
        raise WorkspaceModeError("sparse_copy workspace mode requires code_root")
    source = _validated_source_root(spec.code_root)
    workspace = (spec.task_dir / "workspace").resolve()
    copy_report = sparse_copy_code_workspace(
        source,
        workspace,
        include_patterns=spec.include,
        exclude_patterns=spec.exclude,
        max_file_bytes=spec.max_file_bytes,
    )
    return WorkspaceResult(
        mode="sparse_copy",
        requested_mode="sparse_copy",
        selected_mode="sparse_copy",
        source_root=source,
        workspace_dir=workspace,
        project_root=workspace,
        writable_root=workspace,
        read_only_roots=(),
        created_at=_utcnow_iso(),
        copy_report=copy_report,
        git=None,
        environment_mapping=_environment_mapping(
            source_root=source,
            workspace_dir=workspace,
            reuse_source_venv=spec.reuse_source_venv,
            mode="sparse_copy",
        ),
        patterns={
            "include": list(normalize_copy_patterns(spec.include)),
            "exclude": list(normalize_copy_patterns(spec.exclude)),
            "default_includes_used": not bool(spec.include),
            "default_excludes_always_applied": True,
            "risk": (
                "sparse_copy may omit runtime dependencies. Prefer copy or "
                "git_worktree unless you know the needed project subset."
            ),
        },
        cleanup_hint="Delete the run directory to remove this sparse workspace.",
    )


def _create_git_worktree_workspace(
    spec: WorkspaceSpec,
    *,
    requested_mode: str = "git_worktree",
) -> WorkspaceResult:
    if spec.code_root is None:
        raise WorkspaceModeError("git_worktree workspace mode requires code_root")
    source = _validated_source_root(spec.code_root)
    workspace = (spec.task_dir / "workspace").resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(
            f"Workspace already contains files: {workspace}\n"
            "Next steps:\n"
            f"- Remove the existing run workspace: {workspace}\n"
            f"- If Git still tracks it as a stale worktree, run: git worktree remove {workspace}\n"
            "- Then run: git worktree prune"
        )

    repo_root = _git_repo_root(source)
    project_relative = _relative_path_or_dot(source, repo_root)
    project_root = workspace if project_relative == Path(".") else workspace / project_relative
    commit = _git_head_commit(repo_root)
    branch = _git_output(repo_root, "branch", "--show-current") or "detached"
    dirty_status = _git_output(repo_root, "status", "--short")
    if requested_mode == "auto" and dirty_status.strip():
        raise WorkspaceModeError("Source repository has uncommitted files; use copy to preserve the current source, or explicitly select git_worktree for committed HEAD.")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _run_git(repo_root, "worktree", "add", "--detach", str(workspace), commit)
    if not project_root.exists() or not project_root.is_dir():
        _remove_git_worktree_best_effort(repo_root, workspace)
        raise WorkspaceModeError(
            "git_worktree created the repository worktree, but the requested "
            f"project subdirectory was not found inside it.\n"
            f"code_root: {source}\n"
            f"repo_root: {repo_root}\n"
            f"expected project_root: {project_root}\n"
            "Next steps:\n"
            "- Check whether the code_root path is tracked in the committed baseline.\n"
            "- Commit the intended project directory before using git_worktree.\n"
            "- Or rerun with --workspace-mode copy to include the current filesystem state."
        )
    warnings = _worktree_warnings(
        source_root=source,
        repo_root=repo_root,
        dirty_status=dirty_status,
    )

    return WorkspaceResult(
        mode="git_worktree",
        requested_mode=requested_mode,
        selected_mode="git_worktree",
        source_root=source,
        workspace_dir=workspace,
        project_root=project_root.resolve(),
        writable_root=project_root.resolve(),
        read_only_roots=(source,),
        created_at=_utcnow_iso(),
        copy_report=empty_copy_report(),
        git={
            "repo_root": str(repo_root),
            "source_root": str(source),
            "project_root": str(project_root.resolve()),
            "project_relative_path": "." if project_relative == Path(".") else project_relative.as_posix(),
            "origin_branch": branch,
            "origin_commit": commit,
            "origin_dirty": bool(dirty_status.strip()),
            "origin_status_sample": dirty_status.splitlines()[:50],
            "worktree_path": str(workspace),
            "worktree_commit": commit,
            "mode": "detached",
        },
        environment_mapping=_environment_mapping(
            source_root=source,
            workspace_dir=project_root.resolve(),
            reuse_source_venv=spec.reuse_source_venv,
            mode="git_worktree",
        ),
        patterns={},
        cleanup_hint=(
            "Remove this workspace with `git worktree remove <workspace>` or "
            "delete the run directory and prune stale worktrees."
        ),
        warnings=warnings,
        user_next_steps=_worktree_success_next_steps(
            source_root=source,
            repo_root=repo_root,
            dirty_status=dirty_status,
        ),
    )


def _create_empty_workspace(spec: WorkspaceSpec) -> WorkspaceResult:
    workspace = (spec.task_dir / "workspace").resolve()
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"Workspace already contains files: {workspace}")
    workspace.mkdir(parents=True, exist_ok=True)
    return WorkspaceResult(
        mode="empty",
        requested_mode="empty",
        selected_mode="empty",
        source_root=workspace,
        workspace_dir=workspace,
        project_root=workspace,
        writable_root=workspace,
        read_only_roots=(),
        created_at=_utcnow_iso(),
        copy_report=empty_copy_report(),
        git=None,
        environment_mapping={
            "schema_version": 1,
            "mode": "empty",
            "dependency_files": [],
            "reuse_source_venv": False,
            "source_venv_detected": "",
            "python_executable": "",
            "notes": [
                "Empty workspace prepared for a greenfield code-task run.",
                "Generated code must stay inside this workspace.",
            ],
        },
        patterns={},
        cleanup_hint="Delete the run directory to remove this generated workspace.",
    )


def _validated_source_root(code_root: Path) -> Path:
    _validate_root_boundary(Path(code_root))
    source = Path(code_root).resolve()
    if not source.exists():
        raise FileNotFoundError(f"Code root does not exist: {source}")
    if not source.is_dir():
        raise NotADirectoryError(f"Code root is not a directory: {source}")
    return source


def _normalize_mode(value: str) -> str:
    mode = str(value or "auto").strip().lower().replace("-", "_")
    if mode not in SUPPORTED_WORKSPACE_MODES:
        raise WorkspaceModeError(
            "workspace mode must be one of: "
            + ", ".join(sorted(SUPPORTED_WORKSPACE_MODES))
        )
    return mode


def _relative_path_or_dot(path: Path, parent: Path) -> Path:
    try:
        relative = path.resolve().relative_to(parent.resolve())
    except ValueError as exc:
        raise WorkspaceModeError(
            f"Expected code_root to be inside detected git repository.\n"
            f"code_root: {path}\n"
            f"detected repo root: {parent}"
        ) from exc
    return relative if str(relative) else Path(".")


def _project_relative_path(workspace_dir: Path, project_root: Path) -> str:
    try:
        relative = project_root.resolve().relative_to(workspace_dir.resolve())
    except ValueError:
        return str(project_root)
    return "." if str(relative) == "." else relative.as_posix()


def _git_repo_root(path: Path) -> Path:
    try:
        output = _git_output(path, "rev-parse", "--show-toplevel")
    except WorkspaceModeError as exc:
        if "git executable" in str(exc) or "timed out" in str(exc):
            raise
        raise WorkspaceModeError(
            "git_worktree mode requires code_root to be inside a local git "
            f"repository, but Git could not find a usable repository at: {path}\n"
            "Next steps:\n"
            "- If this project does not need git isolation, rerun with --workspace-mode copy.\n"
            "- If you want git_worktree, run these commands in the baseline project root: "
            "`git init`, `git add .`, `git commit -m \"initial baseline\"`.\n"
            "- If the project lives in a larger repository, pass that project subdirectory as code_root; "
            "SimpleAutoResearch will create a repository worktree and use the matching subdirectory as the editable project root."
        ) from exc
    if not output:
        raise WorkspaceModeError(f"Path is not inside a git repository: {path}")
    return Path(output).resolve()


def _git_head_commit(repo_root: Path) -> str:
    try:
        return _git_output(repo_root, "rev-parse", "HEAD")
    except WorkspaceModeError as exc:
        raise WorkspaceModeError(
            "git_worktree mode requires the baseline repository to have at "
            f"least one commit: {repo_root}\n"
            "Next steps:\n"
            "- Commit the baseline first: `git add .` then `git commit -m \"initial baseline\"`.\n"
            "- Or rerun with --workspace-mode copy if you do not want to use git yet."
        ) from exc


def _worktree_warnings(
    *,
    source_root: Path,
    repo_root: Path,
    dirty_status: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if not _same_path(source_root, repo_root):
        warnings.append(
            "code_root is inside a larger git repository; SimpleAutoResearch "
            "created a repository-level worktree and will use the matching "
            "subdirectory as the project root."
        )
    if dirty_status.strip():
        warnings.append(
            "The source repository has uncommitted changes. git_worktree uses "
            "the committed HEAD snapshot; commit/stash changes first if the "
            "task needs them."
        )
    if (repo_root / ".gitmodules").exists():
        warnings.append(
            "This repository declares submodules. If files are missing in the "
            "workspace, run `git submodule update --init --recursive` in the source repo first."
        )
    if (repo_root / ".gitattributes").exists():
        warnings.append(
            "This repository may use Git LFS or attribute filters. If large "
            "files are placeholders, run `git lfs pull` in the source repo first."
        )
    return tuple(warnings)


def _worktree_success_next_steps(
    *,
    source_root: Path,
    repo_root: Path,
    dirty_status: str,
) -> tuple[str, ...]:
    steps: list[str] = []
    if not _same_path(source_root, repo_root):
        steps.append(
            "The editable project root is the matching subdirectory inside "
            "code_task/workspace, not the repository worktree root."
        )
    if dirty_status.strip():
        steps.append(
            "Uncommitted source changes were not included. Commit or stash them "
            "and rerun if they are part of the baseline."
        )
    steps.append(
        "Cleanup: delete the run directory; if Git reports a stale worktree, run `git worktree prune`."
    )
    return tuple(steps)


def _worktree_failure_next_steps(reason: str) -> tuple[str, ...]:
    text = reason.lower()
    steps = [
        "Auto mode used a guarded copy fallback for this run.",
        "To force git worktree later, set workspace.mode = \"git_worktree\" or pass --workspace-mode git_worktree.",
    ]
    if "not find a usable repository" in text or "inside a local git repository" in text or "not inside a git repository" in text:
        steps.append(
            "Initialize a baseline repository first: `git init`, `git add .`, `git commit -m \"initial baseline\"`."
        )
    if "at least one commit" in text:
        steps.append(
            "Create a baseline commit first: `git add .` then `git commit -m \"initial baseline\"`."
        )
    if "git executable" in text:
        steps.append("Install Git or make sure `git` is available on PATH.")
    if "workspace already contains files" in text or "stale worktree" in text:
        steps.append("Remove the old workspace and run `git worktree prune`.")
    return tuple(steps)


def _git_output(cwd: Path, *args: str) -> str:
    completed = _run_git(cwd, "--no-optional-locks", "-c", "core.fsmonitor=false", *args)
    return completed.stdout.strip()


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-c", "safe.directory=*", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceModeError("git executable was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceModeError(f"git command timed out: {' '.join(args)}") from exc
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise WorkspaceModeError(
            f"git command failed ({' '.join(args)}): {message}"
        )
    return completed


def _remove_git_worktree_best_effort(repo_root: Path, workspace: Path) -> None:
    """Try to remove a partially-created worktree without masking the main error."""
    try:
        subprocess.run(
            [
                "git",
                "-c",
                "safe.directory=*",
                "-C",
                str(repo_root),
                "worktree",
                "remove",
                "--force",
                str(workspace),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return


def _environment_mapping(
    *,
    source_root: Path,
    workspace_dir: Path,
    reuse_source_venv: bool,
    mode: str,
) -> dict[str, Any]:
    source_venv_python = _source_venv_python(source_root)
    selected_python = str(source_venv_python) if reuse_source_venv and source_venv_python else ""
    dependency_files = _dependency_files(workspace_dir)
    if not dependency_files and not workspace_dir.exists():
        dependency_files = _dependency_files(source_root)
    notes = [
        "Workspace creation does not install dependencies.",
        "Use env_mode=external or workspace.reuse_source_venv for existing project environments.",
    ]
    return {
        "schema_version": 1,
        "mode": mode,
        "dependency_files": dependency_files,
        "reuse_source_venv": reuse_source_venv,
        "source_venv_detected": str(source_venv_python) if source_venv_python else "",
        "python_executable": selected_python,
        "notes": notes,
    }


def _source_venv_python(source_root: Path) -> Path | None:
    candidates = (
        source_root / ".venv" / "Scripts" / "python.exe",
        source_root / ".venv" / "bin" / "python",
        source_root / "venv" / "Scripts" / "python.exe",
        source_root / "venv" / "bin" / "python",
    )
    for path in candidates:
        if path.exists() and path.is_file():
            return Path(os.path.abspath(path))
    return None


def _dependency_files(root: Path) -> list[str]:
    if not root.exists() or not root.is_dir():
        return []
    return [name for name in DEPENDENCY_FILE_NAMES if (root / name).is_file()]


def _relative_or_string(root: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
