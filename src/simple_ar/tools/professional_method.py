"""Professional intent on the existing native engine, never an authoring runtime.

Selection is an ordinary immutable native receipt. Workspace owns all reads,
copies, SHA-bound edits and approved processes; inspection/delivery own checks.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.tools.specs import CommonToolSpec, ToolResult


class WorkMethod(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    method: Literal['source', 'document', 'direct']
    goal: str = Field(min_length=1, max_length=16000)
    material_roots: list[str] = Field(default_factory=list, max_length=8)
    project_path: str = Field(default='', description='Actual authorized project directory. '
        'Read-only source stays read-only; use copy_project for an editable working copy.')


def professional_method_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name='select_work_method', domain='professional_method',
        permission_level='plan', risk_level='low', input_schema=WorkMethod.model_json_schema(),
        description='Select source implementation/analysis/figures, document drafting/revision, or direct work '
            'in this same native engine. Supply the current goal and authorized material/project roots; '
            'omitted materials use original task mounts. The next native decision uses the existing code '
            'or writing route and related original feedback. No subagent, generation, execution, new '
            'permission or compulsory workflow. Use existing workspace operations and approved run_process; '
            'source and prose are actual working files. Select again when the goal/project/method changes.')]


_METHODS = {
    'source': ('code', 'Implement and diagnose the current source goal directly with workspace operations. '
        'Investigate actual originals; create or modify an authorized working project, propose an exact '
        'run_process with actual inputs/outputs, and use its retained diagnostics for revision. Preserve '
        'source mounts and valid work. Do not delegate decisions to a source generator or require prepared '
        'candidate admission. A successful run is not scientific verification.'),
    'document': ('writing', 'Draft or revise the current document directly with workspace reads/writes and '
        'SHA-bound edits. Use original material and actual result/check receipts; preserve earlier versions '
        'as needed. Maintain one actual editable manuscript. Do not delegate to work_document or demand '
        'a report-generation pipeline. Independent checks/delivery and requested publication remain separate.'),
    'direct': ('task', 'Own the current goal using the available permissioned operations. Select a '
        'professional method only when useful; selection and notes never certify completion.'),
}


class ProfessionalMethodTools:
    def __init__(self, *, workspace: Any, observations: Callable[[], list[dict]],
                 user_requests: Callable[[], list[str]], work_context: Callable[[list[str]], dict] | None):
        self.workspace, self.observations = workspace, observations
        self.user_requests, self.work_context = user_requests, work_context

    def has_started(self, identity: str) -> bool:
        return False  # Pure root validation; there is no private mutation or request to replay.

    def can_reconcile(self, name: str, identity: str) -> bool:
        return name == 'select_work_method'

    def call(self, name: str, arguments: dict, *, identity: str) -> ToolResult:
        try:
            if name != 'select_work_method':
                raise ValueError('Unknown professional method operation')
            request = WorkMethod.model_validate(arguments)
            roots = request.material_roots or [str(path) for alias, path in self.workspace.mounts.items()
                if alias not in {'workspace', 'records'}]
            roots = list(dict.fromkeys(str(self.workspace.resolve_material_root(path)) for path in roots))
            if any(not Path(path).exists() for path in roots):
                raise ValueError('Professional material root is unavailable')
            project = self.workspace.resolve_material_root(request.project_path) if request.project_path else None
            if project is not None and not project.is_dir():
                raise ValueError('Project must be an actual authorized directory')
            return ToolResult(tool_name=name, data={'method': request.method, 'goal': request.goal,
                'material_roots': roots, 'project_path': str(project) if project else '',
                'request_index': len(self.user_requests()) - 1,
                'scope': 'Working intent and authorized locations only. No source was inspected, '
                    'no files were generated or adopted, and no access/execution permission changed.'})
        except (ValueError, OSError, KeyError) as exc:
            return ToolResult(tool_name=name, status='error', error=str(exc))

    def selected(self) -> dict | None:
        # observations() is the application's accepted common journal port,
        # not a second work-state store. A rejected selection never supersedes it.
        return next((row for row in reversed(self.observations())
            if row.get('tool_name') == 'select_work_method' and row.get('status') == 'ok'), None)

    def purpose(self) -> str:
        selected = self.selected()
        return (_METHODS[selected['data']['method']][0] if selected and
            selected['data']['request_index'] == len(self.user_requests()) - 1 else 'task')

    def instructions(self) -> str:
        selected = self.selected()
        if selected is None or selected['data']['request_index'] != len(self.user_requests()) - 1:
            return (' Select work methods when useful using select_work_method; perform source/prose '
                'work directly in this native engine. No nested prepare_implementation or work_document.')
        return ' Current professional method: ' + _METHODS[selected['data']['method']][1]

    def context(self) -> dict | None:
        selected = self.selected()
        if selected is None:
            return None
        data = deepcopy(selected['data'])
        roots = [*data['material_roots'], *([data['project_path']] if data['project_path'] else [])]
        current = len(self.user_requests()) - 1
        data.update(record=selected['record'], current_request_index=current,
            status='current' if data['request_index'] == current else 'historical',
            project_available=False, project_writable=False)
        available, unavailable = [], []
        for root in dict.fromkeys(roots):
            try:
                path = self.workspace.resolve_material_root(root)
                if not path.exists():
                    raise ValueError('Recorded location is no longer available')
                available.append(str(path))
                if root == data['project_path']:
                    data['project_available'] = path.is_dir()
                    try:
                        self.workspace.resolve(str(path), write=True)
                        data['project_writable'] = True
                    except ValueError:
                        pass
            except (ValueError, OSError, KeyError) as exc:
                unavailable.append({'recorded_path': root, 'diagnostic': str(exc), 'authority': 'none'})
        data['unavailable_locations'] = unavailable
        try:
            data['original_feedback'] = self.work_context(available) if self.work_context and available else {}
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            data['original_feedback'] = {'available': False, 'diagnostic': str(exc)}
        return data
