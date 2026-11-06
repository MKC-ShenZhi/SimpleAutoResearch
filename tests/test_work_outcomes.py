"""Outcome choices reuse the ordinary queue and preserve frozen selection contracts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

from simple_ar.app.tool_application import ToolApplication
from simple_ar.app.work_selection import WorkSelection
from tests import test_professional_work_loop as fixture


def message(**choice):
    return {'role': 'assistant', 'tool_calls': [{'id': 'choice', 'type': 'function',
        'function': {'name': 'select_work', 'arguments': json.dumps(choice)}}]}


def operation(name):
    return {'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'properties': {}}}}


class OutcomeInterpreterTests(unittest.TestCase):
    def compile(self, raw, *, policy='outcomes.v1', names=None, candidate=None):
        return WorkSelection.compile(raw,
            operations=[operation(name) for name in names or (
                'write_file', 'read_file', 'ask_user', 'review_result', 'deliver_result')],
            requests=['Write a useful note.'], candidate=candidate, concerns=[],
            production=['write_file'], policy=policy)

    def test_work_preserves_actions_and_candidate_without_implicit_review_or_delivery(self):
        actions = [fixture._action('write_file', path='note.md', content='Useful work.'),
            fixture._action('read_file', path='note.md')]
        candidate = {'summary': 'A draft', 'artifacts': ['note.md'], 'unresolved': ['Checking remains.']}
        raw = message(decision='work', remaining=[], actions=actions, candidate=candidate)
        retained = deepcopy(raw)
        compiled = self.compile(raw)
        self.assertEqual(raw, retained)
        self.assertEqual([row['function']['name'] for row in compiled['tool_calls']],
            ['write_file', 'read_file'])

    def test_review_is_an_explicit_candidate_call_and_delivery_keeps_its_claim(self):
        draft = {'summary': 'Current draft', 'artifacts': ['note.md'],
            'unresolved': ['Requested publication remains.']}
        reviewed = self.compile(message(decision='review', actions=[], candidate=draft))
        self.assertEqual(reviewed['tool_calls'][0]['function']['name'], 'review_result')
        self.assertEqual(json.loads(reviewed['tool_calls'][0]['function']['arguments'])['unresolved'],
            draft['unresolved'])
        complete = {'summary': 'Completed note', 'artifacts': ['note.md']}
        delivered = self.compile(message(decision='deliver', actions=[], candidate=complete))
        self.assertEqual(delivered['tool_calls'][0]['function']['name'], 'deliver_result')
        for decision in ('review', 'deliver'):
            with self.subTest(decision=decision), self.assertRaises(ValueError):
                self.compile(message(decision=decision, actions=[]), candidate=complete)

    def test_partial_exit_needs_actual_obstacle_and_does_not_create_a_review(self):
        draft = {'summary': 'Useful partial work', 'artifacts': ['note.md'],
            'unresolved': ['Independent checking remains.']}
        with self.assertRaises(ValueError):
            self.compile(message(decision='blocked', actions=[], candidate=draft))
        compiled = self.compile(message(decision='blocked', actions=[], candidate=draft,
            stop_reason='The shared allowance cannot fund the remaining independent check.'))
        self.assertEqual([row['function']['name'] for row in compiled['tool_calls']], ['deliver_result'])
        self.assertEqual(len(json.loads(compiled['tool_calls'][0]['function']['arguments'])['unresolved']), 2)

    def test_actual_offer_binds_review_transition_as_well_as_nested_work(self):
        selector = WorkSelection('outcomes.v1')
        schema = selector.offer([operation('ask_user'), operation('deliver_result')])[0]['function']['parameters']
        self.assertEqual(schema['properties']['decision']['enum'], ['work', 'deliver', 'blocked'])
        self.assertEqual(schema['$defs']['WorkOperation']['properties']['tool_name']['enum'], ['ask_user'])
        with self.assertRaisesRegex(ValueError, 'not offered'):
            self.compile(message(decision='review', actions=[], candidate={
                'summary': 'Draft', 'artifacts': ['note.md']}), names=['ask_user', 'deliver_result'])
        with self.assertRaisesRegex(ValueError, 'cannot introduce another delivery'):
            self.compile(message(decision='work', actions=[fixture._action('review_result',
                summary='Hidden review', artifacts=['note.md'])]))

    def test_frozen_none_and_explicit_contracts_keep_their_distinct_old_effects(self):
        candidate = {'summary': 'Existing work', 'artifacts': ['note.md']}
        action = fixture._action('write_file', path='extra.md', content='Old selected work.')
        old = self.compile(message(remaining=[], actions=[action], candidate=candidate), policy=None)
        explicit = self.compile(message(decision='work', remaining=[], actions=[action],
            candidate=candidate), policy='explicit.v2')
        self.assertEqual([row['function']['name'] for row in old['tool_calls']], ['deliver_result'])
        self.assertEqual([row['function']['name'] for row in explicit['tool_calls']], ['write_file'])
        with self.assertRaises(ValueError):
            WorkSelection.decode(message(decision='review', actions=[], candidate=candidate), 'explicit.v2')

    def test_controlled_fixture_adapter_preserves_work_and_requires_separate_handoffs(self):
        from tests.report_review_fixtures import goal_work_decision
        actions = [fixture._action('write_file', path='note.md', content='Useful work.'),
            fixture._action('read_file', path='note.md')]
        retained = deepcopy(actions)
        choice = goal_work_decision(actions, ['Write a useful note.'], policy='outcomes.v1')[0]['arguments']
        self.assertEqual(choice['decision'], 'work')
        self.assertEqual(choice['actions'], retained)
        partial = fixture._action('deliver_result', summary='Useful partial note', artifacts=['note.md'],
            unresolved=['The remaining shared allowance cannot fund necessary checking.'])
        blocked = goal_work_decision([partial], ['Write a useful note.'], policy='outcomes.v1')[0]['arguments']
        self.assertEqual(blocked['decision'], 'blocked')
        self.assertTrue(blocked['stop_reason'])
        with self.assertRaisesRegex(ValueError, 'observe all selected work'):
            goal_work_decision([*actions, partial], ['Write a useful note.'], policy='outcomes.v1')
        self.assertEqual(actions, retained)


class OutcomeQueueTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case
        case.app = ToolApplication.create(case.root, 'Prepare a bounded note from the supplied observations.',
            model='fixture', sources={'measurements': case.inputs}, services=case.services,
            allow_execution=True, max_turns=24, max_tokens=900000, tool_contracts='on_demand')
        self.assertEqual(case.app.state['config']['work_selection_policy'], 'outcomes.v1')
        self.assertEqual(case.app.state['config']['work_lifecycle'], 'integrated.v2')
        case._reload()

    def drain(self, **kwargs):
        case = self.case
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']), **kwargs)

    def test_approved_work_review_delivery_and_local_feedback_reuse_one_queue_without_replay(self):
        case = self.case
        candidate = {'summary': 'A bounded observed note', 'artifacts': ['note.md']}
        sources = {path.name: path.read_bytes() for path in case.inputs.iterdir() if path.is_file()}
        program = ("from pathlib import Path; "
            "Path('checked.txt').write_bytes(Path('note.md').read_bytes()); "
            "p=Path('counter.txt'); p.write_text((p.read_text() if p.exists() else '')+'run\\n')")
        case.decisions.append(fixture._action('select_work', decision='work', actions=[
            fixture._action('write_file', path='note.md', content='Three supplied observations.\n'),
            fixture._action('run_process', argv=[sys.executable, '-c', program],
                inputs=['note.md'], outputs=['checked.txt', 'counter.txt'])],
            candidate=candidate))
        self.drain()
        self.assertEqual(case.app.state['pending']['kind'], 'tool')
        self.assertFalse((case.root/'workspace/counter.txt').exists())
        token = case.app.approval_token(case.app.state['pending']['call'])
        case._reload()
        self.assertEqual(case.app.approval_token(case.app.state['pending']['call']), token)
        self.drain(approved_execution=token)
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual((case.root/'workspace/counter.txt').read_text(), 'run\n')
        self.assertFalse(case.inspection_requests)
        self.assertFalse((case.root/'delivery.json').exists())
        original = (case.root/'workspace/note.md').read_bytes()
        case._reload()
        self.assertEqual(case.app._selection_context()['current_candidate']['artifacts'], ['note.md'])
        case.decisions.append(fixture._action('select_work', decision='review', actions=[], candidate=candidate))
        self.drain()
        self.assertTrue(case.inspection_requests)
        self.assertFalse((case.root/'delivery.json').exists())
        inspected = len(case.inspection_requests)
        case._reload()
        case.decisions.append(fixture._action('select_work', decision='deliver', actions=[], candidate=candidate))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), inspected)
        self.assertEqual((case.root/'workspace/note.md').read_bytes(), original)
        first_delivery = (case.root/'delivery.json').read_bytes()
        first_ledger = deepcopy(case.app.ledger.to_dict()['entries'])
        proof = case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['identity']

        # Exercise the fixture adapter through real model decisions after user
        # feedback. The original calculation and its accepted process stay put.
        feedback = 'Shorten the same note; reuse unchanged observations and do not rerun the calculation.'
        case.decisions.append(fixture._action('edit_file', path='note.md',
            expected_sha256=hashlib.sha256(original).hexdigest(),
            old_text='Three supplied observations.', new_text='Three observations.'))
        self.drain(feedback=feedback)
        self.assertEqual((case.root/'delivery.json').read_bytes(), first_delivery)
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Three observations.\n')
        self.assertEqual((case.root/'workspace/checked.txt').read_bytes(), original)
        self.assertEqual((case.root/'workspace/counter.txt').read_text(), 'run\n')
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual(len(case.inspection_requests), inspected)
        self.assertEqual(case.app.ledger.to_dict()['entries'][:len(first_ledger)], first_ledger)

        revised = {'summary': 'A shorter bounded observed note', 'artifacts': ['note.md']}
        case._reload()
        case.decisions.append(fixture._action('review_result', **revised))
        self.drain()
        self.assertGreater(len(case.inspection_requests), inspected)
        current_proof = case._last('review_result', reviewed=True)['metadata']['delivery_review']['identity']
        self.assertNotEqual(current_proof, proof)
        checked = len(case.inspection_requests)
        case._reload()
        case.decisions.append(fixture._action('deliver_result', **revised))
        self.drain()
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)
        self.assertEqual(case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['identity'],
            current_proof)
        self.assertEqual((case.root/'workspace/counter.txt').read_text(), 'run\n')
        self.assertEqual(len(case._process_receipts()), 1)
        self.assertEqual({path.name: path.read_bytes() for path in case.inputs.iterdir() if path.is_file()}, sources)
        from simple_ar.tools.delivery_versions import DeliveryVersions
        self.assertEqual([row['request_count'] for row in DeliveryVersions(case.root).versions()], [1, 2])

    def test_retained_choice_is_reinterpreted_without_rebuying_or_accepting_changed_raw_reply(self):
        case = self.case
        case.decisions.append(fixture._action('select_work', decision='work', actions=[
            fixture._action('read_file', path='measurements/conditions.txt')]))
        self.drain()
        turn = case.app.state['turns'][-1]
        store = case.app.session.store
        parent = Path(turn['reply']).parent
        raw = store.read_json(str(parent/'work_choice.json'))
        binding = store.read_json(str(parent/'decision_budget.json'))
        before = case.app.ledger.to_dict()
        compiled = case.app.operations.selection.interpret(raw, binding, case.app.state['requests'])
        self.assertEqual(compiled, store.read_json(turn['reply']))
        altered = deepcopy(raw)
        altered['content'] = 'A changed claim'
        with self.assertRaisesRegex(ValueError, 'changed after admission'):
            case.app.operations.selection.interpret(altered, binding, case.app.state['requests'])
        self.assertEqual(case.app.ledger.to_dict(), before)
        self.assertEqual(hashlib.sha256(json.dumps(raw, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest(), binding['work_choice_sha256'])

    def test_low_capacity_keeps_select_work_wrapper_with_only_funded_inner_transitions(self):
        case = self.case
        case.app.ledger.limits['llm_requests'] = 1
        client = case.app._client()
        context = type('Context', (), {'store': case.app.session.store,
            'attempt': type('Attempt', (), {'attempt_id': 'input-only'})()})()
        _, value, _, _ = case.app._professional_model_input(context, client)
        self.assertTrue(value.closing)
        self.assertEqual([row['function']['name'] for row in value.tools], ['select_work'])
        projection = json.loads(value.messages[1]['content'])['work_selection']
        names = {row['function']['name'] for row in projection['operations']}
        self.assertEqual(names, {'ask_user', 'deliver_result'})
        schema = value.tools[0]['function']['parameters']
        self.assertNotIn('review', schema['properties']['decision']['enum'])
        case.send.assert_not_called()
