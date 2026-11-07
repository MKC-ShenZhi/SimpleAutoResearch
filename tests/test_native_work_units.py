"""Native work keeps actual units, service boundaries and exact recovery."""
from copy import deepcopy
import json
import os
import shutil
import sys
import unittest
from unittest.mock import patch

from simple_ar.app.professional_conversation import work_unit_messages
from simple_ar.app.tool_application import ToolApplication
from tests import test_professional_work_loop as fixture


class NativeWorkProjectionTests(unittest.TestCase):
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

    def test_repeated_supplied_read_has_one_edit_offer_with_normal_work_return(self):
        c = self.case
        c.app.state['config']['tool_contracts'] = 'on_demand'
        c.app._save()
        c._reload()
        for action in [fixture._action('write_file', path='note.md', content='Original note.\n'),
                       fixture._action('read_file', path='note.md'),
                       fixture._action('read_file', path='note.md')]:
            c.decisions.append(action)
            self.drain()
        c._reload()
        c.decisions.append(fixture._action('load_tools', names=['read_file']))
        self.drain()
        decision = c.root / c.app.state['turns'][-1]['reply']
        offer = json.loads(decision.with_name('decision_budget.json').read_text())['input_projection']
        self.assertEqual(offer['observed_edit']['path'], 'note.md')
        self.assertIn('edit_file', offer['tool_names'])
        self.assertNotIn('read_file', offer['tool_names'])
        self.assertEqual(c._last('load_tools')['status'], 'ok')
        c._reload()
        c.decisions.append(fixture._action('read_file', path='note.md'))
        self.drain()
        decision = c.root / c.app.state['turns'][-1]['reply']
        offer = json.loads(decision.with_name('decision_budget.json').read_text())['input_projection']
        self.assertNotIn('observed_edit', offer)
        self.assertIn('read_file', offer['tool_names'])

    def test_observed_edit_and_check_queue_independent_review_before_more_production(self):
        c = self.case
        c.app.state['config']['tool_contracts'] = 'on_demand'
        c.app._save()
        c._reload()
        c.decisions.append(fixture._action('write_file', path='note.md', content='Original note.\n'))
        self.drain()
        c.decisions.append(fixture._action('edit_file', path='note.md',
            old_text='Original note.', new_text='Revised note.'))
        self.drain()
        c.decisions.append(fixture._action('run_process', argv=[sys.executable, '-c',
            "assert 'Revised note.' in open('note.md').read()"], inputs=['note.md']))
        self.drain()
        token = c.app.approval_token(c.app.state['pending']['call'])
        c.decisions.append(fixture._action('read_file', path='note.md'))
        self.drain(approved_execution=token)
        c._reload()
        handoff = c.app.state['turns'][-2]
        self.assertEqual(handoff['origin'], 'professional')
        reply = json.loads((c.root / handoff['reply']).read_text())
        self.assertEqual(reply['source_result'], c.app.state['turns'][-3]['tools'][-1]['result'])
        self.assertEqual(reply['tool_calls'][0]['function']['name'], 'review_result')
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
        c.decisions.append(fixture._action('review_result', **claim))
        self.drain()
        checked = len(c.inspection_requests)
        self.assertGreater(checked, 0)
        proof = c._last('review_result', reviewed=True)['metadata']['delivery_review']['identity']
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


if __name__ == '__main__':
    unittest.main()
