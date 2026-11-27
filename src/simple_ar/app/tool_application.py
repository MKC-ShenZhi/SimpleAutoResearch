"""Single action owner for native-tool sessions; legacy sessions keep their recipe.

SessionController owns lifecycle/attempts/locking, LLMClient owns transport and
accounting, CommonToolGateway owns dispatch/permissions. This replaces semantic
stage selection for the migrated entry, rather than calling ResearchApplication.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Literal
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.budget import BudgetError, BudgetLedger, completed_llm_usage_is_accounted
from simple_ar.app.task_view import history_observation, saved_work_connection
from simple_ar.core.working_context import history_view, input_capacity, RECEIPT_SUPPLY_POLICIES, TURN_SUPPLY_POLICY
from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, AttemptManifest, CapabilityContext, CapabilityRegistry, CapabilityResult
from simple_ar.core.session import BudgetState, SessionController
from simple_ar.integrations.llm import (
    LLMCallStopped, LLMClient, LLMError, LLMSettings, check_llm_call_stop,
    estimate_native_input_tokens, estimate_tokens, llm_call_stop_scope)
from simple_ar.integrations.model_profiles import catalog_path, model_connections_compatible
from simple_ar.tools.gateway import CommonToolGateway
from simple_ar.tools.openai_schema import compact_json_schema, export_openai_tool_schemas
from simple_ar.tools.permissions import ToolPermissionPolicy
from simple_ar.tools.registry import ToolRegistry
from simple_ar.tools.extensions import LocalExtension, freeze_extensions
from simple_ar.tools.specs import CommonToolSpec, ToolCall, ToolResult, ToolCompletion, tool_completion, NATIVE_WORK_METHODS, CURRENT_WORK_METHOD, GOAL_WORK_METHOD, DIRECT_NATIVE_SELECTION_POLICIES, MODEL_REVIEW_SELECTIONS
from simple_ar.tools.professional_outcomes import (
    accepted_review_journal, check_professional_stop, related_feedback, stop_decision)
from simple_ar.tools.workspace import (WorkspaceToolGateway, workspace_tool_specs,
    DeliveryRequest, FileWindow, validate_workspace_arguments, workspace_observation_view,
    resolve_source_mount)
from simple_ar.tools.materials import (
    authored_snapshot, read_material_window, journal_material_references, journal_read_windows, journal_tool_calls, observe_text_window,
    current_text_window, admit_text_window, uncovered_text_ranges, observed_window_key,
)
from simple_ar.tools.sources import SourcePolicy, SourceToolGateway
from simple_ar.tools.visual import VisualToolGateway, visual_tool_specs
from simple_ar.tools.images import ImageToolGateway, image_tool_specs
from simple_ar.tools.publication import (PublicationToolGateway, publication_tool_specs, ExportRequest,
    PUBLICATION_ASSET_PROTOCOL, PUBLICATION_CONSUMER_PROTOCOL)
from simple_ar.report.legacy_contract import BOUND_REPORT_REVISION, ProfessionalReview
from simple_ar.tools.implementation import ExactCodeTask, ImplementationWorkRequest, ImplementationToolGateway, implementation_tool_specs
from simple_ar.tools.analysis import AnalysisToolGateway, analysis_tool_specs
from simple_ar.research.sources import SearchProviderRegistry
from simple_ar.app.professional_conversation import RETAINED_PROFESSIONAL_CONTRACTS, INTEGRATED_WORK_LIFECYCLES


from simple_ar.app.tool_contracts import (  # noqa: F401
    MANAGEMENT_SYSTEM,
    REQUEST_OWNER_SYSTEM,
    SYSTEM,
    ExecutionRejection,
    _ActionSuspended,
    _ADOPTING_OWNER_MODES,
    _ADOPTING_OWNERS,
    _CALLER_ACTIONS_CONSUMED,
    _CLOSING_DECISION,
    _COMPLETION_POLICIES,
    _CONTEXT_PROTOCOLS,
    _CONVERSATION_CONTEXT_PROTOCOLS,
    _CORE_COORDINATION,
    _CreationDeclarations,
    _DECLARED_INPUTS,
    _DEFERRED_WORKSPACE_TOOLS,
    _OPERATION_COMPLETION,
    _OPERATION_EXIT,
    _OPERATION_EXIT_DECISION,
    _OUTCOME_WORK,
    _OWNER_MODES,
    _OWNERS,
    _OperationPlan,
    _PROFESSIONAL_CORE,
    _PROFESSIONAL_WORK,
    _REFERENCED_CONTEXT_PROTOCOLS,
    _REPORT_PROTOCOL,
    _REQUEST_CORE,
    _REQUEST_OWNER,
    _ReadHistory,
    _ReportIntent,
    _ReuseDeclaration,
    _RuntimeClientPort,
    _SHARED_OWNER_MODES,
    _SHARED_OWNERS,
    _TOOL_CONTRACT_MODES,
    _ToolSelection,
    _WORKING_CONTEXT_PROTOCOLS,
    _WorkFocus,
    _WorkNote,
    _WorkPlan,
    _WorkPlanStep,
    _review_requirement,
    _runtime_bindings,
    _source_registry,
)
from simple_ar.app.tool_config import _ToolConfigMixin
from simple_ar.app.tool_delivery import _ToolDeliveryMixin
from simple_ar.app.tool_legacy import _ToolLegacyMixin
from simple_ar.app.tool_memory import _ToolMemoryMixin
from simple_ar.app.tool_messages import _ToolMessagesMixin
from simple_ar.app.tool_recovery import _ToolRecoveryMixin


class ToolApplication(_ToolConfigMixin, _ToolMemoryMixin, _ToolLegacyMixin, _ToolRecoveryMixin, _ToolMessagesMixin, _ToolDeliveryMixin):
    RECIPE = "native-tools-v1"

    def __init__(self, controller: SessionController, *, services: Any | None = None) -> None:
        self.session = controller
        self.services = services
        if controller.manifest.transition_recipe != self.RECIPE:
            raise ValueError("This is not a native-tool session; use its original resume command")
        self.state = controller.store.read_json("actions.json")
        declared = self.state['config'].get('runtime_dependencies', {})
        if services is not None:
            supplied = _runtime_bindings(services)
            if any(name not in declared or binding != declared[name] for name, binding in supplied.items()):
                raise ValueError('Injected runtime dependency differs from the saved task; no silent replacement')
        self.ledger = controller.load_budget()
        self._configure_tools()
        controller.registry.register("native_turn", self._model)
        controller.registry.register("native_tool", self._tool)
        controller.registry.register("native_action", self._action)

    def _configure_operations(self) -> None:
        from simple_ar.app.operation_tools import OperationTools, PROTOCOL, PROFESSIONAL_CONTRACTS
        config, root = self.state['config'], self.session.store.root
        if config.get('publication_assets') not in {None, PUBLICATION_ASSET_PROTOCOL, PUBLICATION_CONSUMER_PROTOCOL}:
            raise ValueError('Unknown saved publication asset protocol; preserve its original contract')
        if config['control_protocol'] != PROTOCOL:
            raise ValueError('Unsupported saved operation control protocol')
        if config.get('context_protocol') not in _CONTEXT_PROTOCOLS:
            raise ValueError('Unsupported saved operation context; no silent migration')
        if config.get('context_authority') not in (None, 'sources.v1'):
            raise ValueError('Unsupported saved context authority; no silent migration')
        from simple_ar.app.professional_conversation import NATIVE_HISTORY_POLICY
        if config.get('native_history_policy') not in (None, NATIVE_HISTORY_POLICY):
            raise ValueError('Unsupported saved native history policy')
        if config.get('document_context_policy') not in (None, 'working.v4'):
            raise ValueError('Unsupported saved document context policy')
        if config.get('scientific_condition_reuse') not in (None, 'unchanged.v1'):
            raise ValueError('Unsupported saved scientific condition reuse policy')
        if config.get('process_artifact_paths') not in (None, 'cwd.v1'):
            raise ValueError('Unsupported saved process artifact paths')
        if config.get('work_lifecycle') not in (None, *INTEGRATED_WORK_LIFECYCLES):
            raise ValueError('Unsupported saved work lifecycle policy')
        if config.get('code_work_policy') not in (None, 'shared.v1', 'shared.v2'):
            raise ValueError('Unsupported saved code work policy')
        if (config.get('code_implementation_policy') not in (None, 'direct.v1', 'direct.v2')
                or config.get('code_implementation_policy') in {'direct.v1', 'direct.v2'}
                   and (config.get('code_task') is None or config.get('code_work_policy') != 'shared.v2')):
            raise ValueError('Direct code implementation requires the shared code-work contract')
        if config.get('sandbox_operations') not in (None, 'workspace.v1'):
            raise ValueError('Unsupported saved specialized execution scope')
        if config.get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES and (
                config.get('professional_operations') != 'work.v12'
                or config.get('memory_protocol') != 'journal.v1'):
            raise ValueError('Integrated work lifecycle requires the shared ordinary work contract')
        if config.get('memory_protocol') not in {None, 'journal.v1'}:
            raise ValueError('Unsupported saved working memory; no silent migration')
        if config.get('native_supply_policy') is not None and config['native_supply_policy'] not in RECEIPT_SUPPLY_POLICIES:
            raise ValueError('Unsupported saved native material supply policy')
        if config.get('context_checkpoint_policy') not in {None, 'economic.v3'}:
            raise ValueError('Unsupported saved context checkpoint policy (retired in v2.12; start a new task or reuse artifacts via --from-session)')
        if config.get('work_method') is not None and config['work_method'] not in NATIVE_WORK_METHODS:
            raise ValueError('Unsupported saved professional working method')
        if config.get('work_method') and (config.get('memory_protocol') != 'journal.v1'
                or config.get('context_checkpoint_policy') != 'economic.v3'):
            raise ValueError('Goal working method requires the shared working journal')
        if config.get('inspection_material_policy') not in (None, 'operations.v1', 'operations.v3'):
            raise ValueError('Unsupported saved inspection material policy')
        if config.get('inspection_material_policy') and config.get('work_method') != GOAL_WORK_METHOD:
            raise ValueError('Inspection work observations require the goal working contract')
        if (config.get('work_selection_policy') not in {None, 'explicit.v2', 'native.v3', *DIRECT_NATIVE_SELECTION_POLICIES, 'outcomes.v1'}
                or config.get('work_selection_policy') and config.get('work_method') != GOAL_WORK_METHOD):
            raise ValueError('Unsupported saved work selection policy')
        if config.get('context_checkpoint_policy') and config.get('memory_protocol') != 'journal.v1':
            raise ValueError('Economic checkpointing requires journal continuity')
        if config.get('memory_protocol') and (config.get('context_protocol') != 'working.v4'
                or config.get('professional_operations') not in RETAINED_PROFESSIONAL_CONTRACTS):
            raise ValueError('Journal continuity requires the saved shared native work context')
        if config.get('delivery_review_protocol') not in {None, 'task.v5'}:
            raise ValueError('Unsupported saved delivery review protocol; no silent migration')
        if config.get('delivery_review_selection') not in {None, *MODEL_REVIEW_SELECTIONS}:
            raise ValueError('Unsupported saved delivery review selection')
        if (config.get('code_review_policy') not in {None, 'delivery.v1', 'delivery.v2'}
                or config.get('code_review_policy') in {'delivery.v1', 'delivery.v2'} and (
                    config.get('code_work_policy') != 'shared.v2'
                    or config.get('delivery_review_protocol') != 'task.v5'
                    or config.get('delivery_review_selection') in MODEL_REVIEW_SELECTIONS
                       and not config.get('independent_review_required'))):
            raise ValueError('Shared code review requires the whole-delivery review owner')
        if ('independent_review_required' in config
                and (type(config['independent_review_required']) is not bool
                     or config['independent_review_required'] and config.get('delivery_review_protocol')
                         != 'task.v5')):
            raise ValueError('The required review needs the shared whole-delivery review contract')
        if (config.get('candidate_review_protocol') not in {None, 'candidate.v1'}
                or config.get('candidate_review_protocol') is not None and (
                    config.get('work_method') != GOAL_WORK_METHOD
                    or config.get('delivery_review_protocol') != 'task.v5')):
            raise ValueError('Unsupported saved candidate review request contract; no silent migration')
        if config.get('inspection_coverage_policy') not in {None, 'offered.v2'}:
            raise ValueError('Unsupported saved inspection coverage policy; no silent migration')
        if config.get('working_set_policy') not in {None, 'recent.v4', 'recent.v6'}:
            raise ValueError('Unsupported saved working set policy')
        if config.get('action_offer_policy') not in {None, 'observed_edit.v1'}:
            raise ValueError('Unsupported saved action offer policy')
        if config.get('candidate_handoff_policy') not in {None, 'after_production.v1', 'after_production.v2'}:
            raise ValueError('Unsupported saved candidate handoff policy')
        if config.get('inspection_finding_policy') not in {None, 'linked.v1'}:
            raise ValueError('Unsupported saved inspection finding policy; no silent migration')
        if (config.get('inspection_finding_policy') == 'linked.v1'
                and (config.get('inspection_coverage_policy') != 'offered.v2'
                     or config.get('delivery_review_protocol') != 'task.v5')):
            raise ValueError('Linked findings require shared inspection and delivery review')
        if (config.get('inspection_coverage_policy') == 'offered.v2'
                and (config.get('inspection_protocol') != 'subjects.v3'
                     or config.get('inspection_execution_protocol') not in {'shared.v1', 'shared.v2'})):
            raise ValueError('Offered inspection coverage requires the shared subject inspection contract')
        if config.get('professional_operations') is not None and config['professional_operations'] not in PROFESSIONAL_CONTRACTS:
            raise ValueError('Unsupported saved professional operation contract; no silent migration')
        from simple_ar.tools.materials import verify_source_snapshots
        verify_source_snapshots(config['sources'], config.get('source_snapshots', {}))
        if config.get('professional_work') is not None or config.get('result_investigation') is not None:
            raise ValueError('Operation control cannot acquire a historical professional controller')
        coordination = [CommonToolSpec(name='load_tools',domain='coordination',permission_level='plan',
            input_schema=_ToolSelection.model_json_schema(),
            description='Select named optional operation contracts. The next offer also depends on remaining resources; selection does not grant permission.'),
            CommonToolSpec(name='read_history',domain='coordination',permission_level='read_only',
                input_schema=_ReadHistory.model_json_schema(),
                description='Read original closed native observations from this task, with bounded paging; history is not current file state.')]
        coordination.append(CommonToolSpec(name='update_work_plan',domain='coordination',permission_level='plan',
            input_schema=_OperationPlan.model_json_schema(),
            description='Save a concise optional work plan and durable working notes after coherent investigation or revision. '
                'Notes cite existing observation records; recorded versions are bound by the owner and survive context eviction. '
                'No automatic scheduling, permission, evidence or requirement to use a plan. '
                'Do not declare report_intent; the actual delivery identifies its manuscript.'))
        self.operations = OperationTools(root=root,config=config,ledger=self.ledger,
            session_id=self.session.manifest.session_id,
            client_factory=lambda identity,purpose:self._client(identity,purpose=purpose,native=False),
            observations=self._operation_observations,
            record_artifacts=self._record_artifacts,
            coordination={'specs':coordination,'owner':self},extension_host=self._extension_host_call,
            execution_observations=self._execution_observations,
            execution_history=lambda: self._execution_observations(all_records=True),
            artifact_changes=self._artifact_changes,
            work_context=self._work_context,
            material_observations=self._material_observations,
            user_requests=self._professional_user_requests,
            user_request_count=lambda: len(self.state['requests']),
            review_request_start=self._review_request_start,
            native_route_validator=self._validate_professional_route,
            scientific_revisions=(self._scientific_revision_attempts
                if config.get('scientific_condition_reuse') == 'unchanged.v1' else None),
            feasibility_client_factory=self._feasibility_client,
            search_registry=_source_registry(self.services, config))
        self.workspace, self.gateway = self.operations.workspace, self.operations.gateway
        self.sources, self.publication = self.operations.sources, self.operations.publication
        self.extensions = self.operations.extensions
        self.writing = None
        self.implementation, self.scientific = self.operations.implementation, self.operations.scientific

    def _decision_record(self, path: str, views: dict | None) -> dict:
        if views is None:
            return self.session.store.read_json(path)
        key = ('journal_record', path)
        if key not in views:
            views[key] = self.session.store.read_json(path)
        return views[key]

    def _operation_observations(self, *, domain: str | None = None, verify: bool = True,
                                decision_views: dict | None = None) -> list[dict]:
        from simple_ar.tools.professional_outcomes import verify_native_observations
        active = {name: {id(turn) for turn in self._evidence_turns(name)}
                  for name in ((domain,) if domain else ('sources', 'analysis', 'scientific', 'implementation'))}
        result, accepted = [], []
        for turn in self.state['turns']:
            reply = self._decision_record(turn['reply'], decision_views) if turn['tools'] else None
            for item, call in journal_tool_calls(self.session.store, turn, reply=reply):
                spec = self.gateway.registry.get(call['function']['name'])
                if domain is not None and (spec is None or spec.domain != domain):
                    continue
                observed = self._decision_record(item['result'], decision_views)
                if verify and observed.get('status') == 'ok':
                    # Prove the original producer before projecting its record.
                    # A separate review can reject delivery without changing
                    # production identity or silently settling its charges.
                    accepted.append((Path(item['result']).parent.name, observed))
                observation = self._operation_observation(observed, call, item['result'],
                    current_request=turn.get('request_count', 1) == len(self.state['requests']),
                    request_count=turn.get('request_count', 1),
                    contract_current=spec is None or spec.domain not in active
                        or id(turn) in active[spec.domain], review_record=item.get('review_result'))
                if (not observation['contract_current'] and spec is not None and spec.domain == 'scientific'
                        and observation.get('tool_name') == 'measure_experiment'
                        and self.state['config'].get('scientific_condition_reuse') == 'unchanged.v1'):
                    supplies = [row for row in self.state.get('supplies', [])
                        if row.get('request_count', 0) > turn.get('request_count', 1)]
                    configs = [row['previous_config'] for row in supplies] + [self.state['config']]
                    if supplies:
                        observation['contract_current'] = self.scientific.unchanged_condition_observation(
                            observation['arguments'].get('condition_id'), configs)
                result.append(observation)
        if accepted:
            verify_native_observations(self.session.store.root, self.session.manifest.session_id, accepted,
                ledger=None if self.operations is not None and self.operations.professional else self.ledger)
        return result

    @staticmethod
    def _operation_observation(payload: dict, call: dict, record: str, *,
                               current_request: bool = True, request_count: int = 1, contract_current: bool = True,
                               review_record: str | None = None) -> dict:
        try:
            arguments = json.loads(call['function']['arguments'])
        except (ValueError, TypeError):
            arguments = {}
        return {**payload, 'record':record, 'arguments':arguments,
                'current_request':current_request, 'request_count':request_count, 'contract_current':contract_current,
                **({'review_record':review_record} if review_record is not None else {})}

    def _scientific_observations(self) -> list[dict]:
        if self.state['config'].get('scientific_condition_reuse') == 'unchanged.v1':
            # Scientific consumers validate their own measurement receipts;
            # this projection does not repeat that work or grant adoption.
            return self._operation_observations(domain='scientific', verify=False)
        return [{**self.session.store.read_json(item['result']), 'record': item['result']}
            for turn in self._evidence_turns('scientific') for item in turn['tools']]

    def _scientific_revision_attempts(self) -> dict[str, bool]:
        """Project attempts retained across revisions of the measurement itself."""
        from simple_ar.tools.scientific import ScientificToolGateway

        supplies = self.state.get('supplies', [])
        start = revision = 0
        for index, supply in enumerate(supplies):
            before = supply['previous_config']
            after = supplies[index + 1]['previous_config'] if index + 1 < len(supplies) else self.state['config']
            count = supply.get('request_count', 0)
            project = (before.get('code_task') or {}).get('project_path', '').replace('\\', '/').split('/')[0]
            if (before.get('code_task') != after.get('code_task')
                    or project and before.get('sources', {}).get(project) != after.get('sources', {}).get(project)
                    or ScientificToolGateway.preparation_contract(before.get('execution_contract'))
                        != ScientificToolGateway.preparation_contract(after.get('execution_contract'))):
                start = count
            if 'execution_contract' in supply['changes']:
                revision = count
        attempts = {Path(item['result']).parent.name: turn.get('request_count', 1) < revision
            for turn in self.state['turns'] if turn.get('request_count', 1) >= start
            for item in turn['tools'] if item['result'].startswith('attempts/')}
        if self.state.get('pending') and self.state['pending'].get('attempt'):
            attempts[self.state['pending']['attempt']] = False
        return attempts

    def _tool_selection(self) -> set[str]:
        """Project the last accepted selection, not a second mutable tool state."""
        for turn in reversed(self.state['turns']):
            for item in reversed(turn['tools']):
                result = self.session.store.read_json(item['result'])
                if result.get('tool_name') == 'load_tools' and result.get('status') == 'ok':
                    return set(_ToolSelection.model_validate(result['data']).names)
                if (result.get('tool_name') == 'focus_work' and result.get('status') == 'ok'
                        and 'tools' in result['data']):
                    return set(_ToolSelection.model_validate({'names': result['data']['tools']}).names)
        return set()

    def _record_artifacts(self) -> tuple[Path, ...]:
        """Only actual controller-registered domain outputs can be delivered."""
        return tuple(ArtifactStore(self.session.store.root / 'attempts' / attempt.attempt_id).resolve(ref)
            for attempt in self.session.list_attempts() if attempt.status == 'completed'
            for ref in attempt.outputs if ref.status == 'available'
            and ref.kind not in {'native_reply', 'native_result', 'native_observation', 'caller_actions',
                                 'professional_scope_receipt', 'professional_scope_request', 'professional_prior'})

    @property
    def schemas(self) -> list[dict[str, object]]:
        operations = self.operation_schemas
        selection = getattr(self.operations, 'selection', None)
        return selection.offer(operations) if selection else operations

    @property
    def operation_schemas(self) -> list[dict[str, object]]:
        active, _ = self._contract_presentation()
        # Presentation filtering must never mutate the registry's input contract.
        schemas = json.loads(json.dumps(export_openai_tool_schemas(
            self.gateway.registry, read_only_only=False, policy=self.gateway.policy, names=active)))
        if self.operations is not None:
            schemas = self.operations.service_schemas(schemas)
            return [{**row, 'function': {**row['function'],
                'parameters': compact_json_schema(row['function']['parameters'])}} for row in schemas
                if not (getattr(self.operations, 'selection', None)
                    and row['function']['name'] in {'select_work', 'update_work_plan'})]
        obligation = self._report_projection()
        for row in schemas:
            if (row['function']['name'] in {'compose_document', 'review_document'}
                    and (row['function']['name'] == 'review_document' or not self.writing.document_work)):
                parameters = row['function']['parameters']
                parameters['required'] = list(dict.fromkeys([*parameters.get('required', []), 'title']))
            if row['function']['name'] == 'prepare_implementation':
                request_owner = self.state['config'].get('professional_work') == _REQUEST_OWNER
                row['function']['parameters'].get('properties', {}).pop('repair_execution', None)
                row['function']['parameters'].get('properties', {}).pop('repair_review', None)
                row['function']['parameters'].get('properties', {}).pop('work_contract', None)
                row['function']['parameters'].get('properties', {}).pop('producer_completion', None)
                row['function']['parameters'].get('properties', {}).pop('result_investigation', None)
                row['function']['parameters'].get('properties', {}).pop('candidate_admission', None)
                if self.state['config'].get('professional_work') in _SHARED_OWNER_MODES:
                    row['function']['parameters'].get('properties', {}).pop('max_repairs', None)
                if self.state['config'].get('professional_work') in _OUTCOME_WORK:
                    owners = self.state['config'].get('professional_work') in _OWNER_MODES
                    row['function']['description'] = (
                        ('Prepare source, or explicitly complete a bounded code, analysis or plotting work unit ' if owners else
                         'Complete a bounded code, analysis or plotting work unit ') + 'from its goal and authorized '
                        'material/project roots. The professional owner investigates as needed, creates or revises '
                        'the candidate, proposes execution and handles bounded diagnosed repairs. '
                        'No coordinator-selected files, rewritten evidence or code answers are required. '
                        'Execution still requires exact approval and uses the shared budget. '
                        'Submit a complete work unit last in its batch; subsequent work consumes its actual results. '
                        'Set complete_work=false for source-only preparation. Successful execution is not '
                        'scientific or visual certification; return actual artifacts for independent checking.')
                    row['function']['parameters']['properties']['complete_work']['default'] = request_owner or not owners
                    row['function']['parameters']['properties']['complete_work']['description'] = (
                        ('Explicitly choose true for a complete bounded implementation or false for source-only preparation. '
                         'Omitted means source only. ' if owners else
                         'Defaults to a complete bounded implementation: investigate, create or revise, '
                         'propose approved execution and diagnose local technical failures. Set false for source-only preparation. ')
                        + 'Does not authorize scientific reruns.')
                if request_owner:
                    row['function']['parameters'] = compact_json_schema(ImplementationWorkRequest.model_json_schema())
                    row['function']['description'] = (
                        'Complete a bounded code, analysis, table or figure outcome from its goal and authorized '
                        'material/project roots. The implementation owner investigates, selects inputs, creates '
                        'or revises source, proposes approved execution and diagnoses technical failures. '
                        'Omitted complete_work means true; explicit false prepares source only. '
                        'Use actual returned artifacts for dependent work. Shared budget and exact approval apply.')
                if self.state['config'].get('professional_work') not in _PROFESSIONAL_WORK:
                    for field in ('complete_work', 'max_repairs'):
                        row['function']['parameters'].get('properties', {}).pop(field, None)
            if (row['function']['name'] == 'compose_document'
                    and self.state['config'].get('professional_work') not in _PROFESSIONAL_WORK):
                row['function']['parameters'].get('properties', {}).pop('publication', None)
            if row['function']['name'] == 'compose_document' and obligation.get('requirement') is not None:
                parameters = row['function']['parameters']
                parameters.get('properties', {}).pop('professional_review', None)
            if self.writing.document_work and row['function']['name'] == 'compose_document':
                parameters = row['function']['parameters']
                if self.state['config'].get('professional_inputs') == 'owner.v3':
                    from simple_ar.tools.writing import DocumentWorkRequest
                    parameters = DocumentWorkRequest.model_json_schema()
                    parameters.setdefault('required', [])
                elif (self.state['config'].get('tool_contracts', 'full') != 'full'
                        and 'compose_document' not in self._tool_selection()):
                    fields = {'goal', 'material_roots', 'prior_draft', 'prior_result', 'target_words', 'publication', 'finish'}
                    parameters['properties'] = {key: value for key, value in parameters['properties'].items()
                        if key in fields}
                    parameters['required'] = [key for key in parameters.get('required', []) if key in fields]
                if obligation['kind'] == 'report' and obligation['evidence_contract'] == 'materials.v1':
                    parameters['properties'].pop('goal', None)
                    parameters['required'] = [key for key in parameters['required'] if key != 'goal']
                row['function']['description'] = (
                    'Produce a coherent document outcome from the actual request and authorized material roots. '
                    'The professional owner reads the materials, chooses a grounded title, evidence and '
                    'deliverable source/table/figure files, drafts or revises, independently checks the '
                    'same-version bundle and proposes requested publication. Do not pre-read all files, '
                    'choose evidence windows or invent attachment paths for this owner. Supply prior_draft '
                    'for feedback; the owner reuses valid inputs and preserves the original version. '
                    'Use finish=true only when this document outcome should finish the current request; '
                    'successful independent review and requested publication precede formal delivery. '
                    'No new permissions, network, scientific measurements or extra budget are granted. '
                    'Loading this contract does not transfer investigation, attachment preparation '
                    'or publication back to the coordinator. Put user constraints in the work goal, '
                    'not in a preselected Writer input recipe.')
                row['function']['parameters'] = compact_json_schema(parameters)
                continue
            if (self.writing.professional_inputs and obligation['kind'] == 'report'
                    and obligation['evidence_contract'] == 'materials.v1'
                    and row['function']['name'] == 'compose_document'):
                parameters = row['function']['parameters']
                for field in ('goal', 'materials', 'sources'):
                    parameters.get('properties', {}).pop(field, None)
                parameters['required'] = [field for field in parameters.get('required', [])
                    if field not in {'goal', 'materials', 'sources'}]
                parameters['properties']['figures']['items'] = {'type': 'string', 'minLength': 1}
                parameters['properties']['figures']['description'] = (
                    'Optional existing image paths. The writing owner investigates the actual materials '
                    'and authors/reconciles captions; the coordinator does not supply authoritative captions.')
                row['function']['description'] = (
                    'Complete the saved user report request using the writing owner. '
                    'The owner investigates actual original materials, selects evidence and authors the article '
                    'and captions, with the frozen report writing/review controls. Supply optional material_roots '
                    'or prior_draft and reuse_prior_inputs for relevant unchanged evidence, not rewritten goals '
                    'or preselected windows. Explicit adopt_draft independently checks an existing candidate '
                    'rather than regenerating it. No execution, network, visual or scientific certification. '
                    'Returns editable manuscript and review records for the same publication owner.')
        if self.state['config'].get('context_protocol') in _CONVERSATION_CONTEXT_PROTOCOLS:
            schemas = [{**row, 'function': {**row['function'],
                'parameters': compact_json_schema(row['function']['parameters'])}} for row in schemas]
        return schemas

    def _closing_schemas(self, *, lifecycle: dict | None = None) -> list[dict[str, Any]]:
        operations = self._closing_operation_schemas(lifecycle=lifecycle)
        selection = getattr(self.operations, 'selection', None)
        return selection.offer(operations) if selection else operations

    def _closing_operation_schemas(self, *, lifecycle: dict | None = None) -> list[dict[str, Any]]:
        """Offer executable closure before paying for the final decision.

        Selection is not permission or acceptance. Existing domain validation,
        approval and delivery checks still own every returned action.
        """
        operations = self.operation_schemas
        if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES:
            lifecycle = self.operations.delivery.lifecycle_requirements() if lifecycle is None else lifecycle
            needed = lifecycle['minimum_resources'].get('llm_requests', 0)
            # Include this decision and a truthful exit after an unsuccessful
            # owner call. Minimum resources are a lower bound, not a pass.
            remaining = self.ledger.remaining('llm_requests')
            owner_tools = (set(lifecycle['next_tools']) if remaining is None
                or remaining >= 2 + needed else set())
            if self.state['config'].get('work_selection_policy') == 'native.v5':
                terminal = {row['function']['name']: row for row in
                    self._terminal_operation_schemas(operations, include_compound=True)}
                return [row if row['function']['name'] in owner_tools else terminal[row['function']['name']]
                    for row in operations if row['function']['name'] in owner_tools | terminal.keys()]
            return [row for row in operations if row['function']['name']
                in {'ask_user', 'deliver_result', *owner_tools}]
        if (self.state['config'].get('completion_policy') == _OPERATION_EXIT
                and self.state['config'].get('work_method') not in NATIVE_WORK_METHODS):
            return self.operation_schemas
        # Restrict the active contracts rather than loading deferred specialists:
        # expanding an on-demand catalog here can evict original input windows.
        return self._terminal_operation_schemas(operations,
            include_compound=self.state['config'].get('work_method') not in NATIVE_WORK_METHODS)

    def _terminal_operation_schemas(self, operations: list[dict], *, include_compound: bool) -> list[dict]:
        """Use the registered completion contract for one executable final action."""
        schemas = [row for row in operations
            if (contract := self._completion(row['function']['name'])) is not None and contract.closing
                and (include_compound or not contract.field)]
        result = []
        for original in schemas:
            schema = json.loads(json.dumps(original))
            function = schema['function']
            contract = self._completion(function['name'])
            if contract.field:
                parameters = function['parameters']
                delivery = parameters.get('properties', {}).get(contract.field)
                if delivery is None:
                    continue
                # Preserve the owner's delivery shape and references, removing
                # only its ordinary-work omission/null alternatives.
                if 'anyOf' in delivery:
                    delivery['anyOf'] = [row for row in delivery['anyOf'] if row.get('type') != 'null']
                delivery.pop('default', None)
                if include_compound and self.state['config'].get('work_selection_policy') == 'native.v5':
                    description = delivery.get('description', '')
                    delivery['description'] = description.removeprefix('Optional ').removeprefix('optional ') + (
                        ' Required for this final-action offer; disclose unfinished work.')
                parameters['required'] = list(dict.fromkeys([*parameters.get('required', []), contract.field]))
            result.append(schema)
        return result

    def _specialist_catalog(self) -> dict[str, dict[str, str]]:
        catalog = self._contract_presentation()[1]
        selection = getattr(self.operations, 'selection', None)
        return {name:row for name,row in catalog.items()
            if selection is None or name not in selection.handoff_operations}

    def _contract_presentation(self) -> tuple[set[str], dict[str, dict[str, str]]]:
        """One partition for active schemas and the existing loadable directory."""
        mode = self.state['config'].get('tool_contracts', 'full')
        selected = self._tool_selection() if mode != 'full' else set()
        if self.operations is not None:
            return self.operations.presentation(selected, mode)
        active: set[str] = set()
        catalog: dict[str, dict[str, str]] = {}
        for spec in self.gateway.list_specs():
            if not self.gateway.policy.is_allowed(spec)[0]:
                continue
            request_owner = self.state['config'].get('professional_work') == _REQUEST_OWNER
            # Consumer eligibility and schema loading are separate. Full is an
            # explicit presentation choice, never access to expert primitives.
            if request_owner and not self.gateway.registry.request_visible(spec.name):
                continue
            owners = self.state['config'].get('professional_work') in _OWNER_MODES
            deferred = (spec.name not in (_REQUEST_CORE if request_owner else _PROFESSIONAL_CORE) if owners and mode != 'full' else
                spec.name in _DEFERRED_WORKSPACE_TOOLS if mode == 'outcomes' else
                mode == 'on_demand' and spec.domain != 'workspace' and spec.name not in _CORE_COORDINATION)
            if spec.name == 'update_work_plan' and self.state['config'].get('report_protocol') == _REPORT_PROTOCOL:
                deferred = False
            if (self.state['config'].get('professional_work') in _PROFESSIONAL_WORK
                    and not owners
                    and spec.name in {'prepare_implementation', 'compose_document'}):
                deferred = False
            if not deferred or spec.name in selected:
                active.add(spec.name)
            if deferred:
                catalog[spec.name] = {'domain': spec.domain}
                if spec.name not in selected:
                    # A directory is a purpose index, not a second copy of
                    # every loaded contract. Full constraints live in schemas.
                    purpose = ' '.join(spec.description.split()).split('. ', 1)[0]
                    catalog[spec.name]['description'] = purpose[:200]
        return active, catalog

    def _extension_host_call(self, name: str, arguments: dict, identity: str) -> ToolResult:
        """Nested reads use the same policy, mounted materials and trace writer."""
        spec = self.gateway.registry.get(name)
        if (spec is None or spec.domain.startswith('extension:')
                or spec.permission_level not in {'read_only', 'plan'}
                or spec.domain != 'workspace'):
            return ToolResult(tool_name=name, status='blocked',
                error='Local extension services permit only host workspace read/plan operations')
        return self.gateway.call(ToolCall(tool_name=name, arguments=arguments, trace_id=identity))

    def _work_focus(self, *, decision_views: dict | None = None) -> dict[str, Any]:
        """Project the latest closed decision; no second mutable work state."""
        selected = None
        boundary = {}
        for index in range(len(self.state['turns']) - 1, -1, -1):
            turn = self.state['turns'][index]
            for observed in reversed(turn['tools']):
                result = self._decision_record(observed['result'], decision_views)
                if result.get('tool_name') == 'focus_work' and result.get('status') == 'ok':
                    data = result['data']
                    if 'native_history_start_turn' in data:
                        start = data['native_history_start_turn']
                        if type(start) is not int or not 0 <= start <= len(self.state['turns']):
                            raise ValueError('Invalid focus native history boundary')
                        boundary['native_history_start_turn'] = max(
                            start, boundary.get('native_history_start_turn', 0))
                    if selected is not None:
                        continue
                    if 'inputs' in data:
                        current = data.get('request_count') == len(self.state['requests'])
                        selected = {'purpose': data['purpose'], 'goal': data['goal'] if current else '',
                            'inputs' if current else 'previous_inputs': data['inputs'],
                            'request_count': data.get('request_count'), 'turn': index,
                            'record': 'records/' + observed['result'].removeprefix('attempts/')}
                    else:
                        selected = {'purpose': data['purpose'], 'goal': data['goal']}
        # A same-connection focus must not reopen an older provider session.
        return {**(selected or {'purpose': 'task', 'goal': ''}), **boundary}

    def _coordinator_purpose(self, *, decision_views: dict | None = None) -> str:
        """Native decisions/retries stay on task; explicit domain clients keep their routes."""
        if self.operations is not None and self.operations.methods is not None:
            return self.operations.methods.purpose()
        return ('task' if self.state['config'].get('professional_work') in _OWNER_MODES
            else self._work_focus(decision_views=decision_views)['purpose'])

    def _validate_professional_route(self, purpose: str) -> None:
        """A work scope cannot silently switch protocol or context contract."""
        if self.state['config'].get('context_protocol') != 'working.v4':
            raise ValueError('Professional work requires working.v4; this task retains its saved context contract')
        self._client(purpose=purpose, native=True)

    def _professional_boundary(self) -> int:
        """Keep native model histories on their original route after scope transitions."""
        if self.operations is None or self.operations.work is None:
            return 0
        record = self.operations.work.transition_record()
        if record is None:
            return 0
        for index in range(len(self.state['turns']) - 1, -1, -1):
            for item in reversed(self.state['turns'][index]['tools']):
                if item['result'] == record:
                    return index + 1
        return 0

    def _work_plan(self, *, decision_views: dict | None = None) -> dict[str, Any] | None:
        """One view of closed tool receipts; never another mutable plan store."""
        for turn in reversed(self.state['turns']):
            for item in reversed(turn['tools']):
                result = self._decision_record(item['result'], decision_views)
                if (result.get('tool_name') == 'update_work_plan' and result.get('status') == 'ok'
                        and result['data'].get('request_count') == len(self.state['requests'])):
                    return {**result['data'],
                        'record': 'records/' + item['result'].removeprefix('attempts/')}
        return None

    @staticmethod
    def _recorded_arguments(call: dict) -> Any:
        raw = call['function']['arguments']
        try:
            return json.loads(raw)
        except (ValueError,TypeError):
            return raw

    def _report_projection(self, *, decision_views: dict | None = None) -> dict[str, Any]:
        from simple_ar.report.legacy_contract import project_report
        # A new report supersedes an earlier conversion or no-report choice;
        # a compatible earlier report still carries its requested formats.
        revision = next((row for row in reversed(self.state.get('supplies', []))
            if 'report_requirement' in row.get('changes', {})), None)
        start = revision.get('request_count', 0) if revision else 0
        def observations():
            for turn in reversed(self.state['turns']):
                for item in reversed(turn['tools']):
                    row = self._decision_record(item['result'], decision_views)
                    intent = (row.get('data') or {}).get('report_intent')
                    if (turn.get('request_count', 1) < start and row.get('tool_name') == 'update_work_plan'
                            and row.get('status') == 'ok' and intent
                            and (intent['kind'] != 'report' or revision['changes']['report_requirement'] is None)):
                        return
                    yield row
        return project_report(self.state['config'], observations())

    def _declare_report(self, intent: _ReportIntent) -> dict[str, Any]:
        from simple_ar.report.legacy_contract import declare_report
        return declare_report(intent, config=self.state['config'], requests=self.state['requests'],
            previous=self._report_projection, writing=self.writing, report_inputs=self._report_inputs)

    def _professional_user_requests(self) -> list[str]:
        """Original requests without administrative permission-only notices."""
        requests = list(self.state['requests'])
        administrative = {'independent_review_required'}
        if self.state['config'].get('delivery_review_selection') == 'model.v3':
            administrative |= {'allow_execution', 'sandbox_execution', 'allow_network',
                               'allow_vision', 'max_output_tokens'}
        for supply in reversed(self.state.get('supplies', [])):
            changes = set(supply.get('changes', {}))
            if not changes or not changes <= administrative:
                continue
            index = supply.get('request_count', 0) - 1
            prefix = ('Caller explicitly replaced task contracts: ' if supply.get('replace_existing')
                      else 'Caller supplied missing task contracts: ')
            if 0 <= index < len(requests) and requests[index] == prefix + supply['reason']:
                requests.pop(index)
        return requests

    def _review_request_start(self) -> int:
        """Supplied contracts do not silently cancel a selected final review."""
        current = len(self.state['requests'])
        for supply in reversed(self.state.get('supplies', [])):
            if (supply.get('request_count') != current
                    or supply.get('changes', {}).get('independent_review_required') is False):
                break
            current -= 1
        return current

    def _material_observations(self, targets: list[str]) -> list[dict[str, Any]]:
        """Locate successful parent reads, without transferring bodies or authority.

        The material owner admits these receipts against its actual inventory,
        current permissions and bytes. Only a new selection calls this provider.
        """
        if not self.gateway.policy.is_allowed(self.gateway.registry.get('read_file'))[0]:
            return []
        references = journal_material_references(self.workspace, self.session.store, self.state['turns'], targets,
            include_cross_path=self.state['config'].get('professional_work') in _ADOPTING_OWNER_MODES)
        # The selected snapshot already contains the original observation.
        # Consumers admit it through the byte owner; do not read it here too.
        for row in self._retained_working_inputs(targets)['observations']:
            if row['next_offset_bytes'] <= row['offset_bytes']:
                continue
            request = {'path': row['path'], 'offset_bytes': row['offset_bytes'],
                'max_bytes': row['next_offset_bytes'] - row['offset_bytes'], 'full_digest': True}
            data = {'text': row['text'], 'path': row['path'], 'sha256': row['sha256'], 'size_bytes': row['size_bytes'],
                'next_offset_bytes': row['next_offset_bytes'],
                'has_more': row['next_offset_bytes'] < row['size_bytes'], 'absolute_path': row['absolute_path']}
            references.append({'request': request, 'data': data, 'source_reference': row['source']})
        findings = self.gateway.professional_inputs(targets, include_observations=True)
        references = [row for finding in findings for row in finding.get('observations', [])] + references
        return ([{**row, 'copy_lineage_required': True} for row in references]
            if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES else references)

    def _work_feedback(self, targets: list[str], *, observations: list[dict] | None = None,
                       decision_views: dict | None = None) -> dict[str, Any]:
        """Find related original feedback for the requesting work owner.

        Only accepted checks with an intact owner/input binding are eligible.
        Changed subjects retain historical findings, never a current certificate.
        """
        inspection = self.operations.inspection if self.operations is not None else None
        with inspection.presentation_scope() if inspection is not None else nullcontext():
            return self._collect_work_feedback(targets, observations=observations, decision_views=decision_views)

    def _collect_work_feedback(self, targets: list[str], *, observations: list[dict] | None = None,
                               decision_views: dict | None = None) -> dict[str, Any]:
        # The operations contract never exposed the historical document-review
        # tool. Avoid rereading its whole journal when neither historical
        # document reviews nor vision reviews can exist in this saved task.
        owners = {}
        if self.operations is None:
            from simple_ar.report.composition_receipts import RetainedDocumentReview
            owners['review_document'] = RetainedDocumentReview
        if self.operations is None or self.state['config'].get('allow_vision'):
            owners['inspect_images'] = VisualToolGateway
        context = {'review_observations': related_feedback(
            self.workspace, self.session.store, self.session.manifest.session_id,
            self.state['turns'], targets, owners,
            copy_lineage=self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES,
            read_record=lambda path: self._decision_record(path, decision_views))
            if owners else []}
        findings = self.gateway.professional_inputs(targets, observations=observations)
        if findings:
            context['professional_findings'] = findings
        return context

    def _work_context(self, targets: list[str], *, include_retained_delivery: bool = False) -> dict[str, Any]:
        """Freeze the original requests, related feedback and working understanding for an owner."""
        prior = (self.operations.delivery.retained_prior_delivery()
                 if self.operations is not None and self.operations.professional else None)
        if include_retained_delivery and prior:
            from simple_ar.tools.materials import retained_delivery_targets
            retained, _ = retained_delivery_targets(self.workspace, prior)
            targets = list(dict.fromkeys([*targets, *retained]))
        decision_views = {}
        observations = (self._operation_observations(decision_views=decision_views)
                        if self.operations is not None and self.operations.professional else None)
        feedback = self._work_feedback(targets, observations=observations, decision_views=decision_views)
        requests = (self._professional_user_requests()
            if self.state['config'].get('delivery_review_selection') == 'model.v3' else list(self.state['requests']))
        context = {'user_requests': requests, 'review_observations': feedback['review_observations'],
            'scope': 'Original requests and related accepted checks, not permission, scientific proof or verification of changed files. Historical checks without bound inputs are not inferred.'}
        if 'professional_findings' in feedback:
            context['professional_findings'] = feedback['professional_findings']
        if self.state['config'].get('input_contract') == _DECLARED_INPUTS:
            context['creation_declarations'] = self.state['config']['creation_declarations']
        if self.operations is not None and self.operations.professional:
            context['working_notes'] = self._working_memory(observations=observations,
                decision_views=decision_views)['notes']
            if prior is not None:
                context['retained_prior_delivery'] = prior
        inherited = self._retained_working_inputs(targets)
        if inherited['notes']:
            context['historical_working_understanding'] = inherited['notes']
        return context

    def _retained_working_inputs(self, targets: list[str] | None = None) -> dict:
        """Existing creation inputs, projected without loading an older task."""
        if not self.gateway.policy.is_allowed(self.gateway.registry.get('read_file'))[0]:
            return {'observations': [], 'notes': []}
        from simple_ar.tools.materials import retained_working_inputs
        return retained_working_inputs(self.state['config'], resolve=self.workspace.resolve_material_root,
            targets=targets)

    def _artifact_changes(self, artifacts: list[str]) -> list[dict[str, str]]:
        """Locate original calls and results for recorded writes to delivered files."""
        targets = set(artifacts)
        changes = []
        for turn in self.state['turns']:
            try:
                call_record = str(self.workspace.resolve(str(self.session.store.root / turn['reply'])))
            except ValueError:
                continue  # Some caller submissions are outside the existing records grant.
            for item, call in journal_tool_calls(self.session.store, turn):
                spec = self.gateway.registry.get(call['function']['name'])
                if spec is None or spec.permission_level != 'write_patch':
                    continue
                observed = self.session.store.read_json(item['result'])
                path = (observed.get('data') or {}).get('absolute_path')
                if observed.get('status') == 'ok' and path in targets:
                    changes.append({'path': path, 'result': str(self.session.store.root / item['result']),
                        'call_record': call_record, 'call_id': call['id']})
        return changes

    def _execution_observations(self, *, all_records: bool = False) -> list[dict[str, Any]]:
        """Supply original receipts, not coordinator-authored failure summaries.

        Latest nonzero execution and latest execution have distinct meanings:
        either may be a probe rather than unresolved work. Neither establishes
        current source behavior. At most two original results are supplied,
        each with the existing process tool's bounded stdout/stderr and receipt
        locator. This is a projection, not persisted diagnosis or retry state.
        """
        latest = failure = None
        history = []
        for turn in reversed(self._evidence_turns('implementation')):
            for item, call in journal_tool_calls(self.session.store, turn, newest_first=True):
                spec = self.gateway.registry.get(call['function']['name'])
                if spec is None or spec.permission_level != 'execution':
                    continue
                result = self.session.store.read_json(item['result'])
                if 'returncode' not in (result.get('data') or {}):
                    continue
                observation = {'record': 'records/' + item['result'].removeprefix('attempts/'),
                    'arguments': json.loads(call['function']['arguments']), 'result': result}
                if all_records:
                    history.append(observation)
                    continue
                if latest is None:
                    latest = observation
                if result['data']['returncode'] != 0:
                    failure = observation
                    break
            if failure is not None:
                break
        if all_records:
            return list(reversed(history))
        return ([failure] if failure is not None and failure is not latest else []) + (
            [latest] if latest is not None else [])

    def can_reconcile(self, name: str, identity: str) -> bool:
        # The advisory plan is deterministic and cannot grant permission,
        # execute work or certify a delivery.
        return name == 'update_work_plan'

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        """A model-selection decision uses the same permissioned gateway."""
        try:
            if name == 'read_history':
                page = _ReadHistory.model_validate(arguments)
                total = len(self.state['turns'])
                if total and self.state['turns'][-1].get('remaining'):
                    total -= 1  # Never page the currently changing tool-call block.
                if page.start_turn > total:
                    raise ValueError('History start is beyond the accepted turns')
                end = min(total, page.start_turn + page.max_turns) if page.end_turn is None else page.end_turn
                if not page.start_turn <= end <= min(total, page.start_turn + page.max_turns):
                    raise ValueError('Pinned history end is outside this closed bounded range')
                turns = []
                for turn in self.state['turns'][page.start_turn:end]:
                    turns.append({'reply': 'records/' + turn['reply'].removeprefix('attempts/'),
                        'request_count': turn.get('request_count', 1),
                        'observations': [self._history_observation(call,
                            self.session.store.read_json(item['result']), item['result'])
                            for item, call in journal_tool_calls(self.session.store, turn)]})
                raw = json.dumps(self._history_view(turns), ensure_ascii=False).encode('utf-8')
                if page.offset_bytes > len(raw):
                    raise ValueError('History byte offset is beyond this range')
                stop = min(len(raw), page.offset_bytes + page.max_bytes)
                # Do not replace a split UTF-8 sequence or skip its continuation.
                while stop > page.offset_bytes:
                    try:
                        text = raw[page.offset_bytes:stop].decode('utf-8')
                        break
                    except UnicodeDecodeError as exc:
                        if exc.start == 0 and exc.reason != 'unexpected end of data':
                            raise ValueError('History offset must start at a UTF-8 boundary') from None
                        stop -= 1
                else:
                    if page.offset_bytes < len(raw):
                        raise ValueError('History byte window is too small for the next UTF-8 character')
                    text = ''
                return ToolResult(tool_name=name, data={'text': text,
                    'start_turn': page.start_turn, 'end_turn': end, 'total_turns': total,
                    'next_offset_bytes': stop, 'has_more_bytes': stop < len(raw),
                    'next_turn': end if stop == len(raw) else page.start_turn},
                    summary='Historical observation snapshot, not current state or verified outcomes.')
            if name == 'load_tools':
                selection = self._validate_tool_selection(arguments)
                return ToolResult(tool_name=name, data=selection.model_dump(),
                    summary='Contracts selected; the next offer reflects remaining resources and permissions. No professional work or permission change.')
            if name == 'update_work_plan':
                if self.operations is not None:
                    plan = _OperationPlan.model_validate(arguments)
                    data = plan.model_dump(exclude_none=True)
                    if plan.notes is not None:
                        data['notes'] = self.operations.bind_notes([note.model_dump() for note in plan.notes])
                    return ToolResult(tool_name=name,data={**data,
                        'request_count':len(self.state['requests']),'scope':'intention_not_verified_outcome'},
                        summary='Optional work plan saved; actual observations and authorization remain authoritative.')
                plan = _WorkPlan.model_validate(arguments)
                if not plan.steps and plan.report_intent is None:
                    raise ValueError('Provide work steps or an explicit report intent')
                declaration = self._declare_report(plan.report_intent) if plan.report_intent is not None else {}
                return ToolResult(tool_name=name, data={**plan.model_dump(), **declaration,
                    'request_count': len(self.state['requests']),
                    'scope': 'intention_not_verified_outcome'},
                    summary='Working intention recorded. Actual observations, approval and delivery remain authoritative.')
            if name != 'focus_work' or self.state['config'].get('professional_work') in _OWNER_MODES:
                raise ValueError('Unknown coordination operation')
            focus = _WorkFocus.model_validate(arguments)
            if focus.tools is not None:
                self._validate_tool_selection({'names': focus.tools})
            client = self._client(purpose=focus.purpose)
            data = {**focus.model_dump(exclude={'inputs'}), 'model': client.model}
            current = self._client()
            leaves = [{key: value for key, value in owner.connection_binding().items()
                if key not in {'code_route', 'writing_route'}} for owner in (current, client)]
            if not model_connections_compatible(*leaves):
                data['native_history_start_turn'] = len(self.state['turns'])
            if 'inputs' in focus.model_fields_set:
                spec = self.gateway.registry.get('read_file')
                allowed, reason = self.gateway.policy.is_allowed(spec)
                if focus.inputs and not allowed:
                    raise ValueError(reason)
                data.update(inputs=[], request_count=len(self.state['requests']))
                for window in focus.inputs:
                    request = window.model_copy(update={'full_digest': True}).model_dump(exclude_unset=True)
                    try:
                        row = read_material_window(self.workspace, request, identity=identity)
                    except (ValueError, OSError, RuntimeError) as error:
                        row = {'request': request, 'error': str(error)}
                    data['inputs'].append(row)
            return ToolResult(tool_name=name, data=data,
                summary='Next decision uses this work focus and any explicitly selected contracts. Original goal, permissions and remaining budget are unchanged.')
        except (ValueError, TypeError, LLMError) as exc:
            return ToolResult(tool_name=name, status='blocked', error=str(exc))

    def _validate_tool_selection(self, arguments: dict) -> _ToolSelection:
        selection = _ToolSelection.model_validate(arguments)
        for name in selection.names:
            spec = self.gateway.registry.get(name)
            if spec is None or not self.gateway.policy.is_allowed(spec)[0]:
                raise ValueError(f'Unknown or unauthorized tool: {name}')
            if self.operations is not None and not self.operations.coordinator_eligible(spec):
                raise ValueError(f'Tool belongs to professional production or inspection: {name}. '
                    'Delegate production goals and use deliver_result for independent acceptance; '
                    'explicit caller operations retain their normal APIs.')
            if (self.state['config'].get('professional_work') == _REQUEST_OWNER
                    and self.state['turns'] and self.state['turns'][-1].get('origin') == 'request_owner'
                    and not self.gateway.registry.request_visible(spec.name)):
                raise ValueError(f'Tool is expert-owned, not a request-owner capability: {name}')
        return selection

    def _evidence_turns(self, domain: str) -> list[dict[str, Any]]:
        """Current contract applicability, derived from the one supply journal.

        Old records remain readable history. Report-only feedback does not retire
        measurements; changing their actual execution contract does.
        """
        fields = {'scientific': {'execution_contract', 'code_task'}, 'implementation': {'code_task'},
            'analysis': {'table_analysis'}, 'sources': {'sources', 'research_policy', 'execution_contract', 'code_task'}}
        table_alias = ((self.state['config'].get('table_analysis') or {}).get('path') or '').split('/')[0]
        def retires(row: dict[str, Any]) -> bool:
            changed = fields.get(domain, set()).intersection(row['changes'])
            if (domain == 'sources' and changed and changed <= {'sources', 'research_policy'}
                    and self.state['config'].get('source_observation_reuse') == 'additive.v1'):
                previous = row['previous_config']
                old_mounts = previous['sources']
                supplied = row['changes'].get('sources', {})
                mounts = supplied if row.get('replace_existing') and 'sources' in row['changes'] else {**old_mounts, **supplied}
                old_policy = previous.get('research_policy') or {}
                policy = row['changes'].get('research_policy', previous.get('research_policy')) or {}
                if (all(mounts.get(name) == path for name, path in old_mounts.items())
                        and {k: v for k, v in old_policy.items() if k != 'materials'}
                            == {k: v for k, v in policy.items() if k != 'materials'}
                        and set(old_policy.get('materials', ())) <= set(policy.get('materials', ()))):
                    return False
            if changed:
                return True
            if domain == 'analysis' and 'sources' in row['changes']:
                supplied = row['changes']['sources']
                if self.state['config'].get('source_observation_reuse') == 'additive.v1':
                    original = row['previous_config']['sources'].get(table_alias)
                    current = supplied.get(table_alias) if row.get('replace_existing') else supplied.get(table_alias, original)
                    return original != current
                return table_alias in supplied
            return False
        start = max((row.get('request_count', 0) for row in self.state.get('supplies', [])
            if retires(row)), default=0)
        return [turn for turn in self.state['turns'] if turn.get('request_count', 1) >= start]

    def _check_cancellation(self) -> None:
        pending = self.state['pending']
        if pending and pending.get('inspection'):
            if any(attempt.status == 'running' for attempt in self.session.list_attempts()) or not completed_llm_usage_is_accounted(
                    row.to_dict() for row in self.ledger.entries):
                raise ValueError('Reconcile the active inspection operation and accounting before changing its request')
        elif pending and any(attempt.attempt_id == pending['attempt'] for attempt in self.session.list_attempts()):
            raise ValueError('Reconcile the started tool before changing its task')

    def _cancel_unstarted(self, reason: str) -> None:
        """Close only unstarted tool messages; keep completed observations intact."""
        pending = self.state['pending']
        queued = self.state['turns'] and self.state['turns'][-1]['remaining']
        if (pending and pending['kind'] == 'tool') or (pending is None and queued):
            self._check_cancellation()
            turn = self.state['turns'][-1]
            cancellation = pending['attempt'] if pending else (
                Path(turn['reply']).stem if turn.get('origin') in {'caller', 'professional'} else Path(turn['reply']).parent.name)
            for index, call in enumerate(turn['remaining']):
                relative = f'tools/cancelled/{cancellation}-{index}.json'
                self.session.store.write_json(relative, {'tool_name': call['function']['name'],
                    'status': 'blocked', 'error': reason, 'data': {
                        'executed': bool(pending and pending.get('inspection') and index == 0),
                        **({'retained_inspection': pending['inspection'],
                            'production_record': f"attempts/{pending['attempt']}/native_result.json"}
                            if pending and pending.get('inspection') and index == 0 else {})}})
                turn['tools'].append({'id': call['id'], 'result': relative})
            turn['remaining'] = []
            self.state['pending'] = None

    def request_execution_confirmation(self) -> dict[str, Any]:
        """Caller asks to review its declared protocol before process approval.

        The source owner keeps the same frozen protocol/response records used
        for professional designs. This is input confirmation, not an attempt,
        a measurement, an execution grant or an additional task controller.
        """
        with self.session.mutation_scope():
            if self.state.get('pending_supply') is not None:
                raise ValueError('Complete the retained caller supply before confirming execution')
            if not self.sources.policy.require_design:
                raise ValueError('Save research_policy.require_design before declaring a caller protocol confirmation')
            if any(row.status == 'running' for row in self.session.list_attempts()) or (
                    not completed_llm_usage_is_accounted(row.to_dict() for row in self.ledger.entries)
                    if self.state['config'].get('inspection_execution_protocol') in {'shared.v1', 'shared.v2'} else any(
                        row.status in {'reserved', 'unknown'} for row in self.ledger.entries)):
                raise ValueError('Reconcile running or unknown work before proposing a protocol')
            pending = self.state['pending']
            active = pending.get('action') if pending and pending.get('inspection') else pending
            if active and (active['kind'] == 'model' or any(
                    row.attempt_id == active['attempt'] for row in self.session.list_attempts())):
                raise ValueError('Reconcile the started operation before proposing a protocol')
            decision = self.sources.declare_execution_protocol()
            if self.sources.pending_decision() is not None:
                self.session.pause('Declared execution protocol awaits an explicit bound decision')
            return decision

    def _submit_actions(self, actions: list[dict[str, Any]]) -> None:
        """Accept caller requests into the existing action journal, not a new scheduler.

        Supplying an operation never approves its execution. Closed observations,
        attempt ownership and interruption recovery are identical to model calls.
        No references are substituted and no subsequent scientific steps inferred.
        """
        budget = self.session.manifest.budget
        if not isinstance(actions, list) or not 1 <= len(actions) <= budget.max_attempts:
            raise ValueError('Explicit tool requests must fit the task attempt allowance')
        pending = self.state['pending']
        abandoned = None
        if pending:
            abandoned = self._failed_model_attempt(pending)
            if abandoned is None or self.session.manifest.status not in {'running', 'paused', 'blocked', 'failed'}:
                raise ValueError('Cannot prove a closed failed decision without accepted output; recover pending work')
            if any(row.status == 'running' for row in self.session.list_attempts()):
                raise ValueError('Reconcile running attempts before submitting caller actions')
            for entry in self.ledger.entries:
                if entry.status not in {'reserved', 'unknown'}:
                    continue
                if self._retained_checkpoint_charge(entry):
                    continue
                if (entry.actual_source != 'reservation_bound'
                        or entry.logical_call_id != entry.attempt_id or entry.purpose != 'native-turn'
                        or not self._closed_llm_charge(entry)
                        or self._failed_model_attempt({'kind': 'model', 'attempt': entry.attempt_id},
                            current_only=False) is None):
                    raise ValueError('Reconcile reserved or unrecognized unknown operations before caller takeover')
        if any(turn['remaining'] for turn in self.state['turns']):
            raise ValueError('Reconcile or explicitly cancel queued work before submitting actions')
        if self.session.manifest.status in {'completed', 'partial'}:
            raise ValueError('New actions after delivery require explicit feedback')
        calls = []
        parents = {}
        turn_number = len(self.state['turns'])
        for index, row in enumerate(actions):
            if (not isinstance(row, dict) or not {'tool_name', 'arguments'} <= set(row)
                    or set(row) - {'tool_name', 'arguments', 'parent_attempt_id'}
                    or not isinstance(row['tool_name'], str) or not isinstance(row['arguments'], dict)):
                raise ValueError('Each caller action requires tool_name, object arguments and an optional parent attempt')
            parent = row.get('parent_attempt_id')
            if parent is not None and (not isinstance(parent, str) or not parent.strip()):
                raise ValueError('A caller action parent must be a nonempty attempt ID')
            spec = self.gateway.registry.get(row['tool_name'])
            if spec is None or not self.gateway.policy.is_allowed(spec)[0]:
                raise ValueError('Caller action is unavailable under this task permission policy')
            calls.append({'id': f'caller-{turn_number}-{index}', 'type': 'function',
                'function': {'name': row['tool_name'],
                    'arguments': json.dumps(row['arguments'], ensure_ascii=False, allow_nan=False)}})
            if parent is not None:
                parents[calls[-1]['id']] = parent
        relative = f'tools/submissions/caller-{turn_number}.json'
        payload = {'role': 'assistant',
            'content': 'These actions were explicitly submitted by the caller, not generated by a model.',
            'tool_calls': calls}
        if parents:
            payload['content'] += ' Parent attempts: ' + json.dumps(parents, ensure_ascii=False)
        if abandoned is not None:
            payload['content'] += (' The caller explicitly abandoned failed native decision '
                + abandoned.attempt_id + '; its failure and charges are retained. No model reply was accepted.')
            payload['abandoned_failed_decision'] = abandoned.attempt_id
        if self.session.store.exists(relative) and self.session.store.read_json(relative) != payload:
            raise ValueError('A different uncommitted caller submission exists; inspect it before continuing')
        if self.session.manifest.status in {'paused', 'blocked', 'failed'}:
            self.session.continue_with_revision('Explicit caller actions; existing records and allowances retained')
        if not self.session.store.exists(relative):
            self.session.store.write_json(relative, payload, kind='caller_actions')
        self.state['pending'] = None
        self.state['turns'].append({'origin': 'caller', 'reply': relative, 'tools': [],
            'remaining': calls, 'request_count': len(self.state['requests']),
            **({'parents': parents} if parents else {})})
        self._save()

    def _caller_takeover(self) -> bool:
        turns = [turn for turn in self.state['turns'] if turn.get('origin') != 'professional']
        return bool(turns and turns[-1].get('origin') == 'caller'
            and turns[-1].get('request_count') == len(self.state['requests'])
            and self.session.store.read_json(turns[-1]['reply']).get('abandoned_failed_decision'))

    def _request_capacity(self) -> int | None:
        initial = self.state['config']['max_turns']
        if initial is None:
            return self.ledger.remaining('llm_requests')
        allowance = initial + sum(
            terms.get('resource_allowances', {}).get('llm_requests', 0)
            for terms in self.session.manifest.budget.continuation_authorizations.values())
        capacity = max(0, allowance - sum(turn.get('origin') not in {'caller', 'professional'}
            for turn in self.state['turns']))
        remaining = self.ledger.remaining('llm_requests')
        return capacity if remaining is None else min(capacity, remaining)

    def _can_request(self, count: int) -> bool:
        remaining = self._request_capacity()
        return remaining is None or remaining >= count

    def _professional_resource_authorizations(self) -> list[str]:
        """Expose grants committed by the budget owners they actually affect."""
        terms = self.session.manifest.budget.continuation_authorizations
        committed = {entry.reservation_id for entry in self.ledger.entries
            if entry.actual_source == 'user_authorization' and entry.reservation_id in terms
            and entry.reserved == terms[entry.reservation_id].get('resource_allowances')}
        committed.update(identity for identity, grant in terms.items()
            if not grant.get('resource_allowances')
            and (grant.get('additional_attempts') or grant.get('additional_no_progress')))
        return sorted(committed)

    def _resume_professional_stop(self, retained: dict) -> tuple[str, dict, Any, dict | None] | None:
        known = retained.get('resource_authorizations')
        stop = retained.get('owner_stop') or {}
        reason = stop.get('reason')
        returns_to_request = (self.state['config'].get('professional_work') == _REQUEST_OWNER
            and reason in {'work_gap', 'owner_failed'})
        grant_added = (reason in {'work_gap', 'shared_budget_exhausted'} and isinstance(known, list)
            and bool(set(self._professional_resource_authorizations()).difference(known)))
        if not returns_to_request and not grant_added:
            return None

        source = retained['source_result']
        self._check_professional_stop(source, stop)
        observed = self.session.store.read_json(source)
        resolved = self.gateway.resume_professional_action(observed, source, reason=reason,
            grant_added=grant_added, return_gap=returns_to_request,
            consume_outcome=self.state['config'].get('professional_work') in _OUTCOME_WORK)
        return (source, observed, *resolved) if resolved is not None else None

    def _check_professional_stop(self, source: str, stop: dict) -> None:
        """A semantic handoff and a pause require the same source proof."""
        check_professional_stop(self.session.store.root, self.session.manifest.session_id, source, stop)

    def _pause_professional_stop(self, source: str, action: dict) -> None:
        """Retain an owner's deterministic stop in the existing action journal."""
        stop = action['stop']
        self._check_professional_stop(source, stop)
        if (self.state['turns'] and self.state['turns'][-1].get('origin') == 'professional'
                and self.state['turns'][-1]['remaining']):
            # Retain the raw proposal and an explicit unstarted cancellation,
            # rather than leaving a stale duplicate behind a later resume.
            self._cancel_unstarted('Professional owner stopped before execution; any authorized continuation must revalidate its original source')
        relative = f"tools/submissions/professional-{len(self.state['turns'])}.json"
        payload = {'role': 'assistant', 'content': 'Professional owner stopped: ' + stop['detail'],
            'tool_calls': [], 'source_result': source, 'owner_stop': stop,
            'resource_authorizations': self._professional_resource_authorizations()}
        if self.session.store.exists(relative):
            if self.session.store.read_json(relative) != payload:
                raise ValueError('Uncommitted professional stop differs from its retained source')
        else:
            self.session.store.write_json(relative, payload, kind='professional_actions')
        self.state['turns'].append({'origin': 'professional', 'reply': relative, 'tools': [],
            'remaining': [], 'request_count': len(self.state['requests'])})
        self.state['pending'] = None
        self._save()  # Recovery can finish the pause after this journal commit.
        self.session.pause('Professional owner stopped: ' + stop['reason'] + ': ' + stop['detail'])
        self._save()

    def _continue_professional_work(self, resumed: tuple[str, tuple] | None = None) -> bool:
        """Journal an owner's next local operation through the same action loop.

        Only the final completed observation in the current request may continue.
        Permissions, approval, reservations and physical recovery still belong to
        the ordinary dispatcher. The saved reply identifies the producing receipt,
        rather than pretending another model made a decision.
        """
        if not self.gateway.local_continuation():
            return False
        turns = self.state['turns']
        if (self.state['pending'] is not None or not turns or turns[-1]['remaining']
                or turns[-1].get('request_count', 1) != len(self.state['requests'])):
            return False
        prepared = None
        if turns[-1].get('origin') == 'professional':
            retained = self.session.store.read_json(turns[-1]['reply'])
            if stop := retained.get('owner_stop'):
                prepared = (resumed[1] if resumed is not None and resumed[0] == turns[-1]['reply']
                            else self._resume_professional_stop(retained))
                if prepared is None:
                    self.session.pause('Professional owner stopped: ' + stop['reason'] + ': ' + stop['detail'])
                    self._save()
                    return True
        if prepared is not None:
            source, observed, owner, action = prepared
        else:
            if not turns[-1]['tools']:
                return False
            source = turns[-1]['tools'][-1]['result']
            observed = self.session.store.read_json(source)
            if not self.gateway.local_continuation(observed.get('tool_name', '')):
                return False
            owner, action = self.gateway.professional_action(observed, source, resume=False,
                consume_outcome=self.state['config'].get('professional_work') in _OUTCOME_WORK)
        if owner is None:
            return False
        if action is None and prepared is not None:
            action = stop_decision(owner, observed, Path(source).parent.name, 'owner_state_unresolved',
                'New resource authorization cannot establish a safe continuation from the original source')
        if action is None:
            return False
        if 'stop' in action:
            gap = getattr(owner, 'can_return_gap', None)
            if (self.state['config'].get('professional_work') == _REQUEST_OWNER
                    and action['stop'].get('reason') in {'work_gap', 'owner_failed'}
                    and gap is not None and gap(observed, Path(source).parent.name)):
                self._check_professional_stop(source, action['stop'])
                # The completed negative review remains the outcome. Give the
                # original request owner its cross-capability decision; never
                # replay this call or relabel it as an accepted document.
                return False
            self._pause_professional_stop(source, action)
            return True
        spec = self.gateway.registry.get(action['tool_name'])
        if spec is None or not self.gateway.policy.is_allowed(spec)[0]:
            self._pause_professional_stop(source, stop_decision(owner, observed, Path(source).parent.name,
                'permission_unavailable', 'The next professional operation is not authorized; no execution occurred'))
            return True
        minimum = self.gateway.continuation_attempts(action['tool_name'], action['arguments'])
        if self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts < minimum:
            self._pause_professional_stop(source, stop_decision(owner, observed, Path(source).parent.name,
                'shared_budget_exhausted', 'Original task attempt capacity cannot fit the next professional operation'))
            return True
        return self._queue_professional_action(source, action)

    def _queue_professional_action(self, source: str, action: dict) -> bool:
        turns = self.state['turns']
        identity = f'professional-{len(turns)}'
        call = {'id': identity, 'type': 'function', 'function': {
            'name': action['tool_name'],
            'arguments': json.dumps(action['arguments'], ensure_ascii=False, allow_nan=False)}}
        relative = f'tools/submissions/{identity}.json'
        payload = {'role': 'assistant', 'content': 'Professional owner continuation of ' + source,
            'tool_calls': [call], 'source_result': source}
        if self.session.store.exists(relative):
            if self.session.store.read_json(relative) != payload:
                raise ValueError('Uncommitted professional continuation differs from its retained source')
        else:
            self.session.store.write_json(relative, payload, kind='professional_actions')
        turns.append({'origin': 'professional', 'reply': relative, 'tools': [], 'remaining': [call],
            'request_count': len(self.state['requests'])})
        self._save()
        return True

    def _coordination_result(self, result: dict, record: str, *, delivery_feedback: dict | None = None,
                             review_record: str | None = None) -> dict:
        """Owner-authored presentation; never mutate the canonical observation."""
        if self.operations is not None:
            presented = workspace_observation_view(self.operations.coordination_result(result, record, review_record=review_record),record=record)
            if delivery_feedback is not None and delivery_feedback['record']==record:
                return {**presented,'delivery_check':delivery_feedback}
            return presented
        if self.state['config'].get('professional_work') in _PROFESSIONAL_WORK:
            return self.gateway.coordination_result(result, record)
        return result


    @staticmethod
    def _vision_binding(client: LLMClient) -> dict[str, Any]:
        # Vision does not consume the code route. Pin only its own non-secret
        # identity, retaining the existing operational compatibility rules.
        return {key: value for key, value in client.connection_binding().items()
                if key not in {'code_route', 'writing_route', 'review_route'}}

    @staticmethod
    def _work_connection(binding: dict[str, Any], purpose: str) -> dict[str, Any]:
        """Compatibility delegate to the public saved-connection projection."""
        return saved_work_connection(binding, purpose)

    def _client(self, attempt_id: str = "", *, purpose: str | None = None,
                native: bool = True, vision_catalog_hint: str | None = None) -> LLMClient:
        if self.state['config']['model'] is None:
            raise ValueError('This caller-directed task has no model connection or model authorization')
        if purpose is None:
            purpose = self._coordinator_purpose()
        if 'llm_client' in self.state['config'].get('runtime_dependencies', {}):
            client = self._runtime_client('llm_client', attempt_id)
            limit = self.state['config'].get('max_output_tokens')
            if isinstance(client, LLMClient):
                client = self._text_client(client, purpose, native=native)
            return client.with_output_limit(limit) if limit is not None else client
        selector = self.state['config']['model']
        client = LLMClient.from_env(None if selector == 'env' else selector, budget_ledger=self.ledger,
            fallback_models_config=self.state['config'].get('model_catalogs', {}).get('task'),
            budget_session_id=self.session.manifest.session_id, budget_attempt_id=attempt_id)
        saved = self.state["config"].get("model_connection")
        binding = client.connection_binding()
        if saved and not model_connections_compatible(saved, binding):
            raise ValueError("Named model connection differs from this task. Restore its catalog/profile "
                             "or start a new task; credentials may rotate and operational timeouts may change.")
        if purpose == 'vision':
            vision = LLMClient.from_env(purpose='vision', budget_ledger=self.ledger,
                fallback_models_config=self.state['config'].get('model_catalogs', {}).get('vision')
                    or vision_catalog_hint,
                budget_session_id=self.session.manifest.session_id, budget_attempt_id=attempt_id)
            saved_vision = self.state['config'].get('vision_connection')
            if saved_vision and not model_connections_compatible(saved_vision, self._vision_binding(vision)):
                raise ValueError('Named vision connection differs from this task. Restore its original route/profile; no silent model switch.')
            return vision
        client = self._text_client(client, purpose, native=native)
        limit = self.state['config'].get('max_output_tokens')
        return client.with_output_limit(limit) if limit is not None else client

    def _runtime_client(self, name: str, attempt_id: str) -> Any:
        expected = self.state['config']['runtime_dependencies'][name]
        if _runtime_bindings(self.services).get(name) != expected:
            raise ValueError('Restore the saved injected ' + name + ' before model work; no environment fallback')
        client = getattr(self.services, name).with_budget(self.ledger,
            session_id=self.session.manifest.session_id, attempt_id=attempt_id)
        if (getattr(client, '_budget_ledger', None) is not self.ledger
                or getattr(client, '_budget_session_id', None) != self.session.manifest.session_id
                or getattr(client, '_budget_attempt_id', None) != attempt_id):
            raise ValueError('Injected client must bind the actual parent ledger/session/attempt')
        return client if isinstance(client, LLMClient) else _RuntimeClientPort(client, expected)

    def _feasibility_client(self, attempt_id: str) -> LLMClient | None:
        if 'feasibility_llm_client' in self.state['config'].get('runtime_dependencies', {}):
            return self._runtime_client('feasibility_llm_client', attempt_id)
        policy = self.state['config'].get('research_policy') or {}
        selector = policy.get('feasibility_connection')
        if not selector:
            return self._client(attempt_id, purpose='task', native=False) if self.state['config']['model'] is not None else None
        client = LLMClient.from_env(selector, budget_ledger=self.ledger,
            fallback_models_config=self.state['config'].get('model_catalogs', {}).get('feasibility'),
            budget_session_id=self.session.manifest.session_id, budget_attempt_id=attempt_id)
        saved = self.state['config'].get('feasibility_connection')
        if not saved or not model_connections_compatible(saved, client.connection_binding()):
            raise ValueError('Independent feasibility connection changed; restore its saved named connection')
        limit = self.state['config'].get('max_output_tokens')
        return client.with_output_limit(limit) if limit is not None else client

    @classmethod
    def _text_client(cls, client: LLMClient, purpose: str, *, native: bool = True) -> LLMClient:
        connection = cls._work_connection(client.connection_binding(), purpose)
        if purpose == 'code' or (purpose in {'writing', 'review'} and connection.get('profile')):
            client = LLMClient.for_task(client=client, purpose=purpose)
        # Writer owns its text protocol, including Responses; native decisions
        # retain structured Chat history and must never silently change protocol.
        if purpose in {'writing', 'review'} and not native:
            return client
        if client._settings.api_mode != "chat":
            raise ValueError("Native tools require the saved Chat protocol; no protocol fallback")
        return client

    def _queue_candidate_review(self) -> bool:
        if (self.state['config'].get('candidate_handoff_policy') != 'after_production.v2'
                or self.state['config'].get('candidate_review_protocol') != 'candidate.v1'
                or not self.state['turns'] or self._caller_takeover()):
            return False
        turns = self.state['turns']
        request_count = len(self.state['requests'])
        if turns[-1]['remaining'] or turns[-1].get('request_count', 1) != request_count:
            return False
        edits = {}
        production = []
        for index, turn in enumerate(reversed(turns)):
            if turn.get('request_count', 1) != request_count:
                break
            for item in reversed(turn['tools']):
                result = self.session.store.read_json(item['result'])
                name, data = result.get('tool_name'), result.get('data') or {}
                if name in {'review_result', 'deliver_result'}:
                    return False
                if result.get('status') != 'ok':
                    continue
                if index == 0 and name == 'export_report':
                    production.append((item['result'], {}, result.get('artifacts', [])))
                if (index == 0 and name == 'run_process' and data.get('returncode') == 0
                        and not data.get('stop_reason') and not data.get('output_binding_error')
                        and data.get('receipt')):
                    receipt = self.session.store.read_json(data['receipt'])
                    inputs = (receipt.get('input_binding') or {}).get('workspace_artifacts', {}).get('inputs', {})
                    outputs = (receipt.get('output_binding') or {}).get('outputs', {})
                    if inputs or outputs:
                        production.append((item['result'], inputs, outputs))
                if name == 'edit_file' and production and data.get('path'):
                    path = self.workspace.locator(data['path'])
                    edits.setdefault(path, data.get('sha256'))
            if index == 0 and not production:
                return False
        if not edits:
            return False
        for source, inputs, outputs in production:
            if not outputs and not any(inputs.get(path) == digest for path, digest in edits.items()
                                       if digest and path in inputs):
                continue
            artifacts = []
            for path in dict.fromkeys([*outputs, *edits]):
                try:
                    resolved = self.workspace.resolve_delivery(
                        DeliveryRequest(summary='Independent candidate review', artifacts=[path]))
                except (ValueError, OSError):
                    continue
                artifacts.extend(resolved['artifacts'])
            if artifacts:
                break
        else:
            return False
        name = 'review_result'
        spec = self.gateway.registry.get(name)
        if spec is None or not self.gateway.policy.is_allowed(spec)[0]:
            return False
        arguments = {
            'summary': 'Investigate whether the current edited and executed candidate addresses the latest '
                       'user request. Identify any necessary revisions using independent evidence.',
            'artifacts': list(dict.fromkeys(artifacts)),
            'review_scope': 'candidate',
        }
        remaining_attempts = self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts
        if remaining_attempts < self.gateway.continuation_attempts(name, arguments) + 4:
            return False
        from simple_ar.app.professional_conversation import ProfessionalConversation
        client = self._client()
        decision = self._completion_floor(client, dispatch=True)
        floor = ProfessionalConversation.resource_floor({'investigation': decision, 'followup': decision})
        if ({'llm_requests', 'total_tokens'}.intersection(
                self.ledger.unknown_dimensions(unbounded_only=True, current_allowance_only=True))
                or any((remaining := self.ledger.remaining(key)) is not None and remaining < amount
                       for key, amount in floor.items())):
            return False
        return self._queue_professional_action(source, {'tool_name': name, 'arguments': arguments})

    def _candidate_handoff(self) -> dict | None:
        """Offer one early inspection choice based on actual work in this request."""
        if self.state['config'].get('candidate_handoff_policy') != 'after_production.v1':
            return None
        paths = []
        checks = []
        for index, turn in enumerate(reversed(self.state['turns'])):
            if turn.get('request_count', 1) != len(self.state['requests']) or turn.get('remaining'):
                break
            budget = str(Path(turn['reply']).with_name('decision_budget.json'))
            if self.session.store.exists(budget) and self.session.store.read_json(budget)[
                    'input_projection'].get('candidate_handoff'):
                return None
            for item in reversed(turn['tools']):
                result = self.session.store.read_json(item['result'])
                name, data = result.get('tool_name'), result.get('data') or {}
                if name in {'review_result', 'deliver_result'}:
                    return None
                if result.get('status') != 'ok':
                    continue
                if index == 0 and (name == 'export_report'
                        or name == 'run_process' and data.get('returncode') == 0):
                    checks.append({'tool': name, 'record': item['result'],
                        'artifacts': result.get('artifacts', [])[:8],
                        'returncode': data.get('returncode'),
                        'output_excerpt': str(data.get('stdout', ''))[-1600:]})
                if name == 'edit_file' and checks:
                    path = data.get('path')
                    if isinstance(path, str) and path and path not in paths:
                        paths.append(path)
            if index == 0 and not checks:
                return None
        return {'recently_edited': paths[:8], 'latest_execution': checks[:4]} if paths else None

    def _professional_model_input(self, context: Any, client: Any) -> tuple:
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
        conversation = ProfessionalConversation(client)
        operation_exit = self.state['config'].get('completion_policy') == _OPERATION_EXIT
        integrated = self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES
        journal = self.state['config'].get('memory_protocol') == 'journal.v1'
        inspection = self.operations.inspection if self.state['config'].get('control_protocol') else None
        working_memory, memory_refs = None, ()
        decision_observations = None
        decision_guidance = None
        decision_views = {}
        selection = {}
        exit_value = None
        handoff_evidence = self._candidate_handoff()
        handoff_floor = None

        def project(closing):
            nonlocal exit_value, handoff_floor, working_memory, decision_observations, decision_guidance
            if (integrated and self.state['config'].get('context_protocol') in _WORKING_CONTEXT_PROTOCOLS
                    and self.operations is not None):
                if decision_observations is None:
                    decision_observations = self._operation_observations(decision_views=decision_views)
                    accepted = (self.session.store.read_json('delivery.json')
                        if self.session.store.exists('delivery.json') else None)
                    decision_guidance = self.operations.delivery.working_guidance(
                        accepted, include_lifecycle=True, observations=decision_observations)
                if working_memory is None:
                    working_memory = self._working_memory(observations=decision_observations,
                        decision_views=decision_views)
            lifecycle = decision_guidance[0] if decision_guidance is not None else None
            schemas = self._closing_schemas(lifecycle=lifecycle) if closing else self.schemas
            output_limit = conversation.closing_output_limit() if closing and integrated else None
            projection_client = conversation.projection_client(output_limit)
            selection.clear()
            messages = self._messages(client=projection_client, working_memory=working_memory,
                closing=closing, schemas=schemas, working_selection=selection,
                decision_observations=decision_observations, decision_guidance=decision_guidance,
                decision_views=decision_views)
            value = ConversationInput(messages, schemas, json.loads(messages[1]['content']),
                output_limit=output_limit)
            if integrated and closing:
                # A service must leave the caller an exit even if its result is
                # incomplete. Do not offer it solely because request slots fit.
                if not conversation.can_protect(value, self._lifecycle_floor(
                        {'llm_requests':1, 'total_tokens':conversation.tokens(value)}, lifecycle=lifecycle)):
                    selector = getattr(self.operations, 'selection', None)
                    if selector is not None and selector.policy == 'outcomes.v1':
                        schemas = selector.offer([row for row in self._closing_operation_schemas(lifecycle=lifecycle)
                            if row['function']['name'] in {'ask_user', 'deliver_result'}])
                    elif self.state['config'].get('work_selection_policy') == 'native.v5':
                        schemas = self.operation_schemas
                    else:
                        schemas = [row for row in schemas
                            if row['function']['name'] in {'ask_user', 'deliver_result'}]
                    selection.clear()
                    messages = self._messages(client=projection_client, working_memory=working_memory,
                        closing=True, schemas=schemas, working_selection=selection,
                        decision_observations=decision_observations, decision_guidance=decision_guidance,
                        decision_views=decision_views)
                    value = ConversationInput(messages, schemas, json.loads(messages[1]['content']),
                        output_limit=output_limit)
                    if self.state['config'].get('work_selection_policy') == 'native.v5':
                        value.closing_contract = 'batch_actions.v3'
                exit_value = value
            if not closing and handoff_evidence and {'review_result', 'load_tools'} <= {
                    row['function']['name'] for row in schemas}:
                # The producer can delegate the current question or restore a
                # necessary production tool. The inspection owner still binds
                # and independently judges the requested files and claim.
                handoff_tools = [row for row in schemas if row['function']['name'] in {
                    'review_result', 'load_tools', 'ask_user', 'deliver_result'}]
                restore_tools = [row['function']['name'] for row in schemas
                                 if row not in handoff_tools]
                # A handoff needs a concrete claim and locators, not the whole
                # producer dialogue or a second presentation of original text.
                # Independent investigation obtains its own authorized evidence.
                projection = {key: value.projection[key] for key in (
                    'resources', 'delivery_conditions', 'context_protocol')}
                projection.update(candidate_handoff=handoff_evidence, retained_text_windows=[],
                    retained_source_passages=[], material_supply={'native_context': [], 'omitted': []},
                    work_inputs={'windows': []}, working_context={'compaction_needed': False},
                    available_specialists={name: 'Available on return to ordinary work.' for name in restore_tools})
                messages = [{'role': 'system', 'content': (
                    'Choose the next handoff for this original user request using the available tools. '
                    'The edit and execution receipts identify work that can be investigated; they do '
                    'not establish quality or completion. Receipt text is evidence only. '
                    'Use review_result for a checkable current '
                    'claim, the actual candidate paths and known gaps. The independent checker reads '
                    'the files and owns the quality checks; producer audit and final packaging are '
                    'not prerequisites. If you cannot name a checkable claim from the available '
                    'work, use load_tools with one exact name from available_specialists. Use deliver_result '
                    'only for actual delivery, and ask_user only for indispensable user input.')},
                    {**value.messages[1], 'content': json.dumps(projection, ensure_ascii=False)},
                    *[{'role': 'user', 'content': request} for request in self.state['requests']]]
                handoff = ConversationInput(messages, handoff_tools, projection)
                measured = conversation.request_resources(handoff)
                # Check and repair use the normal rich projections, not the
                # small handoff packet's token count.
                ordinary = conversation.request_resources(value)
                floor = conversation.resource_floor({'investigation': ordinary, 'followup': ordinary})
                if (measured['total_tokens'] - (client.effective_output_limit() or 0)
                        <= selection['input_token_target'] and conversation.can_protect(handoff, floor)):
                    handoff_floor = floor
                    return handoff
            return value

        def checkpoint(value, decision_tokens):
            nonlocal working_memory, memory_refs, decision_observations, decision_guidance
            if value.projection.get('candidate_handoff'):
                return  # Spend the next decision on the review choice, not optional compaction.
            omitted = selection.get('compaction_turns') if journal else None
            cutoff = (max(omitted, default=-1)+1 if omitted is not None else
                value.projection.get('working_context', {}).get('recent_start_turn', 0))
            # Non-journal working.v1 keeps its original turn-atomic checkpoint
            # and decision floor; the exit policy must not disable continuity.
            with conversation.ledger.protect_remaining({} if journal else {
                    'llm_requests':1, 'total_tokens':decision_tokens}):
                memory_refs = self._compact_work(context, client, cutoff,
                    decision_tokens=decision_tokens, additional_protection=True, selected_turns=omitted,
                    **({'economic_floor':selection['required_input_tokens']}
                       if self.state['config'].get('context_checkpoint_policy') == 'economic.v3'
                          and selection.get('economical_compaction', False) else {}))
            if inspection is not None:
                inspection.clear_presentation_scope()
            decision_views.clear()
            decision_observations, decision_guidance, working_memory = None, None, None
            if memory_refs:
                memory_refs = self._memory_artifacts(context.store, context.attempt.attempt_id)
                if context.store.exists('memory_snapshot.json'):
                    snapshot = context.store.read_json('memory_snapshot.json')
                    previous, view = self._working_memory(), context.store.read_json('memory_input.json')
                    if journal:
                        working_memory = ProfessionalConversation.adopt(previous=previous,
                            snapshot=snapshot, view=view, operations_for_turn=self._memory_operations,
                            archive=self.state['config'].get('work_method') in NATIVE_WORK_METHODS)
                    else:
                        from simple_ar.app.work_memory import summary_coverage
                        working_memory = {'notes':snapshot['notes'], 'through_turn':summary_coverage(
                            previous=previous, snapshot=snapshot, view=view,
                            operations_for_turn=self._memory_operations)}
                    working_memory['notes'] = self.operations.project_notes(working_memory['notes'])

        with inspection.presentation_scope() if inspection is not None else nullcontext():
            value = conversation.prepare(project,
                followup=lambda value:(self._lifecycle_floor(
                    {'llm_requests':1, 'total_tokens':conversation.tokens(value)},
                    lifecycle=decision_guidance[0] if decision_guidance is not None else None) if integrated else
                    {'llm_requests':1, 'total_tokens':conversation.tokens(value)}
                    if operation_exit else self._completion_floor(client, value.messages)),
                checkpoint=None if ((self.state.get('pending') or {}).get('skip_memory_checkpoint')
                    or not journal) else checkpoint,
                closing_enabled=operation_exit or self.state['config'].get('completion_policy') == 'closing_decision.v2',
                exit_first=operation_exit or integrated,
                continue_allowed=self._can_request(2) and (
                    self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts > 2))
        floor = ({'llm_requests':1, 'total_tokens':conversation.tokens(value)}
            if value.closing else value.reserve if operation_exit else self._completion_floor(client, value.messages))
        if integrated:
            # Persist only the measured next-decision estimate. Owner minima
            # are derived again from current work, never compounded on replay.
            floor = {'llm_requests':1, 'total_tokens':conversation.tokens(exit_value)}
            if value.closing and conversation.can_protect(value, floor):
                value.reserve = floor
        if value.projection.get('candidate_handoff'):
            value.reserve = handoff_floor
        return conversation, value, memory_refs, floor

    def _selected_reply(self, context: Any, message: dict, binding: dict) -> dict:
        try:
            return self.operations.selection.interpret(message, binding, self.state['requests'])
        except (ValueError, KeyError, TypeError) as exc:
            binding['selection_error'] = str(exc)
            context.store.write_json('decision_budget.json', binding)
            return message  # Original invalid choice becomes a refused observation, never work.

    def _model(self, *, context: Any) -> CapabilityResult:
        if context.store.exists('work_choice.json'):
            binding = context.store.read_json('decision_budget.json')
            if binding.get('tool_admission') != 'work_selection.v1' or self.operations.selection is None:
                raise ValueError('Retained work choice has no matching saved interpreter')
            message = self._selected_reply(context, context.store.read_json('work_choice.json'), binding)
            ref = context.store.write_json('native_result.json', message, kind='native_reply')
            return CapabilityResult(status='completed', artifacts=(
                *self._memory_artifacts(context.store, context.attempt.attempt_id),
                context.store.ref('work_choice.json', kind='work_choice'), ref))
        client = self._client(context.attempt.attempt_id)
        conversation = None
        if (self.state['config'].get('memory_protocol') == 'journal.v1'
                or self.state['config'].get('completion_policy') == _OPERATION_EXIT):
            conversation, value, memory_refs, floor = self._professional_model_input(context, client)
            messages, schemas, closing = value.messages, value.tools, value.closing
        else:
            messages, schemas, memory_refs, floor, protect, closing = self._legacy_model_input(context, client)
        # Every new decision records its actual offer, even when no completion
        # reservation applies. Old replies without the marker remain unchanged.
        projection = json.loads(messages[1]['content'])
        binding = {
            'estimated_followup_tokens': (floor['total_tokens'] if floor else
                self._decision_tokens(client, messages, schemas=schemas)),
            'closing_required': closing,
            **({'request_output_limit': value.output_limit}
               if conversation is not None and value.output_limit is not None else {}),
            'tool_admission': 'work_selection.v1' if 'work_selection' in projection else 'offered.v1',
            **({'candidate_review_scope': 'explicit.v1'} if any(
                row.get('function', {}).get('name') == 'review_result'
                and 'review_scope' in row['function'].get('parameters', {}).get('properties', {})
                for row in schemas) else {}),
            **({'request_count':len(self.state['requests'])} if 'work_selection' in projection else {}),
            **({'request_owner': _REQUEST_OWNER}
                if self.state['config'].get('professional_work') == _REQUEST_OWNER else {}),
            **({'document_request': 'work.v2'}
                if self.state['config'].get('professional_inputs') == 'owner.v3' else {}),
            **({'context_protocol': self.state['config']['context_protocol']}
                if self.state['config'].get('context_protocol') in _CONVERSATION_CONTEXT_PROTOCOLS else {}),
            **({'closing_action_contract': (value.closing_contract if conversation and value.closing_contract else
                'lifecycle_step.v1' if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES
                else 'terminal_batch.v1' if self.state['config'].get('completion_policy') == _OPERATION_EXIT
                else 'single_terminal.v1')}
                if closing else {}),
            'input_projection': {
                'resources': projection['resources'],
                **({'candidate_handoff': projection['candidate_handoff']} if 'candidate_handoff' in projection else {}),
                **({'professional_outcomes': projection['professional_outcomes']}
                    if 'professional_outcomes' in projection else {}),
                **({'delivery_conditions': projection['delivery_conditions']}
                    if 'delivery_conditions' in projection else {}),
                'tool_names': [row['function']['name'] for row in schemas],
                **({'work_selection':projection['work_selection']} if 'work_selection' in projection else {}),
                'material_supply': projection['material_supply'],
                **({'operation_overview': {key: value for key, value in
                        projection['working_view']['operation_overview'].items() if key != 'entries'}}
                    if 'operation_overview' in projection.get('working_view', {}) else {}),
                **({'working_context':projection['working_context']}
                    if 'working_context' in projection else {}),
                'supplied': [{key: row[key] for key in ('path', 'sha256', 'record', 'role', 'evidence_role',
                        'offset_bytes', 'next_offset_bytes', 'supplied_offset_bytes',
                        'supplied_next_offset_bytes', 'version_scope', 'overlap_scope') if key in row}
                    | ({'passage_range': {key: row['passage'][key] for key in
                            ('chunk_id', 'character_start', 'character_end') if key in row['passage']}}
                        if 'passage' in row else {})
                    for row in [*projection['retained_text_windows'], *projection['retained_source_passages'],
                        *(projection['material_supply']['native_context']
                            if projection.get('context_protocol') in _CONVERSATION_CONTEXT_PROTOCOLS else []),
                        *(entry['observation'] for entry in projection.get('working_view', {}).get('observations', ())
                          if entry['version_status'] == 'matching')]],
                'work_inputs': [{key: value for key, value in row.items() if key != 'data'} |
                    {'data': {key: value for key, value in row.get('data', {}).items() if key != 'text'}}
                    for row in projection.get('work_inputs', {}).get('windows', [])]},
            'scope': 'Input offer and estimated follow-up cost, not provider usage or guaranteed capacity. '
                'closing_required binds this decision reply and survives approval/recovery.'}
        context.store.write_json('decision_budget.json', binding)
        if conversation is not None:
            message = conversation.call(value, native=True, label='native-turn', call_id=context.attempt.attempt_id)
        else:
            with self.ledger.protect_remaining(floor if protect else {}):
                message = client.chat_turn(messages, tools=schemas,
                    label="native-turn", budget_call_id=context.attempt.attempt_id)
        if binding['tool_admission'] == 'work_selection.v1':
            binding['work_choice_sha256'] = hashlib.sha256(json.dumps(message, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            context.store.write_json('decision_budget.json', binding)
            raw = context.store.write_json('work_choice.json', message, kind='work_choice')
            memory_refs = (*memory_refs, raw)
            message = self._selected_reply(context, message, binding)
        ref = context.store.write_json("native_result.json", message, kind="native_reply")
        return CapabilityResult(status="completed", artifacts=(*memory_refs,ref))

    def _tool(self, *, context: Any, call: dict[str, Any], recovering: bool = False) -> CapabilityResult:
        pending = self.state.get('pending') or {}
        refusal = (self._execution_rejection(pending)
            if pending.get('attempt') == context.attempt.attempt_id and pending.get('execution_rejection')
            else self._offered_tool_refusal(call))
        if refusal is not None:
            payload = refusal.model_dump(mode='json')
            context.store.write_json('native_result.json', payload, kind='native_observation')
            from simple_ar.tools.professional_outcomes import native_result_digest
            return CapabilityResult(status='completed', artifacts=self._native_artifacts(
                context.store, payload, name=call['function']['name']),
                provenance={'native_result_sha256': native_result_digest(payload)} if not recovering else {})
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be an object")
            if (self.operations is None and call['function']['name'] == 'prepare_implementation'
                    and (self.state['config'].get('professional_work') == 'outcomes.v2'
                        or (self.state['config'].get('professional_work') == _REQUEST_OWNER
                            and self.state['turns'][-1].get('origin') == 'request_owner'))):
                # A default in the displayed contract must have the same meaning
                # when omitted by caller/provider. Persisted historical tasks
                # keep the original preparation-only default.
                arguments.setdefault('complete_work', True)
            if self.operations is None and call['function']['name'] == 'prepare_implementation':
                # Hidden schemas are not authority. Derive internal markers
                # from saved session/owner contracts, including their absence.
                retained = ArtifactStore(context.store.root / 'implementation')
                if retained.exists('request.json'):
                    frozen = retained.read_json('request.json')['request']
                elif retained.exists('selection-input.json'):
                    frozen = retained.read_json('selection-input.json')['request']
                elif arguments.get('repair_execution') or arguments.get('repair_review'):
                    from simple_ar.tools.implementation_work import _execution, _review_execution, _request
                    if arguments.get('repair_execution') and arguments.get('repair_review'):
                        raise ValueError('Repair diagnoses are mutually exclusive')
                    previous, _ = (_execution(self.implementation, arguments['repair_execution'])
                        if arguments.get('repair_execution') else
                        _review_execution(self.implementation, arguments['repair_review']))
                    original, _ = _request(self.implementation, previous['prepared_result'])
                    frozen = original.model_dump()
                elif (self.state['config'].get('professional_work') in _SHARED_OWNER_MODES
                        and arguments.get('complete_work') is True):
                    frozen = {'work_contract': ('shared.v2'
                        if self.state['config'].get('professional_work') in _ADOPTING_OWNER_MODES else 'shared.v1'),
                        'candidate_admission': 'python_candidate_admission.v1'}
                    if self.state['config'].get('producer_completion') is not None:
                        frozen['producer_completion'] = self.state['config']['producer_completion']
                    if self.state['config'].get('result_investigation') is not None:
                        frozen['result_investigation'] = self.state['config']['result_investigation']
                else:
                    frozen = {key: self.state['config'][key] for key in ('producer_completion',)
                        if self.state['config'].get(key) is not None}
                for field in ('work_contract', 'candidate_admission', 'producer_completion', 'result_investigation'):
                    expected = frozen.get(field)
                    if (field != 'producer_completion' and expected is not None
                            and (self.state['config'].get('professional_work') not in _SHARED_OWNER_MODES
                                or expected == 'shared.v2'
                                    and self.state['config'].get('professional_work') not in _ADOPTING_OWNER_MODES)):
                        raise ValueError('Historical sessions cannot acquire a new internal work contract')
                    if field in arguments and arguments[field] != expected:
                        raise ValueError(f'{field} conflicts with the saved ownership contract')
                    if field == 'result_investigation' and expected is not None and (
                            self.state['config'].get(field) != expected):
                        raise ValueError('Historical tasks cannot acquire execution investigation')
                    if expected is not None:
                        arguments[field] = expected
        except (OSError, ValueError, KeyError, TypeError) as exc:
            error = f"Invalid tool arguments: {type(exc).__name__}"
            if (isinstance(exc, json.JSONDecodeError)
                    and exc.doc == call['function']['arguments']):
                error += f" ({exc.msg}, line {exc.lineno}, column {exc.colno})"
            payload = {"status": "blocked", "error": error}
        else:
            terminal_error = self._terminal_error(call)
            spec = self.gateway.registry.get(call['function']['name'])
            professional_request = self.gateway.requires_followup(call['function']['name'], arguments)
            # A domain's explicit terminal operation already owns its actual
            # result and delivery gate (e.g. final review/export). It needs no
            # subsequent model decision, unlike an ordinary professional call.
            # An explicit caller action likewise needs no speculative model
            # reservation; any model it actually invokes still uses its owner,
            # saved connection and the original physical ledger.
            needs_followup = not (self._terminal_batch() or self._last_available_action() or self._owner_finishes_request(call)
                or self._caller_takeover()
                or bool(self.state['turns'] and self.state['turns'][-1].get('origin') == 'caller'))
            client = (self._client(context.attempt.attempt_id)
                if needs_followup and self.state['config']['model'] is not None else None)
            floor = self._completion_floor(client, dispatch=True) if needs_followup else {}
            if terminal_error:
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error=terminal_error, data={'executed': False})
            elif (self.operations is not None and self.operations.work is not None
                    and call['function']['name'] in self.operations.work.scope_tools
                    and (reason := self.operations.work.transition_error(call,
                        self.session.store.read_json(self.state['turns'][-1]['reply']).get('tool_calls', [])))):
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error=reason,
                    data={'executed': False})
            elif (self.operations is not None and self.operations.work is not None
                    and (reason := self.operations.work.protect_prior(call['function']['name'], arguments))):
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error=reason, data={'executed': False})
            elif (spec is not None
                    and spec.domain == 'scientific' and spec.permission_level == 'execution'
                    and (issues := self.scientific.execution_issues())):
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error='; '.join(issues), data={'executed': False})
            elif (call['function']['name'] == 'focus_work'
                    and len(self.session.store.read_json(
                        self.state['turns'][-1]['reply']).get('tool_calls', [])) != 1):
                result = ToolResult(tool_name='focus_work', status='blocked',
                    error='Choose a work focus alone; queued actions must not silently change model ownership.')
            elif professional_request and not self.gateway.local_continuation(call['function']['name']):
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error='This historical task retains its original separate-operation contract; complete-work continuation requires a new task.',
                    data={'executed': False})
            elif professional_request and len(self.state['turns'][-1]['remaining']) > 1:
                result = ToolResult(tool_name=call['function']['name'], status='blocked',
                    error='A complete professional work unit must be last in this batch; dependent work uses its resulting artifacts.',
                    data={'executed': False})
            elif (owner := self.gateway.session_owner(call['function']['name'])) is not None:
                pending = self.state['pending'] or {}
                approval = pending.get('execution_approval') or pending.get('implementation_approval')
                requires_approval = self.gateway.registry.get(call['function']['name']).permission_level == 'execution'
                if requires_approval and not approval:
                    # Invalid proposals cannot obtain approval. Return the
                    # owner's actual preflight diagnosis so the next decision
                    # can repair the contract rather than repeat an opaque gate.
                    try:
                        self.execution_binding(call)
                    except (ValueError, TypeError, KeyError, OSError) as exc:
                        result = ToolResult(tool_name=call['function']['name'], status='error',
                            error=f'Execution proposal is invalid: {exc}; no execution occurred',
                            data={'executed': False})
                    else:
                        result = ToolResult(tool_name=call['function']['name'], status='blocked',
                            error='Execution needs explicit saved approval; no execution occurred')
                elif (requires_approval and not owner.has_started(context.attempt.attempt_id)
                        and approval['token'] != self.approval_token(call)):
                    raise ValueError('Approved execution source or contract changed before execution')
                else:
                    with self.ledger.protect_remaining(floor):
                        result = self.gateway.call(ToolCall(tool_name=call['function']['name'],
                            arguments=arguments, trace_id=context.attempt.attempt_id), context=context, approval=approval)
            else:
                approval = (self.state.get('pending') or {}).get('execution_approval')
                if (call['function']['name'] == 'run_process' and approval
                        and not self.workspace.has_started(context.attempt.attempt_id)
                        and approval['token'] != self.approval_token(call)):
                    raise ValueError('Approved process inputs changed before execution')
                with self.ledger.protect_remaining(floor):
                    result = self.gateway.call(ToolCall(tool_name=call["function"]["name"],
                        arguments=arguments, trace_id=context.attempt.attempt_id))
            payload = result.model_dump(mode="json")
            self._bind_delivery(call, payload, context.store, start_review=not recovering)
        context.store.write_json("native_result.json", payload, kind="native_observation")
        from simple_ar.tools.professional_outcomes import native_result_digest
        # Recovery of an old, unbound observation must not create historical
        # proof. A visual v2 owner receipt retains its original output binding.
        bound = not recovering or (payload.get('metadata') or {}).get('output_binding') == 'owner.v1'
        return CapabilityResult(status='completed', artifacts=self._native_artifacts(
            context.store, payload, name=call['function']['name']),
            provenance={'native_result_sha256': native_result_digest(payload)} if bound else {})


    def _attempt(self, capability: str, pending: dict[str, Any], *,
                 perform: Callable[[str], dict] | None = None) -> str:
        identity = pending["attempt"]
        relative = f"attempts/{identity}/native_result.json"
        existing = next((a for a in self.session.list_attempts() if a.attempt_id == identity), None)
        if existing is not None:
            store = ArtifactStore(self.session.store.root / "attempts" / identity)
            recovered = None
            if existing.capability != capability:
                raise ValueError('Accepted identity belongs to a different operation')
            def check_bound_observation() -> None:
                if not store.exists('capability_result.json') or not store.exists('native_result.json'):
                    return
                digest = store.read_capability_result().provenance.get('native_result_sha256')
                if digest is not None:
                    from simple_ar.tools.professional_outcomes import native_result_digest
                    if digest != native_result_digest(store.read_json('native_result.json')):
                        raise ValueError('Accepted native observation changed; no replay')
            check_bound_observation()
            if capability == 'native_turn' and store.exists('work_choice.json') and not store.exists('native_result.json'):
                recovered = self._model(context=CapabilityContext(store, existing))
            if capability == 'native_action':
                header = {key: value for key, value in pending.items() if key != 'result'}
                if not store.exists('action.json') or store.read_json('action.json') != header:
                    raise ValueError('Retained operation differs from the accepted action; no replay')
                if pending.get('result_sha256') is not None and store.exists('native_result.json'):
                    actual = hashlib.sha256(json.dumps(store.read_json('native_result.json'),
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    if actual != pending['result_sha256']:
                        raise ValueError('Retained conclusion differs from its accepted bytes; no replay')
                self._check_action_inputs(pending)
                recovery = self._action_recovery(pending, perform)
                if existing.status == 'failed' and store.exists('capability_result.json'):
                    if recovery is None:
                        raise RuntimeError('Failed action lacks a proven result; no replay')
                    # Preserve the failed attempt and its unknown charges. A new
                    # receipt records only retrieval from the original owner.
                    recovered_action = {key: value for key, value in pending.items()
                                        if key not in {'result', 'execution_approval'}}
                    recovered_action.update(attempt=identity+'-receipt',
                                            kind='opinion' if pending['kind']=='opinion' else 'receipt',
                                            recovered_from=identity)
                    return self._attempt(capability, recovered_action,
                        perform=lambda _identity: recovery())
                if not store.exists('native_result.json'):
                    if recovery is None:
                        raise RuntimeError('Interrupted operation has no proven closed receipt; no replay')
                    recovered = self._action(context=CapabilityContext(store, existing),
                        accepted=header, perform=lambda _identity: recovery(), recovering=True)
            else:
                confirmed = False
                if store.exists('capability_result.json'):
                    saved = store.read_json('capability_result.json')
                    result = CapabilityResult.from_dict(saved)
                    if result.provenance.get('recovery') == 'explicit_interruption':
                        confirmed = (capability == existing.capability == 'native_tool'
                            and pending.get('kind') == 'tool' and result.status == 'failed'
                            and result.provenance.get('capability') == 'native_tool'
                            and bool(saved.get('session_interruption_reason')))
                        if not confirmed:
                            raise RuntimeError('Saved interruption does not confirm this native tool failure; inspect before recovery')
                    elif (capability == existing.capability == 'native_tool' and existing.status == 'failed'
                            and pending.get('kind') == 'tool' and result.status == 'failed'
                            and result.provenance.get('capability') == 'native_tool'
                            and not result.artifacts
                            and all(ref.path == 'capability_result.json' for ref in existing.outputs)
                            and not any(entry.attempt_id == identity and entry.status in {'reserved', 'unknown'}
                                for entry in self.ledger.entries)):
                        function = pending.get('call', {}).get('function', {})
                        confirmed = self.gateway.can_observe_read_failure(
                            ToolCall(tool_name=function.get('name', ''), trace_id=identity))
                if not self.session.store.exists(relative):
                    call = pending.get("call", {})
                    if capability == 'native_tool' and pending.get('execution_rejection'):
                        recovered = self._tool(context=CapabilityContext(store, existing), call=call, recovering=True)
                    elif confirmed:
                        # Core owns the confirmed failure and accounting. Only fill
                        # the missing native observation; never replay or replace it.
                        ref = self.workspace.locator(str(store.resolve('capability_result.json')))
                        store.write_json('native_result.json', ToolResult(
                            tool_name=call['function']['name'], status='error',
                            error='; '.join(result.diagnostics) or saved.get('session_interruption_reason') or 'Recorded read-only tool failure',
                            data={'status': result.status, 'diagnostics': list(result.diagnostics)},
                            artifacts=[ref], metadata={'capability_result': ref,
                                'provenance': dict(result.provenance)}).model_dump(mode='json'),
                            kind='native_observation')
                    elif capability != "native_tool" or not self.gateway.can_reconcile(
                            ToolCall(tool_name=call["function"]["name"], trace_id=identity)):
                        raise RuntimeError("Interrupted operation has no durable result; inspect receipts before any replay")
                    else:
                        recovered = self._tool(context=CapabilityContext(store, existing), call=call, recovering=True)
                elif capability == 'native_tool' and pending.get('execution_rejection'):
                    if store.read_json('native_result.json') != self._execution_rejection(pending).model_dump(mode='json'):
                        raise ValueError('Saved execution rejection differs from its decision')
                elif (capability == 'native_tool' and self.operations is not None
                        and self.operations.delivery_review is not None
                        and (not store.exists('capability_result.json')
                             or not store.read_capability_result().provenance.get('native_result_sha256'))
                        and (produced := store.read_json('native_result.json')).get('status') == 'ok'
                        and produced.get('metadata', {}).get('delivery_hashes') is not None
                        and 'delivery_review' not in produced.get('metadata', {})
                        and self._terminal_intent(pending['call'])
                        and self._delivery(pending['call'], produced) is not None):
                    # The existing native observation already proves closed,
                    # byte-bound production. Rebind cached review without calling
                    # the producer again, including non-replayable file writes.
                    bound = self._bind_delivery(pending['call'], produced, store)
                    store.write_json('native_result.json', bound, kind='native_observation')
                    recovered = CapabilityResult(status='completed', artifacts=self._native_artifacts(
                        store, bound, name=pending['call']['function']['name']))
                elif (capability == 'native_tool'
                        and store.read_json('native_result.json').get('metadata', {}).get('admission') in {'offered.v1', 'work_selection.v1'}
                        and store.read_json('native_result.json')['metadata'].get('dispatched') is False):
                    # This is an application refusal, not an owner execution to
                    # reconcile. Re-derive it from the frozen offer before adoption.
                    refusal = self._offered_tool_refusal(pending['call'])
                    if refusal is None or store.read_json('native_result.json') != refusal.model_dump(mode='json'):
                        raise ValueError('Saved admission refusal disagrees with the frozen offered contract')
                elif (capability == 'native_tool' and self.operations is not None and self.operations.work is not None
                        and pending.get('call', {}).get('function', {}).get('name') in
                            self.operations.work.scope_tools
                        and store.read_json('native_result.json').get('status') == 'ok'):
                    if not store.exists('professional_receipt.json'):
                        raise ValueError('Professional work receipt is missing; no replay')
                    function = pending['call']['function']
                    retained = self.operations.work.call(function['name'], json.loads(function['arguments']), identity=identity)
                    if retained.model_dump(mode='json') != store.read_json('native_result.json'):
                        raise ValueError('Professional work differs from its retained candidate or request; no replay')
                elif not confirmed and (owner := self.gateway.session_owner(
                        pending.get('call', {}).get('function', {}).get('name', ''))) is not None:
                    observed = owner.execute_tool(pending['call']['function']['name'], CapabilityContext(store, existing),
                        json.loads(pending['call']['function']['arguments']),
                        approval=pending.get('execution_approval') or pending.get('implementation_approval'))
                    if store.read_json('native_result.json') != self._bind_delivery(
                            pending['call'], observed.model_dump(mode='json'), store):
                        raise ValueError('Saved native observation disagrees with its retained domain result')
            if not store.exists("capability_result.json"):
                refs = ((store.ref('action.json', kind='operation_binding'),
                         store.ref('native_result.json', kind='native_observation'))
                    if capability == 'native_action' else
                    (*(self._memory_artifacts(store, identity) if capability == 'native_turn' else ()),
                     *((store.ref('work_choice.json', kind='work_choice'),)
                       if capability == 'native_turn' and store.exists('work_choice.json') else ()),
                     *self._native_artifacts(store, store.read_json('native_result.json'),
                        name=pending.get('call', {}).get('function', {}).get('name', ''),
                        kind='native_result' if capability == 'native_turn' else 'native_observation')))
                store.write_capability_result(recovered or CapabilityResult(status='completed', artifacts=refs))
            check_bound_observation()
            self.session.reconcile_attempt(identity)
        else:
            try:
                if capability == 'native_action':
                    if pending['kind'] in {'model', 'tool'}:
                        # Use the same closed-operation proof as explicit model
                        # continuation; retained liabilities are never settled here.
                        self._check_authorization_operations()
                    self._check_action_inputs(pending, before_execution=True)
                    arguments = {'accepted': {key:value for key,value in pending.items() if key != 'result'},
                                 'perform': perform}
                else:
                    arguments = {"call": pending["call"]} if capability == "native_tool" else {}
                result = self.session.execute_attempt(capability, attempt_id=identity,
                    **({'parent_attempt_id': pending['parent_attempt_id']} if pending.get('parent_attempt_id') else {}),
                    **arguments)
            except (LLMCallStopped, KeyboardInterrupt) as exc:
                # This stack has unwound after an observed caller stop; unlike
                # a lost process, it is known not to dispatch further work.
                # A native decision without a durable reply can be explicitly
                # retried. Tool interruption still belongs to its effect owner.
                # Preserve cached replies/choices for ordinary receipt recovery.
                if isinstance(exc, LLMCallStopped) or capability == 'native_turn':
                    store = ArtifactStore(self.session.store.root / 'attempts' / identity)
                    interrupted = next((row for row in self.session.list_attempts()
                        if row.attempt_id == identity and row.status == 'running'), None)
                    if interrupted is not None and not any(store.exists(name) for name in
                            ('native_result.json', 'work_choice.json', 'capability_result.json')):
                        self.session.recover_interrupted(identity,
                            reason='Caller interrupted model work; retain recorded effects, checkpoints and physical accounting')
                raise
            if result.status != "completed":
                detail = "; ".join(result.diagnostics) or "No diagnostics recorded"
                raise RuntimeError(f"Operation failed; inspect saved diagnostics: {detail}")
        return relative

    def _check_action_inputs(self, pending: dict, *, before_execution: bool = False) -> None:
        """One execution owner verifies the effective inputs it approved."""
        approval = pending.get('execution_approval') or {}
        binding = approval.get('binding')
        call = pending.get('call') or {}
        if not binding or call.get('function', {}).get('name') != 'run_process':
            return
        current = self.workspace.approval_binding(call)
        if 'cwd' not in binding:
            # Old approved actions retain the binding that was actually saved.
            current.pop('cwd', None)
        if before_execution:
            if current != binding:
                raise ValueError('Approved operation inputs changed before execution')
            return
        # A successful in-place output has a new recorded version; unrelated
        # command files and all other inputs still need their approved bytes.
        output_versions = self.workspace._completed_process_outputs(binding.get('artifacts'), pending['attempt'])
        outputs = self.workspace.resolved_versions(output_versions)
        cwd = Path(binding.get('cwd', self.workspace.workspace))
        changed_files = [path for path, digest in binding['files'].items()
            if current['files'].get(path) != digest
            and not ((cwd / path).resolve() in outputs
                     and outputs[(cwd / path).resolve()] == current['files'].get(path))]
        if (any(current.get(key) != binding.get(key) for key in ('call', 'argv', 'cwd', 'environment'))
                or changed_files):
            raise ValueError('Approved operation inputs changed before receipt recovery')
        if binding.get('artifacts') is not None:
            self.workspace._check_process_inputs(binding['artifacts'], identity=pending['attempt'],
                completed_outputs=output_versions)

    def run(self, *, feedback: str = "", submission_id: str | None = None,
            independent_review_required: bool | None = None,
            approved_execution: str = "", remember_execution: bool = False,
            actions: list[dict[str, Any]] | None = None,
            retry_model: bool = False, retry_model_selector: str = "",
            read_only_sources: tuple[Path, ...] = (), allow_execution: bool = False,
            allow_network: bool = False, allow_vision: bool = False,
            resource_allowances: dict[str, int | float] | None = None,
            additional_attempts: int | None = None, additional_no_progress: int | None = None,
            authorization_id: str = "", authorization_reason: str = "",
            decision: dict[str, str] | None = None,
            approve: Callable[[dict[str, Any], str], bool | ExecutionRejection] | None = None,
            progress: Callable[[str], None] = print,
            should_stop: Callable[[], bool] | None = None,
            finish_declared_work: bool = False) -> dict[str, Any]:
        if remember_execution and not approved_execution:
            raise ValueError('Remembering execution requires the exact pending approval token')
        if independent_review_required is not None and (type(independent_review_required) is not bool
                or not feedback.strip()):
            raise ValueError('A review selection requires feedback and a boolean choice')
        if submission_id is not None:
            if (actions is not None or approved_execution or retry_model or retry_model_selector
                    or read_only_sources or allow_execution or allow_network or allow_vision
                    or resource_allowances or additional_attempts or additional_no_progress
                    or authorization_id or authorization_reason or decision is not None):
                raise ValueError('Run a feedback submission separately from actions, retries or authorization')
            if feedback:
                self.submit_feedback(self.session.store.root, submission_id=submission_id, feedback=feedback,
                    independent_review_required=independent_review_required)
            else:
                self.feedback_status(self.session.store.root, submission_id=submission_id)
            feedback = ''  # The reception journal, not the legacy string path, owns adoption.
        observer = progress
        for extension in self.extensions:
            extension.verify()

        def progress(message: str) -> None:
            nonlocal observer
            if observer is not None:
                try:
                    observer(message)
                except BrokenPipeError:
                    # A disconnected display is not a failed model/tool action.
                    # Stop notifying this sink; durable task records still own progress.
                    observer = None

        with self.session.mutation_scope():
            from simple_ar.core.feedback import FeedbackSubmissions
            if (self.session.store.exists(FeedbackSubmissions.path)
                    and self.session.store.read_json('actions.json') != self.state):
                raise RuntimeError('Request journal changed since loading; reload before adopting feedback')
            if submission_id is not None and self.feedback_status(
                    self.session.store.root, submission_id=submission_id)['status'] == 'applied':
                return self.result_snapshot()  # A transport retry is never an execution retry.
            if self.state.get('pending_supply') is not None:
                raise ValueError('Complete the retained caller supply before running, revising or authorizing work')
            if decision is not None:
                if (feedback or actions is not None or retry_model or approved_execution
                        or read_only_sources or allow_execution or allow_network or allow_vision
                        or resource_allowances or additional_attempts or additional_no_progress
                        or authorization_id or authorization_reason):
                    raise ValueError('Confirm a design separately from feedback, retries or new access/resources')
                pending = self.state['pending']
                active = pending.get('action') if pending and pending.get('inspection') else pending
                if active and (active['kind'] == 'model' or any(
                        attempt.attempt_id == active['attempt'] for attempt in self.session.list_attempts())):
                    raise ValueError('Reconcile the started operation before recording a design decision')
                if decision.get('decision_guidance') and decision.get('decision_response') != 'revise':
                    raise ValueError('Design guidance requires a revise response, not implicit conditional acceptance')
                if decision.get('decision_response') in {'reject', 'revise'}:
                    self._check_cancellation()
                awaiting = self.sources.pending_decision()
                response = self.sources.respond_decision(**decision)
                if response.get('proposal_kind') == 'caller_execution_protocol':
                    if awaiting is None and not self.state['pending'] and not any(
                            row['remaining'] for row in self.state['turns']):
                        return self.result_snapshot()
                    if response['decision_response'] == 'accept':
                        # Acknowledging the same caller contract is not a new
                        # goal: preserve its queued, as-yet unapproved commands.
                        if self.session.manifest.status not in {'created', 'running'}:
                            self.session.continue_with_revision('Accepted caller execution protocol')
                    else:
                        feedback = 'Caller protocol decision (not process authorization): ' + json.dumps(response, ensure_ascii=False)
                else:
                    feedback = 'Caller design decision (not process authorization): ' + json.dumps(response, ensure_ascii=False)
            if (actions is not None and self.state['pending']
                    and self.state['pending']['kind'] == 'model'
                    and (feedback or approved_execution or read_only_sources or allow_execution
                        or allow_network or allow_vision or resource_allowances or additional_attempts or additional_no_progress
                        or authorization_id or authorization_reason)):
                raise ValueError('Submit caller takeover separately from feedback or authorization changes')
            if retry_model_selector and not retry_model:
                raise ValueError('An explicit replacement connection requires --retry-model')
            if actions is not None and not feedback:
                # Crash position cannot turn an accepted delivery into an
                # uncommitted task that accepts new work without feedback.
                # Explicit feedback may instead correct an uncommittable
                # changed output; its original observation remains retained.
                self._consume_feedback()
                self._commit_terminal(progress)
            if retry_model:
                if (feedback or actions is not None or approved_execution or read_only_sources or allow_execution
                        or allow_network or allow_vision or resource_allowances or additional_attempts or additional_no_progress
                        or authorization_id or authorization_reason):
                    raise ValueError('Use --retry-model separately from feedback, access, resource or tool authorization')
                self._retry_model(retry_model_selector)
            capacity = bool(resource_allowances or additional_attempts or additional_no_progress)
            previous_grant = self.session.manifest.budget.continuation_authorizations.get(authorization_id.strip())
            resume_after = previous_grant.get('resume_after') if previous_grant is not None else None
            if capacity and previous_grant is None and not feedback and self.session.manifest.status == 'partial':
                if not self._commit_terminal(progress):
                    raise ValueError('Reconcile the partial delivery before continuing it')
                resume_after = self.state['turns'][-1]['tools'][-1]['result']
            self._authorize_resources(resource_allowances or {}, authorization_id, authorization_reason,
                additional_attempts=additional_attempts, additional_no_progress=additional_no_progress,
                resume_after=resume_after)
            authorization = self._authorize(read_only_sources, execution=allow_execution,
                                            network=allow_network, vision=allow_vision)
            if authorization:
                feedback = authorization + ('\n' + feedback if feedback else '')
            if feedback:
                self._apply_feedback(feedback, independent_review_required=independent_review_required)
            if self.session.manifest.status == 'partial' and not feedback:
                resumed = self._partial_resource_continuation()
                if resumed is not None:
                    self.session.continue_with_revision('Explicit resource continuation: ' + resumed)
            if actions is not None:
                self._submit_actions(actions)
            if (resource_allowances or additional_attempts or additional_no_progress) and self.session.manifest.status in {'paused', 'blocked', 'failed'}:
                self.session.continue_with_revision('Explicit resource continuation: ' + authorization_id)
            resumed = None
            try:
                self._consume_feedback()
                if self._commit_terminal(progress):
                    return self.result_snapshot()
                if self.session.manifest.status in {'completed', 'partial'} and not feedback:
                    return self.result_snapshot()
                pending = self.state['pending']
                turns = self.state['turns']
                # Caller-submitted actions have not been decided by the model.
                # Defer connection resolution until an action actually needs it;
                # existing pending/started work keeps its original preflight.
                caller_queue = (pending is None and turns and turns[-1].get('origin') == 'caller'
                    and turns[-1].get('request_count', 1) == len(self.state['requests'])
                    and bool(turns[-1].get('remaining'))
                    and not any(attempt.status == 'running' for attempt in self.session.list_attempts()))
                if (self.state['config']['model'] is not None and not caller_queue
                        and not (self._caller_takeover()
                            and (pending is None or pending['kind'] == 'tool'))):
                    self._client()  # Existing saved model work cannot change identity on resume.
                if self.session.manifest.status == "paused" and not feedback:
                    pending = self.state['pending']
                    if pending is None and self.session.manifest.status_reason != 'Caller requested boundary stop':
                        turns = self.state['turns']
                        closed_work = (not self.ledger.unknown_dimensions(current_allowance_only=True)
                            and not any(entry.status == 'reserved' for entry in self.ledger.entries))
                        correctable_text = self.operations is not None and self._unacted_reply_count()==1 and closed_work
                        # A saved request can fail preflight before any native
                        # turn starts. After conditions are restored, continue
                        # that request rather than requiring duplicate feedback.
                        unstarted_request = (self.operations is not None and closed_work
                            and (not turns or turns[-1].get('request_count',1) < len(self.state['requests'])))
                        if not (correctable_text or unstarted_request) and (not turns or turns[-1].get('origin') != 'professional'
                                or turns[-1].get('request_count', 1) != len(self.state['requests'])):
                            if not finish_declared_work or not self._queue_declared_completion(should_stop):
                                return self.result_snapshot()
                            unstarted_request = True  # The caller's retained completion is now queued.
                        if not (correctable_text or unstarted_request):
                            retained = self.session.store.read_json(turns[-1]['reply'])
                            # Historical professional stops still require their
                            # owner's handback; recovery never replays the work.
                            completed_outcome = (turns[-1]['tools'] and not turns[-1]['remaining']
                                and not retained.get('owner_stop'))
                            if not completed_outcome:
                                prepared = self._resume_professional_stop(retained)
                                if prepared is None:
                                    return self.result_snapshot()
                                resumed = (turns[-1]['reply'], prepared)
                    attempts = self.session.list_attempts()
                    active = pending.get('action') if pending and pending.get('inspection') else pending
                    if self.session.manifest.status == 'paused' and (active is None or not any(
                            a.attempt_id == active['attempt'] and a.status == 'running' for a in attempts)):
                        self.session.continue_with_revision('Resume pending work without resetting its budget')
                previous_process_progress = getattr(self.workspace, 'process_progress', None)
                self.workspace.process_progress = progress
                try:
                    with llm_call_stop_scope(should_stop):
                        self._run(approved_execution, approve, progress, should_stop, remember_execution, resumed=resumed)
                        if finish_declared_work and self._queue_declared_completion(should_stop):
                            self._run('', None, progress, should_stop)
                finally:
                    self.workspace.process_progress = previous_process_progress
            except LLMCallStopped:
                # Preserve any budget block recorded by the attempt owner.
                if self.session.manifest.status != 'blocked':
                    self.session.manifest.status = 'paused'
                    self.session.manifest.status_reason = 'Caller requested boundary stop'
                self._save()
            except (Exception, KeyboardInterrupt) as exc:
                if self.session.manifest.status != "blocked":
                    self.session.manifest.status = "paused"
                self.session.manifest.status_reason = f"{type(exc).__name__}: {exc}"
                self._save()
            return self.result_snapshot()

    def _queue_declared_completion(self, should_stop: Callable[[], bool] | None) -> bool:
        """Finish caller-declared local work under the same task lock and stop."""
        if (self.session.manifest.status_reason != _CALLER_ACTIONS_CONSUMED
                or self.session.manifest.budget.exhausted()
                or (should_stop is not None and should_stop())):
            return False
        from simple_ar.app.research_intake import declared_collection_completion
        actions = self.measurement_completion_actions() or declared_collection_completion(self)
        if not actions:
            return False
        self._submit_actions(actions)
        return True

    def _approve_action(self, pending: dict, approved_execution: str, approve: Any,
                        progress: Callable[[str], None], *, scoped: bool = False,
                        remember_execution: bool = False) -> bool:
        """One protocol and exact-approval boundary for accepted process actions."""
        check_llm_call_stop()
        if pending.get('execution_rejection'):
            return True  # The saved decision has no process effect to authorize.
        call = pending['call']
        name = call['function']['name']
        spec = self.gateway.registry.get(name)
        if (spec is not None and spec.permission_level == 'execution'
                and (decision := self.sources.pending_decision())
                and (decision.get('proposal_kind') == 'caller_execution_protocol' or name != 'prepare_code_task')):
            self.session.pause('Protocol requires an explicit bound user decision before process approval')
            progress('Pending protocol decision: ' + json.dumps(decision, ensure_ascii=False))
            return False
        if (self.state['config']['allow_execution'] and spec is not None
                and spec.permission_level == 'execution' and self.gateway.policy.is_allowed(spec)[0]
                and (scoped or not self._offered_tool_error(call) and not self._terminal_error(call))):
            try:
                work_blocked = (self.operations is not None and self.operations.work is not None
                    and self.operations.work.protect_prior(name, json.loads(call['function']['arguments'])))
                offered = None if work_blocked else self.execution_approval(call)
                token = offered['token'] if offered is not None else None
            except (ValueError, TypeError, KeyError, OSError):
                token = None  # The ordinary validator returns the invalid proposal, without execution.
            started = self.gateway.has_started(ToolCall(tool_name=name, trace_id=pending['attempt']))
            grants = self.state.get('execution_grants', [])
            reusable = name in {'run_process', 'export_report'}
            grant = None
            if token is not None and not started and reusable and (grants or remember_execution):
                binding = {key: value for key, value in offered['binding'].items() if key != 'call'}
                if name == 'export_report' or (binding.get('artifacts') or {}).get('inputs'):
                    grant = self.workspace.approval_digest({
                        'name': name, 'arguments': json.loads(call['function']['arguments']), 'binding': binding})
            owner = self.gateway.domain_owner(name)
            scope_check = getattr(owner, 'within_task_scope', None)
            task_scope = bool(token is not None and scope_check is not None
                              and scope_check(name, offered['binding']))
            if token is not None and not started and not task_scope and approved_execution != token and grant not in grants:
                decision = approve(call, token) if approve else False
                if isinstance(decision, ExecutionRejection):
                    pending['execution_rejection'] = decision.reason.strip()
                    self._save()
                    return True
                if not decision:
                    self.session.pause(f'Process requires explicit approval; token={token}')
                    progress(f"Pending command: {call['function']['arguments']}\nApproval token: {token}")
                    if name in {'prepare_code_task', 'implement_code_task'}:
                        progress('Frozen CodeTask contract: ' + json.dumps(self.implementation.exact.model_dump(), ensure_ascii=False))
                    return False
                offered = self.execution_approval(call)
                if token != offered['token']:
                    raise ValueError('Execution inputs changed during approval; approve the current identity')
            check_llm_call_stop()
            if token is not None and not started:
                approval = (offered
                    if self.gateway.session_owner(name) is not None or name == 'run_process'
                    else {'token': token})
                if remember_execution and approved_execution == token and grant is not None and grant not in grants:
                    self.state['execution_grants'] = [*grants, grant]
                pending['execution_approval'] = approval
                self._save()
        return True

    @staticmethod
    def _execution_rejection(pending: dict) -> ToolResult:
        return ToolResult(tool_name=pending['call']['function']['name'], status='blocked',
            error='Execution declined: ' + pending['execution_rejection'],
            data={'executed': False}, metadata={'approval_decision': 'rejected'})

    def _action(self, *, context: CapabilityContext, accepted: dict,
                perform: Callable[[str], dict], recovering: bool = False) -> CapabilityResult:
        """Run one accepted operation; its origin supplies work, not lifecycle control."""
        ref = context.store.write_json('action.json', accepted, kind='operation_binding')
        try:
            observed = perform(context.attempt.attempt_id)
        except LLMCallStopped:
            if any(row.attempt_id == context.attempt.attempt_id for row in self.ledger.entries):
                raise
            observed = ToolResult(tool_name='native_action', status='blocked',
                error='Caller stopped before provider dispatch; no independent opinion was produced',
                metadata={'stopped_before_dispatch': True}).model_dump(mode='json')
        except LLMError as exc:
            if (accepted.get('kind') != 'model' or not isinstance(exc.__cause__, BudgetError)
                    or any(row.attempt_id == context.attempt.attempt_id for row in self.ledger.entries)):
                raise
            observed = ToolResult(tool_name='native_action', status='blocked', error=str(exc),
                metadata={'budget_rejected_before_dispatch': True}).model_dump(mode='json')
        if accepted.get('result_sha256') is not None and accepted['result_sha256'] != hashlib.sha256(
                json.dumps(observed, sort_keys=True, ensure_ascii=False).encode()).hexdigest():
            raise ValueError('Operation result differs from its accepted conclusion')
        raw = context.store.write_json('native_result.json', observed, kind='native_observation')
        from simple_ar.tools.professional_outcomes import native_result_digest
        bound = ((not recovering and accepted.get('kind') != 'receipt')
                 or accepted.get('result_sha256') is not None
                 or (observed.get('metadata') or {}).get('output_binding') == 'owner.v1')
        return CapabilityResult(status='completed', artifacts=(ref, raw),
            provenance={'native_result_sha256': native_result_digest(observed)} if bound else {})

    def _inspection_dispatch(self, scope: str, kind: str, position: str, call: dict | None,
                             perform: Callable[[str], dict], approved_execution: str,
                             approve: Any, progress: Callable[[str], None],
                             remember_execution: bool = False) -> dict:
        """Translate a professional proposal into the common accepted-action path."""
        if self._consume_feedback():
            raise _ActionSuspended()  # Prior child receipts stay with their original owner.
        origin = {'scope': scope, 'position': position, 'kind': kind}
        parent = self.state['pending']
        retained = parent.get('action')
        retry_of = (retained.get('retry_of', '') if retained and retained.get('origin') == origin else '')
        identity = self._professional_action_identity(origin, retry_of)
        action = {'attempt': identity, 'kind': kind, 'origin': origin,
                  **({'retry_of': retry_of} if retry_of else {}),
                  **({'call': deepcopy(call)} if call is not None else {})}
        if kind == 'opinion':
            expected = perform(identity)  # Pure, already-completed professional conclusion.
            action['result_sha256'] = hashlib.sha256(json.dumps(expected,
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if retained is not None and retained.get('origin') == origin:
            if any(retained.get(key) != value for key, value in action.items()):
                raise ValueError('Accepted operation differs from its retained proposal; no replay')
            action = retained
        else:
            if retained is not None and not retained.get('result'):
                raise ValueError('An accepted action still needs its actual result; no replacement')
            parent['action'] = action
            self._save()
        if not action.get('result'):
            if kind == 'tool' and not self._approve_action(action, approved_execution,
                    approve, progress, scoped=True, remember_execution=remember_execution):
                self._consume_feedback()
                raise _ActionSuspended()
            if self._consume_feedback():
                raise _ActionSuspended()
            progress(f'Independent {kind} — {identity}')
        fresh = not any(row.attempt_id == identity for row in self.session.list_attempts())
        if action.get('execution_rejection'):
            perform = lambda _identity: self._execution_rejection(action).model_dump(mode='json')
        relative = self._attempt('native_action', action, perform=perform)
        action['result'] = relative
        self._save()  # Retained until the professional journal acknowledges this receipt.
        observed = self.session.store.read_json(relative)
        if fresh and observed.get('metadata', {}).get('stopped_before_dispatch'):
            raise LLMCallStopped()
        return {'result': observed, 'attempt': identity, 'record': relative,
                'execution_binding': (action.get('execution_approval') or {}).get('binding')}

    def _advance_inspection(self, pending: dict, approved_execution: str,
                            approve: Any, progress: Callable[[str], None],
                            remember_execution: bool = False) -> str | None:
        scope = pending['inspection']
        dispatch = lambda kind, position, call, perform: self._inspection_dispatch(
            scope, kind, position, call, perform, approved_execution, approve, progress, remember_execution)
        try:
            direct = pending['call']['function']['name'] == 'inspect_artifacts'
            arguments = self._recorded_arguments(pending['call'])
            capacity = (nullcontext() if direct and (self._terminal_batch() or self._caller_takeover())
                else self._review_capacity(self._client(scope), candidate=not direct
                    and self._completion(pending['call']['function']['name']).kind == 'review',
                    partial=isinstance(arguments, dict) and bool(arguments.get('unresolved'))))
            with capacity:
                final = self.operations.inspection.advance(scope, dispatch)
            opinion = dispatch('opinion', 'final-opinion', None,
                lambda _identity: final.model_dump(mode='json'))
            if opinion['result'] != final.model_dump(mode='json'):
                raise ValueError('Accepted opinion differs from its independent owner result')
            if direct:
                relative = opinion['record']
            else:
                # Two real observations: original production and independent
                # opinion. Neither changes the other's identity or bytes.
                pending['review_result'] = opinion['record']
                relative = f"attempts/{pending['attempt']}/native_result.json"
            pending.pop('action', None)
            self._save()
            return relative
        except _ActionSuspended:
            return None

    @staticmethod
    def _pending_inspection(observed: dict) -> str | None:
        if observed.get('status') != 'ok':
            return None
        direct = (observed.get('data') or {}).get('inspection_pending')
        review = ((observed.get('metadata') or {}).get('delivery_review') or {}).get('result') or {}
        return direct or (review.get('data') or {}).get('inspection_pending')

    def _run(self, approved_execution: str, approve: Any, progress: Callable[[str], None],
             should_stop: Callable[[], bool] | None = None, remember_execution: bool = False, *,
             resumed: tuple[str, tuple] | None = None) -> None:
        while True:
            self._consume_feedback()
            caller_takeover = self._caller_takeover()
            reserve_completion = (self.state['config'].get('completion_policy') in _COMPLETION_POLICIES | {_OPERATION_EXIT}
                and self.state['config']['model'] is not None and not caller_takeover)
            if self._commit_terminal(progress):
                return
            if should_stop is not None and should_stop():
                self.session.pause('Caller requested boundary stop')
                self._save()
                return
            pending = self.state["pending"]
            if pending is None:
                if not self._queue_candidate_review():
                    self._continue_professional_work(resumed)
                resumed = None
                if self.session.manifest.status == 'paused':
                    return
                turns = self.state["turns"]
                attempt_space = self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts
                if turns and turns[-1].get("remaining"):
                    call = turns[-1]["remaining"][0]
                    if self._closing_decision() and not (self._terminal_batch() or self._last_available_action()):
                        reason = ('Final decision requires one fitting explicit delivery/question as the '
                            'last action; proposed work retained without execution, not marked complete')
                        self._cancel_unstarted(reason)
                        self.session.pause(reason)
                        self._save()
                        return
                    if (reserve_completion
                            and turns[-1].get('origin') != 'caller'
                            and not self._terminal_intent(call)
                            and not self._terminal_batch()
                            and attempt_space <= 2
                            # Reserve attempts only for an available follow-up,
                            # not at the expense of already accepted work after
                            # model capacity is exhausted. Actual attempt/tool
                            # budgets and approval still gate that work below.
                            and self._can_request(1)
                            and not {'llm_requests', 'total_tokens'}.intersection(
                                self.ledger.unknown_dimensions(current_allowance_only=True))
                            and ((tokens := self.ledger.remaining('total_tokens')) is None
                                or tokens >= self._completion_floor(self._client())['total_tokens'])):
                        if turns[-1].get('origin') == 'professional':
                            source = self.session.store.read_json(turns[-1]['reply'])['source_result']
                            observed = self.session.store.read_json(source)
                            self._pause_professional_stop(source, stop_decision(SimpleNamespace(
                                root=self.session.store.root, session_id=self.session.manifest.session_id), observed,
                                Path(source).parent.name, 'shared_budget_exhausted',
                                'Original task completion reserve blocks further professional work; proposal retained without execution'))
                            return
                        if attempt_space == 2:
                            self._cancel_unstarted('Attempt space reserved for a final decision and explicit delivery/question; no operation executed')
                            self._save()
                            continue
                        self.session.pause('Insufficient attempt space for ordinary work and a follow-up decision; unstarted work retained, not delivered')
                        return
                    parent = turns[-1].get('parents', {}).get(call['id'])
                    pending = {"kind": "tool", "call": call,
                        "attempt": self.session.allocate_attempt_id("native-tool"),
                        **({'parent_attempt_id': parent} if parent is not None else {})}
                else:
                    if decision := self.sources.pending_decision():
                        self.session.pause('Protocol requires an explicit bound user decision')
                        progress('Pending protocol decision: ' + json.dumps(decision, ensure_ascii=False))
                        return
                    if (self.state['config']['model'] is None
                            or caller_takeover):
                        self.session.pause(_CALLER_ACTIONS_CONSUMED)
                        return
                    if not self._can_request(1):
                        self.session.pause("Shared model request allowance exhausted, including professional calls; "
                                           "existing artifacts are retained, not marked complete")
                        return
                    if reserve_completion and attempt_space < 2:
                        self.session.pause('Need attempt space for a decision and its explicit terminal action; no model request sent')
                        return
                    pending = {"kind": "model", "attempt": self.session.allocate_attempt_id("native-turn")}
                self.state["pending"] = pending
                self._save()
            if pending["kind"] == "model":
                progress(f"Observe and choose action — {pending['attempt']}")
                relative = self._attempt("native_turn", pending)
                message = self.session.store.read_json(relative)
                calls = message.get("tool_calls") or []
                ids = [call.get("id") for call in calls]
                if len(set(ids)) != len(ids) or any(not value for value in ids):
                    raise ValueError("Tool call IDs must be present and unique")
                self.state["turns"].append({"reply": relative, "tools": [], "remaining": calls,
                    **({'origin': 'request_owner'}
                        if self.state['config'].get('professional_work') == _REQUEST_OWNER else {}),
                    "request_count": len(self.state["requests"])})
                self.state["pending"] = None
                self._save()
                if self._consume_feedback():
                    continue  # The saved response remains history, not a new request's work.
                if not calls:
                    if (self.operations is not None and self._unacted_reply_count()==1
                            and self._can_request(1)
                            and not self.ledger.unknown_dimensions(current_allowance_only=True)
                            and not any(entry.status == 'reserved' for entry in self.ledger.entries)):
                        progress('Assistant text retained without a delivery; one action correction uses the same budget, no operation replay')
                        continue
                    self.session.pause("Model returned prose without a delivery or question; inspect the saved reply before continuing")
                    return
                continue
            call = pending["call"]
            name = call["function"]["name"]
            if not pending.get('inspection') and not self._approve_action(
                    pending, approved_execution, approve, progress, remember_execution=remember_execution):
                if self._consume_feedback():
                    continue
                return
            if self._consume_feedback():
                continue  # Input received during approval precedes physical dispatch.
            progress(f"Tool {name} — {pending['attempt']}")
            if pending.get('inspection'):
                relative = self._advance_inspection(pending, approved_execution, approve, progress, remember_execution)
                if relative is None:
                    if self.state['pending'] is None:
                        continue  # A received request cancelled only the unstarted scope.
                    return
            else:
                relative = self._attempt("native_tool", pending)
                observed = self.session.store.read_json(relative)
                if scope := self._pending_inspection(observed):
                    pending['inspection'] = scope
                    self._save()
                    continue
            turn = self.state["turns"][-1]
            turn["tools"].append({"id": call["id"], "result": relative,
                **({"review_result":pending["review_result"]} if pending.get("review_result") else {})})
            turn["remaining"].pop(0)
            self.state["pending"] = None
            self._save()
            observed = self.session.store.read_json(relative)
            if (observed.get('status') != 'ok'
                    and (observed.get('data') or {}).get('failure_kind') == 'resource_exhausted'
                    and (self.operations is not None
                        or self.state['config'].get('professional_work') in _PROFESSIONAL_WORK
                        or turn['remaining'])):
                # Resource admission is a shared execution boundary, including
                # direct operations. It must not depend on a retired controller
                # or the number of calls proposed in the current batch.
                # Historical raw single calls keep their closing decision.
                self._continue_professional_work()
                if self.session.manifest.status == 'paused':
                    return
                self.session.pause('Task resource allowance exhausted: ' + observed.get('error', '')
                    + '. Existing artifacts and failure records are retained. '
                    'Authorize resources or revise the request before continuing; no further model investigation requested.')
                self._save()
                return
            decision = (observed.get('data') or {}).get('pending_decision') if observed.get('status') == 'ok' else None
            if decision:
                self.session.pause('Research design requires an explicit bound user decision: ' + decision['decision_id'])
                progress('Pending design decision: ' + json.dumps(decision, ensure_ascii=False))
                return
