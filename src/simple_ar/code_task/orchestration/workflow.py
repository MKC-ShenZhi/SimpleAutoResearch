from __future__ import annotations

from dataclasses import dataclass
from glob import escape
from pathlib import Path
import stat
from typing import Any

from simple_ar.core.artifacts import write_json, write_text
from simple_ar.code_task.generation.task_contract import build_greenfield_task_contract, save_task_contract
from simple_ar.code_task.execution.comparison import normalize_metric_directions
from simple_ar.code_task.editing.scope import default_edit_scope, is_edit_allowed_path
from simple_ar.code_task.execution.environment import build_code_task_environment_policy
from simple_ar.code_task.analysis.index import build_codebase_index
from simple_ar.code_task.analysis.repo_map import build_repo_map
from simple_ar.code_task.workspace.copy import CopyReport
from simple_ar.code_task.workspace.modes import (
    WorkspaceResult,
    WorkspaceSpec,
    create_workspace,
    suggested_python_executable,
)
from simple_ar.code_task.runtime.state import utcnow_iso as _utcnow_iso


INITIAL_ADAPTER_SOURCE = (
    "# Confirmed reproduction adapter: implement without changing author methods/data.\n"
    'raise NotImplementedError("Reproduction adapter requires implementation")\n'
)


@dataclass(frozen=True)
class CodeTaskInitResult:
    """Result returned after initializing a code-task workspace.

    Args:
        run_dir: Root run directory for this code task.
        task_dir: Directory containing all code-task artifacts.
        workspace_dir: Editable project root for the code task. In worktree
            mode this may be a subdirectory inside the repository worktree.
        meta_dir: Metadata directory for indexes and manifests.
        manifest_path: Root workflow manifest path.
        codebase_index_path: Path to the generated codebase index.
        repo_map_path: Path to the generated layered repository map.
        repo_map_summary_path: Path to the generated repository map summary.
        copy_report: Summary of copied and skipped source paths.
        codebase_index: Generated index data.
        repo_map: Generated layered repository map.
        environment_policy: Initial execution environment policy.
        workspace: Workspace creation metadata.
        kind: Code-task mode, usually ``existing_project`` or ``greenfield``.
    """

    kind: str
    run_dir: Path
    task_dir: Path
    workspace_dir: Path
    meta_dir: Path
    manifest_path: Path
    codebase_index_path: Path
    repo_map_path: Path
    repo_map_summary_path: Path
    copy_report: CopyReport
    codebase_index: dict[str, Any]
    repo_map: dict[str, Any]
    environment_policy: dict[str, Any]
    workspace: WorkspaceResult


def initialize_code_task(
    *,
    run_dir: Path,
    code_root: Path | None,
    task_file: Path,
    kind: str = "existing_project",
    benchmark_command: str | None = None,
    max_file_bytes: int = 2_000_000,
    workspace_mode: str = "copy",
    workspace_include: tuple[str, ...] = (),
    workspace_exclude: tuple[str, ...] = (),
    workspace_reuse_source_venv: bool = False,
    env_mode: str = "current",
    python_executable: str | Path | None = None,
    primary_metric: str | None = None,
    metric_directions: dict[str, str] | None = None,
    edit_scope_mode: str | None = None,
    edit_scope_allowed_patterns: tuple[str, ...] = (),
    edit_scope_protected_patterns: tuple[str, ...] = (),
    respect_allowed_paths: bool = False,
    data_inputs: tuple[str, ...] = (),
    initial_files: tuple[str, ...] = (),
) -> CodeTaskInitResult:
    """Initialize a local code-task run without modifying the source project.

    Args:
        run_dir: New run directory. It may exist, but must be empty.
        code_root: Existing codebase or benchmark directory to copy. Greenfield
            runs may leave this empty when ``workspace_mode`` is ``empty``.
        task_file: Markdown or text file describing the requested change.
        benchmark_command: Optional validation command to record for later
            stages. It is not executed during init.
        max_file_bytes: Maximum file size copied in ``copy`` and
            ``sparse_copy`` modes. Use ``0`` to disable the size guard.
        workspace_mode: Workspace strategy. ``auto`` prefers a detached git
            worktree for Git projects and falls back to copy; ``copy``
            preserves the V2.1 behavior; ``git_worktree`` forces a detached
            worktree; ``sparse_copy`` is experimental.
        workspace_include: POSIX glob patterns copied by sparse mode.
        workspace_exclude: Additional POSIX glob patterns skipped by sparse
            mode.
        workspace_reuse_source_venv: Whether a detected source ``.venv`` may
            be used as the initial external Python interpreter.
        env_mode: Execution environment mode. V2.1 supports ``current`` and
            ``external``.
        python_executable: External interpreter path or executable name when
            ``env_mode`` is ``external``.
        primary_metric: Optional primary metric used for conservative
            baseline-vs-patched verdicts.
        metric_directions: Optional mapping from metric name to direction.
            Supported normalized directions are ``higher_is_better``,
            ``lower_is_better``, ``resource``, and ``ignore``.
        edit_scope_mode: Optional edit-scope mode label stored in the manifest.
        edit_scope_allowed_patterns: Optional allowlist of workspace-relative
            glob patterns. Empty means any non-protected workspace path.
        edit_scope_protected_patterns: Additional workspace-relative glob
            patterns that are read-only for patch proposals and applications.
        respect_allowed_paths: Use the caller's explicit scope for test and
            benchmark files; protected validation inputs remain read-only.
        data_inputs: Explicit project-relative data files/directories. Independent
            copies retain their runtime paths and become protected edit inputs;
            incidental file-size limits do not apply to this selection.
        initial_files: Explicit new Python adapters, never inferred from edit scope.
            Fail-closed placeholders are created only in the isolated workspace.

    Returns:
        Paths and metadata for the initialized code-task run.

    Raises:
        FileNotFoundError: If ``code_root`` or ``task_file`` is missing.
        NotADirectoryError: If ``code_root`` is not a directory.
        FileExistsError: If ``run_dir`` already contains files.
    """
    normalized_kind = _normalize_kind(kind)
    source_root = Path(code_root).resolve() if code_root is not None else None
    task_source = Path(task_file).resolve()
    root = Path(run_dir)
    if initial_files and source_root is not None and root.resolve().is_relative_to(source_root):
        raise ValueError("Adapter preparation artifacts must be outside the author project")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"Run directory already contains files: {root}")
    if not task_source.exists():
        raise FileNotFoundError(f"Task file does not exist: {task_source}")
    if not task_source.is_file():
        raise FileNotFoundError(f"Task file is not a regular file: {task_source}")
    primary_metric_value = (primary_metric or "").strip()
    normalized_metric_directions = normalize_metric_directions(metric_directions)

    task_dir = root / "code_task"
    meta_dir = task_dir / "meta"
    task_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    task_text = task_source.read_text(encoding="utf-8", errors="replace")
    write_text(task_dir / "task.md", task_text)
    workspace = create_workspace(
        WorkspaceSpec(
            code_root=source_root,
            task_dir=task_dir,
            mode=workspace_mode,
            max_file_bytes=max_file_bytes,
            include=workspace_include,
            exclude=workspace_exclude,
            reuse_source_venv=workspace_reuse_source_venv,
            data_inputs=data_inputs,
        )
    )
    workspace_dir = workspace.project_root
    copy_report = workspace.copy_report
    edit_scope = default_edit_scope(
        mode=edit_scope_mode or ('authorized_scope.v1' if respect_allowed_paths else None),
        allowed_patterns=edit_scope_allowed_patterns,
        protected_patterns=(*edit_scope_protected_patterns,
                            *(pattern for name in data_inputs
                              for pattern in (escape(name), escape(name.rstrip("/")) + "/**"))),
        respect_allowed_paths=respect_allowed_paths,
    )
    allowed_patterns = tuple(str(item) for item in edit_scope["allowed_patterns"])
    protected_patterns = tuple(str(item) for item in edit_scope["protected_patterns"])
    if not isinstance(initial_files, (list, tuple)):
        raise ValueError("initial_files must be an explicit list of new Python paths")
    if initial_files:
        if (normalized_kind != "existing_project" or source_root is None
                or workspace.selected_mode not in {"copy", "git_worktree"}
                or workspace_dir.resolve() == source_root):
            raise ValueError("Adapter creation requires an isolated existing project")
        targets = []
        seen = set()
        for name in initial_files:
            if (not isinstance(name, str) or not name or name != name.strip() or "\\" in name
                    or any(c in name for c in ":*?[]") or Path(name).is_absolute()
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or Path(name).suffix != ".py" or name.casefold() in seen
                    or name not in edit_scope_allowed_patterns
                    or not is_edit_allowed_path(name, allowed_patterns=allowed_patterns,
                        protected_patterns=(*protected_patterns, "data/**", "**/data/**", ".git/**"))):
                raise ValueError("initial_files requires distinct exact allowed, unprotected new Python paths")
            seen.add(name.casefold())
            for base in (source_root, workspace_dir.resolve()):
                target = base / name
                for part in (target, *target.parents):
                    if part == base:
                        break
                    reparse = part.exists() and (getattr(part.lstat(), "st_file_attributes", 0)
                        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                    if part.is_symlink() or reparse:
                        raise ValueError("Adapter creation cannot traverse links")
                if not target.resolve().is_relative_to(base) or target.exists():
                    raise ValueError("Adapter must not replace an author or workspace path")
            targets.append(workspace_dir / name)
        # Validate the whole declaration before creating any file; no author writes.
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as handle:
                handle.write(INITIAL_ADAPTER_SOURCE)
    codebase_index_path = meta_dir / "codebase_index.json"
    codebase_index = build_codebase_index(workspace_dir, output_path=codebase_index_path)
    repo_map_path = meta_dir / "repo_map.json"
    repo_map_summary_path = meta_dir / "repo_map_summary.md"
    repo_map = build_repo_map(
        codebase_index,
        output_path=repo_map_path,
        summary_path=repo_map_summary_path,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
    )
    resolved_env_mode = env_mode
    resolved_python = python_executable
    mapped_python = suggested_python_executable(workspace)
    if mapped_python and resolved_python is None and resolved_env_mode in {"current", "external"}:
        resolved_env_mode = "external"
        resolved_python = mapped_python
    environment_policy = build_code_task_environment_policy(
        env_mode=resolved_env_mode,
        python_executable=resolved_python,
    )
    manifest_path = root / "manifest.json"
    write_json(
        manifest_path,
        _manifest(
            run_dir=root,
            code_root=source_root,
            task_file=task_source,
            benchmark_command=benchmark_command,
            primary_metric=primary_metric_value,
            metric_directions=normalized_metric_directions,
            max_file_bytes=max_file_bytes,
            copy_report=copy_report,
            workspace=workspace,
            codebase_index=codebase_index,
            repo_map=repo_map,
            edit_scope=edit_scope,
            environment_policy=environment_policy,
            kind=normalized_kind,
        ),
    )
    if normalized_kind != "greenfield":
        result_schema = _init_result_schema(primary_metric_value, normalized_metric_directions)
        save_task_contract(
            meta_dir,
            build_greenfield_task_contract(
                task_text,
                benchmark_command=benchmark_command or "",
                max_files=0,
                max_generated_lines=0,
                result_schema=result_schema,
                task_kind=normalized_kind,
                source={
                    "kind": "code_task_init",
                    "task_file": str(task_source),
                    "origin": "initialize_code_task",
                },
            ),
        )

    return CodeTaskInitResult(
        kind=normalized_kind,
        run_dir=root,
        task_dir=task_dir,
        workspace_dir=workspace_dir,
        meta_dir=meta_dir,
        manifest_path=manifest_path,
        codebase_index_path=codebase_index_path,
        repo_map_path=repo_map_path,
        repo_map_summary_path=repo_map_summary_path,
        copy_report=copy_report,
        codebase_index=codebase_index,
        repo_map=repo_map,
        environment_policy=environment_policy,
        workspace=workspace,
    )


def _manifest(
    *,
    run_dir: Path,
    code_root: Path | None,
    task_file: Path,
    benchmark_command: str | None,
    primary_metric: str | None,
    metric_directions: dict[str, str] | None,
    max_file_bytes: int,
    copy_report: CopyReport,
    workspace: WorkspaceResult,
    codebase_index: dict[str, Any],
    repo_map: dict[str, Any],
    edit_scope: dict[str, Any],
    environment_policy: dict[str, Any],
    kind: str,
) -> dict[str, Any]:
    project = codebase_index.get("project", {})
    repo_project = repo_map.get("project", {})
    primary = (primary_metric or "").strip()
    directions = dict(metric_directions or {})
    return {
        "schema_version": 1,
        "workflow": "code_task",
        "status": "initialized",
        "code_task": {
            "kind": kind,
        },
        "created_at": _utcnow_iso(),
        "run_dir": str(run_dir),
        "source": {
            "code_root": str(code_root) if code_root is not None else "",
            "task_file": str(task_file),
        },
        "layout": {
            "task": "code_task/task.md",
            "workspace": "code_task/workspace",
            "project_root": workspace.to_manifest(run_dir=run_dir).get("project_root", "code_task/workspace"),
            "meta": "code_task/meta",
            "codebase_index": "code_task/meta/codebase_index.json",
            "repo_map": "code_task/meta/repo_map.json",
            "repo_map_summary": "code_task/meta/repo_map_summary.md",
        },
        "copy": {
            **copy_report.to_json(),
            "max_file_bytes": max_file_bytes,
        },
        "workspace": {
            **workspace.to_manifest(run_dir=run_dir),
            # Freeze only initial syntax failures, reusing existing index
            # checksums. Later index refreshes must not redefine this baseline.
            "initial_syntax_errors": {
                row["path"]: row["sha256"] for row in codebase_index.get("files", [])
                if row.get("kind") == "python" and row.get("python", {}).get("syntax_ok") is False
            },
        },
        "codebase": {
            "file_count": project.get("file_count", 0),
            "python_file_count": project.get("python_file_count", 0),
            "test_file_count": project.get("test_file_count", 0),
            "entrypoint_candidates": project.get("entrypoint_candidates", []),
            "repo_map": {
                "schema_version": repo_map.get("schema_version"),
                "path": "code_task/meta/repo_map.json",
                "summary": "code_task/meta/repo_map_summary.md",
                "directory_count": repo_project.get("directory_count", 0),
                "symbol_count": repo_project.get("symbol_count", 0),
                "benchmark_file_count": repo_project.get("benchmark_file_count", 0),
                "config_file_count": repo_project.get("config_file_count", 0),
            },
        },
        "environment": {
            "status": "not_probed",
            "policy": environment_policy,
        },
        "edit_scope": edit_scope,
        "benchmark": {
            "command": benchmark_command,
            "executed": False,
            "primary_metric": primary,
            "metric_directions": directions,
        },
    }


def _normalize_kind(value: str) -> str:
    kind = str(value or "existing_project").strip().lower().replace("-", "_")
    if kind in {"existing", "existing_code", "project", "patch"}:
        return "existing_project"
    if kind in {"greenfield", "from_scratch", "new_project"}:
        return "greenfield"
    if kind == "existing_project":
        return kind
    raise ValueError("code-task kind must be existing_project or greenfield")


def _init_result_schema(primary_metric: str, metric_directions: dict[str, str]) -> dict[str, Any]:
    primary = (primary_metric or "").strip() or "score"
    required = [primary]
    required.extend(str(name) for name in metric_directions if str(name).strip() and str(name) not in required)
    return {
        "schema_version": "code_task_result_schema.v1",
        "primary_metric": primary,
        "required_metrics": required,
        "metric_directions": dict(metric_directions),
    }
