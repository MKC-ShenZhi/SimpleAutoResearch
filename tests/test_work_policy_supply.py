"""Unrelated material supply preserves frozen work interpreters and receipts.

Historical wire fixtures are loaded explicitly. Provider transport is controlled;
the original queue, tool receipts, supply, ledger and recovery remain production.
"""
from copy import deepcopy
from pathlib import Path
import unittest

from simple_ar.tools.specs import GOAL_WORK_METHOD
from tests import test_professional_work_loop as fixture
from tests.native_history_fixture import load_prepared_operation_history


class WorkPolicySupplyTests(unittest.TestCase):
    def check_policy(self, policy):
        case = fixture.OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        goal = 'Write a bounded note from the supplied observations.'
        config = {
            'model': 'fixture', 'model_connection': case.services.llm_client.connection_binding(),
            'sources': {'measurements': case.inputs}, 'professional_operations': 'work.v12',
            'source_generation_protocol': None, 'source_decision_protocol': None,
            'context_protocol': 'working.v4', 'memory_protocol': 'journal.v1',
            'context_checkpoint_policy': 'economic.v3', 'work_method': GOAL_WORK_METHOD,
            'publication_assets': 'consumed.v3', 'delivery_review_protocol': 'task.v5',
            'tool_contracts': 'full', 'max_turns': 30,
        }
        if policy is not None:
            config['work_selection_policy'] = policy
        if policy == 'outcomes.v1':
            config['work_lifecycle'] = 'integrated.v2'
        elif policy == 'native.v3':
            config['work_lifecycle'] = 'integrated.v1'
        case.app = load_prepared_operation_history(case.root, goal, config=config,
            limits={'llm_requests': 30, 'total_tokens': 900000}, services=case.services)
        action = fixture._action('write_file', path='note.md', content='Three supplied observations.\n')
        if policy == 'native.v3':
            case.decisions.append(action)
        else:
            choice = {'actions': [action], 'remaining': [{
                'request_index': 0, 'quote': goal, 'missing_outcome': 'The requested note.'}]}
            if policy is not None:
                choice['decision'] = 'work'
            case.decisions.append(fixture._action('select_work', **choice))
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))
        case._ok('write_file')
        turn = case.app.state['turns'][-1]
        reply = case.app.session.store.read_json(turn['reply'])
        queued_call = reply['tool_calls'][0]
        directory = Path(turn['reply']).parent
        retained = {name: (case.root / directory / name).read_bytes()
            for name in ('native_result.json', 'decision_budget.json', 'work_choice.json')
            if (case.root / directory / name).is_file()}
        self.assertIn('decision_budget.json', retained)
        self.assertEqual('work_choice.json' in retained, policy != 'native.v3')
        frozen = {key: deepcopy(case.app.state['config'][key])
            for key in ('work_selection_policy', 'work_lifecycle') if key in case.app.state['config']}
        ledger = deepcopy(case.app.ledger.to_dict())
        self.assertTrue(ledger['entries'], 'Retain already-accounted offline decision cost, not an empty ledger')
        calls = case.send.call_count
        case.app.supply(supply_id='extra-material', reason='Expose another authorized material alias',
            changes={'sources': {'extra': case.inputs}})
        case._reload()
        self.assertEqual({key: case.app.state['config'][key]
            for key in ('work_selection_policy', 'work_lifecycle') if key in case.app.state['config']}, frozen)
        self.assertEqual(case.app.ledger.to_dict(), ledger)
        self.assertEqual(case.send.call_count, calls)
        self.assertEqual({name: (case.root / directory / name).read_bytes() for name in retained}, retained)
        # Replay admission only, never physical execution: the saved original
        # decision must still compile to its original accepted operation.
        self.assertIsNone(case.app._offered_tool_refusal(queued_call))
        self.assertEqual(case.app.workspace.resolve('note.md').read_text(encoding='utf-8'),
            'Three supplied observations.\n')
        self.assertEqual(case.app.ledger.to_dict(), ledger)
        self.assertEqual(case.send.call_count, calls)

    def test_absent_selection_and_lifecycle_remain_absent_after_material_supply(self):
        self.check_policy(None)

    def test_explicit_selection_keeps_its_saved_choice_interpreter(self):
        self.check_policy('explicit.v2')

    def test_native_selection_retains_native_receipt_and_lifecycle(self):
        self.check_policy('native.v3')

    def test_outcome_selection_retains_choice_receipt_and_lifecycle(self):
        self.check_policy('outcomes.v1')
