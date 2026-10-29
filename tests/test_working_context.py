from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from simple_ar.core.working_context import history_view, select_working_context
from simple_ar.app.work_memory import compact_work, checkpoint_artifacts, checkpoint_entries, summary_coverage
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
        messages, budget = select(turn_blocks=[block(0, row['text'])], observations=[row],
                                  version_resolver=lambda _: 'changed')
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
    def test_processed_prefix_is_navigation_not_a_demand_to_refill_the_window(self):
        text = 'Original historical source. '*300
        row = observation(0, text, role='historical_observation')
        notes = [{'text':'Earlier work remains a model judgment; consult its original.',
            'records':[row['record']], 'current':True}]
        arguments = dict(material_policy='retained', require_work_notes=True, fit_work_notes=True,
            turn_blocks=[block(0, text), block(1, 'Current operation')], observations=[row],
            work_notes=notes, processed_turns=[0], version_resolver=lambda _:row['sha256'],
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
        app.operations = SimpleNamespace(bind_notes=self.bind, project_notes=deepcopy)
        app._request_capacity = lambda:10
        app._delivery_feedback = lambda:None
        app._coordination_result = lambda result, record, **kwargs:result
        return app

    def test_journal_partial_turn_batches_derive_frontier_without_rebuying_or_claiming_coverage(self):
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
            app._compact_work(next_context, next_client, 1)
            second = next_store.read_json('memory_input.json')
            self.assertEqual([row['record'] for row in second['observations']], ['records/reads/result-1.json'])
            self.assertEqual(json.loads(second['observations'][0]['observation_text']), results[1])
            memory = app._working_memory()
            self.assertEqual(memory['through_turn'], 0)  # Separate partial batches prove no whole turn.
            self.assertEqual(memory['compacted_records'], {'records/reads/result-0.json', 'records/reads/result-1.json'})
            self.assertEqual(app._compact_work(next_context, next_client, 1), ())
            self.assertEqual(send.call_count, 2)

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
                self.assertEqual(app._working_memory(), {'notes':notes, 'through_turn':0, 'compacted_records':set()})

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
