"""Owner-derived resource planning and truthful closure share the physical ledger."""
from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.budget import BudgetExceededError, BudgetLedger


def tool(name):
    return {'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'properties': {}}}}


class WorkLifecycleTests(unittest.TestCase):
    def app(self, requests=6, tokens=100000, *, integrated=True):
        app = ToolApplication.__new__(ToolApplication)
        app.ledger = BudgetLedger({'llm_requests': requests, 'total_tokens': tokens})
        app.state = {'config': {'memory_protocol': 'journal.v1',
            'completion_policy': 'available_operations.v2', 'work_method': 'execute.v2',
            **({'work_lifecycle': 'integrated.v1'} if integrated else {})},
            'turns': [], 'pending': {'skip_memory_checkpoint': True}}
        app.session = SimpleNamespace(manifest=SimpleNamespace(
            budget=SimpleNamespace(max_attempts=100, attempts=0)))
        app.operations = SimpleNamespace(selection=None, delivery=SimpleNamespace(
            lifecycle_requirements=Mock(return_value={'conditions': [
                {'kind': 'independent_review', 'message': 'Independent investigation is unfinished.'}],
                'next_tools': ['review_result', 'deliver_result', 'export_report'],
                'minimum_resources': {'llm_requests': 1}})))
        app._request_capacity = lambda: app.ledger.remaining('llm_requests')
        app._messages = Mock(side_effect=lambda **kwargs: [
            {'role': 'system', 'content': 'Preserve actual work and disclose unfinished requirements.'},
            {'role': 'user', 'content': json.dumps({'working_context': {'compaction_needed': False}})}])
        client = SimpleNamespace(_budget_ledger=app.ledger, effective_output_limit=lambda: 100)
        return app, client

    def prepare(self, app, client):
        offered = [tool(name) for name in (
            'read_file', 'run_process', 'review_result', 'export_report', 'ask_user', 'deliver_result')]
        with patch.object(ToolApplication, 'operation_schemas', new_callable=PropertyMock,
                return_value=offered):
            return app._professional_model_input(SimpleNamespace(), client)

    def test_work_decision_leaves_owner_and_truthful_exit_on_one_ledger(self):
        app, client = self.app(4)
        before = app.ledger.to_dict()
        conversation, value, _, estimate = self.prepare(app, client)
        self.assertFalse(value.closing)
        self.assertEqual(value.reserve['llm_requests'], 2)
        self.assertEqual(estimate['llm_requests'], 1)
        self.assertGreater(value.reserve['total_tokens'], estimate['total_tokens'])
        self.assertEqual(app.ledger.to_dict(), before)

        def send(*args, **kwargs):
            app.ledger.reserve('decision', {'llm_requests': 1, 'total_tokens': conversation.tokens(value)})
            with self.assertRaises(BudgetExceededError):
                app.ledger.reserve('unrelated-professional-work', {'llm_requests': 2})
            return {'role': 'assistant', 'content': 'Submit the candidate.'}

        client.chat_turn = send
        conversation.call(value, native=True, label='work', call_id='decision')
        self.assertEqual(app.ledger.remaining('llm_requests'), 3)
        self.assertEqual(len(app.ledger.entries), 1)

    def test_decision_reuses_guidance_but_the_next_decision_sees_new_review_needs(self):
        app, client = self.app(5)
        app.state['config'].update(context_protocol='working.v4', control_protocol='operations.v1')
        app.session.store = SimpleNamespace(exists=lambda _: False)
        app.operations.inspection = SimpleNamespace(presentation_scope=nullcontext)
        app._operation_observations = Mock(return_value=[])
        app._working_memory = Mock(return_value={'notes': [], 'through_turn': 0})
        lifecycle = lambda count: {'conditions': [], 'next_tools': ['review_result', 'deliver_result'],
            'minimum_resources': {'llm_requests': count}, 'review_followup': False}
        app.operations.delivery.working_guidance = Mock(side_effect=[(lifecycle(1), None), (lifecycle(0), None)])

        _, first, _, _ = self.prepare(app, client)
        _, second, _, _ = self.prepare(app, client)
        self.assertEqual(first.reserve['llm_requests'], 2)
        self.assertEqual(second.reserve['llm_requests'], 1)
        self.assertEqual(app.operations.delivery.working_guidance.call_count, 2)
        self.assertEqual(app._operation_observations.call_count, 2)
        self.assertEqual(app._working_memory.call_count, 2)

    def test_closure_never_offers_unfunded_check_and_keeps_partial_exit(self):
        for requests, expected_reserve in ((2, 1), (1, 0)):
            with self.subTest(requests=requests):
                app, client = self.app(requests)
                before = app.ledger.to_dict()
                _, value, _, _ = self.prepare(app, client)
                self.assertTrue(value.closing)
                self.assertEqual({row['function']['name'] for row in value.tools},
                    {'ask_user', 'deliver_result'})
                self.assertEqual(value.reserve.get('llm_requests', 0), expected_reserve)
                self.assertEqual(app.ledger.to_dict(), before)

    def test_token_shortfall_also_removes_unfunded_owner_operations(self):
        app, client = self.app(4)
        _, _, _, estimate = self.prepare(app, client)
        app.ledger.limits['total_tokens'] = 2 * estimate['total_tokens']
        _, value, _, _ = self.prepare(app, client)
        self.assertTrue(value.closing)
        self.assertEqual({row['function']['name'] for row in value.tools},
            {'ask_user', 'deliver_result'})

    def test_dispatch_releases_only_the_declared_owner_floor(self):
        app, _ = self.app()
        decision = {'llm_requests': 1, 'total_tokens': 250}
        for name, expected in (('read_file', 2), ('review_result', 1), ('export_report', 1)):
            with self.subTest(name=name):
                app.state['turns'] = [{'remaining': [{'function': {'name': name}}]}]
                floor = app._lifecycle_floor(decision, dispatch=True)
                self.assertEqual(floor['llm_requests'], expected)
        app.state['config'].pop('work_lifecycle')
        self.assertEqual(app._lifecycle_floor(decision), decision)

    def test_resource_planning_respects_owner_process_minimum(self):
        ledger = BudgetLedger({'llm_requests': 10, 'total_tokens': 10000, 'process_calls': 0})
        conversation = ProfessionalConversation(SimpleNamespace(
            _budget_ledger=ledger, effective_output_limit=lambda: 100))
        value = conversation.prepare(lambda closing: ConversationInput([], [], {}),
            followup=lambda _: {'llm_requests': 1, 'total_tokens': 200, 'process_calls': 1})
        self.assertTrue(value.closing)
        self.assertEqual(ledger.entries, ())

    def test_new_ordinary_policy_is_explicit_and_old_decision_contract_stays_final(self):
        with patch.object(ToolApplication, '_configuration', return_value=(
                {'model': 'fixture'}, [], {}, 100)):
            config, _, _, _ = ToolApplication.preview_creation(Path('unused'), 'Do useful work',
                model='fixture', sources={})
        self.assertEqual(config['work_lifecycle'], 'integrated.v3')
        self.assertEqual(config['work_selection_policy'], 'native.v4')
        app, _ = self.app()
        app.state['turns'] = [{'reply': 'attempts/decision/native_result.json'}]
        app.session.store = SimpleNamespace(exists=lambda _: True, read_json=Mock())
        for contract, final in (('terminal_batch.v1', True), ('lifecycle_step.v1', False)):
            with self.subTest(contract=contract):
                app.session.store.read_json.return_value = {
                    'closing_required': True, 'closing_action_contract': contract}
                self.assertEqual(app._closing_decision(), final)

    def test_fresh_public_creation_and_reload_supply_current_owner_conditions(self):
        from tests import test_professional_work_loop as fixture
        case = fixture.OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        app = ToolApplication.create(case.root, 'Explain the supplied observations in a note.',
            model='fixture', sources={'measurements': case.inputs}, services=case.services,
            max_turns=8, max_tokens=900000, tool_contracts='on_demand')
        self.assertEqual(app.state['config']['work_lifecycle'], 'integrated.v3')
        app = ToolApplication.load(case.root, services=case.services)
        before = app.ledger.to_dict()
        context = json.loads(app._messages(client=app._client())[1]['content'])
        self.assertIn('candidate_submission',
            {row['kind'] for row in context['delivery_conditions']['conditions']})
        self.assertNotIn('work_selection', context)
        self.assertIn('review_result', {row['function']['name'] for row in app.schemas})
        self.assertNotIn('select_work', {row['function']['name'] for row in app.schemas})
        self.assertEqual(app.ledger.to_dict(), before)
        case.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
