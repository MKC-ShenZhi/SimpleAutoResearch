"""Read and select retained deliveries without inheriting their task authority.

CLI adapters translate the returned value into input options; UI admission is an
explicit callback, independent of argparse and of execution-owner assembly.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from simple_ar.core.console import print_line
from simple_ar.core.artifacts import read_json
from simple_ar.tools.workspace import RetainedDelivery, WorkspaceToolGateway


@dataclass(frozen=True)
class ReuseAccess:
    authorize: Callable[[Path], Path] | None = None
    preflight: Callable[[list[Path]], None] | None = None


@dataclass(frozen=True)
class ReuseSelection:
    root: Path
    names: tuple[str, ...]
    groups: dict[str, list[Path]]
    project: tuple[Path, dict | None, dict] | None
    package_mounts: tuple[Path, ...]
    version: int | None = None

    @property
    def materials(self) -> list[Path]:
        if self.version is not None:
            return []  # The creation owner snapshots the selected retained layout.
        return list(dict.fromkeys(path for name in self.names if name != 'code_project'
                                  for path in self.groups[name]))

    @property
    def prior_drafts(self) -> list[str]:
        if self.version is not None:
            return []
        return [str(self.groups['report'][0])] if 'report' in self.names else []

    @property
    def origin(self) -> dict:
        return {'session': str(self.root), 'results': list(self.names),
            **({'version': self.version} if self.version is not None else {})}


def _native_project(reader: RetainedDelivery) -> tuple[Path, dict | None, dict] | None:
    config = reader.state['config']
    if (config.get('control_protocol') is not None and config.get('code_task') is None
            and config.get('source_generation_protocol') is None):
        # Only existing receipt ownership is needed. Do not load old extensions,
        # interpreters, professional controllers or model connections.
        from simple_ar.core.budget import BudgetLedger

        root = reader.root
        for name in ('workspace', 'evidence', 'attempts'):
            reader.admit(root / name)
        processes = root / 'tools' / 'processes'
        if (root / 'tools').exists():
            reader.admit(root / 'tools')
            if processes.exists():
                reader.admit(processes)
        reader.admit(root / 'budget_ledger.json')
        proof = WorkspaceToolGateway(root,
            {name: path for name, path in reader.mounts.items() if name != 'workspace'},
            BudgetLedger.load(root / 'budget_ledger.json'), create_workspace=False)
        project = reader.reusable_code_project(check_producers=lambda locators, **options:
            proof.check_artifact_producers(locators, retained_reader=reader, **options))
        return (project, None, {}) if project is not None else None
    if reader.authorize is not None:
        raise ValueError('Historical executable project contracts require their original CLI; '
                         'reuse portable artifacts or a delivered ordinary project in this interface')
    # Historical exact/source-generation contracts retain their old reader.
    from simple_ar.app.tool_application import ToolApplication
    return ToolApplication.load(reader.root).reusable_code_project()


def select_reuse(root: Path, names: list[str] | tuple[str, ...], *,
                 access: ReuseAccess | None = None, version: int | None = None) -> ReuseSelection:
    """Select current byte-proved groups; never carry execution authority forward.

    Admission callbacks apply before reading proof/material bytes. A directory
    used to inspect process receipts must already belong to the caller's grants;
    this function adds no grants, and no task is loaded for ordinary projects.
    """
    access = access or ReuseAccess()
    if access.authorize is not None:
        access.authorize(root)
        access.authorize(root / 'session_manifest.json')
    root = root.resolve()
    selected = tuple(dict.fromkeys(names))
    if not selected:
        raise ValueError('Select registered result names with --reuse; inspect with results SESSION.')
    if version is not None:
        from simple_ar.tools.delivery_versions import DeliveryVersions
        owner = DeliveryVersions(root, authorize=access.authorize)
        groups = owner.material_paths(version)
        if any(name not in groups for name in selected):
            raise ValueError('Selected retained version has no requested portable result group')
        if access.preflight is not None:
            access.preflight([path for name in selected for path in groups[name]])
        return ReuseSelection(root, selected, groups, None, (), version)
    manifest = read_json(root / 'session_manifest.json')
    project = None
    if manifest.get('transition_recipe') == 'native-tools-v1':
        reader = RetainedDelivery(root, authorize=access.authorize)
        groups = reader.reusable_materials()
        if 'code_project' in selected:
            project = _native_project(reader)
            if project is not None:
                groups['code_project'] = [project[0]]
    else:
        groups = session_materials(root, authorize=access.authorize,
                                   include_project='code_project' in selected)
        if 'code_project' in selected:
            from simple_ar.core.capabilities import CapabilityRegistry
            from simple_ar.core.session import SessionController
            project = session_project(SessionController.load(root, registry=CapabilityRegistry()))
    for name in selected:
        if name not in groups:
            raise ValueError(f'No current reusable {name} delivery in {root}.')
        if access.preflight is not None:
            access.preflight(groups[name])
    # The unscoped CLI retains its established complete-package directory
    # behavior. Scoped adapters mount selected files only; a proof-read grant is
    # never promoted to a material-directory grant in the new task.
    mounts = tuple(dict.fromkeys(path.parent for name in selected if name != 'code_project'
        for path in groups[name])) if access.authorize is None else ()
    return ReuseSelection(root, selected, groups, project, mounts)


def session_project(session, *, authorize=None) -> tuple[Path, dict | None, dict] | None:
    """Resolve an owner-proved project and its optional exact contract."""
    from simple_ar.app.tool_application import ToolApplication
    if session.manifest.transition_recipe == ToolApplication.RECIPE:
        return _native_project(RetainedDelivery(session.store.root, authorize=authorize))
    from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
    ref = session.manifest.state_refs.get("implementation")
    if ref is None or ref.status != "available":
        return None
    result = session.store.read_json(ref)
    if result.get("status") != "validated" or not result.get("workspace_dir") or not result.get("code_task_run_dir"):
        return None
    workspace, run = Path(result["workspace_dir"]).resolve(), Path(result["code_task_run_dir"]).resolve()
    if (not workspace.is_relative_to(session.store.root) or not run.is_relative_to(session.store.root)
            or not workspace.is_dir() or code_task_paths(run).workspace_dir.resolve() != workspace):
        # A retired or relocated workspace is not an editable offering. Its
        # absence must not block independently registered portable deliveries.
        return None
    manifest = load_code_task_manifest(run)
    if not manifest.get("benchmark", {}).get("command") or not manifest.get("edit_scope", {}).get("allowed_patterns"):
        return None
    policy = {}
    for prepared in session.manifest.state_refs.values():
        if prepared.kind != "prepared_execution" or prepared.status != "available":
            continue
        execution = session.store.read_json(prepared).get("execution", {})
        if execution.get("cwd") and Path(execution["cwd"]).resolve() == workspace:
            task = execution.get("code_task", {})
            policy = {key: task[key] for key in ("budget_profile", "allow_large_edits", "edit_budget_overrides") if key in task}
            break
    return workspace, manifest, policy


def session_materials(root: Path, *, authorize=None, include_project: bool = True,
                      version: int | None = None) -> dict[str, list[Path]]:
    """Resolve current registered deliveries, not arbitrary historical files."""
    if authorize is not None:
        authorize(root)
    root = root.resolve()
    if version is not None:
        from simple_ar.tools.delivery_versions import DeliveryVersions
        return DeliveryVersions(root, authorize=authorize).material_paths(version)
    if authorize is not None:
        authorize(root)
        authorize(root / 'session_manifest.json')
        if read_json(root / 'session_manifest.json').get('transition_recipe') != 'native-tools-v1':
            raise ValueError('Granted retained-material reading requires a native-tool session; '
                             'use the historical CLI reader for this task')
        reader = RetainedDelivery(root, authorize=authorize)
        available = reader.reusable_materials()
        available.pop('code_project', None)
        if include_project:
            try:
                project = _native_project(reader)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print_line(f'code_project unavailable: {exc}')
                project = None
            if project is not None:
                available['code_project'] = [project[0]]
        return available
    from simple_ar.core.capabilities import CapabilityRegistry
    from simple_ar.core.session import SessionController
    session = SessionController.load(root, registry=CapabilityRegistry())
    from simple_ar.app.tool_application import ToolApplication
    if session.manifest.transition_recipe == ToolApplication.RECIPE:
        # A previous delivery remains on disk during feedback. It is not the
        # current completion while the revised task is active or paused.
        if session.manifest.status not in {"completed", "partial"}:
            return {}
        available = RetainedDelivery(root).reusable_materials()
        available.pop('code_project', None)  # Complete project proof is separate from portable file groups.
        if not include_project:
            return available
        try:
            project = session_project(session, authorize=authorize)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Portable files retain their own proof even when the complete
            # executable project is ambiguous, unavailable or changed.
            print_line(f'code_project unavailable: {exc}')
            project = None
        if project is not None:
            available['code_project'] = [project[0]]
        return available
    available: dict[str, list[Path]] = {}
    project = session_project(session, authorize=authorize)
    if project is not None:
        available["code_project"] = [project[0]]
    for name in ("data_analysis", "report", "summary"):
        ref = session.manifest.state_refs.get(name)
        if ref is None or ref.status != "available":
            continue
        path = session.store.resolve(ref).resolve()
        if not path.is_relative_to(session.store.root) or not path.is_file():
            raise ValueError(f"Registered {name} result is missing or outside its session.")
        if name == "report":
            body = path.parent / "report_body.md"
            if not body.is_file():
                continue
            # Keep the draft and its evidence together; the draft alone is not
            # independently verified literature or a measured result.
            paths = [body]
            paths.extend(p for p in (path.parent / "experiment_evidence.json",
                                    path.parent / "citation_map.json") if p.is_file())
            documents = session.manifest.state_refs.get("documents")
            # Reuse the evidence actually consumed by this draft, including
            # later reading/linked documents, not the initial ingest alone.
            writer = session.manifest.state_refs.get("writer")
            if writer is not None and writer.status == "available":
                payload = session.store.read_json(writer)
                snapshot = payload.get("input_snapshot", {}).get("path")
                if snapshot:
                    snapshot_ref = session.store.ref(Path(writer.path).parent / snapshot)
                    sources = session.store.read_json(snapshot_ref).get("sources", [])
                    bundles = [row for row in sources if row.get("schema") == "document_bundle.v1"]
                    if len(bundles) > 1:
                        raise ValueError("Registered report has ambiguous source bundles.")
                    if bundles:
                        from simple_ar.core.capabilities import ArtifactRef
                        documents = ArtifactRef.from_dict(bundles[0])
            if documents is not None and documents.status == "available":
                retained = session.store.resolve(documents).resolve()
                if not retained.is_relative_to(session.store.root) or not retained.is_file():
                    raise ValueError("Registered report sources are missing or outside their session.")
                paths.append(retained)
        else:
            paths = [path]
        available[name] = paths
    implementation = session.manifest.state_refs.get("implementation")
    if implementation is not None and implementation.status == "available":
        payload = session.store.read_json(implementation)
        delivery = payload.get("artifact_refs", {}).get("code_analysis")
        if payload.get("status") == "validated" and isinstance(delivery, dict):
            path = (session.store.resolve(implementation).parent / delivery["path"]).resolve()
            if not path.is_relative_to(session.store.root) or not path.is_file():
                raise ValueError("Registered code analysis package is missing or outside its session.")
            available["code_analysis"] = [path]
            return available
        if payload.get("status") == "validated" and payload.get("workspace_dir"):
            workspace = Path(payload["workspace_dir"]).resolve()
            files = [workspace / "outputs/report.md", workspace / "outputs/results.json"]
            if workspace.is_relative_to(session.store.root) and all(path.is_file() for path in files):
                available["code_analysis"] = files
    return available


