"""Work continuity, independent evidence and actual delivery via the shared loop.

Model decisions are controlled here; execution, approvals, receipts and recovery
are real. These tests do not measure autonomous model quality.
"""
from copy import deepcopy
import json
import sys
import unittest

from simple_ar.app.professional_conversation import ProfessionalConversation
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.working_context import CITATION_HANDOFF_POLICY
from tests import test_professional_work_loop as fixture
from tests.test_working_context import block, observation, select

_action = fixture._action


class WorkingArchiveTests(unittest.TestCase):
    def test_rewritten_notes_archive_work_without_claiming_evidence_coverage(self):
        memory = {'notes': [], 'through_turn': 0, 'compacted_records': set()}
        for i in range(3):
            record = f'records/{i}.json'
            view = {'observations': [{'turn': i, 'record': record, 'arguments': {}, 'truncated': False}],
                    'previous_notes': deepcopy(memory['notes']), 'handoff_policy': CITATION_HANDOFF_POLICY}
            snapshot = {'through_turn': i + 1, 'notes': [{'text': f'Current work {i}', 'records': [record],
                                                        'handoff_policy': CITATION_HANDOFF_POLICY}]}
            memory = ProfessionalConversation.adopt(previous=memory, snapshot=snapshot, view=view,
                archive=True, operations_for_turn=lambda n: {f'records/{n}.json': {}})
            self.assertEqual(memory['compacted_records'], {record})
            self.assertEqual(memory['archived_records'], {f'records/{n}.json' for n in range(i + 1)})
        self.assertEqual(memory['through_turn'], 0)  # No invented contiguous evidence handoff.
        failed = ProfessionalConversation.adopt(previous=memory, snapshot=None, view={
            'observations': [{'turn': 3, 'record': 'records/3.json'}]}, archive=True,
            operations_for_turn=lambda n: {})
        self.assertEqual(failed['archived_records'], memory['archived_records'])

    def test_archive_keeps_recent_results_explicit_originals_and_stale_note_fallback(self):
        text = 'ORIGINAL_UNCHANGED_SOURCE ' * 150
        rows = [observation(0, text, kind='operation_result')]
        args = dict(material_policy='working', require_work_notes=True, fit_work_notes=True,
            sparse_handoffs=True, processed_turns=[], archived_turns=[0],
            work_notes=[{'text': 'Current understanding, not verified evidence', 'records': [],
                         'handoff_policy': CITATION_HANDOFF_POLICY}],
            observations=rows, turn_blocks=[block(0, text), block(1, 'Recent error diagnostic'),
                                           block(2, 'Latest actual result')], context_token_target=20000)
        messages, _ = select(**args)
        self.assertNotIn(text, json.dumps(messages))
        self.assertIn('Recent error diagnostic', json.dumps(messages))
        self.assertIn('Latest actual result', json.dumps(messages))
        explicit, _ = select(**{**args, 'selected_records': ['records/0.json']})
        self.assertIn(text, json.dumps(explicit))
        stale = deepcopy(args)
        stale['work_notes'][0]['current'] = False
        fallback, _ = select(**stale)
        self.assertIn(text, json.dumps(fallback))


class GoalWorkTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._create(delivery_protocol=None, current_method=True,
            goal='Prepare a bounded note from the supplied observations; independently check numerical claims.')

    def test_candidate_check_does_not_finish_and_delivery_reuses_it_after_reload(self):
        case = self.case
        case._write('note.md', 'Three supplied observations; no population inference.\n')
        claim = {'summary': 'Bounded observation note', 'artifacts': ['note.md']}
        def review(view):
            response = case._review_complete_targets(view)
            if isinstance(response, dict):
                response['investigations'] = [{'claim': 'Optional colour refinement.',
                    'necessary': False, 'scope': 'Optional presentation only.', 'basis': 'not_checked',
                    'method': 'Not undertaken; not requested.', 'reasoning': 'No necessary work is missing.',
                    'evidence': [], 'outcome': 'unresolved'}]
            return response
        case.review = review
        case.decisions.append(_action('review_result', **claim))
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))
        self.assertNotIn(case.app.session.manifest.status, {'completed', 'partial'})
        self.assertFalse((case.root / 'delivery.json').exists())
        proof = case._last('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'passed', proof)
        calls = case.send.call_count
        case._reload()
        case._submit(_action('deliver_result', **claim))
        self.assertEqual(case.app.session.manifest.status, 'completed')
        self.assertEqual(case.send.call_count, calls)
        self.assertEqual(case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['identity'],
                         proof['identity'])
        case._submit(_action('write_file', path='note.md', content='Changed note for the next request.\n'),
                     feedback='Revise the note but preserve the original observations.')
        case._submit(_action('deliver_result', **claim))
        self.assertGreater(case.send.call_count, calls)
        self.assertEqual(case.app.session.manifest.status, 'completed')
        self.assertEqual(len(case.app.result_snapshot()['delivery_versions']), 2)
        self.assertFalse(case._process_receipts())

    def test_one_submission_checks_then_commits_after_approval_and_reload(self):
        case = self.case
        case._write('claim.md', 'The supplied observations have mean 2.0.\n')
        work = {}
        def review(view):
            functions = {tool['function']['name']: tool['function'] for tool in case.inspection_requests[-1]['tools']}
            if not view.get('executed_checks'):
                scope = functions['run_process']['parameters']['properties']['cwd']['description'].split(
                    'workspace/inspection-work/', 1)[1].split('/', 1)[0]
                work['path'] = 'workspace/inspection-work/' + scope
                program = ("import csv,json,sys; from pathlib import Path; "
                    "v=[float(r['value']) for r in csv.DictReader(Path(sys.argv[1]).open())]; "
                    "Path('checked.json').write_text(json.dumps({'count':len(v),'mean':sum(v)/len(v)}))")
                return [_action('run_process', cwd=work['path'], argv=[sys.executable, '-c', program,
                    str(case.inputs / 'observations.csv')], inputs=['measurements/observations.csv'],
                    outputs=[work['path'] + '/checked.json'], timeout_sec=5)]
            paths = ['measurements/observations.csv', work['path'] + '/checked.json', 'claim.md']
            sources = case._inspection_sources(view, *paths)
            missing = [_action('read_file', path=path) for path, source in zip(paths, sources) if source is None]
            if missing:
                return missing
            result = json.loads(''.join(w['text'] for w in sources[1]['text_windows']))
            self.assertEqual(result, {'count': 3, 'mean': 2.0})
            ids = [source['subject'] for source in sources]
            return {'applicability': 'Check the requested mean against all supplied CSV rows.',
                'conclusion': 'complete', 'findings': [], 'evidence': ids, 'limitations': [],
                'investigations': [{'claim': 'The supplied sample mean is 2.0.',
                    'scope': 'All three supplied CSV observations, no population inference.',
                    'basis': 'executed', 'execution_records': view['executed_checks'],
                    'method': 'Read original CSV and independently compute arithmetic mean.',
                    'reasoning': 'Actual bound output reports count=3, mean=2.0.',
                    'evidence': ids, 'outcome': 'supported'}]}
        case.review = review
        claim = {'summary': 'Supplied sample mean', 'artifacts': ['claim.md']}
        case._submit(_action('deliver_result', **claim))
        self.assertIsNotNone(case.app.state['pending'], case.app.session.manifest.status_reason)
        self.assertEqual(case.app.state['pending']['action']['call']['function']['name'], 'run_process')
        token = case.app.approval_token(case.app.state['pending']['action']['call'])
        case._reload()
        case._run(approved_execution=token)
        proof = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        assessment = proof['result']['metadata']['delivery_assessment']
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertTrue(assessment['investigations'][0]['execution_records'])
        self.assertEqual(len(case._process_receipts()), 1)
        calls = case.send.call_count
        self.assertEqual(case.app.session.manifest.status, 'completed')
        ledger = deepcopy(case.app.ledger.to_dict())
        case._reload()
        case._run(should_stop=lambda: True)
        self.assertEqual(case.send.call_count, calls)
        self.assertEqual(case.app.ledger.to_dict(), ledger)

    def test_default_work_interface_selects_existing_operations_without_changing_permission(self):
        case = self.case
        self.assertEqual(case.app.state['config']['work_method'], 'execute.v1')
        case.app.state['config']['tool_contracts'] = 'on_demand'
        case.app._save()
        case._reload()
        offered = {tool['function']['name'] for tool in case.app.schemas}
        ordinary = {'read_file', 'search_files', 'list_directory', 'copy_project', 'edit_file',
            'write_file', 'run_process', 'deliver_result', 'load_tools', 'read_history'}
        self.assertTrue(ordinary <= offered)
        self.assertFalse({'review_result', 'inspect_artifacts'} & offered)
        self.assertIn('inspect_artifacts', case.app._specialist_catalog())
        case._submit(_action('load_tools', names=['inspect_artifacts']))
        case._submit(_action('read_file', path='measurements/observations.csv'))
        case._reload()
        self.assertTrue(ordinary <= {tool['function']['name'] for tool in case.app.schemas})
        case._submit(_action('load_tools', names=['review_result']))
        case._submit(_action('read_file', path='measurements/observations.csv'))
        case._ok('read_file')
        self.assertTrue(case.app.workspace.call('read_file', {'path': 'measurements/observations.csv'},
            identity='selected-read').ok)
        self.assertFalse(case.app.workspace.call('write_file', {'path': 'measurements/observations.csv',
            'content': 'Forbidden source mutation'}, identity='source-write').ok)
        case.app.state['config']['work_method'] = 'goal.v1'
        case.app._save()
        case._reload()
        self.assertIn('review_result', {tool['function']['name'] for tool in case.app.schemas})
        self.assertEqual(case.app.state['config']['work_method'], 'goal.v1')

    def test_last_affordable_call_can_deliver_partial_without_unexecutable_read(self):
        case = self.case
        root = case.directory / 'one-call'
        case.app = ToolApplication.create(root, 'Deliver a useful note with honest unfinished checks.',
            model='fixture', services=case.services, sources={}, control_protocol='operations.v1',
            max_turns=1, max_tokens=100000, max_attempts=12, tool_contracts='full')
        (root / 'workspace/note.md').write_text('Existing useful work.\n')
        case.decisions.append(_action('deliver_result', summary='Useful existing note', artifacts=['note.md'],
            unresolved=['Independent verification has not finished.']))
        case.app.run(progress=lambda _: None)
        self.assertEqual(case.app.session.manifest.status, 'partial')
        self.assertEqual(case.send.call_count, 1)
        self.assertEqual({tool['function']['name'] for tool in case.app._closing_schemas()},
                         {'deliver_result', 'ask_user'})
        self.assertFalse(case.app.state['pending'])

    def test_claimed_execution_or_unfinished_check_cannot_become_required_defect(self):
        inspector = self.case.app.operations.inspection
        saved = {'work_method': 'goal.v1', 'source_rounds': [], 'targets': [],
            'evidence': [{'subject': 'evidence-1', 'readable': True, 'sha256': 'a' * 64,
                          'windows': [{'text': 'Actual observed text.'}]}],
            'delivery_context': {'schema_version': 'delivery_review.v3', 'scope_policy': 'applicable.v2',
                                 'artifacts': {'note': 'a' * 64}}}
        value = {'applicability': 'Check the actual requested statement.', 'conclusion': 'incomplete',
            'evidence': ['evidence-1'], 'limitations': ['Check unfinished.'], 'findings': [],
            'investigations': [{'claim': 'The statement is supported.', 'scope': 'Observed text only.',
                'basis': 'executed', 'execution_records': ['made-up-receipt'], 'method': 'Claimed a run.',
                'reasoning': 'No actual computation was performed.', 'evidence': ['evidence-1'],
                'outcome': 'unresolved'}]}
        with self.assertRaisesRegex(ValueError, 'actual own successful'):
            inspector._delivery_decision(saved, value)
        value['investigations'][0].update(basis='not_checked', execution_records=[])
        value['findings'] = [{'severity': 'required', 'message': 'Must be wrong because not checked.',
                              'artifacts': ['note'], 'investigation': 0}]
        with self.assertRaisesRegex(ValueError, 'actually contradicted'):
            inspector._delivery_decision(saved, value)
