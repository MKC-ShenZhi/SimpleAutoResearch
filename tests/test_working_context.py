from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from simple_ar.core.working_context import (
    CITATION_HANDOFF_POLICY, TURN_SUPPLY_POLICY, current_material_paths, history_view, processed_journal_turns, select_working_context)
from simple_ar.app.work_memory import (
    _bound_response, compact_work, checkpoint_arguments, checkpoint_artifacts, checkpoint_decisions, checkpoint_entries, observation_digest,
    prepare_checkpoint_input, summary_coverage, validate_checkpoint_sources)
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.integrations.llm import LLMClient, LLMSettings, estimate_tokens


def block(number, text):
    return [
        {'role': 'assistant', 'content': None, 'reasoning_content': 'recorded reasoning',
         'tool_calls': [{'id': str(number), 'type': 'function',
                         'function': {'name': 'observe', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': str(number),
         'content': json.dumps({'text': text})},
    ]


def observation(turn, text, **extra):
    return {'path': 'materials/source.txt', 'sha256': 'recorded-digest',
            'record': f'records/{turn}.json', 'turn': turn, 'text': text,
            'offset_bytes': 0, 'next_offset_bytes': len(text.encode('utf-8')), **extra}


def select(**kwargs):
    arguments = dict(user_requests=['Compare the source measurements', 'Keep the error bars'],
                     system_text='Respect supplied permissions',
                     base_context={'permissions': {'write': ['outputs/']}},
                     turn_blocks=[], observations=[], schema_tokens=30,
                     context_token_target=3000, estimate_tokens=len)
    arguments.update(kwargs)
    return select_working_context(**arguments)


def expand_history_view(view):
    if isinstance(view, list):
        return view
    groups = [dict(zip(view['columns'], row)) for row in view['rows']]
    return [{**group, 'observations': [
        dict(zip(view['observation_columns'][schema], values))
        for schema, values in group['observations']]} for group in groups]


class WorkingContextTests(unittest.TestCase):
    def test_source_authority_preserves_requests_and_atomic_history(self):
        requests = ['  Analyze the supplied measurements.\n', 'Retain uncertainty: ±1.',
                    'Explain the remaining limitations.']
        blocks = [block(0, 'First observation'), block(1, 'Second observation')]
        blocks[1][0]['reasoning'] = 'Additional recorded reasoning'
        blocks[1].insert(0, {'role': 'user', 'content': json.dumps(
            {'user_request': {'index': 1, 'text': requests[1]}})})
        before = deepcopy(blocks)
        messages, _ = select(context_authority='sources.v1', user_requests=requests,
            turn_blocks=blocks, block_request_counts=[1, 2], context_token_target=50000)
        self.assertEqual(messages[0], {'role': 'system', 'content': 'Respect supplied permissions'})
        self.assertEqual(messages[1]['role'], 'assistant')
        self.assertEqual(messages[1]['reasoning_content'], '')
        self.assertEqual(set(messages[1]), {'role', 'content', 'reasoning_content'})
        self.assertEqual(messages[2:], [
            {'role': 'user', 'content': requests[0]}, *blocks[0],
            {'role': 'user', 'content': requests[1]}, *blocks[1][1:],
            {'role': 'user', 'content': requests[2]}])
        self.assertEqual([message['content'] for message in messages if message['role'] == 'user'], requests)
        self.assertEqual(blocks, before)

    def test_source_authority_preserves_envelope_data_except_scope(self):
        arguments = dict(base_context={
                'permissions': {'write': ['outputs/']},
                'work_plan': {'steps': [{'step': 'Compare measurements', 'status': 'pending'}],
                              'scope': 'intention_not_verified_outcome'}},
            work_notes=[{'text': 'A provisional interpretation', 'records': ['records/0.json']}],
            observations=[observation(0, 'Recorded measurements')],
            material_policy='referenced', estimate_tokens=lambda text: 1)
        before = deepcopy(arguments)
        legacy, legacy_budget = select(**arguments)
        messages, budget = select(**arguments, context_authority='sources.v1')
        context = json.loads(messages[1]['content'])
        scope = context.pop('context_authority')
        self.assertEqual(scope['version'], 'sources.v1')
        self.assertIn('Runtime-derived working view', scope['scope'])
        self.assertIn('not caller instructions', scope['scope'])
        self.assertIn('independently enforced', scope['scope'])
        self.assertEqual(context, json.loads(legacy[1]['content']))
        self.assertEqual(messages[2:], legacy[2:])
        self.assertEqual(budget, legacy_budget)
        self.assertTrue(context['working_view']['notes'])
        self.assertTrue(context['working_view']['observations'])
        self.assertEqual(arguments, before)

    def test_context_authority_none_keeps_legacy_layout(self):
        arguments = dict(turn_blocks=[block(0, 'Recorded result')], context_token_target=50000)
        implicit = select(**arguments)
        explicit = select(**arguments, context_authority=None)
        self.assertEqual(json.dumps(implicit, ensure_ascii=False), json.dumps(explicit, ensure_ascii=False))
        envelope = implicit[0][1]
        self.assertEqual(envelope['role'], 'user')
        self.assertEqual(set(envelope), {'role', 'content'})
        self.assertNotIn('context_authority', json.loads(envelope['content']))

    def test_source_authority_cost_includes_complete_projection(self):
        for authority in (None, 'sources.v1'):
            with self.subTest(authority=authority):
                messages, budget = select(context_authority=authority,
                    base_context={'resources': {}}, turn_blocks=[block(0, 'Recorded result')],
                    trailing_messages=[{'role': 'system', 'content': 'Conclude within the supplied scope.'}],
                    context_token_target=50000)
                measured = 30 + len(json.dumps(messages, ensure_ascii=False))
                context = json.loads(messages[1]['content'])
                self.assertEqual(budget['estimated_input_tokens'], measured)
                self.assertEqual(context['working_context']['estimated_input_tokens'], measured)
                self.assertEqual(context['resources']['approximate_next_input_tokens'], measured)
                self.assertLessEqual(measured, budget['input_token_target'])

    def test_context_authority_rejects_unsupported_values(self):
        for authority in ('', 'sources.v2', False, [], {}):
            with self.subTest(authority=authority):
                with self.assertRaisesRegex(ValueError, 'Unknown context authority policy'):
                    select(context_authority=authority)

    def test_related_feedback_uses_whole_originals_and_does_not_block_later_body(self):
        from simple_ar.core.working_context import feedback_observations
        feedback = {'review_observations': [
            {'record': 'records/large/result.json', 'subjects': [{'current_matches_review': True}],
             'result': {'data': {'review': 'large original ' * 30000}}},
            {'record': 'records/relevant/result.json', 'subjects': [{'current_matches_review': True}],
             'result': {'data': {'review': 'Move the figure caption beside its image.'}}}],
            'working_notes': [{'text': 'A summary must not become an original observation'}]}
        original = deepcopy(feedback)
        rows = feedback_observations(feedback)
        messages, selection = select(observations=rows,
            selected_records=[row['record'] for row in rows], material_policy='referenced',
            context_token_target=10000)
        self.assertEqual(feedback, original)
        view = json.loads(messages[1]['content'])['working_view']['observations']
        self.assertEqual(len(view), 1)
        self.assertEqual(json.loads(view[0]['observation']['text']), feedback['review_observations'][1]['result'])
        self.assertTrue(any(row['source']['record'] == 'records/large/result.json'
            and row['status'] == 'capacity' for row in selection['omitted_records']))
        self.assertNotIn('A summary must not become an original observation', json.dumps(messages))

    def test_related_feedback_preserves_changed_subject_history_without_certifying_it(self):
        from simple_ar.core.working_context import feedback_observations
        stale = {'record': 'records/stale/result.json',
            'subjects': [{'path': 'workspace/figure.png', 'current_matches_review': False}],
            'result': {'data': {'review': 'Earlier figure observation'}}}
        current = {'record': 'records/current/result.json',
            'subjects': [{'path': 'workspace/figure.png', 'current_matches_review': True}],
            'result': {'data': {'review': 'Current bound observation'}}}
        rows = feedback_observations({'review_observations': [stale, current]})
        self.assertEqual([row['record'] for row in rows], [current['record'], stale['record']])
        self.assertFalse(rows[1]['subjects'][0]['current_matches_review'])
        self.assertNotIn('sha256', rows[1])
        self.assertIn('not source evidence', rows[1]['evidence_role'])

    def test_inspection_history_can_recover_nonfinite_numeric_results(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        owner = InspectionToolGateway(root, SimpleNamespace(), Mock(), protocol='subjects.v3')
        saved = {'identity':'numeric-history', 'inspection_history_policy':'receipts.v1',
                 'source_rounds':['unchanged']}
        value = {'tool_name':'run_process', 'status':'ok', 'data':{'value':float('nan')}}
        trace = {'results':[{'result':value}]}
        with patch.object(owner, '_history_round', return_value=trace), patch.object(
                owner, '_history_receipt', return_value=(None, 'round_only')):
            page = owner._read_history(saved, {'start_turn':0, 'action':0, 'field':'result'}, before_turn=1)
        self.assertTrue(__import__('math').isnan(json.loads(page.data['text'])['data']['value']))

    def test_inspection_history_projects_selected_owner_values_without_replaying_offers(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store, identity = ArtifactStore(root), 'own-inspection'
        ledger = BudgetLedger({'llm_requests':3, 'total_tokens':1000})
        ledger.reserve('round-0', {'llm_requests':1, 'total_tokens':100},
            attempt_id=identity, logical_call_id='inspect-artifacts-'+identity)
        ledger.settle('round-0', {'llm_requests':1, 'total_tokens':80})
        arguments = [{'path':'measurements/missing.txt'}, {'path':'measurements/original.txt', 'max_bytes':8}]
        results = [
            {'tool_name':'read_file', 'status':'error', 'error':'原始错误: missing original', 'data':{}},
            {'tool_name':'read_file', 'status':'ok', 'data':{'text':'原始'*2000,
                'next_offset_bytes':12000, 'size_bytes':24000, 'has_more':True}}]
        calls = [{'id':str(i), 'type':'function', 'function':{'name':'read_file',
            'arguments':json.dumps(args)}} for i,args in enumerate(arguments)]
        trace = {'offer':{'messages':[{'role':'user', 'content':'Repeated input. '*60000}]},
            'completed':True, 'response':{'role':'assistant', 'tool_calls':calls},
            'results':[{'call_id':str(i), 'arguments':args, 'result':result}
                for i,(args,result) in enumerate(zip(arguments,results))]}
        relative = f'tools/inspection/{identity}/sources-0.json'
        store.write_json(relative, trace)
        original = store.resolve(relative).read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        saved = {'identity':identity, 'inspection_history_policy':'receipts.v1', 'source_rounds':[digest]}
        owner = InspectionToolGateway(root, SimpleNamespace(ledger=ledger),
            Mock(side_effect=AssertionError('History must not request new work')), protocol='subjects.v3')
        before = ledger.to_dict()
        index = owner._read_history(saved, {}, before_turn=1).data
        entries = json.loads(index['text'])
        self.assertEqual(index['projection'], 'navigation_index')
        self.assertLess(len(index['text'].encode('utf-8')), 2000)
        self.assertNotIn('Repeated input.', index['text'])
        self.assertEqual(entries[0]['sha256'], digest)
        self.assertEqual([row['action'] for row in entries[0]['actions']], [0,1])
        self.assertEqual(entries[0]['actions'][0]['status'], 'error')
        for action in range(2):
            for field in ('result', 'arguments'):
                value = trace['results'][action][field]
                canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':'), allow_nan=False).encode('utf-8')
                payload = bytearray()
                while True:
                    page = owner._read_history(saved, {'action':action, 'field':field,
                        'offset_bytes':len(payload), 'max_bytes':4000}, before_turn=1).data
                    piece = page['text'].encode('utf-8')
                    self.assertEqual(piece, canonical[len(payload):page['next_offset_bytes']])
                    self.assertLessEqual(len(piece), 4000)
                    self.assertEqual(page['projection'], 'retained_value')
                    self.assertEqual(page['sha256'], digest)
                    self.assertEqual(page['pointer'], f'/results/{action}/{field}')
                    self.assertEqual(page['projection_sha256'], hashlib.sha256(canonical).hexdigest())
                    payload.extend(piece)
                    if not page['has_more_bytes']:
                        break
                self.assertEqual(json.loads(payload), value)
                self.assertNotIn(b'Repeated input.', payload)
        self.assertEqual(owner._conversation_rows(saved)[0]['record'], relative+'#results/0')
        for proposal in ({'path':'../.env'}, {'identity':'another-inspector'}, {'start_turn':1}, {'action':2}):
            with self.subTest(proposal=proposal), self.assertRaises(ValueError):
                owner._read_history(saved, proposal, before_turn=1)
        with self.assertRaisesRegex(ValueError, 'saved contract'):
            owner._read_history({'identity':identity, 'source_rounds':[digest]}, {}, before_turn=1)
        self.assertEqual(ledger.to_dict(), before)
        ledger.reserve('unfinished', {'llm_requests':1, 'total_tokens':100},
            attempt_id=identity, logical_call_id='inspect-artifacts-'+identity)
        unsettled = ledger.to_dict()
        with self.assertRaisesRegex(ValueError, 'unsettled'):
            owner._read_history(saved, {}, before_turn=1)
        self.assertEqual(ledger.to_dict(), unsettled)
        self.assertEqual(store.resolve(relative).read_bytes(), original)
        owner.client_factory.assert_not_called()

    def test_retained_rollback_and_final_bookkeeping_fit_the_complete_projection(self):
        # Calibrate from a real, fully serialized admission, not a guessed
        # envelope. Sixteen rejected windows make capacity -> not_requested
        # growth observable at the boundary after an older native trial fails.
        for target, size, estimator, scale in (
                (12000, 37, len, 1), (24000, 700, len, 1),
                (8000, 113, estimate_tokens, 4)):
            with self.subTest(target=target, size=size, scale=scale):
                latest = block(1, 'latest result')
                rows = [observation(None, 'a' * size, chunk_id='first',
                            role='historical_observation'),
                        observation(None, 'second complete window', chunk_id='second',
                            offset_bytes=size, role='historical_observation')]
                rows += [observation(None, 'large' * target, chunk_id=str(i),
                             record=f'records/omitted-{i}.json', role='historical_observation')
                         for i in range(16)]
                notes = [{'text': 'optional judgment', 'records': [], 'current': True}]
                tail = [{'role': 'user', 'content': 'Submit the final opinion.'}]
                measured_admissions = []

                def measure(text):
                    # Only message-list projections have this framing; other
                    # estimator calls measure raw rows/notes/native blocks.
                    if text.startswith('[{"role": "system"'):
                        messages = json.loads(text)
                        context = json.loads(messages[1]['content'])
                        omitted = context['material_supply']['omitted']
                        if (context['working_view']['notes'] and len(omitted) == 16
                                and all(row['status'] == 'capacity' for row in omitted)
                                and not any(message.get('tool_call_id') == '0' for message in messages)):
                            measured_admissions.append(30 + estimator(text))
                    return estimator(text)

                arguments = dict(material_policy='retained', require_work_notes=True,
                    fit_work_notes=True, processed_turns=[], native_observations=[],
                    turn_blocks=[block(0, 'old' * (target * 8)), latest], observations=rows,
                    current_artifacts=['materials/source.txt'], work_notes=notes,
                    version_resolver=lambda _: 'recorded-digest', trailing_messages=tail,
                    context_token_target=target, estimate_tokens=measure)
                select(**arguments)
                self.assertTrue(measured_admissions)
                # Leave only twelve serialized characters of headroom. The
                # historical status-reset bug adds up to eighty characters.
                growth = (target - measured_admissions[-1]) * scale - 12
                self.assertGreater(growth, 0)
                rows[0]['text'] += 'a' * growth
                rows[0]['next_offset_bytes'] = len(rows[0]['text'])
                before = deepcopy((rows, arguments['turn_blocks'], notes, tail))
                messages, budget = select(**arguments)
                context = json.loads(messages[1]['content'])
                self.assertLessEqual(budget['estimated_input_tokens'], target)
                self.assertGreaterEqual(budget['estimated_input_tokens'],
                    30 + estimator(json.dumps(messages, ensure_ascii=False)))
                self.assertEqual(budget['selected_turns'], [1])
                self.assertTrue(budget['compaction_needed'])
                self.assertEqual(budget['compaction_turns'], [0])
                self.assertTrue(all(row['status'] == 'capacity' for row in budget['omitted_records']))
                self.assertEqual([entry['observation'] for entry in context['working_view']['observations']],
                                 rows[:2])
                self.assertEqual(context['working_view']['notes'], notes)
                self.assertEqual(messages[-4:-2], latest)
                self.assertEqual(messages[-1], tail[0])
                self.assertEqual((rows, arguments['turn_blocks'], notes, tail), before)
                # A tighter target must retain the other window of the same
                # file; it cannot reject the file as a single large object.
                smaller, reduced = select(**{**arguments, 'context_token_target': target // 2})
                self.assertLessEqual(reduced['estimated_input_tokens'], target // 2)
                supplied = json.loads(smaller[1]['content'])['working_view']['observations']
                self.assertEqual([entry['observation'] for entry in supplied], rows[1:2])
                navigation = [{'turn': 0, 'request_count': 1, 'reply': 'records/reply.json',
                    'observation': {'tool': 'list_directory', 'arguments': {'path': body}}}
                    for body in ('x' * (target * 16), 'materials/')]
                navigated, indexed = select(**{**arguments, 'context_token_target': target * 2,
                                               'operation_history': navigation})
                overview = json.loads(navigated[1]['content'])['working_view']['operation_overview']
                self.assertLessEqual(indexed['estimated_input_tokens'], target * 2)
                self.assertEqual(overview['omitted_entries'], 1)
                entries = expand_history_view(overview['entries'])
                self.assertEqual(entries[0]['observations'][0]['arguments'], {'path': 'materials/'})
                # When required input itself cannot fit, preserve it and report
                # the overflow instead of hiding it through optional eviction.
                overflow, required = select(**{**arguments, 'context_token_target': 1})
                self.assertGreater(required['required_input_tokens'], 1)
                self.assertTrue(required['context_exceeds_input_target'])
                self.assertEqual(overflow[-4:-2], latest)
                self.assertEqual(overflow[-1], tail[0])

    def test_required_notes_are_charged_once_before_retained_originals(self):
        original = observation(0, 'Original measurement ' * 200,
            role='historical_observation')
        notes = [{'text':'Working judgment ' * 80, 'records':[original['record']],
            'observed_versions':{'source': 'hash' * 1000}, 'current':False,
            'scope':'model_note_not_verified_evidence',
            'version_scope':'Referenced files changed; reassess before reuse'}]
        before = deepcopy(notes)
        arguments = dict(material_policy='retained', observations=[original], work_notes=notes,
            version_resolver=lambda path:'recorded-digest', require_work_notes=True)
        messages, metadata = select(**arguments, context_token_target=2300)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(view['notes'], [{key:value for key,value in notes[0].items()
                                        if key != 'observed_versions'}])
        self.assertFalse(view['observations'])
        self.assertEqual(metadata['notes'][0]['status'], 'working_view')
        self.assertEqual(metadata['selected_records'], [])
        self.assertEqual(metadata['omitted_records'][0]['status'], 'capacity')
        self.assertGreaterEqual(metadata['required_input_tokens'], len(notes[0]['text']))
        self.assertEqual(notes, before)
        roomy, _ = select(**arguments, context_token_target=10000)
        roomy_view = json.loads(roomy[1]['content'])['working_view']
        self.assertEqual(len(roomy_view['notes']), 1)
        self.assertEqual(roomy_view['observations'][0]['observation']['text'], original['text'])
        # Required notes remain even when the mandatory context itself overflows.
        tiny, budget = select(**arguments, context_token_target=100)
        self.assertEqual(json.loads(tiny[1]['content'])['working_view']['notes'], view['notes'])
        self.assertGreater(budget['required_input_tokens'], 100)

    def test_explicit_optional_notes_preserve_all_legacy_policy_bytes(self):
        for policy in ('ranked', 'referenced', 'scoped', 'retained'):
            for through in (0, 1):
                with self.subTest(policy=policy, through=through):
                    arguments = dict(material_policy=policy,
                        observations=[observation(0, 'Archived source', role='historical_observation')],
                        turn_blocks=[block(0, 'old ' * 400), block(1, 'latest')],
                        work_notes=[{'text':'Remember scope', 'records':['records/0.json'],
                            'observed_versions':{'source':'hash'}}],
                        summarized_through_turn=through,
                        version_resolver=lambda path:'recorded-digest')
                    self.assertEqual(json.dumps(select(**arguments), ensure_ascii=False),
                        json.dumps(select(**arguments, require_work_notes=False), ensure_ascii=False))
        # New mandatory presentation does not make notes a replacement for history.
        _, budget = select(material_policy='retained', require_work_notes=True,
            summarized_through_turn=1, work_notes=[{'text':'Judgment', 'records':[]}],
            turn_blocks=[block(0, 'old'), block(1, 'latest')], context_token_target=10000)
        self.assertEqual(budget['selected_turns'], [0, 1])

    def test_scoped_originals_precede_optional_navigation_and_report_real_omissions(self):
        first = observation(0, 'Original observed measurement and its scope.')
        unused = observation(1, 'An unrelated, unreferenced observation.')
        stale = observation(2, 'A stale observation.')
        stale['sha256'] = 'old-digest'
        diagnostic = {'kind': 'operation_result', 'record': 'records/process.json', 'turn': 1,
            'text': json.dumps({'returncode': 1, 'stderr': 'Original execution diagnosis'})}
        navigation = [{'turn': 0, 'request_count': 1, 'reply': 'records/reply.json',
            'observation': {'tool': 'list_directory', 'arguments': {'large': 'x' * 5000}}}]
        messages, metadata = select(material_policy='scoped', context_token_target=4000,
            turn_blocks=[block(0, 'old native ' * 600), block(1, 'older native ' * 600), block(2, 'latest')],
            observations=[first, unused, stale, diagnostic],
            selected_records=[first['record'], stale['record'], diagnostic['record'], 'records/missing.json'],
            version_resolver=lambda path: 'recorded-digest', operation_history=navigation,
            block_request_counts=[1, 1, 1])
        view = json.loads(messages[1]['content'])
        self.assertIn(first['text'], json.dumps(view['working_view']['observations']))
        self.assertIn('Original execution diagnosis', json.dumps(view['working_view']['observations']))
        self.assertTrue(any(row['source']['record'] == diagnostic['record'] and row['version_status'] == 'historical'
                            for row in metadata['selected_records']))
        statuses = {row['source']['record']: row['status'] for row in metadata['omitted_records']}
        self.assertEqual(statuses[unused['record']], 'unreferenced')
        self.assertEqual(statuses[stale['record']], 'stale')
        missing = [row for row in view['material_supply']['references'] if row['record'] == 'records/missing.json']
        self.assertEqual(missing[0]['status'], 'missing')
        self.assertEqual(view['working_view']['operation_overview']['omitted_entries'], 1)
        self.assertLessEqual(metadata['estimated_input_tokens'], 4000)

    def test_shared_history_renderer_keeps_legacy_bytes_and_omits_only_navigation_streams(self):
        call = {'function': {'name': 'run_process', 'arguments': json.dumps({
            'argv': ['python', 'worker.py'], 'cwd': 'workspace'})}}
        data = {'returncode': 0, 'stdout': 'Observed output', 'stderr': '',
                'streams': {'stdout': {'bytes_seen': 15, 'tail_bytes_discarded': 0}}}
        result = {'status': 'ok', 'data': data}
        before = deepcopy(result)
        historical = ToolApplication._history_observation(call, result, 'attempts/tool/result.json')
        self.assertEqual(historical['observed'], data)
        navigation = ToolApplication._history_observation(call, result,
            'attempts/tool/result.json', navigation=True)
        self.assertEqual(navigation['observed'], {key: value for key, value in data.items() if key != 'streams'})
        self.assertEqual(navigation['omitted_fields']['data'], ['streams'])
        self.assertEqual(result, before)
        groups = [{'reply': 'records/reply.json', 'request_count': 1,
                   'observations': [historical] * 8}]
        expected = {'columns': ['reply', 'request_count', 'observations'],
            'observation_columns': [list(historical)],
            'rows': [['records/reply.json', 1, [[0, list(historical.values())]] * 8]]}
        self.assertIs(ToolApplication._history_view, history_view)
        self.assertEqual(json.dumps(ToolApplication._history_view(groups), ensure_ascii=False),
                         json.dumps(expected, ensure_ascii=False))
        self.assertEqual(expand_history_view(history_view(groups)), groups)
        small = [{'reply': 'r', 'request_count': 1, 'observations': []}]
        self.assertEqual(history_view(small), small)

    def test_final_instruction_is_admitted_with_material_and_atomic_history(self):
        latest = block(2, 'Current observed result')
        rows = [observation(0, 'Original measured source with conditions. ' * 600)]
        arguments = dict(turn_blocks=[block(0, 'old'), latest], observations=rows,
                         context_token_target=6000)
        baseline, before_budget = select(**arguments)
        tail = [{'role': 'user', 'content': 'Conclude using the retained sources. ' +
                 'x' * (6000 - before_budget['estimated_input_tokens'] + 512)}]
        before = deepcopy((arguments, tail))
        self.assertGreater(len(json.dumps([*baseline, *tail], ensure_ascii=False)) + 30, 6000)
        messages, budget = select(**arguments, trailing_messages=tail)
        self.assertEqual(messages[-1], tail[0])
        self.assertEqual(messages[-4:-2], latest)
        self.assertLessEqual(budget['estimated_input_tokens'], 6000)
        self.assertGreaterEqual(budget['estimated_input_tokens'],
                                30 + len(json.dumps(messages, ensure_ascii=False)))
        self.assertLessEqual(len(json.dumps(messages[1])), len(json.dumps(baseline[1])))
        self.assertEqual((arguments, tail), before)
        messages[-1]['content'] = 'changed returned view'
        self.assertEqual((arguments, tail), before)
        # Required instructions cannot be silently dropped when even the
        # mandatory contract does not fit, nor split a native tool pair.
        overflow, budget = select(**{**arguments, 'context_token_target': 10}, trailing_messages=tail)
        self.assertEqual(overflow[-1], tail[0])
        self.assertEqual(overflow[-4:-2], latest)
        self.assertTrue(budget['context_exceeds_input_target'])
        for invalid in ([{'role': 'tool', 'content': 'unpaired result'}],
                        [{'role': 'assistant', 'content': 'unrecorded work'}]):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                select(trailing_messages=invalid)

    def test_source_survives_archive_without_recurring_full_transcript(self):
        source = 'Measured source value = 17.25 with uncertainty 0.5'
        blocks = [block(0, source)] + [block(i, 'unrelated payload ' * 100) for i in range(1, 8)]
        blocks.append(block(8, 'Latest accepted output'))
        row = observation(0, source)
        messages, budget = select(turn_blocks=blocks, observations=[row],
                                  work_notes=[{'text': 'Use original measurement',
                                               'records': [row['record']], 'current': True}])
        rendered = json.dumps(messages, ensure_ascii=False)
        self.assertEqual(rendered.count(source), 1)
        self.assertNotIn('unrelated payload', rendered)
        self.assertEqual(messages[-3:-1], blocks[-1])
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'Keep the error bars'})
        self.assertEqual(budget['selected_turns'], [8])
        self.assertEqual(budget['selected_records'][0]['source']['record'], row['record'])
        self.assertLessEqual(budget['estimated_input_tokens'], 3000)
        self.assertGreaterEqual(budget['estimated_input_tokens'],
            30+len(json.dumps(messages,ensure_ascii=False)))
        self.assertEqual(json.loads(messages[1]['content'])['working_context']['estimated_input_tokens'],
            budget['estimated_input_tokens'])

    def test_oversized_material_never_erases_requests_permissions_or_latest_pair(self):
        latest = block(1, 'Current feedback result')
        messages, budget = select(turn_blocks=[block(0, 'old'), latest],
                                  observations=[observation(0, 'huge source ' * 1000)],
                                  context_token_target=10)
        self.assertEqual(messages[-3:-1], latest)
        self.assertIn({'role': 'user', 'content': 'Keep the error bars'}, messages)
        self.assertEqual(json.loads(messages[1]['content'])['permissions'], {'write': ['outputs/']})
        self.assertTrue(budget['context_exceeds_input_target'])
        self.assertGreater(budget['target_overflow_tokens'], 0)
        self.assertEqual(budget['omitted_records'][0]['status'], 'capacity')

    def test_changed_versions_excluded_and_all_inputs_unchanged(self):
        rows = [observation(0, 'Obsolete measurement'),
                observation(1, 'Current measurement', sha256='new-digest')]
        blocks = [block(0, rows[0]['text']), block(1, rows[1]['text']),
                  block(2, 'old oversized material ' * 400), block(3, 'latest')]
        notes = [{'text': 'Supplied claim', 'records': ['records/0.json'], 'current': True}]
        before = deepcopy((rows, blocks, notes))
        original_bytes = json.dumps(before, sort_keys=True).encode('utf-8')
        calls = []

        def resolver(path):
            calls.append(path)
            return 'new-digest'

        messages, budget = select(turn_blocks=blocks, observations=rows, work_notes=notes,
                                  version_resolver=resolver, context_token_target=6000)
        rendered = json.dumps(messages)
        self.assertNotIn('Obsolete measurement', rendered)
        self.assertIn('Current measurement', rendered)
        self.assertEqual(budget['omitted_records'][0]['status'], 'stale')
        self.assertEqual(calls, ['materials/source.txt'])
        self.assertEqual((rows, blocks, notes), before)
        self.assertEqual(json.dumps((rows, blocks, notes), sort_keys=True).encode('utf-8'), original_bytes)
        # Returned structures are also detached from the canonical inputs.
        messages[-1]['content'] = 'modified output'
        budget['history_refs'][0]['path'] = 'modified metadata'
        self.assertEqual((rows, blocks, notes), before)

    def test_notes_and_unresolved_versions_cannot_manufacture_evidence(self):
        notes = [{'text': 'Claim: value is 9000', 'records': ['missing-record'],
                  'status': 'current', 'current': True}]
        messages, budget = select(work_notes=notes, observations=[{'record': 'empty', 'turn': 0}],
                                  context_token_target=6000)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(view['observations'], [])
        self.assertEqual(view['notes'], notes)
        self.assertEqual(budget['selected_records'], [])
        messages, budget = select(observations=[observation(0, 'Actual observed text')],
                                  version_resolver=lambda _: None)
        self.assertEqual(budget['selected_records'][0]['version_status'], 'unknown')
        self.assertIn('Actual observed text', json.dumps(messages))

    def test_note_references_outrank_other_sources_and_duplicate_read_is_not_reinserted(self):
        rows = [observation(0, 'unselected payload ' * 100),
                observation(1, 'Selected original evidence'),
                observation(2, 'Selected original evidence')]
        messages, budget = select(observations=rows, turn_blocks=[[], [], [], block(3, 'latest')],
                                  work_notes=[{'text': 'Use evidence', 'records': ['records/1.json']}])
        rendered = json.dumps(messages)
        self.assertEqual(rendered.count('Selected original evidence'), 1)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(view['observations'][0]['observation']['record'], 'records/1.json')
        self.assertTrue(any(row['status'] == 'duplicate' for row in budget['omitted_records']))

    def test_latest_observation_is_not_projected_twice_and_stale_version_is_marked(self):
        row = observation(0, 'Original latest evidence\n"quoted" \\ path')
        for supply in (None, [{key: value for key, value in row.items() if key != 'text'}]):
            with self.subTest(supply=supply is not None):
                messages, budget = select(turn_blocks=[block(0, row['text'])], observations=[row],
                    native_observations=supply, version_resolver=lambda _: 'changed')
                view = json.loads(messages[1]['content'])['working_view']
                self.assertEqual(view['observations'], [])
                self.assertEqual(view['stale_native_observations'][0]['record'], row['record'])
                self.assertEqual(budget['selected_records'][0]['version_status'], 'stale')
                self.assertEqual(messages[-3:-1], block(0, row['text']))

    def test_recent_coherent_work_sequence_keeps_reasoning_and_directory_process_outcomes(self):
        blocks = [block(0, 'old full material ' * 1000)]
        rows = []
        for i, result in enumerate(({'entries': ['input.csv', 'report.md']},
                                    {'returncode': 0, 'stdout': 'computed result: 17.25'},
                                    {'accepted': True, 'artifact': 'outputs/figure.svg'}), 1):
            turn = block(i, '')
            turn[0]['reasoning_content'] = f'Actual reasoning for accepted step {i}'
            turn[1]['content'] = json.dumps(result)
            blocks.append(turn)
            rows.append({'kind': 'operation_result', 'record': f'records/{i}.json',
                         'turn': i, 'text': json.dumps(result, indent=2)})
        messages, budget = select(turn_blocks=blocks, observations=rows, context_token_target=6000,
                                  block_request_counts=[1] * len(blocks))
        native = [message for message in messages if message['role'] in {'assistant', 'tool'}]
        self.assertEqual(native, [message for turn in blocks[1:] for message in turn])
        self.assertEqual(budget['selected_turns'], [1, 2, 3])
        self.assertEqual(budget['recent_start_turn'], 1)
        self.assertEqual(budget['archive_end_turn'], 1)
        self.assertLessEqual(budget['recent_native_tokens'], 3000)
        self.assertTrue(all(row['status'] == 'native' for row in budget['selected_records']))
        self.assertEqual(json.loads(messages[1]['content'])['working_view']['observations'], [])
        self.assertEqual(messages[-1]['content'], 'Keep the error bars')

    def test_feedback_is_interleaved_once_after_previous_native_work(self):
        requests = ['Initial goal', 'Revise the accepted draft', 'Current pending feedback']
        blocks = [block(0, 'initial work'), block(1, 'draft accepted'),
                  [{'role': 'user', 'content': json.dumps({'user_request': {'index': 1, 'text': requests[1]}})},
                   *block(2, 'revision work')], block(3, 'revision accepted')]
        before = deepcopy(blocks)
        messages, _ = select(user_requests=requests, turn_blocks=blocks,
                             block_request_counts=[1, 1, 2, 2], context_token_target=10000)
        self.assertEqual(messages[2:], [
            {'role': 'user', 'content': requests[0]}, *blocks[0], *blocks[1],
            {'role': 'user', 'content': requests[1]}, *blocks[2][1:], *blocks[3],
            {'role': 'user', 'content': requests[2]},
        ])
        self.assertEqual(blocks, before)
        # The compatibility fallback also leaves pending feedback after work.
        fallback, _ = select(turn_blocks=[block(0, 'previous work')])
        self.assertEqual(fallback[-1]['content'], 'Keep the error bars')

    def test_archived_operation_result_precedes_lexical_sources_and_checkpoint_survives(self):
        outcome = json.dumps({'returncode': 1, 'stderr': 'actual diagnostic', 'outputs': []})
        rows = [observation(0, 'Compare source measurements ' * 500),
                {'kind': 'operation_result', 'record': 'records/process.json', 'turn': 1, 'text': outcome}]
        checkpoint = {'through_turn': 0, 'text': 'Caller-supplied historical checkpoint'}
        messages, budget = select(turn_blocks=[block(0, 'source'), block(1, outcome),
                                               block(2, 'old oversized read ' * 1000), block(3, 'latest')],
                                  observations=rows, base_context={'checkpoint': checkpoint},
                                  context_token_target=4000)
        context = json.loads(messages[1]['content'])
        supplied = context['working_view']['observations']
        self.assertEqual(context['checkpoint'], checkpoint)
        self.assertEqual(supplied[0]['observation']['text'], outcome)
        self.assertEqual(supplied[0]['observation']['kind'], 'operation_result')
        self.assertNotIn('record_text_slice', supplied[0]['observation'])
        self.assertEqual(budget['recent_start_turn'], 3)
        self.assertLessEqual(budget['estimated_input_tokens'], 4000)

    def test_committed_summary_replaces_old_presentation_without_erasing_receipt_access(self):
        original = 'Original measurement and its conditions. ' * 40
        failure = json.dumps({'status':'error','error':'Unresolved original diagnostic'})
        rows = [observation(0, original),
                {'kind':'operation_result','record':'records/failure.json','turn':1,'text':failure}]
        blocks = [block(0,original),block(1,failure),block(2,'Previous complete work'),block(3,'Current correction')]
        notes = [{'text':'Measurement observed; original diagnostic remains unresolved',
                  'records':['records/0.json','records/failure.json'],'current':True}]
        before = deepcopy((rows,blocks,notes))
        messages, budget = select(turn_blocks=blocks,observations=rows,work_notes=notes,
            summarized_through_turn=3,context_token_target=20000)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(budget['selected_turns'],[3])
        self.assertEqual(view['notes'],notes)
        self.assertEqual(view['history']['notes_through_turn'],3)
        self.assertNotIn(original,json.dumps(messages))
        self.assertEqual({r['source']['record'] for r in budget['omitted_records']},
                         {'records/0.json','records/failure.json'})
        self.assertTrue(all(r['status']=='summarized' for r in budget['omitted_records']))
        self.assertEqual(messages[-3:-1],blocks[-1])
        self.assertEqual(messages[-1]['content'],'Keep the error bars')
        self.assertEqual((rows,blocks,notes),before)
        mixed, _ = select(turn_blocks=blocks,observations=rows,
            work_notes=[*notes,{'text':'Changed source requires reassessment','records':['records/old.json'],'current':False}],
            summarized_through_turn=3,context_token_target=20000)
        self.assertFalse(json.loads(mixed[1]['content'])['working_view']['notes'][-1]['current'])
        self.assertNotIn(original,json.dumps(mixed))
        tight, meta = select(turn_blocks=blocks,observations=rows,work_notes=notes,
            summarized_through_turn=3,context_token_target=10)
        self.assertEqual(json.loads(tight[1]['content'])['working_view']['notes'],notes)
        self.assertTrue(meta['context_exceeds_input_target'])
        # An explicit current selection can still recover exact original text.
        restored, _ = select(turn_blocks=blocks,observations=rows,work_notes=notes,
            summarized_through_turn=3,selected_records=['records/0.json'],context_token_target=20000)
        self.assertEqual(json.dumps(restored).count(original),1)
        # Empty/invalidated notes cannot silently retire prior working context.
        for unusable in ([],[{**notes[0],'current':False}]):
            with self.subTest(notes=unusable):
                fallback, meta = select(turn_blocks=blocks,observations=rows,work_notes=unusable,
                    summarized_through_turn=3,context_token_target=20000)
                self.assertIn(original,json.dumps(fallback))
                self.assertEqual(json.loads(fallback[1]['content'])['working_view']['history']['notes_through_turn'],0)
        for bad in (True,-1,5):
            with self.subTest(coverage=bad), self.assertRaises(ValueError):
                select(turn_blocks=blocks,work_notes=notes,summarized_through_turn=bad)

    def test_source_excerpt_offsets_address_exact_original_text_and_budget(self):
        text = 'Unrelated preamble. ' * 800 + 'measurement signal 17.25' + ' Tail.' * 800
        row = observation(0, text)
        original = deepcopy(row)
        messages, budget = select(user_requests=['measurement signal'], observations=[row],
                                  turn_blocks=[block(0, text), block(1, 'oversized barrier ' * 1000),
                                               block(2, 'latest')], context_token_target=6000)
        supplied = json.loads(messages[1]['content'])['working_view']['observations'][0]['observation']
        span = supplied['record_text_slice']
        self.assertTrue(span['truncated'])
        self.assertEqual(supplied['text'], text[span['character_start']:span['character_end']])
        self.assertEqual(span['total_characters'], len(text))
        self.assertEqual(budget['selected_records'][0]['source']['record_text_slice'], span)
        self.assertEqual(row, original)
        self.assertLessEqual(budget['estimated_input_tokens'], 6000)

    def test_archive_cutoff_is_zero_without_any_omitted_native_work(self):
        for blocks in ([], [[], []], [[], block(1, 'recent work')]):
            with self.subTest(blocks=blocks):
                messages, budget = select(turn_blocks=blocks, context_token_target=6000)
                self.assertIs(type(budget['recent_start_turn']), int)
                self.assertEqual(budget['recent_start_turn'], 0)
                self.assertEqual(budget['archive_end_turn'], 0)
                history = json.loads(messages[1]['content'])['working_view']['history']
                self.assertEqual(history['recent_start_turn'], 0)
                self.assertEqual(history['archived_turn_ranges'], [])

    def test_operation_receipt_file_metadata_is_historical_without_current_version_claim(self):
        row = {'kind': 'operation_result', 'record': 'records/result.json', 'turn': 0,
               'path': 'outputs/result.csv', 'sha256': 'recorded-file-digest',
               'text': json.dumps({'returncode': 0, 'created': 'outputs/result.csv'})}

        def resolver(_):
            self.fail('Historical operation receipts must not be validated as current files')

        messages, budget = select(observations=[row], version_resolver=resolver)
        supplied = json.loads(messages[1]['content'])['working_view']['observations'][0]
        self.assertEqual(supplied['version_status'], 'historical')
        self.assertEqual(supplied['observation'], row)
        self.assertEqual(budget['selected_records'][0]['version_status'], 'historical')

    def test_absolute_path_is_resolved_and_explicit_staleness_is_not_upgraded(self):
        row = observation(0, 'Explicitly retired text', absolute_path='/authorized/source.txt', current=False)
        resolved = []

        def resolver(path):
            resolved.append(path)
            return row['sha256']

        messages, budget = select(observations=[row], version_resolver=resolver)
        self.assertEqual(resolved, ['/authorized/source.txt'])
        self.assertNotIn(row['text'], json.dumps(messages))
        self.assertEqual(budget['omitted_records'][0]['status'], 'stale')

    def test_incomplete_tool_block_is_rejected_without_repair_or_fabricated_result(self):
        with self.assertRaises(ValueError):
            select(turn_blocks=[block(0, 'result')[:1]])


class RetainedWorkingContextTests(unittest.TestCase):
    def test_current_revision_keeps_related_recent_inputs_together_before_old_inventory(self):
        original = observation(0, 'Compare source measurements. ' * 210,
            role='historical_observation', path='old/report.md')
        revision = [observation(i + 1, f'Current revision component {i}. ' * 55,
            role='historical_observation', path=f'work/component-{i}.txt') for i in range(3)]
        feedback = {'kind': 'operation_result', 'turn': None, 'record': 'records/related-review.json',
            'text': 'Historical related opinion without a journal turn.'}
        arguments = dict(observations=[original, *revision, feedback],
            turn_blocks=[[], [], [], [], block(4, 'Apply the current revision')],
            current_artifacts=[original['path']], version_resolver=lambda _: 'recorded-digest',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[], native_observations=[], context_token_target=11000)
        before = deepcopy(arguments)
        messages, metadata = select(material_policy='working', **arguments)
        supplied = json.dumps(messages)
        self.assertTrue(all(row['text'] in supplied for row in revision))
        self.assertNotIn(original['text'], supplied)
        self.assertLessEqual(metadata['estimated_input_tokens'], 11000)
        # Explicit requests still win, and saved retained policy keeps its
        # inventory priority. Recency does not certify changed file versions.
        for policy, references in [('working', [original['record']]), ('retained', [])]:
            chosen, _ = select(material_policy=policy, selected_records=references, **arguments)
            self.assertIn(original['text'], json.dumps(chosen))
        changed, changed_metadata = select(material_policy='working', **{
            **arguments, 'version_resolver': lambda path: 'changed' if path == revision[1]['path'] else 'recorded-digest'})
        self.assertNotIn(revision[1]['text'], json.dumps(changed))
        self.assertTrue(any(row['source']['record'] == revision[1]['record'] and row['status'] == 'stale'
            for row in changed_metadata['omitted_records']))
        self.assertEqual(arguments, before)

    def test_working_inventory_prioritizes_current_bodies_without_permanently_pinning_them(self):
        original = observation(0, 'Observed original conditions. ' * 400,
            role='historical_observation')
        arguments = dict(turn_blocks=[block(0, original['text']), block(1, 'Investigated'), block(2, 'Next action')],
            observations=[original], current_artifacts=[original['path']],
            version_resolver=lambda _: 'recorded-digest',
            work_notes=[{'text':'Retained understanding with original conditions and remaining gap.',
                'records':[original['record']], 'handoff_policy':'citations.v1', 'current':True}],
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[0], context_token_target=50000)
        before = deepcopy(arguments)
        historical, old = select(material_policy='retained', **arguments)
        current, selected = select(material_policy='working', **arguments)
        self.assertIn(original['text'], json.dumps(historical))
        self.assertNotIn(original['text'], json.dumps(current))
        self.assertIn(arguments['work_notes'][0]['text'], json.dumps(current))
        self.assertEqual(selected['selected_turns'], [1, 2])
        self.assertLess(selected['estimated_input_tokens'], old['estimated_input_tokens'])
        self.assertEqual(arguments, before)
        # Explicit source selection, failed notes and changed versions each keep
        # their own behavior; an archived read is never silently re-certified.
        explicit, _ = select(material_policy='working', selected_records=[original['record']], **arguments)
        self.assertIn(original['text'], json.dumps(explicit))
        for notes in ([], [{**arguments['work_notes'][0], 'current':False}]):
            messages, _ = select(material_policy='working', **{**arguments, 'work_notes':notes})
            self.assertIn(original['text'], json.dumps(messages))
        messages, metadata = select(material_policy='working', **{
            **arguments, 'version_resolver':lambda _: 'changed-digest',
            'work_notes':[{**arguments['work_notes'][0], 'current':False}]})
        supplied = json.loads(messages[1]['content'])
        self.assertTrue(supplied['working_view']['stale_native_observations'])
        self.assertFalse(any(row['version_status']=='current' for row in metadata['selected_records']))

    def test_current_material_precedes_optional_notes_and_keeps_version_checks(self):
        current = observation(0, 'Current measured material. ' * 140,
            path='outputs/current.txt', role='historical_observation')
        old = observation(1, 'Earlier source. ' * 180, path='inputs/old.txt',
            record='records/old.json', role='historical_observation')
        notes = [{'text':'Previous model understanding. ' * 130, 'records':[old['record']]}]
        arguments = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[], observations=[old, current], work_notes=notes,
            version_resolver=lambda _:'recorded-digest', context_token_target=7000)
        for selection in ({'current_artifacts':[current['path']]}, {'selected_records':[current['record']]}):
            with self.subTest(selection=selection):
                messages, metadata = select(**arguments, **selection)
                view = json.loads(messages[1]['content'])['working_view']
                self.assertIn(current['text'], json.dumps(view))
                self.assertLessEqual(metadata['estimated_input_tokens'], 7000)
                stale, metadata = select(**{**arguments, 'version_resolver':lambda _:'changed'},
                    **selection)
                self.assertNotIn(current['text'], json.dumps(stale))
                self.assertTrue(any(row['source']['record'] == current['record']
                    and row['version_status'] == 'stale' for row in metadata['omitted_records']))

    def test_journal_retirement_requires_each_turns_actually_supplied_support(self):
        rows = [observation(i, f'Original receipt {i}.', kind='operation_result') for i in range(2)]
        notes = [{'text':'Continuity for '+str(i), 'records':[row['record']],
            'handoff_policy':CITATION_HANDOFF_POLICY} for i, row in enumerate(rows)]
        options = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[0, 1], observations=rows,
            turn_blocks=[block(0, rows[0]['text']), block(1, rows[1]['text']), block(2, 'Current work')],
            context_token_target=20000)
        before = deepcopy(options)
        for missing in ([], [{**notes[1], 'current':False}],
                        [{**notes[1], 'text':' '}],
                        [{**notes[1], 'text':'Oversized support. '*20000}]):
            with self.subTest(missing=bool(missing)):
                messages, selection = select(**options, work_notes=[notes[0], *missing])
                supplied = json.dumps(messages)
                self.assertIn(rows[1]['text'], supplied)
                # The native suffix cannot skip an uncovered intervening turn.
                self.assertTrue(1 in selection['selected_turns'] or any(
                    state['source']['record'] == rows[1]['record'] for state in selection['selected_records']))
                self.assertLessEqual(selection['estimated_input_tokens'], 20000)
        messages, _ = select(**options, work_notes=notes)
        self.assertTrue(all(row['text'] not in json.dumps(messages) for row in rows))
        self.assertEqual(options, before)

    def test_journal_navigation_exposes_support_gap_within_one_processed_turn(self):
        row = observation(0, 'Retained body of first action.', kind='operation_result')
        operations = [{'turn':0, 'reply':'reply.json', 'request_count':1,
            'observation':{'record':record}} for record in (row['record'], 'records/uncovered.json')]
        messages, selection = select(material_policy='retained', require_work_notes=True,
            fit_work_notes=True, processed_turns=[0], observations=[row], operation_history=operations,
            work_notes=[{'text':'Only one action is represented', 'records':[row['record']],
                'handoff_policy':CITATION_HANDOFF_POLICY}],
            turn_blocks=[block(0, 'Full original pair'), block(1, 'Current work')],
            context_token_target=20000)
        self.assertIn(row['text'], json.dumps(messages))
        self.assertIn(0, selection['selected_turns'])

    def test_journal_coverage_retires_only_processed_originals_and_keeps_needed_bodies(self):
        covered = observation(0, 'Covered original bytes.', path='covered.csv', role='historical_observation')
        fresh = observation(1, 'New original bytes.', path='fresh.csv', role='historical_observation')
        external = observation(None, 'Original outside this journal.', path='external.csv',
            record='records/external.json', role='historical_observation')
        arguments = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            observations=[covered, fresh, external],
            turn_blocks=[block(0, covered['text']), block(1, fresh['text']), block(2, 'Current target')],
            work_notes=[{'text':'Earlier interpretation, not original evidence.',
                'records':[covered['record']], 'current':True}],
            version_resolver=lambda _:'recorded-digest', context_token_target=40000)
        for processed in ([], [0]):
            with self.subTest(processed=processed):
                messages, metadata = select(**arguments, processed_turns=processed)
                supplied = json.dumps(messages)
                self.assertEqual(covered['text'] in supplied, not bool(processed))
                self.assertIn(fresh['text'], supplied)
                self.assertIn(external['text'], supplied)
                self.assertFalse(metadata['compaction_needed'])
                for selection in ({'selected_records':[covered['record']]},
                                  {'current_artifacts':[covered['path']]}):
                    required, _ = select(**arguments, processed_turns=processed, **selection)
                    self.assertIn(covered['text'], json.dumps(required))
        self.assertEqual(processed_journal_turns([
            {'turn':0, 'record':covered['record']}, {'turn':1, 'record':fresh['record']}],
            {covered['record']}), [0])

    def test_current_material_qualification_keeps_inventory_separate_from_selected_reads(self):
        def identity(path, count, presence='present'):
            return {'locator':path, 'absolute_path':'/'+path, 'presence':presence,
                'version_binding':'recorded', 'provenance':{'request_count':count}}
        rows = [identity('old.csv', 1), identity('new.py', 2), identity('missing.md', 2, 'missing')]
        before = deepcopy(rows)
        paths = current_material_paths(identities=rows, request_count=2, selected_paths=['/primary.md'])
        self.assertEqual(paths, {'new.py', '/new.py', '/primary.md'})
        self.assertEqual(rows, before)
        # Qualification grants neither new reads nor version validity. A
        # current output's old read window must still fail the shared resolver.
        row = observation(0, 'Old output bytes', path='new.py', role='historical_observation')
        for digest, expected in [('changed', 'stale'), (None, 'unknown')]:
            messages, selection = select(material_policy='retained', fit_work_notes=True,
                require_work_notes=True, processed_turns=[], current_artifacts=paths,
                observations=[row], version_resolver=lambda _, value=digest:value)
            self.assertNotIn(row['text'], json.dumps(messages))
            self.assertEqual(selection['omitted_records'][0]['version_status'], expected)

    def test_processed_prefix_is_navigation_not_a_demand_to_refill_the_window(self):
        text = 'Original historical source. '*300
        row = observation(0, text, role='historical_observation')
        notes = [{'text':'Earlier work remains a model judgment; consult its original.',
            'records':[row['record']], 'current':True}]
        journal = [{'turn':0, 'record':row['record']}, {'turn':0, 'record':'records/other.json'},
            {'turn':1, 'record':'records/current.json'}]
        self.assertEqual(processed_journal_turns(journal, {row['record']}), [])
        processed = processed_journal_turns(journal, {row['record'], 'records/other.json'})
        self.assertEqual(processed, [0])
        arguments = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            turn_blocks=[block(0, text), block(1, 'Current operation')], observations=[row],
            work_notes=notes, processed_turns=processed, version_resolver=lambda _:row['sha256'],
            context_token_target=40000)
        messages, selection = select(**arguments)
        self.assertNotIn(text, json.dumps(messages))
        self.assertEqual(selection['selected_turns'], [1])
        self.assertEqual(selection['compaction_turns'], [])
        self.assertFalse(selection['compaction_needed'])
        self.assertLess(selection['estimated_input_tokens'], 10000)
        self.assertEqual(selection['omitted_records'][0]['status'], 'not_requested')
        # Explicit current work can still reuse originals; notes never certify them.
        explicit, selected = select(**arguments, selected_records=[row['record']])
        self.assertIn(text, json.dumps(explicit))
        self.assertEqual(selected['selected_records'][0]['version_status'], 'matching')
        stale, selected = select(**{**arguments, 'version_resolver':lambda _:'changed'},
            selected_records=[row['record']])
        self.assertNotIn(text, json.dumps(stale))
        self.assertEqual(selected['omitted_records'][0]['version_status'], 'stale')
        for supplied in ([], [{**notes[0], 'current':False}], [{**notes[0], 'text':'Oversized note. '*10000}]):
            fallback, _ = select(**{**arguments, 'work_notes':supplied})
            self.assertIn(text, json.dumps(fallback))

    def test_selected_body_is_replaced_by_its_complete_native_operation_before_measuring(self):
        result = {'tool_name':'execute', 'status':'error', 'error':'Original diagnostic. '*100}
        row = observation(0, json.dumps(result), kind='operation_result')
        old = block(0, 'unused')
        old[0]['tool_calls'][0]['function'] = {'name':'execute', 'arguments':'{"argv":["worker"]}'}
        old[1]['content'] = json.dumps(result)
        messages, selection = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[], observations=[row], selected_records=[row['record']],
            native_observations=[{key: value for key, value in row.items() if key != 'text'}],
            turn_blocks=[old, block(1, 'Continue')], context_token_target=20000)
        self.assertEqual(selection['selected_turns'], [0, 1])
        self.assertEqual(json.loads(messages[1]['content'])['working_view']['observations'], [])
        self.assertEqual([m for m in messages if m['role'] in {'assistant','tool'}], old + block(1, 'Continue'))
        self.assertFalse(selection['compaction_needed'])

    def test_visible_result_cannot_hide_capacity_omitted_authored_arguments(self):
        result = {'tool_name':'write_file', 'status':'error', 'error':'Write was rejected'}
        row = observation(0, json.dumps(result), kind='operation_result')
        old = block(0, 'unused')
        old[0]['tool_calls'][0]['function'] = {'name':'write_file',
            'arguments':json.dumps({'path':'code.py', 'content':'Authored implementation. '*4000})}
        old[1]['content'] = json.dumps(result)
        messages, selection = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[], observations=[row], selected_records=[row['record']],
            turn_blocks=[old, block(1, 'Continue')], context_token_target=12000)
        self.assertIn('Write was rejected', json.dumps(messages))
        self.assertEqual(selection['compaction_turns'], [0])
        self.assertTrue(selection['compaction_needed'])
        self.assertFalse(selection['economical_compaction'])
        self.assertEqual(selection['selected_turns'], [1])

    def test_partial_processed_history_and_required_overflow_do_not_claim_compaction_value(self):
        notes = [{'text':'Navigable prior work', 'records':[], 'current':True}]
        blocks = [block(0, 'Unprocessed failure'), block(1, 'Processed receipt'), block(2, 'Current work')]
        messages, selection = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[1], work_notes=notes, turn_blocks=blocks, context_token_target=20000)
        self.assertEqual(selection['selected_turns'], [0,1,2])
        self.assertIn('Unprocessed failure', json.dumps(messages))
        _, selection = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[], turn_blocks=[blocks[0], block(1, 'Required latest. '*4000)], context_token_target=3000)
        self.assertTrue(selection['context_exceeds_input_target'])
        self.assertFalse(selection['compaction_needed'])

    def test_sparse_handoffs_keep_uncited_and_explicit_work_with_feedback(self):
        texts = [f'Original observation {number}: ' + chr(65 + number) * 300 for number in range(6)]
        rows = [observation(number, text, kind='operation_result') for number, text in enumerate(texts)]
        notes = [{'text': 'Earlier work with retained sources',
            'records': ['records/2.json', 'records/3.json'], 'current': True,
            'handoff_policy': CITATION_HANDOFF_POLICY}]
        options = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[0, 2, 3], sparse_handoffs=True, work_notes=notes,
            observations=rows, turn_blocks=[block(i, text) for i, text in enumerate(texts)],
            block_request_counts=[1, 1, 1, 1, 2, 2], context_token_target=50000)
        messages, selection = select(**options)
        self.assertEqual(selection['selected_turns'], [0, 1, 4, 5])
        self.assertEqual(json.loads(messages[1]['content'])['working_view']['history']['archived_turn_ranges'], [[2, 4]])
        self.assertIn(texts[0], json.dumps(messages))  # Processed but not cited.
        self.assertNotIn(texts[3], json.dumps(messages))
        self.assertIn('Keep the error bars', json.dumps(messages))
        _, selected = select(**options, selected_records=['records/2.json'])
        self.assertIn(2, selected['selected_turns'])
        _, uncited = select(**{**options, 'work_notes': [{**notes[0], 'records': []}]})
        self.assertEqual(uncited['selected_turns'], list(range(6)))
        _, stale = select(**{**options, 'work_notes': [{**notes[0], 'current': False}]})
        self.assertEqual(stale['selected_turns'], list(range(6)))

    def test_processed_but_uncited_failure_remains_discoverable_in_compact_history(self):
        texts = ['Original source body. '*300, 'Full failed execution output. '*300]
        operations = [{'turn':i, 'turn_complete':True, 'request_count':1, 'call_id':str(i),
            'reply':f'records/{i}/reply.json', 'observation':{'tool':'execute',
                'record':f'records/{i}/result.json', 'arguments':{'argv':['worker',str(i)]},
                'status':'error' if i else 'ok', 'error':'Recorded execution failure' if i else '',
                'observed':{'returncode':1 if i else 0}}} for i in range(2)]
        arguments = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[0,1], turn_blocks=[block(0,texts[0]), block(1,texts[1]), block(2,'Current work')],
            work_notes=[{'text':'A judgment about only the first record',
                'records':['records/0/result.json'], 'current':True}], context_token_target=40000)
        without, bare = select(**arguments)
        messages, selection = select(**arguments, operation_history=operations)
        overview = json.loads(messages[1]['content'])['working_view']['operation_overview']
        entries = [entry for group in expand_history_view(overview['entries']) for entry in group['observations']]
        self.assertEqual({entry['record'] for entry in entries}, {row['observation']['record'] for row in operations})
        self.assertIn('Recorded execution failure', json.dumps(entries))
        self.assertEqual(overview['omitted_entries'], 0)
        self.assertEqual(selection['selected_turns'], [2])
        self.assertTrue(all(text not in json.dumps(messages) for text in texts))
        self.assertLess(selection['estimated_input_tokens'] - bare['estimated_input_tokens'], sum(map(len,texts)))

    def test_journal_native_uses_available_capacity_beyond_half_the_window(self):
        blocks = [block(0, 'Earlier native work. ' * 500), block(1, 'Latest native work. ' * 500)]
        messages, budget = select(material_policy='retained', require_work_notes=True,
            fit_work_notes=True, turn_blocks=blocks, context_token_target=32000)
        self.assertEqual([m for m in messages if m['role'] in {'assistant', 'tool'}],
                         blocks[0] + blocks[1])
        self.assertGreater(budget['recent_native_tokens'], 16000)
        self.assertLessEqual(budget['estimated_input_tokens'], 32000)
        self.assertFalse(budget['compaction_needed'])

        # A larger model window does not justify replaying growing old work
        # indefinitely. Propose a checkpoint only when repeated history is
        # more costly than the mandatory current input; retain it verbatim
        # until a real, usable journal checkpoint is adopted.
        old = [block(0, 'Earlier observation ' * 700), block(1, 'Another observation ' * 700)]
        current = block(2, 'Current decision')
        options = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            processed_turns=[], turn_blocks=[*old, current], context_token_target=100000)
        legacy, legacy_budget = select(**options)
        proposed, budget = select(**options, economical_checkpoint=True)
        self.assertFalse(legacy_budget['compaction_needed'])
        self.assertTrue(budget['compaction_needed'])
        self.assertTrue(budget['economical_compaction'])
        self.assertEqual(budget['compaction_turns'], [0])
        self.assertEqual([m for m in proposed if m['role'] in {'assistant', 'tool'}], old[0] + old[1] + current)
        compacted, compacted_budget = select(**{**options, 'processed_turns':[0]},
            economical_checkpoint=True, work_notes=[{'text':'Earlier work; originals remain in the journal.',
                'records':[], 'current':True}])
        self.assertEqual(compacted_budget['selected_turns'], [1, 2])
        self.assertLess(compacted_budget['estimated_input_tokens'], budget['estimated_input_tokens'])
        self.assertIn('Current decision', json.dumps(compacted))
        self.assertEqual(legacy_budget['selected_turns'], [0, 1, 2])
        _, after_failure = select(**options, economical_checkpoint=True, economical_after_turn=1)
        self.assertFalse(after_failure['compaction_needed'])
        self.assertFalse(after_failure['economical_compaction'])
        future = {**options, 'turn_blocks':[*old, current, block(3, 'Later decision')]}
        _, after_new_work = select(**future, economical_checkpoint=True, economical_after_turn=1)
        self.assertTrue(after_new_work['compaction_needed'])
        self.assertEqual(after_new_work['compaction_turns'], [0, 1])

    def test_journal_current_references_and_results_precede_native_without_a_snapshot(self):
        current = observation(0, 'Current original. ' * 100, path='outputs/current.txt',
            role='historical_observation')
        referenced = observation(0, 'Referenced original. ' * 100, record='records/reference.json',
            role='historical_observation')
        result = observation(0, json.dumps({'status':'ok', 'data':{'result':'Successful measurement. '*60}}),
            kind='operation_result', record='records/success.json')
        authored = observation(1, 'Authored implementation. '*1500, role='write_context', path='outputs/code.py', record='records/write.json')
        old = block(1, 'Write completed')
        old[0]['tool_calls'][0]['function'] = {'name':'write_file', 'arguments':json.dumps({'path':authored['path'], 'content':authored['text']})}
        rows = [current, referenced, result, authored]
        messages, budget = select(material_policy='retained', require_work_notes=True,
            fit_work_notes=True, observations=rows,
            turn_blocks=[[], old, block(2, 'Latest feedback')],
            selected_records=[referenced['record']], current_artifacts=[current['path']],
            version_resolver=lambda _: 'recorded-digest', context_token_target=18000)
        supplied = json.loads(messages[1]['content'])['working_view']['observations']
        self.assertEqual({row['observation']['record'] for row in supplied},
                         {current['record'], referenced['record'], result['record']})
        self.assertEqual(next(row['version_status'] for row in supplied if
            row['observation']['record'] == result['record']), 'historical')
        self.assertEqual({row['status'] for row in budget['omitted_records']}, {'unavailable'})
        self.assertEqual(budget['selected_turns'], [2])
        self.assertTrue(budget['compaction_needed'])
        self.assertLessEqual(budget['estimated_input_tokens'], 18000)

    def test_required_current_material_precedes_optional_native_under_capacity(self):
        current = observation(0, 'Current file body. ' * 230,
            role='historical_observation', path='outputs/current.txt')
        referenced = observation(0, 'Referenced source. ' * 130,
            role='historical_observation', record='records/reference.json')
        notes = [{'text': 'Keep the recorded uncertainty and scope',
                  'records': [referenced['record']], 'status': 'active'}]
        blocks = [[], block(1, 'Optional previous work. ' * 300), block(2, 'Latest feedback')]
        arguments = dict(material_policy='retained', require_work_notes=True,
            observations=[current, referenced], turn_blocks=blocks, work_notes=notes,
            current_artifacts=[current['path']], version_resolver=lambda _: 'recorded-digest',
            schema_tokens=6000, context_token_target=18000)
        before = deepcopy(arguments)
        messages, budget = select(**arguments)
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['working_view']['notes'], notes)
        self.assertEqual({entry['observation']['record'] for entry in
                          context['working_view']['observations']},
                         {current['record'], referenced['record']})
        for row in (current, referenced):
            self.assertEqual(json.dumps(messages).count(row['text']), 1)
        self.assertEqual([m for m in messages if m['role'] in {'assistant', 'tool'}], blocks[-1])
        self.assertEqual(context['permissions'], {'write': ['outputs/']})
        self.assertEqual(messages[0], {'role': 'system', 'content': 'Respect supplied permissions'})
        self.assertEqual([m['content'] for m in messages[2:] if m['role'] == 'user'],
                         ['Compare the source measurements', 'Keep the error bars'])
        self.assertGreaterEqual(budget['estimated_input_tokens'], 6000 + len(json.dumps(messages)))
        self.assertLessEqual(budget['estimated_input_tokens'], 18000)
        self.assertEqual(arguments, before)

    def test_required_material_leaves_room_for_native_chronology_without_duplicate_bodies(self):
        current = observation(0, 'Current original body', role='historical_observation')
        recent = observation(1, 'Recently observed original', role='historical_observation',
            path='inputs/recent.txt')
        blocks = [block(0, 'Oversized archived work. ' * 2000),
                  block(1, recent['text']), block(2, 'Latest complete result')]
        requests = ['Initial request', 'Revise the result', 'Preserve uncertainty']
        messages, budget = select(material_policy='retained', require_work_notes=True,
            turn_blocks=blocks, observations=[current, recent], user_requests=requests,
            block_request_counts=[1, 1, 2], selected_records=[current['record']],
            work_notes=[{'text': 'Use the supplied originals', 'records': []}],
            version_resolver=lambda _: 'recorded-digest',
            schema_tokens=2000, context_token_target=16000)
        self.assertEqual(messages[2:], [
            {'role': 'user', 'content': requests[0]}, *blocks[1],
            {'role': 'user', 'content': requests[1]}, *blocks[2],
            {'role': 'user', 'content': requests[2]}])
        self.assertEqual(budget['selected_turns'], [1, 2])
        for row in (current, recent):
            self.assertEqual(json.dumps(messages).count(row['text']), 1)
        self.assertGreaterEqual(budget['estimated_input_tokens'], 2000 + len(json.dumps(messages)))
        self.assertLessEqual(budget['estimated_input_tokens'], 16000)

    def test_admitted_failure_receipt_matches_compact_native_without_losing_supply_metadata(self):
        result = {'status': 'error', 'data': {'returncode': 1,
                  'stderr': 'Recorded execution failure', 'outputs': []}}
        failure = {'kind': 'operation_result', 'record': 'records/failure.json', 'turn': 0,
            'path': 'outputs/result.csv', 'sha256': 'recorded-output-version',
            'text': json.dumps(result, indent=2)}
        old = block(0, '')
        old[1]['content'] = json.dumps(result, separators=(',', ':'))
        before = deepcopy((failure, old))
        messages, budget = select(material_policy='retained', require_work_notes=True,
            observations=[failure], turn_blocks=[old, block(1, 'Latest feedback')],
            selected_records=[failure['record']],
            work_notes=[{'text': 'The recorded execution failed', 'records': [failure['record']]}],
            version_resolver=lambda _: 'new-output-version',
            schema_tokens=2000, context_token_target=16000)
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['working_view']['observations'],
                         [{'observation': failure, 'version_status': 'historical'}])
        self.assertEqual(json.dumps(messages).count(result['data']['stderr']), 1)
        self.assertEqual(len(budget['selected_records']), 1)
        supplied = budget['selected_records'][0]
        self.assertEqual(supplied['status'], 'working_view')
        self.assertEqual(supplied['version_status'], 'historical')
        self.assertEqual(supplied['source']['record'], failure['record'])
        self.assertEqual(supplied['source']['sha256'], failure['sha256'])
        self.assertEqual(context['material_supply']['references'], [{
            'record': failure['record'], 'status': 'working_view',
            'version_status': 'historical', 'source': supplied['source']}])
        self.assertFalse(budget['omitted_records'])
        self.assertEqual((failure, old), before)
        self.assertLessEqual(budget['estimated_input_tokens'], 16000)

    def test_required_notes_without_usable_snapshot_keep_legacy_suffix_and_reject_stale_files(self):
        current = observation(0, 'Current file body. ' * 390, role='historical_observation')
        stale = observation(0, 'Retired file original', role='historical_observation',
            record='records/stale.json', path='inputs/retired.txt', sha256='old-digest')
        blocks = [[], block(1, 'Optional previous work. ' * 300), block(2, 'Latest feedback')]
        arguments = dict(material_policy='retained', observations=[current, stale],
            turn_blocks=blocks, current_artifacts=[current['path'], stale['path']],
            version_resolver=lambda _: 'recorded-digest',
            schema_tokens=6000, context_token_target=18000)
        for notes in ([], [{'text': 'Invalidated judgment', 'current': False}],
                      [{'text': 'Expired judgment', 'status': 'expired'}]):
            with self.subTest(notes=notes):
                messages, budget = select(**arguments, work_notes=notes, require_work_notes=True)
                _, legacy = select(**arguments, work_notes=notes, require_work_notes=False)
                self.assertEqual(budget['selected_turns'], legacy['selected_turns'])
                self.assertEqual(budget['selected_turns'], [1, 2])
                self.assertEqual([m for m in messages if m['role'] in {'assistant', 'tool'}],
                                 [m for turn in blocks[1:] for m in turn])
                self.assertNotIn(current['text'], json.dumps(messages))
                self.assertNotIn(stale['text'], json.dumps(messages))
                omitted = {row['source']['record']: row for row in budget['omitted_records']}
                self.assertEqual(omitted[stale['record']]['status'], 'stale')
                self.assertLessEqual(budget['estimated_input_tokens'], 18000)

    def test_recorded_work_survives_eviction_with_versions_and_one_budget(self):
        source = 'Original measurement and observed conditions. ' * 45
        recent_text = 'Recently read original text'

        def read(turn, text, path, **extra):
            return observation(turn, text, path=path, role='historical_observation',
                tool='read_file', field='data.text', **extra)

        original = read(0, source, 'inputs/original.txt')
        failure = {'kind': 'operation_result', 'record': 'records/failure.json', 'turn': 1,
            'tool': 'run_process', 'path': 'outputs/result.csv', 'sha256': 'old-output-version',
            'text': json.dumps({'tool_name': 'run_process', 'status': 'error',
                'data': {'returncode': 1, 'stderr': 'Actual historical execution failure'}})}
        old_duplicate = read(2, recent_text, 'inputs/recent.txt')
        pinned = read(3, 'Explicitly referenced original', 'inputs/pinned.txt')
        current = read(4, 'Current object original', 'inputs/current.txt')
        stale = read(5, 'Original from a changed file', 'inputs/changed.txt', sha256='old-digest')
        unknown = read(6, 'Original with an unavailable file', 'inputs/unavailable.txt')
        missing_digest = read(7, 'Original without a digest', 'inputs/no-digest.txt', sha256=None)
        authored = observation(8, 'Authored payload is not a read', path='outputs/authored.txt',
            role='write_context', field='function.arguments.content')
        large = read(9, 'Whole original too large for the target. ' * 1000, 'inputs/large.txt')
        recent = read(11, recent_text, 'inputs/recent.txt')
        rows = [original, failure, old_duplicate, pinned, current, stale, unknown,
                missing_digest, authored, large, recent]
        blocks = [block(i, text) for i, text in enumerate([
            source, failure['text'], recent_text, pinned['text'], current['text'], stale['text'],
            unknown['text'], missing_digest['text'], authored['text'], large['text'],
            'Oversized intervening native work. ' * 1000, recent_text])]
        operations = [{'turn': 0, 'turn_complete': True, 'request_count': 1,
            'call_id': str(i), 'reply': f'records/navigation-{i}.json',
            'observation': {'tool': 'read_file', 'record': f'records/navigation-result-{i}.json',
                'status': 'ok', 'arguments': {'path': f'inputs/{i}.txt', 'detail': 'n' * 700}}}
            for i in range(12)]
        operations.append({'turn': 11, 'turn_complete': True, 'request_count': 1,
            'call_id': 'recent', 'reply': 'records/recent-reply.json',
            'observation': {'tool': 'read_file', 'record': recent['record'], 'status': 'ok',
                'arguments': {'path': 'inputs/recent.txt'}}})
        versions = {row['path']: 'recorded-digest' for row in rows
                    if row.get('kind') != 'operation_result'}
        versions['inputs/changed.txt'] = 'changed-digest'
        versions['inputs/unavailable.txt'] = None
        calls = []

        def resolver(path):
            calls.append(path)
            return versions[path]

        arguments = dict(material_policy='retained', turn_blocks=blocks, observations=rows,
            selected_records=[pinned['record'], 'records/missing.json'],
            current_artifacts=[current['path']], version_resolver=resolver,
            operation_history=operations, context_token_target=10000)
        before = deepcopy((rows, blocks, operations))
        messages, budget = select(**arguments)
        context = json.loads(messages[1]['content'])
        view = context['working_view']
        supplied = view['observations']
        states = {state['source']['record']: state for state in
                  [*budget['selected_records'], *budget['omitted_records']]}

        self.assertEqual(view['notes'], [])
        self.assertEqual(budget['selected_turns'], [11])
        self.assertEqual([entry['observation']['record'] for entry in supplied[:2]],
                         [pinned['record'], current['record']])
        self.assertIn({'observation': original, 'version_status': 'matching'}, supplied)
        self.assertIn({'observation': failure, 'version_status': 'historical'}, supplied)
        self.assertNotIn('outputs/result.csv', calls)
        self.assertEqual(states[stale['record']]['status'], 'stale')
        self.assertEqual(states[stale['record']]['version_status'], 'stale')
        self.assertEqual(states[unknown['record']]['status'], 'unavailable')
        self.assertEqual(states[unknown['record']]['version_status'], 'unknown')
        self.assertEqual(states[missing_digest['record']]['status'], 'unavailable')
        self.assertEqual(states[authored['record']]['status'], 'unavailable')
        self.assertEqual(states[large['record']]['status'], 'capacity')
        self.assertEqual(states[old_duplicate['record']]['status'], 'duplicate')
        self.assertEqual(states[recent['record']]['status'], 'native')
        self.assertNotIn('not_attempted', {state['status'] for state in states.values()})
        self.assertEqual(json.dumps(messages, ensure_ascii=False).count(recent_text), 1)
        self.assertEqual(json.dumps(messages, ensure_ascii=False).count(source), 1)
        self.assertNotIn(stale['text'], json.dumps(messages, ensure_ascii=False))
        self.assertEqual({item['record'] for item in context['material_supply']['references']},
                         {pinned['record'], 'records/missing.json'})
        self.assertEqual(next(item for item in context['material_supply']['references']
                              if item['record'] == 'records/missing.json')['status'], 'missing')
        overview = view['operation_overview']
        supplied_navigation = [i for start, end in overview['supplied_entry_ranges'] for i in range(start, end)]
        self.assertNotIn(12, supplied_navigation)  # Already in the recent native block.
        self.assertGreater(overview['omitted_entries'], 1)
        navigation_only, _ = select(**{**arguments, 'observations': []})
        self.assertLess(json.loads(navigation_only[1]['content'])['working_view']['operation_overview']
                        ['omitted_entries'], overview['omitted_entries'])
        for request in ('Compare the source measurements', 'Keep the error bars'):
            self.assertEqual(messages.count({'role': 'user', 'content': request}), 1)
        self.assertGreaterEqual(budget['estimated_input_tokens'],
                                30 + len(json.dumps(messages, ensure_ascii=False)))
        self.assertLessEqual(budget['estimated_input_tokens'], 10000)
        self.assertEqual((rows, blocks, operations), before)

        # Optional old notes cannot replace the originals or expand every note
        # reference into required navigation before the selector can admit them.
        notes = [{'text': 'Prior working note',
                  'records': [f'records/note-only-{i}.json' for i in range(200)]}]
        with_notes, _ = select(**{**arguments, 'work_notes': notes,
                                   'summarized_through_turn': len(blocks)})
        noted_context = json.loads(with_notes[1]['content'])
        self.assertEqual(noted_context['working_view']['history']['notes_through_turn'], 0)
        self.assertIn(source, json.dumps(with_notes, ensure_ascii=False))
        self.assertEqual({item['record'] for item in noted_context['material_supply']['references']},
                         {pinned['record'], 'records/missing.json'})

        versions[original['path']] = 'changed-digest'
        versions[recent['path']] = 'changed-digest'
        changed, changed_budget = select(**arguments)
        self.assertNotIn(source, json.dumps(changed, ensure_ascii=False))
        self.assertEqual(next(item for item in changed_budget['omitted_records']
                              if item['source']['record'] == original['record'])['status'], 'stale')
        self.assertEqual(json.loads(changed[1]['content'])['working_view']['stale_native_observations'][0]
                         ['record'], recent['record'])
        unavailable, unavailable_budget = select(**{**arguments, 'version_resolver': None})
        self.assertNotIn(source, json.dumps(unavailable, ensure_ascii=False))
        self.assertEqual(next(item for item in unavailable_budget['omitted_records']
                              if item['source']['record'] == original['record'])['status'], 'unavailable')
        self.assertIn('Actual historical execution failure', json.dumps(unavailable))


class ReferencedWorkingContextTests(unittest.TestCase):
    def test_owner_notes_and_feedback_keep_native_chronology_without_summary_coverage(self):
        requests = ['Initial goal', 'Correct the coordinates', 'Keep uncertainty explicit']
        blocks = [block(0, 'initial work'), block(1, 'corrected work')]
        notes = [{'text': 'Prior interpretation needs correction', 'records': [], 'current': False},
                 {'text': 'Coordinate correction remains to be reviewed', 'records': []}]
        messages, budget = select(material_policy='referenced', user_requests=requests,
            turn_blocks=blocks, block_request_counts=[1, 2], work_notes=notes,
            summarized_through_turn=2, context_token_target=20000)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(view['notes'], notes)
        self.assertEqual(view['history']['notes_through_turn'], 0)
        self.assertEqual(budget['selected_turns'], [0, 1])
        self.assertEqual(messages[2:], [
            {'role': 'user', 'content': requests[0]}, *blocks[0],
            {'role': 'user', 'content': requests[1]}, *blocks[1],
            {'role': 'user', 'content': requests[2]}])
        self.assertTrue(all(row['status'] == 'working_view' for row in budget['notes']))

    def test_unreferenced_archived_material_and_outcomes_do_not_fill_spare_capacity(self):
        text = 'Compare source measurements ' * 600
        rows = [observation(0, text), observation(0, 'Compare source measurements: unrelated old excerpt',
                                               path='materials/other.txt'),
                {'kind': 'operation_result', 'record': 'records/old-operation.json', 'turn': 0,
                 'text': json.dumps({'status': 'error', 'error': 'Historical failure'})}]
        messages, budget = select(material_policy='referenced', observations=rows,
            turn_blocks=[block(0, text), block(1, 'latest result')],
            current_artifacts=['materials/source.txt'], context_token_target=6000)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(budget['selected_turns'], [1])
        self.assertEqual(view['observations'], [])
        self.assertEqual(budget['selected_records'], [])
        rendered = json.dumps(messages)
        self.assertNotIn(text, rendered)
        self.assertNotIn('unrelated old excerpt', rendered)
        self.assertNotIn('Historical failure', rendered)
        self.assertLess(budget['estimated_input_tokens'], 6000)

    def test_explicit_records_recover_original_evidence_across_empty_native_boundary(self):
        rows = [observation(0, 'Original measurement', path='materials/matching.txt'),
                observation(1, 'Unknown version observation', path='materials/unknown.txt',
                            record='records/unknown.json'),
                observation(1, 'Changed original', path='materials/stale.txt',
                            record='records/stale.json'),
                {'kind': 'operation_result', 'record': 'records/failed-process.json', 'turn': 1,
                 'path': 'outputs/result.csv', 'text': json.dumps({'status': 'error', 'returncode': 1})}]
        notes = [{'text': 'Reassess this historical judgment', 'current': False,
                  'records': [rows[1]['record'], rows[3]['record'], 'records/missing.json']}]
        before = deepcopy((rows, notes))

        def resolver(path):
            return {'materials/matching.txt': 'recorded-digest',
                    'materials/unknown.txt': None, 'materials/stale.txt': 'changed-digest'}[path]

        # The caller has retired native work before its model-rebinding boundary;
        # original observations remain independently addressable by record.
        messages, budget = select(material_policy='referenced', observations=rows,
            turn_blocks=[[], [], block(2, 'new native session')], work_notes=notes,
            selected_records=[rows[0]['record'], rows[2]['record']],
            version_resolver=resolver, context_token_target=12000)
        view = json.loads(messages[1]['content'])['working_view']
        self.assertEqual(view['notes'], notes)
        self.assertEqual(view['observations'], [
            {'observation': rows[0], 'version_status': 'matching'},
            {'observation': rows[1], 'version_status': 'unknown'},
            {'observation': rows[3], 'version_status': 'historical'}])
        self.assertEqual(budget['omitted_records'][0]['status'], 'stale')
        supply = json.loads(messages[1]['content'])['material_supply']['references']
        self.assertEqual({entry['record']: entry['status'] for entry in supply}, {
            rows[0]['record']: 'working_view', rows[1]['record']: 'working_view',
            rows[2]['record']: 'stale', rows[3]['record']: 'working_view',
            'records/missing.json': 'missing'})
        self.assertEqual({entry['record']: entry.get('version_status') for entry in supply}, {
            rows[0]['record']: 'matching', rows[1]['record']: 'unknown',
            rows[2]['record']: 'stale', rows[3]['record']: 'historical',
            'records/missing.json': None})
        self.assertNotIn('Changed original', json.dumps(messages))
        self.assertIn('not instructions', view['scope'])
        self.assertIn('verified facts', view['scope'])
        self.assertEqual((rows, notes), before)
        view['observations'][0]['observation']['text'] = 'changed returned evidence'
        budget['selected_records'][0]['source']['path'] = 'changed returned locator'
        self.assertEqual((rows, notes), before)

    def test_native_observation_is_not_reprojected_by_old_or_current_reference(self):
        text = 'Original observed measurement'
        rows = [observation(0, text), observation(2, text)]
        latest = block(2, text)
        messages, budget = select(material_policy='referenced', observations=rows,
            turn_blocks=[[], [], latest], selected_records=[row['record'] for row in rows],
            version_resolver=lambda _: 'recorded-digest', context_token_target=10000)
        self.assertEqual(json.loads(messages[1]['content'])['working_view']['observations'], [])
        self.assertEqual(json.dumps(messages).count(text), 1)
        self.assertEqual(messages[-3:-1], latest)
        self.assertEqual(budget['omitted_records'][0]['status'], 'duplicate')
        self.assertEqual(budget['selected_records'][0]['status'], 'native')
        supply = json.loads(messages[1]['content'])['material_supply']['references']
        self.assertEqual([entry['status'] for entry in supply], ['duplicate', 'native'])

        # Keep the assertions above for saved historical text-based contracts.
        # The new contract cannot borrow an identical body from another receipt,
        # field, file, version or window, even inside the same native turn.
        current = {**rows[-1], 'field':'data.text', 'role':'historical_observation'}
        variants = [dict(current, **change) for change in (
            {'record':'records/another.json'}, {'field':'metadata.other.text'},
            {'path':'materials/another.txt'}, {'sha256':'earlier-version'},
            {'offset_bytes':50, 'next_offset_bytes':50+len(text.encode('utf-8'))})]
        originals = [current, *variants]
        before = deepcopy(originals)
        arguments = dict(material_policy='referenced', observations=originals,
            turn_blocks=[[], [], latest], selected_records=list(dict.fromkeys(row['record'] for row in originals)),
            version_resolver=lambda _:'recorded-digest', context_token_target=100000)
        for visible in ([], [{key:value for key,value in current.items() if key != 'text'}]):
            with self.subTest(actual_supply=bool(visible)):
                messages, budget = select(**arguments, native_observations=visible)
                states = budget['selected_records'] + budget['omitted_records']
                native = [state['source'] for state in states if state['status'] == 'native']
                self.assertEqual(native, [{key:value for key,value in current.items() if key != 'text'}]
                    if visible else [])
                self.assertFalse(any(state['status'] == 'duplicate' for state in states))
                self.assertEqual(sum(state['status'] == 'working_view' for state in states), 4 if visible else 5)
                self.assertTrue(any(state['status'] == 'stale' and state['source']['sha256'] == 'earlier-version'
                    for state in states))
        self.assertEqual(originals, before)

    def test_referenced_oversized_text_is_not_lexically_rewritten(self):
        row = observation(0, 'Compare source measurements. ' * 1000)
        # A receipt can contain several windows. Supplying one must not hide
        # its oversized sibling, even behind more than sixteen old omissions.
        small = observation(0, 'Small referenced window', path='materials/small.txt')
        unreferenced = [observation(0, 'Unrequested text', record=f'records/old-{i}.json')
                        for i in range(20)]
        latest = block(1, 'latest')
        messages, budget = select(material_policy='referenced', observations=[row, small, *unreferenced],
            turn_blocks=[[], latest], selected_records=[row['record']], context_token_target=6000)
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['working_view']['observations'], [
            {'observation': small, 'version_status': 'unchecked'}])
        self.assertEqual(budget['omitted_records'][0]['status'], 'capacity')
        self.assertNotIn(row['record'], [entry['record'] for entry in context['material_supply']['omitted']])
        self.assertEqual([entry['status'] for entry in context['material_supply']['references']],
                         ['capacity', 'working_view'])
        self.assertNotIn('record_text_slice', json.dumps(messages))
        self.assertEqual(messages[-3:-1], latest)
        self.assertLessEqual(budget['estimated_input_tokens'], 6000)

    def test_overflow_keeps_all_notes_requests_contracts_and_complete_latest_failure(self):
        latest = block(1, 'first result')
        latest[0]['tool_calls'].append({'id': 'second', 'type': 'function',
            'function': {'name': 'observe', 'arguments': '{"path":"denied.txt"}'}})
        latest.append({'role': 'tool', 'tool_call_id': 'second',
            'content': json.dumps({'status': 'error', 'error': 'Permission denied'})})
        notes = [{'text': 'Owner understanding ' * 100, 'records': ['records/missing.json']}]
        contracts = {'permissions': ['read_only'], 'resources': {'remaining': {'total_tokens': 100}}}
        tail = [{'role': 'system', 'content': 'Preserve the closing contract'}]
        arguments = dict(material_policy='referenced', turn_blocks=[block(0, 'old work'), latest],
            observations=[observation(0, 'old material')], work_notes=notes,
            base_context=contracts, trailing_messages=tail, context_token_target=10)
        before = deepcopy(arguments)
        messages, budget = select(**arguments)
        context = json.loads(messages[1]['content'])
        self.assertEqual(context['working_view']['notes'], notes)
        self.assertEqual(context['permissions'], contracts['permissions'])
        self.assertEqual(context['resources']['remaining'], contracts['resources']['remaining'])
        self.assertEqual(messages[2:], [
            {'role': 'user', 'content': 'Compare the source measurements'}, *latest,
            {'role': 'user', 'content': 'Keep the error bars'}, *tail])
        self.assertTrue(budget['context_exceeds_input_target'])
        self.assertGreaterEqual(budget['estimated_input_tokens'],
                                30 + len(json.dumps(messages, ensure_ascii=False)))
        self.assertEqual(arguments, before)
        messages[3]['tool_calls'][0]['function']['arguments'] = 'changed returned call'
        budget['notes'][0]['records'].append('changed returned metadata')
        self.assertEqual(arguments, before)

    def test_receipt_overview_survives_native_eviction_without_notes_or_current_authority(self):
        operations, blocks = [], []
        receipts = [
            ('run_process', {'argv': ['python', 'worker.py'], 'cwd': 'workspace'},
             {'returncode': 0, 'stdout': 'Observed execution outcome', 'outputs': ['result.csv']}, 'ok', ''),
            ('write_file', {'path': 'result.csv', 'content': 'a,' * 5000},
             {'path': 'result.csv', 'sha256': 'first-version'}, 'ok', ''),
            ('write_file', {'path': 'result.csv', 'content': 'b,' * 5000},
             {'path': 'result.csv', 'sha256': 'changed-version'}, 'ok', ''),
            ('run_process', {'argv': ['python', 'worker.py'], 'cwd': 'workspace'},
             {'returncode': 1, 'stderr': 'Actual failed validation'}, 'error', 'Execution failed'),
            ('run_process', {'argv': ['python', 'worker.py'], 'cwd': 'workspace'},
             {'receipt': 'process/unknown.json'}, 'unknown', 'Execution outcome unknown'),
        ]
        receipts += [('read_file', {'path': f'inputs/{i}.txt', 'offset_bytes': 0},
            {'path': f'inputs/{i}.txt', 'text': 'Unrelated original bytes ' * 1200,
             'sha256': 'observed-version'}, 'ok', '') for i in range(15)]
        for i, (name, args, data, status, error) in enumerate(receipts):
            call = {'id': str(i), 'function': {'name': name, 'arguments': json.dumps(args)}}
            result = {'status': status, 'data': data, 'error': error,
                      'artifacts': ['result.csv'] if i in (0, 1, 2) else []}
            operations.append({'turn': i, 'turn_complete': True, 'request_count': 1, 'call_id': str(i),
                'reply': f'records/turn-{i}/native_result.json',
                'observation': ToolApplication._history_observation(call, result,
                    f'attempts/tool-{i}/native_result.json', navigation=True)})
            blocks.append(block(i, json.dumps(result)))
        blocks.append(block(len(blocks), 'Current decision'))
        before = deepcopy((operations, blocks))
        arguments = dict(material_policy='referenced', turn_blocks=blocks,
                         context_token_target=24000)
        messages, budget = select(**arguments, operation_history=operations)
        view = json.loads(messages[1]['content'])['working_view']
        overview = view['operation_overview']
        groups = expand_history_view(overview['entries'])
        indexed = [entry for group in groups for entry in group['observations']]
        for group in groups:
            for entry in group['observations']:
                original = operations[entry['entry_index']]
                self.assertEqual((group['reply'], group['request_count']),
                                 (original['reply'], original['request_count']))
                self.assertEqual(entry, {'entry_index': entry['entry_index'],
                    **{key: original[key] for key in ('turn', 'turn_complete', 'call_id')},
                    **original['observation']})
        self.assertEqual(view['notes'], [])
        self.assertGreater(budget['recent_start_turn'], 0)
        self.assertIn('Observed execution outcome', json.dumps(indexed))
        self.assertIn('Actual failed validation', json.dumps(indexed))
        self.assertIn('Execution outcome unknown', json.dumps(indexed))
        self.assertEqual([entry['observed']['sha256'] for entry in indexed
                          if entry['tool'] == 'write_file'], ['first-version', 'changed-version'])
        self.assertEqual(indexed[1]['omitted_fields']['arguments'], ['content'])
        self.assertNotIn('Unrelated original bytes', json.dumps(overview))
        self.assertIn('not original text, read coverage', overview['scope'])
        self.assertFalse(view['observations'])  # Navigation never supplies evidence bodies.
        self.assertLessEqual(budget['estimated_input_tokens'], 24000)
        self.assertGreaterEqual(budget['estimated_input_tokens'], 30 + len(json.dumps(messages, ensure_ascii=False)))
        self.assertEqual((operations, blocks), before)
        tight, metadata = select(**{**arguments, 'context_token_target': 4500}, operation_history=operations)
        coverage = json.loads(tight[1]['content'])['working_view']['operation_overview']
        self.assertGreater(coverage['omitted_entries'], 0)
        # Older failures cannot always crowd successful source navigation out
        # merely because the latter has an omitted original-text body.
        supplied_entries = [entry for group in expand_history_view(coverage['entries'])
                            for entry in group['observations']]
        self.assertTrue(any(entry['tool'] == 'read_file' and entry['status'] == 'ok'
                            for entry in supplied_entries))
        supplied = [i for start, end in coverage['supplied_entry_ranges'] for i in range(start, end)]
        self.assertEqual(supplied, [entry['entry_index'] for entry in supplied_entries])
        self.assertEqual(len(supplied) + coverage['omitted_entries'], len(operations))
        self.assertLessEqual(metadata['estimated_input_tokens'], 4500)
        # Saved working.v2 still supplies exactly its referenced view; no auto index.
        old, _ = select(**arguments)
        explicit_old, _ = select(**arguments, operation_history=None)
        self.assertEqual(old, explicit_old)
        self.assertNotIn('operation_overview', json.loads(old[1]['content'])['working_view'])
        self.assertNotIn('Observed execution outcome', json.dumps(old))

    def test_policy_is_explicit_and_ranked_default_is_unchanged(self):
        arguments = dict(observations=[observation(0, 'Original observed source')],
                         work_notes=[{'text': 'Owner judgment', 'records': []}])
        self.assertEqual(select(**arguments), select(**arguments, material_policy='ranked'))
        with self.assertRaisesRegex(ValueError, 'selection policy'):
            select(material_policy='unsupported')
        with self.assertRaisesRegex(ValueError, 'Incomplete tool call/result block'):
            select(material_policy='referenced', turn_blocks=[block(0, 'result')[:1]])


class CheckpointFailureTests(unittest.TestCase):
    def test_checkpoint_sources_preserve_native_and_inspection_contracts(self):
        decision = {'role':'assistant', 'tool_calls':[{'id':'read', 'function':{
            'name':'read_file', 'arguments':'{}'}}]}
        result = {'tool_name':'read_file', 'status':'ok', 'data':{'path':'inputs/a.txt',
            'absolute_path':'/workspace/inputs/a.txt', 'sha256':'a'*64, 'text':'original'}}
        original = {'turn':0, 'request_count':1, 'decision':decision, 'call_id':'read',
                    'arguments':{}, 'result':result}
        row = {'turn':0, 'record':'original', 'request_count':1, 'call_id':'read',
            'tool':'read_file', 'truncated':False, 'observation_text':json.dumps(result),
            'source_sha256':observation_digest(result), 'source_currentness':'file_bytes'}
        view = {'native_supply_policy':TURN_SUPPLY_POLICY,
            'turn_decisions':[{'turn':0, 'reply':decision}], 'observations':[row]}
        sources = {'original':original}
        def check(value, owner):
            validate_checkpoint_sources(value, source=lambda item:sources.get(item['record']),
                owner=owner, journal=owner == 'native', before_turn=1 if owner == 'inspection' else None)

        for owner in ('native', 'inspection'):
            with self.subTest(owner=owner):
                check(view, owner)
                for field, value in (('record', 'missing'), ('call_id', 'other'),
                        ('source_sha256', 'changed'), ('source_currentness', 'unknown'),
                        ('observation_text', json.dumps({**result, 'data':{'text':'changed'}}))):
                    with self.subTest(field=field), self.assertRaises(ValueError):
                        check({**view, 'observations':[{**row, field:value}]}, owner)
                changed_decision = {**view, 'turn_decisions':[{'turn':0, 'reply':{
                    **decision, 'content':'changed'}}]}
                with self.assertRaises(ValueError):
                    check(changed_decision, owner)

        changed_count = {**view, 'observations':[{**row, 'request_count':2}]}
        with self.assertRaises(ValueError):
            check(changed_count, 'native')
        check(changed_count, 'inspection')
        sources['original'] = {**original, 'turn':1}
        for owner in ('native', 'inspection'):
            with self.subTest(owner=owner, changed_source_turn=True), self.assertRaises(ValueError):
                check(view, owner)
        sources['original'] = original
        with self.assertRaises(ValueError):
            validate_checkpoint_sources(view, source=lambda item:sources.get(item['record']),
                owner='inspection', before_turn=0)

        partial = {**view, 'observations':[{**row, 'truncated':True,
                                            'observation_text':json.dumps({'partial':'view'})}]}
        check(partial, 'native')  # Its identity is bound without claiming complete coverage.
        with self.assertRaises(ValueError):
            check(partial, 'inspection')

        # A rejected inspection action may retain its recorded placeholder;
        # native history must still use the arguments in the original decision.
        rejected_decision = {'role':'assistant', 'tool_calls':[{'id':'read', 'function':{
            'name':'read_file', 'arguments':'{'}}]}
        sources['original'] = {**original, 'decision':rejected_decision}
        rejected = {**view, 'turn_decisions':[{'turn':0, 'reply':rejected_decision}],
            'observations':[{**row, 'recorded_arguments':{}}]}
        check(rejected, 'inspection')
        with self.assertRaises(ValueError):
            check(rejected, 'native')

        # Unmarked native history only required its original position; old
        # inspection history required the full result and recorded arguments.
        sources['original'] = original
        legacy = {'observations':[{'turn':0, 'record':'original', 'request_count':1,
            'arguments':{}, 'truncated':False, 'observation_text':json.dumps(result)}]}
        check(legacy, 'native')
        check(legacy, 'inspection')
        partial_legacy = {'observations':[{**legacy['observations'][0], 'truncated':True}]}
        check(partial_legacy, 'native')
        with self.assertRaises(ValueError):
            check(partial_legacy, 'inspection')

    def test_owner_selected_checkpoint_keeps_decision_and_original_receipt(self):
        # A large useful decision can have a tiny execution receipt. The shared
        # selector has already considered replay cost; the model window size
        # must not impose a second, unrelated floor on that receipt.
        arguments = {'path': 'analysis.py', 'content': 'value = 1\n' * 350}
        decision = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'write', 'function': {'name': 'write_file',
                'arguments': json.dumps(arguments)}}]}
        result = {'tool_name': 'write_file', 'status': 'ok', 'data': {'path': 'analysis.py'}}
        row = {'turn': 2, 'record': 'records/write.json', 'arguments': arguments,
            'decision': decision, 'call_id': 'write', 'result': result}
        previous = {'notes': [], 'through_turn': 0, 'compacted_records': set(),
            'attempted_through_turn': 1, 'attempted_records': {'records/old.json'}}
        options = dict(requests=['Analyze the inputs'], previous=previous, rows=[row],
            target=100000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertIsNone(prepare_checkpoint_input(**options))  # Historical policy remains unchanged.
        view = prepare_checkpoint_input(**options, economic_floor=1000)
        self.assertEqual([item['record'] for item in view['observations']], [row['record']])
        self.assertEqual(checkpoint_decisions(view), {2: decision})
        self.assertEqual(previous['attempted_records'], {'records/old.json'})
        self.assertIsNone(prepare_checkpoint_input(**{**options, 'previous': {
            **previous, 'attempted_records': {'records/old.json', row['record']}}},
            economic_floor=1000))
        tiny_arguments = {'path': 'small.py', 'content': 'x = 1'}
        tiny_decision = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'small', 'function': {'name': 'write_file',
                'arguments': json.dumps(tiny_arguments)}}]}
        tiny = {**row, 'record': 'records/small.json', 'arguments': tiny_arguments,
            'decision': tiny_decision, 'call_id': 'small'}
        oversized_arguments = {'path': 'large.py', 'content': 'x' * 120000}
        oversized_decision = {'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': 'large', 'function': {'name': 'write_file',
                'arguments': json.dumps(oversized_arguments)}}]}
        oversized = {**row, 'turn': 3, 'record': 'records/large.json',
            'arguments': oversized_arguments, 'decision': oversized_decision, 'call_id': 'large'}
        self.assertIsNone(prepare_checkpoint_input(**{**options, 'rows': [tiny, oversized]},
            economic_floor=1000))
        both = {**decision, 'tool_calls': [*decision['tool_calls'],
            {'id': 'read', 'function': {'name': 'read_file', 'arguments': '{}'}}]}
        first = {**row, 'decision': both}
        missing = {**row, 'record': 'records/read.json', 'decision': both, 'call_id': 'read',
            'arguments': {}, 'result': {'tool_name': 'read_file', 'data': {'text': 'x' * 120000}}}
        self.assertIsNone(prepare_checkpoint_input(**{**options, 'rows': [first, missing]},
            economic_floor=1000))

    def test_note_owners_do_not_mark_indirect_or_conflicting_sources_current(self):
        from simple_ar.app.operation_tools import OperationTools
        from simple_ar.app.work_memory import note_currentness
        from simple_ar.tools.inspection import InspectionToolGateway

        def read(digest):
            return {'tool_name':'read_file', 'status':'ok', 'data':{
                'path':'inputs/a.txt', 'absolute_path':'/workspace/inputs/a.txt', 'sha256':digest}}
        first, second = read('a'*64), read('b'*64)
        process = {'tool_name':'run_process', 'status':'ok', 'data':{'returncode':0}}
        items = [{**value, 'record':f'attempts/r{i}/native_result.json'}
                 for i,value in enumerate((first,second,process))]
        owner = OperationTools.__new__(OperationTools)
        owner.config, owner.professional = {'native_supply_policy':TURN_SUPPLY_POLICY}, False
        owner.observations = lambda:items
        owner.workspace = SimpleNamespace(artifact_hashes=lambda value:value)
        mixed = {'text':'Working observation', 'records':['records/r1/native_result.json',
                                                           'records/r2/native_result.json']}
        conflicting = {'text':'Comparison across versions', 'records':['records/r0/native_result.json',
                                                                        'records/r1/native_result.json']}
        notes = owner.bind_notes([mixed,conflicting])
        self.assertEqual([note['source_integrity'] for note in notes], ['unknown','unknown'])
        self.assertEqual([note['current'] for note in owner.project_notes(notes)], [None,None])
        self.assertEqual(notes[1]['observed_versions']['/workspace/inputs/a.txt'], 'a'*64)
        inspector_rows = [{'record':item['record'], 'result':value, 'source_currentness':'file_bytes'}
                          for item,value in zip(items,(first,second,process))]
        inspection_notes = InspectionToolGateway._bind_conversation_notes(
            [{**mixed, 'records':[items[1]['record'],items[2]['record']]},
             {**conflicting, 'records':[items[0]['record'],items[1]['record']]}], inspector_rows)
        self.assertEqual([note['source_integrity'] for note in inspection_notes], ['unknown','unknown'])
        owner.workspace.artifact_hashes = lambda versions: {path:'c'*64 for path in versions}
        self.assertEqual([note['current'] for note in owner.project_notes(notes)], [False,False])
        self.assertFalse(note_currentness(inspection_notes[0], versions_match=lambda versions: False))
        self.assertIsNone(note_currentness(inspection_notes[0], versions_match=lambda versions: True))
        owner.config = {'native_supply_policy':'receipts.v1'}
        old = owner.bind_notes([conflicting])[0]
        self.assertNotIn('source_integrity',old)
        self.assertEqual(old['observed_versions']['/workspace/inputs/a.txt'], 'b'*64)

    def test_checkpoint_note_binding_reuses_one_view_and_reloads_on_next_restoration(self):
        from simple_ar.app.operation_tools import OperationTools
        owner = OperationTools.__new__(OperationTools)
        owner.config, owner.professional = {'native_supply_policy': TURN_SUPPLY_POLICY}, False
        calls = []
        rows = [{'record': 'attempts/read/native_result.json', 'tool_name': 'read_file', 'status': 'ok',
                 'data': {'path': 'inputs/a.txt', 'absolute_path': '/workspace/inputs/a.txt', 'sha256': 'a'*64}}]
        owner.observations = lambda: calls.append(1) or list(rows)
        note = {'text': 'Observed result', 'records': ['records/read/native_result.json']}

        bind = owner.checkpoint_note_binder()
        first = bind([note])
        self.assertEqual(bind([note]), first)
        self.assertEqual(len(calls), 1)
        rows[0] = {**rows[0], 'data': {**rows[0]['data'], 'sha256': 'b'*64}}
        second = owner.checkpoint_note_binder()([note])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(first[0]['observed_versions'], second[0]['observed_versions'])

    def test_checkpoint_supplies_complete_decision_and_preserves_source_bindings(self):
        decision = {'role':'assistant', 'content':'Compare the measurements before reporting.',
            'tool_calls':[{'id':'read', 'function':{'name':'read_file', 'arguments':'{}'}},
                          {'id':'check', 'function':{'name':'run_process', 'arguments':'{}'}},
                          {'id':'prepared', 'function':{'name':'prepare_implementation', 'arguments':'{}'}},
                          {'id':'invalid', 'function':{'name':'read_file', 'arguments':'{'}}]}
        rows = [
            {'turn':0, 'record':'records/huge.json', 'arguments':{},
             'call_id':'huge', 'decision':{'role':'assistant', 'content':'Old large work',
                 'tool_calls':[{'id':'huge', 'function':{'name':'read_file', 'arguments':'{}'}}]},
             'result':{'tool_name':'read_file', 'data':{'text':'x'*40000}}},
            {'turn':1, 'record':'records/read.json', 'arguments':{}, 'decision':decision, 'call_id':'read',
             'source_currentness':'file_bytes', 'result':{'tool_name':'read_file', 'status':'ok',
                 'data':{'path':'inputs/values.txt', 'sha256':'a'*64, 'text':'Actual observed values'}}},
            {'turn':1, 'record':'records/check.json', 'arguments':{}, 'decision':decision, 'call_id':'check',
             'result':{'tool_name':'run_process', 'data':{'returncode':0}}},
            {'turn':1, 'record':'records/invalid.json', 'arguments':{}, 'decision':decision, 'call_id':'invalid',
             'result':{'tool_name':'read_file', 'status':'blocked', 'error':'Invalid arguments'}},
        ]
        old_records = [f'records/prior-{i}.json' for i in range(16)]
        previous = {'notes':[{'text':'Earlier work', 'records':old_records, 'observed_versions':{}}],
                    'through_turn':0, 'compacted_records':set()}
        view = prepare_checkpoint_input(requests=['Explain the results'], previous=previous,
            rows=rows, target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertEqual([row['record'] for row in view['observations']],
                         ['records/read.json', 'records/check.json', 'records/invalid.json'])
        self.assertEqual(checkpoint_decisions(view), {1:decision})
        self.assertNotIn('arguments', view['observations'][0])
        self.assertNotIn('recorded_arguments', view['observations'][0])
        self.assertEqual(view['observations'][2]['recorded_arguments'], {})
        self.assertEqual(checkpoint_arguments(view['observations'][2], checkpoint_decisions(view)), {})
        self.assertEqual(json.loads(view['observations'][0]['observation_text']), rows[1]['result'])
        response = {'notes':[{'text':'Current comparison', 'observations':['p0', 'o1']}]}
        bound = _bound_response(response, view, lambda notes:notes)
        self.assertEqual(bound[0]['records'], old_records+['records/read.json'])
        with self.assertRaisesRegex(ValueError, 'decisions do not match'):
            checkpoint_decisions({**view, 'turn_decisions':[]})
        legacy = {**view, 'native_supply_policy':'receipts.v1'}
        self.assertEqual(_bound_response(response, legacy, lambda notes:notes)[0]['records'], old_records)
        projected = {**previous, 'notes':[{**previous['notes'][0], 'current':False,
                                            'version_scope':'Changed since the observation'}]}
        refreshed = {**projected, 'notes':[{**projected['notes'][0], 'current':True,
                                            'version_scope':'Original bytes restored'}]}
        make_view = lambda memory:prepare_checkpoint_input(requests=['Explain the results'], previous=memory,
            rows=rows, target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertEqual(make_view(projected), make_view(refreshed))
        self.assertNotIn('current', make_view(projected)['previous_notes'][0])

        # A long-lived note retains its useful wording when its growing source
        # list no longer fits, but cannot claim those sources were handed off.
        from simple_ar.app.professional_conversation import ProfessionalConversation
        large_records = [f'records/{i:04d}-' + 'x'*40 + '.json' for i in range(1000)]
        prior = {'text':'Working understanding to reassess', 'records':large_records,
                 'observed_versions':{}, 'handoff_policy':CITATION_HANDOFF_POLICY}
        previous = {'notes':[prior], 'through_turn':0, 'compacted_records':set(large_records)}
        compact = prepare_checkpoint_input(requests=['Explain the results'], previous=previous,
            rows=rows[1:2], target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertEqual(compact['previous_notes'][0]['text'], prior['text'])
        self.assertTrue(compact['previous_notes'][0]['references_omitted'])
        bound = _bound_response(response, compact, lambda notes:notes)
        self.assertEqual(bound[0]['records'], ['records/read.json'])
        self.assertEqual(bound[0]['source_integrity'], 'unknown')
        memory = ProfessionalConversation.adopt(previous=previous,
            snapshot={'through_turn':2, 'notes':bound}, view=compact,
            operations_for_turn=lambda turn: {'records/read.json':{}} if turn == 1 else {})
        self.assertEqual(memory['compacted_records'], {'records/read.json'})
        self.assertNotIn(large_records[0], memory['compacted_records'])

        # A specialized owner's useful presentation is still handed over,
        # without pretending that omitted source fields were summarized.
        partial = {**rows[1], 'record':'records/prepared.json', 'call_id':'prepared',
                   'original':{**rows[1]['result'], 'omitted':'original material'}, 'complete':False}
        partial_view = prepare_checkpoint_input(requests=['Explain the results'],
            previous={'notes':[], 'through_turn':0, 'compacted_records':set()},
            rows=[partial, rows[1]], target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertTrue(partial_view['observations'][0]['truncated'])
        self.assertEqual(checkpoint_decisions(partial_view), {1:decision})
        snapshot = {'through_turn':2, 'notes':_bound_response(
            {'notes':[{'text':'Observed work', 'observations':['o1']}]}, partial_view, lambda notes:notes)}
        memory = ProfessionalConversation.adopt(previous={'notes':[], 'through_turn':0,
            'compacted_records':set()}, snapshot=snapshot, view=partial_view,
            operations_for_turn=lambda turn: {'records/prepared.json':{}, 'records/read.json':{}} if turn == 1 else {})
        self.assertEqual(memory['compacted_records'], set())

        # Mixing a changed/omitted source or an indirect process result with a
        # current file cannot make the old conclusion current again.
        indirect = {**view, 'previous_notes':[], 'observations':[
            {**view['observations'][0], 'source_currentness':'file_bytes'},
            {**view['observations'][1], 'source_currentness':'unknown'}]}
        bound = _bound_response({'notes':[{'text':'Historical comparison', 'observations':['o1','o2']}]},
            indirect, lambda notes:notes)
        self.assertEqual(bound[0]['source_integrity'], 'unknown')
        memory = ProfessionalConversation.adopt(previous={'notes':[], 'through_turn':0,
            'compacted_records':set()}, snapshot={'through_turn':2,'notes':bound}, view=indirect,
            operations_for_turn=lambda turn:{'records/read.json':{}, 'records/check.json':{}} if turn == 1 else {})
        self.assertEqual(memory['compacted_records'], {'records/read.json','records/check.json'})

        # A complete but temporarily stale binding is different from missing
        # provenance: it can become current again if its original bytes return.
        stale = {'id':'p0', 'text':'Earlier file observation', 'records':['records/read.json'],
                 'observed_versions':{'inputs/values.txt':'a'*64}, 'current':False}
        stale_view = {**view, 'previous_notes':[stale]}
        stale_note = _bound_response({'notes':[{'text':'Earlier file observation','observations':['p0']}]},
            stale_view, lambda notes:[{**note,'observed_versions':stale['observed_versions']} for note in notes])[0]
        self.assertNotIn('source_integrity', stale_note)
        historical = {**view, 'previous_notes':[], 'observations':[
            {**view['observations'][0], 'tool':'assistant_reply', 'records':[],
             'source_currentness':'historical'}, view['observations'][1]]}
        decision_note = _bound_response({'notes':[{'text':'Earlier decision','observations':['o1']}]},
            historical, lambda notes:notes)[0]
        self.assertEqual(decision_note['source_integrity'], 'unknown')
        repeated = {**historical, 'previous_notes':[{'id':'p0', **decision_note}], 'observations':[]}
        repeated_note = _bound_response({'notes':[{'text':'Earlier decision','observations':['p0']}]},
            repeated, lambda notes:notes)[0]
        self.assertEqual(repeated_note['source_integrity'], 'unknown')
        numeric = {**rows[1], 'result':{**rows[1]['result'],
            'data':{**rows[1]['result']['data'], 'value':float('nan')}}}
        numeric_view = prepare_checkpoint_input(requests=['Explain the results'],
            previous={'notes':[], 'through_turn':0, 'compacted_records':set()}, rows=[numeric],
            target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        recorded = numeric_view['observations'][0]
        self.assertEqual(recorded['source_sha256'], observation_digest(json.loads(recorded['observation_text'])))
        numeric_decision = {**decision, 'tool_calls':[
            {'id':'read', 'function':{'name':'read_file', 'arguments':'{"value":NaN}'}}]}
        numeric = {**rows[1], 'decision':numeric_decision, 'arguments':{'value':float('nan')}}
        numeric_view = prepare_checkpoint_input(requests=['Explain the results'],
            previous={'notes':[], 'through_turn':0, 'compacted_records':set()}, rows=[numeric],
            target=10000, estimate_tokens=len, native_supply_policy=TURN_SUPPLY_POLICY)
        self.assertNotIn('recorded_arguments', numeric_view['observations'][0])
        self.assertEqual(summary_coverage(previous={'notes':[], 'through_turn':0, 'compacted_records':set()},
            snapshot={'through_turn':2, 'notes':[{'text':'Measured', 'records':['records/read.json']}]},
            view=numeric_view, operations_for_turn=lambda turn:{'records/read.json':{'value':float('nan')}}
            if turn == 1 else {'records/prior':{}}), 0)
        from simple_ar.app.work_memory import summary_records
        self.assertEqual(summary_records(previous={'notes':[], 'through_turn':0, 'compacted_records':set()},
            snapshot={'notes':[{'text':'Measured', 'records':['records/read.json']}]}, view=numeric_view,
            operations_for_turn=lambda turn:{'records/read.json':{'value':float('nan')}}), {'records/read.json'})

    def test_rolling_adoption_replaces_knowledge_and_reopens_dropped_receipts(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation

        old = {'text':'Earlier conclusion', 'records':['old'],
            'observed_versions':{}, 'handoff_policy':CITATION_HANDOFF_POLICY}
        previous = {'notes':[old], 'through_turn':1, 'compacted_records':{'old'}}
        view = {'native_supply_policy':'receipts.v1',
            'handoff_policy':CITATION_HANDOFF_POLICY, 'archive_through_turn':3,
            'previous_notes':[{'id':'p0', **old}], 'observations':[
                {'record':'new', 'turn':2, 'truncated':False, 'arguments':{}}]}
        before = deepcopy((previous, view))
        for records, covered in ((['new'], {'new'}), (['old'], {'old'}),
                                 ([], set()), (['invented'], set())):
            with self.subTest(records=records):
                snapshot = {'through_turn':3, 'notes':[{'text':'Revised conclusion', 'records':records}]}
                actual = ProfessionalConversation.adopt(previous=previous, snapshot=snapshot,
                    view=view, operations_for_turn=lambda turn:{['old','gap','new'][turn]:{}})
                self.assertEqual(actual['notes'], snapshot['notes'])
                self.assertEqual(actual['compacted_records'], covered)
                self.assertEqual(actual['through_turn'], 1 if covered == {'old'} else 0)
                self.assertEqual(actual['attempted_through_turn'], 3)
                self.assertEqual(ProfessionalConversation.adopt(previous=previous, snapshot=snapshot,
                    view=deepcopy(view), operations_for_turn=lambda turn:{['old','gap','new'][turn]:{}}), actual)
        for snapshot in (None, {'notes':[]}):
            self.assertEqual(ProfessionalConversation.adopt(previous=previous, snapshot=snapshot,
                view=view, operations_for_turn=lambda _:{}), {**previous, 'attempted_through_turn':3,
                    'attempted_records':{'new'}})
        # Neither capacity-omitted, partial nor mismatched-argument receipts are coverage.
        for change in ({'truncated':True}, {'arguments':{'offset':1}}):
            actual = ProfessionalConversation.adopt(previous=previous,
                snapshot={'through_turn':3, 'notes':[{'text':'New judgment', 'records':['new']}]},
                view={**view, 'observations':[{**view['observations'][0], **change}]},
                operations_for_turn=lambda turn:{['old','gap','new'][turn]:{}})
            self.assertEqual(actual['compacted_records'], set())
        self.assertEqual((previous, view), before)

    def test_rolling_references_stay_bounded_past_256_receipts(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation

        originals = [observation(i, f'Original measurement {i}', role='historical_observation') for i in range(273)]
        before = deepcopy(originals)
        previous = {'notes':[], 'through_turn':0, 'compacted_records':set()}
        # Fill all 16 x 16 reference slots, then replace them with genuinely new
        # results. The old all-history invariant would reject both later batches.
        for start, end in ((0, 256), (256, 272), (272, 273)):
            batch = originals[start:end]
            view = {'native_supply_policy':'receipts.v1',
                'handoff_policy':CITATION_HANDOFF_POLICY, 'archive_through_turn':end,
                'previous_notes':[{'id':f'p{i}', **note} for i,note in enumerate(previous['notes'])],
                'observations':[{'id':f'o{i}', 'turn':row['turn'], 'record':row['record'],
                    'truncated':False, 'arguments':{}} for i,row in enumerate(batch)]}
            response = {'notes':[{'text':f'Current findings {i}',
                'observations':[f'o{j}' for j in range(i, min(i+16, len(batch)))]}
                for i in range(0, len(batch), 16)]}
            snapshot = {'through_turn':end, 'notes':_bound_response(response, view, self.bind)}
            previous = ProfessionalConversation.adopt(previous=previous, snapshot=snapshot, view=view,
                operations_for_turn=lambda turn:{f'records/{turn}.json':{}})
            self.assertEqual(previous['compacted_records'], {row['record'] for row in batch})
            self.assertLessEqual(len(previous['compacted_records']), 256)
            self.assertEqual(previous['through_turn'], 256 if start == 0 else 0)
            self.assertEqual(previous['attempted_through_turn'], end)
        self.assertEqual(previous['attempted_records'], {row['record'] for row in originals})
        self.assertIsNone(prepare_checkpoint_input(requests=['Continue'], previous=previous,
            rows=[{'turn':row['turn'], 'record':row['record'], 'result':{'data':row['text']}}
                for row in originals], target=12000, estimate_tokens=estimate_tokens,
            native_supply_policy='receipts.v1'))
        # Finite supporting locators cannot launder all inherited references
        # through one p-ID. Only the bound records can remain handed off.
        response = {'notes':[{'text':'Still relevant', 'observations':['p0', 'o0']}]}
        bound = _bound_response(response, view, self.bind)
        self.assertEqual(len(bound[0]['records']), 16)
        self.assertNotIn('records/272.json', bound[0]['records'])
        processed = processed_journal_turns(originals, previous['compacted_records'])
        self.assertEqual(processed, [272])
        messages, _ = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            work_notes=previous['notes'], observations=originals, processed_turns=processed,
            selected_records=['records/0.json'], turn_blocks=[[] for _ in originals]+[block(273, 'Continue')],
            context_token_target=100000, version_resolver=lambda _: 'recorded-digest')
        self.assertIn('Original measurement 0', json.dumps(messages))
        self.assertEqual(originals, before)

    def test_rolling_purchase_floor_counts_only_fitted_new_originals(self):
        previous = {'notes':[], 'through_turn':0, 'compacted_records':set(), 'attempted_through_turn':1,
            'attempted_records':{'records/old.json'}}
        old = {'turn':0, 'record':'records/old.json', 'result':{'data':{'text':'old '*20000}}}
        tiny = {'turn':1, 'record':'records/tiny.json', 'result':{'data':{'value':1}}}
        oversized = {'turn':2, 'record':'records/huge.json', 'result':{'data':{'text':'large '*20000}}}
        before = self.ledger.to_dict()
        with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
            for index, rows in enumerate(([old], [old, tiny], [old, oversized], [old, tiny, oversized])):
                with self.subTest(rows=index):
                    store = ArtifactStore(self.root/f'skipped-{index}')
                    self.assertEqual(compact_work(store=store, client=self.client, requests=['Different request'],
                        previous=previous, rows=rows, bind_notes=self.bind, estimate_tokens=estimate_tokens,
                        attempt_id='native-turn-test', native_supply_policy='receipts.v1'), (None, ()))
                    self.assertFalse(store.exists('memory_input.json'))
            send.assert_not_called()
        self.assertEqual(self.ledger.to_dict(), before)

        # A below-floor omission waits for enough actually fitted work. An
        # oversized receipt cannot inflate this floor or seal the tiny gap.
        fresh = {'turn':3, 'record':'records/fresh.json', 'result':{'tool_name':'observe',
            'data':{'text':'Additional measured conditions. '*500}}}
        rows = [old, tiny, oversized, fresh]
        from simple_ar.app.professional_conversation import ProfessionalConversation
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Tiny result and new conditions","observations":["o1","o2"]}]}')) as send:
            snapshot, _ = compact_work(store=self.store, client=self.client, requests=['Continue'],
                previous=previous, rows=rows, bind_notes=self.bind, estimate_tokens=estimate_tokens,
                attempt_id='native-turn-test', native_supply_policy='receipts.v1')
            view = self.store.read_json('memory_input.json')
            self.assertEqual([row['record'] for row in view['observations']],
                [tiny['record'], fresh['record']])
            memory = ProfessionalConversation.adopt(previous=previous, snapshot=snapshot, view=view,
                operations_for_turn=lambda turn:{row['record']:{} for row in rows if row['turn'] == turn})
            self.assertNotIn(oversized['record'], memory['attempted_records'])
            self.assertNotIn(oversized['record'], memory['compacted_records'])
            paid = self.ledger.to_dict()
            self.assertEqual(compact_work(store=ArtifactStore(self.root/'still-oversized'),
                client=self.client, requests=['Continue'], previous=memory, rows=rows, bind_notes=self.bind,
                estimate_tokens=estimate_tokens, attempt_id='native-turn-test',
                native_supply_policy='receipts.v1'), (None, ()))
            self.assertEqual(self.ledger.to_dict(), paid)
            self.assertEqual(send.call_count, 1)

    def test_rolling_uncited_or_failed_response_is_accounted_and_not_rebought(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation

        rows = [{'turn':0, 'record':'records/new.json', 'result':{
            'tool_name':'observe', 'data':{'text':'Actual original '*300}}}]
        for index, response in enumerate(({'notes':[{'text':'Revised model judgment', 'observations':[]}]},
                                          {'notes':[]})):
            with self.subTest(response=response):
                attempt = f'no-gain-{index}'
                store = ArtifactStore(self.root/attempt)
                client = self.client.with_budget(self.ledger, session_id='session', attempt_id=attempt)
                previous = {'notes':[], 'through_turn':0, 'compacted_records':set()}
                with patch('simple_ar.integrations.llm._call_openai_sdk',
                        return_value=self.response(json.dumps(response))) as send:
                    snapshot, refs = compact_work(store=store, client=client, requests=['First input'],
                        previous=previous, rows=rows, bind_notes=self.bind, estimate_tokens=estimate_tokens,
                        attempt_id=attempt, native_supply_policy='receipts.v1')
                    self.assertEqual(checkpoint_artifacts(store=store, ledger=self.ledger, attempt_id=attempt,
                        session_id='session', bind_notes=self.bind), refs)
                    view = store.read_json('memory_input.json')
                    actual = ProfessionalConversation.adopt(previous=previous, snapshot=snapshot, view=view,
                        failure=store.read_json('memory_failure.json') if snapshot is None else None,
                        operations_for_turn=lambda _:{'records/new.json':{}})
                    self.assertEqual(actual['compacted_records'], set())
                    self.assertEqual(actual['through_turn'], 0)
                    self.assertEqual(actual['attempted_through_turn'], 1)
                    if snapshot is not None:
                        self.assertEqual(actual['notes'][0]['text'], 'Revised model judgment')
                    paid = self.ledger.to_dict()
                    self.assertEqual(compact_work(store=ArtifactStore(self.root/f'retry-{index}'),
                        client=client, requests=['Changed question, same original history'],
                        previous=actual, rows=deepcopy(rows), bind_notes=self.bind,
                        estimate_tokens=estimate_tokens, attempt_id=attempt,
                        native_supply_policy='receipts.v1'), (None, ()))
                    self.assertEqual(self.ledger.to_dict(), paid)
                    self.assertEqual(send.call_count, 1)
                store.write_json('memory_input.json', {**view, 'native_supply_policy':'unknown.v9'})
                with self.assertRaisesRegex(ValueError, 'native supply contract'):
                    checkpoint_artifacts(store=store, ledger=self.ledger, attempt_id=attempt,
                        session_id='session', bind_notes=self.bind)
                store.write_json('memory_input.json', view)

    def test_rolling_full_prior_notes_leave_room_for_actual_new_evidence(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation

        notes = self.bind([{'text':'Earlier interpretation. '*60,
            'records':[f'records/{i}/{j}/'+('long-locator-'*12)+'.json' for j in range(16)]}
            for i in range(16)])
        previous = {'notes':notes, 'through_turn':0, 'attempted_through_turn':1,
            'compacted_records':{record for note in notes for record in note['records']}}
        before = deepcopy(previous)
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Updated measurement","observations":["o1"]}]}')) as send:
            snapshot, refs = compact_work(store=self.store, client=self.client, requests=['Update findings'],
                previous=previous, rows=[{'turn':1, 'record':'records/new.json', 'result':{
                    'tool_name':'observe', 'data':{'text':'New measurement with conditions. '*500}}}],
                bind_notes=self.bind, estimate_tokens=estimate_tokens, attempt_id='native-turn-test',
                native_supply_policy='receipts.v1')
            view = self.store.read_json('memory_input.json')
            self.assertLess(len(view['previous_notes']), 16)
            self.assertEqual([row['record'] for row in view['observations']], ['records/new.json'])
            actual = ProfessionalConversation.adopt(previous=previous, snapshot=snapshot, view=view,
                operations_for_turn=lambda turn:{'records/new.json':{}} if turn == 1 else {'old':{}})
            self.assertEqual(actual['compacted_records'], {'records/new.json'})
            self.assertEqual(actual['attempted_through_turn'], 2)
            self.assertEqual(self.artifacts(), refs)
            self.assertEqual(send.call_count, 1)
        self.assertEqual(previous, before)

    def test_capacity_omissions_are_not_purchases_even_within_one_turn(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation

        for layout in ('same-turn', 'suffix', 'earlier-gap'):
            for failed in (False, True):
                with self.subTest(layout=layout, failed=failed):
                    first = {'turn':2 if layout == 'earlier-gap' else 0,
                        'record':'records/first.json', 'arguments':{},
                        'result':{'tool_name':'observe', 'data':{'text':'a'*7000}}}
                    omitted = {'turn':1 if layout == 'suffix' else 0,
                        'record':'records/omitted.json', 'arguments':{},
                        'result':{'tool_name':'observe', 'data':{'text':'b'*(24000 if layout == 'earlier-gap' else 7000)}}}
                    rows = [omitted, first] if layout == 'earlier-gap' else [first, omitted]
                    originals = deepcopy(rows)
                    memory = {'notes':[], 'through_turn':0, 'compacted_records':set()}
                    prefix = f'{layout}-{failed}'
                    operations = lambda turn:{row['record']:{} for row in rows if row['turn'] == turn}

                    def buy(number, capacity):
                        attempt = f'{prefix}-{number}'
                        store = ArtifactStore(self.root/attempt)
                        client = LLMClient(replace(self.client._settings, context_input_tokens=capacity),
                            budget_ledger=self.ledger, budget_session_id='session', budget_attempt_id=attempt)
                        snapshot, refs = compact_work(store=store, client=client, requests=['Continue'],
                            previous=memory, rows=rows, bind_notes=self.bind, estimate_tokens=len,
                            attempt_id=attempt, native_supply_policy='receipts.v1')
                        if not refs:
                            return None, memory
                        self.assertEqual(checkpoint_artifacts(store=store, ledger=self.ledger,
                            attempt_id=attempt, session_id='session', bind_notes=self.bind), refs)
                        view = store.read_json('memory_input.json')
                        adopted = ProfessionalConversation.adopt(previous=memory, snapshot=snapshot, view=view,
                            failure=store.read_json('memory_failure.json') if snapshot is None else None,
                            operations_for_turn=operations)
                        return view, adopted

                    with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                            '{"notes":[]}' if failed else
                            '{"notes":[{"text":"Actual supplied result","observations":["o1"]}]}')) as send:
                        view, memory = buy(0, 12000)
                        self.assertEqual([row['record'] for row in view['observations']], [first['record']])
                        self.assertEqual(memory['attempted_records'], {first['record']})
                        self.assertEqual(memory['attempted_through_turn'], first['turn']+1)
                        self.assertNotIn(omitted['record'], memory['compacted_records'])
                        if layout == 'earlier-gap':
                            paid = self.ledger.to_dict()
                            self.assertIsNone(buy('still-too-small', 12000)[0])
                            self.assertEqual(self.ledger.to_dict(), paid)
                        send.return_value = self.response(
                            '{"notes":[{"text":"Previously omitted result","observations":["o1"]}]}')
                        view, memory = buy(1, 36000 if layout == 'earlier-gap' else 12000)
                        self.assertEqual([row['record'] for row in view['observations']], [omitted['record']])
                        self.assertEqual(memory['attempted_records'], {first['record'], omitted['record']})
                        self.assertEqual(memory['attempted_through_turn'], max(row['turn'] for row in rows)+1)
                        paid = self.ledger.to_dict()
                        for retry in range(3):
                            self.assertIsNone(buy(f'no-new-{retry}', 36000)[0])
                        self.assertEqual(send.call_count, 2)
                        self.assertEqual(self.ledger.to_dict(), paid)
                    self.assertEqual(rows, originals)

    def test_inspection_paid_checkpoint_recovers_capacity_retired_notes(self):
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
        from simple_ar.tools.inspection import InspectionToolGateway

        notes = self.bind([{'text':'Earlier interpretation. '*60,
            'records':[f'records/{i}/{j}/'+('long-locator-'*12)+'.json' for j in range(16)]}
            for i in range(16)])
        memory = {'notes':notes, 'through_turn':0, 'attempted_through_turn':1,
            'attempted_records':{'records/prior.json'},
            'compacted_records':{record for note in notes for record in note['records']}}
        rows = [{'turn':1, 'record':'records/new.json', 'arguments':{}, 'result':{
            'tool_name':'observe', 'data':{'text':'New measurement with conditions. '*500}}}]
        before = deepcopy((memory, rows))
        owner = InspectionToolGateway(self.root, SimpleNamespace(ledger=self.ledger),
            lambda attempt:self.client.with_budget(self.ledger, session_id='session', attempt_id=attempt))
        saved = {'identity':'inspection', 'user_requests':['Update findings'],
            'native_supply_policy':'receipts.v1'}
        record, captured = {}, {}
        store = ArtifactStore(self.root/'tools/inspection/inspection/sources-2')

        def dispatch(kind, position, arguments, perform):
            captured['perform'] = perform
            captured['result'] = perform('paid-checkpoint')
            raise InterruptedError('After paid checkpoint, before dispatcher receipt')

        def prepare(conversation, project, **kwargs):
            kwargs['checkpoint'](ConversationInput([], [], {'compaction_turns':[1]}), 0)

        with patch.object(owner, '_conversation_rows', return_value=rows), \
                patch.object(owner, '_conversation_memory', return_value=memory), \
                patch.object(ProfessionalConversation, 'prepare', autospec=True, side_effect=prepare), \
                patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                    '{"notes":[{"text":"Updated measurement","observations":["o1"]}]}')) as send:
            with self.assertRaises(InterruptedError):
                owner._prepare_conversation_round(saved, SimpleNamespace(goal='Update findings'),
                    [], [], {}, None, self.client, 2, [10], dispatch=dispatch,
                    record=record, save_round=Mock())
            view = store.read_json('memory_input.json')
            self.assertLess(len(view['previous_notes']), len(notes))
            self.assertEqual(record['checkpoint_input']['context_input_tokens'],
                self.client._settings.context_input_tokens)
            paid = self.ledger.to_dict()
            retained = {path:path.read_bytes() for path in store.root.glob('memory_*.json')}
            with patch.object(owner, 'client_factory', side_effect=AssertionError(
                    'Recovery uses retained capacity, not the active client configuration')):
                for _ in range(2):
                    self.assertEqual(captured['perform'].recover('paid-checkpoint'), captured['result'])
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.ledger.to_dict(), paid)
            self.assertTrue(all(path.read_bytes() == raw for path, raw in retained.items()))
            # Even a correctly paid/digested checkpoint must match the owner's
            # exact preparation, including its original source content.
            rows[0]['result']['data']['text'] = 'Changed original'
            with self.assertRaisesRegex(ValueError, 'retained preparation'):
                captured['perform'].recover('paid-checkpoint')
            rows[0]['result'] = deepcopy(before[1][0]['result'])
            self.assertEqual(self.ledger.to_dict(), paid)
        self.assertEqual((memory, rows), before)

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = ArtifactStore(self.root/'attempts'/'native-turn-test')
        self.ledger = BudgetLedger({'llm_requests':20,'total_tokens':200000})
        self.client = LLMClient(LLMSettings(api_key='fixture',model='fixture',api_mode='chat',
            retry_attempts=1,max_output_tokens=8192,context_input_tokens=12000),
            budget_ledger=self.ledger,budget_session_id='session',budget_attempt_id='native-turn-test')
        self.previous = {'through_turn':0,'notes':[{'text':'Preserve committed understanding',
            'records':['records/prior/native_result.json']}]}

    @staticmethod
    def bind(notes):
        return [{**note,'observed_versions':{},'scope':'model_note_not_verified_evidence'} for note in notes]

    def test_coverage_requires_complete_receipts_and_an_unbroken_nonempty_note_chain(self):
        note = self.bind([{'text':'Actual retained work','records':['records/0.json']}])[0]
        previous = {'through_turn':0,'notes':[]}
        view = {'previous_notes':[],'observations':[
            {'turn':0,'record':'records/0.json','truncated':False},
            {'turn':1,'record':'records/1.json','truncated':True}]}
        records = lambda n:{f'records/{n}.json':{}}
        snapshot = {'through_turn':2,'notes':[note]}
        self.assertEqual(summary_coverage(previous=previous,snapshot=snapshot,view=view,operations_for_turn=records),1)
        # A later snapshot cannot leap over the unsupplied suffix of turn1.
        previous = {'through_turn':1,'notes':[note]}
        later = {'previous_notes':[{'id':'p0',**note}], 'observations':[
            {'turn':2,'record':'records/2.json','truncated':False}]}
        self.assertEqual(summary_coverage(previous=previous,snapshot={**snapshot,'through_turn':3},
            view=later,operations_for_turn=records),1)
        # Empty snapshots reset coverage, including future inherited coverage.
        self.assertEqual(summary_coverage(previous=previous,snapshot={**snapshot,'notes':[]},
            view=view,operations_for_turn=records),0)
        self.assertEqual(summary_coverage(previous={'through_turn':0,'notes':[]},snapshot={**snapshot,'through_turn':3},
            view=later,operations_for_turn=records),0)
        # Supplying the missing complete receipt lets the same chain advance.
        later['observations'].insert(0,{'turn':1,'record':'records/1.json','truncated':False})
        self.assertEqual(summary_coverage(previous=previous,snapshot={**snapshot,'through_turn':3},
            view=later,operations_for_turn=records),3)
        later['previous_notes'] = []
        self.assertEqual(summary_coverage(previous=previous,snapshot={**snapshot,'through_turn':3},
            view=later,operations_for_turn=records),0)
        self.assertEqual(summary_coverage(previous={'through_turn':0,'notes':[]},snapshot=snapshot,
            view={'previous_notes':[],'observations':[]},operations_for_turn=lambda n:{}),0)
        authored = {'content':'Complete generated implementation','path':'analysis.py'}
        one = {'previous_notes':[],'observations':[{'turn':0,'record':'records/0.json','truncated':False,'arguments':{}}]}
        self.assertEqual(summary_coverage(previous={'through_turn':0,'notes':[]},snapshot={**snapshot,'through_turn':1},
            view=one,operations_for_turn=lambda n:{'records/0.json':authored}),0)
        one['observations'][0]['arguments'] = authored
        self.assertEqual(summary_coverage(previous={'through_turn':0,'notes':[]},snapshot={**snapshot,'through_turn':1},
            view=one,operations_for_turn=lambda n:{'records/0.json':authored}),1)

    def compact(self, *, rows=None, required_decision_tokens=0):
        return compact_work(store=self.store,client=self.client,requests=['Explain the measurements'],
            previous=self.previous,rows=rows if rows is not None else [{'turn':0,'record':'records/read/native_result.json',
                'result':{'tool_name':'read_file','status':'ok','data':{
                    'text':'Original recorded material with measurement scope. '*400}}}],
            bind_notes=self.bind,estimate_tokens=estimate_tokens,attempt_id='native-turn-test',
            required_decision_tokens=required_decision_tokens)

    def artifacts(self):
        return checkpoint_artifacts(store=self.store,ledger=self.ledger,attempt_id='native-turn-test',
            session_id='session',bind_notes=self.bind)

    @staticmethod
    def response(content, finish='stop'):
        return {'choices':[{'message':{'role':'assistant','content':content},'finish_reason':finish}],
            'usage':{'prompt_tokens':100,'completion_tokens':40,'total_tokens':140}}

    def test_journal_inherits_caller_output_cap_and_legacy_keeps_8192(self):
        for journal, cap in ((True, 1024), (True, 16384), (True, 65536),
                             (True, None), (False, 65536)):
            with self.subTest(journal=journal, cap=cap):
                self.store = ArtifactStore(self.root/f'{journal}-{cap}')
                # TODO: generic finite uncapped LLM reservations need a real
                # provider output maximum; this case tests unlimited inheritance.
                self.ledger = BudgetLedger({'llm_requests':3,
                    'total_tokens':None if cap is None else 200000})
                client = LLMClient(replace(self.client._settings, max_output_tokens=None),
                    budget_ledger=self.ledger, budget_session_id='session', budget_attempt_id='native-turn-test')
                self.client = client.with_output_limit(cap) if cap is not None else client
                self.previous = {'through_turn':0, 'notes':[], **({'compacted_records':set()} if journal else {})}
                rows = ([{'turn':0, 'record':'records/small.json', 'result':{
                    'tool_name':'read_file', 'status':'ok', 'data':{'text':'Complete short observation'}}}]
                    if journal else None)
                with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                        '{"notes":[{"text":"Retained scope","observations":["o1"]}]}')) as send:
                    snapshot, _ = self.compact(rows=rows)
                self.assertIsNotNone(snapshot)
                send.assert_called_once()
                request = send.call_args.args[1]
                self.assertEqual(request.get('max_tokens'), cap if journal else 8192)
                self.assertEqual(self.client.effective_output_limit(), cap)
                self.assertEqual(json.loads(self.store.read_json('memory_input.json')
                    ['observations'][0]['observation_text'])['status'], 'ok')

    def test_journal_without_output_cap_skips_optional_work_under_finite_budget(self):
        self.previous['compacted_records'] = set()
        self.client = LLMClient(replace(self.client._settings, max_output_tokens=None),
            budget_ledger=self.ledger, budget_session_id='session', budget_attempt_id='native-turn-test')
        before = self.ledger.to_dict()
        with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
            self.assertEqual(self.compact(required_decision_tokens=100), (None, ()))
        send.assert_not_called()
        self.assertEqual(self.ledger.to_dict(), before)
        self.assertFalse(self.store.exists('memory_input.json'))

    def test_journal_complete_batch_uses_actual_target_without_fraction_thresholds(self):
        from simple_ar.app.work_memory import SYSTEM
        self.previous = {'through_turn':0, 'notes':[], 'compacted_records':set()}
        rows = [{'turn':0, 'record':f'records/{i}.json', 'result':{
            'tool_name':'read_file', 'status':'ok', 'data':{'text':letter*18000}}}
            for i, letter in enumerate(('a', 'b'))]
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Both complete observations","observations":["o1","o2"]}]}')):
            snapshot, _ = self.compact(rows=rows)
        self.assertIsNotNone(snapshot)
        view = self.store.read_json('memory_input.json')
        self.assertEqual([json.loads(row['observation_text']) for row in view['observations']],
                         [row['result'] for row in rows])
        self.assertTrue(all(row['truncated'] is False for row in view['observations']))
        measured = estimate_tokens(SYSTEM) + estimate_tokens(json.dumps(view, ensure_ascii=False))
        self.assertGreater(measured, self.client._settings.context_input_tokens * 0.6)
        self.assertLessEqual(measured, self.client._settings.context_input_tokens)
        self.assertEqual(summary_coverage(previous=self.previous, snapshot=snapshot, view=view,
            operations_for_turn=lambda turn:{row['record']:{} for row in rows}), 1)

    def test_journal_notes_fit_remaining_budget_and_leave_next_request_payable(self):
        self.ledger = BudgetLedger({'llm_requests':2, 'total_tokens':85000})
        self.client = LLMClient(replace(self.client._settings,
            max_output_tokens=65536, context_input_tokens=128000), budget_ledger=self.ledger,
            budget_session_id='session', budget_attempt_id='native-turn-test')
        self.previous = {'through_turn':0, 'notes':[], 'compacted_records':set()}
        rows = [{'turn':0, 'record':'records/success.json', 'result':{
            'tool_name':'run_process', 'status':'ok', 'data':{'stdout':'Measured successfully'}}}]
        notes = [{'text':'Retain the successful measurement', 'observations':['o1']},
                 *[{'text':'\x00'*1500, 'observations':['o1']} for _ in range(15)]]
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                json.dumps({'notes':notes}))):
            snapshot, _ = self.compact(rows=rows, required_decision_tokens=7000)
        self.assertIsNotNone(snapshot)  # No hypothetical maximum-note envelope.
        target = min(self.client._settings.context_input_tokens,
                     self.ledger.remaining('total_tokens') - self.client.effective_output_limit())
        messages, budget = select(material_policy='retained', require_work_notes=True,
            fit_work_notes=True, work_notes=snapshot['notes'], context_token_target=target,
            schema_tokens=estimate_tokens('[]'), estimate_tokens=estimate_tokens)
        supplied = json.loads(messages[1]['content'])['working_view']['notes']
        self.assertTrue(supplied)
        self.assertEqual(supplied[0]['text'], notes[0]['text'])
        self.assertLess(len(supplied), len(snapshot['notes']))
        self.assertLessEqual(budget['estimated_input_tokens'], target)
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response('Continue')):
            self.client.chat_turn(messages, tools=[], label='next-decision')
        self.assertEqual([entry.status for entry in self.ledger.entries], ['settled', 'settled'])

    def test_settled_truncated_malformed_and_invalid_schema_keep_previous_notes(self):
        for content, finish in [('partial notes','length'),('not JSON','stop'),('{"notes":[]}','stop'),
                ('{"notes":"invalid"}','stop'),
                ('{"notes":[{"text":"invented","observations":["missing"]}]}','stop')]:
            with self.subTest(finish=finish,content=content):
                self.store = ArtifactStore(self.root/str(len(self.ledger.entries)))
                before = deepcopy(self.previous)
                with patch('simple_ar.integrations.llm._call_openai_sdk',
                        return_value=self.response(content,finish)) as send:
                    snapshot, refs = self.compact()
                self.assertIsNone(snapshot)
                self.assertEqual(send.call_count,1)
                self.assertEqual(self.previous,before)
                self.assertFalse(self.store.exists('memory_snapshot.json'))
                failure = self.store.read_json('memory_failure.json')
                self.assertTrue(failure['diagnostic'])
                self.assertEqual(failure['finish_reason'],'length' if finish == 'length' else None)
                self.assertEqual(refs,self.artifacts())
                self.assertEqual(self.ledger.entries[-1].status,'settled')
                self.assertEqual(self.ledger.entries[-1].actual['total_tokens'],140)
                with self.assertRaisesRegex(ValueError,'no automatic replay'):
                    self.compact()

    def test_checkpoint_recovery_rebinds_recorded_response_and_rejects_tampering(self):
        response = {'notes':[{'text':'Retained scope','observations':['o1','p0']}]}
        with patch('simple_ar.integrations.llm._call_openai_sdk',
                return_value=self.response(json.dumps(response))) as send:
            snapshot, refs = self.compact()
            self.assertEqual(refs,self.artifacts())
            self.assertEqual(send.call_count,1)
            actual = self.store.read_json('memory_input.json')['observations'][0]
            self.assertFalse(actual['truncated'])
            self.assertNotIn('request_count', actual)
            self.assertNotIn('records', actual)
            self.assertGreater(len(actual['observation_text']),16000)
            self.assertEqual(json.loads(actual['observation_text'])['data']['text'],
                             'Original recorded material with measurement scope. '*400)
        for field, value in [('input_sha256','wrong'),('through_turn',99),('notes',[])]:
            with self.subTest(field=field):
                self.store.write_json('memory_snapshot.json',{**snapshot,field:value})
                with self.assertRaisesRegex(ValueError,'disagrees'):
                    self.artifacts()
        self.store.write_json('memory_snapshot.json',snapshot)
        self.store.resolve('memory_response.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.artifacts()

    def test_failed_archive_scope_requires_new_material_without_claiming_summary_coverage(self):
        large = {'turn':0,'record':'records/read/native_result.json','result':{
            'tool_name':'read_file','data':{'text':'Original recorded observation. '*700}}}
        # The bounded prompt fits one chunk, while later history remains unsummarized.
        rows = [large,{**large,'turn':1,'record':'records/read-1/native_result.json'}]
        with patch('simple_ar.integrations.llm._call_openai_sdk',
                return_value=self.response('truncated','length')) as send:
            snapshot, _ = self.compact(rows=rows)
            self.assertIsNone(snapshot)
            failure = self.store.read_json('memory_failure.json')
            self.assertEqual(failure['attempted_through_turn'],2)
            self.assertEqual(failure['supplied_through_turn'],1)
            self.previous['attempted_through_turn'] = failure['attempted_through_turn']
            self.store = ArtifactStore(self.root/'next-attempt')
            self.assertEqual(self.compact(rows=rows),(None,()))
            small = {'turn':2,'record':'records/small/native_result.json',
                'result':{'tool_name':'read_file','data':{'text':'small new observation'}}}
            self.assertEqual(self.compact(rows=[*rows,small]),(None,()))
            self.assertEqual(send.call_count,1)
            # Enough newly archived bytes permits one fresh attempt; old unsummarized
            # material is still included rather than advancing through it as a success.
            self.compact(rows=[*rows,{**large,'turn':3,'record':'records/new/native_result.json'}])
            self.assertEqual(send.call_count,2)
            view = self.store.read_json('memory_input.json')
            self.assertEqual(view['observations'][0]['record'],large['record'])
            self.assertEqual(self.store.read_json('memory_failure.json')['attempted_through_turn'],4)
            self.previous['attempted_through_turn'] = 4
            self.store = ArtifactStore(self.root/'after-new-failure')
            self.assertEqual(self.compact(rows=[*rows,{**large,'turn':3}]),(None,()))
            self.assertEqual(send.call_count,2)

    def test_failure_watermark_must_match_recorded_input_scope(self):
        with patch('simple_ar.integrations.llm._call_openai_sdk',
                return_value=self.response('truncated','length')):
            self.compact()
        failure = self.store.read_json('memory_failure.json')
        self.store.write_json('memory_failure.json',{**failure,'attempted_through_turn':99})
        with self.assertRaisesRegex(ValueError,'Invalid retained checkpoint failure'):
            self.artifacts()

    def test_unknown_reserved_or_misidentified_checkpoint_charges_stop(self):
        for status in ('unknown','reserved','wrong-purpose'):
            with self.subTest(status=status):
                ledger = BudgetLedger({'llm_requests':20,'total_tokens':200000})
                ledger.reserve('checkpoint',{'llm_requests':1,'total_tokens':5000},
                    session_id='session',attempt_id='native-turn-test',
                    logical_call_id='working-memory-native-turn-test',
                    purpose='other' if status == 'wrong-purpose' else 'working-memory')
                if status == 'unknown':
                    ledger.mark_unknown('checkpoint',reason='interrupted',retain_reservation=True)
                elif status == 'wrong-purpose':
                    ledger.settle('checkpoint',{'llm_requests':1,'total_tokens':140})
                with self.assertRaises(ValueError):
                    checkpoint_entries(ledger,'native-turn-test','session')

    def test_same_attempt_checkpoints_bind_their_own_calls_and_never_borrow_settlement(self):
        stores = [ArtifactStore(self.root/f'round-{number}') for number in range(2)]
        rows = [{'turn':0, 'record':'records/read.json', 'result':{
            'tool_name':'read_file', 'status':'ok', 'data':{'text':'x'*20000}}}]
        calls = ['working-memory-native-turn-test-sources-'+str(number) for number in range(3)]
        response = self.response('{"notes":[{"text":"Scoped work","observations":["o1"]}]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as send:
            for store, call_id in zip(stores, calls):
                snapshot, refs = compact_work(store=store, client=self.client, requests=['goal'],
                    previous={'through_turn':0, 'notes':[], 'compacted_records':set()}, rows=rows,
                    bind_notes=self.bind, estimate_tokens=estimate_tokens,
                    attempt_id='native-turn-test', call_id=call_id)
                self.assertEqual(snapshot['logical_call_id'], call_id)
                self.assertEqual(checkpoint_artifacts(store=store, ledger=self.ledger,
                    attempt_id='native-turn-test', session_id='session', bind_notes=self.bind, call_id=call_id), refs)
            self.assertEqual(send.call_count, 2)
        # Even another settled call in the very same attempt cannot fund this snapshot.
        with self.assertRaisesRegex(ValueError, 'disagrees'):
            checkpoint_artifacts(store=stores[0], ledger=self.ledger, attempt_id='native-turn-test',
                session_id='session', bind_notes=self.bind, call_id=calls[1])
        self.ledger.reserve('pending-round', {'llm_requests':1, 'total_tokens':5000},
            session_id='session', attempt_id='native-turn-test', logical_call_id=calls[2], purpose='working-memory')
        for status in ('reserved', 'unknown'):
            with self.subTest(status=status):
                if status == 'unknown':
                    self.ledger.mark_unknown('pending-round', reason='interrupted', retain_reservation=True)
                before = self.ledger.to_dict()
                with self.assertRaises(ValueError):
                    checkpoint_entries(self.ledger, 'native-turn-test', 'session', call_id=calls[2])
                self.assertTrue(checkpoint_entries(self.ledger, 'native-turn-test', 'session', call_id=calls[0]))
                self.assertEqual(self.ledger.to_dict(), before)

    def test_shared_prepare_protects_parent_and_exit_and_closes_before_buying_optional_work(self):
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
        self.ledger = BudgetLedger({'llm_requests':3, 'total_tokens':200000})
        client = self.client.with_budget(self.ledger, session_id='session', attempt_id='native-turn-test')
        conversation = ProfessionalConversation(client)
        def project(closing):
            return ConversationInput([{'role':'user', 'content':'Conclude' if closing else 'Investigate'}],
                [], {}, closing=closing)
        checkpoint = Mock()
        followup = lambda value:{'llm_requests':1, 'total_tokens':10000}
        with self.ledger.protect_remaining({'llm_requests':1, 'total_tokens':5000}):
            value = conversation.prepare(project, followup=followup, additional=True)
            def respond(*args, **kwargs):
                self.assertEqual(self.ledger.remaining('llm_requests'), 0)
                return self.response('Review remains incomplete.')
            with patch('simple_ar.integrations.llm._call_openai_sdk', side_effect=respond):
                conversation.call(value, label='review', call_id='review', additional=True)
            # Only the final opinion can fit beside the parent's reserved exit.
            value = conversation.prepare(project, followup=followup, checkpoint=checkpoint, additional=True)
            self.assertTrue(value.closing)
            self.assertEqual(value.messages, project(True).messages)
            checkpoint.assert_not_called()
            ordinary = conversation.prepare(project, followup=followup,
                closing_enabled=False, additional=True)
            self.assertFalse(ordinary.closing)  # Available operations is not a forced terminal workflow.
            self.assertEqual(ordinary.messages, project(False).messages)
        self.assertEqual(self.ledger.remaining('llm_requests'), 2)

    def test_shared_prepare_checkpoints_only_capacity_pressure_and_keeps_legacy_default(self):
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
        conversation = ProfessionalConversation(self.client)
        for projection, expected in (({'compaction_needed':False}, 0),
                ({'working_context':{'compaction_needed':False}}, 0),
                ({'compaction_needed':True}, 1), ({}, 1)):
            with self.subTest(projection=projection):
                project = Mock(side_effect=lambda closing:ConversationInput(
                    [{'role':'user', 'content':'Retain the observations'}], [], projection))
                checkpoint = Mock()
                before = self.ledger.to_dict()
                value = conversation.prepare(project, checkpoint=checkpoint,
                    followup=lambda value:{'llm_requests':1, 'total_tokens':10000})
                self.assertFalse(value.closing)
                self.assertEqual(checkpoint.call_count, expected)
                self.assertEqual(project.call_count, 1 + expected)
                self.assertEqual(self.ledger.to_dict(), before)

    def test_shared_exit_first_measures_exit_before_unaffordable_ordinary_projection(self):
        from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
        conversation = ProfessionalConversation(self.client)
        tools = [{'type':'function', 'function':{'name':'write_file'}}]
        exit_input = ConversationInput([{'role':'user', 'content':'Finish known work; disclose unfinished work.'}],
            tools, {'compaction_needed':False})
        cost = conversation.tokens(exit_input)
        followup = lambda value:{'llm_requests':1, 'total_tokens':conversation.tokens(value)}
        for remaining, closing, calls in ((2*cost-1, True, [True]), (3*cost, False, [True,False])):
            with self.subTest(remaining=remaining):
                self.ledger.limits['total_tokens'] = remaining
                projected = []
                def project(final):
                    projected.append(final)
                    if not final and closing:
                        self.fail('Ordinary projection would reject before the affordable exit could be offered')
                    return deepcopy(exit_input)
                checkpoint = Mock()
                before = self.ledger.to_dict()
                value = conversation.prepare(project, followup=followup, checkpoint=checkpoint, exit_first=True)
                self.assertEqual(projected, calls)
                self.assertEqual(value.closing, closing)
                self.assertEqual(value.tools, tools)
                self.assertEqual(value.reserve, {} if closing else {'llm_requests':1,'total_tokens':cost})
                self.assertEqual(self.client.effective_output_limit(), 8192)
                checkpoint.assert_not_called()
                self.assertEqual(self.ledger.to_dict(), before)

    def test_shared_exit_first_unknown_capacity_never_projects_or_sends(self):
        from simple_ar.app.professional_conversation import ProfessionalConversation
        self.ledger.reserve('unsettled', {'llm_requests':1, 'total_tokens':1000},
            session_id='session', attempt_id='native-turn-test', purpose='native-turn')
        self.ledger.mark_unknown('unsettled', reason='No usage receipt')
        project, followup = Mock(), Mock()
        before = self.ledger.to_dict()
        with self.assertRaisesRegex(ValueError, 'capacity is unknown'):
            ProfessionalConversation(self.client).prepare(project, followup=followup, exit_first=True)
        project.assert_not_called()
        followup.assert_not_called()
        self.assertEqual(self.ledger.to_dict(), before)

    def test_saved_terminal_batch_shape_survives_current_policy_changes(self):
        app = ToolApplication.__new__(ToolApplication)
        app.operations = None
        calls = [{'terminal':False}, {'terminal':True}]
        app.state = {'config':{'completion_policy':'available_operations.v2'},
            'turns':[{'remaining':calls, 'reply':'attempts/native-turn-test/native_result.json'}]}
        app.session = SimpleNamespace(manifest=SimpleNamespace(
            budget=SimpleNamespace(max_attempts=10, attempts=1)),
            store=SimpleNamespace(exists=lambda _:True, read_json=Mock()))
        app._terminal_intent = lambda call:call['terminal']
        for policy, contract, expected in (
                ('available_operations.v2', 'single_terminal.v1', False),
                ('closing_decision.v2', 'terminal_batch.v1', True)):
            with self.subTest(policy=policy, saved_contract=contract):
                app.state['config']['completion_policy'] = policy
                app.session.store.read_json.return_value = {'closing_required':True, 'closing_action_contract':contract}
                self.assertTrue(app._closing_decision())
                self.assertEqual(app._terminal_batch(), expected)

    def test_historical_model_projection_without_compaction_turns_keeps_its_original_cutoff(self):
        app = ToolApplication.__new__(ToolApplication)
        app.state = {'config':{'memory_protocol':'journal.v1'}}
        self.enterContext(patch.object(ToolApplication, 'schemas', new_callable=PropertyMock, return_value=[]))
        app.session = SimpleNamespace(manifest=SimpleNamespace(
            budget=SimpleNamespace(max_attempts=100, attempts=0)))
        app._request_capacity = lambda:10
        app._completion_floor = lambda client, messages:{'llm_requests':1, 'total_tokens':1000}
        app._messages = Mock(return_value=[{'role':'system','content':'Respect the original contract'},
            {'role':'user','content':json.dumps({'working_context':{'recent_start_turn':2}})}])
        app._compact_work = Mock(return_value=())
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        before = self.ledger.to_dict()
        conversation, value, refs, _ = app._professional_model_input(context, self.client)
        app._compact_work.assert_called_once_with(context, self.client, 2,
            decision_tokens=conversation.tokens(value), additional_protection=True, selected_turns=None)
        self.assertEqual(refs, ())
        self.assertEqual(self.ledger.to_dict(), before)

    def test_explicit_checkpoint_recovery_keeps_bounded_unknown_entries_and_fees(self):
        entry = self.ledger.reserve('checkpoint', {'llm_requests':1,'total_tokens':5000},
            session_id='session', attempt_id='native-turn-test',
            logical_call_id='working-memory-native-turn-test', purpose='working-memory')
        self.ledger.mark_unknown('checkpoint', reason='response interrupted',
            known_actual={'llm_requests':1}, retain_reservation=True)
        before = self.ledger.to_dict()
        remaining = self.ledger.remaining('total_tokens'), self.ledger.remaining('llm_requests')
        with self.assertRaises(ValueError):
            checkpoint_entries(self.ledger, 'native-turn-test', 'session')
        recovered = checkpoint_entries(self.ledger, 'native-turn-test', 'session',
            allow_bounded_unknown=True)
        self.assertEqual(recovered, [entry])
        self.assertIs(recovered[0], entry)
        self.assertEqual(entry.actual_source, 'reservation_bound')
        self.assertEqual(self.ledger.to_dict(), before)
        self.assertEqual((self.ledger.remaining('total_tokens'), self.ledger.remaining('llm_requests')), remaining)
        self.assertEqual(remaining, (195000, 19))
        # A settled fallback may coexist with that unknown physical attempt;
        # recovery still does not authorize ordinary checkpoint adoption.
        fallback = self.ledger.reserve('fallback', {'llm_requests':1,'total_tokens':5000},
            session_id='session', attempt_id='native-turn-test',
            logical_call_id='working-memory-native-turn-test', purpose='working-memory-no-response-format')
        self.ledger.settle('fallback', {'llm_requests':1,'total_tokens':140})
        before = self.ledger.to_dict()
        recovered = checkpoint_entries(self.ledger, 'native-turn-test', 'session', allow_bounded_unknown=True)
        self.assertIs(recovered[0], entry)
        self.assertIs(recovered[1], fallback)
        with self.assertRaises(ValueError):
            checkpoint_entries(self.ledger, 'native-turn-test', 'session')
        self.assertEqual(self.ledger.to_dict(), before)

    def test_checkpoint_recovery_rejects_unbounded_or_misidentified_entries(self):
        valid = dict(session_id='session', attempt_id='native-turn-test',
            logical_call_id='working-memory-native-turn-test', purpose='working-memory',
            status='unknown', actual_source='reservation_bound',
            reserved={'llm_requests':1,'total_tokens':5000}, actual={'llm_requests':1})
        invalid = [
            {'session_id':'other'}, {'attempt_id':'other'}, {'logical_call_id':'other'},
            {'purpose':'native-turn'}, {'status':'reserved'}, {'actual_source':None},
            {'reserved':{'llm_requests':1}},
            {'reserved':{'llm_requests':True,'total_tokens':5000}},
            {'reserved':{'llm_requests':2,'total_tokens':5000}},
            {'reserved':{'llm_requests':1,'total_tokens':0}},
            {'reserved':{'llm_requests':1,'total_tokens':-1}},
            {'reserved':{'llm_requests':1,'total_tokens':True}},
            {'reserved':{'llm_requests':1,'total_tokens':5000.0}},
            {'reserved':{'llm_requests':1,'total_tokens':float('inf')}},
            {'reserved':{'llm_requests':1,'total_tokens':float('nan')}},
            {'reserved':{'llm_requests':1,'total_tokens':5000,'other':1}},
            {'actual':{}}, {'actual':{'llm_requests':0}}, {'actual':{'llm_requests':True}},
            {'actual':{'llm_requests':1,'total_tokens':5001}},
            {'actual':{'llm_requests':1,'total_tokens':-1}},
            {'actual':{'llm_requests':1,'total_tokens':float('inf')}},
            {'actual':{'llm_requests':1,'total_tokens':float('nan')}},
            {'actual':{'llm_requests':1,'total_tokens':'100'}},
            {'actual':{'llm_requests':1,'total_tokens':True}},
            {'actual':{'llm_requests':1,'other':0}},
        ]
        for override in invalid:
            with self.subTest(override=override):
                entry = SimpleNamespace(**{**valid, **override})
                before = deepcopy(vars(entry))
                with self.assertRaises(ValueError):
                    checkpoint_entries(SimpleNamespace(entries=[entry]), 'native-turn-test', 'session',
                        allow_bounded_unknown=True)
                self.assertEqual(vars(entry).keys(), before.keys())
                self.assertIs(entry.reserved, valid['reserved'] if 'reserved' not in override else override['reserved'])
                # A settled response does not excuse an invalid companion charge.
                if override.get('attempt_id') != 'other':
                    settled = SimpleNamespace(**{**valid, 'status':'settled'})
                    with self.assertRaises(ValueError):
                        checkpoint_entries(SimpleNamespace(entries=[settled, entry]),
                            'native-turn-test', 'session', allow_bounded_unknown=True)

    def test_checkpoint_recovery_still_needs_a_settled_or_bounded_unknown_call(self):
        released = SimpleNamespace(session_id='session', attempt_id='native-turn-test',
            logical_call_id='working-memory-native-turn-test', purpose='working-memory', status='released')
        for entries in ([], [released]):
            with self.subTest(entries=entries):
                with self.assertRaises(ValueError):
                    checkpoint_entries(SimpleNamespace(entries=entries), 'native-turn-test', 'session',
                        allow_bounded_unknown=True)

    def test_valid_response_with_unknown_checkpoint_attempt_is_not_adopted_or_swallowed(self):
        def respond(*args, **kwargs):
            self.ledger.reserve('unconfirmed',{'llm_requests':1,'total_tokens':10000},
                session_id='session',attempt_id='native-turn-test',
                logical_call_id='working-memory-native-turn-test',purpose='working-memory')
            self.ledger.mark_unknown('unconfirmed',reason='earlier physical attempt',retain_reservation=True)
            return self.response('{"notes":[]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk',side_effect=respond) as send:
            with self.assertRaisesRegex(ValueError,'unsettled charges'):
                self.compact()
            self.assertEqual(send.call_count,1)
        self.assertFalse(self.store.exists('memory_snapshot.json'))
        self.assertFalse(self.store.exists('memory_failure.json'))

    def test_optional_checkpoint_leaves_protected_main_decision_affordable(self):
        self.ledger = BudgetLedger({'llm_requests':2,'total_tokens':20000})
        self.client = self.client.with_budget(self.ledger,session_id='session',attempt_id='native-turn-test')
        with self.ledger.protect_remaining({'llm_requests':1,'total_tokens':18000}):
            with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
                self.assertEqual(self.compact(),(None,()))
                send.assert_not_called()
        self.assertFalse(self.store.exists('memory_input.json'))
        self.assertEqual(self.ledger.remaining('total_tokens'),20000)

    def test_legacy_required_notes_input_is_protected_before_buying_a_checkpoint(self):
        self.ledger = BudgetLedger({'llm_requests':2,'total_tokens':20000})
        self.client = self.client.with_budget(self.ledger,session_id='session',attempt_id='native-turn-test')
        # The old decision alone fits; escaped maximum-size future notes do not.
        with self.ledger.protect_remaining({'llm_requests':1,'total_tokens':100}):
            with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
                self.assertEqual(self.compact(required_decision_tokens=100), (None, ()))
                send.assert_not_called()
        self.assertFalse(self.store.exists('memory_input.json'))
        self.assertEqual(self.ledger.entries, ())
        response = self.response('{"notes":[{"text":"Retained","observations":["o1"]}]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as send:
            snapshot, _ = self.compact()
        self.assertIsNotNone(snapshot)
        self.assertEqual(send.call_count, 1)

    def test_required_checkpoint_floor_uses_maximum_with_outer_protection(self):
        self.ledger = BudgetLedger({'llm_requests':2,'total_tokens':200000})
        self.client = self.client.with_budget(self.ledger,session_id='session',attempt_id='native-turn-test')
        def respond(*args, **kwargs):
            reserved = sum(entry.reserved.get('total_tokens', 0)
                           for entry in self.ledger.entries if entry.status == 'reserved')
            self.assertEqual(self.ledger.remaining('total_tokens'), 200000-reserved-70000)
            self.assertEqual(self.ledger.remaining('llm_requests'), 0)
            return self.response('{"notes":[{"text":"Retained","observations":["o1"]}]}')
        with self.ledger.protect_remaining({'llm_requests':1,'total_tokens':70000}):
            with patch('simple_ar.integrations.llm._call_openai_sdk', side_effect=respond):
                snapshot, _ = self.compact(required_decision_tokens=100)
        self.assertIsNotNone(snapshot)
        self.assertEqual(self.ledger.remaining('total_tokens'), 200000-140)

    def test_legacy_note_reservation_includes_long_escaped_supplied_record_locators(self):
        self.ledger = BudgetLedger({'llm_requests':2,'total_tokens':200000})
        # Prior-note locators are support too; repeated backslashes cost more
        # after the JSON context is serialized into the decision messages.
        self.previous['notes'][0]['records'] = ['records/' + '\\'*2500 + str(i) for i in range(16)]
        # Keep the summary input small enough to reach the budget check.
        self.client = LLMClient(LLMSettings(api_key='fixture',model='fixture',api_mode='chat',
            retry_attempts=1,max_output_tokens=8192,context_input_tokens=300000),
            budget_ledger=self.ledger,budget_session_id='session',budget_attempt_id='native-turn-test')
        rows = [{'turn':0,'record':'records/read.json','result':{
            'tool_name':'read_file','data':{'text':'x'*300000}}}]
        with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
            self.assertEqual(self.compact(rows=rows, required_decision_tokens=100), (None, ()))
        send.assert_not_called()
        self.assertFalse(self.store.exists('memory_input.json'))
        self.assertEqual(self.ledger.entries, ())
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Retained","observations":["o1"]}]}')) as send:
            snapshot, _ = self.compact(rows=rows)
        self.assertIsNotNone(snapshot)
        self.assertEqual(send.call_count, 1)

    def test_required_checkpoint_does_not_release_unknown_checkpoint_charges(self):
        self.ledger.reserve('interrupted', {'llm_requests':1,'total_tokens':5000},
            session_id='session', attempt_id='prior', logical_call_id='working-memory-prior',
            purpose='working-memory')
        previous = deepcopy(self.previous)
        for status in ('reserved', 'unknown'):
            with self.subTest(status=status):
                if status == 'unknown':
                    self.ledger.mark_unknown('interrupted', reason='interrupted', retain_reservation=True)
                before = [entry.to_dict() for entry in self.ledger.entries]
                with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
                    self.assertEqual(self.compact(required_decision_tokens=100), (None, ()))
                    with self.assertRaisesRegex(ValueError, 'Unsettled provider work'):
                        self.compact()
                send.assert_not_called()
                self.assertEqual([entry.to_dict() for entry in self.ledger.entries], before)
                self.assertEqual(self.ledger.remaining('total_tokens'), 195000)
                self.assertEqual(self.ledger.remaining('llm_requests'), 19)
                self.assertEqual(self.previous, previous)
                self.assertFalse(self.store.exists('memory_input.json'))
                self.assertFalse(self.store.exists('memory_snapshot.json'))
                with self.assertRaises(ValueError):
                    checkpoint_entries(self.ledger, 'prior', 'session')

    def test_native_reply_judgment_has_timing_but_no_original_support_or_coverage(self):
        rows = [{'turn':0,'request_count':2,'record':'records/0/native_reply.json',
            'records':[], 'result':{'tool_name':'assistant_reply','data':'Working judgment. '*900}}]
        before = deepcopy(rows)
        response = self.response('{"notes":[{"text":"Retained judgment","observations":["o1"]}]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response):
            snapshot, refs = self.compact(rows=rows)
        self.assertEqual(rows, before)
        view = self.store.read_json('memory_input.json')
        self.assertEqual(view['observations'][0]['request_count'], 2)
        self.assertEqual(view['observations'][0]['records'], [])
        self.assertEqual(snapshot['notes'][0]['records'], [])
        self.assertEqual(summary_coverage(previous={'through_turn':0,'notes':[]},
            snapshot=snapshot, view=view, operations_for_turn=lambda turn:{}), 0)
        self.assertEqual(refs, self.artifacts())  # Recovery honors explicit empty support.

    def test_oversized_first_complete_turn_never_buys_or_claims_a_summary(self):
        rows = [{'turn':0,'record':'records/large.json','result':{
            'tool_name':'read_file','data':{'text':'large '*10000}}},
            {'turn':1,'record':'records/later.json','result':{
                'tool_name':'read_file','data':{'text':'small'}}}]
        before = deepcopy(self.previous)
        for required in (0, 100):
            with self.subTest(required=required):
                with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
                    self.assertEqual(self.compact(rows=rows, required_decision_tokens=required), (None, ()))
                send.assert_not_called()
                self.assertEqual(self.previous, before)
                self.assertFalse(self.store.exists('memory_input.json'))
                self.assertEqual(self.ledger.entries, ())

    def _journal_app(self, results):
        app = ToolApplication.__new__(ToolApplication)
        journal = ArtifactStore(self.root)
        tools, calls = [], []
        for index, result in enumerate(results):
            path = f'attempts/reads/result-{index}.json'
            journal.write_json(path, result)
            tools.append({'id':str(index), 'result':path})
            calls.append({'id':str(index), 'function':{'name':result['tool_name'], 'arguments':'{}'}})
        journal.write_json('attempts/reads/reply.json', {'tool_calls':calls})
        app.state = {'config':{'memory_protocol':'journal.v1'},
            'requests':['Explain the measurements'], 'turns':[{
                'reply':'attempts/reads/reply.json', 'tools':tools, 'request_count':1}]}
        attempts = [SimpleNamespace(attempt_id='reads', capability='tool', status='completed'),
            SimpleNamespace(attempt_id='native-turn-test', capability='native_turn', status='failed')]
        app.session = SimpleNamespace(store=journal, attempt_lineage=lambda:attempts,
            manifest=SimpleNamespace(current_attempt='native-turn-test', session_id='session'))
        app.ledger = self.ledger
        app.operations = SimpleNamespace(bind_notes=self.bind,
            checkpoint_note_binder=lambda:self.bind, project_notes=deepcopy)
        app._request_capacity = lambda:10
        app._delivery_feedback = lambda:None
        app._coordination_result = lambda result, record, **kwargs:result
        return app

    def test_saved_journal_and_inspection_checkpoints_recover_under_their_recorded_policy(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        original_root = self.root
        results = [{'tool_name':'observe', 'status':'ok', 'data':{'value':3}},
            {'tool_name':'observe', 'status':'error', 'error':'Original unresolved diagnostic'}]
        for role in ('main', 'inspection', 'shared-inspection'):
            for policy in (None, CITATION_HANDOFF_POLICY, TURN_SUPPLY_POLICY):
                with self.subTest(role=role, policy=policy):
                    self.root = original_root/role/(policy or 'historical')
                    self.store = ArtifactStore(self.root/'attempts'/'native-turn-test')
                    self.ledger = BudgetLedger({'llm_requests':20, 'total_tokens':200000})
                    call_id, attempt = None, 'native-turn-test'
                    requests = ['Explain the measurements']
                    if role == 'main':
                        app = self._journal_app(results)
                        if policy == TURN_SUPPLY_POLICY:
                            app.state['config']['native_supply_policy'] = policy
                        records = [f'records/reads/result-{i}.json' for i in range(2)]
                        decision = app.session.store.read_json('attempts/reads/reply.json')
                        load = app._working_memory
                    else:
                        identity = attempt = 'saved-inspection'
                        owner = InspectionToolGateway(self.root, SimpleNamespace(ledger=self.ledger),
                            Mock(side_effect=AssertionError('Recovery must not request new work')),
                            protocol='subjects.v3')
                        source = owner._path(identity, 'sources-0')
                        records = [source+f'#results/{i}' for i in range(2)]
                        decision = {'role':'assistant', 'tool_calls':[
                            {'id':str(i), **({'function':{'name':result['tool_name'], 'arguments':'{}'}}
                                if policy == TURN_SUPPLY_POLICY else {})}
                            for i, result in enumerate(results)]}
                        owner.store.write_json(source, {'completed':True, 'response':decision,
                            **({'model_receipt':{'attempt':attempt}} if role == 'shared-inspection' else {}),
                            'results':[{'call_id':str(i), 'arguments':{}, 'result':result}
                                for i, result in enumerate(results)]})
                        self.ledger.reserve('source-model', {'llm_requests':1, 'total_tokens':5000},
                            session_id='session', attempt_id=attempt,
                            logical_call_id='inspect-artifacts-'+identity, purpose='inspection')
                        self.ledger.settle('source-model', {'llm_requests':1, 'total_tokens':140})
                        call_id = f'working-memory-{identity}-sources-1'
                        self.store = ArtifactStore(self.root/f'tools/inspection/{identity}/sources-1')
                    view = {'requests':requests, 'previous_notes':[], 'archive_through_turn':1,
                        'observations':[{'id':f'o{i+1}', 'turn':0, 'request_count':1,
                            'record':record, 'arguments':{}, 'truncated':False,
                            'observation_text':json.dumps(result)}
                            for i, (record, result) in enumerate(zip(records, results))]}
                    notes = self.bind([{'text':'Only the successful result was retained', 'records':[records[0]]}])
                    if policy == TURN_SUPPLY_POLICY:
                        view['native_supply_policy'] = policy
                        view['turn_decisions'] = [{'turn':0, 'reply':decision}]
                        view['observations'] = [{key:value for key,value in row.items() if key != 'arguments'}
                            | {'tool':results[i]['tool_name'], 'call_id':str(i),
                               'source_sha256':observation_digest(results[i]), 'source_currentness':'unknown'}
                            for i, row in enumerate(view['observations'])]
                        notes = [{**note, 'source_integrity':'unknown'} for note in notes]
                    if policy:
                        view['handoff_policy'] = CITATION_HANDOFF_POLICY
                        notes = [{**note, 'handoff_policy':CITATION_HANDOFF_POLICY} for note in notes]
                    # Unmarked files model the saved v1 format, not today's writer.
                    self.store.write_json('memory_input.json', view)
                    self.store.write_json('memory_response.json', {'notes':[{
                        'text':notes[0]['text'], 'observations':['o1']}]})
                    snapshot = {'schema_version':'working_memory.v1', 'through_turn':1, 'notes':notes,
                        'input_sha256':hashlib.sha256(json.dumps(view, ensure_ascii=False).encode()).hexdigest(),
                        **({'logical_call_id':call_id} if call_id else {})}
                    self.store.write_json('memory_snapshot.json', snapshot)
                    self.ledger.reserve('checkpoint', {'llm_requests':1, 'total_tokens':5000},
                        session_id='session', attempt_id=attempt,
                        logical_call_id=call_id or 'working-memory-'+attempt, purpose='working-memory')
                    self.ledger.settle('checkpoint', {'llm_requests':1, 'total_tokens':140})
                    if role != 'main':
                        checkpoint = {path.relative_to(owner.root).as_posix():
                            hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (self.store.resolve(name) for name in
                                ('memory_input.json', 'memory_response.json', 'memory_snapshot.json'))}
                        pending = owner._path(identity, 'sources-1')
                        owner.store.write_json(pending, {'checkpoint':checkpoint,
                            **({'checkpoint_receipt':{'attempt':attempt}} if role == 'shared-inspection' else {})})
                        saved = {'identity':identity, 'arguments':{'goal':requests[0]},
                            'inspection_history_policy':'receipts.v1',
                            **({'native_supply_policy':policy} if policy == TURN_SUPPLY_POLICY else {}),
                            **({'execution_protocol':'shared.v1'} if role == 'shared-inspection' else {}),
                            'source_rounds':[owner._digest(source), owner._digest(pending)]}
                        load = lambda:owner._conversation_memory(saved, owner._conversation_rows(saved))
                    before = self.ledger.to_dict()
                    retained = {path:path.read_bytes() for path in self.root.rglob('*.json')}
                    memory = load()
                    self.assertEqual(memory['compacted_records'], {records[0]} if policy else set(records))
                    self.assertEqual(memory['through_turn'], 0 if policy else 1)
                    self.assertEqual(memory['notes'], notes)
                    rows = [observation(0, json.dumps(result), kind='operation_result', record=record)
                        for record, result in zip(records, results)]
                    messages, selection = select(material_policy='retained', require_work_notes=True,
                        fit_work_notes=True, work_notes=memory['notes'], observations=rows,
                        processed_turns=processed_journal_turns(rows, memory['compacted_records']),
                        turn_blocks=[block(0, 'Historical native body'), block(1, 'Continue work')],
                        context_token_target=12000)
                    self.assertEqual(results[1]['error'] in json.dumps(messages), bool(policy))
                    self.assertEqual(selection['selected_turns'], [0, 1] if policy else [1])
                    if role != 'main':
                        page = owner._read_history(saved, {'action':1, 'field':'result'}, before_turn=2).data
                        self.assertEqual(json.loads(page['text']), results[1])
                    elif not policy:
                        with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
                            self.assertEqual(app._compact_work(SimpleNamespace(store=self.store,
                                attempt=SimpleNamespace(attempt_id=attempt)), self.client.with_budget(
                                    self.ledger, session_id='session', attempt_id=attempt), 1), ())
                            send.assert_not_called()
                    self.assertEqual(load(), memory)
                    self.assertEqual(self.ledger.to_dict(), before)
                    self.assertTrue(all(path.read_bytes() == raw for path, raw in retained.items()))
                    if policy == TURN_SUPPLY_POLICY:
                        # Exercise the real owner adapters, not just the shared
                        # row validator: a changed original decision must not
                        # be adopted even when the checkpoint itself is intact.
                        if role == 'main':
                            original_path = app.session.store.resolve('attempts/reads/reply.json')
                            original_bytes = original_path.read_bytes()
                            app.session.store.write_json('attempts/reads/reply.json',
                                {**decision, 'content':'Changed original decision'})
                        else:
                            original_path = owner.store.resolve(source)
                            original_bytes = original_path.read_bytes()
                            original_round = owner.store.read_json(source)
                            saved_digest = saved['source_rounds'][0]
                            owner.store.write_json(source, {**original_round,
                                'response':{**decision, 'content':'Changed original decision'}})
                            saved['source_rounds'][0] = owner._digest(source)
                        try:
                            with self.assertRaisesRegex(ValueError, 'differs|disagrees'):
                                load()
                        finally:
                            original_path.write_bytes(original_bytes)
                            if role != 'main':
                                saved['source_rounds'][0] = saved_digest
                        self.assertEqual(load(), memory)
                        self.assertEqual(self.ledger.to_dict(), before)
                    if policy:
                        self.store.write_json('memory_snapshot.json', {**snapshot,
                            'notes':[{key:value for key,value in note.items() if key != 'handoff_policy'}
                                for note in notes]})
                        with self.assertRaisesRegex(ValueError, 'disagrees|changed'):
                            load()

    def test_journal_partial_turn_batches_derive_cited_frontier_without_rebuying(self):
        results = [{'tool_name':'read_file', 'status':status, 'data':{'text':text*26000}}
            for status, text in (('ok', 'a'), ('error', 'b'))]
        # Each receipt fits, but their complete native turn exceeds the window.
        app = self._journal_app(results)
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        response = self.response('{"notes":[{"text":"Retained work","observations":["o1"]}]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as send:
            app._compact_work(context, self.client, 1, selected_turns=[0])
            first = self.store.read_json('memory_input.json')
            self.assertEqual([row['record'] for row in first['observations']], ['records/reads/result-0.json'])
            self.assertEqual(json.loads(first['observations'][0]['observation_text']), results[0])
            self.assertFalse(first['observations'][0]['truncated'])
            self.assertNotIn('compacted_records', first)
            self.assertEqual(app._working_memory()['through_turn'], 0)
            self.assertEqual(app._working_memory()['compacted_records'], {'records/reads/result-0.json'})
            next_store = ArtifactStore(self.root/'attempts'/'next')
            app.session.attempt_lineage().append(SimpleNamespace(
                attempt_id='next', capability='native_turn', status='completed'))
            next_context = SimpleNamespace(store=next_store, attempt=SimpleNamespace(attempt_id='next'))
            next_client = self.client.with_budget(self.ledger, session_id='session', attempt_id='next')
            send.return_value = self.response(
                '{"notes":[{"text":"Retained earlier and new work","observations":["p0","o1"]}]}')
            app._compact_work(next_context, next_client, 1)
            second = next_store.read_json('memory_input.json')
            self.assertEqual([row['record'] for row in second['observations']], ['records/reads/result-1.json'])
            self.assertEqual(json.loads(second['observations'][0]['observation_text']), results[1])
            memory = app._working_memory()
            self.assertEqual(memory['through_turn'], 1)  # Both cited handoffs now cover the closed turn.
            self.assertEqual(memory['compacted_records'], {'records/reads/result-0.json', 'records/reads/result-1.json'})
            self.assertEqual(app._compact_work(next_context, next_client, 1), ())
            self.assertEqual(send.call_count, 2)

    def test_rolling_journal_does_not_rebuy_history_and_can_replace_old_knowledge(self):
        results = [{'tool_name':'observe', 'status':'ok', 'data':{'value':3}},
            {'tool_name':'observe', 'status':'error', 'error':'Unresolved original diagnostic'}]
        app = self._journal_app(results)
        app.state['config']['native_supply_policy'] = 'receipts.v1'
        original = {path:path.read_bytes() for path in (self.root/'attempts/reads').glob('*.json')}

        def context(identity):
            if identity != 'native-turn-test':
                app.session.attempt_lineage().append(SimpleNamespace(
                    attempt_id=identity, capability='native_turn', status='completed'))
            return SimpleNamespace(store=ArtifactStore(self.root/'attempts'/identity),
                attempt=SimpleNamespace(attempt_id=identity))

        def compact(identity, cutoff):
            return app._compact_work(context(identity), self.client.with_budget(self.ledger,
                session_id='session', attempt_id=identity), cutoff)

        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"First result retained","observations":["o1"]}]}')) as send:
            compact('native-turn-test', 1)
            first = app._working_memory()
            self.assertEqual(first['compacted_records'], {'records/reads/result-0.json'})
            self.assertEqual(first['attempted_through_turn'], 1)
            self.assertEqual(self.store.read_json('memory_input.json')['native_supply_policy'], 'receipts.v1')
            rows = [observation(0, json.dumps(result), kind='operation_result',
                record=f'records/reads/result-{i}.json') for i, result in enumerate(results)]
            messages, _ = select(material_policy='retained', require_work_notes=True,
                fit_work_notes=True, work_notes=first['notes'], observations=rows,
                processed_turns=processed_journal_turns(rows, first['compacted_records']),
                turn_blocks=[[], block(1, 'Continue')], context_token_target=12000)
            self.assertIn(results[1]['error'], json.dumps(messages))
            retained = {path:path.read_bytes() for path in (self.root/'attempts').rglob('memory_*.json')}
            ledger = self.ledger.to_dict()
            for number in range(9):
                self.assertEqual(compact(f'same-range-{number}', 1), ())
            self.assertEqual(send.call_count, 1)
            self.assertEqual(app._working_memory(), first)
            self.assertEqual(self.ledger.to_dict(), ledger)

            app.session.store.write_json('attempts/tiny/result.json', {
                'tool_name':'observe', 'status':'ok', 'data':{}})
            app.session.store.write_json('attempts/tiny/reply.json', {'tool_calls':[{
                'id':'tiny', 'function':{'name':'observe', 'arguments':'{}'}}]})
            app.state['turns'].append({'reply':'attempts/tiny/reply.json', 'request_count':1,
                'tools':[{'id':'tiny', 'result':'attempts/tiny/result.json'}]})
            self.assertEqual(compact('tiny-work', 2), ())
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.ledger.to_dict(), ledger)

            app.session.store.write_json('attempts/new/result.json', {'tool_name':'observe',
                'status':'ok', 'data':{'text':'Meaningful additional observation. '*500}})
            app.session.store.write_json('attempts/new/reply.json', {'tool_calls':[{
                'id':'new', 'function':{'name':'observe', 'arguments':'{}'}}]})
            app.state['turns'].append({'reply':'attempts/new/reply.json', 'request_count':1,
                'tools':[{'id':'new', 'result':'attempts/new/result.json'}]})
            send.return_value = self.response(
                '{"notes":[{"text":"New evidence supersedes the earlier result; original diagnostic remains unresolved",'
                '"observations":["o2"]}]}')
            compact('new-batch', 3)
            advanced = app._working_memory()
            self.assertEqual(advanced['compacted_records'],
                {'records/new/result.json'})
            self.assertEqual(advanced['through_turn'], 0)  # Failure remains in the original journal.
            self.assertEqual(advanced['attempted_through_turn'], 3)
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.ledger.to_dict()['entries'][:len(ledger['entries'])], ledger['entries'])
            self.assertTrue(all(path.read_bytes() == raw for path, raw in {**original, **retained}.items()))
            self.assertEqual(app._working_memory(), advanced)
            messages, selection = select(material_policy='retained', require_work_notes=True,
                fit_work_notes=True, work_notes=advanced['notes'], observations=rows,
                processed_turns=processed_journal_turns(rows, advanced['compacted_records']),
                turn_blocks=[[], block(1, 'Continue')], context_token_target=12000)
            self.assertIn(results[1]['error'], json.dumps(messages))
            self.assertTrue(any(row['source']['record'] == rows[0]['record']
                for row in selection['selected_records']))

    def test_rolling_journal_complete_handoffs_advance_real_replayed_coverage(self):
        results = [{'tool_name':'observe', 'status':'ok', 'data':{'value':3}},
            {'tool_name':'observe', 'status':'error', 'error':'Original diagnostic'}]
        app = self._journal_app(results)
        app.state['config']['native_supply_policy'] = 'receipts.v1'
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Value 3; diagnostic unresolved","observations":["o1","o2"]}]}')) as send:
            app._compact_work(context, self.client, 1)
            self.assertEqual(app._working_memory()['through_turn'], 1)
            app.session.store.write_json('attempts/fresh/result.json', {'tool_name':'observe',
                'status':'ok', 'data':{'text':'Fresh measurement and conditions. '*500}})
            app.session.store.write_json('attempts/fresh/reply.json', {'tool_calls':[{
                'id':'fresh', 'function':{'name':'observe', 'arguments':'{}'}}]})
            app.state['turns'].append({'reply':'attempts/fresh/reply.json', 'request_count':1,
                'tools':[{'id':'fresh', 'result':'attempts/fresh/result.json'}]})
            app.session.attempt_lineage().append(SimpleNamespace(
                attempt_id='advance', capability='native_turn', status='completed'))
            store = ArtifactStore(self.root/'attempts/advance')
            send.return_value = self.response('{"notes":[{"text":"New measurement; prior diagnostic still '
                'unresolved","observations":["p0","o1"]}]}')
            app._compact_work(SimpleNamespace(store=store, attempt=SimpleNamespace(attempt_id='advance')),
                self.client.with_budget(self.ledger, session_id='session', attempt_id='advance'), 2)
            actual = app._working_memory()
            self.assertEqual(actual['through_turn'], 2)
            self.assertEqual(actual['attempted_through_turn'], 2)
            self.assertEqual(actual['compacted_records'], {'records/reads/result-0.json',
                'records/reads/result-1.json', 'records/fresh/result.json'})
            self.assertEqual([row['turn'] for row in store.read_json('memory_input.json')['observations']], [1])
            self.assertEqual(send.call_count, 2)
            self.assertEqual(app._working_memory(), actual)

    def test_journal_uncited_failure_remains_selectable_and_reaches_next_checkpoint(self):
        results = [{'tool_name':'observe', 'status':'ok', 'data':{'value':3}},
            {'tool_name':'observe', 'status':'error', 'error':'Unresolved original diagnostic'}]
        app = self._journal_app(results)
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"The first observation returned 3","observations":["o1"]}]}')) as send:
            app._compact_work(context, self.client, 1)
            memory = app._working_memory()
            first, second = [f'records/reads/result-{i}.json' for i in range(2)]
            self.assertEqual(memory['compacted_records'], {first})
            self.assertEqual(memory['through_turn'], 0)
            rows = [observation(0, json.dumps(result), kind='operation_result', record=record)
                for result, record in zip(results, (first, second))]
            messages, selection = select(material_policy='retained', require_work_notes=True,
                fit_work_notes=True, work_notes=memory['notes'], observations=rows,
                processed_turns=processed_journal_turns(rows, memory['compacted_records']),
                turn_blocks=[[], block(1, 'Continue the work')], context_token_target=12000)
            self.assertIn(results[1]['error'], json.dumps(messages))
            self.assertLessEqual(selection['estimated_input_tokens'], 12000)
            before = self.ledger.to_dict()
            self.assertEqual(app._working_memory(), memory)
            self.assertEqual(self.ledger.to_dict(), before)
            next_store = ArtifactStore(self.root/'attempts'/'next')
            app.session.attempt_lineage().append(SimpleNamespace(
                attempt_id='next', capability='native_turn', status='completed'))
            next_context = SimpleNamespace(store=next_store, attempt=SimpleNamespace(attempt_id='next'))
            next_client = self.client.with_budget(self.ledger, session_id='session', attempt_id='next')
            send.return_value = self.response(
                '{"notes":[{"text":"First result retained; second operation still failed",'
                '"observations":["p0","o1"]}]}')
            app._compact_work(next_context, next_client, 1)
            self.assertEqual([row['record'] for row in next_store.read_json('memory_input.json')['observations']],
                [second])
            self.assertEqual(app._working_memory()['compacted_records'], {first, second})
            self.assertEqual(send.call_count, 2)
            # A subsequent rewrite can drop old citations. Reload must reopen
            # those originals, rather than accumulate every past admission.
            third = 'records/later/result.json'
            app.session.store.write_json('attempts/later/result.json', {
                'tool_name':'observe', 'status':'ok', 'data':{'value':4}})
            app.session.store.write_json('attempts/later/reply.json', {'tool_calls':[{
                'id':'later', 'function':{'name':'observe', 'arguments':'{}'}}]})
            app.state['turns'].append({'reply':'attempts/later/reply.json', 'request_count':1,
                'tools':[{'id':'later', 'result':'attempts/later/result.json'}]})
            app.session.attempt_lineage().extend([
                SimpleNamespace(attempt_id='later', capability='tool', status='completed'),
                SimpleNamespace(attempt_id='rewrite', capability='native_turn', status='completed')])
            rewrite = SimpleNamespace(store=ArtifactStore(self.root/'attempts'/'rewrite'),
                attempt=SimpleNamespace(attempt_id='rewrite'))
            send.return_value = self.response(
                '{"notes":[{"text":"Only the latest result was retained","observations":["o1"]}]}')
            app._compact_work(rewrite, self.client.with_budget(self.ledger,
                session_id='session', attempt_id='rewrite'), 2)
            memory = app._working_memory()
            self.assertEqual(memory['compacted_records'], {third})
            self.assertEqual(memory['through_turn'], 0)
            messages, _ = select(material_policy='retained', require_work_notes=True,
                fit_work_notes=True, work_notes=memory['notes'], observations=rows,
                processed_turns=processed_journal_turns(rows, memory['compacted_records']),
                turn_blocks=[[], block(1, 'Continue the work')], context_token_target=12000)
            self.assertIn(results[1]['error'], json.dumps(messages))
            self.assertEqual(send.call_count, 3)

    def test_selected_closed_work_keeps_authored_arguments_failures_and_historical_recovery_contract(self):
        result = {'tool_name':'write_file', 'status':'error', 'error':'Original write rejected'}
        app = self._journal_app([result])
        reply = app.session.store.read_json('attempts/reads/reply.json')
        arguments = {'path':'candidate.py', 'content':'Original authored payload'}
        reply['tool_calls'][0]['function']['arguments'] = json.dumps(arguments)
        app.session.store.write_json('attempts/reads/reply.json', reply)
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        response = self.response('{"notes":[{"text":"The write failed; inspect its original arguments","observations":["o1"]}]}')
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=response) as send:
            self.assertEqual(app._compact_work(context, self.client, 1, selected_turns=[]), ())
            send.assert_not_called()
            self.assertTrue(app._compact_work(context, self.client, 1, selected_turns=[0]))
            supplied, = self.store.read_json('memory_input.json')['observations']
            self.assertEqual(supplied['arguments'], arguments)
            self.assertEqual(json.loads(supplied['observation_text']), result)
            self.assertFalse(supplied['truncated'])
            before = self.ledger.to_dict()
            # Loading old owner presentations must not require today's format.
            app._coordination_result = Mock(side_effect=AssertionError('Historical input must not be reprojected'))
            memory = app._working_memory()
            self.assertTrue(memory['notes'])
            self.assertEqual(app._working_memory(), memory)
            self.assertEqual(self.ledger.to_dict(), before)
            self.assertEqual(send.call_count, 1)

    def test_journal_explicit_notes_replacement_resets_derived_frontier(self):
        app = self._journal_app([{'tool_name':'read_file', 'status':'ok', 'data':{'text':'x'*20000}}])
        app.state['config']['native_supply_policy'] = 'receipts.v1'
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Retained","observations":["o1"]}]}')):
            app._compact_work(context, self.client, 1)
        self.assertEqual(app._working_memory()['compacted_records'], {'records/reads/result-0.json'})
        # A replacement before another receipt in the same later batch must be seen.
        for notes in ([], self.bind([{'text':'Explicit replacement', 'records':[]}])):
            with self.subTest(notes=notes):
                app.session.store.write_json('attempts/replacement/plan.json', {
                    'tool_name':'update_work_plan', 'status':'ok', 'data':{'notes':notes}})
                app.session.store.write_json('attempts/replacement/read.json', {
                    'tool_name':'read_file', 'status':'ok', 'data':{'text':'later work'}})
                if len(app.state['turns']) == 1:
                    app.state['turns'].append({'reply':'attempts/replacement/reply.json', 'tools':[
                        {'id':'plan', 'result':'attempts/replacement/plan.json'},
                        {'id':'read', 'result':'attempts/replacement/read.json'}]})
                    app.session.attempt_lineage().append(SimpleNamespace(
                        attempt_id='replacement', capability='tool', status='completed'))
                self.assertEqual(app._working_memory(), {'notes':notes, 'through_turn':0,
                    'compacted_records':set(), 'attempted_records':{'records/reads/result-0.json'},
                    'attempted_through_turn':1})

    def test_journal_oversized_receipt_does_not_block_later_complete_observation(self):
        rows = [{'turn':0, 'record':f'records/{index}.json', 'result':{
            'tool_name':'read_file', 'data':{'text':'x'*size}}} for index, size in enumerate((50000, 20000))]
        before = deepcopy(rows)
        # Legacy selection remains turn-atomic, independent of the budget floor.
        with patch('simple_ar.integrations.llm._call_openai_sdk') as send:
            self.assertEqual(self.compact(rows=rows), (None, ()))
            send.assert_not_called()
        self.previous['compacted_records'] = set()
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Later complete work","observations":["o1"]}]}')):
            snapshot, refs = self.compact(rows=rows)
        view = self.store.read_json('memory_input.json')
        self.assertEqual([row['record'] for row in view['observations']], ['records/1.json'])
        self.assertEqual(json.loads(view['observations'][0]['observation_text']), rows[1]['result'])
        self.assertEqual(summary_coverage(previous=self.previous, snapshot=snapshot, view=view,
            operations_for_turn=lambda turn:{row['record']:{} for row in rows}), 0)
        self.assertEqual(refs, self.artifacts())
        self.assertEqual(rows, before)

    def test_journal_failed_or_empty_snapshot_supplies_no_frontier(self):
        app = self._journal_app([{'tool_name':'read_file', 'status':'ok', 'data':{'text':'x'*20000}}])
        self.previous['compacted_records'] = set()
        rows = [{'turn':0, 'request_count':1, 'record':'records/reads/result-0.json',
            'result':app.session.store.read_json('attempts/reads/result-0.json')}]
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response('{"notes":[]}')):
            self.assertIsNone(self.compact(rows=rows)[0])
        memory = app._working_memory()
        self.assertEqual(memory['compacted_records'], set())
        self.assertEqual(memory['through_turn'], 0)
        # A settled unusable summary cannot hide the successful original result.
        result = observation(0, json.dumps(rows[0]['result']), kind='operation_result', record=rows[0]['record'])
        messages, budget = select(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            work_notes=memory['notes'], summarized_through_turn=memory['through_turn'], observations=[result],
            turn_blocks=[[], block(1, 'Continue from the successful receipt')],
            context_token_target=12000, estimate_tokens=estimate_tokens)
        supplied, = json.loads(messages[1]['content'])['working_view']['observations']
        self.assertEqual(json.loads(supplied['observation']['text']), rows[0]['result'])
        self.assertEqual(supplied['version_status'], 'historical')
        self.assertFalse(budget['compaction_needed'])
        before = self.ledger.to_dict()
        # Historical empty snapshots validate but cannot acknowledge receipts either.
        failure = self.store.read_json('memory_failure.json')
        self.store.resolve('memory_failure.json').unlink()
        self.store.write_json('memory_snapshot.json', {'schema_version':'working_memory.v1',
            'through_turn':1, 'notes':[], 'input_sha256':failure['input_sha256']})
        self.assertEqual(app._working_memory(), {'notes':[], 'through_turn':0, 'compacted_records':set()})
        self.assertEqual(self.ledger.to_dict(), before)

    def test_journal_unknown_or_inflight_snapshot_never_acknowledges_or_releases_receipts(self):
        app = self._journal_app([{'tool_name':'read_file', 'status':'ok', 'data':{'text':'x'*20000}}])
        context = SimpleNamespace(store=self.store, attempt=SimpleNamespace(attempt_id='native-turn-test'))
        with patch('simple_ar.integrations.llm._call_openai_sdk', return_value=self.response(
                '{"notes":[{"text":"Retained","observations":["o1"]}]}')):
            app._compact_work(context, self.client, 1)
        self.ledger.reserve('unconfirmed', {'llm_requests':1, 'total_tokens':5000}, session_id='session',
            attempt_id='native-turn-test', logical_call_id='working-memory-native-turn-test', purpose='working-memory')
        for status in ('reserved', 'unknown'):
            with self.subTest(status=status):
                if status == 'unknown':
                    self.ledger.mark_unknown('unconfirmed', reason='interrupted', retain_reservation=True)
                before = self.ledger.to_dict()
                self.assertEqual(app._working_memory(), {'notes':[], 'through_turn':0, 'compacted_records':set()})
                self.assertEqual(self.ledger.to_dict(), before)

    def _retry_app(self, *, checkpoint_status='settled', main_unknown=True):
        app = ToolApplication.__new__(ToolApplication)
        app.state = {'pending':{'kind':'model','attempt':'native-turn-test'},'turns':[],
            'config':{'model_connection':{},'max_output_tokens':8192},'requests':['goal']}
        budget = SimpleNamespace(exhausted=lambda:False)
        app.session = SimpleNamespace(manifest=SimpleNamespace(status='failed',
                session_id='session',budget=budget),store=ArtifactStore(self.root),
            list_attempts=lambda:[],continue_with_revision=Mock(),allocate_attempt_id=Mock(return_value='retry'))
        app.ledger = self.ledger
        self.store.write_json('memory_input.json',{'original':'retained checkpoint input'})
        app.ledger.reserve('checkpoint',{'llm_requests':1,'total_tokens':5000},session_id='session',
            attempt_id='native-turn-test',logical_call_id='working-memory-native-turn-test',purpose='working-memory')
        if checkpoint_status == 'settled':
            app.ledger.settle('checkpoint',{'llm_requests':1,'total_tokens':140})
        elif checkpoint_status == 'unknown':
            app.ledger.mark_unknown('checkpoint',reason='interrupted',retain_reservation=True)
        if main_unknown:
            app.ledger.reserve('main',{'llm_requests':1,'total_tokens':9000},session_id='session',
                attempt_id='native-turn-test',logical_call_id='native-turn-test',purpose='native-turn')
            app.ledger.mark_unknown('main',reason='main transport interrupted',
                known_actual={'llm_requests':1},retain_reservation=True)
        app._failed_model_attempt = lambda pending:SimpleNamespace(attempt_id='native-turn-test')
        app._client = lambda attempt:self.client
        app._coordinator_purpose = lambda:'task'
        app._work_connection = lambda binding,purpose:{'connection':{'max_output_tokens':2048}}
        app._request_capacity = lambda:5
        app._messages = lambda **kwargs:[]
        app._decision_tokens = lambda client,messages:client.effective_output_limit()+100
        app._save = Mock()
        return app

    def test_retry_accepts_settled_checkpoint_and_uses_callers_main_output_cap(self):
        app = self._retry_app()
        before = [e.to_dict() for e in app.ledger.entries]
        app._retry_model()
        self.assertEqual(app.state['pending'],{'kind':'model','attempt':'retry','skip_memory_checkpoint':True})
        after = [e.to_dict() for e in app.ledger.entries]
        self.assertEqual(after,before)
        app._save.assert_called_once()

    def test_retry_of_checkpoint_only_length_failure_skips_checkpoint_replay(self):
        app = self._retry_app(main_unknown=False)
        app._retry_model()
        self.assertTrue(app.state['pending']['skip_memory_checkpoint'])

    def test_retry_rejects_unknown_checkpoint_even_with_retained_4096_reservation(self):
        app = self._retry_app(checkpoint_status='unknown')
        with self.assertRaisesRegex(ValueError,'Checkpoint'):
            app._retry_model()
        app.session.allocate_attempt_id.assert_not_called()
        app._save.assert_not_called()
