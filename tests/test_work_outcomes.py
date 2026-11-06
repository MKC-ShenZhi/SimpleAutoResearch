"""Outcome choices reuse the ordinary queue and preserve frozen selection contracts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
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


class OutcomeQueueTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        case = self.case
        case.app = ToolApplication.create(case.root, 'Prepare a bounded note from the supplied observations.',
            model='fixture', sources={'measurements': case.inputs}, services=case.services,
            max_turns=12, max_tokens=900000, tool_contracts='on_demand')
        # A private method-comparison candidate, not a migration of saved work.
        case.app.state['config'].update(work_selection_policy='outcomes.v1', work_lifecycle='integrated.v2')
        case.app._save()
        case._reload()

    def drain(self):
        case = self.case
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))

    def test_work_then_candidate_check_then_same_version_delivery_reuses_opinion(self):
        case = self.case
        candidate = {'summary': 'A bounded observed note', 'artifacts': ['note.md']}
        case.decisions.append(fixture._action('select_work', decision='work', actions=[
            fixture._action('write_file', path='note.md', content='Three supplied observations.\n')],
            candidate=candidate))
        self.drain()
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
