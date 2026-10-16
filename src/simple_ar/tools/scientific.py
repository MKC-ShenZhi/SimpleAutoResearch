"""Native scientific tools over the shared execution and analysis owners.

Requests and receipts belong to the controller's attempt. A request without a
receipt is intentionally not retryable, even if a process log looks finished.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import traceback
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from simple_ar.app.research_execution import execution_request, normalize_execution_config
from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, CapabilityContext
from simple_ar.core.process import verify_process_accounting
from simple_ar.experiment.execution.backend import LocalExecutionBackend
from simple_ar.experiment.execution.measurement import measurement_reuse_matches, snapshot_protocol_assets, representative_measurement_execution
from simple_ar.experiment.execution.outputs import metric_sources
from simple_ar.research.analysis import analyze_experiment_capability
from simple_ar.research.experiment import run_experiment_capability
from simple_ar.research.project_environment import environment_executable as _executable, environment_preparation_identity, verify_environment_preparation
from simple_ar.tools.specs import CommonToolSpec, ToolResult
from simple_ar.tools.workspace import DeliveryRequest


class MeasurementRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    command: list[str] = Field(min_length=1)
    cwd: str = 'workspace'
    timeout_sec: int = Field(gt=0)
    label: str = Field(default='experiment', min_length=1)
    result_schema: dict[str, Any]
    protocol: dict[str, Any] | None = None
    environment: dict[str, Any] | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Explicit preparation: mode current/venv; requirements project-relative files; install_project boolean; optional venv python_executable; timeout_sec; check_command argv. Approved steps share this attempt/budget before measurement. Installation executes project/build code, not an OS sandbox or scientific evidence.')
    condition_id: str | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Exact identity from a caller-frozen condition set; does not authorize changed argv, seed or protocol.')
    implementation_ref: str | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional verified records/<attempt>/implementation.json from this task. Uses its actual candidate interpreter/environment and retains the producing implementation and preparation; never a global latest-candidate hint.')
    preparation_ref: str | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional registered records/<attempt>/execution.json from prepare_code_task. Measures unchanged prepared source/environment, not validated implementation; mutually exclusive with implementation_ref.')
    workspace_outputs: list[str] = Field(default_factory=list, max_length=64, exclude_if=lambda value: not value,
        description='Explicit new candidate-relative data output files. Requires implementation_ref or preparation_ref and execution approval. Cannot overwrite source inputs or substitute dependencies. Prefer {output_dir}; outputs do not extend validation scope.')
    delivery: DeliveryRequest | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Explicit final measurement/interpretation delivery. Adds the actual capability outputs; failed or incomplete evidence forces disclosed partial delivery. Omit for ongoing research.')

    @field_validator('command')
    @classmethod
    def argv(cls, value: list[str]) -> list[str]:
        if any(not arg.strip() or '\x00' in arg for arg in value):
            raise ValueError('Command requires nonempty literal argv entries')
        return value

    @model_validator(mode='after')
    def output_paths(self) -> 'MeasurementRequest':
        if self.implementation_ref is not None and self.preparation_ref is not None:
            raise ValueError('Select implementation_ref or preparation_ref, not both')
        if self.environment is not None:
            from simple_ar.research.project_environment import environment_profile
            self.environment = environment_profile(self.environment)
            if self.implementation_ref is not None or self.preparation_ref is not None:
                raise ValueError('A producing source already owns its environment; do not replace it in measurement')
        if self.workspace_outputs and not (self.implementation_ref or self.preparation_ref):
            raise ValueError('Candidate outputs require an explicit producing source')
        if len(set(self.workspace_outputs)) != len(self.workspace_outputs) or any(
                not name or '\\' in name or ':' in name or Path(name).is_absolute()
                or any(part in {'', '.', '..'} for part in name.split('/')) for name in self.workspace_outputs):
            raise ValueError('Candidate outputs must be unique project-relative file paths')
        return self


class MeasurementPair(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    seed: int
    baseline_ref: str | None = None
    candidate_ref: str | None = None


class MeasurementAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    result_refs: list[str] = Field(default_factory=list,
        description='Legacy single candidate canonical result. For multiple measurements supply explicit pairs instead; no implicit pairing.')
    baseline_ref: str | None = None
    pairs: list[MeasurementPair] | None = Field(default=None, min_length=1,
        exclude_if=lambda value: value is None,
        description='Explicit unique integer seeds with optional baseline_ref/candidate_ref. Missing conditions remain coverage gaps. Every supplied measurement must declare exactly this seed and may appear only once.')
    question: str = ''
    context: str = Field(default='', description='Research prose only; cannot supply observed metrics, directions or comparison conditions.')
    delivery: DeliveryRequest | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def explicit_composition(self) -> 'MeasurementAnalysisRequest':
        if self.pairs is not None:
            if self.result_refs or self.baseline_ref is not None:
                raise ValueError('pairs and result_refs/baseline_ref are mutually exclusive')
            if len({pair.seed for pair in self.pairs}) != len(self.pairs):
                raise ValueError('Pair seeds must be unique; do not duplicate a seed condition')
        elif len(self.result_refs) != 1:
            raise ValueError('Supply one result_ref or explicit pairs for multiple measurements')
        return self


def scientific_tool_specs() -> list[CommonToolSpec]:
    return [
        CommonToolSpec(name='measure_experiment', domain='scientific',
            permission_level='execution', risk_level='high',
            input_schema=MeasurementRequest.model_json_schema(),
            description='Measure one explicitly approved argv with the shared finite process budget. Ordinary work uses the task workspace/interpreter; a verified producing candidate uses its actual environment, while a caller-frozen execution contract preserves its exact cwd/argv and inherited environment. Saves canonical results, logs, declared outputs, metric selectors, guard and observed integrity. Protocol fields are declarations, not verified facts. Optional delivery interprets the measured result locally and delivers actual outputs; no unrequested experiment or model call. Interrupted requests are blocked, never replayed.'),
        CommonToolSpec(name='analyze_measurements', domain='scientific',
            permission_level='write_patch', risk_level='low',
            input_schema=MeasurementAnalysisRequest.model_json_schema(),
            description='Deterministically analyze receipt-verified task measurements and caller-frozen, hash-bound reused baselines: one candidate with optional baseline, or explicit seed/baseline/candidate pairs including missing conditions. Imports retain original measurement identity and disclose their origin, without new process accounting. Existing collection analysis owns protocol grouping and descriptive paired summaries; no inferred pairing, new statistics, process or model call. Saves experiment_set.json for pairs, canonical analysis.json, analysis.md and comparison evidence. Optional delivery includes actual outputs and discloses incomplete/failed evidence; frozen condition coverage is independently enforced.'),
    ]


def measurement_contract(execution: dict[str, Any]) -> dict[str, Any]:
    """Validate an explicitly supplied execution boundary, not a task category."""
    if set(execution) == {'conditions'}:
        rows = execution['conditions']
        if not isinstance(rows, list) or not rows:
            raise ValueError('A condition set requires explicit nonempty conditions')
        seen = set()
        normalized = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) - {'id', 'role', 'seed', 'execution', 'source', 'before_implementation', 'reuse_ref', 'reuse_sha256'}
                    or not {'id', 'role', 'seed', 'execution'}.issubset(row)
                    or not isinstance(row['id'], str) or not row['id'].strip() or row['id'] in seen
                    or row['role'] not in {'baseline', 'candidate'}
                    or 'source' in row and row['source'] not in {'declared', 'preparation', 'implementation'}
                    or 'before_implementation' in row and (type(row['before_implementation']) is not bool
                        or row['before_implementation'] and row.get('source') != 'preparation')
                    or row['seed'] is not None and type(row['seed']) is not int
                    or not isinstance(row['execution'], dict) or 'conditions' in row['execution']):
                raise ValueError('Conditions require unique identities, explicit roles, integer/null seeds and flat execution')
            if 'reuse_ref' in row and (row['role'] != 'baseline'
                    or not isinstance(row['reuse_ref'], str) or not row['reuse_ref'].strip()
                    or row.get('source', 'declared') != 'declared' or row.get('before_implementation')):
                raise ValueError('Reuse requires a nonempty baseline locator with declared source')
            if 'reuse_sha256' in row and ('reuse_ref' not in row
                    or not isinstance(row['reuse_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', row['reuse_sha256'])):
                raise ValueError('Reuse digest requires a baseline locator and SHA256')
            seen.add(row['id'])
            flat = measurement_contract(row['execution'])
            seed = (flat.get('protocol') or {}).get('comparison_conditions', {}).get('seed')
            if row['seed'] is not None and (type(seed) is not int or seed != row['seed']):
                raise ValueError('Frozen condition seed must equal its execution protocol seed')
            normalized.append({**row, 'execution': flat})
        identities = [(row['role'], row['seed']) for row in normalized]
        if len(set(identities)) != len(identities):
            raise ValueError('Condition roles/seeds must be unique')
        if any(seed is None for _, seed in identities) and any(seed is not None for _, seed in identities):
            raise ValueError('Do not mix seeded pairs and unseeded comparisons')
        environments = [row['execution'].get('environment') for row in normalized]
        if any(value is not None for value in environments):
            first = normalized[0]['execution']
            if (any(value != environments[0] for value in environments)
                    or any(Path(row['execution']['cwd']) != Path(first['cwd'])
                        or row.get('source', 'declared') != 'declared' for row in normalized)):
                raise ValueError('Shared environment requires the same declared cwd, profile and source kind for every condition')
        representative_measurement_execution({'conditions': normalized})
        return {'conditions': normalized}
    allowed = {'command', 'cwd', 'timeout_sec', 'label', 'result_schema', 'protocol', 'environment'}
    if set(execution) - allowed:
        raise ValueError('A fixed measurement contract cannot discard execution controls')
    parsed = MeasurementRequest.model_validate(execution)
    cwd = Path(parsed.cwd)
    if not cwd.is_absolute() or not cwd.is_dir():
        raise ValueError('Fixed execution cwd must be an existing absolute directory')
    if parsed.environment is not None:
        from simple_ar.research.project_environment import project_environment_plan
        project_environment_plan(parsed.environment, cwd, Path('__attempt__') / 'environment', parsed.command)
    execution_request(parsed.model_dump(exclude={'implementation_ref', 'preparation_ref', 'delivery'}))
    return parsed.model_dump(exclude={'implementation_ref', 'preparation_ref', 'delivery', 'workspace_outputs', 'condition_id'})


def measurement_conditions(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return contract['conditions'] if 'conditions' in contract else [
        {'id': 'candidate', 'role': 'candidate', 'seed': None, 'execution': contract}]


def freeze_measurement_set(execution: dict[str, Any]) -> dict[str, Any]:
    """Expand caller-declared conditions through the existing sole argv owner."""
    if set(execution) - {'command', 'cwd', 'timeout_sec', 'label', 'result_schema', 'protocol',
            'baseline', 'baseline_policy', 'baseline_ref', 'pairs', 'seeds', 'seed_count', 'seed_flag',
            'protocol_seed_reason', 'protocol_seed_flag', 'environment'}:
        raise ValueError('Condition-set migration cannot discard unsupported preparation/reuse controls')
    config = normalize_execution_config(execution)
    rows = []
    pairs = config.get('pairs') or [None]
    reuse = config['baseline_policy'] == 'reuse'
    references = config.get('baseline_ref')
    if reuse:
        references = [references] * len(pairs) if isinstance(references, str) else references
        if isinstance(references, tuple):
            references = list(references)
        if (not isinstance(references, list) or len(references) != len(pairs)
                or any(not isinstance(ref, str) or not ref.strip() for ref in references)):
            raise ValueError('Reuse requires a baseline_ref or one nonempty locator per baseline condition')
    elif references is not None:
        raise ValueError('baseline_ref requires baseline_policy=reuse')
    for index, pair in enumerate(pairs):
        for role in (('candidate',) if config['baseline_policy'] == 'skip' else ('baseline', 'candidate')):
            request = execution_request(config, condition=role, pair_index=index if pair else None)
            flat = {'command': list(request.run.command), 'cwd': str(request.run.cwd),
                'timeout_sec': request.run.timeout_sec, 'label': request.run.label,
                'result_schema': request.result_schema,
                'protocol': request.experiment_contract.to_row() if request.experiment_contract else None,
                **({'environment': config['environment']} if config.get('environment') is not None else {})}
            rows.append({'id': f'{role}:{index}', 'role': role, 'seed': pair['seed'] if pair else None,
                'execution': flat, **({'reuse_ref': references[index]} if reuse and role == 'baseline' else {})})
    return measurement_contract({'conditions': rows})


def fixed_measurement_issues(declaration: dict[str, Any], accepted: dict[str, Any]) -> list[str]:
    """Match physical slots and caller-explicit protocol fields, not added design prose."""
    declared = (freeze_measurement_set(declaration) if any(key in declaration for key in
        ('baseline', 'pairs', 'seeds', 'seed_count', 'baseline_policy')) else measurement_contract(declaration))
    expected, actual = measurement_conditions(declared), measurement_conditions(measurement_contract(accepted))
    if len(expected) != len(actual):
        return ['Accepted design differs from the fixed execution condition set.']
    for index, (left, right) in enumerate(zip(expected, actual)):
        if ({key: value for key, value in left.items() if key not in {'execution', 'source', 'before_implementation'}}
                != {key: value for key, value in right.items() if key not in {'execution', 'source', 'before_implementation'}}
                or {key: value for key, value in left['execution'].items() if key != 'protocol'}
                != {key: value for key, value in right['execution'].items() if key != 'protocol'}):
            return ['Accepted design differs from the fixed physical execution contract.']
        explicit = (declaration['conditions'][index]['execution'] if 'conditions' in declaration else
            declaration.get('baseline', {}) if left['role'] == 'baseline' else declaration)
        protocol = explicit.get('protocol', declaration.get('protocol')) or {}
        if any((left['execution'].get('protocol') or {}).get(key)
                != (right['execution'].get('protocol') or {}).get(key) for key in protocol):
            return ['Accepted design differs from an explicit fixed execution protocol field.']
    return []


def validate_reused_baseline(canonical: dict[str, Any], execution: dict[str, Any]) -> None:
    """Validate imported domain facts without claiming this task's process ownership."""
    try:
        measurement = canonical.get('measurement') or {}
        physical = measurement.get('measurement_id')
        valid = (measurement.get('source_kind') == 'measured' and isinstance(physical, str)
            and re.fullmatch(r'[0-9a-f]{32}', physical)
            and canonical.get('implementation_ref') is None and canonical.get('preparation') is None
            and not ScientificToolGateway._measurement_issues(canonical)
            and measurement_reuse_matches(canonical, execution_request(execution), same_session_result=False))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        valid = False
    if not valid:
        raise ValueError('Reused baseline is not a passed same-condition measurement with unchanged protected assets '
            'and an original physical identity without producing-source lineage')


def _hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()




def _reject_links(path: Path) -> None:
    # Authorization and boundary observations, not an OS process sandbox.
    for component in (path, *path.parents):
        if component.is_symlink() or component.is_junction():
            raise ValueError('Protected assets cannot traverse symlinks or junctions')


class ScientificToolGateway:
    context_tools = {'measure_experiment', 'analyze_measurements'}

    def __init__(self, root: Path, workspace: Any, ledger: Any, session_id: str,
                 *, implementation: Any | None = None,
                 execution_contract: dict[str, Any] | None = None,
                 research_contract: Any | None = None,
                 research_requirements: Any | None = None) -> None:
        self.root = Path(root).resolve()
        if workspace.root != self.root or workspace.ledger is not ledger or not session_id:
            raise ValueError('Scientific tools require the task workspace, shared ledger and session identity')
        self.workspace, self.ledger, self.session_id = workspace, ledger, session_id
        self.implementation = implementation
        self._declared_execution_contract = measurement_contract(execution_contract) if execution_contract is not None else None
        self._research_contract = research_contract
        self._research_requirements = research_requirements

    def execution_issues(self) -> list[str]:
        issues = self._research_requirements() if self._research_requirements is not None else []
        if issues:
            return issues
        if self._declared_execution_contract is not None and self._research_contract is not None:
            try:
                accepted = self._research_contract()
                if accepted is not None:
                    differences = fixed_measurement_issues(self._declared_execution_contract, accepted)
                    if differences:
                        return ['Accepted research design differs from the fixed scientific execution contract. '
                            + issue for issue in differences]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return ['Accepted research design differs from the fixed scientific execution contract. ' + str(exc)]
        contract = self.execution_contract
        if contract is not None and any(row.get('reuse_ref') and not row.get('reuse_sha256')
                for row in measurement_conditions(contract)):
            return ['Baseline reuse requires a hash-bound task snapshot before any new execution.']
        return []

    @property
    def execution_contract(self) -> dict[str, Any] | None:
        if self._declared_execution_contract is not None:
            return self._declared_execution_contract
        return self._research_contract() if self._research_contract is not None else None

    def _store(self, identity: str) -> ArtifactStore:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
            raise ValueError('Invalid scientific attempt identity')
        path = self.root / 'attempts' / identity
        if path.resolve() != path.absolute():
            raise ValueError('Scientific attempt cannot traverse symlinks')
        return ArtifactStore(path)

    def _locator(self, path: Path) -> str:
        locator = self.workspace.locator(str(path))
        if not locator.startswith(('records/', 'evidence/reused-baselines/')):
            raise ValueError('Task attempts must be exposed through the readonly records mount')
        return locator

    def _condition(self, parsed: MeasurementRequest, *, allow_reused: bool = False) -> dict[str, Any]:
        """Bind a proposal to its explicit source kind, independently of comparison role."""
        scoped = self.implementation is not None and self.implementation.exact is not None
        contract = self.execution_contract
        if contract is None:
            kind = ('preparation' if parsed.preparation_ref is not None else
                    'implementation' if parsed.implementation_ref is not None or scoped else 'declared')
            row = {'source': kind, 'execution': {}}
        elif 'conditions' in contract:
            row = next((row for row in contract['conditions'] if row['id'] == parsed.condition_id), None)
            if row is None or scoped and 'source' not in row:
                raise ValueError('Select an exact frozen condition with explicit source for a scoped task')
            row = {**row, 'source': row.get('source', 'declared')}
        else:
            row = {'source': 'implementation' if scoped else 'declared', 'execution': contract}
        if (parsed.condition_id is not None and (contract is None or 'conditions' not in contract)):
            raise ValueError('Condition identity requires an explicitly frozen condition set')
        if row.get('reuse_ref') and not allow_reused:
            raise ValueError('Reused baseline conditions cannot execute a new measurement')
        kind = row['source']
        if (kind == 'declared' and (parsed.implementation_ref is not None or parsed.preparation_ref is not None)
                or kind == 'implementation' and parsed.implementation_ref is None
                or kind == 'preparation' and parsed.preparation_ref is None):
            raise ValueError('Measurement source reference differs from the frozen source kind')
        if contract is not None:
            expected = dict(row['execution'])
            if 'conditions' in contract:
                expected['condition_id'] = row['id']
            if kind != 'declared':
                expected['cwd'] = parsed.cwd  # Actual owner must verify it below.
            if parsed.model_dump(exclude={'implementation_ref', 'preparation_ref', 'delivery', 'workspace_outputs'}) != expected:
                raise ValueError('Measurement proposal differs from the explicitly authorized execution contract')
        return row

    def _source(self, parsed: MeasurementRequest, identity: str) -> dict[str, Any] | None:
        if parsed.implementation_ref is None and parsed.preparation_ref is None:
            return None
        if self.implementation is None or not identity:
            raise ValueError('Measurement lineage requires its actual source owner and consuming attempt')
        return (self.implementation.prepared_source(parsed.preparation_ref, identity=identity)
            if parsed.preparation_ref is not None else
            self.implementation.measurement_source(parsed.implementation_ref, identity=identity))

    def _source_run(self, source: dict[str, Any]) -> Path:
        ref = source.get('implementation_ref') or source['prepared_ref']
        return self.workspace.workspace / 'code_tasks' / Path(ref['path']).parent.name

    def _shared_environment(self, identity: str) -> tuple[ArtifactStore, dict, dict] | None:
        """Find the sole durable preparation owner; an unfinished owner blocks reuse."""
        if 'conditions' not in (self.execution_contract or {}) or not any(
                row['execution'].get('environment') for row in self.execution_contract['conditions']):
            return None
        owners = []
        for path in sorted((self.root / 'attempts').glob('*/scientific_request.json')):
            if path.parent.name == identity:
                continue
            store = self._store(path.parent.name)
            saved = store.read_json('scientific_request.json')
            binding = saved.get('execution_approval', {}).get('binding', {})
            if saved.get('tool_name') != 'measure_experiment' or not binding.get('environment_preparation'):
                continue
            self._condition(MeasurementRequest.model_validate(saved['arguments']))
            owners.append((store, binding))
        if len(owners) > 1:
            raise ValueError('Shared environment has multiple preparation owners; no execution or replay')
        if not owners:
            return None
        store, binding = owners[0]
        if not store.exists('scientific_result.json'):
            raise ValueError('Shared environment owner is unfinished or interrupted; no execution or replay')
        receipt = self._receipt(store.root.name, 'measure_experiment')
        executable = _executable(binding['argv'], Path(binding['cwd']), None)
        if _hash(executable) != store.read_json('environment_observation.json')['executable_sha256']:
            raise ValueError('Shared environment interpreter changed after preparation')
        return store, binding, receipt

    def _shared_environment_inputs(self) -> dict[str, Any]:
        """Freeze all formal source/asset identities before any setup code runs."""
        rows = measurement_conditions(self.execution_contract)
        sources, assets, executables = {}, {}, {}
        for row in rows:
            execution = row['execution']
            cwd = Path(execution['cwd'])
            for argument in execution['command'][1:]:
                value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
                path = Path(value)
                path = path if path.is_absolute() else cwd / path
                if '{output_dir}' not in value and path.is_file():
                    _reject_links(path)
                    sources[str(path)] = _hash(path)
            for asset in (execution.get('protocol') or {}).get('protected_assets', []):
                path = Path(asset['path'])
                _reject_links(path if path.is_absolute() else cwd / path)
            assets[row['id']] = snapshot_protocol_assets(execution.get('protocol'), cwd)
            if execution['environment']['mode'] == 'current':
                executable = _executable(execution['command'], cwd, None)
                executables[str(executable)] = _hash(executable)
        return {'source_hashes': sources, 'protected_assets': assets, 'executables': executables}

    def _request(self, arguments: dict[str, Any], *, identity: str = ''):
        if issues := self.execution_issues():
            raise ValueError('; '.join(issues))
        parsed = MeasurementRequest.model_validate(arguments)
        fixed = self.execution_contract is not None
        scoped = self.implementation is not None and self.implementation.exact is not None
        if fixed and scoped and self.implementation.measurement != self.execution_contract:
            raise ValueError('Scientific execution differs from the producing implementation approval contract')
        condition = self._condition(parsed)
        source = self._source(parsed, identity)
        if source is not None:
            from simple_ar.code_task.runtime.state import implementation_file_hashes
            run = self._source_run(source)
            if implementation_file_hashes(run) != source['file_hashes']:
                raise ValueError('Candidate contains derived files outside the original validator scope; retain/deliver them, then explicitly revalidate a feedback candidate before another measurement')
        is_declared_source = condition['source'] == 'declared'
        cwd = Path(parsed.cwd) if fixed and is_declared_source else self.workspace.resolve(parsed.cwd, write=True)
        if not cwd.is_dir():
            raise ValueError('Measurement cwd must be an existing writable workspace directory')
        if source is not None and cwd != Path(source['cwd']):
            raise ValueError('Measurement cwd must equal its verified producing candidate')
        metric_sources(parsed.result_schema)  # Validate output declarations before any launch.
        config = parsed.model_dump(exclude={'implementation_ref', 'preparation_ref', 'delivery', 'workspace_outputs', 'condition_id', 'environment'})
        config['cwd'] = str(cwd)
        isolated = parsed.environment is not None and parsed.environment['mode'] == 'venv'
        config['command'] = (list(parsed.command) if isolated or fixed and is_declared_source else [source['python_executable'], *parsed.command[1:]]
            if source is not None and parsed.command[0] in {'python', 'python3'}
            else self.workspace.process_argv(parsed.command))
        origin = (Path(condition['execution']['cwd']) if fixed and source is not None
            and ('conditions' in self.execution_contract or not scoped or self.implementation.exact.project_path) else None)
        if origin is not None:
            from simple_ar.code_task.execution.environment import project_command
            config['command'] = project_command(config['command'], origin, cwd,
                python_executable=source['python_executable'])
        environment = source['environment'] if source is not None else None if fixed else self.workspace.process_environment()
        if parsed.environment is not None:
            from simple_ar.research.project_environment import project_environment_plan, project_environment_process_env
            _reject_links(cwd)
            shared = self._shared_environment(identity)
            owner = shared[0] if shared else self._store(identity)
            plan = project_environment_plan(parsed.environment, cwd, owner.root / 'environment', config['command'])
            config['command'] = plan['command']
            environment = project_environment_process_env(plan['profile'], environment)
        executable = None if isolated else _executable(config['command'], cwd, environment)
        profile = getattr(self.implementation.exact, 'environment', None) if scoped else None
        if (source is not None and profile is not None and profile['mode'] == 'venv'
                and Path(executable).absolute() != Path(source['python_executable']).absolute()):
            raise ValueError('Explicit measurement executable conflicts with the prepared venv; it was not silently replaced')
        if (not fixed or not is_declared_source) and not isolated:
            config['command'][0] = str(Path(executable).absolute())
        # A command is not an execution identity. Validator and measurement
        # remain separate physical invocations, attempts and ledger entries;
        # _verify_measurement_process cannot accept a validator's receipt here.
        protocol = config.get('protocol')
        if source is not None:
            protocol = dict(protocol or {})
            declared_assets = [dict(row) for row in protocol.get('protected_assets', [])]
            by_id = {row['asset_id']: row for row in declared_assets}
            for row in source['protected_assets']:
                if row['asset_id'] in by_id and {k: v for k, v in by_id[row['asset_id']].items() if k != 'path'} != {k: v for k, v in row.items() if k != 'path'}:
                    raise ValueError('Declared protocol conflicts with producing-candidate protection')
                if row['asset_id'] not in by_id:
                    declared_assets.append(dict(row))
                    by_id[row['asset_id']] = declared_assets[-1]
            protocol['protected_assets'] = declared_assets
            config['protocol'] = protocol
        if protocol is not None:
            # Do not let an absolute path, traversal or mount alias bypass the
            # same authorization used by workspace readers and writers.
            assets = protocol.get('protected_assets', [])
            if not isinstance(assets, list):
                raise ValueError('protected_assets must be a list')
            for asset in assets:
                if not isinstance(asset, dict) or not isinstance(asset.get('path'), str):
                    raise ValueError('Each protected asset requires a path')
                supplied = asset['path']
                if origin is not None:
                    supplied = project_command([supplied], origin, cwd)[0]
                alias = supplied.split('/')[0]
                named = (Path(supplied) if Path(supplied).is_absolute() else cwd / supplied if fixed and is_declared_source else
                    self.workspace.mounts[alias].joinpath(*supplied.split('/')[1:])
                    if alias in self.workspace.mounts else cwd / supplied)
                _reject_links(named)
                path = (named.absolute() if fixed and is_declared_source else self.workspace.resolve(supplied) if Path(supplied).is_absolute()
                        or alias in self.workspace.mounts else self.workspace.resolve(str(cwd / supplied)))
                asset['path'] = str(path)
            if source is not None and any(by_id[row['asset_id']] != row for row in source['protected_assets']):
                raise ValueError('Declared protocol conflicts with producing-source protection')
        request = execution_request(config)
        return parsed, replace(request, run=replace(request.run, env=environment)), source

    def approval_binding(self, arguments: dict[str, Any], *, identity: str) -> dict[str, Any]:
        store = self._store(identity)
        parsed, request, source = self._request(arguments, identity=identity)
        plan = None
        shared = None
        if parsed.environment is not None:
            from simple_ar.research.project_environment import project_environment_plan
            shared = self._shared_environment(identity)
            plan = project_environment_plan(parsed.environment, request.run.cwd,
                (shared[0] if shared else store).root / 'environment',
                parsed.command if parsed.environment['mode'] == 'venv' else request.run.command)
        sources = {}
        commands = [request.run.command, *(plan['steps'] if plan else [])]
        for index, argument in [(i, arg) for argv in commands for i, arg in enumerate(argv)]:
            if index == 0:
                continue  # The executable is separately bound, including its bytes.
            value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
            if '{output_dir}' in value:
                continue
            candidate = Path(value)
            candidate = candidate if candidate.is_absolute() else request.run.cwd / candidate
            if candidate.is_file():
                authorized = candidate.absolute() if self.execution_contract is not None and source is None else self.workspace.resolve(str(candidate))
                sources[str(authorized)] = _hash(authorized)
        if plan and plan['profile']['install_project']:
            sources.update({str(path): _hash(path) for name in ('pyproject.toml', 'setup.py', 'setup.cfg')
                if (path := request.run.cwd / name).is_file()})
        protocol = request.normalized_experiment_contract()
        binding = {'tool_name': 'measure_experiment', 'arguments': parsed.model_dump(),
            'session_id': self.session_id, 'attempt_id': identity,
            'argv': request.run.command, 'cwd': str(request.run.cwd),
            'timeout_sec': request.run.timeout_sec, 'label': request.run.label,
            'interpreter': plan['python_executable'] if plan and plan['python_executable'] else
                str(_executable(request.run.command, request.run.cwd, request.run.env)),
            'executable_sha256': None if plan and plan['profile']['mode'] == 'venv' else _hash(_executable(request.run.command, request.run.cwd, request.run.env)),
            'environment': request.run.env if request.run.env is not None and plan is None else {
                'policy': 'inherit' if request.run.env is None else 'caller_supplied',
                'sha256': hashlib.sha256(json.dumps(dict(os.environ) if request.run.env is None else request.run.env, sort_keys=True).encode()).hexdigest()},
            'source_hashes': sources,
            'protected_assets': snapshot_protocol_assets(protocol, request.run.cwd),
            'result_schema': dict(request.result_schema), 'protocol': protocol,
            'output_binding': {'process_root': str(store.root / 'execution' / 'process'),
                'rule': 'LocalExecutionBackend invocation_id/outputs replaces {output_dir} in argv and SIMPLE_AR_OUTPUT_DIR in env; actual values retained in invocation.json'},
            'limits': {key: self.ledger.limits.get(key) for key in
                ('process_invocations', 'process_wall_seconds')}}
        if plan is not None:
            if 'conditions' in (self.execution_contract or {}):
                binding['shared_environment_inputs'] = self._shared_environment_inputs()
            if shared:
                owner, original, receipt = shared
                if (binding['shared_environment_inputs'] != original.get('shared_environment_inputs')
                        or binding['environment'] != original['environment']
                        or any(not Path(path).is_file() or _hash(Path(path)) != digest
                            for path, digest in original['source_hashes'].items())
                        or {key: value for key, value in environment_preparation_identity(
                            plan, request.run.cwd, request.run.env).items() if key != 'command'} !=
                            {key: value for key, value in original['environment_preparation'].items()
                                if key not in {'command', 'prepared_ref'}}):
                    raise ValueError('Shared environment source, preparation inputs or inherited environment changed')
                binding['shared_environment'] = {'attempt_id': owner.root.name,
                    'receipt_sha256': _hash(owner.root / 'scientific_result.json'),
                    'prepared_ref': original['environment_preparation']['prepared_ref']}
                binding['interpreter'] = str(_executable(request.run.command, request.run.cwd, request.run.env))
                binding['executable_sha256'] = _hash(Path(binding['interpreter']))
            else:
                binding['environment_preparation'] = {**environment_preparation_identity(plan, request.run.cwd, request.run.env),
                    'prepared_ref': ArtifactRef((store.root / 'execution.json').relative_to(self.root).as_posix(),
                        kind='prepared_execution', schema='prepared_execution.v1', producer='research.preparation').to_dict()}
        if source is not None:
            binding['source'] = source
            from simple_ar.code_task.runtime.state import implementation_file_hashes, implementation_output_inventory
            run = self._source_run(source)
            inventory = implementation_file_hashes(run)
            implementation_output_inventory(inventory, dict.fromkeys(parsed.workspace_outputs, ''))
            for name in parsed.workspace_outputs:
                path = Path(source['cwd']) / name
                _reject_links(path)
                self.workspace.resolve(str(path), write=True)
                if path.exists():
                    raise ValueError('Candidate output must be a new file; retain prior outputs and use a new output path')
            binding['candidate_inventory'] = inventory
        if parsed.delivery is not None:
            retained = self.workspace.resolve_delivery(parsed.delivery)
            binding['delivery_inputs'] = self.workspace.artifact_hashes(retained['artifacts'])
        return binding

    def has_started(self, identity: str) -> bool:
        try:
            store = self._store(identity)
            return (store.exists('scientific_request.json') or store.exists('execution')
                or any(entry.attempt_id == identity and entry.session_id == self.session_id
                    and entry.purpose == 'local_process' for entry in self.ledger.entries))
        except ValueError:
            return False

    def _receipt(self, identity: str, name: str, *, verify_source: bool = True) -> dict[str, Any]:
        store = self._store(identity)
        receipt = store.read_json('scientific_result.json')
        if (receipt['tool_name'] != name or receipt['session_id'] != self.session_id
                or receipt['attempt_id'] != identity):
            raise ValueError('Receipt belongs to a different tool, task or attempt')
        hashes = receipt['hashes']
        if not hashes or self.workspace.artifact_hashes(hashes) != hashes:
            raise ValueError('Scientific request or retained artifact hashes changed; no replay')
        if self._locator(store.root / 'scientific_request.json') not in hashes:
            raise ValueError('Receipt lacks its durable request binding')
        envelope = store.root / 'scientific_tool_result.json'
        if self._locator(envelope) not in hashes or store.read_json(envelope) != receipt['result']:
            raise ValueError('Receipt result differs from its hash-bound tool result')
        result = ToolResult.model_validate(receipt['result'])
        if result.tool_name != name or result.status != 'ok':
            raise ValueError('Receipt has no completed tool observation')
        request = store.read_json('scientific_request.json')
        if request['tool_name'] != name or request['session_id'] != self.session_id:
            raise ValueError('Request disagrees with receipt')
        if name == 'measure_experiment':
            approval = request.get('execution_approval', request.get('implementation_approval'))
            if not approval or not approval.get('token') or not approval.get('binding'):
                raise ValueError('Measurement has no saved execution approval')
            self._verify_execution(store, request, hashes, verify_source=verify_source)
        elif name == 'analyze_measurements':
            for locator, input_hashes in request['inputs'].items():
                self._measurement_input(locator)
                if self.workspace.artifact_hashes(input_hashes) != input_hashes:
                    raise ValueError('Retained analysis inputs changed')
        return receipt

    def _verify_execution(self, store: ArtifactStore, request: dict, hashes: dict, *, verify_source: bool = True) -> None:
        approval = request.get('execution_approval', request.get('implementation_approval'))
        binding = approval['binding']
        identity = store.root.name
        if (binding['tool_name'] != 'measure_experiment' or binding['arguments'] != request['arguments']
                or binding['session_id'] != self.session_id or binding['attempt_id'] != identity):
            raise ValueError('Approved execution identity disagrees with the saved request')
        canonical = store.read_json('results.json')
        source = binding.get('source')
        implementation = source.get('implementation_ref') if source is not None else None
        environment = binding.get('environment_preparation')
        shared = binding.get('shared_environment')
        preparation = (source.get('prepared_ref') if source is not None else
            (environment or shared)['prepared_ref'] if environment or shared else None)
        preparation_invocations = self._environment_evidence(store, binding, hashes=hashes) if environment else set()
        if shared:
            owner = self._store(shared['attempt_id'])
            if owner.root == store.root or _hash(owner.root / 'scientific_result.json') != shared['receipt_sha256']:
                raise ValueError('Shared environment preparation receipt changed')
            receipt = self._receipt(owner.root.name, 'measure_experiment')
            original = owner.read_json('scientific_request.json')['execution_approval']['binding']
            if (not original.get('environment_preparation')
                    or shared['prepared_ref'] != original['environment_preparation']['prepared_ref']
                    or binding.get('shared_environment_inputs') != original.get('shared_environment_inputs')
                    or any(hashes.get(path) != digest for path, digest in receipt['hashes'].items())
                    or hashes.get(self._locator(owner.root / 'scientific_result.json')) != shared['receipt_sha256']):
                raise ValueError('Measurement lost its shared preparation identity or evidence')
            preparation_invocations = self._environment_evidence(owner, original, hashes=hashes)
        if (canonical.get('implementation_ref') != implementation
                or (canonical.get('preparation') or {}).get('source_ref') != preparation):
            raise ValueError('Measurement disagrees with its approved producing-source lineage')
        if source is not None:
            expected = source['evidence_hashes']
            if (not set(expected).issubset(hashes)
                    or self.workspace.artifact_hashes(expected) != expected
                    or any(hashes[path] != value for path, value in expected.items())):
                raise ValueError('Producing implementation or validation evidence changed')
            if verify_source and self._source(MeasurementRequest.model_validate(request['arguments']), identity) != source:
                raise ValueError('Producing source/validation changed; retain the observation, do not replay')
        invocation = canonical['measurement']['measurement_id']
        if (not isinstance(invocation, str) or not re.fullmatch(r'[0-9a-f]{32}', invocation)
                or invocation in preparation_invocations):
            raise ValueError('Invalid physical invocation identity')
        process_path = store.root / 'execution' / 'process' / invocation / 'invocation.json'
        for path in (process_path, store.root / 'results.json'):
            if self._locator(path) not in hashes:
                raise ValueError('Receipt omits canonical or finalized invocation evidence')
        process = store.read_json(process_path)
        outputs = str(process_path.parent / 'outputs')
        expected_argv = [arg.replace('{output_dir}', outputs) for arg in binding['argv']]
        if (canonical['execution']['process'] != process or process['invocation_id'] != invocation
                or process['status'] not in {'finished', 'stopped'} or process['argv'] != expected_argv
                or canonical['command'] != expected_argv or process['cwd'] != binding['cwd']
                or canonical['execution']['cwd'] != binding['cwd']
                or process['timeout_sec'] != binding['timeout_sec']
                or process['session_id'] != self.session_id or process['attempt_id'] != identity
                or process['environment_policy'] != 'caller_supplied'
                or canonical['execution']['label'] != binding['label']
                or canonical.get('experiment_contract') != binding['protocol']
                or canonical['result_schema'] != binding['result_schema']):
            raise ValueError('Canonical, finalized invocation and approved execution disagree')
        timed_out = process.get('stop_reason') == 'timeout'
        if (canonical['timed_out'] != timed_out
                or canonical['returncode'] != (None if timed_out else process['returncode'])
                or canonical['execution_status'] != ('timed_out' if timed_out else
                    'passed' if process['returncode'] == 0 else 'failed')):
            raise ValueError('Canonical process outcome differs from finalized invocation')
        duration = process['duration_sec']
        if canonical['execution']['duration_sec'] != duration:
            raise ValueError('Invalid physical process wall-clock observation')
        verify_process_accounting(process, self.ledger, session_id=self.session_id,
            attempt_id=identity, timeout_sec=binding['timeout_sec'])
        if environment and 'shared_environment_inputs' in binding:
            expected = preparation_invocations | {invocation}
            entries = [entry for entry in self.ledger.entries
                if entry.attempt_id == identity and entry.purpose == 'local_process']
            if len(entries) != len(expected) or {entry.logical_call_id for entry in entries} != expected:
                raise ValueError('Shared preparation has an unaccounted or unknown physical process')

    def _environment_evidence(self, store: ArtifactStore, binding: dict, *, hashes: dict | None = None) -> set[str]:
        invocations, required = verify_environment_preparation(store, binding, ledger=self.ledger,
            session_id=self.session_id, attempt_id=store.root.name)
        if hashes is not None and any(self._locator(path) not in hashes for path in required):
            raise ValueError('Preparation evidence is missing from the retained measurement receipt')
        return invocations

    def candidate_outputs(self, record: Path) -> dict[str, str]:
        """Project outputs from intact physical receipts, not a second inventory store.

        Source verification is deliberately not recursive: the implementation
        owner compares its original inventory plus these exact output bytes.
        All request/evidence hashes and physical ledger facts still apply.
        """
        outputs = {}
        relative = record.relative_to(self.root).as_posix()
        for path in sorted((self.root / 'attempts').glob('*/scientific_result.json')):
            store = ArtifactStore(path.parent)
            saved = store.read_json('scientific_request.json')
            source = saved.get('execution_approval', {}).get('binding', {}).get('source')
            source_ref = (source.get('implementation_ref') or source.get('prepared_ref')) if source else None
            if source_ref is None or source_ref['path'] != relative:
                continue
            receipt = self._receipt(path.parent.name, 'measure_experiment', verify_source=False)
            produced = receipt['result']['data'].get('workspace_outputs', {})
            if set(produced) - set(saved['arguments'].get('workspace_outputs', [])):
                raise ValueError('Measurement claims candidate outputs outside its approval')
            for name, digest in produced.items():
                locator = self.workspace.locator(str(Path(source['cwd']) / name))
                if receipt['hashes'].get(locator) != digest or name in outputs:
                    raise ValueError('Derived output has conflicting or unbound provenance')
                outputs[name] = digest
        return outputs

    def implementation_inputs(self, locators: list[str], origin: dict[str, str] | None,
                              *, required: bool = True) -> dict[str, Any]:
        """Prove explicit pre-implementation conditions on the unchanged source."""
        conditions = {row['id'] for row in measurement_conditions(self.execution_contract)
            if (row.get('before_implementation') if required else row.get('source') == 'preparation')}
        selected, problems = {}, {}
        if origin is None:
            if required:
                raise ValueError('Required prior measurements need a registered preparation ancestor')
            return {'observations': [], 'evidence_sha256': {}}
        for locator in dict.fromkeys(locators):
            request = self._measurement_request(locator)
            parsed = MeasurementRequest.model_validate(request['arguments'])
            if parsed.condition_id not in conditions:
                continue
            self._condition(parsed)
            source = request.get('execution_approval', {}).get('binding', {}).get('source')
            if not source or source.get('implementation_ref') or source.get('preparation_origin') != origin:
                continue
            ref, receipt = self._measurement_input(locator)
            canonical = ArtifactStore(self.root).read_json(ref)
            issues = self._measurement_issues(canonical, receipt['result']['data'])
            if issues:
                problems[parsed.condition_id] = '; '.join(issues)
            else:
                # A later probe does not rewrite the evidence on which an
                # already approved implementation was based. Task delivery
                # separately checks current observations and all failures.
                selected.setdefault(parsed.condition_id, (ref, receipt))
        if required and (missing := conditions - selected.keys()):
            raise ValueError('Complete the required prepared-source measurements before implementation: ' + '; '.join(
                name + (': ' + problems[name] if name in problems else ': missing') for name in sorted(missing)))
        observations, hashes = [], {}
        for identity, (ref, receipt) in selected.items():
            canonical = ArtifactStore(self.root).read_json(ref)
            observations.append({'condition_id': identity, 'result_ref': ref.to_dict(), **{
                key: canonical[key] for key in ('status', 'metrics', 'experiment_contract', 'measurement', 'output_evidence') if key in canonical}})
            hashes.update(receipt['hashes'])
        return {'observations': observations, 'evidence_sha256': hashes}

    def report_evidence(self, locators: list[str], implementation_refs: list[str],
                        *, analysis_records: list[str] | None = None) -> dict[str, Any]:
        """Verify accepted physical measurements for the selected code lineage."""
        refs, hashes, measurement_sources = [], {}, []
        if 'conditions' in (self.execution_contract or {}):
            locators = list(self._condition_results(locators, implementation_refs=implementation_refs).values())
        for locator in dict.fromkeys(locators):
            saved = self._measurement_request(locator)
            hint = saved.get('execution_approval', saved.get('implementation_approval', {})).get('binding', {}).get('source')
            if ('conditions' not in (self.execution_contract or {})
                    and (((hint or {}).get('implementation_ref') or {}).get('path') not in implementation_refs)
                    and (implementation_refs or hint)):
                continue  # Select lineage before validating unrelated old output bytes.
            ref, receipt = self._measurement_input(locator)
            refs.append(ref.path)
            if saved.get('origin') == 'reused':
                measurement_sources.append(saved['measurement_source'])
            hashes.update({self.workspace.resolve(path).relative_to(self.root).as_posix(): digest
                for path, digest in receipt['hashes'].items()})
        evidence = {'measurement_refs': refs, 'evidence_sha256': hashes}
        if measurement_sources:
            evidence['measurement_sources'] = measurement_sources
        if analysis_records is not None:
            # A report consumes the actual matching analysis, not whichever
            # analysis directory happens to be newest. The original owner
            # verifies every input/output hash and physical measurement.
            evidence['analysis_records'] = []
            evidence['analysis_artifacts'] = {}
            selected = [self._locator(self.root / path) for path in refs]
            if selected:
                fields = {'result_refs', 'baseline_ref', 'pairs'}
                expected = MeasurementAnalysisRequest.model_validate(self.analysis_arguments(
                    selected, implementation_refs=implementation_refs)).model_dump(include=fields)
                match = self._matching_analysis(analysis_records, expected)
                if match is not None:
                    path, receipt = match
                    store = self._store(path.parent.name)
                    record = path.relative_to(self.root).as_posix()
                    hashes.update({self.workspace.resolve(name).relative_to(self.root).as_posix(): digest
                        for name, digest in receipt['hashes'].items()})
                    hashes[record] = _hash(path)
                    hashes[store.root.relative_to(self.root).as_posix() + '/scientific_result.json'] = _hash(
                        store.root / 'scientific_result.json')
                    for row in receipt['result'].get('metadata', {}).get('capability_artifacts', []):
                        if row['kind'] not in {'analysis_result', 'experiment_set_analysis', 'experiment_comparison', 'experiment_set'}:
                            continue
                        artifact = store.resolve(ArtifactRef.from_dict(row))
                        if (self._locator(artifact) not in receipt['hashes']
                                or row['kind'] in evidence['analysis_artifacts']):
                            raise ValueError('Analysis has an unbound or ambiguous registered artifact kind')
                        evidence['analysis_artifacts'][row['kind']] = artifact.relative_to(self.root).as_posix()
                    evidence['analysis_records'] = [record]
        return evidence

    def _matching_analysis(self, records: list[str], expected: dict) -> tuple[Path, dict] | None:
        """One receipt/selection owner for delivery and writing consumers."""
        expected = self._analysis_selection(MeasurementAnalysisRequest.model_validate(expected))
        for record in reversed(records):
            path = self.workspace.resolve(str(self.root / record) if record.startswith('attempts/') else record)
            if path.parent.parent != self.root / 'attempts':
                raise ValueError('Analysis must belong to this task')
            store = self._store(path.parent.name)
            parsed = MeasurementAnalysisRequest.model_validate(store.read_json('scientific_request.json')['arguments'])
            if self._analysis_selection(parsed) != expected:
                continue
            receipt = self._receipt(path.parent.name, 'analyze_measurements')
            observed = ArtifactStore(self.root).read_json(path.relative_to(self.root).as_posix())
            if (observed.get('tool_name') != 'analyze_measurements' or observed.get('status') != 'ok'
                    or observed.get('data') != receipt['result']['data']
                    or observed.get('artifacts') != receipt['result']['artifacts']):
                raise ValueError('Analysis observation disagrees with its verified receipt')
            return path, receipt
        return None

    def _analysis_selection(self, parsed: MeasurementAnalysisRequest) -> dict:
        """Compare physical task artifacts, not locator presentation aliases."""
        selection = parsed.model_dump(include={'result_refs', 'baseline_ref', 'pairs'})
        for group in [selection, *(selection.get('pairs') or [])]:
            for key in ('result_refs', 'baseline_ref', 'candidate_ref'):
                value = group.get(key)
                if value is not None:
                    paths = value if isinstance(value, list) else [value]
                    normalized = [self.workspace.resolve(path).relative_to(self.root).as_posix() for path in paths]
                    group[key] = normalized if isinstance(value, list) else normalized[0]
        return selection

    def delivery_requirement(self, evidence: dict[str, Any]) -> list[str]:
        """Frozen obligations survive feedback; old request failures do not."""
        if 'conditions' in (self.execution_contract or {}):
            locators = [self._locator(self.root / ref) for ref in evidence.get('measurement_refs', [])]
            selected = self._condition_results(locators, implementation_refs=evidence['implementation_refs'])
            return self.evidence_requirement(list(selected.values()), evidence.get('analysis_records', []))
        selected = evidence['implementation_refs'][-1]
        refs = [ref for ref in evidence.get('measurement_refs', [])
            if (ArtifactStore(self.root).read_json(ref).get('implementation_ref') or {}).get('path') == selected]
        if not refs:
            return ['The current implementation has no formal measurement for its frozen scientific contract']
        path = self.root / refs[-1]
        receipt = self._receipt(path.parent.name, 'measure_experiment')
        return self._measurement_issues(ArtifactStore(self.root).read_json(refs[-1]), receipt['result']['data'])

    def _measurement_request(self, locator: str) -> dict[str, Any]:
        """Project a verified source for readers; imports never create requests or processes."""
        path = self.workspace.resolve(locator)
        rows = [row for row in measurement_conditions(self.execution_contract or {})
            if row.get('reuse_ref') and self.workspace.resolve(row['reuse_ref']) == path]
        if rows:
            if len(rows) != 1:
                raise ValueError('A reused result cannot occupy multiple frozen conditions')
            row = rows[0]
            expected = self.root / 'evidence' / 'reused-baselines'
            if (path.name != 'results.json' or path.parent.parent != expected
                    or not re.fullmatch(r'0|[1-9][0-9]*', path.parent.name)):
                raise ValueError('Reuse must select the task-owned frozen baseline snapshot')
            _reject_links(path)
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != row.get('reuse_sha256'):
                raise ValueError('Reused baseline snapshot digest changed or is missing')
            canonical = json.loads(raw)
            validate_reused_baseline(canonical, row['execution'])
            physical = canonical['measurement']['measurement_id']
            if self.implementation is not None and self.implementation.exact is not None:
                raise ValueError('Reused baseline cannot bypass scoped producing-source lineage')
            process = (canonical.get('execution') or {}).get('process') or {}
            return {'origin': 'reused', 'arguments': MeasurementRequest.model_validate({
                    **row['execution'], 'condition_id': row['id']}).model_dump(),
                'hashes': {self.workspace.locator(str(path)): digest},
                'measurement_source': {'origin': 'reused', 'condition_id': row['id'],
                    'result_ref': self.workspace.locator(str(path)), 'sha256': digest,
                    'measurement_id': physical, 'source_session_id': process.get('session_id'),
                    'source_attempt_id': process.get('attempt_id'),
                    'declaration': 'Imported canonical measurement; original identity retained. '
                        'No execution, approval or process accounting was created in this task.'}}
        if path.name != 'results.json' or path.parent.parent != self.root / 'attempts':
            raise ValueError('Measurement must belong to this task')
        return ArtifactStore(path.parent).read_json('scientific_request.json')

    def _condition_results(self, locators: list[str], *, implementation_refs: list[str] | None = None) -> dict[str, str]:
        """Select actual observations and frozen imports through the same source reader."""
        selected, sources, pending = {}, {}, []
        reused = [row['reuse_ref'] for row in measurement_conditions(self.execution_contract or {}) if row.get('reuse_ref')]
        for locator in dict.fromkeys([*locators, *reused]):
            request = self._measurement_request(locator)
            source = request.get('execution_approval', request.get('implementation_approval', {})).get('binding', {}).get('source')
            implementation = ((source or {}).get('implementation_ref') or {}).get('path')
            if implementation_refs is not None and implementation and (
                    not implementation_refs or implementation != implementation_refs[-1]):
                continue  # Obsolete or unrelated revisions cannot satisfy current slots.
            pending.append((locator, request, source))
        revisions = {source['implementation_ref']['path'] for _, _, source in pending
            if source and source.get('implementation_ref')}
        if len(revisions) > 1:
            raise ValueError('Select the current implementation revision before composing frozen conditions')
        origins = []
        # Verify implementation facts before using their owner-bound ancestry
        # to select preparation observations. Do not inspect obsolete outputs.
        for locator, _, source in pending:
            if source and source.get('implementation_ref'):
                self._measurement_input(locator)
                origins.append(source.get('preparation_origin'))
        for locator, request, source in pending:
            if (implementation_refs is not None and source and not source.get('implementation_ref')
                    and source.get('prepared_ref') and origins
                    and source.get('preparation_origin') not in origins):
                continue
            parsed = MeasurementRequest.model_validate(request['arguments'])
            self._condition(parsed, allow_reused=request.get('origin') == 'reused')
            self._measurement_input(locator)
            identity = parsed.condition_id or 'candidate'
            selected[identity] = locator
            sources[identity] = source
        self._check_preparation_ancestry(list(sources.values()))
        physicals = [ArtifactStore(self.root).read_json(self._measurement_input(locator)[0])[
            'measurement']['measurement_id'] for locator in selected.values()]
        if len(physicals) != len(set(physicals)):
            raise ValueError('A physical measurement cannot satisfy multiple frozen conditions')
        return selected

    def _check_preparation_ancestry(self, sources: list[dict[str, Any] | None]) -> None:
        prepared = [source.get('preparation_origin') for source in sources
            if source and source.get('implementation_ref') is None and source.get('prepared_ref')]
        implementations = [source.get('preparation_origin') for source in sources if source and source.get('implementation_ref')]
        if prepared and (any(not origin for origin in prepared)
                or any(origin != prepared[0] for origin in [*prepared, *implementations])):
            raise ValueError('Prepared measurement and implementation do not share the approved preparation ancestor')

    def analysis_arguments(self, locators: list[str], *, implementation_refs: list[str] | None = None) -> dict[str, Any]:
        selected = self._condition_results(locators, implementation_refs=implementation_refs)
        rows = measurement_conditions(self.execution_contract)
        if any(row['seed'] is not None for row in rows):
            seeds = dict.fromkeys(row['seed'] for row in rows)
            return {'pairs': [{'seed': seed, **{row['role'] + '_ref': selected.get(row['id'])
                for row in rows if row['seed'] == seed}} for seed in seeds]}
        roles = {row['role']: selected.get(row['id']) for row in rows}
        if not roles.get('candidate'):
            raise ValueError('The declared candidate has no actual measurement')
        return {'result_refs': [roles['candidate']], **({'baseline_ref': roles['baseline']} if 'baseline' in roles else {})}

    def evidence_requirement(self, locators: list[str], analysis_records: list[str],
                             *, proposed_analysis: dict[str, Any] | None = None,
                             proposed_measurement: dict[str, Any] | None = None) -> list[str]:
        """Full frozen coverage survives feedback and cannot be replaced by a subset."""
        if proposed_measurement is not None and 'conditions' not in self.execution_contract:
            parsed = MeasurementRequest.model_validate(proposed_measurement)
            if parsed.model_dump(exclude={'delivery', 'implementation_ref', 'preparation_ref', 'workspace_outputs', 'condition_id'}) == self.execution_contract:
                return []  # Literal single-run inline delivery; commit verifies the actual receipt.
        selected = self._condition_results(locators)
        issues = []
        for row in measurement_conditions(self.execution_contract):
            locator = selected.get(row['id'])
            if locator is None:
                issues.append(f"Missing declared measurement: {row['id']}")
                continue
            ref, receipt = self._measurement_input(locator)
            issues.extend(f"{row['id']}: {issue}" for issue in self._measurement_issues(
                ArtifactStore(self.root).read_json(ref), receipt['result']['data']))
        if 'conditions' not in self.execution_contract or issues:
            return issues
        expected = MeasurementAnalysisRequest.model_validate(self.analysis_arguments(locators))
        fields = {'result_refs', 'baseline_ref', 'pairs'}
        if proposed_analysis is not None and self._analysis_selection(MeasurementAnalysisRequest.model_validate(
                proposed_analysis)) == self._analysis_selection(expected):
            return []  # Preflight only; the saved domain result is checked again before commit.
        match = self._matching_analysis(analysis_records, expected.model_dump(include=fields))
        if match is not None:
            path, receipt = match
            return self._analysis_issues(receipt['result']['data'], self._store(path.parent.name))
        return ['The complete declared measurement set has no matching deterministic analysis']

    def can_reconcile(self, name: str, identity: str) -> bool:
        try:
            self._receipt(identity, name)
            return name in self.context_tools
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False

    def _measurement_input(self, locator: str) -> tuple[ArtifactRef, dict[str, Any]]:
        path = self.workspace.resolve(locator)
        projected = self._measurement_request(locator)
        if projected.get('origin') == 'reused':
            # In-memory evidence projection only: no imported scientific receipt,
            # approval, finalized process or budget entry is persisted.
            return ArtifactRef(path.relative_to(self.root).as_posix(), kind='experiment_result',
                schema='canonical_results.2.5', producer='research.experiment'), {
                    'origin': 'reused', 'hashes': projected['hashes'],
                    'result': {'data': {'measurement_source': projected['measurement_source']}}}
        if path.name != 'results.json' or path.parent.parent != self.root / 'attempts':
            raise ValueError('Only canonical results from current task attempts are accepted')
        receipt = self._receipt(path.parent.name, 'measure_experiment')
        if self._locator(path) not in receipt['hashes']:
            raise ValueError('Canonical result is not registered in the measurement receipt')
        return ArtifactRef(path.relative_to(self.root).as_posix(), kind='experiment_result',
            schema='canonical_results.2.5', producer='research.experiment'), receipt

    def _analysis_inputs(self, parsed: MeasurementAnalysisRequest) -> tuple[list[ArtifactRef], dict, dict | None]:
        refs, inputs, pairs, seen, lineages, sources = [], {}, [], set(), [], {}
        plan = ([(pair.seed, role, getattr(pair, role + '_ref')) for pair in parsed.pairs
                 for role in ('baseline', 'candidate')] if parsed.pairs is not None else
                [(None, 'candidate', parsed.result_refs[0]), (None, 'baseline', parsed.baseline_ref)])
        for seed, role, locator in plan:
            if parsed.pairs is not None and role == 'baseline':
                pairs.append({'seed': seed, 'baseline': None, 'candidate': None})
            if locator is None:
                continue
            ref, receipt = self._measurement_input(locator)
            saved = self._measurement_request(locator)
            source = saved.get('execution_approval', saved.get('implementation_approval', {})).get('binding', {}).get('source')
            if 'conditions' in (self.execution_contract or {}):
                condition = self._condition(MeasurementRequest.model_validate(saved['arguments']),
                    allow_reused=saved.get('origin') == 'reused')
                if condition['role'] != role or parsed.pairs is not None and condition['seed'] != seed:
                    raise ValueError('Analysis role/seed differs from the frozen measurement slot')
            sources.setdefault(seed, []).append(source)
            canonical = ArtifactStore(self.root).read_json(ref)
            physical = canonical['measurement']['measurement_id']
            if physical in seen:
                raise ValueError('A physical measurement may appear only once in an analysis')
            seen.add(physical)
            for row in (canonical.get('implementation_ref'),
                    (canonical.get('preparation') or {}).get('source_ref')):
                if row is not None:
                    lineage = ArtifactRef.from_dict(row)
                    if lineage not in lineages:
                        lineages.append(lineage)
            if parsed.pairs is not None:
                declared = canonical.get('experiment_contract', {}).get('comparison_conditions', {}).get('seed')
                if type(declared) is not int or declared != seed or canonical['measurement'].get('seed') != seed:
                    raise ValueError('Pair seed must equal the original canonical comparison_conditions.seed; no inferred seed')
                pairs[-1][role] = ref.to_dict()
            refs.append(ref)
            inputs[locator] = receipt['hashes']
        if self.implementation is not None and self.implementation.exact is not None and self.execution_contract is not None:
            revisions = {source['implementation_ref']['path'] for grouped in sources.values()
                for source in grouped if source and source.get('implementation_ref')}
            if len(revisions) > 1:
                raise ValueError('Frozen analysis cannot mix implementation revisions')
            self._check_preparation_ancestry([source for grouped in sources.values() for source in grouped])
        collection = {'schema_version': 'experiment_set.v1', 'pairs': pairs} if parsed.pairs is not None else None
        if (collection is not None and self.execution_contract is not None
                and all(row['role'] == 'candidate' for row in measurement_conditions(self.execution_contract))):
            collection['baseline_policy'] = 'skip'  # Frozen caller obligations, not model-selected missingness.
        return [*refs, *lineages], inputs, collection

    def _analysis_issues(self, data: dict, store: ArtifactStore | None = None) -> list[str]:
        gaps = data.get('comparison_gaps', [])
        if ('comparison_gaps' not in data and data.get('baseline_ref') and store is not None
                and 'conditions' in (self.execution_contract or {})):
            # Older receipts retain the original hash-bound comparison, not
            # this later convenience field. Project it for this frozen set,
            # without retroactively changing older unfrozen delivery contracts.
            comparison = store.read_json('comparison.json')
            gaps = ([] if comparison['comparability'] == 'declared_match' else
                [f"Comparison conditions {comparison['comparability']}; no verified same-condition comparison"])
        return [*gaps, *(
            [f"Analysis remains {data['analysis_status']} with execution {data['execution_status']}"]
            if data['analysis_status'] != 'passed' or data['execution_status'] != 'passed' else [])]

    @staticmethod
    def _measurement_issues(canonical: dict, data: dict | None = None) -> list[str]:
        issues = []
        if canonical.get('status') != 'passed' or canonical.get('execution_status') != 'passed':
            issues.append('measurement did not pass')
        if canonical.get('guard', {}).get('status') == 'failed':
            issues.append('result guard failed')
        if canonical.get('measurement', {}).get('asset_integrity', {}).get('status') == 'changed':
            issues.append('protected assets changed')
        schema, metrics = canonical['result_schema'], canonical['metrics']
        required = list(schema.get('required_metrics', []))
        if schema.get('primary_metric'):
            required.append(schema['primary_metric'])
        missing = [name for name in dict.fromkeys(required) if type(metrics.get(name)) not in (int, float)
                   or not math.isfinite(metrics[name])]
        if missing:
            issues.append('missing/nonfinite required metrics: ' + ', '.join(missing))
        if (data or {}).get('missing_workspace_outputs'):
            issues.append('Declared candidate outputs missing: ' + ', '.join(data['missing_workspace_outputs']))
        return issues

    def delivery_issues(self, observations: list[dict[str, Any]]) -> list[str]:
        """Disclose only this request's accepted evidence, without execution or analysis.

        The caller supplies observations in accepted chronological order and
        owns request/feedback scoping. A later valid measurement supersedes a
        failure only for the identical approved command and conditions.
        """
        issues, measurements = [], {}
        for observation in observations:
            if not observation.get('current_request', True):
                continue
            name = observation.get('tool_name')
            if name not in self.context_tools:
                continue
            record = observation.get('record', '')
            try:
                # The host stores task-root-relative records; public tools use
                # records/ aliases. Both pass the same readonly mount resolver.
                path = self.workspace.resolve(str(self.root / record) if record.startswith('attempts/') else record)
                if not path.is_file() or path.parent.parent != self.root / 'attempts':
                    raise ValueError('Accepted observation is not a current-task attempt record')
                store = self._store(path.parent.name)
                receipt = self._receipt(path.parent.name, name)
                request = store.read_json('scientific_request.json')
                model = MeasurementRequest if name == 'measure_experiment' else MeasurementAnalysisRequest
                if model.model_validate(observation['arguments']).model_dump() != request['arguments']:
                    raise ValueError('Accepted arguments disagree with the verified scientific request')
                if name == 'measure_experiment':
                    approval = request.get('execution_approval', request.get('implementation_approval'))
                    binding = approval['binding']
                    condition = json.dumps([*[binding[key] for key in ('argv', 'cwd', 'result_schema', 'protocol')],
                        binding['arguments'].get('workspace_outputs', [])],
                        sort_keys=True, ensure_ascii=False, allow_nan=False)
                    failures = self._measurement_issues(store.read_json('results.json'), receipt['result']['data'])
                    if failures:
                        measurements[condition] = f"{record}: " + '; '.join(failures)
                    else:
                        measurements.pop(condition, None)
                else:
                    issues.extend(f'{record}: {issue}' for issue in self._analysis_issues(receipt['result']['data'], store))
            except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
                issues.append(f'{record or name}: scientific evidence unavailable or changed ({exc}); disclose this gap')
        return list(dict.fromkeys([*issues, *measurements.values()]))

    def execute_tool(self, name: str, context: CapabilityContext, arguments: dict[str, Any],
                     *, approval: dict | None = None) -> ToolResult:
        store = None
        started = False
        try:
            if name not in self.context_tools or not isinstance(context, CapabilityContext):
                raise ValueError('Scientific execution requires the real task CapabilityContext')
            identity = context.attempt.attempt_id
            store = self._store(identity)
            if context.store.root.resolve() != store.root or context.store.root != store.root:
                raise ValueError('CapabilityContext store must match this task attempt')
            parsed = (MeasurementRequest if name == 'measure_experiment'
                      else MeasurementAnalysisRequest).model_validate(arguments)
            if store.exists('scientific_request.json'):
                saved = store.read_json('scientific_request.json')
                if saved['arguments'] != parsed.model_dump() or saved['tool_name'] != name:
                    raise ValueError('Existing attempt request differs; no replay')
                receipt = self._receipt(identity, name)
                if approval is not None and approval != saved.get('execution_approval', saved.get('implementation_approval')):
                    raise ValueError('Approval differs from the retained execution approval')
                return ToolResult.model_validate(receipt['result'])
            if self.has_started(identity) or store.exists('scientific_result.json'):
                raise ValueError('Unfinished scientific attempt; inspect retained records, no replay')
            self._locator(store.root / 'scientific_request.json')
            inputs = {}
            if name == 'measure_experiment':
                if not approval or not isinstance(approval.get('token'), str) or not approval['token'].strip():
                    raise ValueError('Measurement requires an execution approval token and binding')
                binding = self.approval_binding(arguments, identity=identity)
                if approval.get('binding') != binding:
                    raise ValueError('Execution approval binding changed')
                setup = binding.get('environment_preparation')
                steps = len(setup['steps']) if setup else 0
                formal = ([row for row in measurement_conditions(self.execution_contract) if not row.get('reuse_ref')]
                    if setup and 'conditions' in (self.execution_contract or {}) else
                    [{'execution': {'timeout_sec': parsed.timeout_sec}}])
                for key, required in (('process_invocations', len(formal) + steps),
                        ('process_wall_seconds', sum(row['execution']['timeout_sec'] for row in formal)
                            + (steps * setup['profile']['timeout_sec'] if setup else 0))):
                    limit = self.ledger.limits.get(key)
                    if type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0:
                        raise ValueError('Measurement requires explicit finite process budgets; None is not authorization')
                    remaining = self.ledger.remaining(key)
                    if remaining is None or remaining < required:
                        raise ValueError(f'Insufficient or unknown shared {key} budget before execution')
                # Lineage is explicitly selected and verified by its owner,
                # never inferred from the controller's globally active inputs.
                if any(ref.kind in {'implementation_result', 'prepared_execution'} for ref in context.inputs):
                    raise ValueError('Select verified measurement lineage explicitly, not through inherited inputs')
                _, request, source = self._request(arguments, identity=identity)
            else:
                refs, inputs, collection = self._analysis_inputs(parsed)
            saved = {'tool_name': name, 'session_id': self.session_id,
                'arguments': parsed.model_dump(), 'inputs': inputs}
            if name == 'measure_experiment':
                saved['execution_approval'] = approval
            store.write_json('scientific_request.json', saved)
            started = True
            if name == 'measure_experiment':
                if approval['binding'] != self.approval_binding(arguments, identity=identity):
                    raise ValueError('Execution inputs changed immediately before launch')
                request = replace(request, run=replace(request.run, session_id=self.session_id,
                    attempt_id=identity))
                domain_context = context
                preparation = None
                if binding.get('shared_environment'):
                    owner, original, receipt = self._shared_environment(identity)
                    inputs.update(receipt['hashes'])
                    inputs[self._locator(owner.root / 'scientific_result.json')] = _hash(owner.root / 'scientific_result.json')
                    prepared = ArtifactRef.from_dict(original['environment_preparation']['prepared_ref'])
                    domain_context = replace(context, inputs=(prepared,), input_store=ArtifactStore(self.root))
                elif parsed.environment is not None:
                    from simple_ar.research.preparation import PreparationRequest
                    from simple_ar.research.project_environment import prepare_project_environment
                    declaration = {'command': parsed.command if parsed.environment['mode'] == 'venv' else request.run.command,
                        'cwd': str(request.run.cwd), 'timeout_sec': request.run.timeout_sec,
                        'label': request.run.label, 'result_schema': dict(request.result_schema),
                        'protocol': request.normalized_experiment_contract(), 'environment': parsed.environment}
                    preparation = prepare_project_environment(context=context,
                        request=PreparationRequest(declaration, '', replace(request.run, command=declaration['command'])),
                        backend=LocalExecutionBackend(budget_ledger=self.ledger), record_runtime=True)
                    if preparation.status != 'completed':
                        raise ValueError('Environment preparation failed; formal measurement was not launched')
                    # Preparation may run project/build code. Do not let it
                    # redefine the formal program or protected inputs after approval.
                    if (binding != self.approval_binding(arguments, identity=identity)
                            or snapshot_protocol_assets(binding['protocol'], request.run.cwd) != binding['protected_assets']):
                        raise ValueError('Preparation changed approved formal inputs; no measurement was launched')
                    self._environment_evidence(store, binding)
                    prepared = ArtifactRef.from_dict(binding['environment_preparation']['prepared_ref'])
                    domain_context = replace(context, inputs=(prepared,), input_store=ArtifactStore(self.root))
                    preparation = replace(preparation, artifacts=(*preparation.artifacts,
                        store.ref('environment_processes', kind='environment_processes', producer='research.preparation')))
                if source is not None:
                    source_refs = tuple(ArtifactRef.from_dict(source[key]) for key in
                        ('implementation_ref', 'prepared_ref') if source.get(key) is not None)
                    domain_context = replace(context, inputs=source_refs, input_store=ArtifactStore(self.root))
                capability = run_experiment_capability(context=domain_context, request=request,
                    backend=LocalExecutionBackend(budget_ledger=self.ledger))
                if preparation is not None:
                    capability = replace(capability, artifacts=(*preparation.artifacts, *capability.artifacts))
                canonical = store.read_json('results.json')
                data = {'result_ref': self._locator(store.root / 'results.json'),
                    'returncode': canonical['returncode'], 'timed_out': canonical['timed_out'],
                    'execution_status': canonical['execution_status'],
                    'canonical_status': canonical['status'], 'metrics': canonical['metrics'],
                    'capability_status': capability.status, 'diagnostics': list(capability.diagnostics)}
                if source is not None:
                    data['lineage_limitations'] = source.get('limitations', [])
                    from simple_ar.code_task.runtime.state import retained_implementation_inventory
                    produced = {}
                    for output_name in parsed.workspace_outputs:
                        path = Path(source['cwd']) / output_name
                        _reject_links(path)
                        if path.exists():
                            if not path.is_file():
                                raise ValueError('Declared candidate output is not a regular file')
                            produced[output_name] = _hash(self.workspace.resolve(str(path)))
                    run = self._source_run(source)
                    retained_implementation_inventory(run, binding['candidate_inventory'], produced)
                    if produced:
                        data['workspace_outputs'] = produced
                    data['missing_workspace_outputs'] = sorted(set(parsed.workspace_outputs) - produced.keys())
                    data['validation_scope'] = ('Original validated inventory only; derived outputs are recorded data, not newly validated source. Revalidate before another measurement of this changed candidate.'
                        if source.get('implementation_ref') else 'Prepared source inventory only; no implementation validation is claimed. Derived outputs are recorded data, not validated source.')
                output = (store.root / 'execution' / 'process' /
                    canonical['measurement']['measurement_id'] / 'outputs')
                data['effective_execution'] = {'argv': canonical['command'],
                    'cwd': canonical['execution']['cwd'],
                    'environment': {**request.run.env, 'SIMPLE_AR_OUTPUT_DIR': str(output)}
                        if request.run.env is not None and parsed.environment is None else {
                            **binding['environment'], 'output_dir': str(output)} if parsed.environment is not None
                        else {'policy': 'inherit', 'output_dir': str(output)}}
                if parsed.delivery is not None:
                    result_ref = ArtifactRef((store.root / 'results.json').relative_to(self.root).as_posix(),
                        kind='experiment_result', schema='canonical_results.2.5', producer='research.experiment')
                    input_refs = (result_ref, *domain_context.inputs)
                    analysis = analyze_experiment_capability(context=replace(context, inputs=input_refs,
                        input_store=ArtifactStore(self.root)), result_ref=result_ref,
                        analysis_context={'task_id': self.session_id,
                            'research_question': parsed.delivery.summary}, use_llm=False)
                    handoff = store.read_json('analysis.json')
                    markdown = store.write_text('analysis.md', handoff['analysis']['readme_markdown'],
                        kind='analysis_markdown', producer='research.analysis')
                    capability = replace(capability, artifacts=(*capability.artifacts, *analysis.artifacts, markdown))
                    data['analysis_status'] = handoff['analysis']['status']
            else:
                collection_artifacts = ()
                result_ref = refs[0] if collection is None else None
                if collection is not None:
                    local = store.write_json('experiment_set.json', collection,
                        kind='experiment_set', schema='experiment_set.v1', producer='scientific')
                    # The service reads every input against task root, including
                    # the collection owned by this same output attempt.
                    result_ref = replace(local, path=(store.root / local.path).relative_to(self.root).as_posix())
                    refs.append(result_ref)
                    collection_artifacts = (local,)
                domain_context = replace(context, inputs=tuple(refs), input_store=ArtifactStore(self.root))
                capability = analyze_experiment_capability(context=domain_context, result_ref=result_ref,
                    baseline_ref=refs[1] if parsed.baseline_ref else None,
                    analysis_context={'task_id': self.session_id, 'research_question': parsed.question,
                        'existing_writeup': parsed.context}, use_llm=False)
                analysis = store.read_json('analysis.json')
                reused_sources = [saved['measurement_source'] for locator in inputs
                    if (saved := self._measurement_request(locator)).get('origin') == 'reused']
                if reused_sources:
                    analysis['measurement_sources'] = reused_sources
                    analysis['analysis']['readme_markdown'] += '\n\nReused baseline evidence:\n' + '\n'.join(
                        f"- {item['condition_id']}: {item['result_ref']}; original measurement_id="
                        f"{item['measurement_id']}; SHA256={item['sha256']}. {item['declaration']}"
                        for item in reused_sources) + '\n'
                    store.write_json('analysis.json', analysis)
                markdown = store.write_text('analysis.md', analysis['analysis']['readme_markdown'],
                    kind='analysis_markdown', producer='research.analysis')
                capability = replace(capability, artifacts=(*capability.artifacts, markdown, *collection_artifacts))
                data = {'execution_status': analysis['execution_status'],
                    'analysis_ref': self._locator(store.root / 'analysis.json'),
                    'analysis_status': analysis['analysis']['status'], 'mode': 'deterministic',
                    'result_refs': parsed.result_refs, 'baseline_ref': parsed.baseline_ref,
                    'comparison_granularity': 'explicit seed pairs grouped by existing protocol rules'
                        if collection is not None else 'one candidate with optional baseline',
                    'diagnostics': list(capability.diagnostics)}
                if reused_sources:
                    data['measurement_sources'] = reused_sources
                if collection is not None:
                    paired = store.read_json('paired_analysis.json')
                    data.update(collection_ref=self._locator(store.root / 'experiment_set.json'),
                        pairs=parsed.model_dump()['pairs'], coverage_gaps=paired.get('coverage_gaps', []),
                        missing_measurements=paired['missing_measurements'], failed_measurements=paired['failed_measurements'])
                    if paired.get('baseline_policy') == 'skip':
                        data['comparison_granularity'] = 'candidate-only descriptive summaries; no baseline comparison'
                elif parsed.baseline_ref:
                    comparison = store.read_json('comparison.json')
                    data['comparison_gaps'] = ([] if comparison['comparability'] == 'declared_match' else
                        [f"Comparison conditions {comparison['comparability']}; no verified same-condition comparison"])
                for locator, hashes in inputs.items():
                    self._measurement_input(locator)
                    if self.workspace.artifact_hashes(hashes) != hashes:
                        raise ValueError('Measurement evidence changed during analysis')
            paths = {self._locator(store.root / 'scientific_request.json')}
            if name == 'measure_experiment' and source is not None:
                paths.update(source['evidence_hashes'])
                paths.update(self.workspace.locator(str(Path(source['cwd']) / name))
                    for name in data.get('workspace_outputs', {}))
            paths.update(inputs)  # Writing can cite the original canonical evidence.
            for ref in capability.artifacts:
                path = store.resolve(ref)
                if path.is_dir():
                    paths.update(self._locator(child) for child in path.rglob('*') if child.is_file())
                elif path.is_file():
                    paths.add(self._locator(path))
                else:
                    raise ValueError('Domain result references a missing artifact')
            artifacts = sorted(paths - {self._locator(store.root / 'scientific_request.json')})
            result = ToolResult(tool_name=name, data=data, artifacts=artifacts,
                summary='Recorded execution evidence; scientific success is not certified.' if name == 'measure_experiment'
                    else 'Deterministic analysis of recorded evidence; comparison limitations are retained.',
                metadata={'capability_artifacts': [ref.to_dict() for ref in capability.artifacts]})
            if parsed.delivery is not None:
                if name == 'measure_experiment':
                    expected_delivery = approval['binding'].get('delivery_inputs', {})
                    if self.workspace.artifact_hashes(expected_delivery) != expected_delivery:
                        raise ValueError('Approved existing delivery inputs changed during measurement')
                else:
                    self.workspace.artifact_hashes(parsed.delivery.artifacts)
                issues = self._measurement_issues(canonical, data) if name == 'measure_experiment' else []
                issues.extend(self._analysis_issues(data))
                result.data['delivery'] = {**parsed.delivery.model_dump(),
                    'artifacts': list(dict.fromkeys([*parsed.delivery.artifacts,
                        *(path for path in artifacts if path.startswith(f'records/{identity}/'))])),
                    'unresolved': list(dict.fromkeys([*parsed.delivery.unresolved, *issues]))}
            envelope = store.write_json('scientific_tool_result.json', result.model_dump(mode='json'))
            paths.add(self._locator(store.resolve(envelope)))
            store.write_json('scientific_result.json', {'tool_name': name, 'session_id': self.session_id,
                'attempt_id': identity, 'hashes': self.workspace.artifact_hashes(paths),
                'result': result.model_dump(mode='json')})
            if name == 'measure_experiment':
                self._receipt(identity, name)
            return result
        except Exception as exc:
            if started and store is not None:
                store.write_text('scientific_error.txt', traceback.format_exc())
            return ToolResult(tool_name=name, status='blocked',
                error=f'{type(exc).__name__}: {exc}. Inspect retained evidence; no automatic replay.',
                artifacts=[self._locator(store.root / 'scientific_error.txt')] if started and store else [])

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        return ToolResult(tool_name=name, status='blocked',
            error='Scientific tools require a real task CapabilityContext and shared approval dispatch.')
