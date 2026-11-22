"""Working-memory checkpointing and journal compaction mixin for ToolApplication."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from simple_ar.app.tool_contracts import _REFERENCED_CONTEXT_PROTOCOLS
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.core.working_context import RECEIPT_SUPPLY_POLICIES, TURN_SUPPLY_POLICY
from simple_ar.integrations.llm import estimate_tokens
from simple_ar.tools.specs import GOAL_WORK_METHOD, NATIVE_WORK_METHODS


class _ToolMemoryMixin:
    def _memory_operations(self, number: int) -> dict:
        turn = self.state['turns'][number]
        reply = self.session.store.read_json(turn['reply'])
        calls = {call['id']:call for call in reply.get('tool_calls',[])}
        if not turn['tools'] and self.state['config'].get('memory_protocol') == 'journal.v1':
            return {'records/'+turn['reply'].removeprefix('attempts/'): {}}
        return {'records/'+item['result'].removeprefix('attempts/'):
                self._recorded_arguments(calls[item['id']]) for item in turn['tools']}

    def _working_memory(self, *, observations: list[dict] | None = None,
                        decision_views: dict | None = None) -> dict:
        """Working understanding from the original journal, including prior feedback.

        This is model-authored working understanding, never another fact store.
        """
        from simple_ar.app.work_memory import summary_coverage
        from simple_ar.app.professional_conversation import ProfessionalConversation
        memory = {'notes':[], 'through_turn':0}
        if self.state['config'].get('memory_protocol') == 'journal.v1':
            memory['compacted_records'] = set()
            bind_checkpoint_notes = (self.operations.checkpoint_note_binder() if observations is None else
                                     self.operations.checkpoint_note_binder(observations))
            # The existing attempt lineage orders closed work, including a
            # checkpoint whose following decision failed. There is no second
            # mutable memory state and a retry does not buy the summary again.
            notes = {}
            for turn in self.state['turns']:
                for item in turn['tools']:
                    notes.setdefault(Path(item['result']).parent.name, []).append(item['result'])
            for attempt in (self.session.attempt_lineage() if self.session.manifest.current_attempt else ()):
                if attempt.status not in {'completed', 'failed'}:
                    continue
                store = ArtifactStore(self.session.store.root/'attempts'/attempt.attempt_id)
                if attempt.capability == 'native_turn' and any(store.exists(path) for path in
                        ('memory_snapshot.json','memory_failure.json')):
                    from simple_ar.app.work_memory import MEMORY_PURPOSES
                    charges = [entry for entry in self.ledger.entries if entry.attempt_id == attempt.attempt_id
                        and (entry.logical_call_id == 'working-memory-'+attempt.attempt_id
                             or entry.purpose in MEMORY_PURPOSES)]
                    # Unknown/inflight work is never adopted. Its reservations
                    # still constrain native work on the original ledger.
                    if charges and all(entry.status in {'settled','released'} for entry in charges):
                        self._memory_artifacts(store, attempt.attempt_id, bind_notes=bind_checkpoint_notes)
                        memory = ProfessionalConversation.adopt(previous=memory,
                            snapshot=store.read_json('memory_snapshot.json') if store.exists('memory_snapshot.json') else None,
                            failure=store.read_json('memory_failure.json') if store.exists('memory_failure.json') else None,
                            view=store.read_json('memory_input.json'), operations_for_turn=self._memory_operations,
                            archive=self.state['config'].get('work_method') in NATIVE_WORK_METHODS)
                for path in notes.get(attempt.attempt_id, []):
                    if self.state['config'].get('work_method') == GOAL_WORK_METHOD:
                        continue  # Manual agendas remain history; only knowledge checkpoints supply notes.
                    result = self._decision_record(path, decision_views)
                    data = result.get('data') or {}
                    if (result.get('tool_name') == 'update_work_plan' and result.get('status') == 'ok'
                            and isinstance(data.get('notes'), list)):
                        # Explicit notes replace the understanding, not paid
                        # checkpoint inputs. Coverage still reopens originals.
                        purchases = ({key: memory[key] for key in
                            ('attempted_records', 'attempted_through_turn', 'archived_records') if key in memory
                                and (key != 'archived_records' or self.state['config'].get('work_method') in NATIVE_WORK_METHODS)}
                            if self.state['config'].get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES else {})
                        memory = {'notes':data['notes'], 'through_turn':0,
                            'compacted_records':set(), **purchases}
            return {**memory,'notes':self.operations.project_notes(memory['notes'])}
        for turn in self.state['turns']:
            # Saved owner-note contracts retain their original adoption rules.
            checkpoint = (self._checkpoint_for_turn(turn)
                if self.state['config'].get('context_protocol') not in _REFERENCED_CONTEXT_PROTOCOLS else None)
            if checkpoint is not None:
                store = ArtifactStore(self.session.store.root / Path(turn['reply']).parent)
                coverage = summary_coverage(previous=memory,snapshot=checkpoint,
                    view=store.read_json('memory_input.json'),operations_for_turn=self._memory_operations)
                memory = {'notes':checkpoint['notes'],'through_turn':coverage}
            for item in turn['tools']:
                result = self._decision_record(item['result'], decision_views)
                data = result.get('data') or {}
                if (result.get('tool_name') == 'update_work_plan' and result.get('status') == 'ok'
                        and isinstance(data.get('notes'), list)):
                    memory = {'notes':data['notes'], 'through_turn':0}
        return {**memory,'notes':self.operations.project_notes(memory['notes'])}

    def _checkpoint_for_turn(self, turn: dict) -> dict | None:
        return self._committed_memory_record(turn,'memory_snapshot.json')

    def _committed_memory_record(self, turn: dict, path: str) -> dict | None:
        store = ArtifactStore(self.session.store.root / Path(turn['reply']).parent)
        if not store.exists(path):
            return None
        manifest, capability = store.read_attempt_manifest(), store.read_capability_result()
        if (manifest.status != 'completed' or capability.status != 'completed'
                or not any(ref.path==path
                    and ref in capability.artifacts for ref in manifest.outputs)):
            return None
        refs = self._memory_artifacts(store, manifest.attempt_id)
        if any(ref not in capability.artifacts or ref not in manifest.outputs for ref in refs):
            raise ValueError('Retained checkpoint records are not registered attempt outputs')
        return store.read_json(path)

    def _memory_artifacts(self, store: ArtifactStore, attempt_id: str, *, bind_notes=None) -> tuple:
        if not any(store.exists(path) for path in ('memory_input.json','memory_response.json',
                'memory_snapshot.json','memory_failure.json')):
            return ()
        if self.operations is None:
            raise ValueError('Working-memory records require the operation-composed loop')
        view = store.read_json('memory_input.json')
        journal = self.state['config'].get('memory_protocol') == 'journal.v1'
        if journal and view.get('requests') != self.state['requests'][:len(view.get('requests', []))]:
            raise ValueError('Checkpoint requests disagree with the original request journal')
        through = view.get('archive_through_turn')
        if through is not None and (type(through) is not int or not 0 < through <= len(self.state['turns'])):
            raise ValueError('Checkpoint archive input scope exceeds the original journal')
        from simple_ar.app.work_memory import checkpoint_artifacts, validate_checkpoint_sources
        if (journal and (self.state['config'].get('native_supply_policy') == TURN_SUPPLY_POLICY)
                != (view.get('native_supply_policy') == TURN_SUPPLY_POLICY)):
            raise ValueError('Checkpoint must retain this task\'s saved supply contract')

        def source(row):
            # Resolve only this task's original turn and its accepted call.
            turn = row['turn']
            if type(turn) is not int or not 0 <= turn < len(self.state['turns']):
                return None
            original = self.state['turns'][turn]
            matching = next((item for item in original['tools']
                if 'records/'+item['result'].removeprefix('attempts/') == row['record']), None)
            if matching is None and not (journal and not original['tools']
                    and row['record'] == 'records/'+original['reply'].removeprefix('attempts/')):
                return None
            bound = {'turn':turn, 'request_count':original.get('request_count', 1)}
            if view.get('native_supply_policy') != TURN_SUPPLY_POLICY:
                return bound
            reply = self.session.store.read_json(original['reply'])
            if matching is None:
                return {**bound, 'decision':reply, 'call_id':None, 'arguments':{},
                        'result':{'tool_name':'assistant_reply', 'data':reply}}
            if matching.get('review_result'):
                raise ValueError('A separate review needs its own current owner binding')
            result = self.session.store.read_json(matching['result'])
            if any(key in (result.get('metadata') or {}) for key in ('delivery_review', 'inspection_binding')):
                raise ValueError('An independent opinion needs its own current owner binding')
            call = next((call for call in reply.get('tool_calls', []) if call['id'] == matching['id']), None)
            if call is None:
                raise ValueError('Checkpoint observation has no original tool call')
            return {**bound, 'decision':reply, 'call_id':matching['id'],
                    'arguments':self._recorded_arguments(call), 'result':result}

        validate_checkpoint_sources(view, source=source, owner='native', journal=journal)
        return checkpoint_artifacts(store=store,ledger=self.ledger,attempt_id=attempt_id,
            session_id=self.session.manifest.session_id,
            bind_notes=self.operations.bind_notes if bind_notes is None else bind_notes)

    def _retained_checkpoint_charge(self, entry: Any) -> bool:
        """Allow explicit recovery past optional work, never adoption or free usage."""
        if self.state['config'].get('memory_protocol') != 'journal.v1' or entry.status != 'unknown':
            return False
        from simple_ar.app.work_memory import checkpoint_entries, MEMORY_PURPOSES
        if entry.purpose not in MEMORY_PURPOSES or self._failed_model_attempt(
                {'kind':'model','attempt':entry.attempt_id},current_only=False) is None:
            return False
        store = ArtifactStore(self.session.store.root/'attempts'/entry.attempt_id)
        if not store.exists('memory_input.json'):
            return False
        try:
            return entry in checkpoint_entries(self.ledger,entry.attempt_id,
                self.session.manifest.session_id,allow_bounded_unknown=True)
        except ValueError:
            return False

    def _compact_work(self, context: Any, client: Any, cutoff: int, *, decision_tokens: int = 0,
                      additional_protection: bool = False, selected_turns: list | None = None,
                      economic_floor: int | None = None) -> tuple:
        """Checkpoint old closed work through the same model attempt and ledger."""
        if not self._can_request(2) or not cutoff:
            return ()
        previous = self._working_memory()
        journal = self.state['config'].get('memory_protocol') == 'journal.v1'
        delivery_feedback = self._delivery_feedback()
        failure = (None if journal else next((record for turn in reversed(self.state['turns'])
            if (record:=self._committed_memory_record(turn,'memory_failure.json')) is not None),None))
        if failure is not None and failure['attempted_through_turn'] > previous['through_turn']:
            previous['attempted_through_turn'] = failure['attempted_through_turn']
        rows = []
        complete_turns = journal and self.state['config'].get('native_supply_policy') == TURN_SUPPLY_POLICY
        if complete_turns:
            from simple_ar.app.work_memory import observation_currentness, observation_digest
        for number in range(previous['through_turn'],cutoff):
            if selected_turns is not None and number not in selected_turns:
                continue
            turn = self.state['turns'][number]
            if turn.get('remaining') or not journal and not turn['tools']:
                break
            reply = self.session.store.read_json(turn['reply'])
            calls = {call['id']:call for call in reply.get('tool_calls',[])}
            if not turn['tools']:
                record = 'records/'+turn['reply'].removeprefix('attempts/')
                if record not in previous.get('compacted_records', ()):
                    rows.append({'turn':number, 'record':record, 'arguments':{}, 'records':[],
                        'request_count':turn.get('request_count',1),
                        'result':{'tool_name':'assistant_reply','data':reply},
                        **({'decision':reply, 'call_id':None, 'source_currentness':'historical'} if complete_turns else {})})
            for item in turn['tools']:
                record = 'records/'+item['result'].removeprefix('attempts/')
                if record in previous.get('compacted_records', ()):
                    continue
                result = self.session.store.read_json(item['result'])
                presented = self._coordination_result(result,record,
                    delivery_feedback=None if journal else delivery_feedback, review_record=item.get('review_result'))
                if complete_turns and (item.get('review_result')
                        or (result.get('metadata') or {}).get('delivery_review')
                        or (result.get('metadata') or {}).get('inspection_binding')):
                    # An independent opinion needs its own current owner binding.
                    continue
                rows.append({'turn':number,'record':record,
                    'arguments':self._recorded_arguments(calls[item['id']]),'result':presented,
                    **({'decision':reply, 'original':result,
                        'call_id':item['id'],
                        'complete':observation_digest(presented) == observation_digest(result),
                        'source_currentness':observation_currentness(result, absolute=True)}
                       if complete_turns else {}),
                    **({'request_count':turn.get('request_count',1)} if journal else {})})
        from simple_ar.app.work_memory import compact_work
        _, refs = compact_work(store=context.store,client=client,requests=self.state['requests'],
            previous=previous,rows=rows,bind_notes=self.operations.bind_notes,
            note_contract='knowledge.v1' if self.state['config'].get('work_method') == GOAL_WORK_METHOD else None,
            estimate_tokens=estimate_tokens,attempt_id=context.attempt.attempt_id,
            **({'required_decision_tokens':decision_tokens,
                'native_supply_policy':self.state['config'].get('native_supply_policy'),
                'additional_protection':additional_protection,
                **({'economic_floor':economic_floor} if economic_floor is not None
                    and self.state['config'].get('context_checkpoint_policy') in {'economic.v2', 'economic.v3'} else {})}
               if journal else {}))
        return refs
