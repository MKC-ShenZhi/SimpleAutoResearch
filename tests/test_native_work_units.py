"""Native work keeps actual units, service boundaries and exact recovery."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.app.professional_conversation import work_unit_messages
from simple_ar.app.tool_application import ExecutionRejection, ToolApplication
from tests import test_professional_work_loop as fixture


class NativeWorkProjectionTests(unittest.TestCase):
    def test_unmarked_multiple_outputs_leave_source_choice_to_the_inspector(self):
        from simple_ar.tools.inspection import InspectionToolGateway

        context = {'claim': {}, 'scope_policy': 'applicable.v2',
            'artifacts': {'tool.py': 'a' * 64, 'output.pdf': 'b' * 64}}
        saved = {'delivery_context': context, 'source_priming_policy': 'priority.v2'}
        select = InspectionToolGateway._delivery_source_paths
        self.assertEqual(select(saved), [])
        self.assertEqual(select({**saved, 'source_priming_policy': 'priority.v3'}), [])
        self.assertEqual(select({**saved, 'source_priming_policy': 'priority.v1'}), ['tool.py'])
        one = {**context, 'artifacts': {'tool.py': 'a' * 64}}
        self.assertEqual(select({**saved, 'delivery_context': one}), ['tool.py'])
        named = {**context, 'claim': {'report': {'manuscript': 'tool.py'}}}
        self.assertEqual(select({**saved, 'delivery_context': named}), ['tool.py'])

    def test_retained_inspections_do_not_repeat_the_same_source_window(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        owner = InspectionToolGateway.__new__(InspectionToolGateway)
        owner.workspace = SimpleNamespace(artifact_hashes=lambda paths: {path: 'version' for path in paths})
        previous = {'identity': 'earlier', 'arguments': {}, 'delivery_context': {}}
        owner.store = SimpleNamespace(read_json=lambda _: previous)
        owner._path = lambda identity, leaf: identity + '/' + leaf
        owner._digest = lambda _: 'retained'
        window = {'offset_bytes': 0, 'review_next_offset_bytes': 20, 'source_reference': 'first'}
        duplicate = {**window, 'source_reference': 'another valid receipt'}
        later = {'offset_bytes': 20, 'review_next_offset_bytes': 40, 'source_reference': 'later'}
        source = {'subject': 'evidence-1', 'path': 'values.txt', 'sha256': 'version',
            'readable': True, 'windows': [window]}
        parent = {'identity': 'earlier', 'targets': [], 'evidence': [{**source, 'windows': [duplicate, later]}]}
        owner._retained_inputs = lambda _: [parent]
        saved = {'inspection_material_policy': 'operations.v3', 'work_lifecycle': 'integrated.v3',
            'arguments': {}, 'evidence': [deepcopy(source)]}
        remaining = [100]
        owner._inherit_investigation(saved, {'identity': 'earlier', 'binding': {}, 'arguments': {}}, remaining)
        self.assertEqual(remaining[0], 80)
        self.assertEqual(saved['evidence'][0]['windows'], [window, later])

    def test_retained_inspection_exposes_unselected_sources_without_replaying_their_bodies(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        owner = InspectionToolGateway.__new__(InspectionToolGateway)
        owner.workspace = SimpleNamespace(artifact_hashes=lambda paths: {path: 'version' for path in paths})
        previous = {'identity': 'earlier', 'arguments': {}, 'delivery_context': {}}
        owner.store = SimpleNamespace(read_json=lambda _: previous)
        owner._path = lambda identity, leaf: identity + '/' + leaf
        owner._digest = lambda _: 'retained'
        window = {'offset_bytes': 0, 'review_next_offset_bytes': 20, 'text': 'original evidence'}
        parent = {'identity': 'earlier', 'targets': [], 'evidence': [
            {'subject': 'evidence-1', 'path': name, 'sha256': 'version',
             'readable': True, 'windows': [deepcopy(window)]}
            for name in ('selected.txt', 'supporting.txt')]}
        owner._retained_inputs = lambda _: [parent]
        saved = {'inspection_material_policy': 'operations.v3', 'work_lifecycle': 'integrated.v3',
            'source_priming_policy': 'priority.v3', 'arguments': {}, 'targets': [], 'evidence': [],
            'delivery_context': {'claim': {}, 'artifacts': {'selected.txt': 'version'}}}
        remaining = [100]
        owner._inherit_investigation(saved, {'identity': 'earlier', 'binding': {}, 'arguments': {}}, remaining)
        selected, supporting = saved['evidence']
        self.assertEqual(selected['windows'], [window])
        self.assertEqual(supporting['windows'], [])
        self.assertEqual(supporting['path'], 'supporting.txt')
        self.assertEqual(supporting['retained_from']['inspection'], 'earlier')
        self.assertEqual(parent['evidence'][1]['windows'], [window])
        self.assertEqual(remaining[0], 80)

    def test_prior_executions_are_selected_for_current_artifacts_and_open_findings(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        saved = {'targets': [{'path': 'target.txt'}],
            'evidence': [{'path': 'paper.pdf'}, {'path': 'other.txt', 'retained_from': {'inspection': 'old'}}],
            'delivery_context': {'artifacts': {'output.txt': 'version'}},
            'prior_required': [{'pending_subjects': ['open.txt']}]}
        facts = {name: {'source_record': name, 'dependencies': {path: 'version'}}
            for name, path in (('target', 'target.txt'), ('output', 'output.txt'),
                               ('open', 'open.txt'), ('paper', 'paper.pdf'), ('other', 'other.txt'))}
        self.assertEqual(InspectionToolGateway._relevant_execution_sources(saved, facts),
                         {'target', 'output', 'open', 'paper'})

    def test_units_keep_statements_arguments_results_and_feedback_order_without_double_encoding(self):
        original = [{'role':'system', 'content':'Original permissions'},
            {'role':'user', 'content':json.dumps({'work_plan':{'old':'fallible'}})},
            {'role':'assistant', 'content':'The manuscript is note.md; retain the calculation.',
             'tool_calls':[{'id':'a', 'function':{'name':'write_file',
                 'arguments':json.dumps({'path':'note.md', 'content':'A line\nAnother line'})}}]},
            {'role':'tool', 'tool_call_id':'a', 'content':json.dumps({'status':'ok',
                'record':'records/write/native_result.json', 'data':{'sha256':'original'}})},
            {'role':'user', 'content':'Shorten only the interpretation.'},
            {'role':'assistant', 'content':'Reuse the unchanged data; read the current paragraph.',
             'tool_calls':[{'id':'b', 'function':{'name':'read_file',
                 'arguments':'{"path":"note.md"}'}}]},
            {'role':'tool', 'tool_call_id':'b', 'content':'{"status":"ok","data":{"text":"A line"}}'}]
        frozen = deepcopy(original)
        result = work_unit_messages(original)
        self.assertEqual(original, frozen)
        self.assertNotIn('work_plan', json.loads(result[1]['content']))
        first = json.loads(result[2]['content'])['recorded_work_unit']
        self.assertIn('note.md', first['content'])
        self.assertEqual(first['operations'][0]['arguments']['content'], 'A line\nAnother line')
        self.assertEqual(first['observations'][0]['result']['data']['sha256'], 'original')
        self.assertEqual(result[3], original[4])
        second = json.loads(result[4]['content'])['recorded_work_unit']
        self.assertIn('unchanged data', second['content'])
        # Missing provider reasoning does not require another wrapper: these
        # actual closed statements/actions already become historical data.
        self.assertFalse(any(row['role'] == 'assistant' for row in result[2:]))
        json_request = {'role':'user', 'content':'{"closed_journal_observations":[] }'}
        self.assertEqual(work_unit_messages([*original[:2], json_request])[-1], json_request)

    def test_unclosed_unit_refused_and_invalid_arguments_preserved_as_evidence(self):
        prefix = [{'role':'system', 'content':'Scope'}, {'role':'user', 'content':'{}'}]
        choice = {'role':'assistant', 'content':'A failed attempt', 'tool_calls':[
            {'id':'broken', 'function':{'name':'read_file', 'arguments':'{"path":'}}]}
        with self.assertRaisesRegex(ValueError, 'Unclosed'):
            work_unit_messages([*prefix, choice])
        result = work_unit_messages([*prefix, choice, {'role':'tool',
            'tool_call_id':'broken', 'content':'{"status":"blocked"}'}])
        unit = json.loads(result[2]['content'])['recorded_work_unit']
        self.assertEqual(unit['operations'][0]['arguments'], '{"path":')
        self.assertEqual(unit['observations'][0]['result']['status'], 'blocked')


class NativeWorkQueueTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        c.app = ToolApplication.create(c.root, 'Write a bounded note and preserve the measurements.',
            model='fixture', sources={'measurements':c.inputs}, services=c.services,
            allow_execution=True, max_turns=24, max_tokens=900000, tool_contracts='full')
        c._reload()

    def drain(self, **kwargs):
        c = self.case
        return c._run(should_stop=lambda: not c.decisions and c.app.state['pending'] is None
            and not any(turn['remaining'] for turn in c.app.state['turns']), **kwargs)

    def test_rejected_execution_survives_reload_without_starting_a_process(self):
        c = self.case
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; Path('should-not-exist').write_text('ran')"]))
        self.drain()
        self.assertEqual(c.app.state['pending']['call']['function']['name'], 'run_process')
        reason = 'The proposed output is outside the authorized work area.'
        with patch.object(c.app, '_attempt', side_effect=OSError('Interrupted after decision')):
            self.drain(approve=lambda _call, _token: ExecutionRejection(reason))
        self.assertEqual(c.app.state['pending']['execution_rejection'], reason)
        c._reload()
        self.drain()
        result = c._last('run_process')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn(reason, result['error'])
        self.assertFalse(result['data']['executed'])
        self.assertFalse((c.root/'workspace/should-not-exist').exists())
        self.assertFalse(c._process_receipts())

    def test_invalid_tool_json_reports_the_parse_error_without_execution(self):
        c = self.case
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; Path('unexpected').write_text('ran')"]))
        def malformed(*args, **kwargs):
            response = c._provider(*args, **kwargs)
            response['choices'][0]['message']['tool_calls'][0]['function']['arguments'] += '}'
            return response
        with patch.object(c.send, 'side_effect', malformed):
            self.drain()
        result = c.app.session.store.read_json(c.app.state['turns'][-1]['tools'][-1]['result'])
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('Extra data', result['error'])
        self.assertIn('column', result['error'])
        self.assertFalse((c.root / 'workspace/unexpected').exists())
        self.assertFalse(c._process_receipts())

    def test_task_execution_grant_only_reuses_matching_inputs(self):
        c = self.case
        c.decisions.append(fixture._action('write_file', path='check.py', content='print("first")\n'))
        self.drain()
        c.decisions.append(fixture._action('compile_python', path='check.py'))
        self.drain()
        self.assertEqual(c._last('compile_python')['status'], 'ok')
        action = fixture._action('run_process', argv=[sys.executable, 'check.py'], inputs=['check.py'])
        c.decisions.append(action)
        self.drain()
        token = c.app.approval_token(c.app.state['pending']['call'])
        self.drain(approved_execution=token, remember_execution=True)
        self.assertEqual(c._last('run_process')['status'], 'ok')
        c._reload()
        c.decisions.append(action)
        self.drain()
        self.assertEqual(c._last('run_process')['status'], 'ok')
        self.assertIsNone(c.app.state['pending'])
        c.decisions.append(fixture._action('edit_file', path='check.py',
            old_text='print("first")', new_text='print("second")'))
        self.drain()
        c.decisions.append(action)
        self.drain()
        self.assertEqual(c.app.state['pending']['call']['function']['name'], 'run_process')
        self.assertIn('explicit approval', c.app.session.manifest.status_reason)

    @unittest.skipUnless(shutil.which('pandoc'), 'Publication requires Pandoc')
    def test_legacy_publication_approval_after_remembered_process(self):
        c = self.case
        c.decisions.append([
            fixture._action('write_file', path='check.py', content='print("ready")\n'),
            fixture._action('write_file', path='note.md', content='# Note\n\nOriginal observations.\n'),
        ])
        self.drain()
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, 'check.py'], inputs=['check.py']))
        self.drain()
        token = c.app.approval_token(c.app.state['pending']['call'])
        self.drain(approved_execution=token, remember_execution=True)
        self.assertEqual(c._last('run_process')['status'], 'ok')
        self.assertTrue(c.app.state['execution_grants'])
        self.assertIsNone(c.app.gateway.session_owner('export_report'))

        export = fixture._action('export_report', markdown='note.md', compile_pdf=False)
        c.decisions.append(export)
        self.drain()
        call = c.app.state['pending']['call']
        self.assertIn('explicit approval', c.app.session.manifest.status_reason)
        token = c.app.approval_token(call)
        self.assertEqual(c.app.execution_approval(call)['token'], token)
        self.drain(approved_execution=token, remember_execution=True)
        self.assertEqual(c._ok('export_report')['status'], 'ok')

        c._reload()
        c.decisions.append(export)
        self.drain()
        self.assertEqual(len(c._observations('export_report')), 2)
        self.assertIsNone(c.app.state['pending'])
        c.decisions.append(fixture._action('edit_file', path='note.md',
            old_text='Original observations.', new_text='Revised observations.'))
        self.drain()
        c.decisions.append(export)
        self.drain()
        self.assertIn('explicit approval', c.app.session.manifest.status_reason)
        self.assertNotEqual(c.app.approval_token(c.app.state['pending']['call']), token)

    def test_repeated_read_keeps_authorized_work_available_for_saved_sessions(self):
        c = self.case
        c.app.state['config']['tool_contracts'] = 'on_demand'
        # Older sessions retain this recorded policy, without restricting future choices.
        c.app.state['config']['action_offer_policy'] = 'observed_edit.v1'
        c.app._save()
        c._reload()
        for action in [fixture._action('write_file', path='note.md', content='First line.\nSecond line.\n'),
                       fixture._action('read_file', path='note.md', start_line=1, max_lines=1),
                       [fixture._action('read_file', path='note.md', start_line=1, max_lines=1),
                        fixture._action('read_file', path='note.md', start_line=2, max_lines=1)]]:
            c.decisions.append(action)
            self.drain()
        c._reload()
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; assert 'Second line.' in Path('note.md').read_text()"],
            inputs=['note.md']))
        self.drain()
        decision = c.root / c.app.state['turns'][-1]['reply']
        offer = json.loads(decision.with_name('decision_budget.json').read_text())['input_projection']
        self.assertNotIn('observed_edit', offer)
        self.assertIn('edit_file', offer['tool_names'])
        self.assertIn('read_file', offer['tool_names'])
        self.assertIn('run_process', offer['tool_names'])
        self.assertEqual(c.app.state['pending']['call']['function']['name'], 'run_process')
        token = c.app.approval_token(c.app.state['pending']['call'])
        self.drain(approved_execution=token)
        self.assertEqual(c._last('run_process')['status'], 'ok')

    def test_saved_edit_and_check_handoff_preserves_unknown_usage(self):
        c = self.case
        c.app.state['config']['tool_contracts'] = 'on_demand'
        c.app.state['config']['candidate_handoff_policy'] = 'after_production.v2'
        c.app._save()
        c._reload()
        c.decisions.append(fixture._action('write_file', path='note.md', content='Before\nOriginal note.\nAfter\n'))
        self.drain()
        c.decisions.append(fixture._action('edit_file', path='note.md', replacements=[
            {'start_anchor':'Before\n', 'end_anchor':'After\n', 'new_text':'Revised note.\n'}]))
        self.drain()
        self.assertEqual((c.root/'workspace/note.md').read_text(), 'Before\nRevised note.\nAfter\n')
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "assert 'Revised note.' in open('note.md').read()"], inputs=['note.md']))
        self.drain()
        token = c.app.approval_token(c.app.state['pending']['call'])
        attempt = (c.root / c.app.state['turns'][-2]['reply']).parent.name
        c.app.ledger.reserve('prior-provider-interruption', {'llm_requests': 1, 'total_tokens': 1000},
            session_id=c.app.session.manifest.session_id, attempt_id=attempt, logical_call_id=attempt)
        c.app.ledger.mark_unknown('prior-provider-interruption',
            reason='Provider disconnected before returning usage', known_actual={'llm_requests': 1},
            retain_reservation=True)
        c.decisions.append(fixture._action('read_file', path='note.md'))
        self.drain(approved_execution=token)
        c._reload()
        handoff = c.app.state['turns'][-2]
        self.assertEqual(handoff['origin'], 'professional')
        reply = json.loads((c.root / handoff['reply']).read_text())
        self.assertEqual(reply['source_result'], c.app.state['turns'][-3]['tools'][-1]['result'])
        self.assertEqual(reply['tool_calls'][0]['function']['name'], 'review_result')
        self.assertEqual(c.app.ledger.unknown_dimensions(), ('total_tokens',))
        self.assertTrue(c.inspection_requests)
        self.assertFalse((c.root/'delivery.json').exists())

    def test_real_work_approval_recovery_then_separate_review_and_same_version_delivery(self):
        c = self.case
        self.assertIsNone(c.app.operations.selection)
        self.assertNotIn('select_work', {row['function']['name'] for row in c.app.schemas})
        self.assertNotIn('update_work_plan', {row['function']['name'] for row in c.app.schemas})
        self.assertNotIn('update_work_plan', c.app._specialist_catalog())
        program = "from pathlib import Path;p=Path('runs.txt');p.write_text((p.read_text() if p.exists() else '')+'run\\n')"
        c.decisions.append([fixture._action('write_file', path='note.md', content='Three observations.\n'),
            fixture._action('run_process', argv=[sys.executable,'-c',program], outputs=['runs.txt'])])
        self.drain()
        token = c.app.approval_token(c.app.state['pending']['call'])
        c._reload()
        self.drain(approved_execution=token)
        self.assertEqual((c.root/'workspace/runs.txt').read_text(), 'run\n')
        self.assertFalse(c.inspection_requests)
        self.assertFalse((c.root/'delivery.json').exists())
        self.assertFalse(list((c.root/'attempts').glob('*/work_choice.json')))
        with patch('simple_ar.app.professional_conversation.continuation_blocks',
                side_effect=AssertionError('New work does not reinterpret user JSON as legacy wrappers')):
            messages = c.app._messages(client=c.app._client())
        self.assertTrue(any('recorded_work_unit' in row.get('content', '') for row in messages[2:]))
        before = deepcopy(c.app.ledger.to_dict())
        claim = {'summary':'A bounded note', 'artifacts':['note.md']}
        c.decisions.append(fixture._action('review_result', review_scope='delivery', **claim))
        self.drain()
        checked = len(c.inspection_requests)
        self.assertGreater(checked, 0)
        proof = c._last('review_result', reviewed=True)['metadata']['delivery_review']['identity']
        inspection = c.app.session.store.read_json(f'tools/inspection/{proof}/input.json')
        self.assertEqual(inspection['source_priming_policy'], 'priority.v3')
        c._reload()
        c.decisions.append(fixture._action('deliver_result', **claim))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(len(c.inspection_requests), checked)
        self.assertEqual(c._last('deliver_result', reviewed=True)['metadata']['delivery_review']['identity'], proof)
        self.assertEqual((c.root/'workspace/runs.txt').read_text(), 'run\n')
        self.assertEqual(c.app.ledger.to_dict()['entries'][:len(before['entries'])], before['entries'])
        from simple_ar.tools.delivery_versions import DeliveryVersions
        self.assertEqual(len(DeliveryVersions(c.root).select(1)['files']), 1)

    def test_candidate_review_with_four_calls_can_still_reach_delivery(self):
        c = self.case
        c.root = c.directory / 'bounded-review'
        c.app = ToolApplication.create(c.root, 'Explain the supplied observations.',
            model='fixture', sources={'measurements': c.inputs}, services=c.services,
            max_turns=4, max_tokens=300000, tool_contracts='on_demand')
        c._write('note.md', 'The sample mean is 2.0 across three supplied observations.\n')
        claim = {'summary': 'A bounded note', 'artifacts': ['note.md'],
            'report': {'status': 'canonical', 'manuscript': 'note.md', 'formats': ['markdown']}}
        c.decisions.append(fixture._action('review_result', review_scope='delivery', **claim))
        self.drain()
        self.assertEqual(c._last('review_result')['status'], 'ok')
        self.assertEqual(c.app.ledger.remaining('llm_requests'), 2)
        reviewed = c._last('review_result', reviewed=True)['metadata']['delivery_review']['identity']
        c.decisions.append(fixture._action('deliver_result', **claim))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(c._last('deliver_result', reviewed=True)['metadata']['delivery_review']['identity'], reviewed)

    def test_candidate_review_is_offered_with_one_measured_followup_remaining(self):
        c = self.case
        c.root = c.directory / 'review-with-followup'
        c.app = ToolApplication.create(c.root, 'Inspect the supplied note and retain any gaps.',
            model='fixture', sources={'measurements': c.inputs}, services=c.services,
            max_turns=3, max_tokens=300000, tool_contracts='on_demand')
        c._write('note.md', 'The supplied observations are bounded.\n')

        def choose():
            request = c.send.call_args.kwargs.get('request') or c.send.call_args.args[1]
            self.assertIn('review_result', {row['function']['name'] for row in request['tools']})
            return fixture._action('review_result', review_scope='candidate',
                summary='Inspect the note', artifacts=['note.md'])

        c.decisions.append(choose)
        self.drain()
        self.assertEqual(c._last('review_result')['status'], 'ok')
        self.assertGreaterEqual(c.app.ledger.remaining('llm_requests'), 1)

    def test_current_report_gets_read_when_retained_material_uses_its_allowance(self):
        c = self.case
        body = 'The current report contains the revised observation.\n'
        c._write('report.md', body)
        claim = {'summary': 'Current report', 'artifacts': ['report.md'],
            'report': {'status': 'candidate', 'manuscript': 'report.md', 'formats': ['markdown']}}

        def exhaust_reusable_space(saved, remaining):
            remaining[0] = 0  # Simulate a long retained history, not a missing current report.

        before = len(c.inspection_requests)
        with patch.object(c.app.operations.inspection, '_reuse_observations', side_effect=exhaust_reusable_space):
            c._submit(fixture._action('deliver_result', **claim))
        reviewed = c._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        identity = reviewed['identity']
        saved = c.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        report = next(row for row in saved['evidence'] if row.get('path', '').endswith('/report.md'))
        self.assertEqual(''.join(w['review_text'] for w in report['windows']), body)
        self.assertIn(body.strip(), json.dumps(c.inspection_requests[before]['messages'], ensure_ascii=False))

    def test_current_inspection_target_gets_a_window_before_retained_history(self):
        c = self.case
        body = 'Observed source material.\n' * 100 + 'CURRENT_TARGET_END\n'
        c._write('target.md', body)
        before = len(c.inspection_requests)

        def exhaust_reusable_space(saved, remaining):
            remaining[0] = 0

        with patch.object(c.app.operations.inspection, '_reuse_observations', side_effect=exhaust_reusable_space):
            c._inspect(['target.md'], [])
        supplied = json.dumps(c.inspection_requests[before]['messages'], ensure_ascii=False)
        self.assertIn('CURRENT_TARGET_END', supplied)

    def test_lifecycle_guidance_uses_the_actual_budget_limited_offer(self):
        c = self.case
        c.root = c.directory / 'bounded-offer'
        c.app = ToolApplication.create(c.root, 'Explain the supplied observations.',
            model='fixture', sources={'measurements': c.inputs}, services=c.services,
            max_turns=2, max_tokens=300000, tool_contracts='on_demand')
        c._write('note.md', 'A candidate note.\n')

        def choose():
            request = c.send.call_args.kwargs.get('request') or c.send.call_args.args[1]
            offered = {row['function']['name'] for row in request['tools']}
            context = json.loads(request['messages'][1]['content'])
            self.assertIn('review_result', offered)
            self.assertIn('deliver_result', offered)
            self.assertIn('deliver_result', context['delivery_conditions']['next_tools'])
            self.assertTrue(set(context['delivery_conditions']['next_tools']) <= offered)
            return fixture._action('deliver_result', summary='Candidate note', artifacts=['note.md'],
                unresolved=['Independent review remains unfinished.'])

        c.decisions.append(choose)
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'partial')

    def test_a_new_write_after_review_is_not_treated_as_the_reviewed_version(self):
        c = self.case
        c.root = c.directory / 'write-after-review'
        c.app = ToolApplication.create(c.root, 'Explain the supplied observations.',
            model='fixture', sources={'measurements': c.inputs}, services=c.services,
            max_turns=5, max_tokens=300000, tool_contracts='on_demand')
        written = c._write('note.md', 'The sample mean is 2.0 across three supplied observations.\n')
        claim = {'summary': 'A bounded note', 'artifacts': ['note.md']}
        c.decisions.append(fixture._action('review_result', **claim))
        self.drain()
        c.decisions.append(fixture._action('write_file', path='note.md',
            content='The sample mean is 2.0 for these observations only.\n',
            expected_sha256=written['data']['sha256']))
        self.drain()
        self.assertEqual(c._last('write_file')['status'], 'ok')

        def choose():
            request = c.send.call_args.kwargs.get('request') or c.send.call_args.args[1]
            offered = {row['function']['name']: row['function']['parameters'] for row in request['tools']}
            self.assertIn('delivery', offered['write_file']['properties'])
            self.assertNotIn('delivery', offered['write_file'].get('required', []))
            self.assertIn('deliver_result', offered)
            return fixture._action('deliver_result', **claim,
                unresolved=['The revised note still needs independent review.'])

        c.decisions.append(choose)
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'partial')

    def test_reviewed_candidate_can_be_delivered_without_recopying_or_rechecking(self):
        c = self.case
        c._write('note.md', 'The supplied observations support this bounded note.\n')
        c.decisions.append(fixture._action('review_result', summary='A bounded note',
            artifacts=['note.md'], review_scope='delivery'))
        self.drain()
        reviewed = c._last('review_result')
        identity = reviewed['metadata']['delivery_review']['identity']
        count = len(c.inspection_requests)
        c._reload()
        c.decisions.append(fixture._action('deliver_result', reviewed_candidate=identity))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(len(c.inspection_requests), count)
        self.assertEqual(c.app.session.store.read_json('delivery.json')['summary'], 'A bounded note')
        c._submit(fixture._action('read_file', path='note.md'),
            feedback='Clarify the same note and retain the earlier work.')
        prior = c.app.operations.delivery.retained_prior_delivery()
        self.assertEqual(prior['artifacts'], c.app.session.store.read_json('delivery.json')['artifacts'])
        self.assertTrue(c.app.workspace.resolve(prior['record']).is_file())
        self.assertEqual(c.app._work_context(['note.md'])['retained_prior_delivery'], prior)

    def test_new_model_review_defaults_to_candidate_scope_without_claiming_delivery(self):
        c = self.case
        c._write('note.md', 'A useful intermediate explanation.\n')
        c.decisions.append(fixture._action('review_result', summary='Check the current explanation',
            artifacts=['note.md']))
        self.drain()
        result = c._last('review_result', reviewed=True)
        self.assertEqual(result['metadata']['delivery_review']['binding']['purpose'], 'candidate')
        supplied = json.loads(c.inspection_requests[-1]['messages'][1]['content'])
        self.assertIn('not a complete delivery', supplied['proposed_delivery']['scope'])
        self.assertNotEqual(c.app.session.manifest.status, 'completed')
        self.assertFalse((c.root / 'delivery.json').exists())
        identity = result['metadata']['delivery_review']['identity']
        c.decisions.append(fixture._action('deliver_result', reviewed_candidate=identity))
        self.drain()
        self.assertNotEqual(c.app.session.manifest.status, 'completed')
        self.assertFalse((c.root / 'delivery.json').exists())

    def test_workspace_write_can_deliver_its_actual_partial_result(self):
        c = self.case
        c.decisions.append(fixture._action('write_file', path='note.md', content='Available work.\n',
            delivery={'summary': 'Available note', 'artifacts': ['note.md'],
                      'unresolved': ['The requested analysis remains unfinished.']}))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'partial', c.app.result_snapshot())
        self.assertEqual((c.root / 'workspace/note.md').read_text(), 'Available work.\n')
        self.assertEqual(c.app.session.store.read_json('delivery.json')['unresolved'],
            ['The requested analysis remains unfinished.'])

    def test_final_decision_can_run_authorized_work_and_deliver_its_partial_result(self):
        c = self.case
        c.root = c.directory / 'final-process'
        c.app = ToolApplication.create(c.root, 'Check the note and disclose unfinished review.',
            model='fixture', sources={}, services=c.services, allow_execution=True,
            max_turns=1, max_tokens=200000, max_attempts=20, tool_contracts='full')
        c._write('note.md', 'Available work.\n')
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; Path('checked.txt').write_text(Path('note.md').read_text())"],
            inputs=['note.md'], outputs=['checked.txt'], delivery={
                'summary': 'Checked copy', 'artifacts': ['note.md', 'checked.txt'],
                'unresolved': ['Independent review remains unfinished.']}))
        self.drain(approve=lambda *_: True)
        self.assertEqual(c.app.session.manifest.status, 'partial', c.app.result_snapshot())
        self.assertEqual((c.root / 'workspace/checked.txt').read_text(), 'Available work.\n')
        turn = next(row for row in c.app.state['turns'] if row.get('origin') != 'caller')
        decision = c.app.session.store.read_json(str(Path(turn['reply']).with_name('decision_budget.json')))
        self.assertEqual(decision['closing_action_contract'], 'single_action.v2')
        self.assertIn('run_process', decision['input_projection']['tool_names'])
        self.assertEqual(c.app.session.store.read_json('delivery.json')['unresolved'],
            ['Independent review remains unfinished.'])
        self.assertFalse(c.inspection_requests)

    def test_last_decision_retains_each_offered_read_without_claiming_delivery(self):
        c = self.case
        c.root = c.directory / 'final-reads'
        c.app = ToolApplication.create(c.root, 'Inspect the two existing notes.',
            model='fixture', sources={}, services=c.services, max_turns=1,
            max_tokens=200000, max_attempts=20, tool_contracts='full')
        c._write('one.txt', 'First observation.\n')
        c._write('two.txt', 'Second observation.\n')
        c.decisions.append([fixture._action('read_file', path='one.txt'),
                            fixture._action('read_file', path='two.txt')])
        self.drain()
        turn = next(row for row in c.app.state['turns'] if row.get('origin') != 'caller')
        self.assertEqual(len(turn['tools']), 2)
        results = [c.app.session.store.read_json(row['result']) for row in turn['tools']]
        self.assertEqual([row['status'] for row in results], ['ok', 'ok'])
        self.assertFalse(c.app.session.store.exists('delivery.json'))

    def test_saved_native_contract_keeps_publication_as_a_service(self):
        c = self.case
        c.app.state['config']['work_selection_policy'] = 'native.v4'
        c.app._save()
        c._reload()
        schemas = {row['function']['name']: row['function']['parameters'] for row in c.app.schemas}
        self.assertNotIn('delivery', schemas['export_report']['properties'])

    @unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1',
        'Real publication runs in the server integration phase')
    def test_final_publication_can_deliver_its_actual_output(self):
        if not shutil.which('pandoc'):
            self.skipTest('Publication integration needs Pandoc')
        c = self.case
        c.root = c.directory / 'final-publication'
        c.app = ToolApplication.create(c.root, 'Export the note and disclose unfinished review.',
            model='fixture', sources={}, services=c.services, allow_execution=True,
            max_turns=1, max_tokens=200000, max_attempts=20, tool_contracts='full')
        c._write('note.md', '# Note\n\nAvailable work.\n')
        c.decisions.append(fixture._action('export_report', markdown='note.md',
            layout='article', compile_pdf=False, delivery={
                'summary': 'Editable publication', 'artifacts': ['note.md'],
                'unresolved': ['Independent review remains unfinished.']}))
        self.drain(approve=lambda *_: True)
        self.assertEqual(c.app.session.manifest.status, 'partial', c.app.result_snapshot())
        exported = c._ok('export_report')
        self.assertTrue((c.app.workspace.resolve(exported['data']['project']) / 'main.tex').is_file())
        self.assertEqual(c.app.session.store.read_json('delivery.json')['unresolved'],
            ['Independent review remains unfinished.'])
        self.assertFalse(c.inspection_requests)

    def test_workspace_write_submits_actual_output_for_independent_review(self):
        c = self.case
        c.decisions.append(fixture._action('write_file', path='note.md', content='A bounded result.\n',
            delivery={'summary': 'A bounded note', 'artifacts': ['note.md']}))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(len(c.inspection_requests), 1)
        self.assertEqual(c.app.session.store.read_json('delivery.json')['summary'], 'A bounded note')

    def test_new_task_can_deliver_without_unnecessary_independent_review(self):
        c = self.case
        c._write('note.md', 'A short note.\n')
        c.decisions.append(fixture._action('deliver_result', summary='A short note',
            artifacts=['note.md'], independent_review=False))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertFalse(c.inspection_requests)
        self.assertIs(c.app.session.store.read_json('delivery.json')['independent_review'], False)

    def test_saved_task_keeps_its_automatic_review_contract(self):
        c = self.case
        c.app.state['config'].pop('delivery_review_selection')
        c.app._save()
        c._reload()
        c._write('note.md', 'A short note.\n')
        c.decisions.append(fixture._action('deliver_result', summary='A short note',
            artifacts=['note.md'], independent_review=False))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(len(c.inspection_requests), 1)

    def test_mixed_batch_retains_effects_but_cannot_review_unobserved_production(self):
        c = self.case
        claim = {'summary':'A bounded note', 'artifacts':['note.md']}
        c.decisions.append([fixture._action('write_file', path='note.md', content='Actual work.\n'),
            fixture._action('review_result', **claim)])
        self.drain()
        self.assertEqual((c.root/'workspace/note.md').read_text(), 'Actual work.\n')
        self.assertIn('separate review', c._last('review_result')['error'])
        self.assertFalse(c.inspection_requests)
        receipts = deepcopy(c.app.state['turns'][0]['tools'])
        c._reload()
        c.decisions.append(fixture._action('deliver_result', **claim))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertEqual(c.app.state['turns'][0]['tools'], receipts)

    @unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1',
        'Real PDF compilation runs in the server publication integration phase')
    def test_native_publication_is_a_service_before_checked_same_version_delivery(self):
        if not all(shutil.which(name) for name in ('pandoc', 'pdflatex', 'bibtex')):
            self.skipTest('Publication integration needs the server TeX environment')
        c = self.case
        c.decisions.append(fixture._action('write_file', path='note.md',
            content='# Observations\n\nThis bounded note describes the supplied observations.\n'))
        self.drain()
        c.decisions.append(fixture._action('export_report', markdown='note.md', compile_pdf=True))
        self.drain(approve=lambda *_: True)
        exported = c._ok('export_report')
        project = c.app.workspace.resolve(exported['data']['project'])
        self.assertTrue((project/'main.pdf').read_bytes().startswith(b'%PDF'))
        self.assertFalse(c.inspection_requests)
        self.assertFalse((c.root/'delivery.json').exists())
        claim = {'summary':'A bounded typeset note', 'artifacts':['note.md', *exported['artifacts']],
            'report':{'status':'candidate','manuscript':'note.md','formats':['pdf']}}
        c.decisions.append(fixture._action('deliver_result', **claim))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        from simple_ar.tools.delivery_versions import DeliveryVersions
        version = DeliveryVersions(c.root).select(1)
        self.assertIn(str((project/'main.pdf').relative_to(c.root)), version['files'])
        before = deepcopy(c.app.ledger.to_dict())
        c._reload()
        self.drain()
        self.assertEqual(c.app.ledger.to_dict(), before)


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_work_service_boundary import WorkServiceBoundaryTests
        self.old = WorkServiceBoundaryTests()
        self.old.setUp()
        self.addCleanup(self.old.doCleanups)
        self.case = self.old.case
        self.case.app.state['config'].update(work_selection_policy='native.v4',
            work_lifecycle='integrated.v3', inspection_execution_protocol='shared.v2')
        self.case.app._save()
        self.case._reload()

    def test_native_schema_and_dispatch_keep_completion_with_task_owner(self):
        c = self.case
        available = {row['function']['name']:row['function']['parameters'] for row in c.app.schemas}
        self.assertNotIn('handoff', available['service_demo__optional']['properties'])
        self.assertNotIn('service_demo__required', available)
        self.assertNotIn('service_demo__whole', available)
        c.decisions.append(fixture._action('service_demo__optional', handoff={
            'summary':'Hidden completion', 'artifacts':['existing.txt']}))
        self.old.drain()
        self.assertIn('service operation', c._last('service_demo__optional')['error'])
        self.assertFalse(c.app.operations.extensions[0]._modules)
        self.assertFalse(c.inspection_requests)

    def test_native_service_result_never_acquires_completion(self):
        c = self.case
        c.decisions.append(fixture._action('service_demo__rogue'))
        self.old.drain()
        result = c._last('service_demo__rogue')
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('secret_claim', result['data'])
        self.assertFalse((c.root/'delivery.json').exists())
        self.assertFalse(c.inspection_requests)

    def test_new_native_service_cannot_claim_unrequested_completion(self):
        c = self.case
        c.app.state['config']['work_selection_policy'] = 'native.v5'
        c.app._save()
        c._reload()
        c.decisions.append(fixture._action('service_demo__rogue'))
        self.old.drain()
        self.assertEqual(c._last('service_demo__rogue')['status'], 'blocked')
        self.assertFalse((c.root/'delivery.json').exists())


if __name__ == '__main__':
    unittest.main()
