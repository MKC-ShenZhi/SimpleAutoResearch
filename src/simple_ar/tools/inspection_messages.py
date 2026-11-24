"""Working-view and conversation message projection for InspectionToolGateway."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.core.working_context import RECEIPT_SUPPLY_POLICIES, TURN_SUPPLY_POLICY
from simple_ar.integrations.llm import (
    LLMSettings,
    estimate_native_input_tokens,
    estimate_tokens,
)
from simple_ar.tools.inspection_contracts import (
    InspectionRequest,
    _BINARY,
    _InspectionHistory,
    _PRESENTED_EVIDENCE_POLICIES,
    _PRIORITY_SOURCE_POLICIES,
    _ProvidedInspectionHistory,
    _RetainedInspectionHistory,
    _SHARED_EXECUTION_PROTOCOLS,
    _WORK_MATERIAL_POLICIES,
)
from simple_ar.tools.specs import GOAL_WORK_METHOD, NATIVE_WORK_METHODS, ToolResult
from simple_ar.tools.workspace import FileWindow


class _InspectionMessagesMixin:
    """Message construction and round-preparation methods for InspectionToolGateway."""

    def _view(self, saved: dict[str, Any], request: InspectionRequest, *, source_result: bool = False) -> dict[str, Any]:
        prior = saved['prior_required']
        if saved.get('prior_reference_policy') == 'short.v1':
            prior = [{**row, 'id':name} for row, name in zip(prior, self._prior_references(saved))]
        view = {'goal':request.goal,
            'prior_required':prior,
            'prior_scope':('Reassess these concerns within the explicitly requested target scope and evidence; '
                if saved.get('coverage_policy') == 'offered.v2' else
                'Reassess these concerns against the current full targets and evidence; ') +
                          'earlier opinions are not source evidence or adoption decisions.'}
        if 'user_requests' in saved:
            view['user_requests'] = saved['user_requests']
            view['user_request_scope'] = (
                'Exact original task requests and feedback in journal order, not source evidence or '
                'permission. The separate goal selects this inspection scope. Use relevant user '
                'requirements to judge the selected targets; a narrow check does not certify the '
                'whole task or require review of unrelated work.')
        if 'delivery_context' in saved:
            delivery = saved['delivery_context']
            candidate = delivery.get('purpose') == 'candidate'
            view['user_request_scope'] = (('Exact original requests provide context for the selected '
                'candidate and its claim, not a demand to certify the full task in this inspection. '
                if candidate else
                'Exact original task requests and feedback, not evidence or permission. '
                'This owner requests the complete proposed-delivery review; its fixed goal '
                'does not narrow this to agreement among generated files.'))
            view['proposed_delivery'] = {
                **({'claim':{key:value for key,value in delivery['claim'].items() if key != 'artifacts'}} if self._delivery_scope(saved) else
                    {'manuscript':delivery['manuscript'], 'formats':delivery['formats']}),
                'artifacts': ([self._review_artifact_name(path) for path in delivery['artifacts']]
                    if saved.get('inspection_evidence_policy') == 'presented.v6' else list(delivery['artifacts'])),
                'scope': (('Selected candidate files, not a complete delivery. Check the submitted claim '
                    'and relevant original evidence; missing unrelated outputs are not defects of this local check. '
                    if candidate else 'Owner-bound actual delivery inventory, not proof of correctness. '
                    'Review the relevant original requests across this work, including methods, '
                    'supporting source/code/results and interpretations. ') +
                    'Generated files agreeing with one another do not establish correctness. Locate and read '
                    'original evidence as needed; disclose unsupported conclusions or unfinished checks.')}
            if saved.get('inspection_evidence_policy') == 'presented.v6':
                view['proposed_delivery']['scope'] += ('' if candidate else
                    ' Only these inventory files are delivered; other accessible files are supporting '
                    'material. Judge each requested outcome against this inventory and the delivery text.')
            if self._delivery_scope(saved) and saved.get('finding_policy') != 'linked.v1':
                view['prior_work_opinions'] = [self.feedback_view(row)
                    for row in saved.get('prior_work_opinions', [])]
            if self._applicable_scope(saved):
                view['proposed_delivery']['scope_policy'] = saved['delivery_context']['scope_policy']
                if 'revision_sources' in saved:
                    view['proposed_delivery']['revision'] = {
                        'changed': saved['revision_sources'],
                        'added': saved.get('introduced_artifacts', []),
                        'removed': saved.get('removed_artifacts', []),
                        'scope': 'Investigate new or changed claims and the sources affected by this revision. '
                            'Retained original observations can support unchanged claims when applicable. '
                            'This inventory comparison does not transfer an earlier verdict.'}
                history = saved.get('source_history', {})
                view['source_history'] = {'indexed_paths': len(history.get('entries', [])),
                    'scope': history.get('scope', 'No parent read locators available; use authorized roots.'),
                    'presentation': 'Only source_history descriptors actually selected in working_view '
                        'are supplied. indexed_paths counts the retained index, not supplied or current '
                        'sources. Capacity omissions and access withdrawal can hide descriptors; '
                        'the projection selected_records records each considered descriptor status.'}
                if saved.get('parent_process_receipts'):
                    view['parent_process_receipts'] = {
                        'paths': list(saved['parent_process_receipts']),
                        'scope': 'Previously recorded parent executions, not independent checks or proof '
                            'of current inputs. Only the receipt text actually supplied in working_view '
                            'is observed; inspect its command, result and input declarations before use.'}
                if saved.get('parent_process_history'):
                    view['parent_process_history'] = {
                        'indexed_executions': len(saved['parent_process_history']),
                        'scope': 'Selected working_view entries navigate recorded parent executions. '
                            'Read a receipt to inspect its actual result; the index is not an independent check.'}
        source_cache: dict = {}
        source_views: dict[str, dict] = {}
        if saved.get('source_provenance') in {'versions.v1', 'versions.v2'}:
            role_versions = {role: {row['path']: row['sha256'] for row in saved[role]
                if row.get('readable') and row.get('path') and row.get('sha256')}
                for role in ('targets', 'evidence')}
            targets, evidence = role_versions['targets'], role_versions['evidence']
            shared = None
            if all(path not in targets or targets[path] == digest for path, digest in evidence.items()):
                introduced = {path: saved['delivery_context']['artifacts'][path]
                    for path in saved.get('introduced_artifacts', ())}
                versions = {**introduced, **targets, **evidence}
                try:
                    shared = (self.workspace.artifact_sources(versions,
                        records=saved.get('source_invocations', []), cache=source_cache) if versions else {})
                except (ValueError, OSError, KeyError, TypeError, RuntimeError):
                    pass  # Keep the previous per-role availability boundary below.
            if shared is not None:
                source_views = {'targets': shared, 'evidence': shared}
                copies = []
                delivery_versions = saved.get('delivery_context', {}).get('artifacts', {})
                # Copy provenance is compact and version-bound. It remains
                # relevant for unchanged deliverables already observed in a
                # revision, even when their old bodies are deliberately omitted.
                for path, digest in versions.items():
                    if delivery_versions.get(path) != digest:
                        continue
                    graph = shared[path].get('lineage')
                    if graph is None:
                        continue
                    nodes = {node['id']: node for node in graph['nodes']}
                    root = nodes[graph['root']]
                    if root['state'] != 'recorded':
                        continue
                    for edge in root['edges']:
                        original = nodes[edge['node']]
                        origin = str(original['path'])
                        if edge['kind'] == 'copy' and origin != path and original['sha256'] == digest:
                            copies.append({'artifact': path, 'original_at_copy': origin,
                                'sha256': digest, 'record': root['owner']['record']})
                if copies:
                    view['proposed_delivery']['recorded_copies'] = {
                        'files': copies,
                        'scope': 'A bound copy proves byte identity with the original at copy time. '
                            'It supplies neither the file body nor a scientific judgment.'}
            else:
                for role, versions in role_versions.items():
                    try:
                        source_views[role] = (self.workspace.artifact_sources(versions,
                            records=saved.get('source_invocations', []), cache=source_cache) if versions else {})
                    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
                        source_views[role] = {}
        for role in ('targets','evidence'):
            view[role] = [{'subject':row['subject'], 'path':row.get('path'), 'complete':row['complete'],
                'limitation':row.get('limitation',''),
                'text_windows':[{'offset_bytes':window['offset_bytes'],
                    'next_offset_bytes':window['review_next_offset_bytes'], 'size_bytes':window['size_bytes'],
                    'text':window.get('review_text',window['text'])} for window in row['windows']
                    if source_result or not window.get('source_reference')]
                    if row.get('readable') and (self.protocol or role=='targets' or row['independent'])
                        and (source_result or role == 'targets' or saved.get('memory_protocol') != 'journal.v1') else []}
                for row in saved[role]]
            if saved.get('inspection_evidence_policy') == 'presented.v6' and 'delivery_context' in saved:
                delivered = saved['delivery_context']['artifacts']
                for original, visible in zip(saved[role], view[role]):
                    visible['delivery_role'] = ('delivered' if original.get('path') in delivered
                                                else 'supporting_source_only')
            if saved.get('source_provenance') in {'versions.v1', 'versions.v2'}:
                sources = source_views[role]
                for original, visible in zip(saved[role], view[role]):
                    source = sources.get(original.get('path'), {})
                    lineage = source.get('lineage', {})
                    root = next((node for node in lineage.get('nodes', [])
                                 if node['id'] == lineage.get('root')), {})
                    visible['provenance'] = {'status': source.get('status', 'unknown'),
                        **({'ancestry_states': root['ancestry_states']}
                           if root.get('ancestry_states') else {}),
                        **({'tool_name': source['tool_name'], 'record': source['record']}
                           if source.get('status') == 'recorded' else {}),
                        'scope': ('Only this exact output version is associated with the recorded operation; '
                                  'a copy inherits an association only through a recorded copy. '
                                  'Unknown content is ordinary material, not a prior inspection result.')}
                    change = next((item for item in saved.get('parent_artifact_changes', [])
                                   if item['result'] == source.get('record')), None)
                    if change is not None:
                        visible['provenance'].update(call_record=change['call_record'], call_id=change['call_id'])
                        visible['provenance']['scope'] += (
                            ' Read the call record and result record for the original operation; '
                            'neither is an independent check.')
            if self.progressive:
                all_rows = [*saved['targets'], *saved['evidence']]
                for index, (original, visible) in enumerate(zip(saved[role], view[role])):
                    visible['sha256'] = original.get('sha256')
                    visible['version_scope'] = 'Owner-computed current full-file digest, not a scientific opinion'
                    visible['observed_ranges'] = [[w['offset_bytes'], w['review_next_offset_bytes']]
                        for w in original['windows']]
                    if role == 'targets':
                        visible['complete'] = self._complete_target(original, all_rows)
                        if visible['complete']:
                            visible['limitation'] = ''
                        scope = self._requested_window(saved, original, index)
                        if scope is not None:
                            visible['requested_scope'] = scope
                            visible['scope_description'] = ('The requested text window only; '
                                'complete refers to the whole file, not this narrower request.')
                    if not source_result and original['subject'] not in saved.get('initial_subjects', []):
                        visible['text_windows'] = []
        view['authorized_roots'] = {name:str(path) for name,path in self.workspace.mounts.items()}
        if not self.adaptive:
            view['source_rounds_remaining'] = 6-len(saved.get('source_rounds',[]))
        return view

    def _working_messages(self, saved: dict, request: InspectionRequest, history: list | None,
                          offered: list, client: Any, *, finish: dict | None = None,
                          validate_capacity: bool = True, prioritize_delivery: bool = False) -> tuple[list, dict]:
        """Project actual turns; admission uses the owner's closed journal.

        ``None`` requests that journal for a capacity probe. An empty live prefix
        cannot represent turns already handed off to working notes.
        """
        from simple_ar.core.working_context import (
            current_material_paths, fit_working_context, input_capacity, processed_journal_turns)

        policy = saved.get('inspection_context_policy')
        if 'inspection_context_policy' in saved and policy != 'retained.v1':
            raise ValueError('Unknown inspection context policy')
        retained = self.adaptive and policy == 'retained.v1'
        continuity = retained and saved.get('memory_protocol') == 'journal.v1'
        checkpoint_policy = saved.get('context_checkpoint_policy')
        if checkpoint_policy not in (None, 'economic.v3'):
            raise ValueError('Unknown saved inspection checkpoint policy')
        economic = checkpoint_policy == 'economic.v3'
        if saved.get('work_lifecycle') not in (None, 'integrated.v3'):
            raise ValueError('Unknown saved inspection work lifecycle')
        if economic and not continuity:
            raise ValueError('Inspection checkpoint policy requires its saved journal')

        def version(path: str) -> str | None:
            # artifact_hashes resolves current mount/private-path authority before
            # hashing. A receipt digest alone cannot renew access or its version.
            try:
                return self.workspace.artifact_hashes([path])[path]
            except (OSError, ValueError):
                return None

        target = getattr(getattr(client, '_settings', None), 'context_input_tokens', LLMSettings.context_input_tokens)
        if type(target) is not int or target <= 0:
            target = LLMSettings.context_input_tokens
        model_target = target
        if saved.get('memory_protocol') == 'journal.v1' and client._budget_ledger is not None:
            remaining = client._budget_ledger.remaining('total_tokens')
            target = input_capacity(target, remaining, client.effective_output_limit() or 0)
        base = self._view(saved, request)
        if saved.get('execution_protocol') == 'shared.v2':
            ledger = client._budget_ledger
            capabilities = {row['function']['name'] for row in offered}
            base['available_evidence'] = {
                'rendered_image_observation': 'inspect_images' in capabilities,
                'new_execution': 'run_process' in capabilities,
                'scope': ('Judge each requirement with evidence that can actually be obtained. '
                    'Use recorded observations when applicable. If required evidence cannot be obtained, '
                    'return the supported findings and the explicit gap. Text, metadata and a successful render '
                    'do not establish visual quality; only pursue a measurement if it can answer the specific question.')}
            base['resource_handoff'] = {
                'spendable_after_caller_protection': {key: ledger.remaining(key)
                    for key in ledger.limits},
                'unknown_usage': list(ledger.unknown_dimensions()),
                'phase': ('opinion_only' if all(row['function']['name'] == 'record_inspection'
                    for row in offered) else 'investigation'),
                'scope': 'Shared ledger capacity after the caller has withheld subsequent work. '
                    'This is neither another allowance nor a promised number of useful checks. '
                    'Further investigation also protects a measured final opinion. Return '
                    'established required findings promptly; disclose unfinished necessary scope.'}
        applicable = self._applicable_scope(saved)
        retained_inputs = (self._retained_inputs(saved)
            if saved.get('inspection_material_policy') == 'operations.v3' else None)
        execution_facts = (self._execution_facts(saved, retained=retained_inputs)
            if applicable and saved.get('inspection_material_policy') == 'operations.v3' else None)
        if saved.get('work_method') in NATIVE_WORK_METHODS and self._delivery_scope(saved):
            if (saved.get('inspection_evidence_policy') == 'presented.v6'
                    and saved.get('inspection_material_policy') == 'operations.v3'):
                facts = (execution_facts if execution_facts is not None else
                    self._execution_facts(saved, retained=retained_inputs))
                names = self._review_execution_names(facts)
                visible = {fact['process_receipt'] if names.get(fact.get('process_receipt')) == record
                           else fact['source_record']:record for record, fact in facts.items()}
                base['executed_checks'] = sorted(visible)
                base['executed_check_details'] = {}
                for name in base['executed_checks']:
                    fact = facts[visible[name]]
                    base['executed_check_details'][name] = {'cwd': fact.get('cwd', 'workspace'),
                        'argv_preview': self._command_preview(fact.get('argv', []))}
                if visible:
                    base['executed_check_scope'] = ('Commands are previews of the accepted executions, '
                        'not their outputs or proof of a claim. Read the original action for details.')
            else:
                base['executed_checks'] = sorted(execution_facts if execution_facts is not None
                    else self._executed_check_records(saved))
        if saved.get('inspection_material_policy') == 'operations.v3':
            base['retained_investigations'] = [{'inspection':item['identity'],
                'source_rounds':len(item.get('source_rounds', []))} for item in retained_inputs]
            base['retained_scope'] = ('Original independent work, including failed opinions. '
                'Use applicable observations; decide current requests and claims anew. Retained executions '
                'are historical, not fresh runs. read_history accepts one of these inspection IDs. '
                'execution_records may support derived as well as executed judgments; only the displayed '
                'executed_checks have currently eligible input bindings. A failed opinion is not a pass.')
            if 'source_history' in base:
                base['source_history']['scope'] = base['source_history']['scope'].replace(
                    'read_history addresses only this inspection, not the parent task.',
                    'read_history addresses this inspection and supplied retained inspection IDs; '
                    'other parent records require ordinary authorized reads.')
        if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS:
            # A primary target is a current material, not permanently pinned
            # prompt text. Its complete read/version proof stays in the source
            # journal; the common selector supplies useful original windows.
            for row in base['targets']:
                row['text_windows'] = []
            if self._offered_coverage(saved):
                base['read_range_scope'] = ('complete and observed_ranges describe retained file reads, '
                    'not what this model received. Only text_windows, working_view observations and '
                    'intact tool replies supply original text; archived notes do not.')
        pinned = {(row['path'], row['sha256']): row for role in ('targets', 'evidence')
            for row in base[role] if row.get('text_windows')}
        blocks, current, read_turns = [], [], {}
        if history is None:
            # Preserve original round positions, including unfinished gaps. Only
            # verified closed receipts may enter an admission projection.
            for number in range(len(saved.get('source_rounds', []))) if continuity else ():
                trace = self._closed_source_round(saved, number)
                blocks.append([] if trace is None else [trace['response'], *[
                    {'role': 'tool', 'tool_call_id': row['call_id'],
                     'content': json.dumps(row['result'], ensure_ascii=False)} for row in trace['results']]])
        else:
            for message in history[2:]:
                if message['role'] == 'assistant' and current:
                    blocks.append(current)
                    current = []
                current.append(message)
            if current:
                blocks.append(current)
        for number, block in enumerate(blocks):
            for index, message in enumerate(block):
                if message['role'] != 'tool':
                    continue
                result = json.loads(message['content'])
                data = result.get('data', {})
                if result.get('tool_name') == 'read_file' and result.get('status') == 'ok':
                    read_turns[data.get('subject')] = number
                owner = pinned.get((data.get('path'), data.get('sha256')))
                if (result.get('tool_name') == 'read_file' and result.get('status') == 'ok'
                        and owner and data.get('subject') == owner.get('subject') and data.get('text_windows')
                        and all(window in owner['text_windows'] for window in data['text_windows'])):
                    data = {key: value for key, value in data.items() if key != 'text_windows'}
                    data['text_window_owner'] = owner['subject']
                    block[index] = {**message, 'content': json.dumps({**result, 'data': data}, ensure_ascii=False)}
        # Supplementary evidence uses the same bounded material selector as
        # ordinary work. Retiring a native turn must not retire its only usable
        # source body while unused input capacity remains. Original read IDs,
        # decoded ranges and receipts remain the authority for adoption.
        memory, notes, original_observations, handoff_size = None, [], None, None
        if continuity:
            from simple_ar.app.work_memory import note_currentness
            memory, observations, journal_rows = self._conversation_material(
                saved, execution_facts=execution_facts, retained_inputs=retained_inputs)
            for note in memory['notes']:
                current = note_currentness(note,
                    versions_match=lambda versions: all(version(path) == digest for path, digest in versions.items()))
                notes.append({**note, 'current':current,
                    **({'version_scope':'Source support is incomplete or indirect; reassess before reuse'}
                       if current is None else {})})
            if saved.get('initial_material_memory') == 'provided.v1':
                original_observations = list(observations)
                observations = self._handoff_provided_sources(observations, journal_rows, notes,
                    memory['compacted_records'], self._path(saved['identity'], 'input'))
                handoff_size = len(observations)
        else:
            observations = [{'record':self._path(saved['identity'], 'input'),
                'chunk_id':f"{row['subject']}:{index}", 'subject':row['subject'],
                'path':row['path'], 'sha256':row['sha256'], 'kind':'source_text',
                **({'role':'historical_observation'} if retained else {}),
                'turn':read_turns.get(row['subject']), 'offset_bytes':window['offset_bytes'],
                'next_offset_bytes':window['review_next_offset_bytes'],
                'size_bytes':window['size_bytes'], 'text':window.get('review_text',window['text'])}
                for row in saved['evidence'] if row.get('readable')
                for index, window in enumerate(row['windows'])
                if row['subject'] not in saved.get('initial_subjects', []) or window.get('source_reference')]
        native_observations = (self._native_source_supply(saved, observations, blocks)
            if applicable or saved.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES else None)
        if applicable:
            # Optional navigation competes for the same input budget as other
            # observations. No parent body is adopted or marked source_text.
            accessible = 0
            for entry in saved.get('source_history', {}).get('entries', []):
                try:
                    self.workspace.resolve(entry['path'])
                    records = [row for row in entry['records']
                        if self._accessible_history_record(row)]
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                accessible += 1
                # The saved index owns receipt digests and full requests. The
                # model needs paths and windows for navigation, not another
                # copy of every path and digest inside each receipt descriptor.
                navigation = []
                for row in records:
                    window = {key: row['request'][key] for key in
                        ('start_line', 'max_lines', 'offset_bytes', 'max_bytes')
                        if key in row.get('request', {})}
                    item = {key: row[key] for key in ('record', 'input_index') if key in row}
                    if window:
                        item['window'] = window
                    if item not in navigation:
                        navigation.append(item)
                observations.append({'record':self._path(saved['identity'], 'input'),
                    'field':'source_history', 'chunk_id':'source-history:' + entry['path'],
                    'kind':'operation_result', 'role':'source_navigation',
                    'text':json.dumps({'path':entry['path'], 'requested_byte_ranges':entry.get('requested_byte_ranges', []),
                        'line_windows':entry.get('line_windows', []), 'records':navigation},
                        ensure_ascii=False)})
            base['source_history'].update(accessible_paths=accessible,
                inaccessible_paths=base['source_history']['indexed_paths'] - accessible)
            for entry in saved.get('parent_process_history', []):
                observations.append({'record':self._path(saved['identity'], 'input'),
                    'field':'parent_process_history', 'chunk_id':'parent-process:' + entry['receipt'],
                    'kind':'operation_result', 'role':'source_navigation',
                    'text':json.dumps({'receipt':entry['receipt'], 'cwd':entry.get('cwd'),
                        'returncode':entry.get('returncode'), 'argv_preview':self._command_preview(entry.get('argv', []))},
                        ensure_ascii=False)})
        if 'user_requests' in saved:
            # The shared projector pins exact user text; the coordinator's local
            # inspection goal stays a separately labelled scope in base context.
            base.pop('user_requests', None)
        else:
            base.pop('goal', None)  # Historical input retains its original view.
        history = self._history_enabled(saved)
        if history:
            base['history_archive'] = {'total_turns':len(saved.get('source_rounds', [])), 'access':'read_history',
                'scope':'Zero-based source rounds of this inspection only. Omit action for a compact turn/action '
                    'index; pin end_turn while paging it. Select start_turn, action and field=result or arguments '
                    'to page its retained semantic value directly, without replaying offers. Byte offsets address '
                    'a canonical JSON projection, not original formatting. Round/receipt/action hashes and JSON '
                    'pointers identify the immutable originals. Historical output is not current evidence or permission.'}
            if saved.get('inspection_material_policy') == 'operations.v3':
                base['history_archive']['scope'] = base['history_archive']['scope'].replace(
                    'of this inspection only', 'of the selected inspection (default: this inspection)')
            if saved.get('initial_material_memory') == 'provided.v1':
                base['history_archive']['scope'] = base['history_archive']['scope'].replace(
                    'field=result or arguments ', 'field=result, arguments or provided ')
        base['reading_scope'] = ('Only the supplied decoded windows were observed. Retired source bodies '
            'remain in the original inspection records; ' + (
                'use read_history for original observations, including failures and unread tails. Read an '
                'authorized source path/range only when new or current bytes are needed. ' if history else
                'read an authorized original path/range again when its text is needed. ') +
            'Full-file hashes are computed by the read owner. Source opinions '
            'and self-reported manifests do not replace actual source versions or scientific reasoning.')
        system = self._system(saved)
        from simple_ar.app.professional_conversation import continuation_blocks
        blocks = continuation_blocks(blocks, policy=saved.get('native_history_policy'),
            thinking_mode=getattr(getattr(client, '_settings', None), 'thinking_mode', ''))
        selected_records = ([row['record'] for row in journal_rows
            if row['result'].get('tool_name') in {'run_process', 'inspect_images'}]
            if continuity and saved.get('inspection_material_policy') in _WORK_MATERIAL_POLICIES else [])
        if execution_facts is not None:
            if saved.get('source_priming_policy') == 'priority.v4':
                selected_records.extend(self._relevant_execution_sources(saved, execution_facts))
            else:
                pending = {path for finding in saved['prior_required'] for path in finding['pending_subjects']}
                selected_records.extend(fact['source_record'] for fact in execution_facts.values()
                    if fact['inspection'] != saved['identity']
                    or pending.intersection(fact['dependencies']))
        fit_options = dict(
            measure_input=lambda value: estimate_native_input_tokens(client, value, offered),
            context_authority=saved.get('context_authority'),
            user_requests=saved.get('user_requests', [request.goal]),system_text=system,
            base_context=base,turn_blocks=blocks,observations=observations,
            block_request_counts=([len(saved['user_requests'])] * len(blocks)
                if 'user_requests' in saved else None),
            schema_tokens=estimate_tokens(json.dumps(offered,ensure_ascii=False)),
            context_token_target=target,
            steady_working_target=(min(target, max(36000, target // 3)) if economic else None),
            estimate_tokens=estimate_tokens,
            priority_source_paths=(
                [*self._delivery_source_paths(saved,
                    include_artifacts=prioritize_delivery),
                 *saved.get('production_receipts', ())]
                if applicable else ()),
            material_policy='working' if economic else 'retained' if retained else 'ranked',
            selected_records=selected_records,
            native_observations=native_observations,
            preserve_material_observations=False,
            working_set_policy=(saved.get('working_set_policy') or 'recent.v4')
                if saved.get('work_lifecycle') == 'integrated.v3' else None,
            version_resolver=version if retained else None,
            **({'work_notes':notes, 'require_work_notes':True, 'fit_work_notes':True,
                'economical_checkpoint':economic,
                'economical_after_turn':memory.get('attempted_through_turn', 0),
                'sparse_handoffs':economic,
                'summarized_through_turn':min(memory['through_turn'], len(blocks)),
                'current_artifacts':sorted(current_material_paths(
                    request_count=len(saved.get('user_requests', [request.goal])),
                    selected_paths=[row['path'] for row in saved['targets']]
                        + (list(saved['delivery_context']['artifacts']) if
                            saved.get('delivery_context', {}).get('schema_version') in
                                {'delivery_review.v3'} else []))),
                'processed_turns':processed_journal_turns(journal_rows, memory['compacted_records']),
                **({'archived_turns':processed_journal_turns(journal_rows, memory.get('archived_records', ())) }
                   if economic and saved.get('work_method') in NATIVE_WORK_METHODS else {})
                } if continuity else {}),
            history_access=('read_history' if history else 'read_file at the recorded authorized source path and range'),
            trailing_messages=[finish] if finish is not None else [])
        def fit():
            fit_options['observations'] = observations
            messages, metadata = fit_working_context(**fit_options)
            if original_observations is not None and handoff_size != len(original_observations):
                supplied_notes = json.loads(messages[1]['content'])['working_view']['notes']
                visible = self._handoff_provided_sources(original_observations, journal_rows, supplied_notes,
                    memory['compacted_records'], self._path(saved['identity'], 'input'))
                if len(visible) != handoff_size:
                    # A capacity-omitted note cannot replace the original source.
                    fit_options['observations'] = [*original_observations, *observations[handoff_size:]]
                    messages, metadata = fit_working_context(**fit_options)
            return messages, metadata

        messages, metadata = fit()
        ledger = client._budget_ledger
        if (validate_capacity and ledger is not None and ledger.has_yieldable_protection
                and ledger.yield_protection_for({'llm_requests': 1,
                    'total_tokens': metadata['estimated_input_tokens'] + (client.effective_output_limit() or 0)})):
            # The retained caller exit is only a forecast. Fit the actual
            # review against the remaining ledger before sending any request.
            target = input_capacity(model_target, ledger.remaining('total_tokens'), client.effective_output_limit() or 0)
            fit_options['context_token_target'] = target
            if 'resource_handoff' in base:
                base['resource_handoff']['spendable_after_caller_protection'] = {
                    key: ledger.remaining(key) for key in ledger.limits}
            messages, metadata = fit()
        if continuity and saved.get('initial_material_memory') == 'provided.v1':
            self._schedule_provided_source_memory(metadata, journal_rows, memory, blocks,
                self._path(saved['identity'], 'input'))
        if validate_capacity and metadata['estimated_input_tokens'] > target:
            raise ValueError('Inspection working view cannot fit the available model input budget; '
                'retain the requested delivery and observations, then continue with sufficient resources '
                'or return an honest partial result')
        return messages, metadata

    @staticmethod
    def _handoff_provided_sources(observations: list[dict], rows: list[dict], notes: list[dict],
                                  compacted: set[str], initial: str) -> list[dict]:
        """Retire only supplied windows carried by an applicable cited note."""
        def source_key(value):
            return tuple(value.get(key) for key in ('subject', 'path', 'sha256',
                'offset_bytes', 'next_offset_bytes', 'size_bytes', 'text'))
        handed_off = {record for note in notes if note.get('current') is True
                      for record in note.get('records', [])} & compacted
        provided = {source_key(row['result']['data']): row['record'] for row in rows
                    if row['result'].get('tool_name') == 'provided_source'}
        return [row for row in observations
                if row.get('record') != initial or provided.get(source_key(row)) not in handed_off]

    @staticmethod
    def _schedule_provided_source_memory(metadata: dict, rows: list[dict], memory: dict,
                                         blocks: list, initial: str) -> None:
        """Let the existing checkpoint carry costly, repeated initial sources."""
        if sum(bool(block) for block in blocks) < 2:
            return
        keys = ('subject', 'path', 'sha256', 'offset_bytes', 'next_offset_bytes', 'size_bytes')
        supplied = {tuple(item['source'].get(key) for key in keys)
            for item in metadata['selected_records'] if item['source'].get('record') == initial
            and item['source'].get('kind') == 'source_text'}
        pending = [row for row in rows if row['result'].get('tool_name') == 'provided_source'
            and row['record'] not in memory.get('attempted_records', ())
            and tuple(row['result']['data'].get(key) for key in keys) in supplied]
        if (pending and estimate_tokens(json.dumps([row['result'] for row in pending], ensure_ascii=False))
                > metadata['required_input_tokens']):
            metadata['compaction_needed'] = True
            metadata['compaction_turns'] = sorted({*metadata.get('compaction_turns', []), 0})
            metadata['economical_compaction'] = True

    def _accessible_history_record(self, row: dict) -> bool:
        try:
            return self.workspace.resolve(row['record']).is_file()
        except (ValueError, OSError, KeyError, TypeError):
            return False

    def _native_source_supply(self, saved: dict, observations: list[dict], blocks: list) -> list[dict]:
        """Bind native supply to its actual owner reply, never text elsewhere.

        The shared journal verifies owner receipts; only an intact presented
        read_file window or operation result at that same turn/action can suppress its body.
        The selector still checks versions and the final owner checks supply.
        """
        supplied = []
        traces = {}
        prefix = re.escape(self._path(saved['identity'], 'sources-0').rsplit('sources-', 1)[0])
        for observation in observations:
            match = re.fullmatch(prefix + r'sources-(\d+)\.json#results/(\d+)',
                                 str(observation.get('record')))
            if not match or observation.get('kind') not in {'source_text', 'operation_result'}:
                continue
            turn, index = map(int, match.groups())
            if observation.get('turn') != turn or not 0 <= turn < len(blocks):
                continue
            if turn not in traces:
                traces[turn] = self._closed_source_round(saved, turn)
            trace = traces[turn]
            if trace is None or not 0 <= index < len(trace.get('results', [])):
                continue
            row = trace['results'][index]
            receipt, relation = self._history_receipt(saved, turn, index, trace)
            if receipt is None or relation not in {'identical', 'inspection_source_omitted'}:
                continue
            original = row['result']
            if observation.get('kind') == 'operation_result':
                if relation == 'identical' and json.loads(observation['text']) == original:
                    for message in blocks[turn]:
                        if (message.get('role') == 'tool' and message.get('tool_call_id') == row.get('call_id')
                                and json.loads(message['content']) == original):
                            supplied.append({key:value for key,value in observation.items() if key != 'text'})
                            break
                continue
            data = original.get('data') or {}
            if (row.get('tool') != 'read_file' or original.get('status') != 'ok'
                    or any(data.get(key) != observation.get(key) for key in ('path', 'sha256', 'subject'))):
                continue
            for number, window in enumerate(data.get('text_windows', [])):
                if (any(window.get(key) != observation.get(key) for key in
                        ('offset_bytes', 'next_offset_bytes', 'size_bytes', 'text'))):
                    continue
                field = f'data.text_windows.{number}.text'
                if observation.get('field') not in (None, field):
                    continue
                observation['field'] = field
                for message in blocks[turn]:
                    if message.get('role') != 'tool' or message.get('tool_call_id') != row.get('call_id'):
                        continue
                    try:
                        visible = json.loads(message['content'])
                    except (ValueError, TypeError, KeyError):
                        continue
                    if visible == original:
                        supplied.append({key:value for key,value in observation.items() if key != 'text'})
                        break
        return supplied

    @staticmethod
    def _history_enabled(saved: dict) -> bool:
        policy = saved.get('inspection_history_policy')
        if policy not in (None, 'receipts.v1'):
            raise ValueError('Unknown inspection history policy')
        return policy is not None

    def _unissued_source_round(self, saved: dict, number: int) -> bool:
        """A recorded budget refusal is closed without becoming a source observation."""
        path = self._path(saved['identity'], f'sources-{number}')
        trace = self._bound_json(path, saved['source_rounds'][number], None,
            'Inspection source investigation record changed; no replay')
        response, receipt = trace.get('response') or {}, trace.get('model_receipt')
        if (not trace.get('completed') or trace.get('results') or not isinstance(receipt, dict)
                or response.get('status') != 'blocked'
                or not (response.get('metadata') or {}).get('budget_rejected_before_dispatch')):
            return False
        original, action = self._owned_receipt(receipt)
        return (original == response and action.get('origin') == {
            'scope': saved['identity'], 'kind': 'model', 'position': self._position(number, 'model')}
            and not any(row.attempt_id == receipt['attempt'] for row in self.workspace.ledger.entries))

    def _closed_source_round(self, saved: dict, number: int, *, allow_final: bool = False,
                             model_only: bool = False,
                             validation_records: dict | None = None) -> dict | None:
        """Validate the original request; consumers decide whether tools must also close."""
        path = self._path(saved['identity'], f'sources-{number}')
        trace = self._bound_json(path, saved['source_rounds'][number], validation_records,
            'Inspection source investigation record changed; no replay')
        response = trace.get('response')
        if not trace.get('completed') or not response:
            return None
        call_id = 'inspect-artifacts-'+saved['identity'] + (f'-sources-{number}' if number else '')
        shared = saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS
        model_receipt = trace.get('model_receipt') if shared else None
        if shared and not isinstance(model_receipt, dict):
            return None
        model_attempt = model_receipt['attempt'] if shared else saved['identity']
        if self._offered_coverage(saved) and shared:
            original, action = self._owned_receipt(model_receipt)
            if (original != response
                    or action.get('origin') != {'scope':saved['identity'], 'kind':'model',
                        'position':self._position(number, 'model')}):
                raise ValueError('Inspection model response differs from its closed owner receipt')
        usage = [entry.to_dict() for entry in self.workspace.ledger.entries
            if entry.attempt_id == model_attempt and entry.logical_call_id == call_id]
        if not any(entry['status'] == 'settled' for entry in usage) or not self._accounted(usage):
            return None
        if self._offered_coverage(saved) and not any(entry['status'] == 'settled'
                and entry['actual'].get('llm_requests', 0) > 0
                and entry['actual'].get('total_tokens', 0) > 0 for entry in usage):
            return None
        calls, results = response.get('tool_calls') or [], trace['results']
        terminal = (allow_final and not trace.get('malformed_calls') and isinstance(calls, list)
            and len(calls) == 1 and isinstance(calls[0], dict) and not results
            and isinstance(calls[0].get('function'), dict)
            and calls[0]['function'].get('name') == 'record_inspection')
        if not model_only and (trace.get('malformed_calls') or not terminal and
                [row['call_id'] for row in results] != [call['id'] for call in calls]):
            return None  # A response with unfinished tool work is not closed.
        return trace

    def _history_round(self, saved: dict, number: int) -> dict:
        """Read this inspection's decoded, digest-bound closed owner journal."""
        path = self.root
        relative = self._path(saved['identity'], f'sources-{number}')
        for part in ('', *Path(relative).parts):
            path = path / part
            if path.is_symlink() or path.is_junction():
                raise ValueError('Inspection history cannot traverse links')
        trace = self._closed_source_round(saved, number)
        if trace is None:
            raise ValueError('History round has unfinished or unsettled operations')
        return trace

    def _history_receipt(self, saved: dict, number: int, index: int, trace: dict) -> tuple[dict | None, str]:
        row = trace['results'][index]
        receipt = row.get('receipt')
        if receipt is None:
            return None, 'round_only'  # Historical/local failures remain bound to their round.
        original, action = self._owned_receipt(receipt)
        call = trace['response']['tool_calls'][index]
        if (action.get('origin') != {'scope':saved['identity'], 'kind':'tool',
                    'position':self._position(number, f'tool-{index}', call['id'])}
                or action.get('call') != call):
            raise ValueError('Inspection history receipt belongs to a different action or scope')
        recorded, name = row['result'], call['function']['name']
        if original == recorded:
            return receipt, 'identical'
        # _inspect removes only this private source/allowance envelope from a
        # shared read's visible result. Keep the hashed owner receipt untouched.
        if (name == 'read_file' and original.get('tool_name') == name and isinstance(original.get('metadata'), dict)
                and 'inspection_source' in original.get('metadata', {})):
            projected = deepcopy(original)
            projected['metadata'].pop('inspection_source')
            if projected == recorded:
                return receipt, 'inspection_source_omitted'
        # A post-owner validation failure records a fresh blocked diagnostic,
        # not a rewritten owner result. Validate its exact catch-path shape;
        # the round hash binds the diagnostic, the receipt binds the owner work.
        error = recorded.get('error')
        if (isinstance(error, str) and error and recorded == ToolResult(
                tool_name=name, status='blocked', error=error).model_dump(mode='json')):
            return receipt, 'post_owner_blocked'
        raise ValueError('Inspection history result differs from its original operation receipt')

    def investigation_actions(self, identity: str) -> list[dict]:
        """Expose actual scoped executions, not an opinion that they prove a claim."""
        self._receipt(identity)
        saved = self.store.read_json(self._path(identity, 'input'))
        return self._investigation_actions(saved)

    def _investigation_actions(self, saved: dict, *, receipts: bool = False) -> list[dict]:
        actions = []
        for number in range(len(saved.get('source_rounds', []))):
            trace = self._closed_source_round(saved, number)
            if trace is None:
                continue
            for index, row in enumerate(trace['results']):
                if row.get('tool') not in {'run_process', 'inspect_images'}:
                    continue
                receipt, relation = self._history_receipt(saved, number, index, trace)
                # A blocked owner projection is a separate diagnosis, not the
                # successful native process result behind that diagnosis.
                if receipt is not None and relation == 'identical':
                    actions.append({'call': trace['response']['tool_calls'][index],
                        'result': row['result'], 'record': receipt['record'],
                        **({'receipt':receipt, 'arguments':row['arguments'],
                            'owner_receipts':row.get('owner_receipts', {}),
                            'source_record':self._path(saved['identity'], f'sources-{number}')+f'#results/{index}'}
                           if receipts else {})})
        return actions

    def _provided_sources(self, saved: dict, trace: dict):
        """Windows actually supplied in the first closed inspection offer."""
        if saved.get('initial_material_memory') != 'provided.v1':
            return
        offer = json.loads(trace['offer']['messages'][1]['content'])
        initial = self._path(saved['identity'], 'input')
        for index, entry in enumerate(offer.get('working_view', {}).get('observations', [])):
            observed = entry['observation']
            if (entry.get('version_status') == 'matching' and observed.get('kind') == 'source_text'
                    and observed.get('record') == initial):
                yield index, observed

    def _read_history(self, saved: dict, arguments: dict, *, before_turn: int,
                      read_limit: int | None = None) -> ToolResult:
        """Project navigation or a selected semantic value from validated own receipts."""
        if not self._history_enabled(saved):
            raise ValueError('Inspection history was not offered by this saved contract')
        provided = saved.get('initial_material_memory') == 'provided.v1'
        if saved.get('inspection_material_policy') == 'operations.v3':
            selection = (_ProvidedInspectionHistory if provided else _RetainedInspectionHistory).model_validate(arguments)
            if selection.inspection is not None and selection.inspection != saved['identity']:
                prior = {item['identity']:item for item in self._retained_inputs(saved)}
                if selection.inspection not in prior:
                    raise ValueError('History owner is not an accessible retained investigation')
                old = prior[selection.inspection]
                return self._read_history(old, selection.model_dump(exclude={'inspection'}),
                    before_turn=len(old.get('source_rounds', [])), read_limit=read_limit)
            arguments = selection.model_dump(exclude={'inspection'})
        page = (_ProvidedInspectionHistory if provided else _InspectionHistory).model_validate(arguments)
        if read_limit is not None and (type(read_limit) is not int or read_limit <= 0):
            raise ValueError('History read capacity must be a positive integer')
        total = min(before_turn, len(saved.get('source_rounds', [])))
        if page.start_turn >= total:
            raise ValueError('History round is outside the prior closed inspection history')
        relative = self._path(saved['identity'], f'sources-{page.start_turn}')
        end_turn = page.start_turn+1
        binding = {'record':relative, 'sha256':saved['source_rounds'][page.start_turn]}
        if page.action is None:
            end_turn = min(total, page.start_turn+page.max_turns) if page.end_turn is None else page.end_turn
            if not page.start_turn < end_turn <= min(total, page.start_turn+page.max_turns):
                raise ValueError('Pinned history end is outside this prior bounded range')
            entries = []
            for number in range(page.start_turn, end_turn):
                trace = self._history_round(saved, number)
                record = self._path(saved['identity'], f'sources-{number}')
                actions = []
                for index, row in enumerate(trace['results']):
                    result = row['result']
                    arguments = row.get('arguments', {})
                    actions.append({'action':index, 'call_id':row['call_id'], 'tool':result.get('tool_name'),
                        'status':result.get('status'), 'error_preview':str(result.get('error', ''))[:256],
                        'arguments':{key:value for key,value in arguments.items()
                            if key in {'path','cwd','offset_bytes','max_bytes','query'}
                            and len(json.dumps(value,ensure_ascii=False)) <= 256} if isinstance(arguments,dict) else {},
                        'result':record+f'#results/{index}/result',
                        'original_arguments':record+f'#results/{index}/arguments'})
                if not trace['results']:
                    actions.append({'action':0, 'tool':'assistant_reply', 'result':record+'#response'})
                if number == 0 and provided:
                    actions.extend({'action':index, 'field':'provided', 'tool':'provided_source',
                        'path':source['path'], 'result':record+f'#provided/{index}'}
                        for index, source in self._provided_sources(saved, trace))
                entries.append({'turn':number, 'record':record, 'sha256':saved['source_rounds'][number], 'actions':actions})
            value = entries
            binding = {'projection':'navigation_index', 'scope':'Derived navigation; previews are incomplete '
                'and do not validate individual operation receipts. Select an action to check its owner and '
                'read the retained semantic value.'}
        else:
            trace = self._history_round(saved, page.start_turn)
            if page.field == 'provided':
                sources = dict(self._provided_sources(saved, trace)) if page.start_turn == 0 else {}
                if page.action not in sources:
                    raise ValueError('Selected initial source window is absent')
                receipt, relation, value = None, 'round_only', sources[page.action]
                pointer = f'/offer/messages/1/content#/working_view/observations/{page.action}/observation'
            elif trace['results']:
                if page.action >= len(trace['results']):
                    raise ValueError('Selected history action is outside this round')
                row = trace['results'][page.action]
                receipt, relation = self._history_receipt(saved, page.start_turn, page.action, trace)
                value = row[page.field]
                pointer = f'/results/{page.action}/{page.field}'
            elif page.action == 0 and page.field == 'result':
                receipt, relation, value, pointer = None, 'round_only', trace['response'], '/response'
            else:
                raise ValueError('Selected history action or field is absent')
            binding.update(projection='retained_value', action=page.action, field=page.field,
                pointer=pointer, receipt_projection=relation, **({'receipt':receipt} if receipt else {}),
                scope='Projection of the retained inspection journal value, not original formatting or new authoritative state. '
                    'inspection_source_omitted removes only the owner source/allowance metadata envelope. '
                    'post_owner_blocked is the round-bound post-check diagnostic, not the owner result; '
                    'the receipt retains the original owner output and metadata. '
                    'Historical output is not current evidence or permission.')
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        binding.update(serialization='canonical_json', projection_sha256=hashlib.sha256(raw).hexdigest())
        size = len(raw)
        if page.offset_bytes > size:
            raise ValueError('History byte offset is beyond this value')
        limit = min(page.max_bytes, read_limit) if read_limit is not None else page.max_bytes
        window = raw[page.offset_bytes:page.offset_bytes+limit]
        try:
            text = window.decode('utf-8')
        except UnicodeDecodeError as exc:
            if exc.reason != 'unexpected end of data' or not exc.start:
                raise ValueError('History byte window must contain complete UTF-8 characters') from None
            window = window[:exc.start]
            text = window.decode('utf-8')
        stop = page.offset_bytes+len(window)
        return ToolResult(tool_name='read_history', data={'text':text, **binding,
            'start_turn':page.start_turn, 'end_turn':end_turn, 'size_bytes':size,
            'total_turns':total, 'offset_bytes':page.offset_bytes, 'next_offset_bytes':stop,
            'has_more_bytes':stop < size, 'next_turn':page.start_turn if stop < size else end_turn},
            summary='Own inspection receipt history, not current evidence or permission.')

    def _conversation_rows(self, saved: dict) -> list[dict]:
        """Exact closed source results from this role's original round journal."""
        from simple_ar.app.work_memory import observation_currentness
        rows = []
        for number in range(len(saved.get('source_rounds', []))):
            trace = self._closed_source_round(saved, number)
            if trace is None:
                continue
            path = self._path(saved['identity'], f'sources-{number}')
            response, results = trace['response'], trace['results']
            calls = response.get('tool_calls') or []
            if number == 0 and saved.get('initial_material_memory') == 'provided.v1':
                # The first offer is an actual model input, not a tool call.
                # Give its supplied source windows the same checkpoint owner as
                # later reads, while retaining their original offer and bytes.
                for index, observed in self._provided_sources(saved, trace):
                    data = {key: observed[key] for key in ('subject', 'path', 'sha256', 'offset_bytes',
                        'next_offset_bytes', 'size_bytes', 'text') if key in observed}
                    for key in ('offset_bytes', 'next_offset_bytes'):
                        if 'supplied_' + key in observed:
                            data[key] = observed['supplied_' + key]
                    rows.append({'turn': number, 'record': path + f'#provided/{index}', 'arguments': {},
                        'result': {'tool_name': 'provided_source', 'status': 'ok', 'data': data,
                            'scope': 'Source window actually supplied in the initial offer, not a tool call.'},
                        'decision': response, 'call_id': None, 'source_currentness': 'file_bytes'})
            for index, row in enumerate(results):
                rows.append({'turn':number, 'record':path+f'#results/{index}',
                    'arguments':row['arguments'], 'result':row['result'],
                    **({'decision':response, 'call_id':row['call_id'],
                        'source_currentness':observation_currentness(row['result'])}
                       if saved.get('native_supply_policy') == TURN_SUPPLY_POLICY else {})})
            if not calls:
                rows.append({'turn':number, 'record':path+'#response', 'arguments':{}, 'records':[],
                    'result':{'tool_name':'assistant_reply', 'data':response},
                    **({'decision':response, 'call_id':None, 'source_currentness':'historical'}
                       if saved.get('native_supply_policy') == TURN_SUPPLY_POLICY else {})})
        return rows

    @staticmethod
    def _bind_conversation_notes(notes: list, rows: list) -> list:
        from simple_ar.app.work_memory import observation_currentness
        strict = any('source_currentness' in row for row in rows)
        originals = {row['record']:row['result'] for row in rows}
        bound = []
        for note in notes:
            versions = {}
            unknown = strict and not note['records']
            for record in note['records']:
                if record not in originals:
                    raise ValueError('Inspection note references work outside its source journal')
                result = originals[record]
                if (strict and result.get('tool_name') != 'provided_source'
                        and observation_currentness(result) == 'unknown'):
                    unknown = True
                data = result.get('data') or {}
                if result.get('tool_name') in {'read_file', 'provided_source'} and result.get('status') == 'ok':
                    path, digest = data['path'], data['sha256']
                    if strict and path in versions and versions[path] != digest:
                        unknown = True
                    else:
                        versions[path] = digest
            bound.append({**note, 'observed_versions':versions, 'scope':'model_note_not_verified_evidence',
                          **({'source_integrity':'unknown'} if unknown else {})})
        return bound

    def _conversation_memory(self, saved: dict, rows: list) -> dict:
        from simple_ar.app.professional_conversation import ProfessionalConversation
        from simple_ar.app.work_memory import (checkpoint_artifacts, checkpoint_entries,
                                               validate_checkpoint_sources)
        memory = {'notes':[], 'through_turn':0, 'compacted_records':set()}
        originals = {row['record']:row for row in rows}
        operations = lambda turn:{row['record']:row['arguments'] for row in rows if row['turn'] == turn}
        sessions = {entry.session_id for entry in self.workspace.ledger.entries
            if entry.attempt_id == saved['identity'] and entry.status == 'settled'
            and entry.logical_call_id == 'inspect-artifacts-'+saved['identity']}
        for number in range(len(saved.get('source_rounds', []))):
            trace = self.store.read_json(self._path(saved['identity'], f'sources-{number}'))
            recorded = trace.get('checkpoint', {})
            if not recorded:
                continue
            for path, digest in recorded.items():
                if self._digest(path) != digest:
                    raise ValueError('Inspection checkpoint record changed; no replay')
            store = ArtifactStore(self.root/f"tools/inspection/{saved['identity']}/sources-{number}")
            call_id = f"working-memory-{saved['identity']}-sources-{number}"
            attempt_id = (trace['checkpoint_receipt']['attempt'] if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS
                else saved['identity'])
            checkpoint_sessions = ({entry.session_id for entry in self.workspace.ledger.entries
                if entry.attempt_id == attempt_id and entry.status == 'settled'
                and entry.logical_call_id == call_id}
                if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS else sessions)
            if len(checkpoint_sessions) != 1:
                raise ValueError('Inspection checkpoint has no unique original conversation session')
            session_id, = checkpoint_sessions
            try:
                checkpoint_entries(self.workspace.ledger, attempt_id,
                    session_id, call_id=call_id,
                    completed_retry=saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS)
            except ValueError:
                continue  # Unknown/inflight work is not adopted and its fees remain.
            view = store.read_json('memory_input.json')
            if view['requests'] != saved.get('user_requests', [saved['arguments']['goal']]):
                raise ValueError('Inspection checkpoint changed its original requests')
            if ((saved.get('native_supply_policy') == TURN_SUPPLY_POLICY)
                    != (view.get('native_supply_policy') == TURN_SUPPLY_POLICY)):
                raise ValueError('Inspection checkpoint changed its saved supply contract')
            validate_checkpoint_sources(view, source=lambda row:originals.get(row['record']),
                owner='inspection', before_turn=number)
            refs = checkpoint_artifacts(store=store, ledger=self.workspace.ledger, attempt_id=attempt_id,
                session_id=session_id, call_id=call_id,
                completed_retry=saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS,
                bind_notes=lambda notes:self._bind_conversation_notes(notes, rows))
            expected = {store.resolve(ref.path).relative_to(self.store.root).as_posix() for ref in refs}
            if set(recorded) != expected or view['archive_through_turn'] > number:
                raise ValueError('Inspection checkpoint artifacts exceed their recorded round scope')
            memory = ProfessionalConversation.adopt(previous=memory, view=view, operations_for_turn=operations,
                snapshot=store.read_json('memory_snapshot.json') if store.exists('memory_snapshot.json') else None,
                failure=store.read_json('memory_failure.json') if store.exists('memory_failure.json') else None,
                archive=saved.get('work_method') in NATIVE_WORK_METHODS)
        return memory

    def _conversation_material(self, saved: dict, *, execution_facts: dict | None = None,
                               retained_inputs: list[dict] | None = None) -> tuple[dict, list, list]:
        """Adapt original reads and operation outcomes, never another role's notes."""
        rows = self._conversation_rows(saved)
        memory = self._conversation_memory(saved, rows)
        input_record = self._path(saved['identity'], 'input')
        strict_source = self._applicable_scope(saved) or saved.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES
        initial = Counter()
        if strict_source and saved.get('source_rounds'):
            first = self._closed_source_round(saved, 0, allow_final=True, model_only=True)
            projection = (first or {}).get('offer', {}).get('projection', {})
            for state in [*projection.get('selected_records', []), *projection.get('omitted_records', [])]:
                source = state.get('source', {})
                if source.get('kind') == 'source_text' and source.get('record') == input_record:
                    initial[tuple(source.get(key) for key in
                        ('subject', 'path', 'sha256', 'offset_bytes', 'next_offset_bytes', 'size_bytes'))] += 1
        sources = {}
        for row in rows:
            data = row['result'].get('data') or {}
            if row['result'].get('tool_name') == 'read_file' and row['result'].get('status') == 'ok':
                for index, window in enumerate(data.get('text_windows', [])):
                    if strict_source:
                        key = (data.get('subject'), data.get('path'), data.get('sha256'),
                            window.get('offset_bytes'), window.get('next_offset_bytes'), window.get('size_bytes'))
                        sources.setdefault(key, []).append((row, index, window.get('text')))
                    else:
                        sources[(data['subject'], window['offset_bytes'],
                            window['next_offset_bytes'], window['text'])] = row
        observations = []
        # Shared targets and evidence use the common material selector. Legacy
        # owners keep target text in their required domain input.
        materials = ([*saved['targets'], *saved['evidence']]
            if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS else saved['evidence'])
        materials = [row for row in materials if row.get('readable')]
        if self._applicable_scope(saved):
            # Earlier windows of different current objects should have a fair
            # chance to fit before a single large object's later windows.
            windows = ((row, row['windows'][index])
                for index in range(max((len(row['windows']) for row in materials), default=0))
                for row in materials if index < len(row['windows']))
        else:
            windows = ((row, window) for row in materials for window in row['windows'])
        for row, window in windows:
            text = window.get('review_text', window['text'])
            key = (row['subject'], row['path'], row['sha256'], window['offset_bytes'],
                window['review_next_offset_bytes'], window['size_bytes'])
            # The first recorded offer distinguishes primed input from later
            # actions. Match later actions by subject/path/version/range and
            # verify bytes, never identify a receipt by matching prose alone.
            if strict_source:
                is_initial = initial[key] > 0
                if is_initial:
                    initial[key] -= 1
                matches = [] if is_initial or window.get('source_reference') else sources.get(key, [])
                match = next((i for i, (_, _, body) in enumerate(matches) if body == text), None)
                source, index = matches.pop(match)[:2] if match is not None else (None, None)
            else:
                source = sources.get((row['subject'], window['offset_bytes'],
                    window['review_next_offset_bytes'], text))
                index = None
            observations.append({'record':source['record'] if source else input_record,
                **({'field':f'data.text_windows.{index}.text'} if source and index is not None else {}),
                'subject':row['subject'],
                'path':row['path'], 'sha256':row['sha256'], 'kind':'source_text',
                'role':'historical_observation', 'turn':source['turn'] if source else None,
                'offset_bytes':window['offset_bytes'], 'next_offset_bytes':window['review_next_offset_bytes'],
                'size_bytes':window['size_bytes'], 'text':text})
        if saved.get('inspection_material_policy') in _WORK_MATERIAL_POLICIES:
            observations.extend({'record':row['record'], 'turn':row['turn'],
                'kind':'operation_result', 'role':'historical_observation',
                'text':json.dumps(row['result'], ensure_ascii=False),
                'scope':'Original own operation outcome, including failures; historical execution '
                    'or diagnostic, not current file contents or scientific certification.'}
                for row in rows if row['result'].get('tool_name') not in {
                    'read_file', 'assistant_reply', 'provided_source'})
        if saved.get('inspection_material_policy') == 'operations.v3':
            parents = self._retained_inputs(saved) if retained_inputs is None else retained_inputs
            if execution_facts is None:
                execution_facts = self._execution_facts(saved, retained=parents)
            facts = {value['source_record']:record for record,value in execution_facts.items()}
            relevant = (self._relevant_execution_sources(saved, execution_facts)
                if saved.get('source_priming_policy') == 'priority.v4' else None)
            for parent in parents:
                observations.extend({'record':row['record'],
                    'kind':'operation_result', 'role':'historical_observation',
                    'text':json.dumps({'inspection':parent['identity'], 'result':row['result'],
                        'execution_record':facts.get(row['record']),
                        'scope':'Original independent outcome, not an adopted opinion or current file body. '
                            'A missing execution_record means diagnostic history only.'}, ensure_ascii=False)}
                    for row in self._conversation_rows(parent)
                    if row['result'].get('tool_name') not in {
                        'read_file', 'assistant_reply', 'record_inspection', 'provided_source'}
                    and (relevant is None or row['record'] in relevant))
        return memory, observations, rows

    def _inspection_projector(self, saved, request, messages, tools, terminal, finish, client, response_model):
        """Build both investigation and final offers with the same evidence contract."""
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation

        conversation = ProfessionalConversation(client)

        previously_supplied = (self._offered_text_ranges(saved)
            if response_model is not None and self._delivery_scope(saved)
            and saved.get('inspection_evidence_policy') in _PRESENTED_EVIDENCE_POLICIES else {})

        def project(closing):
            offered = [terminal] if closing else [*tools, terminal]
            limit = (conversation.closing_output_limit()
                if closing and saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS else None)
            projected, metadata = self._working_messages(saved, request, messages, offered,
                conversation.projection_client(limit),
                finish=finish if closing else None,
                prioritize_delivery=closing and saved.get('source_priming_policy') == 'priority.v4')
            if (response_model is not None and self._delivery_scope(saved)
                    and saved.get('inspection_evidence_policy') in _PRESENTED_EVIDENCE_POLICIES):
                # A retained source descriptor is navigation, not a body the
                # reviewer can cite. Advertise windows actually presented in
                # this investigation; the final owner still checks saved supply.
                subjects = {entry['source'].get('subject') for entry in metadata.get('selected_records', [])
                    if entry.get('status') in {'native', 'working_view'}
                    and entry.get('version_status') == 'matching'
                    and entry.get('source', {}).get('kind') == 'source_text'}
                for row in [*saved['targets'], *saved['evidence']]:
                    ranges = previously_supplied.get((row.get('path'), row.get('sha256')), [])
                    if any(max(window['offset_bytes'], start) < min(window['review_next_offset_bytes'], end)
                           or window['size_bytes'] == 0 and start == end == 0
                           for window in row.get('windows', []) for start, end in ranges):
                        subjects.add(row['subject'])
                final = deepcopy(terminal)
                final['function']['parameters'] = self._delivery_schema(saved, response_model, subjects=subjects)
                offered = [final] if closing else [*tools, final]
            return ConversationInput(projected, offered, metadata, output_limit=limit)

        return project

    def _prepare_conversation_round(self, saved, request, messages, tools, terminal, finish,
                                    client, number, remaining, *, dispatch=None, record=None, save_round=None,
                                    response_model=None):
        from simple_ar.app.professional_conversation import ProfessionalConversation
        from simple_ar.app.work_memory import (checkpoint_artifacts, compact_work, prepare_checkpoint_input,
                                               validate_checkpoint_sources)
        conversation = ProfessionalConversation(client)
        trace = self._path(saved['identity'], f'sources-{number}')
        last_floor = {}
        project = self._inspection_projector(saved, request, messages, tools, terminal, finish, client, response_model)
        exit_first = saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS

        def followup(value):
            nonlocal last_floor
            last_floor = conversation.request_resources(value if exit_first else project(True))
            return last_floor

        def shared_checkpoint(checkpoint_input, *, restore_floor):
            rows = self._conversation_rows(saved)
            originals = {row['record']:row for row in rows}
            if any(key not in originals for key in checkpoint_input['records']):
                raise ValueError('Inspection checkpoint source receipt changed; no replay')
            pending = [originals[key] for key in checkpoint_input['records']]
            memory = self._conversation_memory(saved, rows)
            store = ArtifactStore(self.root/f"tools/inspection/{saved['identity']}/sources-{number}")
            call_id = f"working-memory-{saved['identity']}-sources-{number}"

            def perform(actual_id):
                checkpoint_client = self.client_factory(actual_id)
                if (saved.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES
                        and checkpoint_client._settings.context_input_tokens != checkpoint_input['context_input_tokens']):
                    raise ValueError('Inspection checkpoint capacity changed before purchase')
                snapshot, refs = compact_work(store=store, client=checkpoint_client,
                    requests=saved.get('user_requests', [request.goal]), previous=memory, rows=pending,
                    bind_notes=lambda notes:self._bind_conversation_notes(notes, rows),
                    estimate_tokens=estimate_tokens, attempt_id=actual_id, call_id=call_id,
                    required_decision_tokens=checkpoint_input['decision_tokens'], additional_protection=True,
                    native_supply_policy=saved.get('native_supply_policy'),
                    economic_floor=checkpoint_input.get('economic_floor'),
                    note_contract='knowledge.v1' if saved.get('work_method') == GOAL_WORK_METHOD else None,
                    completed_retry=True)
                return {'snapshot':snapshot, 'artifacts':[ref.to_dict() for ref in refs]}

            def recover(actual_id):
                # Reconstitute only a paid, fully closed checkpoint. A lost
                # ordinary model reply has no such owner proof or recovery hook.
                if (not store.exists('memory_input.json')
                        or not (store.exists('memory_snapshot.json') or store.exists('memory_failure.json'))):
                    raise ValueError('Inspection checkpoint has no complete owner receipt; no replay')
                sessions = {entry.session_id for entry in self.workspace.ledger.entries
                    if entry.attempt_id == actual_id and entry.logical_call_id == call_id
                    and entry.status == 'settled'}
                if len(sessions) != 1:
                    raise ValueError('Inspection checkpoint has no unique settled owner session')
                session_id, = sessions
                refs = checkpoint_artifacts(store=store, ledger=self.workspace.ledger,
                    attempt_id=actual_id, session_id=session_id, call_id=call_id,
                    completed_retry=True,
                    bind_notes=lambda notes:self._bind_conversation_notes(notes, rows))
                view = store.read_json('memory_input.json')
                if view.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES:
                    expected = prepare_checkpoint_input(
                        requests=saved.get('user_requests', [request.goal]), previous=memory, rows=pending,
                        target=checkpoint_input['context_input_tokens'],
                        estimate_tokens=estimate_tokens, native_supply_policy=saved.get('native_supply_policy'),
                        economic_floor=checkpoint_input.get('economic_floor'),
                        note_contract=view.get('note_contract'))
                    if view != expected:
                        raise ValueError('Inspection checkpoint differs from its retained preparation')
                    snapshot = store.read_json('memory_snapshot.json') if store.exists('memory_snapshot.json') else None
                    return {'snapshot':snapshot, 'artifacts':[ref.to_dict() for ref in refs]}
                # Real historical unmarked inputs keep their recorded contract;
                # they did not permit capacity retirement of previous notes.
                if (view['requests'] != saved.get('user_requests', [request.goal])
                        or view['previous_notes'] != [{'id':'p'+str(index), **note}
                            for index, note in enumerate(memory.get('notes', []))]
                        or view['archive_through_turn'] != max(row['turn'] for row in pending)+1):
                    raise ValueError('Inspection checkpoint differs from its retained preparation')
                positions = {row['record']:index for index, row in enumerate(pending)}
                supplied = view['observations']
                order = [positions.get(row['record'], -1) for row in supplied]
                if (not order or any(index < 0 for index in order)
                        or order != sorted(set(order))):
                    raise ValueError('Inspection checkpoint observations differ from the retained plan')
                validate_checkpoint_sources(view, source=lambda row:originals[row['record']],
                    owner='inspection', error='Inspection checkpoint changed its original source receipt')
                snapshot = store.read_json('memory_snapshot.json') if store.exists('memory_snapshot.json') else None
                return {'snapshot':snapshot, 'artifacts':[ref.to_dict() for ref in refs]}

            perform.recover = recover

            if 'checkpoint_receipt' in record:
                receipt = record['checkpoint_receipt']
            else:
                position = self._position(number, 'checkpoint')
                if restore_floor:
                    with self.workspace.ledger.protect_remaining(checkpoint_input['floor'], additional=True):
                        wrapper = dispatch('model', position, None, perform)
                else:
                    wrapper = dispatch('model', position, None, perform)
                receipt = self._closed_receipt(saved, wrapper)
                record['checkpoint_receipt'] = receipt
            try:
                result = self._receipt_result(receipt)
                if result.get('status') in {'blocked', 'error'}:
                    raise ValueError(result.get('error') or 'Inspection checkpoint produced no response')
                if 'snapshot' not in result or not isinstance(result.get('artifacts'), list):
                    raise ValueError('Inspection checkpoint dispatcher returned a malformed receipt')
                paths = [ref['path'] for ref in result['artifacts']]
                if (len(set(paths)) != len(paths) or any(path not in
                        {'memory_input.json', 'memory_response.json', 'memory_snapshot.json', 'memory_failure.json'}
                        for path in paths)):
                    raise ValueError('Inspection checkpoint receipt names unexpected artifacts')
                record['checkpoint'] = {store.resolve(path).relative_to(self.store.root).as_posix():
                    hashlib.sha256(store.require(path).read_bytes()).hexdigest() for path in paths}
            finally:
                save_round()

        def checkpoint(value, decision_tokens):
            rows = self._conversation_rows(saved)
            memory = self._conversation_memory(saved, rows)
            selected = value.projection.get('compaction_turns')
            pending = [row for row in rows if (row['turn'] in selected if selected is not None
                else row['turn'] < value.projection['recent_start_turn'])
                and row['record'] not in memory['compacted_records']]
            if dispatch is not None:
                if not pending:
                    return
                checkpoint_input = {'records':[row['record'] for row in pending],
                    'decision_tokens':decision_tokens, 'floor':deepcopy(last_floor)}
                if (saved.get('context_checkpoint_policy') == 'economic.v3'
                        and value.projection.get('economical_compaction')):
                    checkpoint_input['economic_floor'] = value.projection['required_input_tokens']
                if saved.get('native_supply_policy') in RECEIPT_SUPPLY_POLICIES:
                    # Recovery must reproduce the purchased input even if the
                    # active client's capacity changes after interruption.
                    checkpoint_input['context_input_tokens'] = client._settings.context_input_tokens
                record['checkpoint_input'] = checkpoint_input
                save_round()
                shared_checkpoint(checkpoint_input, restore_floor=False)
                return
            store = ArtifactStore(self.root/f"tools/inspection/{saved['identity']}/sources-{number}")
            try:
                compact_work(store=store, client=client, requests=saved.get('user_requests', [request.goal]),
                    previous=memory, rows=pending, bind_notes=lambda notes:self._bind_conversation_notes(notes, rows),
                    estimate_tokens=estimate_tokens, attempt_id=saved['identity'],
                    call_id=f"working-memory-{saved['identity']}-sources-{number}",
                    native_supply_policy=saved.get('native_supply_policy'),
                    note_contract='knowledge.v1' if saved.get('work_method') == GOAL_WORK_METHOD else None,
                    economic_floor=(value.projection['required_input_tokens']
                        if value.projection.get('economical_compaction') else None),
                    required_decision_tokens=decision_tokens, additional_protection=True)
            finally:
                # Bind all paid work, even when the following model turn fails.
                # These are checkpoint artifacts in the original round journal,
                # not an adopted-note cache or a second progress watermark.
                legacy_record = self.store.read_json(trace)
                legacy_record['checkpoint'] = {path.relative_to(self.store.root).as_posix():
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    for name in ('memory_input.json', 'memory_response.json', 'memory_snapshot.json', 'memory_failure.json')
                    if (path := store.resolve(name)).exists()}
                self.store.write_json(trace, legacy_record)
                saved['source_rounds'][number] = self._digest(trace)
                self.store.write_json(self._path(saved['identity'], 'input'), saved)

        checkpoint_input = record.get('checkpoint_input') if dispatch is not None else None
        if checkpoint_input is not None:
            shared_checkpoint(checkpoint_input, restore_floor=True)
        value = conversation.prepare(project,
            checkpoint=None if checkpoint_input is not None else checkpoint,
            followup=followup,
            force_close=remaining[0] <= 0, additional=True,
            exit_first=exit_first)
        return conversation, value

    def _legacy_inspection_input(self, saved, request, messages, tools, terminal, client, finish, number, remaining):
        """Saved non-journal owners retain their original source bounds."""
        def tokens(history, offered):
            return (estimate_tokens(json.dumps(history, ensure_ascii=False))
                + estimate_tokens(json.dumps(offered, ensure_ascii=False))
                + (client.effective_output_limit() or 0))
        projected, final_history = messages, [*messages, finish]
        projection = final_projection = None
        if self.progressive:
            projected, projection = self._working_messages(saved, request, messages, [*tools, terminal], client)
            final_history, final_projection = self._working_messages(saved, request, messages, [terminal], client, finish=finish)
        final_tokens = tokens(final_history, [terminal])
        slots, capacity = self.workspace.ledger.remaining('llm_requests'), self.workspace.ledger.remaining('total_tokens')
        terminal_only = (not self.adaptive and number == 6
            or self.adaptive and remaining[0] <= 0 or slots is not None and slots < 2
            or capacity is not None and capacity < final_tokens + tokens(projected, [*tools, terminal]))
        if terminal_only:
            messages.append(finish)
        return (final_history if terminal_only else projected,
            [terminal] if terminal_only else [*tools, terminal],
            final_projection if terminal_only else projection, terminal_only, final_tokens)

    def _prime_targets(self, saved: dict, request: InspectionRequest, identity: str,
                       remaining: list[int], client: Any, offered: list) -> None:
        """Supply whole-file requests without asking the reviewer to select pages.

        Only a whole-file request may expand. The ordinary projector admits the
        actual decoded bytes; reading them neither concludes nor passes a review.
        A cached inspection never enters this fresh-work path.
        """
        if not self.progressive:
            return
        if saved.get('source_priming_policy') in _PRIORITY_SOURCE_POLICIES:
            originals = [(row['path'], row['sha256'], [row])
                for value, row in zip(request.targets, saved['targets'])
                if isinstance(value, str) and row.get('readable') and not row.get('complete')]
            self._prime_originals(saved, request, identity, remaining, client, offered, originals)
            return
        for index, value in enumerate(request.targets):
            row = saved['targets'][index]
            if not isinstance(value, str) or row.get('complete') or not row.get('readable'):
                continue
            _, projection = self._working_messages(saved, request, None, offered, client)
            # Optional originals can yield to required target bytes. The final
            # candidate projection below still enforces the actual input target.
            cost = ('required_input_tokens' if self.adaptive
                and saved.get('inspection_context_policy') == 'retained.v1' else 'estimated_input_tokens')
            headroom = projection['input_token_target'] - projection[cost]
            window = row['windows'][-1]
            size, cursor = window['size_bytes'], window['review_next_offset_bytes']
            count = size - cursor
            # Historical owners retain their original one-read allocation.
            bound = (256000 if False
                else 64000)
            if not 0 < count <= min(bound, remaining[0], headroom):
                continue
            tail = self._observe(FileWindow(path=row['path'], offset_bytes=cursor, max_bytes=count),
                row['subject'], identity, remaining)
            if not tail.get('readable') or tail.get('sha256') != row.get('sha256'):
                raise ValueError('Inspection target changed or became unreadable while supplying its text')
            candidate = deepcopy(saved)
            merged = candidate['targets'][index]
            merged['windows'].extend(tail['windows'])
            merged['complete'] = self._complete_target(merged, [merged])
            if merged['complete']:
                merged.pop('limitation', None)
            _, admitted = self._working_messages(candidate, request, None, offered, client,
                validate_capacity=False)
            if admitted['estimated_input_tokens'] <= admitted['input_token_target']:
                saved['targets'][index] = merged
        self.store.write_json(self._path(identity, 'input'), saved)

    @staticmethod
    def _missing_original_ranges(size: int, owners: list[dict]) -> list[tuple[int, int]]:
        windows = sorted((window['offset_bytes'], window['review_next_offset_bytes'])
            for owner in owners for window in owner['windows'] if window['size_bytes'] == size)
        gaps, cursor = [], 0
        for start, end in windows:
            if not 0 <= start <= end <= size:
                continue
            if start > cursor:
                gaps.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < size or not size and not owners:
            gaps.append((cursor, size))
        return gaps

    @staticmethod
    def _delivery_source_paths(saved: dict, *, include_artifacts: bool = False) -> list[str]:
        claim = saved['delivery_context']['claim']
        primary = [path for path in ((claim.get('report') or {}).get('manuscript'),
            (claim.get('summary_source') or {}).get('path')) if path]
        artifacts = [path for path in saved['delivery_context']['artifacts']
                     if Path(path).suffix.lower() not in _BINARY]
        if 'revision_sources' in saved:
            pending = [path for finding in saved.get('prior_required', ())
                       for path in finding.get('pending_subjects', ())]
            return list(dict.fromkeys([*primary,
                *(path for path in [*pending, *saved['revision_sources'], *saved.get('introduced_artifacts', ())]
                  if path in artifacts)]))
        if include_artifacts or saved['delivery_context'].get('scope_policy') == 'applicable.v1':
            # This contract primes the whole delivery, so its other originals
            # must get a window before a long manuscript consumes the input.
            return list(dict.fromkeys([*primary, *artifacts]))
        if saved.get('source_priming_policy') == 'priority.v4' and len(saved['delivery_context']['artifacts']) > 1:
            # A multi-file inventory does not identify which sources answer
            # the claim. The inspector can select other files when needed.
            return primary
        return primary or artifacts

    def _prime_delivery(self, saved: dict, request: InspectionRequest, identity: str,
                        remaining: list[int], client: Any, offered: list, *,
                        terminal_only: bool = False) -> None:
        """Supply authorized originals without an all-or-nothing bundle gate."""
        delivery = saved.get('delivery_context', {})
        if self._applicable_scope(saved):
            # Supply actual current deliverables before asking for an opinion.
            # The claim explicitly identifies manuscript/summary sources; neither
            # inventory cardinality nor the supplied bytes decide applicability.
            priority = self._delivery_source_paths(saved, include_artifacts=terminal_only)
            originals = []
            if delivery.get('scope_policy') == 'applicable.v2':
                receipts = {}
                artifacts = delivery['artifacts']
                if 'revision_sources' in saved:
                    artifacts = {path: artifacts[path] for path in priority if path in artifacts}
                for source in self.workspace.artifact_sources(artifacts).values():
                    graph = source.get('lineage')
                    if graph is None:
                        continue
                    node = next(row for row in graph['nodes'] if row['id'] == graph['root'])
                    producer = node.get('producer')
                    if node['state'] == 'recorded' and producer:
                        receipts[str(self.root / producer['receipt'])] = producer['receipt_sha256']
                # A useful execution need not produce a deliverable. Reuse the
                # existing parent execution projection as historical evidence,
                # never as an independent check or a proof of undeclared inputs.
                if 'revision_sources' not in saved:
                    for path, digest in saved.get('parent_process_receipts', {}).items():
                        receipts.setdefault(path, digest)
                saved['production_receipts'] = list(receipts)
                priority = ([*priority, *receipts] if terminal_only else [*receipts, *priority])
                for path, digest in receipts.items():
                    owners = [row for row in saved['evidence'] if row.get('readable')
                        and row.get('path') == path and row.get('sha256') == digest]
                    originals.append((path, digest, owners))
            for path, digest in delivery['artifacts'].items():
                if delivery.get('scope_policy') == 'applicable.v2' and path not in priority:
                    # An explicitly named manuscript/summary is current work;
                    # inventory membership alone is not relevance. Other actual
                    # evidence is selected on demand by this same inspector.
                    continue
                if Path(path).suffix.lower() in _BINARY:
                    continue  # Text decoding capability, never a check exemption.
                owners = [row for row in saved['evidence'] if row.get('readable')
                    and row.get('path') == path and row.get('sha256') == digest]
                originals.append((path, digest, owners))
            self._prime_originals(saved, request, identity, remaining, client, offered,
                originals, priority=priority, leave_read_window=not terminal_only)
            return
        if not self.adaptive or delivery.get('schema_version') != 'delivery_review.v3':
            return
        targets = {row['path'] for row in saved['targets']}
        if saved.get('source_priming_policy') not in _PRIORITY_SOURCE_POLICIES:
            # Historical inspections keep their saved all-or-nothing contract.
            pending = []
            for path, digest in delivery['artifacts'].items():
                if path in targets or Path(path).suffix.lower() in _BINARY:
                    continue
                owners = [row for row in saved['evidence']
                    if row.get('readable') and row.get('path') == path and row.get('sha256') == digest]
                if owners and self._complete_target(owners[0], owners):
                    continue
                size = self.workspace.resolve(path).stat().st_size
                if size > 256000:
                    return
                pending.append((path, digest, self._missing_original_ranges(size, owners), owners))
            if sum(end-start for _, _, gaps, _ in pending for start, end in gaps) > remaining[0]:
                return
            number = max((int(row['subject'].split('-')[1]) for row in saved['evidence']), default=0)
            for path, digest, gaps, owners in pending:
                if not owners:
                    number += 1
                subject = owners[0]['subject'] if owners else f'evidence-{number}'
                for start, end in gaps:
                    observed = self._observe(FileWindow(path=path, offset_bytes=start, max_bytes=max(1, end-start)),
                        subject, identity, remaining)
                    if observed.get('sha256') and observed['sha256'] != digest:
                        raise ValueError('Delivery input changed before review; no stale inspection')
                    if not observed.get('readable'):
                        break
                    if owners:
                        owners[0]['windows'].extend(observed['windows'])
                        owners[0]['windows'].sort(key=lambda window: window['offset_bytes'])
                    else:
                        saved['evidence'].append(observed)
                        owners = [observed]
                    owner = owners[0]
                    owner['complete'] = self._complete_target(owner, owners)
                    if owner['complete']:
                        owner.pop('limitation', None)
                    self.store.write_json(self._path(identity, 'input'), saved)
            saved['initial_subjects'] = [row['subject'] for row in [*saved['targets'], *saved['evidence']]]
            self._mark_evidence(saved)
            self.store.write_json(self._path(identity, 'input'), saved)
            return
        originals = []
        for path, digest in delivery['artifacts'].items():
            if path in targets or Path(path).suffix.lower() in _BINARY:
                continue
            owners = [row for row in saved['evidence']
                if row.get('readable') and row.get('path') == path and row.get('sha256') == digest]
            if owners and self._complete_target(owners[0], owners):
                continue
            originals.append((path, digest, owners))
        self._prime_originals(saved, request, identity, remaining, client, offered, originals)
