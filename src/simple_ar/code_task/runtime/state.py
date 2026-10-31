from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, write_json


@dataclass(frozen=True)
class CodeTaskPaths:
    """Common paths for a code-task run.

    Args:
        run_dir: Root run directory.
        task_dir: Directory containing code-task artifacts.
        workspace_dir: Editable project root. For git worktree runs this may
            be a subdirectory inside the repository worktree.
        meta_dir: Metadata directory.
        run_artifact_dir: Latest benchmark execution directory.
        repairs_dir: Directory containing repair attempts.
        manifest_path: Root code-task manifest.
    """

    run_dir: Path
    task_dir: Path
    workspace_dir: Path
    meta_dir: Path
    run_artifact_dir: Path
    repairs_dir: Path
    manifest_path: Path


def code_task_paths(run_dir: Path) -> CodeTaskPaths:
    """Return the standard path layout for a code-task run."""
    root = Path(run_dir)
    task_dir = root / "code_task"
    workspace_dir = _manifest_project_root(root, task_dir)
    return CodeTaskPaths(
        run_dir=root,
        task_dir=task_dir,
        workspace_dir=workspace_dir,
        meta_dir=task_dir / "meta",
        run_artifact_dir=task_dir / "run",
        repairs_dir=task_dir / "repairs",
        manifest_path=root / "manifest.json",
    )


def _manifest_project_root(run_dir: Path, task_dir: Path) -> Path:
    """Return project_root from manifest when available."""
    manifest_path = run_dir / "manifest.json"
    default = task_dir / "workspace"
    if not manifest_path.exists():
        return default
    try:
        data = read_json(manifest_path)
    except Exception:
        return default
    if not isinstance(data, dict):
        return default
    workspace = data.get("workspace", {})
    if not isinstance(workspace, dict):
        return default
    layout = data.get('layout', {})
    layout = layout if isinstance(layout, dict) else {}
    value = (workspace.get("project_root") or workspace.get("workspace_dir")
             or layout.get('project_root') or layout.get('workspace'))
    if not isinstance(value, str) or not value.strip():
        return default
    path = Path(value)
    return path if path.is_absolute() else run_dir / path


def code_task_workspace_boundary(run_dir: Path) -> Path:
    """Read the original workspace grant, not the possibly nested project root.

    Legacy layout.workspace is the old workspace grant; without either field
    only the standard workspace is authorized. A project root or source path
    alone never expands that grant.
    This read never repairs a manifest or changes historical receipts.
    """
    run_dir = Path(run_dir)
    standard = run_dir / 'code_task' / 'workspace'
    manifest_path = run_dir / 'manifest.json'
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    if not isinstance(manifest, dict):
        raise ValueError('Saved project manifest must be an object')
    workspace = manifest.get('workspace', {})
    if not isinstance(workspace, dict):
        raise ValueError('Saved project workspace boundary is invalid')
    layout = manifest.get('layout', {})
    layout = layout if isinstance(layout, dict) else {}
    project = workspace.get('project_root')
    if project is None and not workspace.get('workspace_dir'):
        project = layout.get('project_root')
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise ValueError('Saved project root is invalid')
    value = workspace.get('workspace_dir', layout.get('workspace'))
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValueError('Saved project workspace boundary is invalid')
    boundary = Path(value) if value else standard
    if value and not boundary.is_absolute():
        boundary = run_dir / boundary
    try:
        project_root = Path(project or value) if project or value else standard
        if (project or value) and not project_root.is_absolute():
            project_root = run_dir / project_root
        project_root.absolute().relative_to(boundary.absolute())
        project_root.resolve().relative_to(boundary.resolve())
    except (OSError, ValueError, RuntimeError):
        raise ValueError('Saved project workspace boundary is unavailable; restore it before reading.') from None
    return boundary


def review_workspace_boundary(project: Path, *, boundary: Path | None = None) -> Path:
    """Resolve a review's existing grant before any new source observation."""
    from simple_ar.code_task.analysis.index import code_task_access_filter

    if boundary is None:
        raise ValueError('Review needs an explicit authorized workspace boundary')
    if not code_task_access_filter(project, boundary=boundary)(project):
        raise ValueError('Review project is private or outside its authorized workspace boundary')
    return Path(boundary)


def load_code_task_manifest(run_dir: Path) -> dict[str, Any]:
    """Load and validate a code-task manifest."""
    paths = code_task_paths(run_dir)
    if not paths.manifest_path.exists():
        raise FileNotFoundError(f"Missing code-task manifest: {paths.manifest_path}")
    data = read_json(paths.manifest_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {paths.manifest_path}")
    if data.get("workflow") != "code_task":
        raise RuntimeError(f"Run is not a code-task workflow: {paths.run_dir}")
    return data


def save_code_task_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Write a code-task manifest back to disk."""
    write_json(code_task_paths(run_dir).manifest_path, manifest)


def implementation_file_hashes(run_dir: Path) -> dict[str, str]:
    """Use the CodeTask source inventory to pin a validated candidate."""
    from simple_ar.code_task.analysis.index import build_codebase_index
    index = build_codebase_index(code_task_paths(run_dir).workspace_dir,
        access_boundary=code_task_workspace_boundary(run_dir))
    hashes = {row['path']: row['sha256'] for row in index['files']}
    manifest = load_code_task_manifest(run_dir)
    names = manifest.get('code_task', {}).get('native_binding', {}).get('binding', {}).get('contract', {}).get('new_files', [])
    # Explicit creation obligations remain bound even when the browsing index
    # omits their directory (for example a hidden application configuration).
    import hashlib
    invalid = required_new_file_issues(manifest, code_task_paths(run_dir).workspace_dir)
    for name in names:
        path = code_task_paths(run_dir).workspace_dir / name
        if name not in invalid:
            with path.open('rb') as handle:
                hashes[name] = hashlib.file_digest(handle, 'sha256').hexdigest()
    return hashes


def required_new_file_issues(manifest: dict[str, Any], workspace: Path) -> list[str]:
    """Inspect frozen creation obligations, never create initializer placeholders."""
    from simple_ar.code_task.analysis.index import code_task_access_filter
    allowed = code_task_access_filter(workspace, boundary=workspace)
    names = manifest.get('code_task', {}).get('native_binding', {}).get('binding', {}).get('contract', {}).get('new_files', [])
    issues = []
    for name in names:
        path = workspace / name
        components = []
        current = path
        while current != workspace and current.is_relative_to(workspace):
            components.append(current)
            current = current.parent
        if (not allowed(path) or not path.resolve().is_relative_to(workspace.resolve())
                or any(part.is_symlink() or part.is_junction() for part in components)
                or not path.is_file() or path.stat().st_size == 0):
            issues.append(name)
    return issues


def implementation_output_inventory(expected: dict[str, str], outputs: dict[str, str]) -> dict[str, str]:
    """Pin the validated inventory plus explicitly observed derived files.

    An output never replaces a validated input. New source/dependency files
    need another implementation/validator, even when produced by a process.
    No filename or extension by itself establishes output provenance.
    """
    from simple_ar.code_task.analysis.index import SOURCE_SUFFIXES
    from simple_ar.code_task.execution.environment import DEPENDENCY_FILE_NAMES
    dependencies = {name.casefold() for name in DEPENDENCY_FILE_NAMES}
    if (set(outputs) & set(expected) or any(
            Path(name).suffix.lower() in SOURCE_SUFFIXES or Path(name).name.casefold() in dependencies
            for name in outputs)):
        raise ValueError('New source or dependency files require independent validation; outputs cannot replace validated inputs')
    return {**expected, **outputs}


def retained_implementation_inventory(run_dir: Path, expected: dict[str, str],
                                      outputs: dict[str, str] | None = None) -> dict[str, str]:
    current = implementation_file_hashes(run_dir)
    if current != implementation_output_inventory(expected, outputs or {}):
        raise ValueError('Retained candidate inventory changed without retained execution output provenance')
    return current


def native_workspace_binding(manifest: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Select approved initialization facts; old native approvals keep their shape."""
    bound = manifest['code_task']['native_binding']['binding']
    source = bound.get('source', {})
    if 'workspace_input' not in source:
        return {'file_hashes': source.get('file_hashes', {}), 'environment': bound['environment']}
    mode = manifest['workspace']['selected_mode']
    original = source['workspace_input']['source_snapshots']
    prior = bound.get('prior_source', {})
    hashes = prior['file_hashes'] if 'file_hashes' in prior else original.get(mode)
    process = bound['process_environments'].get(mode)
    if hashes is None or process is None or Path(process['cwd']).resolve() != workspace.resolve():
        raise ValueError('Prepared workspace does not match any explicitly approved source/environment alternative')
    return {'file_hashes': hashes, 'environment': process['environment']}


def native_preparation_processes(run_dir: Path, *, ledger: Any,
                                 session_id: str, attempt_id: str) -> set[str]:
    """Read proof-backed preparation identities without replaying or settling work."""
    manifest = load_code_task_manifest(run_dir)
    bound = (manifest.get('code_task', {}).get('native_binding') or {}).get('binding', {})
    if 'environment_preparations' not in bound or bound.get('environment_origin'):
        return set()  # Historical approvals and inherited environments do not prepare here.
    from simple_ar.core.capabilities import ArtifactStore
    from simple_ar.research.project_environment import verify_environment_preparation
    view = bound['environment_preparations'][manifest['workspace']['selected_mode']]
    root = Path(view['store_root'])
    store = ArtifactStore(root)
    if (not root.is_absolute() or store.root.name != attempt_id or store.root.parent.name != 'attempts'
            or store.root.resolve() != store.root
            or Path(view['cwd']).resolve() != code_task_paths(run_dir).workspace_dir.resolve()):
        raise ValueError('Preparation store or selected workspace differs from its frozen attempt owner')
    if not session_id:
        session_id = store.read_json('environment_setup.json')['steps'][0]['process_record']['session_id']
    identities, _ = verify_environment_preparation(store, view, ledger=ledger,
        session_id=session_id, attempt_id=attempt_id)
    # Excluding by physical ID must not hide a second reservation using that ID.
    if any(sum(row.logical_call_id == identity for row in ledger.entries) != 1 for identity in identities):
        raise ValueError('Preparation physical identity has duplicate or missing ledger ownership')
    if any(path.parent.name not in identities
            for path in (store.root / 'environment_processes').glob('*/invocation.json')):
        raise ValueError('Preparation contains a physical invocation omitted from its verified proof')
    return identities


def manifest_section(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    """Return a mutable manifest section, creating it when necessary."""
    value = manifest.get(key)
    return value if isinstance(value, dict) else {}


def read_required_json(path: Path) -> dict[str, Any]:
    """Read an object-valued CodeTask artifact; never replace missing evidence."""
    if not path.exists():
        raise FileNotFoundError(f"Missing required artifact: {path}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def workspace_file(workspace_dir: Path, relative_path: str) -> Path | None:
    """Resolve a workspace-relative path without allowing traversal."""
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    workspace = Path(workspace_dir).resolve()
    path = (workspace / rel).resolve()
    if not is_relative_to(path, workspace):
        return None
    return path


def is_relative_to(path: Path, parent: Path) -> bool:
    """Return true when ``path`` is inside ``parent``."""
    return path.is_relative_to(parent)


def utcnow_iso() -> str:
    """Return a compact UTC timestamp for artifacts."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
