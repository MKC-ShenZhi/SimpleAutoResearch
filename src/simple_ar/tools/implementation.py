"""Thin native consumer of the existing code-generation owner and file windows."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.code_task.generation.common import safe_relative_path
from simple_ar.code_task.generation.writer import write_implementation_unit
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.integrations.llm import LLMClient
from simple_ar.tools.specs import CommonToolSpec, ToolResult
from simple_ar.tools.workspace import FileWindow
from simple_ar.code_task.generation.selection import select_implementation_files, bind_implementation_originals


class ImplementationFile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(description='Relative path in a new candidate project, not an existing source to overwrite.')
    purpose: str = Field(min_length=1, max_length=4000)
    source_path: str = Field(default='', description='Optional authorized existing text file to modify into this candidate. Original remains unchanged; return exact replacements, not a rewritten whole file.')
    source_sha256: str = Field(default='', description='Observed full original digest required with source_path; changes since investigation are rejected.')


class ImplementationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    goal: str = Field(min_length=1, max_length=16000)
    project_path: str = Field(default='', description='Authorized existing project root. With files omitted, the implementation owner selects related existing/new paths and binds originals automatically.')
    files: list[ImplementationFile] = Field(default_factory=list, max_length=12,
        description='Optional explicit candidate files for greenfield or already investigated work. Omit to let the implementation owner select files from the goal and optional existing project.')
    source_windows: list[FileWindow] = Field(default_factory=list, max_length=12,
        description='Exact mounted original code, extracted paper text, data descriptions or diagnostics to use. Same bounded reads and coverage as read_file. No guessed evidence text.')


def implementation_tool_specs() -> list[CommonToolSpec]:
    return [CommonToolSpec(name='prepare_implementation', domain='implementation',
        permission_level='write_patch', risk_level='medium', input_schema=ImplementationRequest.model_json_schema(),
        description='Prepare a coherent code candidate from a goal without coordinator-selected files or supplied code. With no files or project_path, the implementation owner selects necessary new files. An optional existing project_path adds project inspection/indexed bounded source context to the same shared-budget selection request, then the existing writer generates related files. Or supply explicit files/source_windows for greenfield or investigated work. New files use full content; existing files use exact replacements against hash-bound complete originals. A complete response-contract rejection may receive one correction using the same remaining allowance; rejected proposals are retained. Transport, unknown-use and syntax failures are not replayed here. Saves selection, coverage omissions, source bindings and unresolved gaps in this attempt. Repeating an identity returns an unchanged completed result; interrupted/failed model requests are not blindly replayed. Historical execution observations are evidence, not unresolved-failure declarations. No installs, execution, copying or transfer. Original sources remain unchanged; candidates require separate review and verification.')]


class ImplementationToolGateway:
    def __init__(self, root: Path, workspace: Any, ledger: Any, model: str, session_id: str,
                 *, client_factory: Callable[[str], LLMClient] | None = None,
                 execution_observations: Callable[[], list[dict[str, Any]]] | None = None) -> None:
        self.root, self.workspace, self.ledger = root, workspace, ledger
        self.model, self.session_id = model, session_id
        self.client_factory = client_factory
        self.execution_observations = execution_observations

    def _client(self, identity: str) -> LLMClient:
        if self.client_factory:
            return self.client_factory(identity)
        return LLMClient.for_task(client=LLMClient.from_env(self.model, budget_ledger=self.ledger,
            budget_session_id=self.session_id, budget_attempt_id=identity), purpose='code')

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if name != 'prepare_implementation' or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
                raise ValueError('Invalid implementation invocation')
            request = ImplementationRequest.model_validate(arguments)
            prefix = f'workspace/implementations/{identity}'
            output = self.workspace.resolve(prefix, write=True)
            owner = ArtifactStore(self.root / 'attempts' / identity / 'implementation')
            project = self.workspace.resolve(request.project_path) if request.project_path else None
            if project is not None and (not project.is_dir() or output.is_relative_to(project)
                    or project.is_relative_to(output)):
                raise ValueError('project_path must be a directory separate from the candidate output')
            if output.exists() and not owner.exists('result.json'):
                raise ValueError('Candidate output already exists without a completed receipt; inspect it and use a new identity')
            # Selection and generation use the same execution basis. A later
            # probe during recovery must not change an already selected unit.
            selection_input = owner.read_json('selection-input.json') if owner.exists('selection-input.json') else None
            if owner.exists('request.json'):
                saved_binding = owner.read_json('request.json')
                observations = saved_binding.get('execution_observations', [])
            elif selection_input is not None and 'execution_observations' in selection_input:
                observations = selection_input['execution_observations']
            else:
                observations = self.execution_observations() if self.execution_observations else []
            selection_observations = (selection_input.get('execution_observations')
                if selection_input is not None else observations)
            client, selection = None, None
            windows = []
            for i, source in enumerate(request.source_windows):
                observed = self.workspace.call('read_file', source.model_dump(exclude_unset=True), identity=f'{identity}-source-{i}')
                if observed.status != 'ok':
                    raise ValueError(f'Original window unavailable: {observed.error}')
                windows.append({'request': source.model_dump(), **observed.data,
                    'role': 'supplied_source_or_diagnostic_not_certified_fact'})
            if not request.files:
                selection, client = select_implementation_files(goal=request.goal,
                    request_binding=request.model_dump(), project=project, owner=owner, windows=windows,
                    bind_inventory=lambda: self.workspace.bind_inventory(project, identity=identity),
                    resolve_source=self.workspace.resolve,
                    validate_destination=lambda path: self.workspace.resolve(f'{prefix}/{path}', write=True),
                    client_factory=lambda: self._client(identity),
                    execution_observations=selection_observations)
                request.files = [ImplementationFile.model_validate(row) for row in selection['files']]
                windows.append({'role': 'owner_project_context_not_execution_authority',
                    'project_path': str(project) if project is not None else None,
                    'declared_project': selection['declared_project'],
                    'coverage': selection['coverage'],
                    'snapshots': [row for row in selection['source_context']
                        if row['path'] not in {item.path for item in request.files if item.source_path}]})
            else:
                # Selection already validates its destinations and source locators.
                # Explicit files need the same checks before reads or dispatch.
                paths = set()
                for row in request.files:
                    path = safe_relative_path(row.path)
                    if not path or path in paths:
                        raise ValueError('Use distinct safe relative output paths')
                    paths.add(path)
                    row.path = path
                    self.workspace.resolve(f'{prefix}/{path}', write=True)
                    if row.source_path:
                        self.workspace.resolve(row.source_path)
            originals = bind_implementation_originals([row.model_dump() for row in request.files],
                resolve_source=self.workspace.resolve)
            request_data = request.model_dump()
            if not request.project_path:
                # Preserve the pre-project-selection durable explicit contract.
                request_data.pop('project_path')
            request_data['files'] = [row.model_dump(exclude={'source_path', 'source_sha256'}
                if not row.source_path else set()) for row in request.files]
            binding = {'request': request_data, 'source_windows': windows,
                **({'original_files': originals} if originals else {}),
                **({'selection': selection} if selection else {})}
            if owner.exists('request.json'):
                # Automatic observations belong to the original invocation,
                # not the changing history at recovery time.
                if {key: value for key, value in saved_binding.items()
                        if key != 'execution_observations'} != binding:
                    raise ValueError('Original inputs changed; preserve this attempt and select a new candidate')
                if not owner.exists('result.json'):
                    raise ValueError('Interrupted implementation: inspect saved proposal and budget; no blind replay')
                saved = owner.read_json('result.json')
                hashes = {f"{prefix}/{row['path']}": row['sha256'] for row in saved['data']['files']}
                if self.workspace.artifact_hashes(hashes) != hashes:
                    raise ValueError('Prepared candidate changed; inspect the current files, not the historical generation result')
                current = self.workspace.call('list_directory', {'path': prefix,
                    'recursive': True, 'limit': 200}, identity=identity)
                if (current.status != 'ok' or current.data['has_more'] or current.data['inventory_truncated']
                        or {row['path'] for row in current.data['entries']} != set(hashes)):
                    raise ValueError('Prepared candidate inventory changed; inspect the current files')
                return ToolResult(tool_name=name, **saved)
            if observations:
                binding['execution_observations'] = observations
            owner.write_json('request.json', binding)
            client = client or self._client(identity)
            result = write_implementation_unit(project_dir=output, goal=request.goal,
                files=[row.model_dump() for row in request.files], source_windows=windows,
                client=client, artifact_dir=owner.root, original_files=originals,
                task_python=str(self.workspace.python), execution_observations=observations)
            if selection and project is not None and self.workspace.bind_inventory(project, identity=identity) != {
                    key: selection['input'][key] for key in ('project_path', 'file_hashes', 'inventory_scope')}:
                raise ValueError('Project changed during generation; candidate retained for inspection, use a new identity')
            data = {'data': {**result, 'source_binding': str(owner.root / 'request.json'),
                    **({'selection_receipt': str(owner.root / 'selection.json'),
                        'coverage': selection['coverage']} if selection else {})},
                'artifacts': [f"{prefix}/{row['path']}" for row in result['files']],
                'summary': 'Candidate saved. Original sources unchanged; execution and scientific correctness remain unverified.'}
            owner.write_json('result.json', data)
            return ToolResult(tool_name=name, **data)
        except Exception as exc:
            return ToolResult(tool_name=name, status='error',
                error=f'Implementation unavailable ({type(exc).__name__}: {exc}); retained inputs/proposal/budget are not replayed automatically.')
