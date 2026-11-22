"""Authorization, budget continuation, model switching, and recovery mixin for ToolApplication."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable

from simple_ar.app.professional_conversation import INTEGRATED_WORK_LIFECYCLES
from simple_ar.core.budget import completed_llm_usage_is_accounted
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest
from simple_ar.integrations.llm import LLMClient, LLMSettings
from simple_ar.integrations.model_profiles import model_connections_compatible
from simple_ar.tools.publication import ExportRequest
from simple_ar.tools.specs import ToolCall, ToolResult
from simple_ar.tools.workspace import resolve_source_mount


def _tool_app_module():
    import simple_ar.app.tool_application as module
    return module


class _ToolRecoveryMixin:
    def _action_recovery(self, pending: dict, perform: Callable | None) -> Callable | None:
        """Recover only an existing owner's closed receipt, never repeat its effect."""
        if pending.get('execution_rejection'):
            return lambda: self._execution_rejection(pending).model_dump(mode='json')
        if pending['kind'] in {'opinion', 'receipt'}:
            return lambda: perform(pending['attempt'])
        if pending['kind'] == 'model':
            recovery = getattr(perform, 'recover', None)
            if recovery is not None:
                return lambda: recovery(pending['attempt'])
            # Earlier action owners persisted a pre-dispatch budget rejection as
            # a failed attempt rather than a closed observation. Its exact
            # diagnostic and absent reservation establish that no provider call
            # occurred; retain the failure and publish only that fact.
            store = ArtifactStore(self.session.store.root / 'attempts' / pending['attempt'])
            if store.exists('capability_result.json') and not any(
                    row.attempt_id == pending['attempt'] for row in self.ledger.entries):
                result = store.read_capability_result()
                if (result.status == 'failed' and result.provenance.get('capability') == 'native_action'
                        and len(result.diagnostics) == 1
                        and result.diagnostics[0].startswith('LLMError: LLM budget prevented provider attempt 1 for ')):
                    return lambda: ToolResult(tool_name='native_action', status='blocked',
                        error=result.diagnostics[0], metadata={'budget_rejected_before_dispatch': True}).model_dump(mode='json')
            return None
        call = pending.get('call') or {}
        name = call.get('function', {}).get('name', '')
        if self.gateway.can_reconcile(ToolCall(tool_name=name, trace_id=pending['attempt'])):
            return lambda: perform(pending['attempt'])
        return None

    def execution_binding(self, call: dict[str, Any]) -> dict[str, Any]:
        """Describe the actual inputs bound to a pending execution approval."""
        return self.execution_approval(call)['binding']

    def execution_approval(self, call: dict[str, Any]) -> dict[str, Any]:
        """Project approval through the execution owner, without granting it."""
        owner = self.gateway.session_owner(call['function']['name'])
        if owner is None and call['function']['name'] == 'export_report':
            request = ExportRequest.model_validate(json.loads(call['function']['arguments']))
            source = self.workspace.resolve(request.markdown, write=True)
            binding = {'call': deepcopy(call), 'inputs': self.publication.input_binding(source, request)}
            return {'binding': binding, 'token': self.workspace.approval_digest(binding)}
        return self.workspace.execution_approval(call, pending=self.state.get('pending'),
            owner=owner)

    def approval_token(self, call: dict[str, Any]) -> str:
        return self.execution_approval(call)['token']

    def _authorize(self, sources: tuple[Path, ...], *, execution: bool, network: bool, vision: bool) -> str:
        """Explicit caller grants within the existing task, never inferred intent."""
        if not (sources or execution or network or vision):
            return ''
        self._check_authorization_operations()
        pending = self.state['pending']
        if pending and (pending['kind'] == 'model' or any(
                row.attempt_id == pending['attempt'] for row in self.session.list_attempts())):
            raise ValueError('Reconcile the started operation before changing authorization')
        paths = tuple(resolve_source_mount(source) for source in sources)
        if self.state['config']['model'] is not None:
            self._client()  # Authorization is not a way to switch the saved model.
        config = dict(self.state['config'])
        if vision:
            vision_hint = config.get('model_catalogs', {}).get('vision')
            if not config.get('allow_vision', False) and not config.get('vision_connection'):
                # Only this explicit first grant may discover a new role in
                # the task's previously selected catalog. Historical grants
                # without role hints retain their original env/global lookup.
                vision_hint = vision_hint or config.get('model_catalogs', {}).get('task')
            vision_client = self._client(purpose='vision', vision_catalog_hint=vision_hint)
            if not config.get('allow_vision', False):
                config['vision_connection'] = self._vision_binding(vision_client)
                path = _tool_app_module().catalog_path(fallback_path=vision_hint)
                if path is not None:
                    config['model_catalogs'] = {**config.get('model_catalogs', {}),
                        'vision': str(path.resolve())}
        mounts = dict(config['sources'])
        changes = []
        for path in paths:
            if str(path) in mounts.values():
                continue
            index = 0
            while f'source{index}' in mounts:
                index += 1
            mounts[f'source{index}'] = str(path)
            changes.append(f'read-only source{index}: {path}')
        for key, granted in (('allow_execution', execution), ('allow_network', network), ('allow_vision', vision)):
            if granted and not config.get(key, False):
                config[key] = True
                changes.append(key)
        if not changes:
            return ''
        config['sources'] = mounts
        self.state['config'] = config
        self._configure_tools()
        return 'User explicitly authorized additional access: ' + '; '.join(changes) + '. Saved budgets are unchanged; individual process commands still need approval.'

    def _closed_llm_charge(self, entry: Any) -> bool:
        """Prove local operation closure, not provider usage or output validity."""
        from simple_ar.app.work_memory import MEMORY_PURPOSES
        if self.state['config'].get('memory_protocol') == 'journal.v1' and entry.purpose in MEMORY_PURPOSES:
            if self._retained_checkpoint_charge(entry):
                return True
        if (entry.status != 'unknown' or entry.actual_source not in {None, 'reservation_bound'}
                or entry.session_id != self.session.manifest.session_id
                or not entry.logical_call_id
                or set(entry.reserved) != {'llm_requests', 'total_tokens'}
                or entry.reserved['llm_requests'] != 1
                or entry.actual.get('llm_requests') != 1
                or type(entry.reserved['total_tokens']) is not int or entry.reserved['total_tokens'] <= 0
                or any(key not in entry.reserved or type(value) not in {int, float}
                    or not math.isfinite(value) or not 0 <= value <= entry.reserved[key]
                    for key, value in entry.actual.items())):
            return False
        attempt = next((row for row in self.session.list_attempts()
                        if row.attempt_id == entry.attempt_id), None)
        if attempt is None or attempt.status not in {'completed', 'failed'}:
            return False
        if attempt.capability == 'native_turn':
            return (self._accepted_model_attempt(entry.attempt_id) is not None
                or self._failed_model_attempt({'kind':'model','attempt':entry.attempt_id},
                    current_only=False) is not None)
        if attempt.capability == 'native_action':
            if self._failed_professional_model(attempt):
                return True
            store = ArtifactStore(self.session.store.root / 'attempts' / entry.attempt_id)
            if (attempt.status != 'completed' or not store.exists('capability_result.json')
                    or not completed_llm_usage_is_accounted(row.to_dict() for row in self.ledger.entries
                        if row.attempt_id == entry.attempt_id)):
                return False
            result = store.read_capability_result()
            return result.status == 'completed' and all(store.require(ref).exists() for ref in result.artifacts)
        if attempt.capability != 'native_tool':
            return False
        relative = f'attempts/{entry.attempt_id}/native_result.json'
        if not any(tool.get('result') == relative for turn in self.state['turns']
                   for tool in turn.get('tools', [])):
            return False
        store = ArtifactStore(self.session.store.root / 'attempts' / entry.attempt_id)
        if not store.exists('capability_result.json') or not store.exists('native_result.json'):
            return False
        result = store.read_capability_result('capability_result.json')
        if result.status != attempt.status:
            return False
        if attempt.status == 'failed':
            observed = store.read_json('native_result.json')
            return (result.provenance.get('recovery') == 'explicit_interruption'
                and observed.get('metadata', {}).get('provenance') == dict(result.provenance)
                and observed.get('metadata', {}).get('capability_result') == self.workspace.locator(
                    str(store.resolve('capability_result.json'))))
        return any(ref.path == 'native_result.json' for ref in result.artifacts)

    def _check_authorization_operations(self, *, explicit_continuation: bool = False) -> None:
        """Distinguish a closed operation from the liability it left behind.

        An explicit model switch or remaining-allowance grant may refer to a
        closed call with unknown usage. Automatic work still requires that its
        unknown dimensions belong to an earlier explicitly authorized epoch.
        Neither operation changes or settles the original charge.
        """
        attempts = {attempt.attempt_id: attempt for attempt in self.session.list_attempts()}
        if any(attempt.status == 'running' for attempt in attempts.values()):
            raise ValueError('Reconcile in-flight or unknown operations before authorizing resources')
        current_unknown = set(self.ledger.unknown_dimensions(
            unbounded_only=True, current_allowance_only=True))
        for entry in self.ledger.entries:
            if entry.status not in {'reserved', 'unknown'}:
                continue
            if self._retained_checkpoint_charge(entry):
                continue
            if (self._closed_llm_charge(entry) and (explicit_continuation
                    or entry.actual_source == 'reservation_bound'
                    or not (set(entry.reserved) - set(entry.actual)) & current_unknown)):
                continue
            raise ValueError('Reconcile in-flight or unknown operations before authorizing resources')
    def _authorize_resources(self, allowances: dict[str, int | float],
                             authorization_id: str, reason: str, *,
                             additional_attempts: int | None = None,
                             additional_no_progress: int | None = None,
                             model_binding: dict[str, Any] | None = None, persist: bool = True,
                             resume_after: str | None = None) -> None:
        for value in (additional_attempts, additional_no_progress):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Additional attempt allowances must be non-negative integers')
        if not allowances and not additional_attempts and not additional_no_progress:
            if authorization_id or reason:
                raise ValueError('Resource authorization requires an explicit allowance')
            return
        requests = allowances.get('llm_requests', 0)
        if 'llm_requests' in allowances and (type(requests) is not int or requests <= 0):
            raise ValueError('llm_requests allowance must be a positive integer')
        self._check_authorization_operations(explicit_continuation=True)
        if self.state['config']['model'] is not None:
            self._client()  # Resource authorization cannot change model identity.
        elif not model_binding and set(allowances) & {'llm_requests', 'total_tokens', 'image_requests'}:
            raise ValueError('Caller-directed tasks cannot acquire model access through resource continuation')
        if allowances:
            self.ledger.validate_remaining(allowances, authorization_id=authorization_id, reason=reason)
        self.session.manifest.budget.authorize_additional(authorization_id,
            attempts=requests * 10 if additional_attempts is None else additional_attempts,
            no_progress=additional_no_progress or 0, resource_allowances=allowances, reason=reason,
            resume_after=resume_after)
        self.session.save()
        if allowances:
            self.ledger.authorize_remaining(allowances, authorization_id=authorization_id, reason=reason)
        pending = self.state['pending']
        if pending and pending['kind'] == 'model':
            attempt = self._failed_model_attempt(pending)
            if (attempt is not None
                    and not any(entry.attempt_id == attempt.attempt_id for entry in self.ledger.entries)):
                # Budget preflight failed before a provider reservation. Preserve
                # that failed attempt; the next action receives a new identity.
                self.state['pending'] = None
        if persist:
            self._save()

    def _failed_model_attempt(self, pending: dict[str, Any], *, current_only: bool = True) -> AttemptManifest | None:
        """Prove a closed failure; pending/retry callers require the current identity."""
        identity = pending['attempt']
        attempt = next((row for row in self.session.list_attempts() if row.attempt_id == identity), None)
        store = ArtifactStore(self.session.store.root / 'attempts' / identity)
        if (pending['kind'] != 'model' or attempt is None
                or attempt.capability != 'native_turn' or attempt.status != 'failed'
                or current_only and self.session.manifest.current_attempt != identity
                or store.exists('native_result.json') or not store.exists('capability_result.json')):
            return None
        result = store.read_capability_result('capability_result.json')
        if result.status != 'failed' or any(
                ref.path != 'capability_result.json' for ref in (*attempt.outputs, *result.artifacts)):
            return None
        return attempt

    def _accepted_model_attempt(self, identity: str) -> AttemptManifest | None:
        """Prove a locally closed model operation whose reply entered history.

        A completed logical call can retain unknown physical charges from an
        earlier transport failure. Accepting its reply does not settle them.
        """
        attempt = next((row for row in self.session.list_attempts() if row.attempt_id == identity), None)
        if (attempt is None or attempt.capability != 'native_turn' or attempt.status != 'completed'
                or f'attempts/{identity}/native_result.json' not in {row['reply'] for row in self.state['turns']}):
            return None
        store = ArtifactStore(self.session.store.root / 'attempts' / identity)
        if not store.exists('capability_result.json') or not store.exists('native_result.json'):
            return None
        result = store.read_capability_result('capability_result.json')
        memory_refs = self._memory_artifacts(store,identity)
        if (result.status != 'completed'
                or [ref for ref in result.artifacts if ref.path != 'native_result.json'] != list(memory_refs)
                or sum(ref.path == 'native_result.json' for ref in result.artifacts) != 1):
            return None
        return attempt

    def _replacement_text_client(self, model_selector: str, *, attempt_id: str = "",
                                 allow_completed_unknown: bool = False) -> tuple[LLMClient, dict[str, Any]]:
        """Resolve an explicit binding without sending work or altering accounting."""
        if not model_selector.startswith(('profile:', 'route:')):
            raise ValueError('Explicit model continuation requires a named profile:NAME or route:NAME')
        parent = LLMClient.from_env(model_selector, budget_ledger=self.ledger,
            budget_session_id=self.session.manifest.session_id, budget_attempt_id=attempt_id)
        replacement = parent.connection_binding()
        if not replacement or parent._settings.api_mode != 'chat':
            raise ValueError('Replacement must be a named Chat connection; no protocol conversion')
        client = self._text_client(parent, self._coordinator_purpose())
        attempts = self.session.list_attempts()
        # Historical retry keeps its failure-only contract. Modern explicit
        # recovery and switching share the same proof of closed model work;
        # neither settles charges or replays the professional operation.
        if allow_completed_unknown:
            self._check_authorization_operations(explicit_continuation=True)
        for entry in (() if allow_completed_unknown else self.ledger.entries):
            if entry.status == 'unknown' and (
                    entry.purpose != 'native-turn' or entry.actual_source != 'reservation_bound'
                    or type(entry.reserved.get('total_tokens')) is not int
                    or entry.reserved['total_tokens'] <= 0
                    or not (any(row.attempt_id == entry.attempt_id and row.status == 'failed'
                                 and row.capability == 'native_turn' for row in attempts)
                             and not self.session.store.exists(f'attempts/{entry.attempt_id}/native_result.json'))):
                raise ValueError('Unconfirmed operation must be reconciled before changing models')
        if self.state['config'].get('allow_vision'):
            vision = LLMClient.from_env(purpose='vision',
                fallback_models_config=self.state['config'].get('model_catalogs', {}).get('vision'))
            if not model_connections_compatible(self.state['config'].get('vision_connection') or {},
                                                 self._vision_binding(vision)):
                raise ValueError('Text replacement cannot silently change the saved vision connection')
        return client, replacement

    @staticmethod
    def _replacement_catalogs(config: dict[str, Any]) -> dict[str, Any]:
        """An explicit text selection owns a fresh hint, independent of other consumers."""
        hints = dict(config.get('model_catalogs', {}))
        hints.pop('task', None)
        path = _tool_app_module().catalog_path()
        if path is not None:
            hints['task'] = str(path.resolve())
        return {'model_catalogs': hints} if hints or 'model_catalogs' in config else {}

    def switch_model(self, model_selector: str, *, switch_id: str, reason: str,
                     expected_connection: dict[str, Any] | None = None) -> dict[str, Any]:
        """Start a native session after reconciliation; retain the task journal and budgets."""
        with self.session.mutation_scope():
            if (not isinstance(switch_id, str) or not switch_id.strip()
                    or not isinstance(reason, str) or not reason.strip()
                    or not isinstance(model_selector, str)
                    or not model_selector.startswith(('profile:', 'route:'))):
                raise ValueError('Model switch requires a named connection, stable switch id and explicit reason')
            switch_id, reason = switch_id.strip(), reason.strip()
            config = self.state['config']
            records = config.get('model_rebindings', [])
            previous = next((row for row in records if row.get('switch_id') == switch_id), None)
            if previous is not None:
                if (previous['to_selector'] != model_selector or previous['reason'] != reason
                        or expected_connection is not None and previous['to_connection'] != expected_connection):
                    raise ValueError('Model switch id already exists with different terms')
                return previous  # Replaying an older id never reverts a later switch.
            if config['model'] is None or 'llm_client' in config.get('runtime_dependencies', {}):
                raise ValueError('Model switch requires an existing non-injected text model authorization')
            if self.state.get('pending_supply') is not None:
                raise ValueError('Complete the retained caller supply before changing models')
            attempts = self.session.list_attempts()
            accepted = {turn['reply'] for turn in self.state['turns']}
            if (self.state['pending'] or any(turn['remaining'] for turn in self.state['turns'])
                    or any(row.status == 'running' for row in attempts)
                    or any(entry.status == 'reserved' for entry in self.ledger.entries)
                    or any(row.capability == 'native_turn'
                        and self.session.store.exists(f'attempts/{row.attempt_id}/native_result.json')
                        and f'attempts/{row.attempt_id}/native_result.json' not in accepted for row in attempts)):
                raise ValueError('Reconcile pending, running, reserved or cached operations before changing models')
            _, replacement = self._replacement_text_client(model_selector, allow_completed_unknown=True)
            if expected_connection is not None and replacement != expected_connection:
                raise ValueError('Resolved connection changed after confirmation; inspect and confirm again')
            record = {'switch_id': switch_id, 'reason': reason,
                'from_selector': config['model'], 'from_connection': config.get('model_connection') or {},
                'to_selector': model_selector, 'to_connection': replacement,
                'native_history_start_turn': len(self.state['turns'])}
            # One original journal commit. No attempt, transition, allowance,
            # feedback or delivery invalidation accompanies this operation.
            self.state['config'] = {**config, 'model': model_selector, 'model_connection': replacement,
                **self._replacement_catalogs(config),
                'model_rebindings': [*records, record]}
            self._save()
            return record

    def _failed_professional_model(self, attempt: AttemptManifest) -> bool:
        """Identify a stopped professional model action with no accepted reply."""
        if attempt.capability != 'native_action' or attempt.status != 'failed':
            return False
        store = ArtifactStore(self.session.store.root / 'attempts' / attempt.attempt_id)
        if (store.exists('native_result.json') or not store.exists('action.json')
                or not store.exists('capability_result.json')):
            return False
        action = store.read_json('action.json')
        result = store.read_capability_result()
        return (action.get('attempt') == attempt.attempt_id
            and action.get('kind') == action.get('origin', {}).get('kind') == 'model'
            and result.status == 'failed'
            and all(ref.path == 'capability_result.json' for ref in (*attempt.outputs, *result.artifacts)))

    @staticmethod
    def _professional_action_identity(origin: dict, retry_of: str = '') -> str:
        key = {**origin, 'retry_of': retry_of} if retry_of else origin
        return 'native-action-' + hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:40]

    def _retry_professional_model(self, parent: dict, model_selector: str) -> None:
        action = parent.get('action') or {}
        if action.get('kind') != 'model' or action.get('origin', {}).get('scope') != parent['inspection']:
            raise ValueError('--retry-model requires a pending model decision inside the inspection')
        if model_selector:
            raise ValueError('Resume a professional model step with its saved connection')
        identity = action['attempt']
        store = ArtifactStore(self.session.store.root / 'attempts' / identity)
        if action.get('result') or store.exists('native_result.json'):
            return  # Ordinary recovery adopts the existing response.
        attempts = self.session.list_attempts()
        attempt = next((row for row in attempts if row.attempt_id == identity), None)
        if attempt is None and action.get('retry_of'):
            return  # The previously authorized retry has yet to dispatch.
        if (attempt is None or not self._failed_professional_model(attempt)
                or store.read_json('action.json') != action):
            raise ValueError('Cannot prove a closed professional model failure without a response')
        self._check_authorization_operations()
        charges = [row for row in self.ledger.entries if row.attempt_id == identity]
        if (not charges or any(row.status == 'settled' and row.actual.get('total_tokens')
                and row.actual_source != 'estimated' for row in charges)
                or not any(row.actual_source == 'transport_pre_dispatch'
                    or row.actual_source == 'reservation_bound'
                    and 'transport phase=possibly_dispatched' in row.reason
                    or row.actual_source == 'estimated'
                    and row.reason.startswith('provider attempt failed:') for row in charges)):
            raise ValueError('The failed professional step has no retryable provider failure')
        if any(self.ledger.remaining(key) == 0 for key in ('llm_requests', 'total_tokens')):
            raise ValueError('Remaining model budget is exhausted')
        self._client(parent['inspection'])
        if self.session.manifest.status != 'running':
            self.session.continue_with_revision('Explicit --retry-model continuation of ' + identity)
        parent['action'] = {**action, 'retry_of': identity,
            'attempt': self._professional_action_identity(action['origin'], identity)}
        self._save()

    def _retry_model(self, model_selector: str = "") -> None:
        """Authorize one new decision, retaining failed attempts and all charges."""
        pending = self.state['pending']
        if pending and pending.get('inspection'):
            self._retry_professional_model(pending, model_selector)
            return
        if 'llm_client' in self.state['config'].get('runtime_dependencies', {}):
            raise ValueError('Injected clients do not support named-connection retries; restore their original service and reconcile the failed operation')
        if not pending or pending['kind'] != 'model' or any(turn['remaining'] for turn in self.state['turns']):
            raise ValueError('--retry-model requires a pending native model decision, not a tool or new task')
        if self.session.manifest.status in {'created', 'completed', 'partial'}:
            raise ValueError('--retry-model requires unsettled failed work, not a new or delivered task')
        attempts = self.session.list_attempts()
        cached = self.session.store.exists(f"attempts/{pending['attempt']}/native_result.json")
        if cached and not any(row.attempt_id == pending['attempt'] and row.capability == 'native_turn'
                for row in attempts):
            raise ValueError('Cached response does not belong to a native_turn; inspect before recovery')
        if any(row.status == 'running' and (row.attempt_id != pending['attempt'] or not cached)
                for row in attempts) or any(entry.status == 'reserved' for entry in self.ledger.entries):
            raise ValueError('Model operation is in-flight or unconfirmed; cannot retry running/reserved work')
        # A durable reply is consumed by the existing recovery path, even if
        # the outer result/status write was interrupted. Never request it again.
        if cached:
            if model_selector:
                raise ValueError('Recover the cached reply with its saved connection before changing models')
            self._client(pending['attempt'])
            return
        attempt = self._failed_model_attempt(pending)
        if attempt is None:
            raise ValueError('Cannot prove a terminated failed native_turn without accepted outputs')
        replacement = None
        if model_selector:
            client, replacement = self._replacement_text_client(model_selector, attempt_id=attempt.attempt_id,
                allow_completed_unknown=self.operations is not None)
            if self.state['config'].get('max_output_tokens') is not None:
                client = client.with_output_limit(self.state['config']['max_output_tokens'])
        else:
            client = self._client(pending['attempt'])
        entries = [entry for entry in self.ledger.entries if entry.attempt_id == attempt.attempt_id]
        from simple_ar.app.work_memory import checkpoint_entries, MEMORY_PURPOSES
        memory_entries = []
        if any(entry.logical_call_id == 'working-memory-'+attempt.attempt_id
                or entry.purpose in MEMORY_PURPOSES for entry in entries):
            store = ArtifactStore(self.session.store.root / 'attempts' / attempt.attempt_id)
            store.require('memory_input.json')
            memory_entries = checkpoint_entries(self.ledger,attempt.attempt_id,self.session.manifest.session_id,
                **({'allow_bounded_unknown':True} if self.state['config'].get('memory_protocol') == 'journal.v1' else {}))
            if (all(entry.status in {'settled','released'} for entry in memory_entries)
                    and (store.exists('memory_snapshot.json') or store.exists('memory_failure.json'))):
                self._memory_artifacts(store,attempt.attempt_id)
        entries = [entry for entry in entries if entry not in memory_entries]
        for entry in entries:
            if (entry.purpose != 'native-turn' or entry.logical_call_id != attempt.attempt_id
                    or entry.session_id != self.session.manifest.session_id):
                raise ValueError('Failed decision has an unrecognized budget record; inspect before retry')
            if entry.status == 'unknown' and (entry.actual_source != 'reservation_bound'
                    or entry.actual.get('llm_requests') != 1
                    or type(entry.reserved.get('total_tokens')) is not int
                    or entry.reserved['total_tokens'] <= 0):
                raise ValueError('Unknown model charges have no finite retained reservation; cannot retry')
        binding = self.state['config'].get('model_connection') or {}
        settings = self._work_connection(binding, self._coordinator_purpose()).get('connection')
        if not settings:
            raise ValueError('Saved model output cap is unavailable; cannot safely retry')
        saved_client = LLMClient(LLMSettings(api_key=client._settings.api_key, **{key: settings[key] for key in (
            'max_output_tokens', 'reasoning_effort', 'thinking_mode', 'reasoning_output_tokens')
            if key in settings}))
        if self.state['config'].get('max_output_tokens') is not None:
            saved_client = saved_client.with_output_limit(self.state['config']['max_output_tokens'])
        saved_cap = saved_client.effective_output_limit()
        cap = client.effective_output_limit()
        if cap != saved_cap:
            raise ValueError('Retry requires the saved output setting; restore compatible settings without changing it')
        failed_offer = ArtifactStore(self.session.store.root / 'attempts' / attempt.attempt_id)
        failed_cap = (failed_offer.read_json('decision_budget.json').get('request_output_limit', saved_cap)
            if failed_offer.exists('decision_budget.json') else saved_cap)
        if any(entry.status == 'unknown' and (failed_cap is None
                or entry.reserved['total_tokens'] < failed_cap) for entry in entries):
            raise ValueError('Unknown reservation does not cover the saved output cap; inspect before retry')
        budget = self.session.manifest.budget
        requests = self.ledger.remaining('llm_requests')
        if (budget.exhausted() or not self._can_request(1)
                or requests is None and self.ledger.limits.get('llm_requests') is not None
                or requests is not None and requests < 1):
            raise ValueError('Remaining decision budget is exhausted; retry cannot increase it')
        # This is a read-only preflight using the same serialized inputs and
        # effective cap as chat_turn. LLMClient still owns actual reservations
        # and each physical provider attempt, including its existing retries.
        tokens = self.ledger.remaining('total_tokens')
        from simple_ar.app.professional_conversation import ProfessionalConversation
        conversation = ProfessionalConversation(client)
        output_limit = (conversation.closing_output_limit()
            if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES else None)
        retry_client = conversation.projection_client(output_limit)
        schemas = self._closing_schemas() if output_limit is not None else None
        required = self._decision_tokens(retry_client, self._messages(client=retry_client,
            closing=output_limit is not None, schemas=schemas,
            native_history_start_turn=len(self.state['turns']) if model_selector else None), schemas=schemas)
        if (tokens is None and self.ledger.limits.get('total_tokens') is not None
                or tokens is not None and tokens < required):
            raise ValueError('Remaining token budget cannot cover the saved output cap and decision input')
        if self.session.manifest.status != 'running':
            self.session.continue_with_revision('Explicit --retry-model continuation of ' + attempt.attempt_id)
        identity = self.session.allocate_attempt_id('native-turn')
        if replacement is not None:
            config = self.state['config']
            # One actions.json commit owns both the explicit connection change
            # and new decision identity. Historical replies and ledger entries
            # are never rewritten, and no second transition state is created.
            self.state['config'] = {**config, 'model': model_selector, 'model_connection': replacement,
                **self._replacement_catalogs(config),
                'model_rebindings': [*config.get('model_rebindings', []), {
                    'failed_attempt': attempt.attempt_id,
                    'from_selector': config['model'], 'from_connection': binding,
                    'to_selector': model_selector, 'to_connection': replacement,
                    'native_history_start_turn': len(self.state['turns']),
                    'reason': 'Explicit --retry-model --model continuation; original task, access and budget retained',
                }]}
        self.state['pending'] = {'kind': 'model', 'attempt': identity,
            **({'skip_memory_checkpoint':True} if memory_entries else {})}
        self._save()

    def _retained_work(self, *, observations: list[dict] | None = None) -> dict[str, Any]:
        """Supply native owner observations to the shared read-only workspace view."""
        if observations is None:
            observations = []
            for turn in self.state.get('turns', []):
                for tool in turn.get('tools', []):
                    try:
                        observed = self.session.store.read_json(tool['result'])
                    except (OSError, ValueError, KeyError):
                        continue
                    observations.append({'record': tool['result'], 'observation': observed,
                                         'request_count': turn.get('request_count', 1)})
        try:
            delivery = self.session.store.read_json('delivery.json')
        except (OSError, ValueError, KeyError):
            delivery = None
        return self.workspace.working_result(observations, saved_delivery=delivery,
            completions=self.state['config'].get('tool_completions'))

    def result_snapshot(self) -> dict[str, Any]:
        """Read saved results for users; never execute, certify or change state."""
        from simple_ar.app.task_view import read_task_view
        result = self.session.status_snapshot()
        from simple_ar.core.feedback import FeedbackSubmissions
        result['feedback_submissions'] = FeedbackSubmissions(self.session.store).receipts(self.state)
        result['task_view'] = read_task_view(self.session.store.root,
            read_record=self.session.store.read_json, ledger=self.ledger,
            native_recipe=self.RECIPE)
        result['retained_work'] = self._retained_work()
        result['delivery_version_scope'] = ('Only captured terminal deliveries have restorable bytes; '
            'missing historical versions are not reconstructed from current files.')
        from simple_ar.tools.delivery_versions import DeliveryVersions
        try:
            versions = DeliveryVersions(self.session.store.root)
            result['delivery_versions'] = [{
                **versions.describe(number, v), 'reason': v.get('reason'),
                'files': {locator: {'source': 'records/' + row['blob'].removeprefix('attempts/'),
                    'source_sha256': row['sha256'], 'size_bytes': row['size_bytes']}
                    for locator, row in v['files'].items()}} for number, v in
                enumerate(versions.versions(), 1)]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result['delivery_versions'] = []
            result['delivery_versions_error'] = str(exc)
        return result
