"""Task creation, configuration validation, feedback, and supply mixin for ToolApplication."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Literal

from simple_ar.app.tool_contracts import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MAX_TURNS,
    _COMPLETION_POLICIES,
    _CONTEXT_PROTOCOLS,
    _CONVERSATION_CONTEXT_PROTOCOLS,
    _CreationDeclarations,
    _DECLARED_INPUTS,
    _OPERATION_COMPLETION,
    _OPERATION_EXIT,
    _REPORT_PROTOCOL,
    _TOOL_CONTRACT_MODES,
    _WORKING_CONTEXT_PROTOCOLS,
    _review_requirement,
    _runtime_bindings,
)
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore, CapabilityRegistry
from simple_ar.core.session import BudgetState, SessionController
from simple_ar.core.working_context import TURN_SUPPLY_POLICY
from simple_ar.integrations.llm import LLMClient
from simple_ar.integrations.model_profiles import model_connections_compatible
from simple_ar.tools.extensions import freeze_extensions
from simple_ar.tools.implementation import ExactCodeTask
from simple_ar.tools.publication import PUBLICATION_CONSUMER_PROTOCOL
from simple_ar.tools.sources import SourcePolicy, SourceToolGateway
from simple_ar.tools.specs import GOAL_WORK_METHOD, MODEL_REVIEW_SELECTIONS
from simple_ar.tools.workspace import resolve_source_mount


def _tool_app_module():
    import simple_ar.app.tool_application as module
    return module


class _ToolConfigMixin:
    @classmethod
    def _configuration(cls, root: Path, goal: str, *, model: str | None, sources: dict[str, Path],
               allow_execution: bool = False, sandbox_execution: bool = False,
               allow_network: bool = False, allow_vision: bool = False, max_turns: int | None = DEFAULT_MAX_TURNS,
               max_tokens: int | None = DEFAULT_MAX_TOKENS, max_images: int = 0,
               project_python: Path | None = None, code_task: dict[str, Any] | None = None,
               process_limits: dict[str, int | float] | None = None,
               report_requirement: dict[str, Any] | None = None, report_protocol: str | None = None,
               research_policy: dict[str, Any] | None = None,
               table_analysis: dict[str, Any] | None = None,
               max_output_tokens: int | None = None,
               max_attempts: int | None = None,
               source_operations: int | None = None,
               execution_contract: dict[str, Any] | None = None,
               services: Any | None = None,
               snapshot_sources: bool = False,
               max_snapshot_bytes: int = 16 * 1024 * 1024,
               tool_contracts: Literal['full', 'on_demand', 'outcomes'] = 'on_demand',
               completion_policy: str | None = None, control_protocol: str | None = None,
               delivery_files: list[str] | None = None,
               independent_review_required: bool | None = None,
               model_catalogs: dict[str, str | None] | None = None,
               extensions: list[Path] | None = None,
               creation_declarations: dict[str, Any] | None = None,
               retained: bool = False) -> tuple[dict[str, Any], list, dict[str, Any], int]:
        """Validate caller contracts before creation or supply; never execute or persist."""
        if control_protocol not in {None, 'operations.v1'}:
            raise ValueError('Unsupported operation control protocol')
        if independent_review_required is None:
            # A free-text request may require review even if the producing model
            # overlooks that requirement. The independent owner decides how much
            # investigation is needed. Only the caller can make it optional.
            independent_review_required = (model is not None and control_protocol == 'operations.v1'
                and not retained)
        elif type(independent_review_required) is not bool:
            raise ValueError('independent_review_required must be a boolean')
        if independent_review_required and (model is None or control_protocol != 'operations.v1'):
            raise ValueError('A required independent review needs a model and the common operation delivery owner')
        if type(sandbox_execution) is not bool or sandbox_execution and not allow_execution:
            raise ValueError('Workspace execution requires explicit process authorization')
        if sandbox_execution:
            import shutil
            import sys
            if not sys.platform.startswith('linux') or shutil.which('bwrap') is None:
                raise ValueError('Workspace execution requires bubblewrap on a Linux execution host')
        if completion_policy is None:
            completion_policy = _OPERATION_EXIT if control_protocol is not None else 'closing_decision.v2'
        frozen_extensions = freeze_extensions(extensions or [])
        if report_protocol not in {None, _REPORT_PROTOCOL}:
            raise ValueError('Unsupported report protocol')
        if type(tool_contracts) is not str or tool_contracts not in _TOOL_CONTRACT_MODES:
            raise ValueError('Tool contracts must be full, on_demand or outcomes')
        if completion_policy not in _COMPLETION_POLICIES | {_OPERATION_COMPLETION, _OPERATION_EXIT}:
            raise ValueError('Unsupported completion policy')
        if delivery_files is not None:
            from simple_ar.tools.workspace import validate_delivery_patterns
            delivery_files = validate_delivery_patterns(delivery_files)
        if (not goal.strip() or max_images < 0 or
                (model is not None and (not model.strip() or max_turns == 0 or max_tokens == 0))):
            raise ValueError('Goal and positive model resource bounds are required')
        if source_operations is not None and (type(source_operations) is not int or source_operations < 0):
            raise ValueError('Source operation allowance must be a finite nonnegative count')
        if any(value is not None and (type(value) is not int or value < 0) for value in (max_turns, max_tokens)):
            raise ValueError('Model resource allowances must be nonnegative integers or unlimited')
        if model is None and (allow_vision or max_images
                or code_task is not None or max_output_tokens is not None):
            raise ValueError('A caller-directed task cannot authorize a model-owned contract')
        attempt_limit = max_attempts if max_attempts is not None else (max_turns * 10 if max_turns is not None else 300)
        if type(attempt_limit) is not int or attempt_limit < 1:
            raise ValueError('A finite positive task attempt allowance is required')
        if execution_contract is not None:
            from simple_ar.tools.scientific import measurement_contract
            if not allow_execution:
                raise ValueError('A fixed measurement contract needs explicit execution access')
            execution_contract = measurement_contract(execution_contract)
        if process_limits is not None and (not isinstance(process_limits, dict)
                or set(process_limits) - {'process_invocations', 'process_wall_seconds'}
                or any(type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)) or value < 0
                       for value in process_limits.values())
                or ('process_invocations' in process_limits and type(process_limits['process_invocations']) is not int)):
            raise ValueError('Process limits require finite nonnegative counts/wall seconds; zero is not omission')
        mounts = {}
        snapshots = []
        if type(snapshot_sources) is not bool or type(max_snapshot_bytes) is not int or max_snapshot_bytes < 1:
            raise ValueError('Source snapshots require a boolean choice and positive byte bound')
        if project_python is not None:
            project_python = project_python.expanduser().absolute()
            if not project_python.is_file():
                raise ValueError('Task Python interpreter is unavailable')
        for name, path in sources.items():
            if name in {"workspace", "records", "evidence"} or not name.isidentifier():
                raise ValueError("Invalid or reserved source alias")
            path = resolve_source_mount(path)
            mounts[name] = str(path)
            if snapshot_sources:
                if not path.is_file() or path.stat().st_size > max_snapshot_bytes:
                    raise ValueError('Snapshot sources must be explicitly selected bounded regular files')
                with path.open('rb') as stream:
                    content = stream.read(max_snapshot_bytes + 1)
                if len(content) > max_snapshot_bytes:
                    raise ValueError('Snapshot source grew beyond its declared bound')
                snapshots.append((name, path, content))
        # A reused result is retained user evidence, not a new physical run.
        # Validate every declared slot before creating the session, then bind
        # the original bytes into the existing read-only evidence mount.
        reused_inputs = []
        if execution_contract is not None:
            from simple_ar.tools.scientific import measurement_conditions, _reject_links, validate_reused_baseline
            from simple_ar.tools.workspace import resolve_mounted_path
            for row in measurement_conditions(execution_contract):
                if 'reuse_ref' not in row:
                    continue
                available = {name: Path(path) for name, path in mounts.items()}
                if retained:
                    available['evidence'] = Path(root) / 'evidence'
                path = resolve_mounted_path(available, row['reuse_ref'])
                _reject_links(path)
                if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('Reused baseline must be a canonical result file of at most 16 MiB')
                content = path.read_bytes()
                canonical = json.loads(content)
                validate_reused_baseline(canonical, row['execution'])
                digest = hashlib.sha256(content).hexdigest()
                if row.get('reuse_sha256') is not None and row['reuse_sha256'] != digest:
                    raise ValueError('Reused baseline differs from its declared digest')
                if retained and path.is_relative_to((Path(root) / 'evidence/reused-baselines').resolve()):
                    row['reuse_ref'], row['reuse_sha256'] = path.relative_to(Path(root).resolve()).as_posix(), digest
                    continue
                index = len(reused_inputs)
                while retained and (Path(root) / f'evidence/reused-baselines/{index}/results.json').exists():
                    index += 1
                relative = f'evidence/reused-baselines/{index}/results.json'
                destination = Path(root) / relative
                _reject_links(destination)
                if destination.exists():
                    raise ValueError('Retained baseline destination already exists')
                reused_inputs.append((relative, content))
                row['reuse_ref'], row['reuse_sha256'] = relative, digest
        if research_policy is not None:
            from simple_ar.tools.workspace import resolve_mounted_path
            policy = SourcePolicy.model_validate(research_policy)
            readable = {name: Path(path) for name, path in mounts.items()}
            if retained:
                readable['evidence'] = Path(root) / 'evidence'
            SourceToolGateway.validate_material_paths(
                [resolve_mounted_path(readable, locator) for locator in policy.materials], policy,
                excluded_roots=(Path(root) / 'workspace', Path(root) / 'attempts'))
            if policy.source_workspace and not resolve_mounted_path(readable, policy.source_workspace).is_dir():
                raise ValueError('Research source_workspace must name an authorized project directory')
            research_policy = policy.model_dump(mode='json')
        if table_analysis is not None:
            from types import SimpleNamespace
            from simple_ar.tools.analysis import validate_table_analysis_contract
            from simple_ar.tools.workspace import resolve_mounted_path
            table_analysis = validate_table_analysis_contract(SimpleNamespace(resolve=lambda value:
                resolve_mounted_path({name: Path(path) for name, path in mounts.items()}, value)), table_analysis)
        if code_task is not None:
            from simple_ar.tools.scientific import measurement_conditions
            conditions = measurement_conditions(execution_contract) if execution_contract is not None else []
            if any(row['execution'].get('environment') is not None for row in conditions):
                raise ValueError('CodeTask preparation owns its environment; do not replace it in measurement')
            if execution_contract is not None and 'conditions' in execution_contract and any('source' not in row for row in conditions):
                raise ValueError('Scoped conditions require explicit preparation/implementation/declared source ownership')
            code_task = ExactCodeTask.model_validate(code_task).model_dump()
            if not allow_execution or (code_task['project_path'] and code_task['project_path'] not in mounts):
                raise ValueError('Exact CodeTask requires a mounted project and execution proposals')
            if code_task['project_path'] and not Path(mounts[code_task['project_path']]).is_dir():
                raise ValueError('Exact CodeTask project must be a directory')
            if (code_task['project_path'] and any(row.get('source', 'implementation') != 'declared'
                    and Path(row['execution']['cwd']).resolve() != Path(mounts[code_task['project_path']]).resolve()
                    for row in conditions)):
                raise ValueError('CodeTask measurement origin must match its explicitly mounted project')
        elif execution_contract is not None:
            from simple_ar.tools.scientific import measurement_conditions
            if any(row.get('before_implementation') for row in measurement_conditions(execution_contract)):
                raise ValueError('Pre-implementation conditions require a frozen code task')
        if report_requirement is not None:
            from simple_ar.report.requirements import validate_report_requirement
            report_requirement = validate_report_requirement(report_requirement)
        # Preflight connection/protocol before creating a session or making a call.
        dependencies = _runtime_bindings(services)
        hints = model_catalogs or {}
        selected_catalogs = {}

        def remember_catalog(purpose: str) -> None:
            # Historical consumers without a hint keep their original contract.
            # A location never substitutes for the caller's binding checks.
            if not retained or purpose in hints:
                path = _tool_app_module().catalog_path(fallback_path=hints.get(purpose))
                if path is not None:
                    selected_catalogs[purpose] = str(path.resolve())

        client = getattr(services, 'llm_client', None)
        if client is not None and model is None:
            raise ValueError('An injected model still requires explicit model resource authorization')
        if client is None:
            client = LLMClient.from_env(None if model == 'env' else model,
                fallback_models_config=hints.get('task')) if model is not None else None
            if client is not None and client.connection_binding():
                remember_catalog('task')
        if 'llm_client' in dependencies and allow_vision:
            raise ValueError('Injected text clients do not authorize a named vision route')
        if research_policy is not None and getattr(services, 'search_registry', None) is not None:
            if missing := set(research_policy['providers']) - set(services.search_registry.names()):
                raise ValueError('Injected registry lacks declared providers: ' + ', '.join(sorted(missing)))
        if max_output_tokens is not None:
            limiter = getattr(client, 'with_output_limit', None)
            if not callable(limiter):
                raise ValueError('Injected client cannot honor an explicit output limit')
            limiter(max_output_tokens)  # Validate before session mutation.
        named_client = client is not None and ('llm_client' not in dependencies or isinstance(client, LLMClient))
        if named_client and client._settings.api_mode != "chat":
            raise ValueError("Native tools currently require a named Chat connection; no protocol fallback")
        feasibility_connection = None
        if research_policy and research_policy.get('feasibility_connection'):
            if 'feasibility_llm_client' in dependencies:
                raise ValueError('Choose a named feasibility connection or an injected reviewer, not both')
            if client is None:
                raise ValueError('Independent feasibility review requires model authorization')
            feasibility_connection = LLMClient.from_env(research_policy['feasibility_connection'],
                fallback_models_config=hints.get('feasibility')).connection_binding()
            remember_catalog('feasibility')
        vision_connection = None
        if allow_vision:
            vision_connection = cls._vision_binding(LLMClient.from_env(purpose="vision",
                fallback_models_config=hints.get('vision')))
            remember_catalog('vision')
        if max_images:
            from simple_ar.integrations.model_profiles import load_model_catalog
            catalog = load_model_catalog(fallback_path=hints.get('image'))
            if catalog is None:
                raise ValueError("Image operations require a named image route")
            _, profile = catalog.select(purpose="image")
            profile.credential()
            profile.resolve_proxy()
            remember_catalog('image')
        config = {"model": model, "tool_contracts": tool_contracts,
            "publication_assets": PUBLICATION_CONSUMER_PROTOCOL,
            "model_connection": client.connection_binding() if named_client else None,
            "vision_connection": vision_connection,
            "project_python": str(project_python) if project_python else None,
            "sources": mounts, "allow_execution": allow_execution, "allow_network": allow_network,
            "allow_vision": allow_vision, "scientific_tools": True, "scientific_lineage": True,
            "execution_contract": execution_contract,
            "research_policy": research_policy, "table_analysis": table_analysis,
            **({'runtime_dependencies': dependencies} if dependencies else {}),
            "feasibility_connection": feasibility_connection,
            "max_images": max_images,
            "completion_policy": completion_policy,
            "max_turns": max_turns}
        if control_protocol is not None:
            config['control_protocol'] = control_protocol
        if sandbox_execution:
            config['sandbox_execution'] = True
        if frozen_extensions:
            config['extensions'] = frozen_extensions
            config['extension_host_protocol'] = 'nested_calls.v1'
        if creation_declarations is not None:
            config['input_contract'] = _DECLARED_INPUTS
            config['creation_declarations'] = _CreationDeclarations.model_validate(creation_declarations).freeze(mounts)
        if selected_catalogs or any(hints.values()):
            config['model_catalogs'] = selected_catalogs
        if code_task is not None:
            config['code_task'] = code_task
        if report_requirement is not None:
            config['report_requirement'] = report_requirement
            config['report_evidence_contract'] = (
                'execution.v1' if code_task is not None or execution_contract is not None
                    or allow_execution and (research_policy or {}).get('require_design') else 'materials.v1')
        if report_protocol is not None:
            config['report_protocol'] = report_protocol
        if delivery_files is not None:
            config['delivery_files'] = delivery_files
        if independent_review_required:
            config['independent_review_required'] = True
        if snapshots:
            config['source_snapshots'] = {}
            for name, original, content in snapshots:
                relative = f'evidence/inputs/{name}/{original.name}'
                if (Path(root) / relative).exists():
                    raise ValueError('Retained source destination already exists')
                reused_inputs.append((relative, content))
                config['sources'][name] = str(Path(root).resolve() / relative)
                config['source_snapshots'][name] = {'original_path': str(original),
                    'sha256': hashlib.sha256(content).hexdigest(), 'size_bytes': len(content)}
        origin = config.get('creation_declarations', {}).get('reuse_origin', {})
        if origin.get('version') is not None and not retained:
            from simple_ar.tools.delivery_versions import DeliveryVersions
            alias = 'retained_delivery'
            if alias in config['sources']:
                raise ValueError('retained_delivery is reserved for selected historical input')
            selected, inputs = DeliveryVersions(Path(origin['session'])).snapshot(
                origin['version'], origin['results'], max_bytes=max_snapshot_bytes, working_context=True)
            relative = 'evidence/inputs/' + alias
            if (Path(root) / relative).exists():
                raise ValueError('Retained input destination already exists')
            reused_inputs.extend((relative + '/' + path, content) for path, content in inputs)
            config['sources'][alias] = str(Path(root).resolve() / relative)
            config.setdefault('source_snapshots', {})[alias] = selected
            declarations = config['creation_declarations']
            declarations.setdefault('material_roles', {}).setdefault('material', []).append(alias)
            if selected.get('project_root') is not None:
                declarations['material_roles'].setdefault('project', []).append(alias + '/' + selected['project_root'])
            origin['prior_drafts'] = [alias + '/' + path for path in selected['groups'].get('report', [])]
        if max_output_tokens is not None:
            config['max_output_tokens'] = max_output_tokens
        return config, reused_inputs, _ToolConfigMixin.creation_limits(max_turns, max_tokens, max_images,
            process_limits=process_limits, source_operations=source_operations), attempt_limit

    @staticmethod
    def creation_limits(max_turns: int | None, max_tokens: int | None, max_images: int, *,
                        process_limits: dict | None = None, source_operations: int | None = None) -> dict:
        """Resolve the resource defaults for a new task's physical ledger."""
        return {"llm_requests": max_turns, "total_tokens": max_tokens,
            # One model turn can contain multiple bounded tool operations.
            # Search backends share process receipts, but must not consume a
            # one-process-per-model-turn allowance before validation starts.
            "process_invocations": max_turns * 10 if max_turns is not None else None,
            "process_wall_seconds": max_turns * 300 if max_turns is not None else None,
            "source_operations": source_operations if source_operations is not None else
                max_turns * 3 if max_turns is not None else None,
            "image_requests": max_images, **(process_limits or {})}

    @classmethod
    def preview_creation(cls, root: Path, goal: str, **options: Any) -> tuple[dict[str, Any], list, dict[str, Any], int]:
        """Resolve the same creation contract as create(), without writing a session.

        The returned configuration and resources are a preview, not permission
        or a persistent task. Creation resolves them again before admission.
        """
        professional_inputs = options.pop('professional_inputs', False)
        investigate_results = options.pop('investigate_results', True)
        context_protocol = options.pop('context_protocol', 'auto')
        control_protocol = options.setdefault('control_protocol', 'operations.v1')
        if control_protocol != 'operations.v1':
            raise ValueError('New tasks require control_protocol="operations.v1"; use load() for a saved historical task')
        if type(professional_inputs) is not bool or type(investigate_results) is not bool:
            raise ValueError('professional_inputs and investigate_results must be boolean')
        if professional_inputs or not investigate_results or options.get('report_protocol') is not None:
            raise ValueError('Professional controller options are load-only; new tasks use source operations, inspect_artifacts and export_report')
        automatic_memory = context_protocol == 'auto'
        if automatic_memory:
            context_protocol = 'working.v4'
        if context_protocol not in _CONTEXT_PROTOCOLS:
            raise ValueError('Unsupported context protocol')
        if options.get('creation_declarations') is not None and context_protocol not in _CONVERSATION_CONTEXT_PROTOCOLS:
            raise ValueError('Declared inputs require a journal-backed conversation context')
        config, reused, limits, attempts = cls._configuration(root, goal, **options)
        if config.get('research_policy') is None:
            # New unconfigured tasks can collect the available source set in
            # one operation. Saved tasks retain their original default policy.
            config['source_policy_defaults'] = {'max_results': 20, 'max_documents': 20}
        # Keep independent condition reuse with the scientific owner regardless
        # of the task's model prompt or working-memory selection.
        config['scientific_condition_reuse'] = 'unchanged.v1'
        config['source_observation_reuse'] = 'additive.v1'
        if config.get('sandbox_execution'):
            # New specialized actions can adopt the same task execution scope.
            # Saved tasks without this contract retain their original approval.
            config['sandbox_operations'] = 'workspace.v1'
        if config.get('code_task') is not None:
            config['code_work_policy'] = 'shared.v2'
            preparation = config['code_task'].get('preparation') or {}
            config['code_implementation_policy'] = 'direct.v2' if preparation.get('kind') == 'greenfield' else 'direct.v1'
            # A required whole-delivery check owns the advisory model review;
            # deterministic patch and validation checks remain in CodeTask.
            config['code_review_policy'] = 'delivery.v2' if preparation.get('kind') == 'greenfield' else 'delivery.v1'
        if config.get('code_task') is not None or config.get('report_requirement') is not None:
            # Structured analysis/measurement tasks need the same frozen
            # report requirements at the actual document-work consumer.
            config['document_context_policy'] = 'working.v4'
        config['inspection_protocol'] = 'subjects.v3'
        config['inspection_execution_protocol'] = 'shared.v1'
        config['inspection_coverage_policy'] = 'offered.v2'
        if config['model'] is not None:
            config['delivery_review_protocol'] = 'task.v5'
            config['inspection_finding_policy'] = 'linked.v1'
            if config.get('control_protocol') == 'operations.v1':
                config['delivery_review_selection'] = 'model.v3'
                if config.get('code_task') is not None and not config.get('independent_review_required'):
                    config['code_review_policy'] = None
        from simple_ar.app.operation_tools import CURRENT_PROFESSIONAL_CONTRACT
        config['professional_operations'] = 'work.v5'
        if context_protocol is not None:
            config['context_protocol'] = context_protocol
        if context_protocol in _WORKING_CONTEXT_PROTOCOLS:
            config['context_authority'] = 'sources.v1'
        config['native_supply_policy'] = TURN_SUPPLY_POLICY
        if automatic_memory:
            config['memory_protocol'] = 'journal.v1'
            config['context_checkpoint_policy'] = 'economic.v3'
            config['work_method'] = (_tool_app_module().CURRENT_WORK_METHOD if not any(config.get(key) for key in
                ('code_task', 'execution_contract', 'table_analysis', 'source_generation_protocol')) else 'execute.v3')
            if config['work_method'] == GOAL_WORK_METHOD:
                if config['model'] is not None:
                    config['professional_operations'] = CURRENT_PROFESSIONAL_CONTRACT
                    from simple_ar.app.professional_conversation import NATIVE_HISTORY_POLICY
                    config['native_history_policy'] = NATIVE_HISTORY_POLICY
                    config['document_context_policy'] = 'working.v4'
                    config['process_artifact_paths'] = 'cwd.v1'
                    config['work_lifecycle'] = 'integrated.v3'
                    config['inspection_execution_protocol'] = 'shared.v2'
                    config['candidate_review_protocol'] = 'candidate.v1'
                    config['working_set_policy'] = 'recent.v6'
                config['inspection_material_policy'] = 'operations.v3'
                config['work_selection_policy'] = ('native.v5'
                    if config.get('work_lifecycle') == 'integrated.v3' else 'native.v3')
        return config, reused, limits, attempts

    @classmethod
    def create(cls, root: Path, goal: str, *, model: str | None, sources: dict[str, Path],
               allow_execution: bool = False, sandbox_execution: bool = False,
               allow_network: bool = False, allow_vision: bool = False, max_turns: int | None = DEFAULT_MAX_TURNS,
               max_tokens: int | None = DEFAULT_MAX_TOKENS, max_images: int = 0,
               project_python: Path | None = None, code_task: dict[str, Any] | None = None,
               process_limits: dict[str, int | float] | None = None,
               report_requirement: dict[str, Any] | None = None, report_protocol: str | None = None,
               research_policy: dict[str, Any] | None = None,
               table_analysis: dict[str, Any] | None = None,
               max_output_tokens: int | None = None,
               max_attempts: int | None = None,
               source_operations: int | None = None,
               execution_contract: dict[str, Any] | None = None,
               services: Any | None = None, snapshot_sources: bool = False,
               extensions: list[Path] | None = None,
               max_snapshot_bytes: int = 16 * 1024 * 1024,
               delivery_files: list[str] | None = None,
               independent_review_required: bool | None = None,
               tool_contracts: Literal['full', 'on_demand', 'outcomes'] = 'on_demand',
               completion_policy: str | None = None, professional_inputs: bool = False,
               investigate_results: bool = True,
               preparation_ledger: dict[str, Any] | None = None,
               control_protocol: Literal['operations.v1'] = 'operations.v1',
               creation_declarations: dict[str, Any] | None = None,
               verify_creation: Callable[[tuple[dict[str, Any], list, dict[str, Any], int]], None] | None = None,
               initialize_session: Callable[[SessionController], None] | None = None,
               context_protocol: Literal['auto', 'conversation.v1', 'working.v1', 'working.v2', 'working.v3', 'working.v4'] | None = 'auto') -> ToolApplication:
        """Create work on the operation-composed loop.

        Historical professional controllers are load-only. Loading and supply
        retain their saved contracts, approvals and ledger without migration.
        """
        prepared = cls.preview_creation(root, goal, model=model, sources=sources,
            allow_execution=allow_execution, sandbox_execution=sandbox_execution,
            allow_network=allow_network, allow_vision=allow_vision,
            max_turns=max_turns, max_tokens=max_tokens, max_images=max_images, project_python=project_python,
            code_task=code_task, process_limits=process_limits, report_requirement=report_requirement, report_protocol=report_protocol,
            research_policy=research_policy, table_analysis=table_analysis, max_output_tokens=max_output_tokens,
            max_attempts=max_attempts, source_operations=source_operations, execution_contract=execution_contract,
            services=services, snapshot_sources=snapshot_sources, max_snapshot_bytes=max_snapshot_bytes,
            tool_contracts=tool_contracts, completion_policy=completion_policy, control_protocol=control_protocol,
            delivery_files=delivery_files, independent_review_required=independent_review_required,
            extensions=extensions, creation_declarations=creation_declarations,
            professional_inputs=professional_inputs, investigate_results=investigate_results,
            context_protocol=context_protocol)
        if verify_creation is not None:
            verify_creation(deepcopy(prepared))
        config, reused, limits, attempts = prepared
        ledger = (BudgetLedger.from_dict({**preparation_ledger, 'limits': {**preparation_ledger['limits'], **limits}})
                  if preparation_ledger is not None else BudgetLedger(limits))
        from simple_ar.core.artifacts import write_atomically
        app: ToolApplication

        def initialize(session: SessionController) -> None:
            nonlocal app
            for relative, content in reused:
                write_atomically(session.store.resolve(relative), content)
            session.manifest.state_refs['actions'] = session.store.write_json('actions.json',
                {'config': config, 'requests': [goal], 'turns': [], 'pending': None}, kind='native_actions')
            if initialize_session is not None:
                initialize_session(session)
            app = cls(session, services=services)
            app.state['config']['tool_completions'] = app.gateway.registry.completions()
            session.store.write_json('actions.json', app.state)

        SessionController.create(root, session_id=root.name, topic=goal,
            registry=CapabilityRegistry(), budget=BudgetState(max_attempts=attempts, max_no_progress=6),
            transition_recipe=cls.RECIPE, budget_ledger=ledger, initialize=initialize)
        return app

    @classmethod
    def load(cls, root: Path, *, services: Any | None = None) -> ToolApplication:
        return cls(SessionController.load(root, registry=CapabilityRegistry()), services=services)

    @staticmethod
    def creation_binding(prepared: tuple) -> tuple:
        """Stable creation evidence; owned snapshot destinations depend on the new task root."""
        config, inputs, limits, attempts = deepcopy(prepared)
        for alias in config.get('source_snapshots', {}):
            config['sources'][alias] = 'retained-input:' + alias
        return config, inputs, limits, attempts

    def _save(self) -> None:
        self.session.store.write_json("actions.json", self.state)
        self.session.save()

    def retry_experiment(self, *, command: tuple[str, ...], cwd: str | Path | None = None,
                         timeout_sec: int, result_schema: dict[str, object] | None = None,
                         label: str = 'experiment-retry', parent_attempt_id: str | None = None,
                         reason: str | None = None,
                         progress: Callable[[str], None] = print) -> dict[str, Any]:
        """Run one caller-specified correction through the existing scientific owner."""
        from simple_ar.app.research_execution import merge_result_schema
        from simple_ar.tools.scientific import measurement_conditions, measurement_contract

        if self.operations is None or self.scientific is None:
            raise ValueError('This task has no common scientific execution owner')
        source = self.scientific.retry_source(self._operation_observations(domain='scientific'),
            parent_attempt_id=parent_attempt_id,
            allow_reuse=self.state['config'].get('scientific_condition_reuse') == 'unchanged.v1')
        parent = source['parent_attempt_id']
        revised = json.loads(json.dumps(source['contract']))
        condition_id = source['condition_id']
        execution = (next(row['execution'] for row in revised['conditions'] if row['id'] == condition_id)
            if condition_id else revised)
        run_cwd = str(Path(cwd if cwd is not None else source['source_cwd'] or execution['cwd']).expanduser().absolute())
        if source['source']:
            if run_cwd != str(Path(source['source_cwd']).absolute()):
                raise ValueError('The corrected measurement must use its retained source workspace')
            execution.update(command=list(command), timeout_sec=timeout_sec)
        else:
            execution.update(command=list(command), cwd=run_cwd, timeout_sec=timeout_sec)
        if label.strip():
            execution['label'] = label.strip()
        if result_schema:
            execution['result_schema'] = merge_result_schema(execution.get('result_schema'), result_schema)
        revised = measurement_contract(revised)
        arguments = ({**next(row['execution'] for row in revised['conditions'] if row['id'] == condition_id),
            'condition_id': condition_id} if condition_id else revised)
        if source['source']:
            arguments = {**arguments, 'cwd': run_cwd, **source['source']}
        reason = reason if reason is not None else f'Retry the failed scientific measurement {parent} using the caller-supplied command.'
        if not reason.strip():
            raise ValueError('An experiment retry needs a nonempty reason')
        supplies = self.state.get('supplies', [])
        supplied = (bool(supplies) and supplies[-1].get('reason') == reason
            and supplies[-1].get('changes') == {'execution_contract': revised}
            and supplies[-1].get('request_count') == len(self.state['requests']))
        if not source['contract_current'] and not supplied:
            raise ValueError('The latest measurement is not part of the current execution contract')

        pending = self.state.get('pending')
        queued = any(turn['remaining'] for turn in self.state['turns'])
        if pending or queued:
            turns = self.state['turns']
            turn = turns[-1] if turns else {}
            remaining = turn.get('remaining', [])
            call = remaining[0] if len(remaining) == 1 else None
            if (not supplied or turn.get('origin') != 'caller' or call is None
                    or call.get('function', {}).get('name') != 'measure_experiment'
                    or json.loads(call['function']['arguments']) != arguments
                    or turn.get('parents', {}).get(call['id']) != parent
                    or pending and (pending.get('kind') != 'tool' or pending.get('call') != call)):
                raise ValueError('Reconcile the existing task action before submitting an experiment retry')
        elif not supplied:
            self.supply(supply_id=f'retry-{parent}-{len(supplies) + 1}', reason=reason,
                changes={'execution_contract': revised}, replace_existing=True)

        def approve(call: dict[str, Any], _token: str) -> bool:
            return (call['function']['name'] == 'measure_experiment'
                and json.loads(call['function']['arguments']) == arguments
                and self.state['turns'][-1].get('origin') == 'caller')

        def stop_after_caller_action() -> bool:
            return self.state['pending'] is None and not any(turn['remaining'] for turn in self.state['turns'])

        snapshot = self.run(actions=None if pending or queued else [
            {'tool_name': 'measure_experiment', 'arguments': arguments, 'parent_attempt_id': parent}], approve=approve,
            should_stop=stop_after_caller_action, progress=progress)
        turn = self.state['turns'][-1] if self.state['turns'] else {}
        observed = (self.session.store.read_json(turn['tools'][-1]['result'])
            if turn.get('origin') == 'caller' and turn.get('tools') else None)
        analysis = None
        missing_conditions = []
        data = (observed or {}).get('data') or {}
        if observed and observed.get('status') == 'ok' and data.get('execution_status') == 'passed':
            refs = self.scientific.measurement_refs(self._operation_observations(domain='scientific'))
            selected = self.scientific.condition_results(refs)
            missing_conditions = [row['id'] for row in measurement_conditions(revised) if row['id'] not in selected]
            if not missing_conditions and not self.session.manifest.budget.exhausted():
                snapshot = self.run(actions=[{'tool_name': 'analyze_measurements',
                    'arguments': self.scientific.analysis_arguments(refs)}],
                    should_stop=stop_after_caller_action, progress=progress)
                turn = self.state['turns'][-1]
                analysis = (self.session.store.read_json(turn['tools'][-1]['result'])
                    if turn.get('origin') == 'caller' and turn.get('tools') else None)
        return {'parent_attempt_id': parent, 'observation': observed, 'analysis': analysis,
            'missing_conditions': missing_conditions, 'task': snapshot}

    def current_report_requirement(self) -> dict[str, Any] | None:
        """Return the saved or declared report contract for its actual consumers."""
        saved = self.state['config'].get('report_requirement')
        if saved is None:
            projection = self._report_projection()
            saved = projection['requirement'] if projection['kind'] == 'report' else None
        return deepcopy(saved) if saved is not None else None

    def report_revision(self, settings: dict[str, Any], *, refresh: bool = False,
                        method: str | None = None) -> dict[str, Any] | None:
        """Resolve a requested report against the retained Writer contract."""
        from simple_ar.report.requirements import freeze_report_requirement

        config = self.state['config']
        if config.get('model') is None:
            raise ValueError('Bind a model and its budget to the saved task before requesting a report')
        saved = self.current_report_requirement()
        if saved is None:
            requirement = freeze_report_requirement(settings, method=method or 'sections')
        else:
            if method is not None and method != saved['method']:
                raise ValueError('The saved report keeps its writing method and checkpoint owner')
            merged = {**saved['config'], **settings}
            if merged == saved['config']:
                requirement = saved
            else:
                template = saved['template'] if merged.get('template') == saved['config'].get('template') else None
                requirement = freeze_report_requirement(merged, method=saved['method'], template=template)
        delivery = (self.session.store.read_json('delivery.json')
                    if self.session.store.exists('delivery.json') else {})
        report = delivery.get('report') or {}
        # Earlier native tasks enforced their saved report requirement before
        # completion, including deliveries without an explicit report field.
        delivered = bool(self.operations is None and saved and self.session.manifest.status == 'completed' and delivery
                         and not delivery.get('unresolved')) or bool(
            report.get('manuscript') and report.get('status') in {'candidate', 'canonical'}
            and not delivery.get('unresolved'))
        return (requirement if saved != requirement or refresh
            or self.session.manifest.status in {'completed', 'partial'} and not delivered else None)

    def request_report(self, settings: dict[str, Any], *, refresh: bool = False) -> bool:
        """Add or revise a report in the saved task; do not run it here."""
        requirement = self.report_revision(settings, refresh=refresh)
        if requirement is None:
            return False
        if self.state['pending'] is not None:
            raise ValueError('Finish or reconcile the pending task operation before changing its report request')
        self.supply(supply_id=f"research-report-{len(self.state['requests']) + 1}",
            reason='Generate a report from the retained research and results. Reuse existing work; '
                   'do not rerun the experiments merely to prepare the report.',
            changes={'report_requirement': requirement}, replace_existing=self.state['config'].get('report_requirement') is not None)
        return True

    @classmethod
    def _feedback_journal(cls, root: Path):
        from simple_ar.core.feedback import FeedbackSubmissions
        store = ArtifactStore(root)
        manifest = store.read_json('session_manifest.json')
        if (manifest.get('transition_recipe') != cls.RECIPE
                or manifest.get('schema_version') != 'session_manifest.v2'):
            raise ValueError('Feedback reception requires a writable native task; retain its historical recovery command')
        store.read_json('actions.json')  # Do not create a task by accepting text.
        return FeedbackSubmissions(store)

    @classmethod
    def submit_feedback(cls, root: Path, *, submission_id: str, feedback: str,
                        independent_review_required: bool | None = None) -> dict:
        """Receive exact text once, including while run() owns the execution lock.

        Reuse the same id, text and review choice on transport retry; conflicting
        terms raise ValueError without replacing the original. This never starts or
        retries work, changes access, or reserves resources. ``status=pending``
        acknowledges receipt only; run() adopts it at a safe action boundary.
        """
        journal = cls._feedback_journal(root)
        row = journal.accept(submission_id, feedback, independent_review_required=independent_review_required)
        return journal.receipt(row, journal.store.read_json('actions.json'))

    @classmethod
    def feedback_status(cls, root: Path, *, submission_id: str) -> dict:
        """Read a submission's pending/applied receipt without loading a model."""
        journal = cls._feedback_journal(root)
        for row in journal.records():
            if row['submission_id'] == submission_id:
                return journal.receipt(row, journal.store.read_json('actions.json'))
        raise ValueError('Unknown feedback submission_id; no receipt is recorded')

    def _apply_feedback(self, feedback: str, *, submission_id: str | None = None,
                        independent_review_required: bool | None = None) -> bool:
        """Adopt a request through the original cancellation/lifecycle boundary."""
        try:
            if self.state.get('pending_supply') is not None:
                raise ValueError('Complete the retained caller supply before adopting feedback')
            if self.state['pending'] and self.state['pending']['kind'] == 'model':
                raise ValueError('Unreconciled model operation; feedback cannot silently replay it')
            config = (_review_requirement(self.state['config'], independent_review_required)
                if independent_review_required is not None else self.state['config'])
            self._check_cancellation()
            if self.session.manifest.status not in {'created', 'running'}:
                self.session.continue_with_revision(feedback)
        except (ValueError, RuntimeError) as exc:
            if submission_id is None:
                raise  # Preserve the legacy string entry's explicit refusal.
            waiting = {'status': 'pending', 'reason': str(exc)}
            if self.state.get('feedback_submissions', {}).get(submission_id) != waiting:
                self.state.setdefault('feedback_submissions', {})[submission_id] = waiting
                self._save()
            return False
        # IO failures and errors committing request/adoption must propagate;
        # they are not policy refusals and cannot be marked as adopted earlier.
        self._cancel_unstarted('Unstarted action cancelled by new user feedback')
        changed_config = config != self.state['config']
        self.state['config'] = config
        self.state['requests'].append(feedback)
        if submission_id is not None:
            self.state.setdefault('feedback_submissions', {})[submission_id] = {
                'status': 'applied', 'request_index': len(self.state['requests']) - 1,
                **({'independent_review_required': independent_review_required}
                   if independent_review_required is not None else {})}
        self._save()
        if changed_config:
            self._configure_tools()
        return True

    def _consume_feedback(self) -> bool:
        """Execution owner only: reconcile started work before adopting input.

        Reception and adoption have separate locks and never write each other's
        files. Requests and their adoption markers share one atomic actions.json
        write, so reloading after a crash cannot append an input a second time.
        """
        from simple_ar.core.feedback import FeedbackSubmissions
        journal = FeedbackSubmissions(self.session.store)
        applied = False
        for row in journal.records():
            if journal.receipt(row, self.state)['status'] == 'applied':
                continue
            if not self._apply_feedback(row['feedback'], submission_id=row['submission_id'],
                    independent_review_required=row.get('independent_review_required')):
                break  # Arrival order is preserved, including deferred input.
            applied = True
        return applied

    def supply(self, *, supply_id: str, reason: str, changes: dict[str, Any],
               resource_allowances: dict[str, int | float] | None = None,
               replace_existing: bool = False, additional_attempts: int | None = None,
               additional_no_progress: int | None = None) -> dict[str, Any]:
        """Record caller contracts, optionally replacing them with explicit intent.

        Ordinary supply only fills missing contracts. Explicit replacement keeps
        old facts and approvals as history, not authority for the new conditions.
        The single actions journal owns this supply and its previous config;
        resources use the original idempotent ledger authorization, not a reset.
        """
        with self.session.mutation_scope():
            if (not isinstance(supply_id, str) or not supply_id.strip()
                    or not isinstance(reason, str) or not reason.strip()
                    or not isinstance(changes, dict) or not changes or type(replace_existing) is not bool):
                raise ValueError('Supply requires a stable id, reason, explicit contracts and a boolean replacement choice')
            for value in (additional_attempts, additional_no_progress):
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError('Additional attempt allowances must be non-negative integers')
            supply_id, reason = supply_id.strip(), reason.strip()
            fields = {'model', 'sources', 'allow_execution', 'sandbox_execution', 'allow_network', 'code_task',
                'execution_contract', 'research_policy', 'table_analysis', 'report_requirement', 'max_output_tokens',
                'tool_contracts', 'completion_policy', 'delivery_files', 'document_source_contract',
                'independent_review_required'}
            if set(changes) - fields:
                raise ValueError('Unsupported supply fields: ' + ', '.join(sorted(set(changes) - fields)))
            supplied = dict(changes)
            if 'document_source_contract' in supplied:
                if supplied['document_source_contract'] != 'document_source.v1':
                    raise ValueError('Unsupported document work source contract')
                if self.state['pending'] is not None or any(turn['remaining'] for turn in self.state['turns']):
                    raise ValueError('Finish or explicitly cancel accepted work before changing its document source contract')
            if 'sources' in supplied:
                if not isinstance(supplied['sources'], dict):
                    raise ValueError('Supply sources must map aliases to read-only paths')
                if any(not isinstance(path, (str, Path)) or not str(path).strip()
                        for path in supplied['sources'].values()):
                    raise ValueError('Supply sources require nonempty paths, not alias removal')
                supplied['sources'] = {name: str(Path(path).expanduser().absolute())
                    for name, path in supplied['sources'].items()}
            supplied = json.loads(json.dumps(supplied, ensure_ascii=False, allow_nan=False))
            terms = {'supply_id': supply_id, 'reason': reason, 'changes': supplied,
                'resource_allowances': dict(resource_allowances or {})}
            if additional_attempts is not None:
                terms['additional_attempts'] = additional_attempts
            if additional_no_progress is not None:
                terms['additional_no_progress'] = additional_no_progress
            if replace_existing:
                terms['replace_existing'] = True
            interrupted = self.state.get('pending_supply')
            if interrupted is not None and (interrupted.get('replace_existing', False) != replace_existing
                    or any(interrupted.get(key) != value for key, value in terms.items())):
                raise ValueError('Finish the retained supply with its original id and terms before supplying different contracts')
            previous = next((row for row in self.state.get('supplies', []) if row['supply_id'] == supply_id), None)
            if previous is not None:
                if (previous.get('replace_existing', False) != replace_existing
                        or any(previous.get(key) != value for key, value in terms.items())):
                    raise ValueError('Supply id already exists with different terms')
                return previous
            self._check_authorization_operations()
            pending = self.state['pending']
            if pending and (pending['kind'] == 'model' or any(
                    row.attempt_id == pending['attempt'] for row in self.session.list_attempts())):
                raise ValueError('Reconcile the started operation before supplying contracts')
            old = json.loads(json.dumps(self.state['config']))
            if (supplied.get('independent_review_required') is True
                    and old.get('delivery_review_protocol') not in {'task.v3', 'task.v4', 'task.v5'}):
                raise ValueError('This saved task has no shared whole-delivery review contract')
            if _runtime_bindings(self.services) != old.get('runtime_dependencies', {}):
                raise ValueError('Restore unchanged runtime dependencies before supplying new contracts')
            for name, value in supplied.items():
                if name == 'sources':
                    if not replace_existing and any(alias in old['sources'] and old['sources'][alias] != path for alias, path in value.items()):
                        raise ValueError('Supply cannot replace an existing source alias')
                elif name in {'allow_execution', 'sandbox_execution', 'allow_network'}:
                    if type(value) is not bool or not replace_existing and old.get(name, False) and not value:
                        raise ValueError('Supply access is an explicit additional grant, not revocation')
                elif name == 'model' and old.get(name) is not None and old[name] != value:
                    raise ValueError(f'{name} is already frozen; use its existing feedback/revision contract')
            candidate = {**old, **supplied,
                'sources': supplied['sources'] if replace_existing and 'sources' in supplied else
                    {**old['sources'], **supplied.get('sources', {})}}
            allowances = terms['resource_allowances']
            first_model = old['model'] is None and candidate['model'] is not None
            hints = dict(old.get('model_catalogs', {}))
            # New grants may record their first location; retained consumers
            # without hints must not be migrated as a side effect of supply.
            for purpose, newly_authorized in (
                    ('task', first_model),
                    ('vision', candidate.get('allow_vision') and not old.get('allow_vision')),
                    ('image', candidate.get('max_images') and not old.get('max_images')),
                    ('feasibility', (candidate.get('research_policy') or {}).get('feasibility_connection')
                        and not (old.get('research_policy') or {}).get('feasibility_connection'))):
                if newly_authorized:
                    hints.setdefault(purpose, old.get('model_catalogs', {}).get('task'))
            if first_model and (type(allowances.get('llm_requests')) is not int or allowances['llm_requests'] < 1
                    or type(allowances.get('total_tokens')) is not int or allowances['total_tokens'] < 1):
                raise ValueError('First model binding requires explicit positive llm_requests and total_tokens allowances')
            config, reused, _, _ = self._configuration(self.session.store.root, self.state['requests'][0],
                model=candidate['model'], sources={key: Path(value) for key, value in candidate['sources'].items()},
                allow_execution=candidate['allow_execution'], sandbox_execution=candidate.get('sandbox_execution', False),
                allow_network=candidate.get('allow_network', False),
                allow_vision=candidate.get('allow_vision', False), max_images=candidate.get('max_images', 0),
                max_turns=allowances['llm_requests'] if first_model else (
                    max(1, candidate['max_turns']) if candidate['max_turns'] is not None else None),
                max_tokens=allowances['total_tokens'] if first_model else (
                    max(1, self.ledger.limits['total_tokens']) if self.ledger.limits['total_tokens'] is not None else None),
                project_python=Path(candidate['project_python']) if candidate.get('project_python') else None,
                code_task=candidate.get('code_task'), execution_contract=candidate.get('execution_contract'),
                research_policy=candidate.get('research_policy'), table_analysis=candidate.get('table_analysis'),
                report_requirement=candidate.get('report_requirement'), report_protocol=old.get('report_protocol'),
                max_output_tokens=candidate.get('max_output_tokens'),
                max_attempts=self.session.manifest.budget.max_attempts, retained=True, services=self.services,
                tool_contracts=candidate.get('tool_contracts', 'full'),
                completion_policy=candidate.get('completion_policy', 'protected_decision.v1'),
                delivery_files=candidate.get('delivery_files'), model_catalogs=hints,
                independent_review_required=candidate.get('independent_review_required', False),
                control_protocol=old.get('control_protocol'),
                creation_declarations=old.get('creation_declarations') if old.get('input_contract') == _DECLARED_INPUTS else None)
            if 'sandbox_execution' in supplied and not supplied['sandbox_execution']:
                config['sandbox_execution'] = False
            if old.get('professional_inputs') is not None:
                config['professional_inputs'] = old['professional_inputs']
            if old.get('control_protocol') is not None:
                config['control_protocol'] = old['control_protocol']
            if 'context_protocol' in old:
                config['context_protocol'] = old['context_protocol']
            if 'professional_work' in old:
                config['professional_work'] = old['professional_work']
            if candidate.get('document_source_contract') is not None:
                config['document_source_contract'] = candidate['document_source_contract']
            if 'producer_completion' in old:
                config['producer_completion'] = old['producer_completion']
            if 'result_investigation' in old:
                config['result_investigation'] = old['result_investigation']
            if 'independent_review_required' in supplied:
                # Explicit replacement with false must also override the old
                # value when the final configuration is merged with history.
                config['independent_review_required'] = supplied['independent_review_required']
            for name in fields - {'model', 'sources', 'allow_execution', 'sandbox_execution', 'allow_network'}:
                if not replace_existing and old.get(name) is not None and old[name] != config.get(name):
                    raise ValueError(f'{name} is already frozen; use its existing feedback/revision contract')
                if name in supplied and supplied[name] is None:
                    config[name] = None  # Explicit removal must not resurrect the old optional contract on merge.
            if 'report_requirement' in supplied and supplied['report_requirement'] is None:
                config['report_evidence_contract'] = None
            elif (old.get('report_requirement') is not None and 'report_evidence_contract' not in old
                    and 'report_requirement' not in supplied):
                config.pop('report_evidence_contract', None)  # Unrelated supply is not a historical contract migration.
            for name in ('model_connection', 'vision_connection', 'feasibility_connection'):
                if old.get(name) is not None:
                    if not model_connections_compatible(old[name], config.get(name) or {}):
                        raise ValueError('Supply cannot replace the saved ' + name)
                    config[name] = old[name]
            # max_turns remains the initial allowance: the budget authorization
            # below accounts for newly authorized decisions exactly once.
            config['max_turns'] = old['max_turns']
            # A policy upgrade is a caller supply, never a side effect of load.
            # Its previous config is journaled below; old decision receipts keep
            # their original closing obligation. Absent historical fields stay
            # absent unless explicitly supplied, including unrelated supplies.
            for field in ('completion_policy', 'tool_contracts', 'report_protocol', 'tool_completions', 'inspection_protocol',
                          'inspection_coverage_policy', 'inspection_finding_policy',
                          'delivery_review_protocol', 'publication_assets', 'professional_operations', 'memory_protocol',
                          'context_checkpoint_policy', 'work_method', 'work_selection_policy', 'native_supply_policy', 'context_authority',
                          'native_history_policy', 'document_context_policy', 'process_artifact_paths', 'work_lifecycle', 'code_work_policy',
                          'code_review_policy', 'code_implementation_policy',
                          'sandbox_operations', 'scientific_condition_reuse', 'source_observation_reuse',
                          'document_source_contract', 'source_generation_protocol', 'source_decision_protocol'):
                if field not in supplied:
                    if field in old:
                        config[field] = old[field]
                    else:
                        config.pop(field, None)
            if (supplied.get('report_requirement') is not None
                    and old.get('control_protocol') == 'operations.v1'):
                # Supplying a report is an explicit new request: route its new
                # document work to the same Writer used at task creation.
                # Started operations retain their own saved document contract.
                config['document_context_policy'] = ('working.v4' if old.get('document_context_policy') == 'working.v4'
                    else 'working.v3')
            if supplied.get('sandbox_execution') is True and old.get('control_protocol') == 'operations.v1':
                # Explicitly granting isolation is a new execution contract;
                # supply cancels unstarted work and never rewrites started work.
                config['sandbox_operations'] = 'workspace.v1'
            if (old.get('control_protocol') == 'operations.v1' and supplied.get('code_task') is not None
                    and supplied['code_task'] != old.get('code_task')):
                # An explicitly supplied new code contract is new work, not a
                # change to an already running historical code operation.
                config['code_work_policy'] = 'shared.v2'
                preparation = config['code_task'].get('preparation') or {}
                config['code_implementation_policy'] = 'direct.v2' if preparation.get('kind') == 'greenfield' else 'direct.v1'
                if (config.get('delivery_review_protocol') == 'task.v5'
                        and (old.get('delivery_review_selection') not in MODEL_REVIEW_SELECTIONS
                             or config.get('independent_review_required'))):
                    preparation = (config['code_task'].get('preparation') or {})
                    config['code_review_policy'] = 'delivery.v2' if preparation.get('kind') == 'greenfield' else 'delivery.v1'
                else:
                    config['code_review_policy'] = None
            elif 'code_task' in supplied and supplied['code_task'] is None:
                config['code_work_policy'] = config['code_review_policy'] = config['code_implementation_policy'] = None
            if 'independent_review_required' in supplied:
                review_config = _review_requirement({**old, **config}, supplied['independent_review_required'])
                config['independent_review_required'] = review_config['independent_review_required']
                if 'code_review_policy' in review_config:
                    config['code_review_policy'] = review_config['code_review_policy']
            if old.get('source_snapshots'):
                config['source_snapshots'] = {name: value for name, value in old['source_snapshots'].items()
                    if old['sources'][name] == config['sources'].get(name)}
            scope_fields = {'sources', 'research_policy', 'execution_contract', 'code_task', 'table_analysis'}
            if (any(old.get(name) != config.get(name) for name in scope_fields)
                    or replace_existing and 'sources' in supplied):
                config['authority_revision'] = old.get('authority_revision', 0) + 1
            terms['previous_config'] = old
            terms['request_count'] = len(self.state['requests']) + 1
            capacity = bool(allowances or additional_attempts or additional_no_progress)
            if capacity:
                if allowances:
                    self.ledger.validate_remaining(allowances, authorization_id=supply_id, reason=reason)
                check = BudgetState.from_dict(self.session.manifest.budget.to_dict())
                attempts = allowances.get('llm_requests', 0) * 10 if additional_attempts is None else additional_attempts
                check.authorize_additional(supply_id, attempts=attempts, no_progress=additional_no_progress or 0,
                    resource_allowances=allowances, reason=reason)
            else:
                check = self.session.manifest.budget
            if check.exhausted():
                raise ValueError('Supply cannot reset exhausted task attempts or no-progress limits')
            # Journal intent before the companion budget files. A crash cannot
            # expose a new allowance to the old task configuration; run refuses
            # work until this exact caller supply is completed idempotently.
            self.state['pending_supply'] = terms
            self._save()
            # Persist a grant before its config consumer; the same id recovers a
            # crash here without replenishing spent resources or authorizing a call.
            self._authorize_resources(allowances, supply_id if capacity else '', reason if capacity else '',
                additional_attempts=additional_attempts, additional_no_progress=additional_no_progress,
                model_binding=config.get('model_connection'), persist=False)
            from simple_ar.core.artifacts import write_atomically
            for relative, content in reused:
                write_atomically(self.session.store.resolve(relative), content)
            self._cancel_unstarted('Explicit caller supply: ' + supply_id)
            self.state['config'] = {**old, **config}
            self.state.pop('pending_supply', None)
            self.state.setdefault('supplies', []).append(terms)
            self.state['requests'].append(('Caller explicitly replaced task contracts: ' if replace_existing else
                'Caller supplied missing task contracts: ') + reason)
            if self.session.manifest.status not in {'created', 'running'}:
                self.session.continue_with_revision(reason)
            self._save()
            self._configure_tools()
            return terms
