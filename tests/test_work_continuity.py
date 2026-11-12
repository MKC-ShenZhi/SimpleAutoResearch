"""Server-only behavior checks for selected work continuity and input supply."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.app.reuse import ReuseAccess, select_reuse
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.working_context import material_window_views, select_working_context
from simple_ar.tools.delivery_versions import DeliveryVersions
from simple_ar.tools.materials import retained_working_inputs, _recorded_copy_source


def action(name, **arguments):
    return {'tool_name': name, 'arguments': arguments}


class SelectedWorkContinuityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.app = ToolApplication.create(self.base / 'old', 'Original goal', model=None, sources={})
        self.file = self.app.workspace.workspace / 'selected.txt'
        self.file.write_bytes(b'first\nsecond\nthird\n')
        (self.app.workspace.workspace / 'unselected.txt').write_text('UNSELECTED_PRIVATE_CONTENT')
        result = self.app.run(actions=[
            action('read_file', path='workspace/selected.txt', offset_bytes=6, max_bytes=7, full_digest=True),
            action('read_file', path='workspace/unselected.txt', full_digest=True),
            action('deliver_result', summary='Original result', artifacts=['workspace/selected.txt'])],
            progress=lambda _: None)
        self.assertEqual(result['status'], 'completed')

    def fresh(self):
        selection = select_reuse(self.app.session.store.root, ['artifacts'], version=1)
        return ToolApplication.create(self.base / 'fresh', 'Different current goal', model=None,
            sources={}, creation_declarations={'reuse_origin': selection.origin})

    def test_native_read_rows_keep_full_and_partial_offsets_and_needed_sources_after_archiving(self):
        from simple_ar.integrations.llm import LLMClient, LLMSettings
        from tests.report_review_fixtures import goal_work_decision
        client = LLMClient(LLMSettings(api_key='offline-fixture', model='fixture', api_mode='chat',
            retry_attempts=1, max_output_tokens=1024, context_input_tokens=100000))
        services = SimpleNamespace(llm_client=client)
        app = ToolApplication.create(self.base / 'native', 'Use the original source without changing it',
            model='fixture', sources={'original': self.file}, services=services,
            max_turns=8, max_tokens=600000, tool_contracts='full')
        app.state['config']['work_lifecycle'] = 'integrated.v2'
        calls = []
        def provider(_mode, request, **_):
            number = len(calls)
            calls.append(request)
            operations = ([action('read_file', path='original', offset_bytes=6, max_bytes=7, full_digest=True),
                action('read_file', path='original', full_digest=True)] if number == 0 else
                [action('list_directory', path='workspace')])
            names = {row['function']['name'] for row in request.get('tools', [])}
            if 'select_work' in names:
                operations = goal_work_decision(operations, app.state['requests'],
                    policy=app.state['config'].get('work_selection_policy'))
            self.assertTrue(all(item['tool_name'] in names for item in operations))
            return {'choices': [{'message': {'role': 'assistant', 'content': None,
                'reasoning_content': 'Use recorded source observations.', 'tool_calls': [
                    {'id': f'native-{number}-{i}', 'type': 'function', 'function': {
                        'name': item['tool_name'], 'arguments': json.dumps(item['arguments'])}}
                    for i, item in enumerate(operations)]}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 40, 'total_tokens': 140}}
        with patch('simple_ar.integrations.llm._call_openai_sdk', side_effect=provider):
            app.run(progress=lambda _: None,
                should_stop=lambda: len(app.state['turns']) >= 3 and not app.state['pending'])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(turn.get('origin') != 'caller' for turn in app.state['turns']))
        first = app.state['turns'][0]
        records = ['records/' + row['result'].removeprefix('attempts/') for row in first['tools']]
        memory = {'notes': [{'text': 'Necessary original scope', 'records': records}], 'through_turn': 1,
            'compacted_records': set(records), 'archived_records': set(records)}
        state, ledger = deepcopy(app.state), deepcopy(app.ledger.to_dict())
        with patch('simple_ar.core.working_context.select_working_context', wraps=select_working_context) as select:
            messages = app._messages(client=client, schemas=app.schemas, working_memory=memory)
        rows = [row for row in select.call_args.kwargs['observations'] if row.get('tool') == 'read_file']
        self.assertEqual([(row['kind'], row['offset_bytes'], row['next_offset_bytes']) for row in rows],
            [('source_text', 6, 13), ('source_text', 0, len(self.file.read_bytes()))])
        originals = [row['observation'] for row in json.loads(messages[1]['content'])['working_view']['observations']
            if row['observation'].get('kind') == 'source_text']
        self.assertEqual(sum(len(row['text'].encode()) for row in originals), len(self.file.read_bytes()))
        self.assertEqual(app.state, state)
        self.assertEqual(app.ledger.to_dict(), ledger)
        app.state['config']['work_lifecycle'] = 'integrated.v1'
        with patch('simple_ar.core.working_context.select_working_context', wraps=select_working_context) as old:
            app._messages(client=client, schemas=app.schemas, working_memory=memory)
        self.assertTrue(all('kind' not in row for row in old.call_args.kwargs['observations']
            if row.get('tool') == 'read_file'))

    def test_selected_partial_original_observation_reaches_common_working_input(self):
        old = {name: (self.app.session.store.root / name).read_bytes()
            for name in ('actions.json', 'budget_ledger.json', 'session_manifest.json')}
        fresh = self.fresh()
        view = fresh._retained_working_inputs()
        self.assertEqual(len(view['observations']), 1)
        observation = view['observations'][0]
        self.assertEqual((observation['offset_bytes'], observation['next_offset_bytes']), (6, 13))
        self.assertEqual(observation['text'], 'second\n')
        self.assertIn('records/', observation['source']['record'])
        self.assertNotIn('UNSELECTED_PRIVATE_CONTENT', json.dumps(fresh.state))
        self.assertEqual(fresh.state['requests'], ['Different current goal'])
        self.assertFalse(fresh.ledger.entries)
        self.assertFalse((fresh.session.store.root / 'delivery.json').exists())
        self.assertEqual({name: (self.app.session.store.root / name).read_bytes() for name in old}, old)
        provider = fresh._material_observations(['retained_delivery'])
        self.assertEqual(provider[0]['data']['text'], 'second\n')
        ToolApplication.load(fresh.session.store.root)

    def test_current_registered_result_selection_uses_its_exact_retained_version(self):
        selection = select_reuse(self.app.session.store.root, ['artifacts'])
        self.assertEqual(selection.version, 1)
        self.assertFalse(selection.materials)

    def test_changed_original_source_uses_retained_bytes_but_changed_input_is_rejected(self):
        self.file.write_text('later work')
        fresh = self.fresh()
        self.assertEqual(fresh._retained_working_inputs()['observations'][0]['text'], 'second\n')
        copied = Path(fresh.state['config']['sources']['retained_delivery']) / 'workspace/selected.txt'
        copied.write_text('tampered input')
        with self.assertRaisesRegex(ValueError, 'Retained source snapshot changed'):
            ToolApplication.load(fresh.session.store.root)

    def test_changed_original_receipt_does_not_become_source_by_matching_file_hash(self):
        turn = self.app.state['turns'][0]
        path = self.app.session.store.root / turn['tools'][0]['result']
        value = read_json(path)
        value['data']['text'] = 'forged!'
        write_json(path, value)
        self.assertFalse(self.fresh()._retained_working_inputs()['observations'])

    def test_ui_grant_checks_handoff_receipts_before_creation(self):
        first = self.app.state['turns'][0]['tools'][0]['result']
        root = self.app.session.store.root
        def authorize(path):
            if path == root / first:
                raise ValueError('Receipt outside UI grant')
            return path
        with self.assertRaisesRegex(ValueError, 'outside UI grant'):
            select_reuse(root, ['artifacts'], version=1, access=ReuseAccess(authorize=authorize))
        self.assertFalse((self.base / 'fresh').exists())

    def test_scoped_reuse_without_optional_historical_checkpoint_keeps_selected_inputs(self):
        root = self.app.session.store.root
        before = {path: path.read_bytes() for path in root.rglob('*.json')}
        admitted = []
        def authorize(path):
            admitted.append(path)
            return path.resolve(strict=True)
        selection = select_reuse(root, ['artifacts'], version=1,
            access=ReuseAccess(authorize=authorize))
        self.assertEqual(selection.version, 1)
        self.assertTrue(any(path.name == 'memory_snapshot.json' for path in admitted))
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        fresh = self.fresh()
        self.assertEqual(fresh._retained_working_inputs()['observations'][0]['text'], 'second\n')

    def test_optional_checkpoint_path_denial_remains_a_grant_failure(self):
        root = self.app.session.store.root
        for error in (ValueError, PermissionError):
            with self.subTest(error=error):
                def authorize(path):
                    if path.name == 'memory_snapshot.json':
                        raise error('Optional checkpoint outside UI grant')
                    return path
                with self.assertRaisesRegex(error, 'outside UI grant'):
                    select_reuse(root, ['artifacts'], version=1, access=ReuseAccess(authorize=authorize))
        self.assertFalse((self.base / 'fresh').exists())

    def test_interrupted_creation_input_is_not_rebuilt_from_the_old_task_on_load(self):
        fresh = self.fresh()
        copied = Path(fresh.state['config']['sources']['retained_delivery']) / 'workspace/selected.txt'
        copied.unlink()
        with self.assertRaisesRegex(ValueError, 'Retained source snapshot changed'):
            ToolApplication.load(fresh.session.store.root)
        self.assertFalse(copied.exists())

    def test_retained_byte_link_is_rejected_before_creating_a_new_task(self):
        owner = DeliveryVersions(self.app.session.store.root)
        row = owner.select(1)['files']['workspace/selected.txt']
        blob = self.app.session.store.root / row['blob']
        content = blob.read_bytes()
        outside = self.base / 'outside.txt'
        outside.write_bytes(content)
        blob.unlink()
        try:
            try:
                blob.symlink_to(outside)
            except OSError as error:
                self.skipTest(f'Symbolic links unavailable: {error}')
            with self.assertRaises(ValueError):
                self.fresh()
            self.assertFalse((self.base / 'fresh/session_manifest.json').exists())
        finally:
            if blob.is_symlink():
                blob.unlink()
            if not blob.exists():
                blob.write_bytes(content)

    def test_import_projection_uses_only_explicit_subjects_and_cited_understanding(self):
        fresh = self.fresh()
        config = deepcopy(fresh.state['config'])
        handoff = config['source_snapshots']['retained_delivery']['working_context']
        record = handoff['observations'][0]['source']['record']
        source = {'record': 'memory_response.json', 'sha256': '0' * 64}
        handoff['notes'] = [
            {'text': 'Revisable interpretation', 'records': [record], 'source': source},
            {'text': 'Unrelated private note', 'records': ['records/unrelated.json'], 'source': source}]
        view = retained_working_inputs(config, resolve=fresh.workspace.resolve_material_root)
        self.assertEqual([note['text'] for note in view['notes']], ['Revisable interpretation'])
        self.assertEqual(view['notes'][0]['records'], [view['observations'][0]['record']])
        self.assertFalse(retained_working_inputs(config, resolve=fresh.workspace.resolve_material_root,
            targets=['workspace'])['observations'])


class WorkingSupplyContinuityTests(unittest.TestCase):
    def test_feedback_working_set_keeps_relevant_originals_without_filling_old_history(self):
        def read(turn, record, text):
            return {'turn': turn, 'record': record, 'kind': 'source_text',
                'role': 'historical_observation', 'tool': 'read_file', 'path': 'workspace/' + record,
                'sha256': 'a' * 64, 'offset_bytes': 0, 'next_offset_bytes': len(text), 'text': text}
        rows = [read(0, 'old-unused', 'UNRELATED HISTORICAL BODY'),
                read(1, 'old-needed', 'NOTE-SUPPORTED ORIGINAL'),
                read(2, 'old-selected', 'EXPLICIT ORIGINAL'),
                read(3, 'current-read', 'CURRENT ORIGINAL')]
        blocks = [[{'role': 'assistant', 'content': 'UNRELATED OLD WORK'}], [], [],
            [{'role': 'assistant', 'content': 'CURRENT WORK'}],
            [{'role': 'assistant', 'content': 'LATEST WORK'}]]
        arguments = dict(user_requests=['Original task', 'Revise the work'], system_text='Work', base_context={},
            turn_blocks=blocks, block_request_counts=[1, 1, 1, 2, 2], observations=rows,
            selected_records=['old-selected'], native_observations=[], schema_tokens=0,
            context_token_target=100000, estimate_tokens=len, material_policy='working',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[], archived_turns=[],
            work_notes=[{'text': 'Keep the established result', 'records': ['old-needed']}],
            version_resolver=lambda _: 'a' * 64)
        previous, _ = select_working_context(**arguments, working_set_policy='recent.v4')
        focused, selection = select_working_context(**arguments, working_set_policy='recent.v5')
        self.assertIn('UNRELATED HISTORICAL BODY', json.dumps(previous))
        self.assertNotIn('UNRELATED HISTORICAL BODY', json.dumps(focused))
        self.assertNotIn('UNRELATED OLD WORK', json.dumps(focused))
        for body in ('NOTE-SUPPORTED ORIGINAL', 'EXPLICIT ORIGINAL', 'CURRENT ORIGINAL', 'CURRENT WORK'):
            self.assertIn(body, json.dumps(focused))
        self.assertIn('old-unused', {row['source']['record'] for row in selection['omitted_records']})
        self.assertLess(selection['estimated_input_tokens'], 100000)

    def test_compact_feedback_keeps_unresolved_checks_and_original_receipt(self):
        from simple_ar.core.working_context import feedback_observations
        from simple_ar.tools.inspection import InspectionToolGateway
        record = 'records/review/result.json'
        supported = {'claim': 'The calculation ran', 'outcome': 'supported', 'scope': 'recorded output',
            'method': 'LONG SUPPORT METHOD', 'reasoning': 'LONG SUPPORT REASONING', 'evidence': ['e1']}
        unresolved = {'claim': 'The method generalizes', 'outcome': 'unresolved',
            'method': 'NOT YET CHECKED', 'reasoning': 'Need additional data', 'evidence': []}
        result = {'data': {'source_observations': {'record': record}}, 'metadata': {'delivery_opinion': {
            'findings': [{'severity': 'required', 'message': 'Check the scope'}],
            'investigations': [supported, unresolved], 'limitations': ['One dataset']}}}
        feedback = {'review_observations': [{'record': record, 'subjects': [], 'result': result}]}
        original = deepcopy(feedback)
        compact_result = InspectionToolGateway.compact_feedback(result)
        compact, = feedback_observations({'review_observations': [{
            'record': record, 'subjects': [], 'result': compact_result}]})
        opinion = json.loads(compact['text'])['metadata']['delivery_opinion']
        self.assertEqual(feedback, original)
        self.assertEqual(compact['record'], record)
        self.assertEqual(opinion['investigations'][1], unresolved)
        self.assertEqual(opinion['findings'], result['metadata']['delivery_opinion']['findings'])
        self.assertNotIn('LONG SUPPORT REASONING', compact['text'])
        self.assertIn('original receipt', compact['text'])

    def test_producer_findings_can_share_the_feedback_view_without_inspector_wrapper(self):
        from simple_ar.core.working_context import feedback_observations
        finding = {'record': 'records/producer/native_result.json', 'summary': 'The code produced a table.',
            'issues': ['The next comparison is still needed.']}
        row, = feedback_observations({'professional_findings': [finding]})
        self.assertEqual(row['record'], finding['record'])
        self.assertEqual(json.loads(row['text']), finding)
        self.assertEqual(row['subjects'], [])

    def test_recorded_read_windows_share_an_authored_body_without_losing_its_receipt(self):
        text = 'Current report, one body.\n'
        split = 8
        common = {'path': 'workspace/report.md', 'sha256': 'a' * 64}
        rows = [
            {**common, 'turn': 0, 'record': 'records/write.json', 'kind': 'authored_text',
             'role': 'write_context', 'offset_bytes': 0, 'next_offset_bytes': len(text), 'text': text},
            {**common, 'turn': 1, 'record': 'records/read.json', 'kind': 'source_text',
             'role': 'historical_observation', 'tool': 'read_file', 'offset_bytes': 0,
             'next_offset_bytes': split, 'text': text[:split]},
            {**common, 'turn': 1, 'record': 'records/read.json', 'kind': 'source_text',
             'role': 'historical_observation', 'tool': 'read_file', 'offset_bytes': split,
             'next_offset_bytes': len(text), 'text': text[split:]},
        ]
        arguments = dict(user_requests=['Continue'], system_text='Work', base_context={},
            turn_blocks=[[], [], [{'role': 'assistant', 'content': 'Latest decision'}]],
            observations=rows, native_observations=[], schema_tokens=0, context_token_target=100000,
            estimate_tokens=len, material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[], archived_turns=[], working_set_policy='recent.v4',
            version_resolver=lambda _: 'a' * 64)
        messages, selection = select_working_context(**arguments)
        supplied = [item['observation'] for item in json.loads(messages[1]['content'])['working_view']['observations']]
        self.assertEqual(''.join(row['text'] for row in sorted(supplied, key=lambda row: row['offset_bytes'])), text)
        self.assertEqual({row['kind'] for row in supplied}, {'source_text'})
        omitted = next(row for row in selection['omitted_records'] if row['source']['record'] == 'records/write.json')
        self.assertEqual(omitted['status'], 'duplicate')
        explicit, _ = select_working_context(**arguments, selected_records=['records/write.json'])
        supplied = json.loads(explicit[1]['content'])['working_view']['observations']
        self.assertTrue(any(row['observation']['record'] == 'records/write.json' for row in supplied))

    def test_identical_authored_snapshots_share_text_without_replacing_a_read(self):
        text = 'Current result.\n'
        base = {'path': 'workspace/report.md', 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': len(text), 'text': text}
        rows = [
            {**base, 'turn': 0, 'record': 'records/write-1.json', 'kind': 'authored_text', 'role': 'write_context'},
            {**base, 'turn': 1, 'record': 'records/write-2.json', 'kind': 'authored_text', 'role': 'write_context'},
            {**base, 'turn': 1, 'record': 'records/read.json', 'kind': 'source_text', 'role': 'historical_observation'}]
        messages, _ = select_working_context(
            user_requests=['Continue'], system_text='Work', base_context={},
            turn_blocks=[[], [], [{'role': 'assistant', 'content': 'Latest decision'}]],
            observations=rows, selected_records=[row['record'] for row in rows], native_observations=[],
            schema_tokens=0, context_token_target=100000, estimate_tokens=len, material_policy='working',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[], archived_turns=[], working_set_policy='recent.v4',
            version_resolver=lambda _: 'a' * 64)
        context = json.loads(messages[1]['content'])
        supplied = [item['observation'] for item in context['working_view']['observations']]
        self.assertEqual([row['kind'] for row in supplied].count('authored_text'), 1)
        self.assertEqual([row['kind'] for row in supplied].count('source_text'), 1)
        self.assertEqual({row['record'] for row in context['material_supply']['references']},
                         {row['record'] for row in rows})

    def test_related_opinion_does_not_pin_every_window_in_its_receipt(self):
        from simple_ar.core.working_context import feedback_observations

        record = 'records/inspection/result.json'
        feedback = feedback_observations({'review_observations': [{
            'record': record, 'subjects': [{'path': 'workspace/report.md', 'current_matches_review': True}],
            'result': {'data': {'review': 'Reuse the recorded comparison.'}}}]})
        old_text = 'UNRELATED INSPECTION ' * 800
        rows = feedback + [{'turn': 0, 'record': record, 'kind': 'source_text',
            'role': 'historical_observation', 'path': 'workspace/other.txt', 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': len(old_text), 'text': old_text}]
        arguments = dict(user_requests=['Continue the work'], system_text='Respect the task', base_context={},
            turn_blocks=[[{'role': 'assistant', 'content': 'Old inspection'}],
                [{'role': 'assistant', 'content': 'RECENT_EXECUTION_RESULT'}],
                [{'role': 'assistant', 'content': 'Latest decision'}]],
            observations=rows, native_observations=[], schema_tokens=0, context_token_target=10000,
            estimate_tokens=len, material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[0], archived_turns=[0],
            work_notes=[{'text': 'The earlier review is retained.'}], version_resolver=lambda _: 'a' * 64,
            working_set_policy='recent.v4')
        messages, _ = select_working_context(**arguments)
        context = json.loads(messages[1]['content'])
        self.assertIn('RECENT_EXECUTION_RESULT', json.dumps(messages))
        self.assertIn('Reuse the recorded comparison.', json.dumps(messages))
        self.assertNotIn('UNRELATED INSPECTION', json.dumps(messages))
        references = context['material_supply']['references']
        self.assertEqual([(row['record'], row['source']['kind']) for row in references],
                         [(record, 'operation_result')])

        explicit, _ = select_working_context(**{**arguments, 'context_token_target': 100000}, selected_records=[record])
        self.assertIn('UNRELATED INSPECTION', json.dumps(explicit))

    def test_related_feedback_precedes_optional_older_work(self):
        from simple_ar.core.working_context import feedback_observations

        feedback = feedback_observations({'review_observations': [{
            'record': f'records/review-{i}.json', 'subjects': [],
            'result': {'review': f'RELATED_REVIEW_{i} ' * 200}} for i in range(4)]})
        report = 'CURRENT_REPORT ' * 220
        observations = feedback + [{'turn': 0, 'record': 'records/report.json', 'kind': 'source_text',
            'role': 'historical_observation', 'tool': 'inspect_artifacts', 'path': 'workspace/report.md',
            'sha256': 'a' * 64, 'offset_bytes': 0, 'next_offset_bytes': len(report), 'text': report}]
        messages, _ = select_working_context(
            user_requests=['Continue the work'], system_text='Work', base_context={},
            turn_blocks=[[{'role': 'assistant', 'content': 'OLDER_WORK ' * 800}],
                [{'role': 'assistant', 'content': 'RECENT_EXECUTION_RESULT'}],
                [{'role': 'assistant', 'content': 'Latest decision'}]],
            observations=observations, native_observations=[], schema_tokens=0, context_token_target=12000,
            estimate_tokens=len, material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[], archived_turns=[], working_set_policy='recent.v4',
            current_artifacts=['workspace/report.md'], version_resolver=lambda _: 'a' * 64)
        supplied = json.dumps(messages)
        self.assertIn('RECENT_EXECUTION_RESULT', supplied)
        self.assertIn('CURRENT_REPORT', supplied)
        self.assertIn('RELATED_REVIEW', supplied)
        self.assertNotIn('OLDER_WORK', supplied)

    def test_current_observations_remain_eligible_after_multiple_read_turns(self):
        def read(turn, name, text, digest='a' * 64):
            return {'turn': turn, 'record': f'records/{turn}.json', 'kind': 'source_text',
                'role': 'historical_observation', 'tool': 'read_file', 'path': f'workspace/{name}',
                'sha256': digest, 'offset_bytes': 0, 'next_offset_bytes': len(text), 'text': text}
        rows = [read(0, 'old', 'PREVIOUS REQUEST'), read(1, 'report', 'REPORT'),
                read(2, 'script', 'SCRIPT'), read(3, 'metrics', 'METRICS'),
                read(4, 'stale', 'STALE', 'b' * 64)]
        common = dict(user_requests=['Previous task', 'Continue the study'], system_text='Work', base_context={},
            turn_blocks=[[], [], [], [], [], [{'role': 'assistant', 'content': 'Latest decision'}]],
            block_request_counts=[1, 2, 2, 2, 2, 2], observations=rows, native_observations=[],
            schema_tokens=0, context_token_target=100000, estimate_tokens=len,
            material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[0, 1, 2, 3, 4], archived_turns=[0, 1, 2, 3, 4],
            work_notes=[{'text': 'Prior work retained in the journal'}], version_resolver=lambda _: 'a' * 64)
        _, previous = select_working_context(**common, working_set_policy='recent.v3')
        self.assertNotIn('records/1.json', {x['source']['record'] for x in previous['selected_records']})
        for policy, inspection_observations in ((policy, inspected) for policy in ('recent.v4', 'recent.v5')
                                                for inspected in (False, True)):
            with self.subTest(policy=policy, inspection_observations=inspection_observations):
                observations = deepcopy(rows)
                if inspection_observations:
                    # Independent inspection supplies actual read windows with
                    # their source rounds, without the workspace tool label.
                    for row in observations:
                        row.pop('tool')
                _, selected = select_working_context(**{**common, 'observations': observations},
                    working_set_policy=policy)
                self.assertEqual({x['source']['record'] for x in selected['selected_records']},
                    {'records/1.json', 'records/2.json', 'records/3.json'})

    def test_active_read_windows_survive_archiving_without_pinning_unrelated_files(self):
        def read(turn, name, text, digest='a' * 64):
            return {'turn': turn, 'record': f'records/{turn}.json', 'kind': 'source_text',
                'role': 'historical_observation', 'tool': 'read_file', 'path': f'workspace/{name}',
                'sha256': digest, 'offset_bytes': 0, 'next_offset_bytes': len(text), 'text': text}
        rows = [read(0, 'old', 'UNRELATED'), read(1, 'draft', 'EARLIER TEXT'),
                read(2, 'draft', 'STALE TEXT', 'b' * 64), read(3, 'script', 'CURRENT CODE')]
        common = dict(user_requests=['Revise the work'], system_text='Work', base_context={},
            turn_blocks=[[], [], [], [], [{'role': 'assistant', 'content': 'Latest decision'}]],
            observations=rows, native_observations=[], schema_tokens=0, context_token_target=100000,
            estimate_tokens=len, material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[0, 1, 2, 3], archived_turns=[0, 1, 2, 3],
            work_notes=[{'text': 'Prior work retained in the journal'}], version_resolver=lambda _: 'a' * 64)
        _, old = select_working_context(**common, working_set_policy='recent.v2')
        _, new = select_working_context(**common, working_set_policy='recent.v3')
        self.assertEqual(old['selected_records'], [])
        self.assertEqual({x['source']['record'] for x in new['selected_records']},
                         {'records/1.json', 'records/3.json'})

    def test_recent_work_precedes_unneeded_archived_originals_but_note_sources_remain(self):
        def row(record, text):
            return {'turn': 0, 'record': record, 'kind': 'source_text', 'role': 'historical_observation',
                'path': 'workspace/' + record, 'sha256': 'a' * 64, 'offset_bytes': 0,
                'next_offset_bytes': len(text), 'text': text}
        unused, needed = row('unused', 'U' * 20000), row('needed', 'NEEDED ORIGINAL')
        blocks = [[], [{'role': 'assistant', 'content': 'Recent complete reasoning ' * 60}],
            [{'role': 'assistant', 'content': 'Current decision'}]]
        arguments = dict(user_requests=['Continue'], system_text='Allowed work', base_context={},
            turn_blocks=blocks, observations=[unused, needed], native_observations=[], schema_tokens=0,
            context_token_target=28000, estimate_tokens=len, material_policy='working',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[0], archived_turns=[0], work_notes=[{'text': 'Use the original scope',
                'records': ['needed']}], version_resolver=lambda _: 'a' * 64)
        before = deepcopy(arguments)
        messages, selected = select_working_context(**arguments, working_set_policy='recent.v2')
        self.assertIn(1, selected['selected_turns'])
        shown = json.loads(messages[1]['content'])['working_view']['observations']
        self.assertEqual([item['observation']['text'] for item in shown], ['NEEDED ORIGINAL'])
        self.assertEqual(arguments, before)
        _, explicit = select_working_context(**arguments, working_set_policy='recent.v2',
            selected_records=['unused'])
        self.assertTrue(any(item['source']['record'] == 'unused' for item in explicit['selected_records']))

    def test_overlapping_unicode_windows_show_only_actual_unsupplied_ranges(self):
        text = 'αβγδε'
        full = {'record': 'read/full', 'path': 'workspace/a', 'sha256': 'a' * 64,
            'kind': 'source_text', 'role': 'historical_observation', 'offset_bytes': 0,
            'next_offset_bytes': len(text.encode()), 'text': text}
        middle = {**full, 'record': 'read/middle', 'offset_bytes': 2, 'next_offset_bytes': 8, 'text': 'βγδ'}
        views = material_window_views(full, [middle])
        self.assertEqual([row['text'] for row in views], ['α', 'ε'])
        self.assertEqual([(row['supplied_offset_bytes'], row['supplied_next_offset_bytes'])
            for row in views], [(0, 2), (8, 10)])
        self.assertEqual([row['offset_bytes'] for row in views], [0, 0])
        self.assertEqual(material_window_views(full, [middle, *views]), [])
        self.assertEqual(material_window_views(full, [{**full, 'path': 'workspace/copy'}]), [full])
        self.assertEqual(material_window_views(full, [{**full, 'sha256': 'b' * 64}]), [full])
        self.assertEqual(material_window_views(full, [{**middle, 'text': 'XYZ123'}]), [full])

    def test_native_partial_window_and_explicit_full_window_share_body_without_claiming_a_new_read(self):
        partial = {'turn': 1, 'record': 'read/partial', 'field': 'data.text', 'kind': 'source_text',
            'role': 'historical_observation', 'path': 'workspace/a', 'sha256': 'a' * 64,
            'offset_bytes': 2, 'next_offset_bytes': 8, 'text': 'cdefgh'}
        full = {**partial, 'turn': 0, 'record': 'read/full', 'offset_bytes': 0,
            'next_offset_bytes': 10, 'text': 'abcdefghij'}
        message = {'role': 'assistant', 'content': 'Latest original cdefgh'}
        native = {key: value for key, value in partial.items() if key != 'text'}
        messages, selection = select_working_context(user_requests=['Continue'], system_text='Allowed work',
            base_context={}, turn_blocks=[[], [message]], observations=[full, partial],
            native_observations=[native], schema_tokens=0, context_token_target=100000,
            estimate_tokens=len, material_policy='working', require_work_notes=True,
            fit_work_notes=True, sparse_handoffs=True, processed_turns=[], archived_turns=[],
            selected_records=['read/full'], version_resolver=lambda _: 'a' * 64,
            working_set_policy='recent.v2')
        supplied = json.loads(messages[1]['content'])['working_view']['observations']
        self.assertEqual([row['observation']['text'] for row in supplied], ['ab', 'ij'])
        slices = [row['source']['record_text_slice'] for row in selection['selected_records']
            if row['source']['record'] == 'read/full']
        self.assertEqual([(row['character_start'], row['character_end']) for row in slices], [(0, 2), (8, 10)])
        self.assertIn(message, messages)

    def test_archiving_a_receipt_does_not_remove_its_current_original_material(self):
        row = {'turn': 0, 'record': 'records/read.json', 'kind': 'source_text',
            'role': 'historical_observation', 'path': 'workspace/a.txt', 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': 4, 'text': 'DATA'}
        common = dict(user_requests=['Continue'], system_text='Allowed work', base_context={},
            turn_blocks=[[], []], observations=[row], native_observations=[], schema_tokens=0,
            context_token_target=100000, estimate_tokens=len, material_policy='working',
            require_work_notes=True, fit_work_notes=True, sparse_handoffs=True,
            processed_turns=[0], archived_turns=[0],
            work_notes=[{'text': 'A revisable interpretation', 'records': [row['record']]}],
            version_resolver=lambda _: 'a' * 64)
        _, old = select_working_context(**common)
        _, current = select_working_context(**common, preserve_material_observations=True)
        self.assertFalse(old['selected_records'])
        self.assertEqual(current['selected_records'][0]['source']['record'], row['record'])

    def test_repeated_same_path_window_shares_body_but_equal_hash_elsewhere_does_not(self):
        rows = [{'record': f'records/{i}.json', 'kind': 'source_text',
            'role': 'historical_observation', 'path': path, 'sha256': 'a' * 64,
            'offset_bytes': 0, 'next_offset_bytes': 4, 'text': 'DATA'}
            for i, path in enumerate(['workspace/a', 'workspace/a', 'workspace/b'])]
        _, selected = select_working_context(user_requests=['Continue'], system_text='Allowed work',
            base_context={}, turn_blocks=[], observations=rows, native_observations=[],
            schema_tokens=0, context_token_target=100000, estimate_tokens=len,
            material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[], archived_turns=[],
            version_resolver=lambda _: 'a' * 64, preserve_material_observations=True)
        self.assertEqual(len(selected['selected_records']), 2)
        self.assertEqual([row['status'] for row in selected['omitted_records']], ['duplicate'])

    def test_copy_reuse_consumes_only_existing_recorded_copy_edges(self):
        from types import SimpleNamespace
        source, target = Path('/source'), Path('/target')
        graph = {'root': 0, 'nodes': [
            {'id': 0, 'path': target, 'sha256': 'a', 'state': 'recorded',
                'edges': [{'kind': 'input', 'node': 1}]},
            {'id': 1, 'path': source, 'sha256': 'a', 'state': 'ordinary', 'edges': []}]}
        workspace = SimpleNamespace(artifact_sources=lambda *_args, **_kw: {'/target': {'lineage': graph}},
            resolve=Path)
        self.assertFalse(_recorded_copy_source(workspace, '/target', 'a', '/source'))
        graph['nodes'][0]['edges'][0]['kind'] = 'copy'
        self.assertTrue(_recorded_copy_source(workspace, '/target', 'a', '/source'))
        graph['nodes'][0]['state'] = 'unbound'
        self.assertFalse(_recorded_copy_source(workspace, '/target', 'a', '/source'))


class InspectionWorkingContinuityTests(unittest.TestCase):
    def test_feedback_usage_is_shared_within_one_view_and_refreshed_afterward(self):
        from simple_ar.core.budget import BudgetEntry
        from simple_ar.tools.inspection import InspectionToolGateway

        with tempfile.TemporaryDirectory() as temporary:
            entries = [BudgetEntry('first', {}, {'total_tokens': 10}, 'settled', attempt_id='action-a'),
                       BudgetEntry('second', {}, {}, 'unknown', attempt_id='action-b')]
            ledger = SimpleNamespace(entries=entries)
            owner = InspectionToolGateway(Path(temporary), SimpleNamespace(ledger=ledger), lambda _: None)
            for identity in ('review-a', 'review-b'):
                owner.store.write_json(owner._path(identity, 'input'),
                    {'execution_protocol': 'shared.v1', 'source_invocations': []})
            for attempt, identity in (('action-a', 'review-a'), ('action-b', 'review-b')):
                owner.store.write_json(f'attempts/{attempt}/action.json',
                    {'attempt': attempt, 'origin': {'scope': identity}})
            expected = {identity: owner._usage(identity) for identity in ('review-a', 'review-b')}
            with patch.object(owner.store, 'exists', wraps=owner.store.exists) as exists:
                with owner.presentation_scope():
                    self.assertEqual(owner._usage('review-a'), expected['review-a'])
                    self.assertEqual(owner._usage('review-b'), expected['review-b'])
                    self.assertEqual(owner._usage('review-a'), expected['review-a'])
                headers = [call.args[0] for call in exists.call_args_list
                           if str(call.args[0]).endswith('/action.json')]
                self.assertEqual(len(headers), len(set(headers)))
            entries[1].status = 'settled'
            with owner.presentation_scope():
                self.assertEqual(owner._usage('review-b')[0]['status'], 'settled')

    def test_independent_inspection_freezes_work_policy_and_resume_preserves_it(self):
        from simple_ar.tools.inspection import InspectionRequest, InspectionToolGateway
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            app = ToolApplication.create(root / 'task', 'Independent work', model=None, sources={})
            file = app.workspace.workspace / 'original.txt'
            file.write_text('An original observation', encoding='utf-8')

            def gateway(policy):
                return InspectionToolGateway(app.session.store.root, app.workspace,
                    lambda _: self.fail('Preparing or resuming inspection must not call a model'),
                    protocol='subjects.v3', execution_protocol='shared.v1',
                    native_supply_policy='turns.v1', context_checkpoint_policy='economic.v3',
                    work_method='goal.v2', work_lifecycle=policy,
                    user_requests=lambda: ['Independent work'], operation_specs=lambda: [],
                    operation_call=lambda *_: self.fail('Pending inspection must not execute a tool'))

            arguments = {'goal': 'Check the claim', 'targets': ['workspace/original.txt']}
            request = InspectionRequest.model_validate(arguments)
            owner = gateway('integrated.v1')
            prepared = owner.call('inspect_artifacts', arguments, identity='inspection')
            self.assertEqual(prepared.data, {'inspection_pending': 'inspection'})
            path = owner._path('inspection', 'input')
            saved = owner.store.read_json(path)
            self.assertEqual(saved['work_lifecycle'], 'integrated.v1')
            before = owner.store.resolve(path).read_bytes()
            resumed = gateway(None)
            self.assertEqual(resumed.call('inspect_artifacts', arguments, identity='inspection').data,
                {'inspection_pending': 'inspection'})
            self.assertEqual(resumed.store.resolve(path).read_bytes(), before)

            record = 'inspection-original-read'
            raw = file.read_bytes()
            observation = {'turn': 0, 'record': record, 'path': str(file),
                'sha256': hashlib.sha256(raw).hexdigest(), 'offset_bytes': 0,
                'next_offset_bytes': len(raw), 'size_bytes': len(raw), 'text': raw.decode(),
                'kind': 'source_text', 'role': 'historical_observation'}
            memory = {'notes': [{'text': 'A fallible interpretation', 'records': [record]}],
                'through_turn': 1, 'compacted_records': {record}, 'archived_records': {record}}
            journal = [{'turn': 0, 'record': record, 'result': {'tool_name': 'read_file'}}]
            history = [{}, {}, {'role': 'assistant', 'content': 'Earlier work', 'reasoning_content': ''},
                {'role': 'assistant', 'content': 'Continue independent work', 'reasoning_content': ''}]
            client = SimpleNamespace(_budget_ledger=None,
                _settings=SimpleNamespace(context_input_tokens=128000, thinking_mode=''))
            with patch.object(resumed, '_conversation_material', return_value=(memory, [observation], journal)):
                _, supply = resumed._working_messages(saved, request, history, [], client)
            self.assertTrue(any(row['source']['record'] == record for row in supply['selected_records']))

            # A historical started inspection remains historical even under a
            # freshly configured integrated gateway. Its immutable input wins.
            historical = deepcopy(saved)
            historical.pop('work_lifecycle')
            owner.store.write_json(path, historical)
            current = gateway('integrated.v1')
            frozen = owner.store.resolve(path).read_bytes()
            current.call('inspect_artifacts', arguments, identity='inspection')
            self.assertEqual(owner.store.resolve(path).read_bytes(), frozen)
            with patch.object(current, '_conversation_material', return_value=(memory, [observation], journal)):
                _, old_supply = current._working_messages(historical, request, history, [], client)
            self.assertFalse(any(row['source']['record'] == record for row in old_supply['selected_records']))


if __name__ == '__main__':
    unittest.main()
