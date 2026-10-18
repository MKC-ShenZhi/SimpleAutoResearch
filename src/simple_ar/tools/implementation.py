"""Thin native consumer of the existing code-generation owner and file windows."""
from __future__ import annotations

from pathlib import Path
import hashlib
import json
import re
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator

from simple_ar.code_task.editing.budget import VALID_BUDGET_PROFILES, validated_edit_budget_overrides
from simple_ar.code_task.generation.common import safe_relative_path
from simple_ar.code_task.generation.writer import write_implementation_unit
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.integrations.llm import LLMClient
from simple_ar.tools.specs import CommonToolSpec, ToolResult
from simple_ar.tools.workspace import FileWindow
from simple_ar.code_task.generation.selection import select_implementation_files, bind_implementation_originals
from simple_ar.tools.implementation_run import ExecuteImplementationRequest


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
    material_roots: list[str] = Field(default_factory=list, max_length=8,
        exclude_if=lambda value: not value,
        description='Optional authorized material directories or exact files, separate from the editable project. The owner investigates actual bounded windows before choosing code files; no coordinator-selected file list or hashes needed. Does not authorize execution or scientific verification.')


class ExactCodePreparation(BaseModel):
    """Frozen initializer settings; not selectable by a model tool call."""
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['existing_project', 'greenfield'] = 'existing_project'
    workspace_mode: Literal['auto', 'copy', 'sparse_copy', 'git_worktree', 'empty'] = 'auto'
    max_file_bytes: int = Field(default=2_000_000, ge=0)
    workspace_include: list[str] = Field(default_factory=list)
    workspace_exclude: list[str] = Field(default_factory=list)
    workspace_reuse_source_venv: bool = False
    env_mode: Literal['current', 'external'] = 'current'
    python_executable: str | None = None
    max_repairs: int = Field(default=0, ge=0)
    data_inputs: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def initializer_contract(self) -> 'ExactCodePreparation':
        if self.kind == 'greenfield' and self.workspace_mode not in {'empty', 'copy', 'sparse_copy'}:
            raise ValueError('Greenfield exact creation requires empty, copy or sparse_copy mode')
        if self.kind != 'greenfield' and self.workspace_mode == 'empty':
            raise ValueError('Empty workspace is only for greenfield creation')
        if self.env_mode == 'external' and not (self.python_executable or '').strip():
            raise ValueError('External environment requires its explicit Python executable')
        if self.python_executable is not None and not self.python_executable.strip():
            raise ValueError('Omit an unspecified Python executable instead of supplying an empty path')
        if self.data_inputs and self.workspace_mode in {'empty', 'sparse_copy'}:
            raise ValueError('Declared project data requires auto, copy or git_worktree mode')
        for name in self.data_inputs:
            if (not name or name != name.strip() or '\\' in name or Path(name).is_absolute()
                    or ':' in name or any(part in {'', '.', '..'} for part in name.split('/'))):
                raise ValueError('Data inputs must be explicit project-relative paths')
        return self


class ExactCodeTask(BaseModel):
    """Creation-time authority, never model-selected execution arguments."""
    model_config = ConfigDict(extra='forbid', strict=True)
    project_path: str = ''
    goal: str = Field(min_length=1)
    allowed_patterns: list[str] = Field(min_length=1)
    protected_patterns: list[str] = Field(default_factory=list)
    validation_command: list[str] = Field(min_length=1)
    timeout_sec: int = Field(default=300, gt=0)
    new_files: list[str] = Field(default_factory=list, exclude_if=lambda value: not value,
        description='Explicit required new project-relative files. They must be absent from the original source, within edit scope, and created as real nonempty files by the code owner, not initializer placeholders.')
    environment: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Explicit current/venv preparation, owned by this implementation attempt before code work. Same shared preparation plan, physical ledger and no-replay policy as scientific measurement.')
    edit_policy: dict[str, Any] | None = None
    preparation: ExactCodePreparation | None = None

    @field_validator('edit_policy', mode='before')
    @classmethod
    def valid_edit_policy(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) - {
                'budget_profile', 'allow_large_edits', 'edit_budget_overrides'}:
            raise ValueError('Edit policy must contain only supported edit settings')
        policy = dict(value)
        profile = policy.get('budget_profile')
        if profile is not None and (not isinstance(profile, str) or profile not in VALID_BUDGET_PROFILES):
            raise ValueError('Unknown edit budget profile')
        if 'allow_large_edits' in policy and type(policy['allow_large_edits']) is not bool:
            raise ValueError('allow_large_edits must be a boolean')
        if 'edit_budget_overrides' in policy:
            policy['edit_budget_overrides'] = validated_edit_budget_overrides(policy['edit_budget_overrides'])
        return policy

    @model_serializer(mode='wrap')
    def serialize_contract(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        if self.edit_policy is None:
            data.pop('edit_policy', None)
        if self.preparation is None:
            data.pop('preparation', None)
        return data

    @model_validator(mode='after')
    def exact_paths(self) -> 'ExactCodeTask':
        if self.environment is not None:
            from simple_ar.research.project_environment import environment_profile
            self.environment = environment_profile(self.environment)
            if self.preparation is None or self.preparation.workspace_reuse_source_venv:
                raise ValueError('Managed preparation requires an explicit isolated workspace and cannot combine source-venv reuse')
        greenfield = self.preparation is not None and self.preparation.kind == 'greenfield'
        empty = greenfield and self.preparation.workspace_mode == 'empty'
        if empty and self.project_path.strip():
            raise ValueError('Empty greenfield creation cannot use a source project; select copy or sparse_copy for a scaffold')
        if not empty and not self.project_path.strip():
            raise ValueError('Existing tasks and scaffold creation require a mounted source project')
        for pattern in (*self.allowed_patterns, *self.protected_patterns):
            if (not pattern.strip() or Path(pattern).is_absolute() or ':' in pattern
                    or '..' in pattern.replace('\\', '/').split('/')):
                raise ValueError("Editable/protected paths must be nonempty project-relative patterns without '..'")
        if any(not item.strip() for item in self.validation_command):
            raise ValueError('Validator argv must contain nonempty arguments')
        from simple_ar.code_task.editing.scope import is_edit_allowed_path
        seen = set()
        for name in self.new_files:
            if (safe_relative_path(name) != name or any(c in name for c in ':*?[]\\')
                    or name.casefold() in seen or not is_edit_allowed_path(name,
                        allowed_patterns=self.allowed_patterns,
                        protected_patterns=[*self.protected_patterns, 'data/**', '**/data/**', '.git/**'])):
                raise ValueError('Required new files must be distinct literal allowed, unprotected project paths')
            seen.add(name.casefold())
        return self


class ExactCodeWork(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    instruction: str = Field(default='', max_length=16000,
        description='Requested work or user feedback within the frozen goal/scope. Displayed for explicit approval; cannot change scope, validator or resources.')
    prior_result: str = Field(default='', max_length=300,
        description='Optional records/<attempt>/implementation.json from a previous validated result artifact in this task. Continue its actual candidate rather than losing earlier changes. Not an arbitrary source directory.')
    prepared_result: str = Field(default='', max_length=300, exclude_if=lambda value: not value,
        description='Optional registered records/<attempt>/execution.json from prepare_code_task. Copy its unchanged prepared source and reuse its actual environment; not a validated implementation or arbitrary directory.')

    @model_validator(mode='after')
    def one_source(self) -> 'ExactCodeWork':
        if self.prior_result and self.prepared_result:
            raise ValueError('Choose a validated prior implementation or a prepared source, not both')
        return self


def implementation_tool_specs(*, exact: bool = False) -> list[CommonToolSpec]:
    if exact:
        return [CommonToolSpec(name='prepare_code_task', domain='implementation',
            permission_level='execution', risk_level='high',
            input_schema={'type': 'object', 'properties': {}, 'additionalProperties': False},
            description='Prepare the frozen isolated source/environment without generating code or running the validator or formal experiment. Returns a registered execution.json source for an unchanged baseline and later implement_code_task(prepared_result=...). Preparation is not validated code and cannot satisfy code delivery. Uses the original process budget and approved install/check steps once; interrupted setup is not replayed.'),
            CommonToolSpec(name='implement_code_task', domain='implementation',
            permission_level='execution', risk_level='high',
            input_schema=ExactCodeWork.model_json_schema(),
            description='Implement the frozen scoped code task using its approved isolated-workspace preparation, CodeTask investigation, edit/protected-file gates, independent review and declared validator. Uses this task budget and attempt. Optional instruction/prior_result support feedback on the existing candidate; no arguments change scope, preparation or validator. Requires explicit approval of the displayed work and current source/environment identity. Only the saved technical-repair ceiling is authorized (one for legacy contracts); no scientific measurement. Installation is allowed only by an explicitly frozen environment preparation contract; it shares this attempt and process budget. Inspect the retained result before delivery. Interrupted work without a complete domain result is never replayed automatically.')]
    return [CommonToolSpec(name='prepare_implementation', domain='implementation',
        permission_level='write_patch', risk_level='medium', input_schema=ImplementationRequest.model_json_schema(),
        description='Prepare coherent code from a goal and optional read-only material_roots, without coordinator-selected files or code. The owner investigates actual windows, selects files and generates a candidate; project_path optionally supplies editable originals. When the goal needs a run, the same generation can propose one execution_plan; execute_implementation uses its registered result after explicit approval, without hand-copied commands/digests. Existing files require exact replacements. One complete response-contract correction shares the original budget; transport, unknown-use and syntax failures are not replayed. Original sources stay unchanged. Candidate preparation is not execution or scientific/visual verification.'),
        CommonToolSpec(name='execute_implementation', domain='implementation',
            permission_level='execution', risk_level='high', input_schema=ExecuteImplementationRequest.model_json_schema(),
            description='Execute one registered prepare_implementation execution_plan through the existing approved process owner, interpreter and shared physical ledger. Takes its prepared_result, not hand-written commands or hashes. Pins all generated files, consumed input identities, exact command/environment and expected fresh outputs. Saves real errors for later professional repair, without retries, installation or a second workflow. Source/output changes invalidate success; interrupted unbound outputs are not adopted. Optional delivery finishes after this run succeeds, not after another coordinator decision. Physical execution does not certify scientific correctness or visual quality.')]


class ImplementationToolGateway:
    def delivery_preflight(self, observations: list[dict[str, Any]], arguments: dict[str, Any]) -> list[str]:
        # Admission to this owner's final operation is not its success gate.
        # Actual execution/version issues are checked again at terminal commit.
        return []

    def delivery_issues(self, observations: list[dict[str, Any]]) -> list[str]:
        if self.exact is not None:
            return []
        from simple_ar.tools.implementation_run import delivery_issues
        return delivery_issues(self, observations)

    @property
    def context_tools(self) -> set[str]:
        return {'prepare_code_task', 'implement_code_task'} if self.exact is not None else {'execute_implementation'}

    def execute_tool(self, name: str, context: Any, arguments: dict[str, Any],
                     *, approval: dict[str, Any] | None = None) -> ToolResult:
        if name not in self.context_tools:
            raise ValueError('Operation is not bound to this implementation owner')
        if name == 'execute_implementation':
            from simple_ar.tools.implementation_run import execute_prepared
            return execute_prepared(self, context, arguments, approval=approval or {})
        return (self.prepare(context, arguments, approval=approval or {}) if name == 'prepare_code_task'
            else self.execute(context, arguments, approval=approval or {}))

    def has_started(self, identity: str) -> bool:
        return ((self.workspace.workspace / 'code_tasks' / identity).exists() if self.exact
            else self.workspace.has_started(identity))

    def can_reconcile(self, name: str, identity: str) -> bool:
        if name == 'execute_implementation' and self.exact is None:
            return ((self.root / 'attempts' / identity / 'candidate_execution.json').is_file()
                or self.workspace.can_reconcile('run_process', identity))
        if self.exact:
            if name == 'prepare_code_task':
                try:
                    self._check_preparation(self.root / 'attempts' / identity / 'execution.json')
                    return True
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    return False
            if name != 'implement_code_task':
                return False
            if (self.root / 'attempts' / identity / 'implementation.json').is_file():
                return True
            from simple_ar.code_task.orchestration.execute import implementation_resume_allowed
            return implementation_resume_allowed(self.workspace.workspace / 'code_tasks' / identity,
                ledger=self.ledger, attempt_id=identity)
        return name == 'prepare_implementation' and (
            self.root / 'attempts' / identity / 'implementation' / 'result.json').is_file()

    def __init__(self, root: Path, workspace: Any, ledger: Any, model: str, session_id: str,
                 *, client_factory: Callable[[str], LLMClient] | None = None,
                 execution_observations: Callable[[], list[dict[str, Any]]] | None = None,
                 candidate_outputs: Callable[[Path], dict[str, str]] | None = None,
                 measurement_inputs: Callable[[dict[str, str] | None, bool], dict[str, Any]] | None = None,
                 research_inputs: Callable[[], dict[str, Any]] | None = None,
                 measurement: dict[str, Any] | None = None,
                 measurement_supplier: Callable[[], dict[str, Any] | None] | None = None,
                 exact: dict[str, Any] | None = None) -> None:
        self.root, self.workspace, self.ledger = root, workspace, ledger
        self.model, self.session_id = model, session_id
        self.client_factory = client_factory
        self.execution_observations = execution_observations
        self.candidate_outputs = candidate_outputs
        self.measurement_inputs = measurement_inputs
        self.research_inputs = research_inputs
        self.exact = ExactCodeTask.model_validate(exact) if exact is not None else None
        if measurement_supplier is not None and self.exact is None:
            raise ValueError('A producing measurement supplier requires the exact implementation owner')
        self._measurement_supplier = measurement_supplier
        self._declared_measurement = self._validate_measurement(measurement)

    def _validate_measurement(self, measurement: dict[str, Any] | None) -> dict[str, Any] | None:
        if measurement is not None:
            if self.exact is None:
                raise ValueError('A producing measurement contract requires the exact implementation owner')
            from simple_ar.tools.scientific import measurement_contract
            measurement = measurement_contract(measurement)
            from simple_ar.tools.scientific import measurement_conditions
            if any(row['execution'].get('environment') is not None for row in measurement_conditions(measurement)):
                raise ValueError('Scoped measurement inherits its implementation preparation; do not request a second environment owner')
        return measurement

    @property
    def measurement(self) -> dict[str, Any] | None:
        # The scientific owner projects accepted source authority. Do not cache
        # a second mutable copy that can outlive a revision or bound acceptance.
        if self._declared_measurement is not None:
            return self._declared_measurement
        return self._validate_measurement(self._measurement_supplier()) if self._measurement_supplier else None

    def _formal_execution(self) -> dict[str, Any]:
        from simple_ar.experiment.execution.measurement import representative_measurement_execution
        return (representative_measurement_execution(self.measurement) if self.measurement is not None
            else {'command': self.exact.validation_command})

    def approval_binding(self, arguments: dict[str, Any], *, identity: str,
                         for_preparation: bool = False) -> dict[str, Any]:
        if self.exact is None:
            from simple_ar.tools.implementation_run import execution_binding
            return execution_binding(self, arguments, identity=identity)
        work = ExactCodeWork.model_validate(arguments)
        project = self.workspace.resolve(self.exact.project_path) if self.exact.project_path else None
        if project is not None:
            if self.measurement is not None:
                from simple_ar.tools.scientific import measurement_conditions
                if any(row.get('source', 'implementation') != 'declared'
                        and Path(row['execution']['cwd']).resolve() != project.resolve()
                        for row in measurement_conditions(self.measurement)):
                    raise ValueError('Source-owned measurement cwd must match the approved project before relocation')
            for name in self.exact.new_files:
                target = project / name
                parts = [target]
                while parts[-1].parent != project:
                    parts.append(parts[-1].parent)
                if (target.exists() or any(part.is_symlink() or part.is_junction() for part in parts)
                        or any(part.is_file() for part in parts[1:]) or not target.resolve().is_relative_to(project)):
                    raise ValueError('Required new file must not replace an original or traverse an external link')
        from simple_ar.code_task.analysis.index import build_codebase_index
        preparation = self.exact.preparation
        cwd = self.workspace.workspace / 'code_tasks' / identity / 'code_task' / 'workspace'
        source = ({row['path']: row['sha256'] for row in build_codebase_index(project)['files']}
                  if project is not None and preparation is None else {})
        snapshot = None
        python = str(self.workspace.python)
        if preparation is not None:
            from simple_ar.code_task.workspace.modes import WorkspaceSpec, workspace_source_binding, _source_venv_python
            from simple_ar.code_task.execution.environment import _resolve_policy_python
            snapshot = workspace_source_binding(WorkspaceSpec(project, cwd.parent,
                mode=preparation.workspace_mode, max_file_bytes=preparation.max_file_bytes,
                include=tuple(preparation.workspace_include), exclude=tuple(preparation.workspace_exclude),
                reuse_source_venv=preparation.workspace_reuse_source_venv, data_inputs=tuple(preparation.data_inputs)))
            if not snapshot['source_snapshots']:
                raise ValueError('Requested workspace has no safely bound initialization alternative')
            git = snapshot.get('git')
            if (preparation.workspace_mode == 'auto' and git and not git['origin_dirty']
                    and 'git_worktree' not in snapshot['source_snapshots']):
                raise ValueError('Auto would prefer an unbound Git checkout; explicitly select copy or use the expert protocol before approval')
            source_python = _source_venv_python(project) if project and preparation.workspace_reuse_source_venv else None
            python = str(source_python) if source_python and not preparation.python_executable else _resolve_policy_python(
                preparation.env_mode, preparation.python_executable)
        protected = list(self.exact.protected_patterns)
        validators = {}
        external_inputs = {}
        inputs = [(index, argument, False) for index, argument in enumerate(self.exact.validation_command)]
        if self.exact.environment is not None:
            profile = self.exact.environment
            inputs += [(index, argument, True) for index, argument in enumerate(profile.get('check_command', []))]
            inputs += [(1, name, True) for name in [*profile['requirements'],
                *([name for name in ('pyproject.toml', 'setup.py', 'setup.cfg')
                   if project and (project / name).is_file()] if profile['install_project'] else [])]]
        for index, argument, preparing in inputs:
            value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
            candidate = ((project or self.workspace.workspace) / value).resolve()
            if candidate.is_file():
                if project is None or not candidate.is_relative_to(project):
                    if preparation is not None and index == 0:
                        continue  # The executable argv is explicit in the frozen approval, not source code.
                    if preparation is not None:
                        supplied = self.workspace.resolve(str(candidate))
                        if any(name not in {'workspace', 'records', 'evidence'} and supplied.is_relative_to(root)
                               for name, root in self.workspace.mounts.items()):
                            from simple_ar.experiment.execution.measurement import snapshot_protocol_assets
                            observed = snapshot_protocol_assets({'protected_assets': [
                                {'asset_id': 'supplied_input', 'path': str(supplied)}]}, project or self.workspace.workspace)
                            external_inputs[str(supplied)] = observed['supplied_input']['sha256']
                            continue
                    raise ValueError('Validator files must be inside the mounted project')
                relative = candidate.relative_to(project).as_posix()
                if snapshot is not None and any(relative not in files for files in snapshot['source_snapshots'].values()):
                    raise ValueError('Workspace policy omits a declared validator file; correct it before approval')
                validators[relative] = self.workspace.artifact_hashes([str(candidate)])[str(candidate)]
                protected.append(relative)
        from simple_ar.code_task.execution.runner import _safe_env
        binding = {'contract': self.exact.model_dump(),
            'source': {'project_path': str(project) if project else '', 'file_hashes': source},
            'validator_files': validators, 'protected_patterns': sorted(set(protected)),
            'python_executable': python,
            'environment': _safe_env(cwd)}
        if self.measurement is not None:
            binding['measurement_contract'] = self.measurement
        if work.instruction or work.prior_result or work.prepared_result:
            binding['work'] = work.model_dump()
        predecessor = work.prior_result or work.prepared_result
        if predecessor:
            record, base = (self._prior_candidate(predecessor, identity=identity) if work.prior_result
                else self._prior_preparation(predecessor, identity=identity))
            binding['prior_source'] = {'record': predecessor, 'project_path': str(base),
                'sha256': hashlib.sha256(record.read_bytes()).hexdigest()}
            from simple_ar.code_task.runtime.state import implementation_file_hashes
            inventory = implementation_file_hashes(self.workspace.workspace / 'code_tasks' / record.parent.name)
            if preparation is not None or work.prepared_result or inventory != ArtifactStore(record.parent).read_json(
                    'implementation.json')['implementation_file_hashes']:
                binding['prior_source']['file_hashes'] = inventory
            if snapshot is None:
                binding['source']['file_hashes'] = inventory
        if snapshot is not None:
            binding['source'] = {'project_path': str(project) if project else '', 'workspace_input': snapshot}
            modes = ['copy'] if predecessor else snapshot['source_snapshots']
            binding['process_environments'] = {}
            for mode in modes:
                relative = (snapshot['git']['project_relative_path']
                    if mode == 'git_worktree' else '.')
                actual = cwd / relative
                binding['process_environments'][mode] = {'cwd': str(actual), 'environment': _safe_env(actual)}
            binding.pop('environment')  # The selected mode owns its actual project PYTHONPATH.
            if external_inputs:
                binding['external_inputs'] = external_inputs
        if self.exact.environment is not None:
            if predecessor:
                from simple_ar.code_task.runtime.state import load_code_task_manifest
                previous = load_code_task_manifest(self.workspace.workspace / 'code_tasks' / record.parent.name)
                binding['python_executable'] = previous['environment']['policy']['python_executable']
                prior_binding = previous['code_task']['native_binding']['binding']
                binding['environment_origin'] = prior_binding.get('environment_origin') or {
                    'record': predecessor, 'sha256': binding['prior_source']['sha256']}
            else:
                binding['environment_preparations'] = {mode: self._environment_plan(
                    Path(process['cwd']), project or self.workspace.workspace, identity, python)
                    for mode, process in binding['process_environments'].items()}
                binding['python_executable'] = next(iter(binding['environment_preparations'].values()))['interpreter']
            from simple_ar.research.project_environment import project_environment_process_env
            for process in binding['process_environments'].values():
                process['environment'] = project_environment_process_env(self.exact.environment, process['environment'])
        conditions = (self.measurement or {}).get('conditions', [])
        prior_required = any(row.get('before_implementation') for row in conditions)
        if not for_preparation and (predecessor and prior_required or any(row.get('reuse_ref') for row in conditions)):
            if self.measurement_inputs is None:
                raise ValueError('Required prior measurements have no scientific evidence owner')
            binding['measurement_inputs'] = self.measurement_inputs(self._preparation_origin(
                {'binding': binding}, identity=identity), prior_required)
        if not for_preparation and self.research_inputs is not None:
            research = self.research_inputs()
            if research:
                binding['research_inputs'] = research
        return binding

    def _environment_plan(self, cwd: Path, origin: Path, identity: str, python: str) -> dict[str, Any]:
        from simple_ar.code_task.execution.environment import project_command
        from simple_ar.research.project_environment import project_environment_plan, environment_preparation_identity, project_environment_process_env
        from simple_ar.code_task.execution.runner import _safe_env
        formal = self._formal_execution()
        command = project_command(formal['command'], origin, cwd,
            python_executable=python if self.exact.environment['mode'] == 'current' else None)
        profile = dict(self.exact.environment)
        if profile.get('check_command'):
            profile['check_command'] = project_command(profile['check_command'], origin, cwd,
                python_executable=python if profile['mode'] == 'current' else None)
        plan = project_environment_plan(profile, cwd, self.root / 'attempts' / identity / 'environment',
            command, input_project=origin)
        environment = project_environment_process_env(profile, _safe_env(cwd))
        protocol = formal.get('protocol')
        if protocol is not None:
            protocol = {**protocol, 'protected_assets': [{**row,
                'path': project_command([row['path']], origin, cwd)[0]} for row in protocol.get('protected_assets', [])]}
        return {'environment_preparation': environment_preparation_identity(plan, cwd, environment), 'command': command,
            'store_root': str(self.root / 'attempts' / identity),
            'cwd': str(cwd), 'argv': plan['command'], 'result_schema': formal.get('result_schema'),
            'protocol': protocol, 'interpreter': plan['python_executable'] or python}

    def _environment_evidence(self, store: ArtifactStore, run: Path, approval: dict[str, Any]) -> list[Path]:
        """Resolve actual setup provenance, including feedback that reuses it."""
        if self.exact.environment is None:
            return []
        from simple_ar.code_task.runtime.state import load_code_task_manifest
        from simple_ar.research.project_environment import verify_environment_preparation, environment_executable
        binding = approval['binding']
        retained = []
        if binding.get('environment_origin'):
            origin = binding['environment_origin']
            record = self.workspace.resolve(origin['record'])
            if (record.name not in {'implementation.json', 'execution.json'} or record.parent.parent != self.root / 'attempts'
                    or hashlib.sha256(record.read_bytes()).hexdigest() != origin['sha256']):
                raise ValueError('Inherited preparation origin changed or escaped this task')
            if record.name == 'execution.json':
                self._prior_preparation(origin['record'], identity=run.name)
            store = ArtifactStore(record.parent)
            run = self.workspace.workspace / 'code_tasks' / record.parent.name
            approval = load_code_task_manifest(run)['code_task']['native_binding']
            if (store.read_json(record.name).get('native_binding') != approval
                    or approval['binding']['contract'] != self.exact.model_dump()):
                raise ValueError('Inherited environment has a different approved contract')
            retained.append(record)
        manifest = load_code_task_manifest(run)
        if store.exists('implementation.json') or store.exists('execution.json') and store.read_json('execution.json').get('artifact_hashes'):
            payload = store.read_json('implementation.json' if store.exists('implementation.json') else 'execution.json')
            for name in ('environment_setup', 'environment_observation'):
                if (payload.get('artifact_hashes', {}).get(name + '.json')
                        != hashlib.sha256(store.resolve(name + '.json').read_bytes()).hexdigest()):
                    raise ValueError('Retained preparation evidence changed or lacks its original implementation binding')
        view = approval['binding']['environment_preparations'][manifest['workspace']['selected_mode']]
        _, required = verify_environment_preparation(store, view, ledger=self.ledger,
            session_id=self.session_id, attempt_id=run.name)
        observed = store.read_json('environment_observation.json')
        process_environment = approval['binding']['process_environments'][manifest['workspace']['selected_mode']]['environment']
        executable = environment_executable(view['argv'], Path(view['cwd']), process_environment)
        if hashlib.sha256(executable.read_bytes()).hexdigest() != observed['executable_sha256']:
            raise ValueError('Prepared interpreter changed after its recorded check')
        return [*retained, *required, Path(run) / 'manifest.json']

    def _prior_candidate(self, locator: str, *, identity: str) -> tuple[Path, Path]:
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
        record = self.workspace.resolve(locator)
        if (record.name != 'implementation.json' or record.parent.parent != (self.root / 'attempts').resolve()
                or record.parent.name == identity):
            raise ValueError('Prior implementation must be an existing different attempt in this task')
        store = ArtifactStore(record.parent)
        payload = store.read_json('implementation.json')
        run = (self.workspace.workspace / 'code_tasks' / record.parent.name).resolve()
        if Path(payload['code_task_run_dir']).resolve() != run:
            raise ValueError('Prior implementation belongs to a different candidate')
        binding = load_code_task_manifest(run)['code_task']['native_binding']
        if (payload.get('status') != 'validated' or binding['binding']['contract'] != self.exact.model_dump()
                or binding['binding'].get('measurement_contract') != self.measurement):
            raise ValueError('Prior candidate is incomplete or has a different scope/validator contract')
        self._check_retained_result(store, payload, run, binding)
        return record, code_task_paths(run).workspace_dir

    def _registered_ref(self, record: Path, *, kind: str, store_root: Path | None = None) -> Any:
        store = ArtifactStore(store_root or record.parent)
        relative = record.relative_to(store.root).as_posix()
        attempt = store.read_attempt_manifest()
        capability = store.read_capability_result()
        session = ArtifactStore(self.root).read_json('session_manifest.json')
        if (attempt.attempt_id != store.root.name or attempt.capability != 'native_tool'
                or attempt.status != 'completed' or capability.status != 'completed'
                or session['session_id'] != self.session_id
                or attempt.attempt_id not in session['budget']['recorded_attempts']):
            raise ValueError('Source is not a completed native_tool attempt in this task')
        registered = [ref for ref in attempt.outputs if ref.path == relative
                      and ref.kind == kind and ref.status == 'available' and ref in capability.artifacts]
        if len(registered) != 1:
            raise ValueError('Source is not an actual registered output')
        return registered[0]

    def _check_preparation(self, record: Path, *, allow_pre_design: bool = False) -> tuple[dict[str, Any], Path]:
        """Verify an unedited preparation, not an implementation or a directory hint."""
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, retained_implementation_inventory, native_preparation_processes
        store = ArtifactStore(record.parent)
        payload = store.read_json('execution.json')
        run = self.workspace.workspace / 'code_tasks' / record.parent.name
        manifest = load_code_task_manifest(run)
        approval = manifest['code_task']['native_binding']
        measurement = self.measurement
        pre_design = (allow_pre_design and self._measurement_supplier is not None
            and self._declared_measurement is None and measurement is not None
            and 'measurement_contract' not in approval['binding'])
        candidate = code_task_paths(run).workspace_dir.resolve()
        task = payload.get('execution', {}).get('code_task', {})
        if (payload.get('schema_version') != 'prepared_execution.v1'
                or payload.get('native_binding') != approval or not approval.get('token')
                or approval['binding']['contract'] != self.exact.model_dump()
                or not pre_design and approval['binding'].get('measurement_contract') != measurement
                or Path(payload['workspace']).resolve() != candidate
                or Path(payload['execution']['cwd']).resolve() != candidate
                or Path(task['run_dir']).resolve() != run.resolve()
                or task.get('validation_command') != self.exact.validation_command
                or task.get('validation_timeout_sec') != self.exact.timeout_sec
                or manifest['environment']['policy']['python_executable'] != approval['binding']['python_executable']):
            raise ValueError('Prepared source differs from its frozen source/environment contract')
        outputs = self.candidate_outputs(record) if self.candidate_outputs else {}
        retained_implementation_inventory(run, payload['file_hashes'], outputs)
        for name, digest in payload['artifact_hashes'].items():
            if hashlib.sha256(store.resolve(name).read_bytes()).hexdigest() != digest:
                raise ValueError('Retained preparation evidence changed')
        self._environment_evidence(store, run, approval)
        processes = native_preparation_processes(run, ledger=self.ledger,
            session_id=self.session_id, attempt_id=run.name)
        entries = [row for row in self.ledger.entries if row.attempt_id == run.name
            and row.purpose == 'local_process' and row.status != 'released']
        if (len(entries) != len(processes) or {row.logical_call_id for row in entries} != processes
                or any(code_task_paths(run).run_artifact_dir.glob('*/process/*/invocation.json'))
                or any((store.root / 'execution' / 'process').glob('*/invocation.json'))):
            raise ValueError('Preparation contains unknown, extra or orphan physical work; no replay')
        return payload, candidate

    def _prior_preparation(self, locator: str, *, identity: str) -> tuple[Path, Path]:
        record = self.workspace.resolve(locator)
        if (record.name != 'execution.json' or record.parent.parent != (self.root / 'attempts').resolve()
                or record.parent.name == identity):
            raise ValueError('Prepared source must be a different registered preparation in this task')
        self._registered_ref(record, kind='prepared_execution')
        # Consume the unchanged, registered preparation under a NEW approval.
        # This does not replay/reapprove its old physical work or revise receipts.
        payload, candidate = self._check_preparation(record, allow_pre_design=True)
        observed = ToolResult.model_validate(ArtifactStore(record.parent).read_json('native_result.json'))
        if (observed.tool_name != 'prepare_code_task' or observed.status != 'ok'
                or observed.data != self._prepared_result(record, payload, candidate).data):
            raise ValueError('Preparation observation differs from its actual retained source')
        return record, candidate

    def _prepared_result(self, record: Path, payload: dict[str, Any], candidate: Path) -> ToolResult:
        locator = self.workspace.locator(str(record))
        return ToolResult(tool_name='prepare_code_task', status='ok',
            data={'record': locator, 'sha256': hashlib.sha256(record.read_bytes()).hexdigest(),
                'project_path': self.workspace.locator(str(candidate)),
                'python_executable': payload['native_binding']['binding']['python_executable']},
            artifacts=[record.relative_to(self.root).as_posix()],
            summary='Prepared source/environment only; no code generation, validator or formal measurement has been performed.')

    def prepare(self, context: Any, arguments: dict[str, Any], *, approval: dict[str, Any]) -> ToolResult:
        from simple_ar.code_task.runtime.state import implementation_file_hashes
        if self.exact is None or arguments or not approval.get('token'):
            raise ValueError('Preparation requires the frozen exact contract, empty arguments and explicit approval')
        identity = context.attempt.attempt_id
        if approval['binding'] != self.approval_binding({}, identity=identity, for_preparation=True):
            raise ValueError('Approved preparation source or environment changed; no replay')
        record = context.store.root / 'execution.json'
        run = self.workspace.workspace / 'code_tasks' / identity
        if record.is_file() and context.store.read_json(record.name).get('native_binding'):
            payload, candidate = self._check_preparation(record)
            if payload['native_binding'] != approval:
                raise ValueError('Preparation belongs to a different approval')
        else:
            if run.exists():
                raise ValueError('Interrupted preparation is not replayable; inspect original records and ledger')
            project = self.workspace.resolve(self.exact.project_path) if self.exact.project_path else None
            self._prepare_candidate(context, ExactCodeWork(), approval, run, project, self.exact.goal,
                for_preparation=True)
            payload = context.store.read_json(record.name)
            payload.update(native_binding=approval, file_hashes=implementation_file_hashes(run),
                artifact_hashes={path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in self._environment_evidence(context.store, run, approval)
                    if path.parent == context.store.root and path != record})
            context.store.write_json(record.name, payload, kind='prepared_execution', schema='prepared_execution.v1')
            payload, candidate = self._check_preparation(record)
        return self._prepared_result(record, payload, candidate)

    def prepared_source(self, locator: str, *, identity: str) -> dict[str, Any]:
        from simple_ar.code_task.runtime.state import load_code_task_manifest, native_workspace_binding
        record, candidate = self._prior_preparation(locator, identity=identity)
        payload = ArtifactStore(record.parent).read_json(record.name)
        manifest = load_code_task_manifest(self.workspace.workspace / 'code_tasks' / record.parent.name)
        bound = payload['native_binding']['binding']
        protected = self._source_protected_assets(manifest, candidate)
        evidence = [record, record.parent / 'native_result.json', record.parent / 'attempt_manifest.json',
            record.parent / 'capability_result.json', self.workspace.workspace / 'code_tasks' / record.parent.name / 'manifest.json',
            *(record.parent / name for name in payload['artifact_hashes'])]
        evidence.extend(self._environment_evidence(ArtifactStore(record.parent),
            self.workspace.workspace / 'code_tasks' / record.parent.name, payload['native_binding']))
        ref = self._registered_ref(record, kind='prepared_execution')
        return {'implementation_ref': None,
            'preparation_origin': {'path': record.relative_to(self.root.resolve()).as_posix(),
                'sha256': hashlib.sha256(record.read_bytes()).hexdigest()},
            'prepared_ref': {**ref.to_dict(), 'path': record.relative_to(self.root.resolve()).as_posix()},
            'cwd': str(candidate), 'python_executable': bound['python_executable'], 'validation_command': None,
            'environment': native_workspace_binding(manifest, candidate)['environment'],
            'file_hashes': payload['file_hashes'], 'protected_assets': protected,
            'evidence_hashes': self.workspace.artifact_hashes(self.workspace.locator(str(path)) for path in evidence),
            'limitations': ['Unedited prepared source; no implementation validation is claimed.']}

    def _source_protected_assets(self, manifest: dict[str, Any], cwd: Path) -> list[dict[str, Any]]:
        protected = []
        for asset in manifest['code_task']['native_protocol']['protected_assets']:
            named = Path(asset['path'])
            named = named if named.is_absolute() else cwd / named
            if any(part.is_symlink() or part.is_junction() for part in (named, *named.parents)):
                raise ValueError('Native protected assets cannot traverse links')
            path = self.workspace.resolve(str(named))
            if not path.is_file():
                raise ValueError('Native protected asset is unavailable')
            protected.append({**asset, 'path': str(path)})
        for path, digest in manifest['code_task']['native_binding']['binding'].get('external_inputs', {}).items():
            if self.workspace.artifact_hashes([path])[path] != digest:
                raise ValueError('Approved external implementation input changed')
        return protected

    def _preparation_origin(self, approval: dict[str, Any], *, identity: str) -> dict[str, str] | None:
        """Follow retained source links; never guess lineage from a comparison role."""
        seen = {identity}
        while prior := approval['binding'].get('prior_source'):
            record = self.workspace.resolve(prior['record'])
            if (record.parent.name in seen or record.parent.parent != (self.root / 'attempts').resolve()
                    or hashlib.sha256(record.read_bytes()).hexdigest() != prior['sha256']):
                raise ValueError('Prepared source ancestry changed or contains a cycle')
            seen.add(record.parent.name)
            if record.name == 'execution.json':
                self._prior_preparation(prior['record'], identity=identity)
                return {'path': record.relative_to(self.root.resolve()).as_posix(), 'sha256': prior['sha256']}
            self._registered_ref(record, kind='implementation_result')
            if record.name != 'implementation.json':
                raise ValueError('Source ancestry must name a retained implementation or preparation')
            self._prior_candidate(prior['record'], identity=identity)
            approval = ArtifactStore(record.parent).read_json(record.name)['native_binding']
        return None

    def measurement_source(self, locator: str, *, identity: str) -> dict[str, Any]:
        """Read a completed native implementation and its independent validator.

        No active-candidate hint, model call or recovery mutation is involved.
        Later checks pin the approved inventory; newly produced files are not
        retroactively treated as implementation source.
        """
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, native_workspace_binding
        from simple_ar.code_task.execution.runner import _argv_command, _split_command, native_validation_process
        if self.exact is None or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
            raise ValueError('Measurement source requires a frozen native implementation owner and valid consuming identity')
        record, candidate = self._prior_candidate(locator, identity=identity)
        store = ArtifactStore(record.parent)
        registered = self._registered_ref(record, kind='implementation_result')
        payload = store.read_json('implementation.json')
        observed = ToolResult.model_validate(store.read_json('native_result.json'))
        if (observed.tool_name != 'implement_code_task' or observed.status != 'ok'
                or {key: value for key, value in observed.data.items() if key != 'project_path'} != payload
                or self.workspace.resolve(observed.data['project_path']) != candidate.resolve()):
            raise ValueError('Native implementation observation differs from its domain payload')
        run = Path(payload['code_task_run_dir']).resolve()
        paths, manifest = code_task_paths(run), load_code_task_manifest(run)
        cwd = self.workspace.resolve(str(candidate), write=True)
        approval = manifest['code_task']['native_binding']
        bound = approval['binding']
        python = manifest['environment']['policy'].get('python_executable')
        environment = native_workspace_binding(manifest, cwd)['environment']
        if (not approval.get('token') or not python or python != bound['python_executable']
                or not Path(python).is_absolute() or not Path(python).is_file()
                or Path(payload['workspace_dir']).resolve() != cwd):
            raise ValueError('Candidate interpreter/workspace does not match its approved actual environment')
        validation = payload.get('validation') or {}
        from simple_ar.code_task.execution.environment import native_validation_command
        validator = native_validation_command(manifest, cwd)
        if (validation.get('status') != 'passed' or validation.get('returncode') != 0
                or validation.get('timed_out') is not False
                or validation.get('command') != validator
                or validation.get('timeout_sec') != self.exact.timeout_sec):
            raise ValueError('Implementation lacks independent passed validator facts')
        report_ref = ArtifactRef.from_dict(payload['artifact_refs']['validation_report'])
        report = store.read_json(report_ref)
        invocation_id = report.get('process', {}).get('invocation_id', '')
        if not re.fullmatch(r'[0-9a-f]{32}', invocation_id):
            raise ValueError('Independent validator has no physical invocation identity')
        invocation = paths.run_artifact_dir / 'patched' / 'process' / invocation_id / 'invocation.json'
        process = native_validation_process(run, budget_ledger=self.ledger, session_id=self.session_id,
            attempt_id=record.parent.name, invocation_id=invocation_id,
            file_hashes=payload['implementation_file_hashes'])
        if (report.get('status') != 'passed' or report.get('returncode') != 0 or report.get('timed_out') is not False
                or report.get('native_binding') != approval
                or report.get('implementation_file_hashes') != payload['implementation_file_hashes']
                or report.get('environment', {}).get('python_executable') != python
                or report.get('command') != _split_command(_argv_command(validator), environment_policy=manifest['environment']['policy'])
                or report.get('process') != process or process.get('status') != 'finished'
                or process.get('returncode') != 0 or process.get('stop_reason') is not None):
            raise ValueError('Independent validator record disagrees with approved candidate/environment')
        protected = self._source_protected_assets(manifest, cwd)
        evidence = [record, store.root / 'native_result.json', store.root / 'attempt_manifest.json',
            store.root / 'capability_result.json', paths.manifest_path, invocation,
            *(store.resolve(row['path']) for row in payload['artifact_refs'].values())]
        attempt, capability = store.read_attempt_manifest(), store.read_capability_result()
        prepared = [ref for ref in attempt.outputs if ref.kind == 'prepared_execution' and ref in capability.artifacts]
        if len(prepared) > 1 or (store.exists('execution.json') and not prepared):
            raise ValueError('Preparation exists without one registered native prepared_execution output')
        prepared_ref = None
        if prepared:
            ref = prepared[0]
            preparation = store.read_json(ref)
            task = preparation['execution']['code_task']
            if (ref.path != 'execution.json' or preparation.get('schema_version') != 'prepared_execution.v1'
                    or Path(preparation['workspace']).resolve() != cwd
                    or Path(preparation['execution']['cwd']).resolve() != cwd
                    or Path(task['run_dir']).resolve() != run
                    or task.get('validation_command') != self.exact.validation_command
                    or task.get('validation_timeout_sec') != self.exact.timeout_sec
                    or task.get('python_executable') not in (None, python)):
                raise ValueError('Registered preparation does not describe this actual candidate')
            expected = {**self._formal_execution(), 'cwd': str(cwd)} if self.measurement is not None else {}
            if bound.get('environment_preparations'):
                view = bound['environment_preparations'][manifest['workspace']['selected_mode']]
                expected = {'command': view['argv'], 'cwd': view['cwd'], **{
                    key: view[key] for key in ('result_schema', 'protocol') if view[key] is not None}}
            if any(preparation['execution'].get(key) != value for key, value in expected.items()):
                raise ValueError('Registered preparation differs from its frozen formal measurement contract')
            prepared_ref = {**ref.to_dict(), 'path': (store.root / ref.path).relative_to(self.root.resolve()).as_posix()}
            evidence.append(store.resolve(ref))
        evidence.extend(self._environment_evidence(store, run, approval))
        hashes = self.workspace.artifact_hashes(self.workspace.locator(str(path)) for path in evidence)
        origin = self._preparation_origin(approval, identity=identity)
        return {**({'preparation_origin': origin} if origin else {}),
            'implementation_ref': {**registered.to_dict(), 'path': record.relative_to(self.root.resolve()).as_posix()},
            'prepared_ref': prepared_ref, 'cwd': str(cwd), 'python_executable': python,
            'validation_command': report['command'],
            'environment': environment, 'file_hashes': dict(payload['implementation_file_hashes']),
            'protected_assets': protected, 'evidence_hashes': hashes,
            'limitations': [] if prepared_ref else ['This completed native implementation has no preparation record; none was synthesized.']}

    def execute(self, context: Any, arguments: dict[str, Any], *, approval: dict[str, Any]) -> ToolResult:
        """Consume the controller's actual attempt; retain only domain outputs."""
        from simple_ar.core.capabilities import CapabilityContext
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, implementation_file_hashes, native_workspace_binding
        from simple_ar.research.implementation import ImplementationRequest as CodeRequest, run_implementation_capability

        if self.exact is None or not approval.get('token'):
            raise ValueError('Implementation requires the frozen exact contract and explicit approval')
        work = ExactCodeWork.model_validate(arguments)
        contract = self.exact
        identity = context.attempt.attempt_id
        run = self.workspace.workspace / 'code_tasks' / identity
        binding = approval['binding']
        if binding['contract'] != contract.model_dump():
            raise ValueError('Saved approval belongs to a different CodeTask contract')
        if binding != self.approval_binding(arguments, identity=identity):
            raise ValueError('Approved source identity or process environment changed; no replay')
        if (any(row.get('before_implementation') for row in (self.measurement or {}).get('conditions', []))
                and not binding.get('measurement_inputs')):
            raise ValueError('Prepare and complete the required prior measurements before implementing from that source')
        if run.exists():
            manifest = load_code_task_manifest(run)
            if manifest.get('code_task', {}).get('native_binding') != approval:
                raise ValueError('CodeTask manifest does not bind this original contract/approval/source')
            from simple_ar.code_task.editing.scope import is_edit_allowed_path
            scope = manifest['edit_scope']
            current = implementation_file_hashes(run)
            for name, digest in native_workspace_binding(manifest, code_task_paths(run).workspace_dir)['file_hashes'].items():
                if not is_edit_allowed_path(name, allowed_patterns=tuple(scope['allowed_patterns']),
                        protected_patterns=tuple(scope['protected_patterns'])) and current.get(name) != digest:
                    raise ValueError('Protected original or validator changed in the CodeTask workspace')
        if not context.store.exists('implementation.json') and context.store.exists('native_result.json'):
            # The accepted observation already contains the original domain
            # payload. Re-running the orchestrator would replace its original
            # steps with later "skipped" steps and break source identity.
            observed = ToolResult.model_validate(context.store.read_json('native_result.json'))
            retained = {key: value for key, value in observed.data.items() if key != 'project_path'}
            if (observed.tool_name != 'implement_code_task' or observed.status not in {'ok', 'blocked'}
                    or retained.get('schema_version') != 'research_implementation.v1'
                    or Path(retained.get('code_task_run_dir', '')).resolve() != run.resolve()
                    or self.workspace.resolve(observed.data['project_path']) != code_task_paths(run).workspace_dir.resolve()
                    or (observed.status == 'ok') != (retained.get('status') == 'validated')):
                raise ValueError('Retained native observation does not describe the approved implementation')
            self._check_retained_result(context.store, retained, run, approval)
            context.store.write_json('implementation.json', retained,
                kind='implementation_result', schema='research_implementation.v1', producer='research.implementation')
        if context.store.exists('implementation.json'):
            payload = context.store.read_json('implementation.json')
            self._check_retained_result(context.store, payload, run, approval)
        else:
            project = self.workspace.resolve(contract.project_path) if contract.project_path else None
            research_ref = None
            if research := binding.get('research_inputs'):
                design_path = self.workspace.resolve(research['design_ref'])
                content = design_path.read_bytes()
                if hashlib.sha256(content).hexdigest() != research['hashes'].get(research['design_ref']):
                    raise ValueError('Accepted research design changed before implementation')
                research_ref = context.store.write_json('inputs/research_design.json', json.loads(content),
                    kind='research_design', schema='research_design.v1', producer='sources')
            if work.prior_result:
                _, project = self._prior_candidate(work.prior_result, identity=identity)
            elif work.prepared_result:
                _, project = self._prior_preparation(work.prepared_result, identity=identity)
            task_text = contract.goal + (
                '\n\nApproved work within the original scope:\n' + work.instruction if work.instruction else '')
            if binding.get('measurement_inputs'):
                task_text += '\n\nRecorded prior measurement evidence (not scientific proof or permission to change conditions):\n' + json.dumps(
                    binding['measurement_inputs']['observations'], ensure_ascii=False)
            if contract.new_files:
                task_text += '\n\nRequired new project files (create real implementations directly, not initializer placeholders):\n' + '\n'.join(contract.new_files)
            if self.measurement is not None and 'conditions' in self.measurement:
                task_text += ('\n\nCaller-frozen formal conditions the implementation must support. '
                    'These are requirements, not permission to execute experiments during implementation; '
                    'the independent validator and each actual formal measurement remain separate:\n'
                    + json.dumps(self.measurement, ensure_ascii=False))
            if not run.exists():
                self._prepare_candidate(context, work, approval, run, project, task_text)
            else:
                from simple_ar.code_task.orchestration.execute import implementation_resume_allowed
                if not implementation_resume_allowed(run, ledger=self.ledger, attempt_id=identity):
                    raise ValueError('Interrupted CodeTask has uncommitted or unknown operations; inspect its manifest and original ledger without replay')
            brief = context.store.write_json('inputs/brief.json',
                {'request_text': task_text, 'objective': task_text, 'hard_constraints': []},
                kind='research_brief')
            domain_context = CapabilityContext(context.store, context.attempt,
                inputs=(brief, research_ref) if research_ref is not None else (brief,), input_store=context.store)
            manifest = load_code_task_manifest(run)
            from simple_ar.code_task.execution.environment import native_validation_command
            run_implementation_capability(context=domain_context, request=CodeRequest(
                run_dir=run, cwd=code_task_paths(run).workspace_dir, approval_note='Explicit native approval: ' + approval['token'],
                llm_client=self._client(identity), validation_command=tuple(native_validation_command(manifest, code_task_paths(run).workspace_dir)),
                validation_timeout_sec=contract.timeout_sec,
                max_repairs=contract.preparation.max_repairs if contract.preparation is not None else 1,
                protocol=manifest['code_task']['native_protocol'],
                **(contract.edit_policy or {}),
                budget_ledger=self.ledger, session_id=self.session_id, attempt_id=identity))
            payload = context.store.read_json('implementation.json')
            self._check_retained_result(context.store, payload, run, approval)
        workspace = code_task_paths(run).workspace_dir
        return ToolResult(tool_name='implement_code_task',
            status='ok' if payload['status'] == 'validated' else 'blocked',
            data={**payload, 'project_path': workspace.relative_to(self.root).as_posix()},
            artifacts=[(context.store.root / 'implementation.json').relative_to(self.root).as_posix()],
            summary='Saved CodeTask execution facts; inspect source and retained independent validation before delivery.',
            error='' if payload['status'] == 'validated' else payload['next_action'])

    def _prepare_candidate(self, context: Any, work: ExactCodeWork, approval: dict[str, Any],
                           run: Path, project: Path | None, task_text: str, *, for_preparation: bool = False) -> None:
        """Prepare a real isolated source once; code and measurement consume it."""
        from simple_ar.research.preparation import PreparationRequest, run_preparation_capability
        from simple_ar.experiment.execution.backend import RunRequest
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest, save_code_task_manifest, implementation_file_hashes, native_workspace_binding
        contract, binding, identity = self.exact, approval['binding'], context.attempt.attempt_id
        preparation = contract.preparation
        if contract.environment is not None:
            plan = next(iter(binding['environment_preparations'].values()))['environment_preparation'] if binding.get('environment_preparations') else None
            steps = len(plan['steps']) if plan else 0
            from simple_ar.tools.scientific import measurement_conditions
            conditions = measurement_conditions(self.measurement) if self.measurement is not None else []
            inputs = (self.measurement_inputs(self._preparation_origin(approval, identity=identity), False)
                if conditions and self.measurement_inputs and (work.prior_result or work.prepared_result) else {})
            completed = {row['condition_id'] for row in inputs.get('observations', [])}
            conditions = [row for row in conditions if row['id'] not in completed and not row.get('reuse_ref')]
            for key, required in (('process_invocations', steps + 1 + len(conditions)),
                    ('process_wall_seconds', (steps * plan['profile']['timeout_sec'] if plan else 0)
                        + contract.timeout_sec + sum(row['execution']['timeout_sec'] for row in conditions))):
                remaining = self.ledger.remaining(key)
                if remaining is None or remaining < required:
                    raise ValueError('Implementation preparation and validator require their complete finite process budget before setup')
        settings = ({'kind': 'existing_project', 'workspace_mode': 'copy', 'max_file_bytes': 0,
            'env_mode': 'external', 'python_executable': str(self.workspace.python), 'max_repairs': 1}
            if preparation is None else preparation.model_dump(exclude={'data_inputs'}))
        data_inputs = [] if preparation is None else preparation.data_inputs
        generation_source = None
        if work.prior_result or work.prepared_result:
            # Feedback starts from the accepted candidate, not a fresh
            # filtered import or committed baseline. Keep its actual env.
            settings.update(kind=preparation.kind if preparation is not None else 'existing_project', workspace_mode='copy',
                workspace_include=[], workspace_exclude=[], workspace_reuse_source_venv=False,
                env_mode='external', python_executable=binding['python_executable'])
            if work.prior_result and preparation is not None and preparation.kind == 'greenfield':
                prior = ArtifactStore(self.workspace.resolve(work.prior_result).parent)
                payload = prior.read_json('implementation.json')
                original = load_code_task_manifest(Path(payload['code_task_run_dir']))['benchmark']
                generation_source = {'documents': {name: prior.read_json(
                    payload['artifact_refs'][f'generation_{name}']['path']) for name in (
                        'task_contract', 'architecture_plan', 'code_artifacts', 'dependency_advice', 'implementation_plan')},
                    'result_schema': {key: original[key] for key in ('primary_metric', 'metric_directions') if key in original}}
        task = {**settings, 'code_root': str(project) if project else None,
            'allowed_patterns': contract.allowed_patterns, 'protected_patterns': binding['protected_patterns'],
            'approval_note': 'Explicit native approval: ' + approval['token'],
            'validation_command': contract.validation_command, 'validation_timeout_sec': contract.timeout_sec,
            **(contract.edit_policy or {})}
        execution = {'command': contract.validation_command, 'cwd': str(project),
            'timeout_sec': contract.timeout_sec, 'code_task': task}
        if self.measurement is not None:
            execution.update(self._formal_execution())
        root = project or self.workspace.workspace
        execution['cwd'] = str(root)
        origin = self.workspace.resolve(contract.project_path) if contract.project_path else root
        run_preparation_capability(context=context, request=PreparationRequest(
            execution, task_text, RunRequest(contract.validation_command, root, contract.timeout_sec),
            run_dir=run, source_project=origin if contract.project_path else None,
            data_paths=tuple(origin / name for name in data_inputs), generation_source=generation_source))
        manifest = load_code_task_manifest(run)
        manifest['code_task']['native_binding'] = approval
        initialized = native_workspace_binding(manifest, code_task_paths(run).workspace_dir)
        if preparation is not None and manifest['workspace']['selected_mode'] == 'git_worktree':
            frozen = binding['source']['workspace_input']['git']
            actual = manifest['workspace']
            if (actual['git']['origin_commit'] != frozen['commit']
                    or actual['project_relative_path'] != frozen['project_relative_path']):
                raise ValueError('Prepared Git commit or project directory differs from approval')
        expected = (binding['prior_source']['file_hashes'] if work.prepared_result else
            binding.get('prior_source', {}).get('file_hashes')
            or ArtifactStore(self.workspace.resolve(work.prior_result).parent).read_json(
                'implementation.json')['implementation_file_hashes']
            if work.prior_result or work.prepared_result else initialized['file_hashes'])
        if implementation_file_hashes(run) != expected:
            raise ValueError('Isolated project differs from the approved source snapshot; inspect copy omissions or source changes before any model call')
        from simple_ar.code_task.editing.scope import is_edit_allowed_path
        scope = manifest['edit_scope']
        manifest['code_task']['native_protocol'] = {'protected_assets': [
            {'asset_id': f'input_{i}', 'path': name}
            for i, name in enumerate(sorted(expected))
            if not is_edit_allowed_path(name, allowed_patterns=tuple(scope['allowed_patterns']),
                protected_patterns=tuple(scope['protected_patterns']))] + [
            {'asset_id': f'external_input_{i}', 'path': name}
            for i, name in enumerate(sorted(binding.get('external_inputs', {})))]}
        save_code_task_manifest(run, manifest)
        if binding.get('environment_preparations'):
            from simple_ar.research.project_environment import prepare_project_environment
            from simple_ar.experiment.execution.backend import LocalExecutionBackend
            view = binding['environment_preparations'][manifest['workspace']['selected_mode']]
            if binding != self.approval_binding(work.model_dump(), identity=identity, for_preparation=for_preparation):
                raise ValueError('Approved source or executable identity changed after copying; preparation was not started')
            payload = context.store.read_json('execution.json')
            configuration = {**payload['execution'], 'command': view['command'],
                'environment': view['environment_preparation']['profile']}
            for key in ('result_schema', 'protocol'):
                if view[key] is not None:
                    configuration[key] = view[key]
            prepared = prepare_project_environment(context=context, request=PreparationRequest(
                configuration, task_text, RunRequest(view['command'], code_task_paths(run).workspace_dir,
                    contract.timeout_sec, env=initialized['environment'], session_id=self.session_id, attempt_id=identity)),
                prepared_payload=payload, backend=LocalExecutionBackend(budget_ledger=self.ledger), record_runtime=True)
            if prepared.status != 'completed':
                raise ValueError('Candidate environment preparation failed; no implementation or measurement was started')
            if binding != self.approval_binding(work.model_dump(), identity=identity, for_preparation=for_preparation):
                raise ValueError('Approved source or preparation inputs changed during setup')
            current = implementation_file_hashes(run)
            if any(current.get(name) != digest for name, digest in expected.items()):
                raise ValueError('Environment setup changed imported project bytes; inspect the actual preparation before code work')
            workspace = code_task_paths(run).workspace_dir
            for name in contract.new_files:
                target = workspace / name
                if (target.exists() or target.is_symlink() or target.is_junction()
                        or any(part.is_symlink() or part.is_junction() or part.is_file()
                            for part in target.parents if part != workspace and workspace in part.parents)):
                    raise ValueError('Environment setup must not create required code targets or their link boundaries')
            self._environment_evidence(context.store, run, approval)
        manifest = load_code_task_manifest(run)
        if manifest['environment']['policy']['python_executable'] != binding['python_executable']:
            raise ValueError('Prepared interpreter differs from the explicitly approved environment')

    def _client(self, identity: str) -> LLMClient:
        if self.client_factory:
            return self.client_factory(identity)
        return LLMClient.for_task(client=LLMClient.from_env(self.model, budget_ledger=self.ledger,
            budget_session_id=self.session_id, budget_attempt_id=identity), purpose='code')

    def check_delivery(self, result: dict[str, Any], artifacts: list[str], *,
                       require_current_origin: bool = True) -> None:
        """Accept only the existing verified candidate, never a substitute output."""
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
        run = Path(result['code_task_run_dir']).resolve()
        if run.parent != (self.workspace.workspace / 'code_tasks').resolve():
            raise ValueError('Exact delivery does not belong to this task')
        binding = load_code_task_manifest(run)['code_task']['native_binding']
        store = ArtifactStore(self.root / 'attempts' / run.name)
        payload = store.read_json('implementation.json')
        if {key: value for key, value in result.items() if key != 'project_path'} != payload:
            raise ValueError('Exact delivery observation differs from its saved domain result')
        self._check_retained_result(store, payload, run, binding)
        if require_current_origin and binding['binding'] != self.approval_binding(binding['binding'].get('work', {}), identity=run.name):
            raise ValueError('Approved source or predecessor changed; inspect before completed delivery')
        if (self.exact is None or binding['binding']['contract'] != self.exact.model_dump()
                or payload.get('status') != 'validated'):
            raise ValueError('Exact candidate changed or is not independently validated; no completed delivery')
        candidate = code_task_paths(run).workspace_dir.resolve()
        if not any(self.workspace.resolve(path).is_relative_to(candidate) for path in artifacts):
            raise ValueError('Exact delivery must include the verified candidate, not only unrelated artifacts')

    def _check_retained_result(self, store: ArtifactStore, payload: dict[str, Any], run: Path,
                               approval: dict[str, Any]) -> None:
        from simple_ar.code_task.runtime.state import retained_implementation_inventory, required_new_file_issues, load_code_task_manifest, code_task_paths
        for row in payload['artifact_refs'].values():
            expected = payload.get('artifact_hashes', {}).get(row['path'])
            if expected != hashlib.sha256(store.resolve(row['path']).read_bytes()).hexdigest():
                raise ValueError('Retained implementation evidence changed; no replay')
        expected = payload.get('implementation_file_hashes')
        if (payload.get('native_binding') != approval or not isinstance(expected, dict)
                or approval['binding'].get('measurement_contract') != self.measurement):
            raise ValueError('Retained domain result does not match its approved execution and candidate')
        outputs = self.candidate_outputs(store.root / 'implementation.json') if self.candidate_outputs else {}
        retained_implementation_inventory(run, expected, outputs)
        self._environment_evidence(store, run, approval)
        missing = required_new_file_issues(load_code_task_manifest(run), self.workspace.resolve(payload['workspace_dir']))
        if missing:
            raise ValueError('Required new project files are absent/empty/linked: ' + ', '.join(missing))
        if (approval['binding']['contract'].get('new_files') or self.exact.environment is not None) and payload.get('status') == 'validated':
            from simple_ar.core.artifacts import read_json
            from simple_ar.code_task.execution.runner import native_validation_process
            paths = code_task_paths(run)
            report = read_json(paths.run_artifact_dir / 'patched' / 'execution_report.json')
            native_validation_process(run, budget_ledger=self.ledger, session_id=self.session_id,
                attempt_id=run.name, invocation_id=report['process']['invocation_id'], file_hashes=expected)

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
            retained_request = (owner.read_json('request.json').get('request') if owner.exists('request.json')
                else owner.read_json('selection-input.json').get('request') if owner.exists('selection-input.json') else None)
            if retained_request is not None and retained_request.get('material_roots', []) != request.material_roots:
                raise ValueError('Material root contract changed; preserve this identity without model dispatch')
            for locator in request.material_roots:
                material = self.workspace.resolve_material_root(locator)
                if material == output or material.is_relative_to(output) or output.is_relative_to(material):
                    raise ValueError('Material roots must be separate from the candidate output')
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
            material_selection = None
            if request.material_roots:
                from simple_ar.tools.materials import implementation_materials
                material_selection, client = implementation_materials(workspace=self.workspace,
                    roots=request.material_roots, goal=request.goal,
                    owner=ArtifactStore(owner.root / 'material-selection'), identity=identity,
                    client_factory=lambda: self._client(identity))
                windows.extend(material_selection['windows'])
            if not request.files:
                selection, client = select_implementation_files(goal=request.goal,
                    request_binding=request.model_dump(), project=project, owner=owner, windows=windows,
                    bind_inventory=lambda: self.workspace.bind_inventory(project, identity=identity),
                    resolve_source=self.workspace.resolve,
                    validate_destination=lambda path: self.workspace.resolve(f'{prefix}/{path}', write=True),
                    client_factory=lambda: client or self._client(identity),
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
                **({'selection': selection} if selection else {}),
                **({'material_selection': material_selection} if material_selection else {})}
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
            if material_selection:
                current, _ = implementation_materials(workspace=self.workspace,
                    roots=request.material_roots, goal=request.goal,
                    owner=ArtifactStore(owner.root / 'material-selection'), identity=identity,
                    client_factory=lambda: self._client(identity))
                if current != material_selection:
                    raise ValueError('Materials changed during generation; retain candidate without verification')
            if selection and project is not None and self.workspace.bind_inventory(project, identity=identity) != {
                    key: selection['input'][key] for key in ('project_path', 'file_hashes', 'inventory_scope')}:
                raise ValueError('Project changed during generation; candidate retained for inspection, use a new identity')
            data = {'data': {**result, 'source_binding': str(owner.root / 'request.json'),
                    **({'material_coverage': material_selection['coverage']} if material_selection else {}),
                    **({'selection_receipt': str(owner.root / 'selection.json'),
                        'coverage': selection['coverage']} if selection else {})},
                'artifacts': [f"{prefix}/{row['path']}" for row in result['files']],
                'summary': 'Candidate saved. Original sources unchanged; execution and scientific correctness remain unverified.'}
            if result.get('execution_plan') is not None:
                data['data']['prepared_result'] = f'records/{identity}/implementation/result.json'
                data['metadata'] = {'capability_artifacts': [ArtifactStore(owner.root.parent).ref(
                    'implementation/result.json', kind='prepared_implementation',
                    schema='prepared_implementation.v1').to_dict()]}
            owner.write_json('result.json', data)
            return ToolResult(tool_name=name, **data)
        except Exception as exc:
            return ToolResult(tool_name=name, status='error',
                error=f'Implementation unavailable ({type(exc).__name__}: {exc}); retained inputs/proposal/budget are not replayed automatically.')
