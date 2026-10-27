from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from simple_ar.core.working_context import select_working_context
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


class WorkingContextTests(unittest.TestCase):
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
            operations.append({'turn': i, 'request_count': 1, 'call_id': str(i),
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
        indexed = [entry['observation'] for entry in overview['entries']]
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
        supplied = [i for start, end in coverage['supplied_entry_ranges'] for i in range(start, end)]
        self.assertEqual(supplied, [entry['entry_index'] for entry in coverage['entries']])
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

    def compact(self, *, rows=None):
        return compact_work(store=self.store,client=self.client,requests=['Explain the measurements'],
            previous=self.previous,rows=rows if rows is not None else [{'turn':0,'record':'records/read/native_result.json',
                'result':{'tool_name':'read_file','status':'ok','data':{
                    'text':'Original recorded material with measurement scope. '*400}}}],
            bind_notes=self.bind,estimate_tokens=estimate_tokens,attempt_id='native-turn-test')

    def artifacts(self):
        return checkpoint_artifacts(store=self.store,ledger=self.ledger,attempt_id='native-turn-test',
            session_id='session',bind_notes=self.bind)

    @staticmethod
    def response(content, finish='stop'):
        return {'choices':[{'message':{'role':'assistant','content':content},'finish_reason':finish}],
            'usage':{'prompt_tokens':100,'completion_tokens':40,'total_tokens':140}}

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
