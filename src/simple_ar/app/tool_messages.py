"""Prompt assembly, message projection, and input-capacity selection for ToolApplication."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from typing import Any

from simple_ar.app.professional_conversation import (
    INTEGRATED_WORK_LIFECYCLES,
    RETAINED_PROFESSIONAL_CONTRACTS,
)
from simple_ar.app.task_view import history_observation
from simple_ar.app.tool_contracts import (
    MANAGEMENT_SYSTEM,
    REQUEST_OWNER_SYSTEM,
    SYSTEM,
    _CLOSING_DECISION,
    _COMPLETION_POLICIES,
    _CONVERSATION_CONTEXT_PROTOCOLS,
    _DECLARED_INPUTS,
    _OPERATION_COMPLETION,
    _OPERATION_EXIT,
    _OPERATION_EXIT_DECISION,
    _OWNER_MODES,
    _REFERENCED_CONTEXT_PROTOCOLS,
    _REPORT_PROTOCOL,
    _REQUEST_OWNER,
    _WORKING_CONTEXT_PROTOCOLS,
)
from simple_ar.core.budget import budget_pacing_notice
from simple_ar.core.working_context import RECEIPT_SUPPLY_POLICIES, history_view, input_capacity
from simple_ar.integrations.llm import (
    LLMClient,
    estimate_native_input_tokens,
    estimate_tokens,
)
from simple_ar.report.legacy_contract import BOUND_REPORT_REVISION
from simple_ar.tools.implementation import ImplementationWorkRequest
from simple_ar.tools.materials import (
    admit_text_window,
    authored_snapshot,
    current_text_window,
    journal_read_windows,
    observe_text_window,
    observed_window_key,
    read_material_window,
    uncovered_text_ranges,
)
from simple_ar.tools.professional_outcomes import accepted_review_journal
from simple_ar.tools.specs import (
    DIRECT_NATIVE_SELECTION_POLICIES,
    GOAL_WORK_METHOD,
    NATIVE_WORK_METHODS,
    ToolResult,
)
from simple_ar.tools.workspace import (
    DeliveryRequest,
    FileWindow,
    workspace_observation_view,
)


class _ToolMessagesMixin:
    """Message projection and capacity budgeting methods for ToolApplication."""

    def _system_text(self, *, closing: bool = False) -> str:
        if self.operations is not None:
            exit_text = (_OPERATION_EXIT_DECISION
                if self.state['config'].get('completion_policy') == _OPERATION_EXIT else _CLOSING_DECISION)
            if getattr(self.operations, 'selection', None) is not None:
                exit_text = self.operations.selection.closing_system
            elif self.state['config'].get('work_method') in NATIVE_WORK_METHODS:
                exit_text = ('This is the last affordable decision. Use exactly one offered delivery '
                    'or indispensable question. Do not propose further reads, edits or computations. '
                    'Reuse an applicable existing review for complete delivery; otherwise deliver '
                    'useful existing artifacts with the actual unfinished work in unresolved. '
                    'Research scope limitations alone do not make finished requested work unresolved.')
            if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES:
                exit_text = ('Shared resources now constrain further production. Use the offered '
                    'owner operations to resolve a concrete outstanding delivery condition when feasible; '
                    'their minimum resource estimates do not guarantee completion. Preserve capacity '
                    'for a truthful exit. Deliver useful existing work with actual unfinished requirements '
                    'in unresolved when checking, publication or repair cannot finish. If no honest '
                    'delivery is ready, prefer an action that directly creates or validates requested work '
                    'without another decision. Preserve genuine gaps for continuation. A candidate review '
                    'is independent evidence, not formal delivery. Final acceptance remains unchanged.')
                if (selection := getattr(self.operations, 'selection', None)) is not None and selection.policy == 'outcomes.v1':
                    exit_text = selection.closing_system
            return self.operations.system_text() + ('\n\n'+exit_text if closing else '')
        # Do not append delegation advice to the old do-everything solver role:
        # professional owners now hold the complete investigation-to-result unit.
        mode = self.state['config'].get('professional_work')
        if closing and self.state['config'].get('input_contract') == _DECLARED_INPUTS:
            return ('Finish the current user request using only the supplied closing tool contracts. '
                'Use retained observations, adopted versions and unresolved findings; do not invent evidence '
                'or treat intentions as completed work. Source declarations are not verification or permission. '
                'Do not load tools or start new investigation. Preserve prior work, accounting and uncertainty.\n\n'
                + _CLOSING_DECISION)
        text = (REQUEST_OWNER_SYSTEM if mode == _REQUEST_OWNER else
            MANAGEMENT_SYSTEM if mode in _OWNER_MODES else SYSTEM)
        return text + ('\n\n' + _CLOSING_DECISION if closing else '')

    def _decision_tokens(self, client: Any, messages: list | None = None, *, schemas: list | None = None) -> int:
        """Same serialized input/output reservation used by explicit retry."""
        return (estimate_native_input_tokens(client, messages if messages is not None else
                    self._messages(client=client), self.schemas if schemas is None else schemas)
                + (client.effective_output_limit() or 0))

    def _completion_floor(self, client: Any, messages: list | None = None, *,
                          dispatch: bool = False) -> dict[str, int]:
        policies = _COMPLETION_POLICIES | {_OPERATION_EXIT} | ({_OPERATION_COMPLETION} if dispatch else set())
        if client is None or self.state['config'].get('completion_policy') not in policies:
            return {}
        if messages is None and self.state['turns']:
            # Reuse this decision's recorded reservation estimate instead of
            # rebuilding history or resolving a second professional client for
            # every read/coordination tool. This is an attempt fact, not memory.
            path = str(Path(self.state['turns'][-1]['reply']).with_name('decision_budget.json'))
            if self.session.store.exists(path):
                estimate = self.session.store.read_json(path)['estimated_followup_tokens']
                if type(estimate) is not int or estimate < 0:
                    raise ValueError('Invalid retained decision reservation estimate')
                floor = {'llm_requests': 1, 'total_tokens': estimate}
                return self._lifecycle_floor(floor, dispatch=dispatch)
        # A temporary spending policy, not a fake provider reservation or new
        # allowance. Every nested physical call still uses this same ledger.
        return self._lifecycle_floor(
            {'llm_requests': 1, 'total_tokens': self._decision_tokens(client, messages)}, dispatch=dispatch)

    def _lifecycle_floor(self, decision: dict[str, int], *, dispatch: bool = False,
                         lifecycle: dict | None = None) -> dict[str, int]:
        """Let owners spend their own floor while ordinary work leaves it available."""
        if self.state['config'].get('work_lifecycle') not in INTEGRATED_WORK_LIFECYCLES:
            return decision
        lifecycle = self.operations.delivery.lifecycle_requirements() if lifecycle is None else lifecycle
        if (self.state['config'].get('work_lifecycle') == 'integrated.v3'
                and lifecycle.get('review_followup')):
            return decision  # Act on the completed opinion before reserving a later check.
        queued = self.state['turns'][-1].get('remaining', []) if self.state['turns'] else []
        if dispatch and queued and queued[0]['function']['name'] in lifecycle['next_tools']:
            return decision
        resources = lifecycle['minimum_resources']
        requests = resources.get('llm_requests', 0)
        # A review uses its own selected evidence, tools and output allowance.
        # Its next request slot is known before that evidence is assembled;
        # the producer's input size is not a cost estimate for the reviewer.
        return {**resources, 'llm_requests': decision['llm_requests'] + requests,
            'total_tokens': decision['total_tokens'] + resources.get('total_tokens', 0)}

    @contextmanager
    def _review_capacity(self, client: Any, *, candidate: bool, partial: bool = False):
        terminal = self._terminal_batch()
        floor = {} if terminal and not candidate else self._completion_floor(client, dispatch=True)
        # A standalone check of work proposed as complete still needs a
        # coordinator decision. An explicitly unfinished candidate can leave
        # an opinion as its partial result. Token forecasts can yield to evidence.
        after_review = False
        if (self.state['config'].get('work_lifecycle') == 'integrated.v3'
                and self.state['turns'] and self.state['turns'][-1].get('origin') != 'caller'):
            if not candidate and terminal:
                # A direct submission can use the last call for its opinion.
                # With spare capacity, preserve recovery if that opinion
                # cannot authorize the submitted delivery.
                available = self.ledger.remaining('llm_requests')
                after_review = available is not None and available > 1
            else:
                after_review = (candidate and not partial and not terminal and floor.get('llm_requests')
                    and self.state['config'].get('independent_review_required'))
        with self.ledger.protect_remaining({'llm_requests': 1} if after_review else {}), \
                self.ledger.protect_remaining(floor, yield_to_work=True):
            yield

    def _terminal_batch(self) -> bool:
        """A fitting task exit or professional return needs no later decision."""
        queued = self.state['turns'][-1].get('remaining', []) if self.state['turns'] else []
        space = self.session.manifest.budget.max_attempts - self.session.manifest.budget.attempts
        if queued:
            path = str(Path(self.state['turns'][-1]['reply']).with_name('decision_budget.json'))
            if (self.session.store.exists(path)
                    and self.session.store.read_json(path).get('closing_action_contract') in {'single_terminal.v1', 'single_action.v2'}
                    and len(queued) != 1):
                return False
        if not queued or len(queued) > space:
            return False
        exits = [self._terminal_intent(call) for call in queued]
        return exits[-1] and sum(exits) == 1

    def _last_available_action(self) -> bool:
        """A final decision may leave offered work for later continuation."""
        if not self.state['turns']:
            return False
        turn = self.state['turns'][-1]
        path = str(Path(turn['reply']).with_name('decision_budget.json'))
        if not self.session.store.exists(path):
            return False
        contract = self.session.store.read_json(path).get('closing_action_contract')
        if contract == 'single_action.v2':
            calls = self.session.store.read_json(turn['reply']).get('tool_calls') or []
            return len(calls) == len(turn.get('remaining', [])) == 1
        if contract != 'batch_actions.v3':
            return False
        # Each proposed operation still passes the normal offered-tool,
        # permission and resource checks. Do not discard an otherwise usable
        # batch merely because the model returned more than one action.
        return bool(turn.get('remaining'))

    def _owner_finishes_request(self, call: dict[str, Any]) -> bool:
        """A bounded owner may finish without a later coordinator decision.

        This only releases the speculative decision floor. The owner must still
        pay every professional call, and only real terminal output can deliver.
        """
        try:
            arguments = json.loads(call['function']['arguments'])
        except (ValueError, TypeError):
            return False
        owner = self.gateway.domain_owner(call['function']['name'])
        accepts = getattr(owner, 'finishes_request', None)
        return bool(isinstance(arguments, dict) and accepts is not None
            and accepts(arguments) and self.state['turns'][-1]['remaining'] == [call])

    def _offered_tool_refusal(self, call: dict[str, Any]) -> ToolResult | None:
        """Admit a new model call against its actual supplied contract.

        This is protocol admission, not a permission grant or a domain route.
        Caller/owner proposals and old accepted replies keep their contracts.
        """
        # Exact caller operations and historical recovery may have no model
        # decision. Admission applies only to a retained, actually offered turn;
        # permissions and the domain contract still validate every dispatch.
        if not self.state['turns']:
            return None
        turn = self.state['turns'][-1]
        if turn.get('origin') in {'caller', 'professional'}:
            return None
        path = str(Path(turn['reply']).with_name('decision_budget.json'))
        if not self.session.store.exists(path):
            return None
        binding = self.session.store.read_json(path)
        contract = binding.get('tool_admission')
        if contract == 'work_selection.v1':
            try:
                if binding.get('selection_error'):
                    raise ValueError(binding['selection_error'])
                raw = self.session.store.read_json(str(Path(turn['reply']).with_name('work_choice.json')))
                expected = self.operations.selection.interpret(raw, binding, self.state['requests'])
                if call not in expected['tool_calls']:
                    raise ValueError('Queued action differs from the retained goal-work decision')
                if (self.operations.selection.policy == 'outcomes.v1'
                        and self.operations.selection.decode(raw, 'outcomes.v1').decision == 'work'):
                    error = self.operations.service_error(self.gateway.registry.get(call['function']['name']),
                        json.loads(call['function']['arguments']), selected_work=True)
                    if error:
                        raise ValueError(error)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                return ToolResult(tool_name=call['function']['name'], status='blocked',
                    error='Work selection refused: '+str(exc), data={'executed':False},
                    metadata={'admission':contract, 'dispatched':False})
            return None
        if contract != 'offered.v1':
            return None
        name, projection = call['function']['name'], binding['input_projection']
        error = ''
        if name not in projection['tool_names']:
            error = 'This tool contract was not supplied to the decision; no operation executed. Use a currently supplied contract.'
            if 'load_tools' in projection['tool_names']:
                error += ' To select an optional contract, use load_tools; selection does not grant permission or resources.'
        elif (self.state['config'].get('work_selection_policy') in DIRECT_NATIVE_SELECTION_POLICIES
                and name in {'review_result', 'deliver_result'}
                and len(self.session.store.read_json(turn['reply']).get('tool_calls', [])) != 1):
            error = ('Observe all selected work before a separate review or delivery decision. '
                'Other selected operations keep their actual receipts; no work is replayed.')
        elif name == 'compose_document' and binding.get('document_request') is not None:
            try:
                if binding['document_request'] != 'work.v2':
                    raise ValueError('Unknown saved document request contract')
                from simple_ar.tools.writing import DocumentWorkRequest
                DocumentWorkRequest.model_validate_json(call['function']['arguments'])
            except ValueError as exc:
                error = ('Professional document requests accept an outcome, material roots, prior draft '
                    'and publication/finish options, not coordinator-selected evidence or attachments. '
                    f'No operation executed: {exc}')
        elif name == 'prepare_implementation' and binding.get('request_owner') == _REQUEST_OWNER:
            try:
                ImplementationWorkRequest.model_validate_json(call['function']['arguments'])
            except ValueError as exc:
                error = f'Implementation work accepts an outcome and roots, not an expert input recipe. No operation executed: {exc}'
        if not error and self.operations is not None:
            try:
                arguments = json.loads(call['function']['arguments'])
                if isinstance(arguments, dict):
                    error = self.operations.service_error(self.gateway.registry.get(name), arguments)
            except (ValueError, TypeError):
                pass  # Ordinary argument admission retains its original diagnostics.
        return (ToolResult(tool_name=call['function']['name'], status='blocked', error=error,
            data={'executed': False}, metadata={'admission': contract, 'dispatched': False})
            if error else None)

    def _offered_tool_error(self, call: dict[str, Any]) -> str:
        refusal = self._offered_tool_refusal(call)
        return refusal.error if refusal else ''

    def _closing_decision(self) -> bool:
        """The saved decision's admission contract, not current-budget inference.

        Old replies have no such obligation. Approval/recovery must retain the
        contract used to obtain the reply, even if resources later change.
        """
        if not self.state['turns']:
            return False
        path = str(Path(self.state['turns'][-1]['reply']).with_name('decision_budget.json'))
        if not self.session.store.exists(path):
            return False
        binding = self.session.store.read_json(path)
        return (binding.get('closing_required') is True
            and binding.get('closing_action_contract') != 'lifecycle_step.v1')

    @staticmethod
    def _history_observation(call: dict, result: dict, record: str, *, navigation: bool = False) -> dict:
        """Compatibility delegate to the public receipt projection."""
        return history_observation(call, result, record, navigation=navigation)

    _history_view = staticmethod(history_view)

    def _messages(self, *, client: LLMClient | None = None, closing: bool = False,
                  native_history_start_turn: int | None = None, schemas: list | None = None,
                  working_memory: dict | None = None, working_selection: dict | None = None,
                  decision_observations: list[dict] | None = None,
                  decision_guidance: tuple | None = None,
                  decision_views: dict | None = None) -> list[dict[str, Any]]:
        protocol = self.state['config'].get('context_protocol')
        if protocol in _CONVERSATION_CONTEXT_PROTOCOLS:
            def conversation():
                return self._conversation_messages(client=client, closing=closing,
                    native_history_start_turn=native_history_start_turn, schemas=schemas, working_memory=working_memory,
                    working_selection=working_selection, decision_observations=decision_observations,
                    decision_guidance=decision_guidance, decision_views=decision_views)
            if self.operations is not None:
                with self.operations.inspection.presentation_scope():
                    return conversation()
            return conversation()
        if protocol is not None:
            raise ValueError('Unknown saved context protocol; no projection migration')
        config = self.state["config"]
        if native_history_start_turn is None:
            rebindings = config.get('model_rebindings', [])
            native_history_start_turn = rebindings[-1].get('native_history_start_turn', 0) if rebindings else 0
        if type(native_history_start_turn) is not int or not 0 <= native_history_start_turn <= len(self.state['turns']):
            raise ValueError('Invalid native history boundary')
        focus = self._work_focus()
        native_history_start_turn = max(native_history_start_turn, focus.get('native_history_start_turn', 0))
        # Feedback retires the old selection's priority, not its receipts.
        # Otherwise an oversized historical selection can evict every newer
        # read indefinitely, so the model never sees what it just requested.
        explicit_inputs = 'inputs' in focus
        blocks = []
        progress = []
        latest_execution = latest_failure = latest_tool_failure = None
        text_windows: dict[tuple[str, str], dict[str, Any]] = {}
        unavailable_reads = []
        source_observations = []
        request_count = 1
        for turn in self.state["turns"]:
            count = turn.get("request_count", 1)
            block = [{"role": "user", "content": request}
                     for request in self.state["requests"][request_count:count]]
            reply = self.session.store.read_json(turn["reply"])
            if turn.get('origin') in {'caller', 'professional'}:
                # Caller actions are journal evidence, not provider-authored
                # assistant messages with an invented reasoning history.
                native_history_start_turn = max(native_history_start_turn, len(blocks) + 1)
            observations = {item["id"]: self.session.store.read_json(item["result"]) for item in turn["tools"]}
            calls = {call['id']: call for call in reply.get('tool_calls', [])}
            outcomes = []
            for item in turn['tools']:
                call, result = calls[item['id']], observations[item['id']]
                try:
                    arguments = json.loads(call['function']['arguments'])
                except (ValueError, TypeError):
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                # This is an observation index, never substituted tool source
                # or a second mutable memory. Original bodies remain readable.
                data = result.get('data') or {}
                name = call['function']['name']
                if result.get('status') == 'ok':
                    if name == 'find_source_passages':
                        source_observations.append({'data': data, 'turn': len(blocks),
                            'record': 'records/' + item['result'].removeprefix('attempts/')})
                    # Derive exact text observations from existing receipts,
                    # not another memory or a model-authored summary. Processes
                    # may change files; validate candidates once at selection,
                    # rather than discard observations or reread at each receipt.
                    if name in {'read_file', 'focus_work', 'write_file', 'edit_file', 'copy_file'}:
                        text_windows = observe_text_window(text_windows, self.workspace, name, arguments, data,
                            record='records/' + item['result'].removeprefix('attempts/'), turn=len(blocks),
                            receipt_sha256=hashlib.sha256(self.session.store.resolve(item['result']).read_bytes()).hexdigest())
                        # Retired versions belong to the existing history archive,
                        # not a mandatory growing tombstone list in every decision.
                        if name == 'read_file' and data.get('text') and not any(
                                window['record'] == 'records/' + item['result'].removeprefix('attempts/')
                                for window in text_windows.values()):
                            unavailable_reads.append({'path': data.get('absolute_path', arguments.get('path')),
                                'sha256': data.get('sha256'), 'offset_bytes': data.get('offset_bytes', arguments.get('offset_bytes', 0)),
                                'next_offset_bytes': data.get('next_offset_bytes'), 'turn': len(blocks),
                                'record': 'records/' + item['result'].removeprefix('attempts/'),
                                'status': 'unavailable', 'reason': 'Receipt lacks verifiable byte coverage/version.'})
                outcomes.append(self._history_observation(call, result, item['result']))
                if result.get('status') != 'ok' or result.get('error'):
                    failure_view = outcomes[-1]
                    if estimate_tokens(json.dumps(failure_view, ensure_ascii=False)) > 2000:
                        failure_view = {key: value for key, value in failure_view.items()
                            if key in {'tool', 'record', 'status'}} | {'details_omitted': True}
                    latest_tool_failure = {'observation': failure_view, 'turn': len(blocks)}
                if ((execution_spec := self.gateway.registry.get(call['function']['name'])) is not None
                        and execution_spec.permission_level == 'execution' and 'returncode' in data):
                    latest_execution = {'observation': outcomes[-1], 'data': data,
                        'result': result, 'turn': len(blocks),
                        'path': str(self.session.store.root / item['result'])}
                    if data['returncode'] != 0:
                        latest_failure = latest_execution
            progress.append({'reply': 'records/' + turn['reply'].removeprefix('attempts/'),
                'request_count': count, 'observations': outcomes})
            # Tool arguments describe actual operations, not summaries. Keep
            # them exact; capacity eviction below removes whole turns.
            # Replacing write payloads with markers caused a real model to
            # copy the marker back into executable source.
            block.append(reply)
            for observation, outcome in zip(turn["tools"], outcomes):
                # Directory tables and explicit copy-inventory previews reduce
                # repeated metadata; raw receipts and source text are unchanged.
                view = workspace_observation_view(observations[observation["id"]],
                    record='records/' + observation['result'].removeprefix('attempts/'),
                    directory_path=outcome.get('arguments', {}).get('path'))
                if outcome['tool'] == 'read_file' and view.get('status') == 'ok' and (view.get('data') or {}).get('text'):
                    view = {**view, 'data': {key: value for key, value in view['data'].items() if key != 'text'} | {
                        'text_supply': 'Current bytes/ranges are in retained_text_windows or work_inputs; '
                            'material_supply explains current gaps and archived observations. Backend ok is not model supply.'}}
                if outcome['tool'] == 'find_source_passages' and view.get('status') == 'ok':
                    data = view.get('data') or {}
                    view = {**view, 'data': {key: ([{k: v for k, v in row.items() if k != 'text'} for row in value]
                        if key in {'matches', 'retained_preview'} and isinstance(value, list) else value) for key, value in data.items()} | {
                            'text_supply': 'Exact current passages are in retained_source_passages; see material_supply.omitted.'}}
                if outcome['tool'] == 'focus_work' and (view.get('data') or {}).get('inputs'):
                    # This is a receipt view, not changed tool arguments or
                    # substituted source text. Current validated window bytes
                    # and gaps are supplied once by work_inputs below; the raw
                    # focus receipt remains available at its original record.
                    data = view.get('data') or {}
                    view = {**view, 'data': {**{key: value for key, value in data.items() if key != 'inputs'},
                        'input_receipt': 'records/' + observation['result'].removeprefix('attempts/'),
                        'input_window_count': len(data.get('inputs', [])),
                        'input_scope': 'Current work_inputs supplies revalidated windows or explicit gaps. '
                            'The original read observations are retained at input_receipt.'}}
                block.append({"role": "tool", "tool_call_id": observation["id"],
                    "content": json.dumps(view, ensure_ascii=False, separators=(',', ':'))})
            blocks.append(block)
            request_count = count
        executions = ([latest_failure] if latest_failure is not None
            and latest_failure is not latest_execution else []) + (
            [latest_execution] if latest_execution is not None else [])
        for execution in executions:
            execution['blocks'] = {execution['turn']}
        # Count actual serialized history and schemas, not an assumed overhead.
        # Request allowance is a ceiling, not a planned number of future turns.
        # Dividing tokens by that ceiling makes a more permissive allowance
        # erase the same useful observations and induce repeated investigation.
        # This is an input working-set target, not an output cap or permission
        # to drop progress: archived turns remain available through read_history.
        client = client or self._client()
        output_reservation = client.effective_output_limit() or 0
        remaining_requests = self._request_capacity()
        remaining_tokens = self.ledger.remaining('total_tokens')
        input_target = input_capacity(client._settings.context_input_tokens, remaining_tokens, output_reservation)
        # Preserve a contiguous causal conversation, not only the last tool
        # result. Earlier decisions explain why a probe ran and what its result
        # changed. Drop complete oldest blocks only when capacity requires it;
        # never splice a call/result or manufacture a new user turn.
        # Material admission owns the working set. Native conversation is a
        # later, evictable consumer, never an implicit reservation for a large
        # last read, generated script or reasoning block.
        selected = []
        vision_spec = self.gateway.registry.get('inspect_images')
        vision_available, vision_reason = (self.gateway.policy.is_allowed(vision_spec)
            if vision_spec is not None else (False, 'inspect_images is not registered for this task.'))
        messages = [{"role": "system", "content": self._system_text(closing=closing)},
            {"role": "user", "content": json.dumps({
            "requests": self.state["requests"], "mounts": config["sources"],
            "workspace": str(self.workspace.workspace),
            "authorized_roots": {name: str(path) for name, path in self.workspace.mounts.items()},
            "permissions": sorted(self.gateway.policy.allowed_permissions),
            "vision_inspection": {"available": vision_available, "reason": vision_reason,
                "scope": "Tool/policy availability, not sufficient budget or route health. Load its contract in on-demand mode."},
            "process_cwd": str(self.workspace.workspace),
            "task_python": str(self.workspace.python),
            "work_focus": {key: value for key, value in focus.items()
                if key not in {'inputs', 'previous_inputs', 'turn'}},
            "work_plan": self._work_plan(),
            "required_delivery_files": config.get('delivery_files', []),
            "work_focus_authority": "A bounded work intention, not a new user request or permission. Preserve the original task and feedback; return to task focus for broader decisions if needed.",
            "saved_evidence": "evidence: read-only acquired originals and search records",
            "earlier_turns": [],
            "record_access": "Read earlier raw replies/tool results via records/<attempt-id>/native_result.json",
            "resources": {"limits": self.ledger.limits,
                "remaining": {name: self.ledger.remaining(name) for name in self.ledger.limits},
                "unknown_usage": list(self.ledger.unknown_dimensions()),
                "model_turns_remaining": remaining_requests,
                "decision_budget": {
                    "includes_current_decision": True,
                    "provider_slots_after_one_attempt": max(0, remaining_requests - 1) if remaining_requests is not None else None,
                    "nested_provider_slots_after_decision_and_followup": max(0, remaining_requests - 2) if remaining_requests is not None else None,
                    "followup_policy": config.get('completion_policy', 'none'),
                    "closing_required": closing,
                    "scope": "Shared physical attempts, not completed actions. Professional selection, writing, review and retries also spend them. Protection is a follow-up opportunity, not guaranteed task completion."},
                "semantics": {
                    "llm_requests": "Shared physical provider attempts: decisions, professional selection/writing/review, inspect_images and retries.",
                    "total_tokens": "Model input/output, including image inputs to inspect_images.",
                    "image_requests": "Image generation/edit requests only; zero does not prohibit authorized inspection."},
                "accounting": "Raw ledger limits are not permissions. Null means unlimited or unknown; consult unknown_usage. "
                    "Remaining tokens cover input and output; unknown usage retains its reservation. Resume does not reset limits."},
        }, ensure_ascii=False)}]
        prefix = messages
        # Feedback is a new user turn after prior actions, not only a change
        # to the initial task description before an old delivery response.
        feedback = [{"role": "user", "content": request}
                    for request in self.state["requests"][request_count:]]
        context = json.loads(prefix[1]["content"])
        execution_contract = self.scientific.execution_contract if self.scientific is not None else None
        if execution_contract is not None:
            context['execution_contract'] = execution_contract
            context['execution_source'] = ('Use each frozen condition source: preparation requires prepare_code_task and its registered preparation_ref/cwd; '
                'implementation requires a validated implementation_ref/cwd; declared retains its original cwd. '
                'To preserve an unchanged baseline, implement using that prepared_result, then retain it across feedback. '
                'Complete any before_implementation conditions on that exact preparation before code work; their verified observations are supplied to implementation. '
                'Select the exact condition_id and analyze all required actual conditions of the current revision; do not infer source from comparison role. '
                'Flat contracts use the validated implementation. Preparation, validator and formal measurement are distinct.'
                if config.get('code_task') is not None else 'Caller-frozen command or complete condition set. Select exact condition_id when present; do not alter or drop declared roles, seeds or protocol. Analyze the complete actual set before claiming completion. Subsequent execution still requires approval.')
        report = self._report_projection()
        if config.get('report_protocol') == _REPORT_PROTOCOL:
            context['report_protocol'] = {'version': _REPORT_PROTOCOL,
                'effective': {key: value for key, value in report.items() if key != 'requirement'},
                'instruction': 'Interpret the actual user request as report, conversion of existing text, or none using update_work_plan.report_intent with a zero-based request_index and exact quote. Declare in the same normal action batch before substantive work; steps may be empty. Existing report obligations persist across feedback and omitted plans. Unknown intent does not forbid investigation. Declare before terminal delivery. Conversion preserves the supplied original; it does not require scientific rewriting. Direct writes/processes remain candidates or technical work. Formal report identity does not verify scientific claims.'}
        if report['kind'] == 'report':
            requirement = report['requirement']
            # Writer owns the full frozen template/configuration. The action
            # loop needs delivery constraints, not another copy of its prompts.
            context['report_requirement'] = {'method': requirement['method'], 'outline': requirement['outline'],
                'max_cited_sources': requirement['config']['max_cited_sources'],
                'target_words': requirement['config']['longform']['target_words']}
            context['report_delivery'] = (('compose_document uses the frozen material/report requirements and actual supplied/source/table evidence. '
                'Select relevant original passages and analysis artifacts, not just summaries. ' if
                report['evidence_contract'] == 'materials.v1' else
                'After required execution and analysis, compose_document consumes actual code/measurement ')
                + 'evidence automatically and executes the frozen writing method, including adaptive planning/review/revision. '
                'Include its canonical manuscript.md and requested execution artifacts in delivery. '
                + BOUND_REPORT_REVISION + ' Required findings or missing reports must be disclosed as unresolved.')
            if self.writing.document_work:
                context['report_delivery'] = (
                    'Delegate the requested document outcome with material_roots and optional prior_draft. '
                    'The document owner investigates, selects actual evidence, figures and portable attachments, '
                    'and writes/revises under these frozen requirements. Do not prepare its title, evidence '
                    'windows or attachment list. Explicit publication plus finish=true requests formal closure '
                    'only after same-version independent review and successful approved export. '
                    + BOUND_REPORT_REVISION + ' Required findings remain unresolved until actually addressed.')
        if config.get('research_policy'):
            context['research_policy'] = config['research_policy']
        if config.get('table_analysis'):
            context['table_analysis'] = config['table_analysis']
        if config.get('tool_contracts', 'full') != 'full':
            context['available_specialists'] = self._specialist_catalog()
            context['tool_contracts'] = (
                'Only the supplied function contracts are callable in this decision. '
                'available_specialists is a discovery directory, not active function contracts. '
                'Use load_tools with exact name keys for optional contracts on the next decision; '
                'this replaces the prior selection without changing permissions. Delegate coherent '
                'professional work rather than preparing its answers. Loading is not execution or validation.')
        context["resources"]["context_input_target_tokens"] = input_target
        context["resources"]["next_output_token_reservation"] = output_reservation
        schema_cost = estimate_tokens(json.dumps(self.schemas if schemas is None else schemas, ensure_ascii=False))
        supplied_execution_records: set[str] = set()
        execution_details: set[str] = set()
        def project_archive(cutoff: int) -> None:
            context['retained_turn_start'] = cutoff
            context['history_archive'] = {'start_turn': 0, 'end_turn': cutoff,
                'total_turns': len(blocks), 'tool_failures': 0, 'nonzero_executions': 0}
            for row in progress[:cutoff]:
                for observation in row['observations']:
                    context['history_archive']['tool_failures'] += int(
                        observation.get('status') != 'ok' or bool(observation.get('error')))
                    context['history_archive']['nonzero_executions'] += int(
                        observation['observed'].get('returncode', 0) != 0)
            context['execution_observations'] = [{**execution['observation'], **(
                {'observed': execution['data']} if execution['path'] in execution_details else
                {'details_not_supplied': {key: {'offset_bytes': 0, 'next_offset_bytes': len(value.encode('utf-8'))}
                    for key, value in execution['data'].items() if isinstance(value, str)
                    and key not in execution['observation'].get('observed', {})}})}
                for execution in executions
                if execution['path'] not in supplied_execution_records
                and not any(index >= cutoff for index in execution['blocks'])]
            context['latest_tool_failure'] = (latest_tool_failure.copy()
                if latest_tool_failure is not None and latest_tool_failure['turn'] < cutoff else None)

        project_archive(len(blocks) - len(selected))
        context['history_scope'] = (
            'earlier_turns is a bounded recent observation view, not the complete history. '
            'history_archive is the half-open accepted-turn range omitted from this view, '
            'not attempt numbers. Use read_history to page its original indexes and inline '
            'cancellation results; original attempt replies/results remain at their records. '
            'Omission does not mean absence, success or resolution. Identities/digests describe '
            'historical observations, not current files. Archive failure counts are historical '
            'invocation facts, not a list of unresolved blockers. Table rows map to columns and '
            'observations map through observation_columns. Current requests, permissions, '
            'unknown usage, focus and plan retain their existing authority. '
            'execution_observations retains the latest execution and latest nonzero execution '
            'when neither is in the retained native conversation or its complete read-back. '
            'A later successful probe does not resolve a failure; these are execution facts '
            'rather than claims about the current project.')
        context['retained_text_windows'] = []
        context['retained_source_passages'] = []
        context['text_window_scope'] = (
            'Exact historical file text, not summaries or verified claims. Windows are '
            'revalidated against current authorized paths and matching full digests, including '
            'after execution. Changes and symlink redirection invalidate them; validation is '
            'a snapshot, not a guarantee against later changes. Original receipts remain '
            'readable. Windows share actual serialized capacity with native turns/contracts; '
            'omission is not evidence of absence. Retained source passages are exact observed '
            'chunk windows bound to the unchanged original source-record bytes, not a summary, '
            'complete reading or verified support. Both collections share the same input capacity.')
        def input_cost() -> int:
            # One serialized capacity includes the material, native protocol,
            # indexes, contracts and the cost report itself. Separate fixed
            # quotas evicted useful originals despite spare shared capacity.
            resources = context['resources']
            resources['approximate_next_input_tokens'] = 0
            resources['context_exceeds_input_target'] = False
            while True:
                prefix[1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
                cost = estimate_tokens(json.dumps(prefix + [message for block in selected
                    for message in block] + feedback, ensure_ascii=False)) + schema_cost
                exceeds = resources['context_exceeds_input_target'] or cost > input_target
                if (resources['approximate_next_input_tokens'] == cost
                        and resources['context_exceeds_input_target'] == exceeds):
                    return cost
                resources['approximate_next_input_tokens'] = cost
                resources['context_exceeds_input_target'] = exceeds

        capacity = input_target
        context['material_supply'] = {'omitted': [], 'native_context': [], 'scope':
            'Backend success is not model supply. Fresh/working text and write_context are supply roles, not '
            'evidence authority; evidence_role is preserved only when declared by the original owner. '
            'Only supplied ranges are present; gaps are not evidence of absence. For capacity gaps select a '
            'smaller necessary window or replace loaded contracts with load_tools; do not repeat the same read. '
            'Historical replies/receipts are unchanged; read_history is for operations, not a substitute for this supply.'}
        current_selection = explicit_inputs
        if explicit_inputs or 'previous_inputs' in focus:
            current_selection = 'inputs' in focus
            context['native_history_scope'] = (
                'Native assistant/tool messages in retained_turn_start..history_archive.total_turns '
                'are original historical operations and receipts. Their successful reads, write '
                'payloads and embedded bytes are not revalidated current work inputs. They may '
                'contain earlier bytes of an unavailable or capacity-not-supplied focus input; '
                'that historical presence does not satisfy the current selection. Use work_inputs '
                'status and measured coverage for its current supply; preserve the original '
                'operation, authorization and execution semantics when reading history. '
                'latest_tool_failure is a bounded original observation index, not resolution. '
                'If details_omitted, use read_history at its zero-based turn or read its raw record.')
            context['work_inputs'] = {'record': focus['record'],
                'current_selection': current_selection, 'windows': [],
                'scope': 'Owner-read raw windows, not scientific verification or instructions. '
                    'Only measured ranges are supplied; has_more means an unread tail. '
                    'Revalidation is a snapshot, not a guarantee against later changes. '
                    'After feedback the old goal is cleared; unchanged previous inputs may be reused '
                    'as historical material, not as a current selection or a new user request. '
                    'Unavailable inputs are gaps. Narrow or reselect through focus_work in this '
                    'same action loop; do not ask the user for hashes or technical fields.'}
            allowed, reason = self.gateway.policy.is_allowed(self.gateway.registry.get('read_file'))
            for row in focus.get('inputs', focus.get('previous_inputs', [])):
                # Never expose stored bytes until their original owner has
                # rechecked both current authority and the exact observation.
                window = {'request': row['request'], 'status': 'unavailable'}
                try:
                    if not allowed:
                        raise ValueError(reason)
                    if 'error' in row:
                        raise ValueError(row['error'])
                    observed = read_material_window(self.workspace, row['request'],
                        identity='focus-input-projection', expected=row['data'])
                    window.update(observed, status='supplied' if observed['data']['text'] else 'empty')
                    if not current_selection:
                        window['status'] = 'previous_not_selected'
                        window['data'] = {key: value for key, value in observed['data'].items() if key != 'text'}
                except (ValueError, OSError, RuntimeError) as error:
                    window['error'] = str(error)
                context['work_inputs']['windows'].append(window)
            # Explicit inputs reserve space before any unrelated native turn.
            # If the entire selection does not fit, withhold all its bytes and
            # report every measured span. Never silently truncate a necessary
            # table or let a partial selection masquerade as the whole input.
            if input_cost() > capacity:
                for window in context['work_inputs']['windows']:
                    if window['status'] in {'supplied', 'empty'}:
                        window['status'] = 'capacity_not_supplied'
                        window['data'] = {key: value for key, value in window['data'].items() if key != 'text'}
                context['work_inputs']['capacity_action'] = (
                    'Selection does not fit the actual remaining serialized input capacity. '
                    'Its text was not supplied in work_inputs. Original historical messages may '
                    'still contain earlier bytes; those do not satisfy this selection. '
                    'Select smaller necessary windows with focus_work; '
                    'do not infer missing values or claim complete reading.')
        # One admission order: selected inputs, recent working observations
        # (reads AND generated editing context), then evictable conversation.
        # Supply priority never invents or upgrades an evidence role.
        # Every omission has actual observed coverage, never a silent fallback
        # to rereading the same receipt or a coordinator-authored answer.
        omitted = context['material_supply']['omitted']
        pinned = []
        focused_paths = set()
        for row in context.get('work_inputs', {}).get('windows', []):
            if current_selection:
                try:
                    focused_paths.add(str(self.workspace.resolve(row['request']['path'])))
                except (ValueError, OSError, RuntimeError):
                    pass  # The selected window already carries its authority failure.
            if row['status'] in {'supplied', 'empty'}:
                path, start, end, digest = observed_window_key(self.workspace,
                    FileWindow.model_validate(row['request']), row['data'])
                pinned.append({'path': path, 'sha256': digest, 'offset_bytes': start,
                    'next_offset_bytes': end, 'text': row['data']['text']})
        def coverage(row: dict) -> dict:
            return {key: row[key] for key in ('path', 'sha256', 'record', 'offset_bytes', 'next_offset_bytes',
                'evidence_role', 'role', 'status', 'reason', 'version_scope', 'overlap_scope') if key in row}

        context['material_supply']['archived_observations'] = 0
        def record_gap(row: dict, *, fresh: bool) -> None:
            if fresh:
                omitted.append(row)
            else:
                context['material_supply']['archived_observations'] += 1
        context['material_supply']['archive_scope'] = (
            'Older unsupplied or retired observations remain only in history_archive/read_history and original records; '
            'they are not supplied text. Current requested gaps are explicit below. No archive index proves reading.')
        # Loading contracts or inspecting metadata does not retire the latest
        # working input. Its missing ranges stay current across those decisions;
        # earlier work can be archived without accumulating mandatory metadata.
        working_turn = max((row['turn'] for row in
            [*text_windows.values(), *source_observations, *unavailable_reads]), default=-1)
        for row in unavailable_reads:
            record_gap(coverage(row), fresh=row['turn'] == working_turn)
        candidates = []
        read_allowed, read_reason = self.gateway.policy.is_allowed(self.gateway.registry.get('read_file'))
        for window in sorted(text_windows.values(), key=lambda value: value['turn'], reverse=True):
            if explicit_inputs and window['record'] == focus['record']:
                continue
            if window['path'] in focused_paths:
                continue  # A selected range must not silently expand back to an old whole file.
            role = ('write_context' if window['operation'] == 'write_file' else
                'fresh_read' if window['turn'] == len(blocks) - 1 else 'working_read')
            try:
                if not read_allowed:
                    raise ValueError(read_reason)
                observation = current_text_window(self.workspace, window)
            except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
                record_gap(coverage(window) | {'role': role, 'status': 'unavailable', 'reason': str(error)},
                    fresh=window['turn'] == working_turn)
                continue
            candidates.append({key: value for key, value in observation.items() if key != 'turn'} |
                {'role': role, '_observed_turn': window['turn']})
        source_windows = self.sources.retained_passages(source_observations, before_turn=len(blocks))
        for window in source_windows:
            candidates.append({key: value for key, value in window.items() if key != 'turn'}
                | {'role': 'fresh_read' if window['turn'] == len(blocks) - 1 else 'working_read',
                   '_observed_turn': window['turn']})
        for row in source_observations:
            data = row['data']
            for passage in data.get('matches', data.get('retained_preview', [])):
                if not any(window['path'] == data.get('document_path') and window['sha256'] == data.get('document_sha256')
                        and window['passage']['chunk_id'] == passage.get('chunk_id')
                        and window['passage']['character_start'] <= passage.get('character_start', -1)
                        and passage.get('character_end', -1) <= window['passage']['character_end'] for window in source_windows):
                    record_gap({'path': data.get('document_path'), 'record': row['record'],
                        'sha256': data.get('document_sha256'), 'character_start': passage.get('character_start'),
                        'character_end': passage.get('character_end'), 'chunk_id': passage.get('chunk_id'),
                        'status': 'unavailable', 'reason': 'Source owner could not admit this original passage/version.'},
                        fresh=row['turn'] == working_turn)
        seen_ranges, pending_material = set(), []
        # Reading requests a working input; writing authors an output, not a
        # request to pin its whole body over earlier inputs. Explicit rereads
        # of edited/generated files are inputs too, without suffix/domain rules.
        for candidate in sorted(candidates, key=lambda value: (
                value['role'] != 'write_context', value['_observed_turn']), reverse=True):
            observation = {key: value for key, value in candidate.items() if key != '_observed_turn'}
            passage = observation.get('passage')
            key = (observation['path'], observation['sha256'], json.dumps(observation.get('evidence_role'), sort_keys=True),
                observation['record'] if observation['sha256'] is None else None,
                (passage['chunk_id'], passage['character_start'], passage['character_end']) if passage else
                (observation['offset_bytes'], observation['next_offset_bytes']))
            if key in seen_ranges:
                continue  # Same observed coverage, not another copy/diagnostic.
            seen_ranges.add(key)
            gap = coverage(observation) | {'status': 'capacity_not_supplied',
                'reason': 'Requested observed range does not fit after higher-priority material and contracts.'}
            if passage:
                gap.update({key: passage[key] for key in ('chunk_id', 'character_start', 'character_end')})
            fresh = candidate['_observed_turn'] == working_turn
            pending_material.append((observation, gap, fresh))
            if fresh:
                omitted.append(gap)
        # Only current gaps are mandatory. Historical ranges are evictable
        # with their receipts; long tasks cannot grow a second context ledger.
        if input_cost() > capacity and explicit_inputs:
            for window in context['work_inputs']['windows']:
                if window['status'] in {'supplied', 'empty'}:
                    window['status'] = 'capacity_not_supplied'
                    window['data'] = {key: value for key, value in window['data'].items() if key != 'text'}
            pinned.clear()
            context['work_inputs']['capacity_action'] = 'Selection plus necessary range diagnostics does not fit; select smaller windows with focus_work.'
        # Repair diagnostics precede historical material. A successful backend
        # read cannot evict the failure needed to choose the next operation.
        for execution in executions:
            execution_details.add(execution['path'])
            project_archive(len(blocks))
            if input_cost() > capacity:
                execution_details.remove(execution['path'])
                project_archive(len(blocks))
        for observation, gap, fresh in pending_material:
            if fresh:
                omitted.remove(gap)
            field = 'retained_source_passages' if 'passage' in observation else 'retained_text_windows'
            retained = context[field]
            previous = retained[:]
            if field == 'retained_text_windows':
                proposed = admit_text_window(retained, observation, pinned=pinned)
                if proposed is None:
                    continue
                # Only a successfully admitted containing original replaces
                # smaller windows. Keep them as fallback if it cannot fit;
                # partial overlap alone never discards observed coverage.
                retained[:] = proposed
            else:
                retained.append(observation)
            if input_cost() > capacity:
                retained[:] = previous
                record_gap(gap, fresh=fresh)
        # Native read/passage views reference the admitted bytes, rather than
        # copying them. Native write arguments remain exact; if a write block
        # fits, its authored body replaces the duplicate write_context copy.
        authored = [row for row in candidates if row['role'] == 'write_context']
        for execution in executions:
            for row in context['retained_text_windows']:
                if row['path'] != execution['path']:
                    continue
                try:
                    if json.loads(row['text']) == execution['result']:
                        supplied_execution_records.add(execution['path'])
                except (ValueError, TypeError):
                    pass
            project_archive(len(blocks))
        for index in range(len(blocks) - 1, native_history_start_turn - 1, -1):
            previous = context['retained_text_windows'][:]
            previous_native = context['material_supply']['native_context'][:]
            writes = [row for row in authored if row['_observed_turn'] >= index]
            context['retained_text_windows'][:] = [row for row in previous
                if not any(write['path'] == row['path'] and write['sha256'] == row['sha256']
                    and write['offset_bytes'] <= row['offset_bytes']
                    and row['next_offset_bytes'] <= write['next_offset_bytes'] for write in writes)]
            context['material_supply']['native_context'] = [coverage(row) | {'status': 'supplied_native',
                'role': 'write_context'} for row in writes]
            selected.insert(0, blocks[index])
            project_archive(index)
            if input_cost() > capacity:
                selected.pop(0)
                context['retained_text_windows'][:] = previous
                context['material_supply']['native_context'] = previous_native
                break
        cutoff = len(blocks) - len(selected)
        project_archive(cutoff)
        # Report only genuinely missing byte intervals, including coverage
        # supplied by whole original observations, selections or native writes.
        supplied = [*pinned, *context['retained_text_windows'], *context['material_supply']['native_context']]
        gaps = []
        for gap in omitted:
            if (gap.get('status') == 'capacity_not_supplied' and isinstance(gap.get('offset_bytes'), int)
                    and isinstance(gap.get('next_offset_bytes'), int)):
                gaps.extend({**gap, 'offset_bytes': start, 'next_offset_bytes': end}
                    for start, end in uncovered_text_ranges(gap, supplied))
            else:
                gaps.append(gap)
        omitted[:] = gaps
        recent = []
        for row in reversed(progress[:cutoff]):
            proposed = self._history_view([row] + recent)
            if estimate_tokens(json.dumps(proposed, ensure_ascii=False)) > 2000:
                break
            context['earlier_turns'] = proposed
            context['history_archive']['end_turn'] = cutoff - len(recent) - 1
            if input_cost() > capacity:
                context['earlier_turns'] = self._history_view(recent)
                context['history_archive']['end_turn'] = cutoff - len(recent)
                break
            recent.insert(0, row)
        for row in recent:
            for observation in row['observations']:
                context['history_archive']['tool_failures'] -= int(
                    observation.get('status') != 'ok' or bool(observation.get('error')))
                context['history_archive']['nonzero_executions'] -= int(
                    observation['observed'].get('returncode', 0) != 0)
        if input_cost() > capacity:
            raise ValueError('Current requests, contracts and execution facts exceed the remaining input capacity; no model request sent')
        if getattr(client._settings, 'thinking_mode', '') == 'enabled' and any(
                message['role'] == 'assistant' and not any(isinstance(message.get(key), str)
                    for key in ('reasoning_content', 'reasoning'))
                for block in selected for message in block):
            raise ValueError('Native history lacks reasoning_content or reasoning; explicitly switch models to start '
                             'a new native session without fabricating or changing saved reasoning')
        return prefix + [message for block in selected for message in block] + feedback

    @staticmethod
    def _offered_operation_names(schemas: list) -> set[str]:
        names = {row['function']['name'] for row in schemas}
        if 'select_work' in names:
            wrapper = next(row['function']['parameters'] for row in schemas
                if row['function']['name'] == 'select_work')
            names.update(wrapper['$defs']['WorkOperation']['properties']['tool_name']['enum'])
            decisions = wrapper['properties']['decision']['enum']
            if 'review' in decisions:
                names.add('review_result')
            if 'deliver' in decisions or 'blocked' in decisions:
                names.add('deliver_result')
        return names

    def _selection_context(self, *, closing: bool = False, schemas: list | None = None,
                           observations: list[dict] | None = None, lifecycle: dict | None = None) -> dict:
        """Project the last declared candidate, never a separate adoption state."""
        candidate = self.operations.selection.retained_candidate(
            self.session.store, self.state['turns'], len(self.state['requests']))
        if candidate is None and self.session.store.exists('delivery.json'):
            candidate = self.session.store.read_json('delivery.json')
        artifacts = []
        if candidate is not None:
            try:
                artifacts = self.workspace.resolve_delivery(DeliveryRequest.model_validate(candidate))['artifacts']
            except (ValueError, OSError):
                artifacts = []  # A refused candidate must remain correctable, not break input construction.
        review = self.operations.delivery_review
        concerns = review.pending_concerns(artifacts, observations) if review is not None else []
        operations = self._closing_operation_schemas(lifecycle=lifecycle) if closing else self.operation_schemas
        if self.operations.selection.policy == 'outcomes.v1' and schemas is not None:
            # The actual measured outer offer owns the inner action/transition
            # admission too, including a token-constrained partial-only exit.
            names = self._offered_operation_names(schemas)
            operations = [row for row in operations if row['function']['name'] in names]
        production = ([] if self.operations.selection.policy else
            [row['function']['name'] for row in operations
             if self.gateway.registry.get(row['function']['name']).permission_level in {'write_patch', 'execution'}])
        return self.operations.selection.context(operations, candidate, concerns, production)

    def _conversation_messages(self, *, client: LLMClient | None = None, closing: bool = False,
                               native_history_start_turn: int | None = None,
                               schemas: list | None = None, working_memory: dict | None = None,
                               working_selection: dict | None = None,
                               decision_observations: list[dict] | None = None,
                               decision_guidance: tuple | None = None,
                               decision_views: dict | None = None) -> list[dict[str, Any]]:
        """One chronological working set; receipts retain their actual text once.

        This is historical conversation, not a revalidated evidence cache. Tool
        owners still validate current permissions, source bytes and acceptance.
        No file discovery, source reads, model calls or new persisted state.
        """
        config, turns = self.state['config'], self.state['turns']
        # Raw inputs and owner projections may be shared across offers prepared
        # for this decision. The next decision reads the journal again.
        if decision_views is None:
            decision_views = {}
        working_mode = config.get('context_protocol') in _WORKING_CONTEXT_PROTOCOLS
        receipt_supply = config.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES
        operation_history = [] if config.get('context_protocol') == 'working.v4' else None
        declared = config.get('input_contract') == _DECLARED_INPUTS
        client = client or self._client(purpose=self._coordinator_purpose(decision_views=decision_views))
        focus = self._work_focus(decision_views=decision_views)
        if native_history_start_turn is None:
            rebindings = config.get('model_rebindings', [])
            native_history_start_turn = rebindings[-1].get('native_history_start_turn', 0) if rebindings else 0
        if type(native_history_start_turn) is not int or not 0 <= native_history_start_turn <= len(turns):
            raise ValueError('Invalid native history boundary')
        boundary = max(native_history_start_turn, focus.get('native_history_start_turn', 0),
                       self._professional_boundary())
        output = client.effective_output_limit() or 0
        remaining = self.ledger.remaining('total_tokens')
        target = input_capacity(client._settings.context_input_tokens, remaining, output)
        requests = self._request_capacity()
        report = self._report_projection(decision_views=decision_views)
        plan = None if config.get('work_method') == GOAL_WORK_METHOD else self._work_plan(decision_views=decision_views)
        context = {
            'context_protocol': config['context_protocol'], 'requests': self.state['requests'],
            'mounts': config['sources'], 'workspace': str(self.workspace.workspace),
            'authorized_roots': {name: str(path) for name, path in self.workspace.mounts.items()},
            'permissions': sorted(self.gateway.policy.allowed_permissions),
            'process_cwd': str(self.workspace.workspace), 'task_python': str(self.workspace.python),
            'required_delivery_files': config.get('delivery_files', []),
            'work_focus': {key: value for key, value in focus.items()
                if key not in {'inputs', 'previous_inputs', 'turn'}},
            'work_focus_authority': 'Saved work intention, not a user request, evidence or permission.',
            'work_plan': {key: plan[key] for key in ('steps', 'scope', 'request_count', 'record') if key in plan} if plan else None,
            'frozen_contracts': {key: config[key] for key in ('code_task', 'execution_contract',
                'research_policy', 'table_analysis') if config.get(key) is not None},
            'resources': {'limits': self.ledger.limits,
                'remaining': {key: self.ledger.remaining(key) for key in self.ledger.limits},
                'unknown_usage': list(self.ledger.unknown_dimensions()),
                'model_turns_remaining': requests, 'context_input_target_tokens': target,
                'next_output_token_reservation': output,
                'decision_budget': {'includes_current_decision': True,
                    'provider_slots_after_one_attempt': max(0, requests - 1) if requests is not None else None,
                    'nested_provider_slots_after_decision_and_followup': max(0, requests - 2) if requests is not None else None,
                    'followup_policy': config.get('completion_policy', 'none'), 'closing_required': closing},
                'semantics': {
                    'llm_requests': 'Shared physical provider attempts: decisions, professional selection/writing/review, inspect_images and retries.',
                    'total_tokens': 'Model input/output, including image inputs to inspect_images.',
                    'image_requests': 'Image generation/edit requests only; zero does not prohibit authorized inspection.'}},
            'report_protocol': {'version': config.get('report_protocol'),
                'effective': {key: value for key, value in report.items() if key != 'requirement'}},
            'retained_text_windows': [], 'retained_source_passages': [],
            'work_inputs': {'windows': [], 'scope': 'Focus text is supplied only in retained native tool messages; no second text projection.'},
            'material_supply': {'native_context': [], 'omitted': [],
                'scope': 'Original historical observations in native messages, not current file state or verified facts. '
                    'No read text or write payload is replaced. Archived text is not supplied; use read_history for original receipts.'},
        }
        if not closing:
            pacing_notice = budget_pacing_notice(self.ledger)
            if pacing_notice is not None:
                context['resources']['pacing_notice'] = pacing_notice
        files = {alias: path.name for alias, path in self.workspace.mounts.items() if path.is_file()}
        if files:
            context['file_mounts'] = {'paths': files,
                'scope': 'These aliases are usable file paths as written. The displayed names are not suffixes.'}
        delivery_guidance = None
        observations = None
        if self.operations is not None:
            # All consumers in this projection see the same original journal.
            # A later decision rebuilds it after any action or checkpoint.
            observations = (self._operation_observations(decision_views=decision_views) if decision_observations is None
                            else decision_observations)
            if decision_guidance is None:
                accepted_delivery = (self.session.store.read_json('delivery.json')
                    if self.session.store.exists('delivery.json') else None)
                delivery_guidance = self.operations.delivery.working_guidance(accepted_delivery,
                    include_lifecycle=config.get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES,
                    observations=observations)
            else:
                delivery_guidance = decision_guidance
        if config.get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES:
            context['delivery_conditions'] = dict(delivery_guidance[0])
            context['delivery_conditions'].pop('review_followup', None)
            key = ('retained_prior_delivery',)
            if key not in decision_views:
                decision_views[key] = self.operations.delivery.retained_prior_delivery()
            prior = decision_views[key]
            if prior is not None:
                context['retained_prior_delivery'] = prior
            if schemas is not None:
                offered = self._offered_operation_names(schemas)
                context['delivery_conditions']['next_tools'] = [name for name in
                    context['delivery_conditions']['next_tools'] if name in offered]
                if closing and config.get('work_selection_policy') == 'native.v5':
                    for row in schemas:
                        name = row['function']['name']
                        contract = self._completion(name)
                        if (contract is not None and contract.closing and contract.field
                                and contract.field in row['function']['parameters'].get('required', [])
                                and name not in context['delivery_conditions']['next_tools']):
                            context['delivery_conditions']['next_tools'].append(name)
        if declared:
            # One operable directory; the resolver still owns exact OS paths
            # and independent file/parent grants. Never broaden a mount here.
            context['mounts'] = {name: str(path) for name, path in self.workspace.mounts.items()}
            context.pop('authorized_roots')
            context.pop('workspace')
            context['creation_declarations'] = config['creation_declarations']
            context['input_contract'] = _DECLARED_INPUTS
            context['request_count'] = len(self.state['requests'])
            context['current_request_index'] = len(self.state['requests']) - 1
            if config.get('code_task') is not None:
                context['existing_code_task'] = {
                    'required_tool': 'implement_code_task', 'scope': 'frozen_contracts.code_task',
                    'completion': 'An accepted implementation and validation through this owner is required. '
                        'Ordinary workspace writes or process calls cannot replace the frozen edit/check contract.'}
        if report['requirement'] is not None and self.operations is None:
            requirement = report['requirement']
            controls = requirement['config']
            context['report_requirement'] = {'method': requirement['method'], 'outline': requirement['outline'],
                'max_cited_sources': controls.get('max_cited_sources', 0),
                'target_words': controls.get('longform', {}).get('target_words', 0),
                'review': {key: controls[key] for key in ('reviewer', 'document_review', 'review_scope',
                    'max_review_iterations') if key in controls}}
            context['report_delivery'] = BOUND_REPORT_REVISION
        elif report['requirement'] is not None:
            from simple_ar.report.requirements import working_report_requirement
            context['requested_report'] = working_report_requirement(report['requirement'])
        if self.operations is not None:
            for name in ('report_protocol','work_focus','work_focus_authority','work_inputs'):
                context.pop(name,None)
            method = self.operations.method_context()
            if method is not None:
                context['professional_work' if self.operations.work is not None else 'professional_method'] = method
            delivery_feedback = delivery_guidance[1]
            if delivery_feedback:
                context['last_delivery_check'] = delivery_feedback
            if self.operations.selection is not None:
                context.pop('work_plan', None)
                context['work_selection'] = self._selection_context(
                    closing=closing, schemas=schemas, observations=observations,
                    lifecycle=delivery_guidance[0])
        if config.get('report_protocol') == _REPORT_PROTOCOL and not (declared and closing):
            context['report_protocol']['instruction'] = (
                'Declare report, original-text conversion or none using update_work_plan.report_intent, '
                'with the actual current request_index and exact quote. Existing obligations persist across feedback. '
                'Formal reports require the canonical writing/publication identity; raw writes remain candidates.')
        if config.get('tool_contracts', 'full') != 'full' and not (declared and closing):
            context['available_specialists'] = self._specialist_catalog()
            context['tool_contracts'] = {'mode': config['tool_contracts'],
                'instruction': 'load_tools selects exact catalog names; selection grants no permission or completion.'}
        vision = self.gateway.registry.get('inspect_images')
        allowed, reason = self.gateway.policy.is_allowed(vision) if vision is not None else (False, 'Not registered')
        context['vision_inspection'] = {'available': allowed, 'reason': reason}
        # Index columns are fixed and bodies stay at the exact reply/result refs.
        columns = ['turn', 'request_count', 'reply', 'operations']
        operation_columns = ['id', 'tool', 'arguments', 'record', 'status', 'error', 'artifacts', 'outcome']
        blocks, index, supplies, request_blocks = [], [], [], []
        working_rows = []
        native_observations = []
        owner_observations = []
        professional_outcomes = []
        count = 1
        for position, turn in enumerate(turns):
            current = turn.get('request_count', 1)
            request_indices = set(range(count, current))
            request_messages = [{'role': 'user', 'content': (json.dumps({'user_request': {'index': number, 'text': self.state['requests'][number]}},
                ensure_ascii=False) if declared else self.state['requests'][number])} for number in range(count, current)]
            block = list(request_messages)
            count = current
            reply = self._decision_record(turn['reply'], decision_views)
            calls = {call['id']: call for call in reply.get('tool_calls', [])}
            results = {item['id']: item for item in turn['tools']}
            complete = not turn.get('remaining') and set(calls) == set(results)
            block.append(reply)
            operations, supplied, presented_results = [], [], {}
            for call_id in [*results, *(key for key in calls if key not in results)]:
                call = calls[call_id]
                item = results.get(call_id)
                if item:
                    result = self._decision_record(item['result'], decision_views)
                    owner_observations.append({'record': item['result'], 'observation': result,
                                               'request_count': current})
                else:
                    result = {'status': 'not_executed'}
                record = 'records/' + item['result'].removeprefix('attempts/') if item else None
                if item:
                    key = ('observation', record, item.get('review_result'))
                    if decision_guidance is not None and key in decision_views:
                        presented = decision_views[key]
                    else:
                        presented = self._coordination_result(result, record,
                            delivery_feedback=delivery_feedback if self.operations is not None else None,
                            review_record=item.get('review_result'))
                        if decision_guidance is not None:
                            decision_views[key] = presented
                else:
                    presented = result
                if item:
                    presented_results[call_id] = presented
                indexed = self._history_observation(call, result, item['result'] if item else turn['reply'])
                if operation_history is not None:
                    history_result = self._history_observation(call, result,
                        item['result'] if item else turn['reply'], navigation=True)
                    if not item:
                        history_result.pop('record', None)  # No result receipt exists yet.
                    operation_history.append({'turn': position, 'request_count': current,
                        'turn_complete': complete, 'call_id': call_id,
                        'reply': 'records/' + turn['reply'].removeprefix('attempts/'),
                        'observation': history_result})
                error = result.get('error', '')
                if isinstance(error, str) and len(error) > 512:
                    error = {'preview': error[:512], 'omitted_characters': len(error) - 512,
                        'record': record, 'field': 'error'}
                operations.append([call_id, call['function']['name'], indexed['arguments'], record,
                    result.get('status'), error, result.get('artifacts', []), presented.get('outcome')])
                if call['function']['name'] == 'write_file':
                    try:
                        arguments = json.loads(call['function']['arguments'])
                    except (ValueError, TypeError):
                        arguments = {}
                    text = arguments.get('content') if isinstance(arguments, dict) else None
                    if isinstance(text, str) and text:
                        supplied.append({'turn': position, 'record': 'records/' + turn['reply'].removeprefix('attempts/'),
                            'call_id': call_id, 'field': 'function.arguments.content', 'role': 'write_context',
                            'path': arguments.get('path'), 'text_bytes': len(text.encode('utf-8')),
                            'text_characters': len(text), 'version_scope': 'Authored payload, not evidence or current file state'})
                        if working_mode and item and result.get('status') == 'ok':
                            data = result.get('data') or {}
                            working_rows.append({**supplied[-1], 'text':text,
                                **({'kind': 'authored_text', 'size_bytes': len(text.encode('utf-8')),
                                    'offset_bytes': 0, 'next_offset_bytes': len(text.encode('utf-8'))}
                                   if config.get('work_lifecycle') == 'integrated.v3' else {}),
                                **{key:data[key] for key in ('absolute_path','sha256') if key in data}})
                            if receipt_supply and turn.get('origin') != 'professional':
                                native_observations.append({key: value for key, value in working_rows[-1].items() if key != 'text'})
                if item:
                    block.append({'role': 'tool', 'tool_call_id': call_id,
                        'content': json.dumps(presented,
                            ensure_ascii=False, separators=(',', ':'))})
                    data = result.get('data') or {}
                    authored = None
                    if working_mode and call['function']['name'] == 'edit_file':
                        try:
                            authored = authored_snapshot(self.workspace, result, record,
                                self.session.manifest.session_id)
                        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
                            presented = {**presented, 'authored_snapshot_diagnostic': {
                                'status': 'unavailable', 'record': record, 'field': 'data.authored_text',
                                'reason': str(error)}}
                            presented_results[call_id] = presented
                            block[-1]['content'] = json.dumps(presented, ensure_ascii=False, separators=(',', ':'))
                        if authored is not None:
                            authored = {**authored, 'turn': position, 'record': record, 'call_id': call_id}
                            working_rows.append(authored)
                            # Only the actually presented field can supply a
                            # native body. The compact view normally omits it.
                            visible = presented.get('data') or {}
                            if (receipt_supply and isinstance(visible, dict)
                                    and visible.get('authored_text') == authored['text']
                                    and all(visible.get(key) == authored[key] for key in ('absolute_path', 'sha256'))):
                                native_observations.append({key: value for key, value in authored.items()
                                    if key != 'text'})
                    read_arguments = self._recorded_arguments(call) if receipt_supply else {}
                    read_windows = journal_read_windows(result, read_arguments if isinstance(read_arguments, dict) else {})
                    retained_inputs = config.get('professional_operations') in RETAINED_PROFESSIONAL_CONTRACTS
                    if (working_mode and call['function']['name']!='write_file' and authored is None
                            and (not isinstance(data.get('text'),str) or retained_inputs and not read_windows)):
                        working_rows.append({'turn':position,'record':record,'kind':'operation_result',
                            'tool':call['function']['name'],'text':json.dumps(presented,ensure_ascii=False,separators=(',',':')),
                            'version_scope':'Historical operation result, not proof of current output bytes or new permission'})
                        # This exact presentation is serialized above (or in
                        # the professional operation below), not its raw result.
                        if receipt_supply:
                            native_observations.append({key: value for key, value in working_rows[-1].items() if key != 'text'})
                    # This projection consumes recorded text only; malformed old
                    # arguments must not break later context or grant read access.
                    text_rows = [(row['data'], row['field'], record, call['function']['name'], row['request'])
                        for row in read_windows]
                    if working_mode and retained_inputs and item.get('review_result'):
                        try:
                            review = accepted_review_journal(self.workspace, self.session.store, item, result)
                            review_record = 'records/' + review['record'].removeprefix('attempts/')
                            assessment = ((review['receipt'].get('metadata') or {}).get('delivery_assessment') or {})
                            selected = assessment.get('evidence_sha256', {})
                            if (config.get('working_set_policy') == 'recent.v6'
                                    and assessment.get('coverage') == 'complete'
                                    and assessment.get('verdict') == 'passed'
                                    and not assessment.get('findings')):
                                # The completed opinion already supplies source locators.
                                # Reopen its original windows when needed, rather than repeat
                                # the entire inspection on each production decision. Opinions
                                # needing action still supply their supporting originals.
                                selected = {}
                            if selected:
                                text_rows.extend((row['data'], row['field'], review_record, 'inspect_artifacts', row['request'])
                                    for row in journal_read_windows(review['receipt'], {})
                                    if selected.get(row['data'].get('absolute_path')) == row['data'].get('sha256')
                                    and row['data'].get('sha256'))
                            for action in self.operations.inspection.investigation_actions(Path(item['result']).parent.name):
                                observed = self._history_observation(action['call'], action['result'],
                                    action['record'], navigation=True)
                                working_rows.append({'turn': position, 'record': observed['record'],
                                    'kind': 'operation_result', 'tool': action['call']['function']['name'],
                                    'text': json.dumps({'inspection_action': observed,
                                        'scope': 'Recorded inspection operation; execution alone does not prove '
                                            'independent calculation or scientific correctness.'},
                                        ensure_ascii=False, separators=(',', ':')),
                                    'version_scope': 'Historical inspection action, not current evidence or permission'})
                        except (ValueError, TypeError, KeyError, OSError, RuntimeError):
                            pass  # A missing or unverified opinion grants no material observation.
                    if not read_windows and not retained_inputs:
                        text_rows.append((data, 'data.text', record, call['function']['name'], None))
                    if not retained_inputs:
                        for key in ('matches', 'retained_preview'):
                            text_rows.extend((row, f'data.{key}.{number}.text', record, call['function']['name'], None)
                                for number, row in enumerate(data.get(key) or []) if isinstance(row, dict))
                    for row, field, source_record, source_tool, request in text_rows:
                        if receipt_supply and request is not None:
                            row = {**row, 'offset_bytes': row.get('offset_bytes', request.get('offset_bytes', 0))}
                        text = row.get('text')
                        if not isinstance(text, str) or not text:
                            continue
                        supplied.append({'turn': position, 'record': source_record, 'field': field,
                            'tool': source_tool, 'role': 'historical_observation',
                            **({'kind': 'source_text', 'call_id': call_id,
                                **({'size_bytes': row['size_bytes']} if 'size_bytes' in row else {})}
                               if config.get('work_lifecycle') == 'integrated.v3' else {}),
                            **({'path': indexed['arguments']['path']}
                                if 'path' in indexed['arguments'] and field == 'data.text' else {}),
                            **{key: row[key] for key in ('path', 'absolute_path', 'sha256', 'offset_bytes',
                                'next_offset_bytes', 'has_more', 'chunk_id') if key in row},
                            'text_bytes': len(text.encode('utf-8')), 'text_characters': len(text),
                            'version_scope': 'Recorded observation; current bytes not revalidated by conversation projection'})
                        if working_mode:
                            working_rows.append({**supplied[-1], 'text':text})
                            # Follow the original field in THIS result's actual
                            # presentation. Equal text elsewhere is no supply.
                            # Separate review receipts are not this tool result.
                            if receipt_supply and source_record == record:
                                original, visible = result, presented
                                try:
                                    for part in field.split('.')[:-1]:
                                        original = original[int(part)] if isinstance(original, list) else original[part]
                                        visible = visible[int(part)] if isinstance(visible, list) else visible[part]
                                    if original.get('text') == text and all(original.get(key) == visible.get(key) for key in (
                                            'text', 'path', 'absolute_path', 'sha256', 'offset_bytes',
                                            'next_offset_bytes', 'size_bytes', 'chunk_id')):
                                        native_observations.append(dict(supplied[-1]))
                                except (KeyError, IndexError, TypeError, AttributeError, ValueError):
                                    pass  # Omitted/transformed fields stay eligible as originals.
            index.append([position, current, 'records/' + turn['reply'].removeprefix('attempts/'), operations])
            if complete:
                professional_outcomes.extend({'turn': position, 'request_count': current,
                    'record': operation[3], 'tool': operation[1], 'outcome': operation[7]}
                    for operation in operations if operation[3] and operation[7])
            if turn.get('origin') == 'professional' and complete:
                # Owner-scheduled operations are real observations, not invented
                # provider reasoning/tool calls. Keep their outcome in context.
                block = (request_messages if declared else []) + [{'role': 'user', 'content': json.dumps({
                    'professional_work': reply['content'],
                    'operations': [{'tool': calls[item['id']]['function']['name'],
                        **({'arguments':self._recorded_arguments(calls[item['id']])}
                           if config.get('memory_protocol') == 'journal.v1' else {}),
                        'result': presented_results[item['id']]}
                        for item in turn['tools']]}, ensure_ascii=False)}]
            blocks.append(block if complete else [])
            request_blocks.append(request_indices if complete else set())
            supplies.append(supplied)
            if turn.get('origin') == 'caller' or not complete:
                boundary = max(boundary, position + 1)
        if config.get('work_selection_policy') not in DIRECT_NATIVE_SELECTION_POLICIES:
            from simple_ar.app.professional_conversation import continuation_blocks
            blocks = continuation_blocks(blocks, policy=config.get('native_history_policy'),
                thinking_mode=getattr(client._settings, 'thinking_mode', ''))
        if self.operations is not None and self._unacted_reply_count():
            blocks[-1].append({'role':'system','content':
                'The preceding assistant text did not execute an operation or record delivery. '
                'Use an offered operation or submit a valid delivery/question under the original goal and permissions. '
                'Preserve completed work; textual completion claims are not a saved delivery.'})
        feedback = [{'role': 'user', 'content': (json.dumps({'user_request': {'index': number, 'text': self.state['requests'][number]}},
            ensure_ascii=False) if declared else self.state['requests'][number])} for number in range(count, len(self.state['requests']))]
        prefix = [{'role': 'system', 'content': self._system_text(closing=closing)},
                  {'role': 'user', 'content': ''}]
        selected_schemas = self.schemas if schemas is None else schemas
        schema_cost = estimate_tokens(json.dumps(selected_schemas, ensure_ascii=False))
        if working_mode:
            from simple_ar.core.working_context import (
                current_material_paths, fit_working_context, processed_journal_turns)
            current_artifacts, feedback_targets = set(), set()
            if config.get('professional_operations') in RETAINED_PROFESSIONAL_CONTRACTS and config['context_protocol'] == 'working.v4':
                retained = self._retained_work(observations=owner_observations)
                # Owner references remain compact; the complete inventory stays at
                # its original receipts and in the user snapshot, not mandatory input.
                produced = retained['latest_produced']
                latest = produced['identities'] if produced else []
                delivered = retained['last_delivery']
                # The delivery owner already references its full inventory.
                # Do not duplicate it as mandatory "new production" on feedback.
                produced_versions = ({} if delivered and produced and delivered['record'] == produced['record'] else
                    {row['locator']: row['recorded_sha256'] for row in latest})
                context['retained_work'] = {
                    'acceptance': 'not_inferred', 'last_delivery': retained['last_delivery'],
                    'latest_produced': ({key: value for key, value in produced.items() if key != 'identities'}
                        | {'artifact_count': len(latest),
                           'versions': produced_versions,
                           'unavailable': {row['locator']: row['presence'] for row in latest
                                           if row['presence'] != 'present'},
                           'current_version': 'unchecked',
                           'inventory': ('Same inventory as last_delivery; full versions remain at that record.'
                               if delivered and delivered['record'] == produced['record'] else
                               'Recorded versions from this producer; full owner details remain at record/receipt.')}) if produced else None,
                    'scope': 'Prior delivery and recorded production are not current acceptance. '
                        'Recorded hashes describe prior bindings. See working_view for matching/stale/unknown read versions.'}
                manuscript = ((delivered or {}).get('report') or {}).get('manuscript')
                manuscript_path = None
                if manuscript:
                    try:
                        path = str(self.workspace.resolve(manuscript))
                        primary = next(
                            ({key: row[key] for key in ('locator', 'recorded_sha256', 'current_version', 'presence')}
                             for row in reversed(retained['identities']) if row.get('absolute_path') == path
                             and row['provenance']['record'] == delivered['record']), None)
                        context['retained_work']['last_delivery']['manuscript_identity'] = primary
                        if primary and primary['presence'] == 'present' and primary['recorded_sha256']:
                            manuscript_path = path
                    except (OSError, ValueError):
                        pass
                # Keep the complete delivered inventory discoverable as names.
                # Prior delivery names remain discoverable independently of
                # whether its owner declared a usable primary manuscript.
                revision = (delivered
                    and isinstance(delivered.get('request_count'), int)
                    and delivered['request_count'] < len(self.state['requests']))
                if revision:
                    delivery_rows = [row for row in retained['identities']
                        if row.get('provenance', {}).get('record') == delivered['record']
                        and row['provenance'].get('kind') == 'delivery_binding']
                    context['retained_work']['last_delivery']['artifacts'] = list(dict.fromkeys(
                        row['locator'] for row in delivery_rows))
                    unavailable = {row['locator']: row['presence'] for row in delivery_rows
                        if row.get('presence') != 'present'}
                    if unavailable:
                        context['retained_work']['last_delivery']['unavailable'] = unavailable
                # Resolution grants no new mount and does not read output bytes.
                selected_paths = [primary['locator'], manuscript_path] if manuscript_path is not None else []
                feedback_targets = current_material_paths(identities=retained['identities'],
                    request_count=len(self.state['requests']), selected_paths=selected_paths)
                current_identities = retained['identities']
                if config.get('working_set_policy') == 'recent.v6':
                    # Delivery inventories identify reusable files, not newly
                    # produced work. Preserve their locators above without
                    # promoting every old body into the next working view.
                    current_identities = [row for row in current_identities
                        if row.get('provenance', {}).get('kind') != 'delivery_binding']
                current_artifacts = current_material_paths(identities=current_identities,
                    request_count=len(self.state['requests']), selected_paths=selected_paths)
                spellings = {value for row in working_rows
                    for value in (row.get('path'), row.get('absolute_path')) if isinstance(value, str)}
                for value in spellings:
                    try:
                        resolved = str(self.workspace.resolve(value))
                        if resolved in current_artifacts:
                            current_artifacts.add(value)
                        if resolved in feedback_targets:
                            feedback_targets.add(value)
                    except (OSError, ValueError):
                        pass
            context.pop('requests',None)  # Requests are retained verbatim as messages, once.
            context['current_request_index'] = len(self.state['requests'])-1
            context['history_archive'] = {'total_turns':len(turns), 'access':'read_history',
                'native_history_start_turn':boundary,
                'scope':'All original replies and receipts remain available; no generated summary replaces them.'}
            def version(path):
                try:
                    return self.workspace.artifact_hashes([path])[path]
                except (ValueError,OSError):
                    return None
            journal = config.get('memory_protocol') == 'journal.v1'
            memory = (self._working_memory(observations=observations, decision_views=decision_views)
                      if working_memory is None else working_memory)
            key = ('retained_working_inputs',)
            if key not in decision_views:
                decision_views[key] = self._retained_working_inputs()
            inherited = decision_views[key]
            working_rows.extend(inherited['observations'])
            work_notes = [*memory['notes'], *inherited['notes']]
            selected_records = list((plan or {}).get('evidence_records', []))
            if self.operations is not None and self.operations.work is not None:
                # Recent work already has its atomic journal presentation.
                # Do not turn every post-boundary read into a permanent full
                # source reference. The work owner still resolves its actual
                # selected locations and explicit evidence references.
                scope = self.operations.work.input_view(rows=[] if journal else working_rows,
                    operation_history=operation_history,
                    boundary=boundary, references=selected_records, notes=work_notes)
                if scope is not None:
                    selected_records, work_notes = scope['references'], scope['notes']
                    operation_history = scope['operation_history']
                    archived = set(scope.get('archived_records', ()))
                    if archived:
                        def visible(row):
                            record = row.get('record', '')
                            record = 'records/' + record.removeprefix('attempts/') if record.startswith('attempts/') else record
                            return record not in archived
                        working_rows = [row for row in working_rows if visible(row)]
                        native_observations = [row for row in native_observations if visible(row)]
                    if scope['diagnostic']:
                        context['professional_work']['material_diagnostic'] = scope['diagnostic']
            # Caller turns are not replayed as native assistant/tool messages.
            # Keep their explicitly selected windows eligible throughout this
            # request, not just until the next read or edit. The shared selector
            # still checks versions and fits these observations to capacity.
            selected_records = list(dict.fromkeys([*selected_records, *(
                row['record'] for row in working_rows
                if isinstance(row.get('turn'), int) and 0 <= row['turn'] < len(turns)
                and turns[row['turn']].get('origin') == 'caller'
                and not turns[row['turn']].get('remaining')
                and turns[row['turn']].get('request_count', 1) == len(self.state['requests'])
                and row.get('tool') == 'read_file' and row.get('role') == 'historical_observation'
                and row.get('record'))]))
            if (config.get('professional_operations') in RETAINED_PROFESSIONAL_CONTRACTS
                    and config['context_protocol'] == 'working.v4'):
                # All related checks, including those from an active retained
                # professional work unit, share the same input selection.
                # Their original receipts stay available through the journal.
                from simple_ar.core.working_context import feedback_observations
                related = []
                professional = context.get('professional_work')
                if isinstance(professional, dict) and isinstance(professional.get('original_feedback'), dict):
                    feedback = professional['original_feedback']
                    related.extend(feedback_observations(feedback))
                    professional['original_feedback'] = {key: value for key, value in feedback.items()
                        if key not in {'review_observations', 'professional_findings'}}
                if feedback_targets:
                    targets = tuple(sorted(feedback_targets))
                    key = ('feedback', *targets)
                    if key not in decision_views:
                        decision_views[key] = self._work_feedback(list(targets), observations=observations,
                                                                  decision_views=decision_views)
                    feedback = decision_views[key]
                    related.extend(feedback_observations(feedback))
                additions = []
                for row in related:
                    same = next((existing for existing in [*working_rows, *additions]
                        if existing.get('record') == row['record']
                        and existing.get('kind') == 'operation_result'
                        and existing.get('text') == row['text']), None)
                    if same is None:
                        additions.append(row)
                    else:
                        same.update({key: row[key] for key in ('subjects', 'evidence_role', 'review_scope') if key in row},
                                    related_feedback=True)
                working_rows = [*additions, *working_rows]
            journal_rows = [{'turn':number, 'record':'records/'+path.removeprefix('attempts/')}
                for number, turn in enumerate(turns) if not turn.get('remaining')
                for path in ([item['result'] for item in turn['tools']] or [turn['reply']])] if journal else []
            revision = context.get('delivery_conditions', {}).get('revision')
            priority_sources = []
            if revision:
                # Read windows from the accepted opinion are already in this
                # view. Prefer affected files and short supporting originals;
                # the shared selector checks versions and fits actual windows.
                from simple_ar.tools.workspace import FileWindow

                affected = {path for finding in revision['findings'] for path in finding['artifacts']}
                priority_sources = list(dict.fromkeys(row['absolute_path'] for row in sorted(
                    (row for row in working_rows if row['kind'] == 'source_text' and row.get('absolute_path')
                     and row['text_bytes'] <= FileWindow.model_fields['max_bytes'].default
                     and row.get('offset_bytes') == 0 and row.get('next_offset_bytes') == row.get('size_bytes')
                     and (row['absolute_path'] in affected or row['record'] == revision['record'])),
                    key=lambda row: row['absolute_path'] not in affected)))
                if operation_history is not None:
                    # Older operation listings stay in read_history; the
                    # handoff already identifies the relevant prior work.
                    operation_history = [row for row in operation_history
                        if row['request_count'] == len(self.state['requests'])]
            messages, selection = fit_working_context(
                measure_input=lambda value: estimate_native_input_tokens(client, value, selected_schemas),
                user_requests=self.state['requests'],
                context_authority=config.get('context_authority'),
                message_projector=(self.operations.work_projection if self.operations is not None else None),
                system_text=prefix[0]['content'],base_context=context,
                turn_blocks=[block if position>=boundary else [] for position,block in enumerate(blocks)],
                observations=working_rows, schema_tokens=schema_cost,context_token_target=target,
                steady_working_target=(min(target, max(36000, target // 3))
                    if config.get('context_checkpoint_policy') == 'economic.v3' else None),
                native_observations=native_observations if receipt_supply else None,
                preserve_material_observations=False,
                working_set_policy=(config.get('working_set_policy', 'recent.v6')
                    if config.get('work_lifecycle') == 'integrated.v3' else None),
                estimate_tokens=estimate_tokens,work_notes=work_notes,
                require_work_notes=config.get('memory_protocol') == 'journal.v1',
                fit_work_notes=config.get('memory_protocol') == 'journal.v1',
                economical_checkpoint=config.get('context_checkpoint_policy') == 'economic.v3',
                economical_after_turn=memory.get('attempted_through_turn', 0),
                sparse_handoffs=config.get('context_checkpoint_policy') == 'economic.v3',
                summarized_through_turn=memory['through_turn'],
                selected_records=selected_records, priority_source_paths=priority_sources,
                current_artifacts=sorted(current_artifacts), version_resolver=version,
                block_request_counts=[turn.get('request_count',1) for turn in turns],
                material_policy=('working' if config.get('context_checkpoint_policy') == 'economic.v3' else
                    'retained' if config.get('professional_operations') in RETAINED_PROFESSIONAL_CONTRACTS
                    and config['context_protocol'] == 'working.v4' else
                    'scoped' if config['context_protocol'] == 'working.v4' else
                    'referenced' if config['context_protocol'] in _REFERENCED_CONTEXT_PROTOCOLS else 'ranked'),
                operation_history=operation_history,
                **({'processed_turns':processed_journal_turns(journal_rows, memory.get('compacted_records', ())),
                    **({'archived_turns':processed_journal_turns(journal_rows, memory.get('archived_records', ()))}
                       if config.get('work_method') in NATIVE_WORK_METHODS else {})} if journal else {}))
            if working_selection is not None:
                working_selection.update(selection)
            # The selector has already measured this exact final request with
            # selected_schemas; reuse that result for the allowance boundary.
            cost = selection['estimated_input_tokens']
            if remaining is not None and cost+output>remaining:
                raise ValueError('Current working view exceeds the remaining token allowance; no request sent')
            if getattr(client._settings, 'thinking_mode', '') == 'enabled' and any(
                    message['role'] == 'assistant' and not any(isinstance(message.get(key), str)
                        for key in ('reasoning_content', 'reasoning')) for message in messages):
                raise ValueError('Native history lacks reasoning_content or reasoning; explicitly switch models to start a new native session')
            return messages
        start = boundary

        def project() -> tuple[list, int]:
            if declared:
                supplied_requests = set(range(count, len(self.state['requests'])))
                for indices in request_blocks[start:]:
                    supplied_requests.update(indices)
                context['requests'] = [{'index': number, 'text': text}
                    for number, text in enumerate(self.state['requests']) if number not in supplied_requests]
            index_start = max(0, start - 16)
            context['retained_turn_start'] = start
            context['history_archive'] = {'start_turn': 0, 'end_turn': start, 'total_turns': len(turns),
                'native_history_start_turn': boundary, 'capacity_evicted_turns': start - boundary}
            context['earlier_turns'] = {'columns': columns, 'operation_columns': operation_columns,
                'start_turn': index_start, 'end_turn': start, 'total_archived_turns': start,
                'omitted_turns': index_start, 'rows': index[index_start:start],
                'scope': 'Recent archived index only (at most 16 turn blocks), not exhaustive history. '
                    'Ranges are zero-based and end-exclusive. Use read_history for older turns; '
                    'complete exact arguments are at reply refs under their call id, and results at record refs.'}
            # Professional results have a different lifetime from low-level
            # read conversation. Rebuild a bounded index from closed receipts;
            # do not require the coordinator to reread scientific files merely
            # to rediscover a valid production root after unrelated reads.
            context['professional_outcomes'] = [row for row in professional_outcomes
                if row['turn'] < index_start][-16:]
            if context['professional_outcomes']:
                context['professional_outcome_scope'] = ('Older owner-reported work phases outside the recent '
                    'archive index; not current bytes, independent verification, permission or a prescribed '
                    'next operation. Request counts distinguish earlier goals; dependent owners revalidate inputs.')
            else:
                context.pop('professional_outcome_scope', None)
            context['material_supply']['native_context'] = [row for rows in supplies[start:] for row in rows]
            context['material_supply']['omitted'] = ([{'start_turn': 0, 'end_turn': start,
                'status': 'history_not_supplied', 'access': 'read_history for the archived turn range'}] if start else [])
            if 'inputs' in focus or 'previous_inputs' in focus:
                current_selection = 'inputs' in focus
                context['work_inputs'].update(record=focus['record'], current_selection=current_selection,
                    windows=[{'request': row['request'],
                        'status': ('unavailable' if 'error' in row else
                            ('supplied_native' if (row.get('data') or {}).get('text') else 'empty')
                            if focus['turn'] >= start else 'history_not_supplied')
                            if current_selection else 'previous_not_selected',
                        **({'error': row['error']} if 'error' in row else {})}
                        for row in focus.get('inputs', focus.get('previous_inputs', []))])
            resources = context['resources']
            resources.update(approximate_next_input_tokens=0, context_exceeds_input_target=False)
            while True:
                prefix[1]['content'] = json.dumps(context, ensure_ascii=False, separators=(',', ':'))
                messages = prefix + [message for block in blocks[start:] for message in block] + feedback
                cost = estimate_tokens(json.dumps(messages, ensure_ascii=False)) + schema_cost
                exceeds = resources['context_exceeds_input_target'] or cost > target
                if resources['approximate_next_input_tokens'] == cost and resources['context_exceeds_input_target'] == exceeds:
                    return messages, cost
                resources.update(approximate_next_input_tokens=cost, context_exceeds_input_target=exceeds)

        messages, cost = project()
        # The working target is not permission to discard the only fresh
        # observation and pay to read it again. Keep the latest atomic native
        # turn; the actual shared token allowance remains a hard preflight.
        latest = max((number for number in range(boundary, len(blocks)) if blocks[number]), default=None)
        while cost > target and start < (latest if latest is not None else len(blocks)):
            start += 1
            messages, cost = project()
        if remaining is not None and cost + output > remaining:
            raise ValueError('Latest complete observation and contracts exceed the remaining token allowance; no model request sent')
        if cost > target and latest is None:
            raise ValueError('Current requests, contracts and history index exceed remaining input capacity; no model request sent')
        if getattr(client._settings, 'thinking_mode', '') == 'enabled' and any(
                message['role'] == 'assistant' and not any(isinstance(message.get(key), str)
                    for key in ('reasoning_content', 'reasoning')) for block in blocks[start:] for message in block):
            raise ValueError('Native history lacks reasoning_content or reasoning; explicitly switch models to start a new native session')
        return messages
