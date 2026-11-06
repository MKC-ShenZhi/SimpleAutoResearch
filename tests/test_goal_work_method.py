"""Work continuity, independent evidence and actual delivery via the shared loop.

Model decisions are controlled here; execution, approvals, receipts and recovery
are real. These tests do not measure autonomous model quality.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

from simple_ar.app.professional_conversation import ProfessionalConversation
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.working_context import CITATION_HANDOFF_POLICY
from tests import test_professional_work_loop as fixture
from tests.test_working_context import block, observation, select

_action = fixture._action


class NativeGoalWorkTests(unittest.TestCase):
    def test_closed_history_continues_without_replaying_or_forging_provider_reasoning(self):
        from simple_ar.app.professional_conversation import continuation_blocks, NATIVE_HISTORY_POLICY
        reply = {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'known-write', 'type': 'function',
            'function': {'name': 'write_file', 'arguments': '{"path":"note.md","content":"known"}'}}]}
        result = {'role': 'tool', 'tool_call_id': 'known-write', 'content': '{"status":"ok","sha256":"bound"}'}
        feedback = {'role': 'user', 'content': 'Keep the known output and explain its limits.'}
        valid = {'role': 'assistant', 'content': 'Continue from actual work.', 'reasoning_content': 'Original provider text.'}
        blocks = [[feedback, reply, result], [valid]]
        original = deepcopy(blocks)
        projected = continuation_blocks(blocks, policy=NATIVE_HISTORY_POLICY, thinking_mode='enabled')
        self.assertEqual(blocks, original)
        self.assertEqual(projected[0][0], feedback)
        self.assertEqual(json.loads(projected[0][1]['content'])['closed_journal_observations'], [reply, result])
        self.assertEqual(projected[1], [valid])
        self.assertFalse(any(row['role'] in {'assistant', 'tool'} for row in projected[0]))
        self.assertEqual(continuation_blocks(blocks, policy=None, thinking_mode='enabled'), original)
        with self.assertRaisesRegex(ValueError, 'Unclosed native work'):
            continuation_blocks([[reply]], policy=NATIVE_HISTORY_POLICY, thinking_mode='enabled')

    def test_native_work_uses_one_queue_through_approval_delivery_and_feedback(self):
        case = fixture.OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._create(delivery_protocol=None, current_method=True, selection_policy='native.v3',
            professional_contract='work.v12',
            goal='Prepare a bounded note from the supplied observations; preserve the sources.')
        self.assertEqual(case.app.state['config']['work_selection_policy'], 'native.v3')
        self.assertIsNone(case.app.operations.selection)
        self.assertNotIn('select_work', {r['function']['name'] for r in case.app.schemas})
        source = (case.inputs/'observations.csv').read_bytes()
        program = ("from pathlib import Path\np=Path('executions.txt')\n"
            "p.write_text(p.read_text()+'run\\n' if p.exists() else 'run\\n')\n"
            "Path('note.md').write_text('Three supplied observations.\\n')\n")
        case.decisions.append([
            _action('write_file', path='project/produce.py', content=program),
            _action('run_process', argv=[sys.executable,'produce.py'], cwd='workspace/project', inputs=['produce.py'],
                outputs=['note.md','executions.txt']),
            _action('deliver_result', summary='Bounded note', artifacts=['project/note.md'])])
        case._run()
        self.assertEqual(case.app.state['pending']['kind'], 'tool')
        self.assertTrue((case.root/'workspace/project/produce.py').exists())
        self.assertFalse((case.root/'workspace/project/note.md').exists())
        self.assertFalse((case.root/'delivery.json').exists())
        token = case.app.approval_token(case.app.state['pending']['call'])
        from simple_ar.app.task_view import read_task_view
        view = read_task_view(case.root, read_record=case.app.session.store.read_json,
            ledger=case.app.ledger, authorize_execution=lambda: None)
        self.assertEqual(view['approval']['token'], token)
        case._reload()
        self.assertEqual(case.app.approval_token(case.app.state['pending']['call']), token)
        case._run(approved_execution=token)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/project/executions.txt').read_text(), 'run\n')
        self.assertIsNone(case.app.operations.work)
        self.assertIsNone(case.app.implementation)
        self.assertTrue(case.inspection_requests)
        versions = case.app.workspace.artifact_hashes(['project/note.md'])
        self.assertEqual(case.app.workspace.artifact_sources(versions)['project/note.md']['status'], 'recorded')
        self.assertFalse(list((case.root/'attempts').glob('*/work_choice.json')))
        prefix = deepcopy(case.app.ledger.to_dict()['entries'])
        initial = (case.root/'workspace/project/note.md').read_bytes()
        context = json.loads(case.app._messages()[1]['content'])
        self.assertNotIn('work_selection', context)
        self.assertNotIn('work_plan', context)
        self.assertTrue(context['recorded_work']['observations'])
        case.decisions.append([
            _action('write_file', path='short-note.md', content='Three observations.\n'),
            _action('deliver_result', summary='Shortened bounded note', artifacts=['short-note.md'])])
        case._run(feedback='Keep the original and save a shorter note; do not repeat unchanged calculations.')
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/project/note.md').read_bytes(), initial)
        self.assertEqual((case.root/'workspace/project/executions.txt').read_text(), 'run\n')
        self.assertEqual((case.inputs/'observations.csv').read_bytes(), source)
        self.assertEqual(case.app.ledger.to_dict()['entries'][:len(prefix)], prefix)
        from simple_ar.tools.delivery_versions import DeliveryVersions
        self.assertEqual([v['request_count'] for v in DeliveryVersions(case.root).versions()], [1,2])
        case._reload()
        self.assertEqual(case.app.session.manifest.status, 'completed')


class ExplicitWorkDecisionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._create(delivery_protocol=None, current_method=True, selection_policy='explicit.v2',
            goal='Prepare a bounded note from the supplied observations; preserve the sources.')

    def choice(self, decision='work', actions=(), **kwargs):
        return _action('select_work', decision=decision, actions=list(actions), **kwargs)

    def drain(self, **kwargs):
        case = self.case
        return case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']), **kwargs)

    def test_work_candidate_never_discards_actions_or_implicitly_delivers_across_reload_feedback(self):
        case = self.case
        program = "from pathlib import Path;Path('note.md').write_text('Three supplied observations.\\n')"
        candidate = {'summary':'Draft awaiting its requested investigation', 'artifacts':['note.md'],
                     'unresolved':['The requested work is still in progress.']}
        case.decisions.append(self.choice(actions=[_action('write_file', path='produce.py', content=program)],
            candidate=candidate))
        self.drain()
        self.assertFalse((case.root/'delivery.json').exists())
        self.assertFalse(case.inspection_requests)
        self.assertIn('work_selection_policy', case.app.state['config'])
        self.assertEqual(case.app._selection_context()['current_candidate'], candidate)
        case.decisions.append(self.choice(actions=[_action('run_process', argv=[sys.executable,'produce.py'],
            inputs=['produce.py'], outputs=['note.md'])]))
        self.drain()
        self.assertEqual(case.app.state['pending']['kind'], 'tool')
        token = case.app.approval_token(case.app.state['pending']['call'])
        case._reload()
        self.drain(approved_execution=token)
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertFalse((case.root/'delivery.json').exists())
        case.decisions.append(self.choice('deliver', candidate={'summary':'Bounded note', 'artifacts':['note.md']}))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        original = (case.root/'delivery.json').read_bytes()
        reviews = len(case.inspection_requests)
        feedback = 'Shorten the same note without recomputing unchanged observations.'
        case.decisions.append(self.choice(actions=[_action('write_file', path='note.md',
            content='Three observations.\n',
            expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest())]))
        self.drain(feedback=feedback)
        self.assertEqual((case.root/'delivery.json').read_bytes(), original)
        self.assertEqual(len(case.inspection_requests), reviews)
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertNotIn(case.app.session.manifest.status, {'completed','partial'})
        case._reload()
        case.decisions.append(self.choice('deliver'))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        from simple_ar.tools.delivery_versions import DeliveryVersions
        self.assertEqual([v['request_count'] for v in DeliveryVersions(case.root).versions()], [1,2])

    def test_explicit_stop_is_separate_from_work_and_preserves_an_honest_resource_exit(self):
        case = self.case
        candidate = {'summary':'Partial observations', 'artifacts':['note.md'],
                     'unresolved':['Independent verification is unfinished.']}
        case.decisions.extend([
            self.choice('deliver', actions=[_action('write_file', path='discarded.txt', content='No.')],
                candidate=candidate),
            self.choice('blocked', candidate=candidate),
            self.choice(actions=[_action('write_file', path='note.md', content='Three observations.\n')],
                candidate=candidate)])
        self.drain()
        self.assertFalse((case.root/'workspace/discarded.txt').exists())
        self.assertFalse((case.root/'delivery.json').exists())
        self.assertEqual([r['status'] for r in case._observations('select_work')], ['blocked','blocked'])
        reason = 'The remaining shared allowance cannot fund the necessary independent check.'
        case.decisions.append(self.choice('blocked', candidate=candidate, stop_reason=reason))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'partial', case.app.result_snapshot())
        self.assertIn(reason, case.app.session.store.read_json('delivery.json')['unresolved'])
        self.assertFalse(case.inspection_requests)

    def test_rejected_delivery_returns_to_explicit_work_without_a_new_scheduler(self):
        case = self.case
        def review(view):
            opinion = case._review_complete_targets(view)
            if isinstance(opinion, dict) and 'population' in (case.root/'workspace/note.md').read_text():
                opinion['investigations'] = [{'claim':'The observation generalizes to a population.',
                    'scope':'Supplied note.', 'basis':'observed', 'necessary':True,
                    'method':'Read the actual bounded observation.', 'reasoning':'No population sample was supplied.',
                    'evidence':opinion['evidence'], 'outcome':'contradicted'}]
                opinion['findings'] = [{'severity':'required', 'message':'Remove the population inference.',
                    'artifacts':[str(case.root/'workspace/note.md')], 'investigation':0}]
            elif isinstance(opinion, dict):
                opinion['reassessed_prior'] = [r['id'] for r in view.get('prior_required', [])]
            return opinion
        case.review = review
        candidate = {'summary':'Bounded observation note', 'artifacts':['note.md']}
        case.decisions.extend([
            self.choice(actions=[_action('write_file', path='note.md', content='The population is characterized.\n')],
                candidate=candidate), self.choice('deliver')])
        self.drain()
        self.assertNotIn(case.app.session.manifest.status, {'completed','partial'})
        self.assertEqual(len(case.app._selection_context()['required_findings']), 1)
        case._reload()
        case.decisions.append(self.choice(actions=[_action('write_file', path='note.md',
            expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest(),
            content='Only the supplied observations are characterized.\n')]))
        self.drain()
        self.assertFalse((case.root/'delivery.json').exists())
        case.decisions.append(self.choice('deliver'))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertFalse(case._process_receipts())


class WorkingArchiveTests(unittest.TestCase):
    def test_knowledge_checkpoint_cannot_supply_an_action_agenda_and_old_contract_still_loads(self):
        from simple_ar.app.work_memory import _bound_response
        from pydantic import ValidationError
        view = {'note_contract':'knowledge.v1', 'observations':[{'id':'o1','record':'records/result.json'}],
                'previous_notes':[]}
        bound = _bound_response({'notes':[{'kind':'result', 'text':'The recorded calculation returned 2.',
            'observations':['o1']}]}, view, lambda notes:notes)
        self.assertEqual(bound[0]['knowledge_kind'], 'result')
        self.assertEqual(bound[0]['records'], ['records/result.json'])
        with self.assertRaises(ValidationError):
            _bound_response({'notes':[{'kind':'next_action', 'text':'Repeat optional checks.',
                'observations':['o1']}]}, view, lambda notes:notes)
        legacy = {k:v for k,v in view.items() if k!='note_contract'}
        self.assertEqual(_bound_response({'notes':[{'text':'Old saved understanding.',
            'observations':['o1']}]}, legacy, lambda notes:notes)[0]['text'], 'Old saved understanding.')
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

    def choice(self, *, actions=(), candidate=None, remaining=None):
        requests = self.case.app.state['requests']
        return _action('select_work', remaining=(remaining if remaining is not None else [
            {'request_index':len(requests)-1, 'quote':requests[-1],
             'missing_outcome':'Produce the requested bounded note.'}]),
            actions=list(actions), candidate=candidate)

    def test_usable_candidate_skips_optional_process_and_checks_same_version(self):
        case = self.case
        case._write('note.md', 'Three supplied observations; no population inference.\n')
        body = (case.root/'workspace/note.md').read_bytes()
        case.decisions.append(self.choice(remaining=[],
            actions=[_action('run_process', argv=[sys.executable, '-c',
                "from pathlib import Path;Path('unrequested.txt').write_text('extra work')"])],
            candidate={'summary':'Bounded observation note', 'artifacts':['note.md']}))
        case.app.run(progress=lambda _:None)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/note.md').read_bytes(), body)
        self.assertFalse((case.root/'workspace/unrequested.txt').exists())
        self.assertFalse(case._process_receipts())
        turn = next(row for row in case.app.state['turns']
            if case.app.session.store.exists(str(Path(row['reply']).with_name('work_choice.json'))))
        raw = case.app.session.store.read_json(str(Path(turn['reply']).with_name('work_choice.json')))
        self.assertEqual(json.loads(raw['tool_calls'][0]['function']['arguments'])['actions'][0]['tool_name'], 'run_process')
        interpreted = case.app.session.store.read_json(turn['reply'])
        self.assertEqual([c['function']['name'] for c in interpreted['tool_calls']], ['deliver_result'])

    def test_offered_source_variants_require_the_sources_that_admission_checks(self):
        from simple_ar.app.work_selection import _OfferedChoice, WorkChoice
        from pydantic import ValidationError
        schema = self.case.app.schemas[0]['function']['parameters']
        variants = schema['properties']['remaining']['items']['anyOf']
        requirements = [set(schema['$defs'][row['$ref'].split('/')[-1]]['required']) for row in variants]
        self.assertIn({'request_index','quote','missing_outcome'}, requirements)
        self.assertIn({'finding_id','missing_outcome'}, requirements)
        self.assertNotIn('candidate', schema['required'])
        self.assertNotIn('anyOf', schema['properties']['candidate'])
        for source in ({'request_index':0,'quote':self.case.app.state['requests'][0]},
                       {'finding_id':'actual-required-finding'}):
            wire = {'remaining':[{**source,'missing_outcome':'An actual requested outcome.'}], 'actions':[]}
            self.assertIsNone(_OfferedChoice.model_validate(wire).candidate)
            self.assertIsNone(WorkChoice.model_validate(wire).candidate)
        with self.assertRaises(ValidationError):
            _OfferedChoice.model_validate({'remaining':[{'missing_outcome':'Inventory the project.'}],
                'actions':[]})
        legacy = {'remaining':[{'request_index':0,'quote':'Original request','finding_id':'',
            'missing_outcome':'Old retained choice.'}], 'actions':[], 'candidate':None}
        self.assertIsNone(WorkChoice.model_validate(legacy).candidate)
        for closing in (False, True):
            offered = self.case.app._closing_schemas() if closing else self.case.app.schemas
            names = set(offered[0]['function']['parameters']['$defs']['WorkOperation']
                ['properties']['tool_name']['enum'])
            supplied = {row['function']['name'] for row in
                self.case.app._selection_context(closing=closing)['operations']}
            self.assertEqual(names, supplied)
            self.assertFalse(names & {'deliver_result','review_result','select_work'})
            self.assertTrue(all('delivery' not in row['function']['parameters'].get('properties', {})
                for row in self.case.app._selection_context(closing=closing)['operations']))
            if closing:
                self.assertEqual(names, {'ask_user'})
        self.assertIn('delivery', self.case.app.gateway.registry.get('write_file').input_schema['properties'])
        closing_text = self.case.app._system_text(closing=True)
        self.assertIn('candidate.unresolved', closing_text)
        self.assertIn('actions empty', closing_text)

    def test_production_hands_off_candidate_across_approval_reload_and_feedback_without_reexecution(self):
        case = self.case
        program = ("from pathlib import Path\np=Path('executions.txt')\n"
            "p.write_text(p.read_text()+'run\\n' if p.exists() else 'run\\n')\n"
            "Path('note.md').write_text('Three supplied observations; no population inference.\\n')\n")
        case._write('produce.py', program)
        case.decisions.append(self.choice(actions=[_action('run_process',
            argv=[sys.executable, 'produce.py'], cwd='workspace', inputs=['produce.py'],
            outputs=['note.md','executions.txt'])],
            candidate={'summary':'Bounded observation note', 'artifacts':['note.md']}))
        case.app.run(progress=lambda _:None)
        self.assertEqual(case.app.state['pending']['kind'], 'tool')
        self.assertFalse((case.root/'workspace/note.md').exists())
        self.assertEqual(case.send.call_count, 1)
        token = case.app.approval_token(case.app.state['pending']['call'])
        case._reload()
        case._run(approved_execution=token)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/executions.txt').read_text(), 'run\n')
        calls = case.send.call_count
        feedback = 'Make the bounded note shorter while keeping the same observations.'
        case.decisions.append(_action('select_work', remaining=[{'request_index':1, 'quote':feedback,
            'missing_outcome':'Shorten the requested note.'}], actions=[_action('edit_file', path='note.md',
                expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest(),
                replacements=[{'old_text':'Three supplied observations; no population inference.',
                               'new_text':'Three observations only.'}])], candidate=None))
        case.app.run(feedback=feedback, progress=lambda _:None)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertGreater(case.send.call_count, calls)
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Three observations only.\n')
        self.assertEqual((case.root/'workspace/executions.txt').read_text(), 'run\n')
        from simple_ar.tools.delivery_versions import DeliveryVersions
        self.assertEqual([v['request_count'] for v in DeliveryVersions(case.root).versions()], [1,2])

    def test_generated_agenda_cannot_authorize_work_and_invalid_choice_can_be_corrected(self):
        case = self.case
        case.decisions.extend([
            self.choice(remaining=[{'request_index':0, 'quote':'Run SVG byte checks until deterministic.',
                'missing_outcome':'Follow an invented agenda.'}],
                actions=[_action('write_file', path='extra.txt', content='Must not execute.')]),
            self.choice(actions=[_action('write_file', path='note.md', content='Only three observations.\n')],
                candidate={'summary':'Bounded note', 'artifacts':['note.md']})])
        case._run()
        self.assertFalse((case.root/'workspace/extra.txt').exists())
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        rejected = case._observations()[0]
        self.assertEqual(rejected['status'], 'blocked')
        self.assertFalse(rejected['data']['executed'])
        self.assertIn('exact supplied original request', rejected['error'])

    def test_closed_raw_choice_recovers_without_rebuying_the_decision(self):
        from simple_ar.core.capabilities import ArtifactStore
        from tests.report_review_fixtures import CheckpointStop
        from unittest.mock import patch
        case = self.case
        case.decisions.append(self.choice(actions=[_action('write_file', path='note.md',
            content='Only the supplied observations.\n')],
            candidate={'summary':'Bounded note', 'artifacts':['note.md']}))
        write = ArtifactStore.write_json
        def interrupt(store, path, value, *args, **kwargs):
            if (path=='native_result.json' and store.root.name.startswith('native-turn-')
                    and store.exists('work_choice.json')):
                raise CheckpointStop()
            return write(store,path,value,*args,**kwargs)
        with patch.object(ArtifactStore,'write_json',new=interrupt), self.assertRaises(CheckpointStop):
            case.app.run(progress=lambda _:None)
        self.assertEqual(case.send.call_count, 1)
        self.assertFalse((case.root/'workspace/note.md').exists())
        case._reload()
        case._run()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertFalse(case.decisions)
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Only the supplied observations.\n')

    def test_partial_candidate_keeps_necessary_production_and_honest_unfinished_work(self):
        case = self.case
        case.decisions.append(self.choice(actions=[_action('write_file', path='note.md',
            content='Only the supplied observations are retained.\n')],
            candidate={'summary':'Useful partial note', 'artifacts':['note.md'],
                'unresolved':['Independent verification has not finished.']}))
        case._run()
        self.assertEqual(case.app.session.manifest.status, 'partial', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/note.md').read_text(),
            'Only the supplied observations are retained.\n')
        self.assertEqual(case.app.session.store.read_json('delivery.json')['unresolved'],
            ['Independent verification has not finished.'])
        self.assertFalse(case._process_receipts())

    def test_feedback_read_retains_candidate_without_submitting_unrevised_work(self):
        case = self.case
        case.decisions.append(self.choice(actions=[_action('write_file', path='note.md',
            content='Three supplied observations; no population inference.\n')],
            candidate={'summary':'Bounded observation note', 'artifacts':['note.md']}))
        case._run()
        self.assertEqual(case.app.session.manifest.status, 'completed')
        prior = (case.root/'delivery.json').read_bytes()
        reviews = len(case.inspection_requests)
        case.decisions.append(self.choice(actions=[_action('read_file', path='note.md')]))
        case.app.run(feedback='Shorten the same note after reading it.', progress=lambda _:None,
            should_stop=lambda: not case.decisions and case.app.state['pending'] is None
                and not any(turn['remaining'] for turn in case.app.state['turns']))
        self.assertEqual((case.root/'delivery.json').read_bytes(), prior)
        self.assertEqual(len(case.inspection_requests), reviews)
        self.assertNotIn(case.app.session.manifest.status, {'completed','partial'})
        self.assertEqual(case.app._selection_context()['current_candidate']['artifacts'], ['note.md'])

    def test_required_opinion_drives_only_the_affected_revision_and_candidate_handoff(self):
        case = self.case
        fixed = False
        def review(view):
            response = case._review_complete_targets(view)
            if isinstance(response, dict) and fixed:
                response['reassessed_prior'] = [row['id'] for row in view.get('prior_required', [])]
            if isinstance(response, dict) and not fixed:
                response['investigations'] = [{'claim':'The note describes the supplied observations.',
                    'scope':'Actually supplied note text.', 'basis':'observed', 'necessary':True,
                    'method':'Read the supplied note.', 'reasoning':'The controlled note states a population claim.',
                    'evidence':response['evidence'], 'outcome':'contradicted'}]
                response['findings'] = [{'severity':'required', 'message':'Limit the inference to the supplied sample.',
                    'artifacts':[str(case.root/'workspace/note.md')], 'investigation':0}]
            return response
        case.review = review
        def repair():
            nonlocal fixed
            concerns = case.app._selection_context()['required_findings']
            self.assertEqual(len(concerns), 1)
            fixed = True
            return self.choice(remaining=[{'finding_id':concerns[0]['id'],
                    'missing_outcome':'Resolve the actual inference defect.'}],
                actions=[_action('edit_file', path='note.md',
                    expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest(),
                    replacements=[{'old_text':'population', 'new_text':'sample'}])])
        case.decisions.extend([
            self.choice(actions=[_action('write_file', path='note.md', content='The population mean is 2.0.\n')],
                candidate={'summary':'Bounded observation note', 'artifacts':['note.md']}), repair])
        case._run()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'The sample mean is 2.0.\n')
        assessments = [json.loads(p.read_text()) for p in sorted((case.root/'attempts').glob('*/inspection/delivery_assessment.json'))]
        self.assertEqual([a['verdict'] for a in assessments], ['needs_revision','passed'])
        self.assertFalse(case._process_receipts())

    def test_candidate_check_does_not_finish_and_delivery_reuses_it_after_reload(self):
        case = self.case
        case._write('note.md', 'Three supplied observations; no population inference.\n')
        claim = {'summary': 'Bounded observation note', 'artifacts': ['note.md']}
        def review(view):
            response = case._review_complete_targets(view)
            if isinstance(response, dict):
                response['investigations'] = [{'claim':'The note is a bounded statement of supplied observations.',
                    'necessary':True, 'scope':'Actually read note and supplied source observations.',
                    'basis':'observed', 'method':'Read the supplied text.',
                    'reasoning':'The note retains the supplied evidence limits.',
                    'evidence':response['evidence'], 'outcome':'supported'},
                    {'claim': 'Optional colour refinement.',
                    'necessary': False, 'scope': 'Optional presentation only.', 'basis': 'not_checked',
                    'method': 'Not undertaken; not requested.', 'reasoning': 'No necessary work is missing.',
                    'evidence': [], 'outcome': 'unresolved'}]
            return response
        case.review = review
        case._submit(_action('review_result', **claim))
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
        case._submit(_action('write_file', path='note.md', content='Changed note for the next request.\n',
            expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest()),
                     feedback='Revise the note but preserve the original observations.')
        case._ok('write_file')
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Changed note for the next request.\n')
        case._submit(_action('deliver_result', **claim))
        self.assertGreater(case.send.call_count, calls)
        self.assertEqual(case.app.session.manifest.status, 'completed')
        self.assertEqual(len(case.app.result_snapshot()['delivery_versions']), 2)
        self.assertFalse(case._process_receipts())

    def test_one_submission_checks_then_commits_after_approval_and_reload(self):
        from unittest.mock import patch
        from simple_ar.tools.inspection import _DeliveryInspectionRequest
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
                    "Path('checked.json').write_text(json.dumps({'count':len(v),'mean':sum(v)/len(v)})); "
                    "print('RETAINED_OWN_CHECK='+json.dumps({'count':len(v),'mean':sum(v)/len(v)}))")
                return [_action('run_process', cwd=work['path'], argv=[sys.executable, '-c', program,
                    str(case.inputs / 'observations.csv')], inputs=['measurements/observations.csv'],
                    outputs=[work['path'] + '/checked.json'], timeout_sec=5)]
            paths = ['measurements/observations.csv', work['path'] + '/checked.json', 'claim.md']
            sources = case._inspection_sources(view, *paths)
            missing = [_action('read_file', path=path) for path, source in zip(paths, sources) if source is None]
            if missing:
                return missing[:1]  # Leave the calculation behind several later read turns.
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

        # A fallible checkpoint can replace native presentation, but cannot be
        # the only representation of an already executed check. Reopening the
        # owner's view must supply the original stdout without executing again.
        owner = case.app.operations.inspection
        saved = owner.store.read_json(owner._path(proof['identity'], 'input'))
        rows = owner._conversation_rows(saved)
        computation = next(row for row in rows if row['result'].get('tool_name') == 'run_process')
        self.assertLess(computation['turn'], max(row['turn'] for row in rows)-1)
        memory = {'notes':[{'text':'A fallible checkpoint says the mean may be 9.',
            'records':[computation['record']], 'source_integrity':'unknown',
            'observed_versions':{}, 'knowledge_kind':'result', 'handoff_policy':'citations.v1'}],
            'through_turn':0, 'compacted_records':set(),
            'archived_records':{row['record'] for row in rows}}
        protected = {name:(case.root/name).read_bytes() for name in
            ('actions.json','session_manifest.json','budget_ledger.json','delivery.json')}
        request = _DeliveryInspectionRequest.model_validate(saved['arguments'])
        with patch.object(owner, '_conversation_memory', return_value=memory):
            messages, projection = owner._working_messages(saved, request, None, [],
                case.services.llm_client, validate_capacity=False)
            self.assertIn('RETAINED_OWN_CHECK=', json.dumps(messages))
            supplied = [row for row in projection['selected_records']
                if row['source'].get('record') == computation['record']]
            self.assertTrue(supplied)
            self.assertTrue(all(row['version_status']=='historical' for row in supplied))
            legacy = {key:value for key,value in saved.items() if key!='inspection_material_policy'}
            old_messages, _ = owner._working_messages(legacy, request, None, [],
                case.services.llm_client, validate_capacity=False)
            self.assertNotIn('RETAINED_OWN_CHECK=', json.dumps(old_messages))
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual(case.send.call_count, calls)
        self.assertEqual(protected, {name:(case.root/name).read_bytes() for name in protected})

    def test_saved_selector_interface_retains_its_operation_offer_and_permissions(self):
        case = self.case
        self.assertEqual(case.app.state['config']['work_method'], 'goal.v2')
        self.assertEqual(case.app.state['config']['inspection_material_policy'], 'operations.v3')
        case.app.state['config']['tool_contracts'] = 'on_demand'
        case.app._save()
        case._reload()
        self.assertEqual({tool['function']['name'] for tool in case.app.schemas}, {'select_work'})
        offered = {tool['function']['name'] for tool in case.app.operation_schemas}
        ordinary = {'read_file', 'search_files', 'list_directory', 'copy_project', 'edit_file',
            'write_file', 'run_process', 'deliver_result', 'load_tools', 'read_history'}
        self.assertTrue(ordinary <= offered)
        self.assertFalse({'review_result', 'inspect_artifacts'} & offered)
        self.assertIn('inspect_artifacts', case.app._specialist_catalog())
        self.assertNotIn('review_result', case.app._specialist_catalog())
        case._submit(_action('load_tools', names=['inspect_artifacts']))
        case._submit(_action('read_file', path='measurements/observations.csv'))
        case._reload()
        self.assertTrue(ordinary <= {tool['function']['name'] for tool in case.app.operation_schemas})
        case._submit(_action('load_tools', names=['review_result']))
        case._submit(_action('read_file', path='measurements/observations.csv'))
        case._ok('read_file')
        self.assertTrue(case.app.workspace.call('read_file', {'path': 'measurements/observations.csv'},
            identity='selected-read').ok)
        self.assertFalse(case.app.workspace.call('write_file', {'path': 'measurements/observations.csv',
            'content': 'Forbidden source mutation'}, identity='source-write').ok)
        case.app.state['config']['work_method'] = 'goal.v1'
        case.app.state['config'].pop('inspection_material_policy', None)
        case.app._save()
        case._reload()
        self.assertIn('review_result', {tool['function']['name'] for tool in case.app.schemas})
        self.assertEqual(case.app.state['config']['work_method'], 'goal.v1')

    def test_claim_owned_defect_survives_rejected_opinion_reload_and_revision(self):
        case = self.case
        case._write('note.md', 'The population mean is 2.0.\n')
        source_bytes = (case.inputs/'observations.csv').read_bytes()
        malformed, revised = True, False
        concern = 'The three supplied observations support a sample mean, not a population mean.'

        def review(view):
            schema = next(row['function']['parameters'] for row in
                case.inspection_requests[-1]['tools'] if row['function']['name'] == 'record_inspection')
            self.assertNotIn('evidence', schema['properties'])
            self.assertNotIn('findings', schema['properties'])
            paths = ['measurements/observations.csv', 'note.md']
            sources = case._inspection_sources(view, *paths)
            missing = [_action('read_file', path=path) for path, row in zip(paths, sources) if row is None]
            if missing:
                return missing
            note = ''.join(window['text'] for window in sources[1]['text_windows'])
            self.assertIn('sample' if revised else 'population', note)
            check = {'claim':'The note limits inference to the supplied observations.',
                'scope':'The actual note and three supplied CSV rows.', 'basis':'observed',
                'method':'Compare the stated inference scope with the supplied observation scope.',
                'reasoning': 'Controlled fixture judgment of the text actually read.',
                'evidence':[row['subject'] for row in sources], 'necessary':True,
                'outcome':'supported' if revised else 'contradicted',
                'findings':[] if revised else [{'severity':'required', 'message':concern,
                    'artifacts':[str(case.root/'workspace/note.md')]}]}
            if malformed:
                check['claim_short'] = 'Undeclared field must remain rejected.'
            return {'applicability':'Check the requested note against the supplied observation scope.',
                'conclusion':'complete', 'investigations':[check], 'limitations':[],
                'reassessed_prior':[row['id'] for row in view.get('prior_required', [])]}

        case.review = review
        claim = {'summary':'Bounded explanation of the supplied observations.', 'artifacts':['note.md']}
        case._submit(_action('deliver_result', **claim))
        rejected = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertIn('claim_short', rejected['error'])
        self.assertEqual(rejected['result']['data']['rejected_opinion']['candidate_excerpt'][0]['candidate_messages'],
                         [concern])
        self.assertFalse((case.root/'delivery.json').exists())
        trace = case.app.session.store.resolve(rejected['result']['data']['rejected_opinion']['record'])
        original_trace = trace.read_bytes()
        case._reload()
        malformed = False
        case._submit(_action('deliver_result', **claim))
        assessment = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['result']['metadata']['delivery_assessment']
        self.assertEqual(assessment['verdict'], 'needs_revision')
        self.assertEqual(assessment['findings'][0]['message'], concern)
        self.assertFalse((case.root/'delivery.json').exists())
        revised = True
        case._submit(_action('write_file', path='note.md',
            content='The sample mean of the three supplied observations is 2.0.\n',
            expected_sha256=hashlib.sha256((case.root/'workspace/note.md').read_bytes()).hexdigest()))
        case._ok('write_file')
        case._submit(_action('deliver_result', **claim))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(trace.read_bytes(), original_trace)
        self.assertEqual((case.inputs/'observations.csv').read_bytes(), source_bytes)

    def test_last_affordable_call_can_deliver_partial_without_unexecutable_read(self):
        case = self.case
        root = case.directory / 'one-call'
        case.app = ToolApplication.create(root, 'Deliver a useful note with honest unfinished checks.',
            model='fixture', services=case.services, sources={}, control_protocol='operations.v1',
            max_turns=1, max_tokens=100000, max_attempts=12, tool_contracts='full')
        (root / 'workspace/note.md').write_text('Existing useful work.\n')
        case.decisions.append(_action('deliver_result', summary='Useful existing note', artifacts=['note.md'],
            unresolved=['The last affordable decision cannot fund independent verification.']))
        case.app.run(progress=lambda _: None)
        self.assertEqual(case.app.session.manifest.status, 'partial')
        self.assertEqual(case.send.call_count, 1)
        self.assertEqual({tool['function']['name'] for tool in case.app._closing_schemas()},
                         {'select_work'})
        context = json.loads(case.app._messages(client=case.app._client(), closing=True,
            schemas=case.app._closing_schemas())[1]['content'])
        self.assertEqual({tool['function']['name'] for tool in context['work_selection']['operations']},
            {'ask_user', 'deliver_result'})
        self.assertFalse(case.app.state['pending'])

    def test_terminal_json_opinion_uses_the_same_evidence_and_recovery_contract(self):
        from unittest.mock import patch
        from simple_ar.tools.inspection import _ClaimDeliveryResponse
        case = self.case
        root = case.directory / 'one-inspection-call'
        case.app = ToolApplication.create(root, 'Deliver the existing note with honest limits.',
            model='fixture', services=case.services, sources={}, max_turns=3,
            max_tokens=100000, max_attempts=12, tool_contracts='full')
        case.root = root
        (root / 'workspace/note.md').write_text('Existing useful work.\n')
        provider = case._provider
        originals = []
        def text_final(settings, request, **kwargs):
            response = provider(settings, request, **kwargs)
            offered = [item['function']['name'] for item in request.get('tools', [])]
            if offered != ['record_inspection']:
                return response
            message = response['choices'][0]['message']
            self.assertEqual(message['tool_calls'][0]['function']['name'], 'record_inspection')
            message['content'] = message.pop('tool_calls')[0]['function']['arguments']
            response['choices'][0]['finish_reason'] = 'stop'
            originals.append(deepcopy(message))
            return response
        case.send.side_effect = text_final
        case._submit(_action('deliver_result', summary='Useful existing note', artifacts=['note.md']))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(case.send.call_count, 2)
        owner = case.app.operations.inspection
        inputs = list((root/'tools/inspection').glob('*/input.json'))
        self.assertEqual(len(inputs), 1)
        saved = json.loads(inputs[0].read_text())
        trace_path = inputs[0].parent/f"sources-{len(saved['source_rounds'])-1}.json"
        trace = json.loads(trace_path.read_text())
        self.assertEqual(trace['response'], originals[0])
        self.assertEqual(trace['admission']['representation'], 'content_json')
        self.assertEqual(trace['results'], [])
        self.assertEqual(trace['admission']['parsed'], [])
        content = trace['response']['content']
        offered = trace['offer']['tools']
        with patch.object(owner, '_shared_final_bindings', side_effect=AssertionError('Admission dispatches nothing')):
            for text in ('```json\n'+content+'\n```', content+' trailing', '[]',
                         json.dumps({**json.loads(content), 'unknown':True})):
                with self.subTest(text=text[:40]):
                    _, error, final, _ = owner._admit_response(saved, [], offered,
                        _ClaimDeliveryResponse, set(), shared=True, content=text, closing=True)
                    self.assertTrue(error)
                    self.assertIsNone(final)
            for policy, closing, tools in [('operations.v1', True, offered),
                    (None, True, offered), ('operations.v2', False, offered),
                    ('operations.v2', True, offered*2)]:
                previous = {**saved, 'inspection_material_policy':policy}
                self.assertIsNone(owner._admit_response(previous, [], tools,
                    _ClaimDeliveryResponse, set(), shared=True, content=content, closing=closing)[2])
            invalid = json.loads(content)
            invalid['investigations'] = [{'claim':'The note was independently verified.',
                'scope':'All results', 'necessary':True, 'basis':'observed', 'execution_records':[],
                'method':'Read the note', 'reasoning':'Unsupported evidence ID',
                'evidence':['evidence-missing'], 'outcome':'supported', 'findings':[]}]
            _, error, final, _ = owner._admit_response(saved, [], offered,
                _ClaimDeliveryResponse, set(), shared=True, content=json.dumps(invalid), closing=True)
            self.assertTrue(error)
            self.assertIsNone(final)
        before = trace_path.read_bytes()
        case._reload()
        self.assertEqual(trace_path.read_bytes(), before)
        self.assertEqual(case.app.session.manifest.status, 'completed')
        self.assertEqual(case.send.call_count, 2)

    def test_failed_opinion_retains_investigation_across_retry_feedback_and_input_change(self):
        case = self.case
        case._write('claim.md', 'The sample mean is 2.0.\n')
        reject = True
        def review(view):
            if not view.get('executed_checks'):
                functions = {tool['function']['name']:tool['function']
                    for tool in case.inspection_requests[-1]['tools']}
                scope = functions['run_process']['parameters']['properties']['cwd']['description'].split(
                    'workspace/inspection-work/', 1)[1].split('/', 1)[0]
                return [_action('run_process', cwd='workspace/inspection-work/'+scope,
                    argv=[sys.executable, '-c', "import csv,sys; v=[float(r['value']) for r in "
                        "csv.DictReader(open(sys.argv[1]))]; print('INDEPENDENT_MEAN',sum(v)/len(v))",
                        str(case.inputs/'observations.csv')], inputs=['measurements/observations.csv'],
                    timeout_sec=5)]
            sources = case._inspection_sources(view, 'measurements/observations.csv', 'claim.md')
            missing = [_action('read_file', path=path) for path,source in
                zip(('measurements/observations.csv', 'claim.md'), sources) if source is None]
            if missing:
                return missing
            value = {'conclusion':'complete', 'applicability':'Check the note against the complete sample.',
                'findings':[], 'limitations':[], 'evidence':[row['subject'] for row in sources],
                'investigations':[{'claim':'The sample mean is 2.0.', 'necessary':True,
                    'scope':'Three supplied observations, not a population or causal claim.',
                    'basis':'derived', 'execution_records':view['executed_checks'],
                    'method':'Read the data and derive its mean; compare the actual independent calculation.',
                    'reasoning':'The observed values 1,2,3 average to 2.0; the independent run agrees.',
                    'evidence':[row['subject'] for row in sources], 'outcome':'supported'}]}
            if reject:
                value['unknown_field'] = 'Deliberately rejected controlled opinion, not a scientific defect.'
            return value
        case.review = review
        claim = {'summary':'Sample mean', 'artifacts':['claim.md']}
        case._submit(_action('deliver_result', **claim))
        token = case.app.approval_token(case.app.state['pending']['action']['call'])
        case._reload()
        case._run(approved_execution=token)
        failed = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertNotEqual(failed['result']['status'], 'ok')
        self.assertFalse((case.root/'delivery.json').exists())
        owner = case.app.operations.inspection
        protected = {owner._path(failed['identity'], leaf):owner.store.resolve(
            owner._path(failed['identity'], leaf)).read_bytes() for leaf in ('input','response','result')}
        prefix = deepcopy(case.app.ledger.to_dict()['entries'])
        reject = False
        case._reload()
        case._submit(_action('deliver_result', **claim))
        accepted = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(case.app.session.manifest.status, 'completed', accepted.get('error'))
        self.assertNotEqual(accepted['identity'], failed['identity'])
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual(case.app.ledger.to_dict()['entries'][:len(prefix)], prefix)
        owner = case.app.operations.inspection
        self.assertEqual({path:owner.store.resolve(path).read_bytes() for path in protected}, protected)
        saved = owner.store.read_json(owner._path(accepted['identity'], 'input'))
        inherited = owner._retained_inputs(saved)
        self.assertEqual([row['identity'] for row in inherited], [failed['identity']])
        history = owner._read_history(saved, {'inspection':failed['identity'], 'start_turn':0}, before_turn=0)
        self.assertIn(failed['identity'], history.data['text'])
        with self.assertRaisesRegex(ValueError, 'not an accessible retained'):
            owner._read_history(saved, {'inspection':'unrelated-owner'}, before_turn=0)
        calls = case.send.call_count
        case._reload()
        self.assertEqual(case.send.call_count, calls)
        case._submit(_action('edit_file', path='claim.md', replacements=[{
            'old_text':'The sample mean is 2.0.', 'new_text':'Sample mean: 2.0.'}],
            expected_sha256=hashlib.sha256((case.root/'workspace/claim.md').read_bytes()).hexdigest()),
            feedback='Shorten the note, retaining the same sample and its valid calculations.')
        case._submit(_action('deliver_result', **claim))
        latest = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(case.app.session.manifest.status, 'completed', latest.get('error'))
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual(len([row for row in case.app.result_snapshot()['delivery_versions']
            if row['status'] == 'retained']), 2)
        owner = case.app.operations.inspection
        saved = owner.store.read_json(owner._path(latest['identity'], 'input'))
        self.assertTrue(owner._executed_check_records(saved))
        original = (case.inputs/'observations.csv').read_bytes()
        try:
            (case.inputs/'observations.csv').write_text('id,value\n1,10\n2,20\n3,30\n')
            self.assertFalse(owner._executed_check_records(saved))
            self.assertTrue(owner._retained_result(latest['identity'], current_inputs=False).ok)
        finally:
            (case.inputs/'observations.csv').write_bytes(original)
        path = owner.store.resolve(next(iter(protected)))
        original = path.read_bytes()
        try:
            path.write_bytes(original+b' ')
            with self.assertRaisesRegex(ValueError, 'Retained investigation changed'):
                owner._retained_inputs(saved)
        finally:
            path.write_bytes(original)
        self.assertEqual({path:owner.store.resolve(path).read_bytes() for path in protected}, protected)

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
        for method in ('goal.v1', 'execute.v2'):
            with self.subTest(method=method):
                saved['work_method'] = method
                opinion = deepcopy(value)
                if method == 'execute.v2':
                    opinion.pop('evidence')
                    opinion['investigations'][0]['findings'] = opinion.pop('findings')
                with self.assertRaisesRegex(ValueError, 'actual own successful'):
                    inspector._delivery_decision(saved, opinion)
                opinion['investigations'][0].update(basis='not_checked', execution_records=[])
                finding = {'severity':'required', 'message':'Must be wrong because not checked.', 'artifacts':['note']}
                if method == 'execute.v2':
                    opinion['investigations'][0]['findings'] = [finding]
                else:
                    opinion['findings'] = [{**finding, 'investigation':0}]
                with self.assertRaisesRegex(ValueError, 'actually contradicted'):
                    inspector._delivery_decision(saved, opinion)
