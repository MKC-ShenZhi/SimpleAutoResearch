"""Pre-operations.v1 historical session setup and legacy input projection mixin."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simple_ar.app.tool_contracts import (
    _ADOPTING_OWNER_MODES,
    _CreationDeclarations,
    _DECLARED_INPUTS,
    _OUTCOME_WORK,
    _OWNER_MODES,
    _PROFESSIONAL_WORK,
    _REPORT_PROTOCOL,
    _REQUEST_OWNER,
    _ReadHistory,
    _TOOL_CONTRACT_MODES,
    _ToolSelection,
    _WorkFocus,
    _WorkPlan,
    _source_registry,
)
from simple_ar.tools.analysis import AnalysisToolGateway, analysis_tool_specs
from simple_ar.tools.extensions import LocalExtension
from simple_ar.tools.gateway import CommonToolGateway
from simple_ar.tools.images import ImageToolGateway, image_tool_specs
from simple_ar.tools.implementation import ImplementationToolGateway, implementation_tool_specs
from simple_ar.tools.permissions import ToolPermissionPolicy
from simple_ar.tools.publication import (
    PUBLICATION_ASSET_PROTOCOL,
    PUBLICATION_CONSUMER_PROTOCOL,
    PublicationToolGateway,
    publication_tool_specs,
)
from simple_ar.tools.registry import ToolRegistry
from simple_ar.tools.sources import SourceToolGateway
from simple_ar.tools.specs import CommonToolSpec
from simple_ar.tools.visual import VisualToolGateway, visual_tool_specs
from simple_ar.tools.workspace import WorkspaceToolGateway, workspace_tool_specs


class _ToolLegacyMixin:
    def _configure_tools(self) -> None:
        controller = self.session
        config = self.state["config"]
        self.operations = None
        if config.get('control_protocol') is not None:
            self._configure_operations()
            self.gateway.saved_complete_work = config.get('professional_work') in _PROFESSIONAL_WORK
            return
        from simple_ar.tools.writing import WritingToolGateway, writing_tool_specs
        from simple_ar.tools.materials import verify_source_snapshots
        verify_source_snapshots(config['sources'], config.get('source_snapshots', {}))
        if (type(config.get('tool_contracts', 'full')) is not str
                or config.get('tool_contracts', 'full') not in _TOOL_CONTRACT_MODES):
            raise ValueError('Unknown saved tool-contract mode; do not silently change historical tasks')
        if config.get('report_protocol') not in {None, _REPORT_PROTOCOL}:
            raise ValueError('Unknown saved report protocol; preserve its original contract')
        if config.get('publication_assets') not in {None, PUBLICATION_ASSET_PROTOCOL, PUBLICATION_CONSUMER_PROTOCOL}:
            raise ValueError('Unknown saved publication asset protocol; preserve its original contract')
        if config.get('context_protocol') not in {None, 'conversation.v1'}:
            raise ValueError('Unknown saved context protocol; preserve its original contract')
        if config.get('input_contract') not in {None, _DECLARED_INPUTS}:
            raise ValueError('Unknown saved input contract; preserve its original contract')
        if config.get('input_contract') == _DECLARED_INPUTS:
            if config.get('context_protocol') != 'conversation.v1':
                raise ValueError('Declared inputs require their frozen conversation projection')
            _CreationDeclarations.model_validate(config['creation_declarations']).freeze(config['sources'])
        if config.get('professional_work') not in {None, *_PROFESSIONAL_WORK}:
            raise ValueError('Unknown saved professional work contract')
        self.workspace = WorkspaceToolGateway.for_task(controller.store.root, config, self.ledger,
            record_artifacts=self._record_artifacts)
        registry = ToolRegistry()
        registry.register_package(workspace_tool_specs(sandbox_execution=config.get('sandbox_execution', False)), self.workspace,
            request_visible={'ask_user', 'deliver_result', 'read_file', 'list_directory'})
        self.extensions = LocalExtension.register_task_packages(config, registry, self._extension_host_call)
        coordination = [CommonToolSpec(name='read_history', domain='coordination',
            permission_level='read_only', risk_level='low', input_schema=_ReadHistory.model_json_schema(),
            description='Read a bounded byte window of closed accepted turn observation indexes, including archived history and inline cancellation receipts. Turn indexes are zero-based, not attempt numbers. Continue with the returned end_turn pinned and next_offset_bytes while has_more_bytes; otherwise next_turn starts the next range. Original replies and attempt results remain readable at their record locators. Historical identities and statuses are not current file contents, authorization, verified success or resolved failures. No model call or new state.')]
        if config.get('professional_work') not in _OWNER_MODES:
            coordination.append(CommonToolSpec(name='focus_work', domain='coordination',
                permission_level='plan', risk_level='low', input_schema=_WorkFocus.model_json_schema(),
                description='Select the saved code, writing or task connection and goal for subsequent decisions in this same action loop. This operation only records the selection: it does not invoke a model, investigate, edit, execute, inspect or deliver work. Subsequent decisions need their own remaining shared allowance. Optional inputs select necessary raw FileWindow inputs, read under the original read_file permission and saved in this receipt. They take priority over unrelated native history; changed/unavailable/oversized inputs require focused reselection, not user-supplied hashes. No file list or mandatory compose_document step is needed. Writing without an explicit route keeps the original task client. Native work requires Chat mode. Choose task to return to the original task connection. No new subtask, authorization, allowance or delivery. Request this tool alone, not together with other actions.'))
        coordination.append(CommonToolSpec(name='update_work_plan', domain='coordination',
            permission_level='plan', risk_level='low', input_schema=_WorkPlan.model_json_schema(),
            description='Save or replace a concise working plan for this user request. Use after choosing an approach or when observations/feedback change it; may accompany actual work, no extra planning model. The latest plan stays visible across history compaction and resume. Statuses and evidence locators are model-authored intentions/claims, not verified outcomes, approval, execution or delivery. No automatic steps or fixed task pipeline.'))
        if config.get('tool_contracts', 'full') != 'full':
            coordination.append(CommonToolSpec(name='load_tools', domain='coordination',
                permission_level='plan', risk_level='low', input_schema=_ToolSelection.model_json_schema(),
                description='Choose exact named tool contracts from available_specialists for upcoming model decisions. Replaces the previous optional selection; default contracts remain. No model/network call, execution, new permission or task pipeline. Choices are retained through normal action receipts and resume. This operation does not perform the selected work.'))
        registry.register_package(coordination, self, request_visible=True)
        self.writing = WritingToolGateway(controller.store.root, self.workspace, self.ledger,
            config['model'], controller.manifest.session_id,
            client_factory=lambda identity: self._client(identity, purpose='writing', native=False),
            work_context=self._work_context,
            material_observations=self._material_observations,
            report_inputs=self._report_inputs if config.get('report_requirement') or config.get('report_protocol') else None)
        if config.get('professional_inputs') not in {None, 'owner.v1', 'owner.v2', 'owner.v3'}:
            raise ValueError('Unsupported professional input contract')
        self.writing.professional_inputs = config.get('professional_inputs') in {'owner.v1', 'owner.v2', 'owner.v3'}
        self.writing.document_work = config.get('professional_inputs') in {'owner.v2', 'owner.v3'}
        self.writing.document_source_contract = config.get('document_source_contract')
        if self.writing.document_source_contract not in {None, 'document_source.v1'}:
            raise ValueError('Unsupported document work source contract')
        self.writing.joint_delivery = config.get('professional_work') in _OUTCOME_WORK
        self.writing.producer_completion = config.get('producer_completion')
        if config.get('result_investigation') not in {None, 'execution_findings.v1'}:
            raise ValueError('Unsupported result investigation contract')
        if self.writing.producer_completion not in {None, 'goal_findings.v1'}:
            raise ValueError('Unsupported producer completion contract')
        if config.get('professional_work') == _REQUEST_OWNER and self.writing.producer_completion is None:
            from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
            self.writing.declaration_adoption_contract = CLAIM_ADOPTION_V2
        scientific = {}
        self.implementation = ImplementationToolGateway(controller.store.root, self.workspace, self.ledger,
            config['model'], controller.manifest.session_id,
            declaration_review=(config.get('professional_work') in _ADOPTING_OWNER_MODES
                or config.get('producer_completion') == 'goal_findings.v1' and config.get('code_task') is None),
            professional_requests=config.get('professional_work') == _REQUEST_OWNER,
            client_factory=lambda identity: self._client(identity, purpose='code'),
            work_context=self._work_context,
            material_observations=self._material_observations,
            execution_observations=self._execution_observations, exact=config.get('code_task'),
            research_inputs=lambda: self.sources.implementation_evidence(),
            candidate_outputs=lambda record: scientific['scientific'].candidate_outputs(record) if scientific else {},
            measurement_inputs=lambda origin, required: scientific['scientific'].implementation_inputs(
                scientific['scientific'].measurement_refs(self._scientific_observations()), origin, required=required),
            measurement=config.get('execution_contract') if config.get('code_task') is not None else None,
            measurement_supplier=(lambda: scientific['scientific'].execution_contract)
                if config.get('code_task') is not None and config.get('scientific_tools', False) else None)
        if config.get('scientific_tools', False):
            from simple_ar.tools.scientific import ScientificToolGateway
            scientific['scientific'] = ScientificToolGateway(controller.store.root, self.workspace,
                self.ledger, controller.manifest.session_id,
                implementation=self.implementation if config.get('scientific_lineage', False) else None,
                execution_contract=config.get('execution_contract'),
                current_attempts=lambda: {Path(item['result']).parent.name
                    for turn in self._evidence_turns('scientific') for item in turn['tools']
                    if item['result'].startswith('attempts/')} | (
                        {self.state['pending']['attempt']} if self.state['pending'] else set()),
                research_contract=lambda: self.sources.accepted_execution_contract(),
                research_requirements=lambda: self.sources.execution_issues())
        self.scientific = scientific.get('scientific')
        self.sources = SourceToolGateway.for_task(controller.store.root, self.workspace, config,
            client_factory=lambda identity: self._client(identity, purpose='task', native=False),
            feasibility_client_factory=self._feasibility_client,
            registry=_source_registry(self.services, config))
        publication_contract = config.get('publication_preparation')
        if publication_contract not in {None, 'bound_candidate.v1'}:
            raise ValueError('Unsupported publication preparation contract')

        def report_source(path):
            report = self._report_projection()
            return self.writing.publication_source(path,
                self._report_inputs(report=report) if report['requirement'] else None,
                intent=report['intent'], adopted=report['conversion_source'])

        self.publication = PublicationToolGateway(controller.store.root, self.workspace, self.ledger,
            asset_protocol=config.get('publication_assets'),
            candidate_source=self.writing.preparation_source,
            owned_execution=publication_contract == 'bound_candidate.v1',
            report_source=report_source if config.get('report_protocol') == _REPORT_PROTOCOL else None)
        self.writing.check_publication_delivery = self.publication.check_report_delivery
        if publication_contract == 'bound_candidate.v1':
            self.writing.publication_inspection = 'prepared_document.v1'
            self.writing.prepared_publication = lambda ref: self.publication.inspection_source(
                ref, session_id=controller.manifest.session_id)
        # Contracts and owners enter together; discovery and dispatch consume
        # this same registration, including explicitly selected local packages.
        registry.register_package(self.sources.tool_specs(), self.sources, request_visible=True)
        registry.register_package(publication_tool_specs(sandbox_execution=config.get('sandbox_execution', False)),
            self.publication, request_visible=True)
        registry.register_package(writing_tool_specs(), self.writing, request_visible=True)
        registry.register_package(implementation_tool_specs(exact=config.get('code_task') is not None,
            defer_code_review=self.implementation.defer_code_review),
            self.implementation, request_visible=False if self.implementation.exact else {'prepare_implementation'})
        registry.register_package(analysis_tool_specs(),
            AnalysisToolGateway(controller.store.root, self.workspace, contract=config.get('table_analysis')),
            request_visible=True)
        if config.get('scientific_tools', False):
            from simple_ar.tools.scientific import scientific_tool_specs
            registry.register_package(scientific_tool_specs(), self.scientific, request_visible=True)
        if config.get('allow_vision', False):
            registry.register_package(visual_tool_specs(),
                VisualToolGateway(controller.store.root, self.workspace, self.ledger,
                    controller.manifest.session_id, client_factory=lambda identity: self._client(identity, purpose='vision')),
                request_visible=True)
        if config.get('max_images', 0) > 0:
            registry.register_package(image_tool_specs(), ImageToolGateway(self.workspace, self.ledger,
                fallback_models_config=config.get('model_catalogs', {}).get('image')), request_visible=True)
        if config.get('report_protocol') != _REPORT_PROTOCOL:
            # Disabled protocol fields must not consume historical context in
            # every nested delivery schema (or change the old planning shape).
            for spec in registry.list_specs():
                schema = spec.input_schema
                definitions = schema.get('$defs', {})
                definitions.pop('ReportDelivery', None)
                definitions.pop('_ReportIntent', None)
                definitions.get('DeliveryRequest', {}).get('properties', {}).pop('report', None)
                if spec.name == 'deliver_result':
                    schema.get('properties', {}).pop('report', None)
                if spec.name == 'update_work_plan':
                    schema['properties'].pop('report_intent', None)
                    schema['properties']['steps'].pop('default', None)
                    schema['properties']['steps']['minItems'] = 1
                    schema['required'] = ['steps']
        self.gateway = CommonToolGateway(controller.store.root, registry=registry,
            policy=ToolPermissionPolicy.for_task(config, registry.list_specs()),
            saved_complete_work=config.get('professional_work') in _PROFESSIONAL_WORK)

    def _legacy_model_input(self, context: Any, client: Any) -> tuple:
        """Frozen non-journal admission, including its original recovery rules."""
        messages = self._messages(client=client)
        memory_refs = ()
        working_memory = None
        if (self.state['config'].get('context_protocol')=='working.v1'
                and not (self.state.get('pending') or {}).get('skip_memory_checkpoint')):
            projection = json.loads(messages[1]['content'])
            cutoff = projection.get('working_context',{}).get('recent_start_turn',0)
            # Optional continuity work must leave the actual decision affordable.
            with self.ledger.protect_remaining({'llm_requests':1,
                    'total_tokens':self._decision_tokens(client,messages)}):
                memory_refs = self._compact_work(context,client,cutoff)
            if context.store.exists('memory_snapshot.json'):
                snapshot = context.store.read_json('memory_snapshot.json')
                # This historical contract requires the complete turn to commit.
                from simple_ar.app.work_memory import summary_coverage
                coverage = summary_coverage(previous=self._working_memory(),snapshot=snapshot,
                    view=context.store.read_json('memory_input.json'),operations_for_turn=self._memory_operations)
                working_memory = {'notes':self.operations.project_notes(snapshot['notes']),
                                  'through_turn':coverage}
            if memory_refs:
                memory_refs = self._memory_artifacts(context.store,context.attempt.attempt_id)
                messages = self._messages(client=client,working_memory=working_memory)
        floor = self._completion_floor(client, messages)
        remaining = self.ledger.remaining('total_tokens')
        # A last affordable decision can deliver/question, but its retry must
        # not steal a future decision while ordinary work is still affordable.
        protect = bool(floor and self._can_request(2)
            and self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts > 2
            and (remaining is None or remaining >= 2 * floor['total_tokens']))
        closing = self.state['config'].get('completion_policy') == 'closing_decision.v2' and not protect
        schemas = self._closing_schemas() if closing else self.schemas
        if closing:
            # Include the closing contract before context trimming/reservation,
            # not as an unbudgeted suffix added to a capacity-fitted request.
            messages = self._messages(client=client, closing=True, schemas=schemas,working_memory=working_memory)
            floor = {'llm_requests': 1, 'total_tokens': self._decision_tokens(client, messages, schemas=schemas)}
        return messages, schemas, memory_refs, floor, protect, closing
