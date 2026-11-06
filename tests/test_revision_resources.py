"""Inspection returns on the shared ledger before spending the caller's repair work."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from simple_ar.app.professional_conversation import ConversationInput, ProfessionalConversation
from simple_ar.core.budget import BudgetExceededError, BudgetLedger
from simple_ar.tools.inspection import InspectionToolGateway


def tool(name):
    return {'type': 'function', 'function': {'name': name,
        'parameters': {'type': 'object', 'properties': {}}}}


class RevisionResourceTests(unittest.TestCase):
    def conversation(self, requests=8, tokens=100000):
        ledger = BudgetLedger({'llm_requests': requests, 'total_tokens': tokens,
            'process_invocations': 2})
        client = SimpleNamespace(_budget_ledger=ledger, effective_output_limit=lambda: 100)
        return ProfessionalConversation(client), ledger, client

    def stages(self, conversation):
        decision = conversation.request_resources(ConversationInput(
            [{'role': 'user', 'content': 'Resolve the observed defect or disclose unfinished work.'}],
            [tool('edit_file'), tool('deliver_result')], {}))
        return {'revision': {**decision, 'process_invocations': 1},
            'submission': decision, 'reinspection': decision, 'fallback': decision}

    def test_stage_forecast_preserves_dimensions_and_does_not_create_accounting(self):
        conversation, ledger, _ = self.conversation()
        before = ledger.to_dict()
        stages = self.stages(conversation)
        original = deepcopy(stages)
        floor = conversation.resource_floor(stages)
        self.assertEqual(floor['llm_requests'], 4)
        self.assertEqual(floor['process_invocations'], 1)
        self.assertEqual(floor['total_tokens'], sum(row['total_tokens'] for row in stages.values()))
        self.assertEqual(stages, original)
        self.assertEqual(ledger.to_dict(), before)
        for unknown in (None, True, -1, float('nan'), float('inf')):
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                conversation.resource_floor({'unknown': {'total_tokens': unknown}})

    def test_owner_round_returns_an_opinion_before_spending_the_revision_cycle(self):
        conversation, ledger, client = self.conversation()
        owner = InspectionToolGateway.__new__(InspectionToolGateway)
        terminal = tool('record_inspection')
        owner._working_messages = Mock(side_effect=lambda saved, request, messages, offered, client, **kwargs: (
            [{'role': 'user', 'content': 'Actual observed work with an established defect.'}],
            {'compaction_needed': False}))
        floor = conversation.resource_floor(self.stages(conversation))
        recorded = []

        def respond(messages, *, tools, **kwargs):
            identity = 'physical-' + str(len(recorded))
            resources = conversation.request_resources(ConversationInput(messages, tools, {}))
            ledger.reserve(identity, resources)
            ledger.settle(identity, resources, actual_source='test-transport')
            recorded.append([row['function']['name'] for row in tools])
            return {'role': 'assistant', 'content': None}

        client.chat_turn = respond
        with ledger.protect_remaining(floor):
            while True:
                current, value = owner._prepare_conversation_round(
                    {'identity': 'review', 'execution_protocol': 'shared.v2'}, None,
                    [], [tool('read_file')], terminal, {'role': 'user', 'content': 'Return actual scope.'},
                    client, len(recorded), [100000])
                current.call(value, label='inspection', call_id='inspection', additional=True)
                if value.closing:
                    break
            self.assertEqual(recorded[-1], ['record_inspection'])
            self.assertEqual(ledger.remaining('llm_requests'), 0)
            with self.assertRaises(BudgetExceededError):
                ledger.reserve('unfunded-further-inspection', {'llm_requests': 1})
        self.assertEqual(ledger.remaining('llm_requests'), floor['llm_requests'])
        self.assertGreaterEqual(ledger.remaining('total_tokens'), floor['total_tokens'])
        self.assertEqual(ledger.remaining('process_invocations'), 2)

    def test_insufficient_opinion_capacity_does_not_spend_the_caller_floor(self):
        conversation, ledger, client = self.conversation(requests=4)
        floor = conversation.resource_floor(self.stages(conversation))
        client.chat_turn = lambda *args, **kwargs: ledger.reserve('not-affordable', {'llm_requests': 1})
        with ledger.protect_remaining(floor):
            value = conversation.prepare(lambda closing: ConversationInput([], [tool('record_inspection')], {}),
                followup=conversation.request_resources, exit_first=True, additional=True)
            self.assertTrue(value.closing)
            with self.assertRaises(BudgetExceededError):
                conversation.call(value, label='inspection', call_id='inspection', additional=True)
        self.assertEqual(ledger.remaining('llm_requests'), 4)
        self.assertEqual(ledger.entries, ())

    def test_new_inspection_instructions_preserve_the_old_saved_contract(self):
        owner = InspectionToolGateway.__new__(InspectionToolGateway)
        saved = {'identity': 'review', 'inspection_material_policy': 'operations.v3',
            'delivery_context': {'schema_version': 'delivery_review.v3'}}
        old = owner._system({**saved, 'execution_protocol': 'shared.v1'})
        new = owner._system({**saved, 'execution_protocol': 'shared.v2'})
        self.assertTrue(new.startswith(old))
        self.assertNotIn('caller protects', old)
        self.assertIn('return the evidence and finding promptly', new)
        self.assertIn('passing conclusion still needs all applicable necessary checks', new)


class RevisionInspectionFlowTests(unittest.TestCase):
    def test_candidate_repair_and_final_acceptance_spend_the_reserved_stages_once(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action
        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Prepare a bounded note from the supplied observations.',
            model='fixture', sources={'measurements': case.inputs}, services=case.services,
            max_turns=8, max_tokens=900000, tool_contracts='on_demand')
        self.assertEqual(case.app.state['config']['work_lifecycle'], 'integrated.v3')
        case._write('note.md', 'The population is characterized.\n')
        source = (case.inputs/'conditions.txt').read_bytes()

        def review(view):
            old_claim = (case.root/'workspace/note.md').read_text().startswith('The population')
            offered = {row['function']['name'] for row in case.inspection_requests[-1]['tools']}
            if old_claim and offered != {'record_inspection'}:
                return [_action('read_file', path='measurements/conditions.txt')]
            opinion = case._review_complete_targets(view)
            self.assertIsInstance(opinion, dict)
            if old_claim:
                opinion.update(conclusion='incomplete', investigations=[{
                    'claim': 'These supplied observations characterize the population.',
                    'scope': 'The actual current note and supplied conditions.', 'basis': 'observed',
                    'necessary': True, 'method': 'Read the note and original evidence boundary.',
                    'reasoning': 'The supplied conditions do not support a population inference.',
                    'evidence': opinion['evidence'], 'outcome': 'contradicted'}],
                    findings=[{'severity': 'required', 'message': 'Keep the note within the supplied observations.',
                        'artifacts': [str(case.root/'workspace/note.md')], 'investigation': 0}],
                    limitations=['Other necessary investigation remains; return the established defect for repair.'])
            else:
                opinion['reassessed_prior'] = [row['id'] for row in view.get('prior_required', [])]
            return opinion

        case.review = review
        claim = {'summary': 'Bounded observation note', 'artifacts': ['note.md']}
        case._submit(_action('review_result', **claim))
        case._not_delivered()
        assessment = case._last('review_result', reviewed=True)['metadata']['delivery_review']['result']['metadata']['delivery_assessment']
        self.assertEqual(assessment['verdict'], 'needs_revision')
        self.assertGreaterEqual(case.app.ledger.remaining('llm_requests'), 4)
        case._reload()
        case.decisions.append(_action('edit_file', path='note.md',
            old_text='The population is characterized.', new_text='Only supplied observations are characterized.'))
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))
        remaining = case.app.ledger.remaining('llm_requests')
        self.assertLess(remaining, 4)  # Another candidate cycle cannot fit now.
        case._reload()
        case.decisions.append(_action('deliver_result', **claim))
        case._run(should_stop=lambda: False)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Only supplied observations are characterized.\n')
        self.assertEqual((case.inputs/'conditions.txt').read_bytes(), source)
        self.assertFalse(case._process_receipts())
        self.assertLessEqual(case.send.call_count, 8)
        self.assertEqual(case.app.ledger.limits['llm_requests'], 8)

    def test_incomplete_review_returns_capacity_for_an_actual_revision_and_truthful_exit(self):
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action
        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case._create(delivery_protocol=None, current_method=True, selection_policy='native.v3',
            professional_contract='work.v12', goal='Prepare a bounded note from the supplied observations.')
        case._write('note.md', 'The population is characterized.\n')
        owner = case.app.operations.inspection
        owner.execution_protocol, owner.work_lifecycle = 'shared.v2', 'integrated.v3'
        case.app.ledger.limits['llm_requests'] = 8
        original_ledger = deepcopy(case.app.ledger.to_dict()['entries'])
        conversation = ProfessionalConversation(case.app._client())
        decision = conversation.request_resources(ConversationInput(
            case.app._messages(client=case.app._client()), case.app.schemas, {}))
        stages = {'revision': decision, 'submission': decision,
            'reinspection': decision, 'fallback': decision}
        floor = conversation.resource_floor(stages)

        def constrained_review(view):
            offered = {row['function']['name'] for row in case.inspection_requests[-1]['tools']}
            if offered != {'record_inspection'}:
                return [_action('read_file', path='measurements/conditions.txt')]
            return {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
                'limitations': ['Necessary investigation remains; return for production within shared resources.']}

        case.review = constrained_review
        with case.app.ledger.protect_remaining(floor):
            case._submit(_action('review_result', summary='Bounded note candidate', artifacts=['note.md']))
        case._not_delivered()
        self.assertGreaterEqual(case.app.ledger.remaining('llm_requests'), floor['llm_requests'])
        self.assertGreaterEqual(case.app.ledger.remaining('total_tokens'), floor['total_tokens'])
        inspected = case._last('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(inspected['result']['metadata']['delivery_assessment']['verdict'], 'incomplete')
        saved = owner.store.read_json(owner._path(inspected['identity'], 'input'))
        self.assertEqual(saved['execution_protocol'], 'shared.v2')
        offer = owner.store.read_json(owner._path(inspected['identity'], 'sources-0'))['offer']
        self.assertIn('resource_handoff', json.loads(offer['messages'][1]['content']))
        before = case.app.ledger.to_dict()
        case._reload()
        restored_owner = case.app.operations.inspection
        recovered = restored_owner.call('inspect_artifacts', saved['arguments'], saved['identity'],
            delivery_context=saved.get('delivery_context'), prior_required=saved.get('prior_required'))
        self.assertTrue(recovered.ok, recovered.error)
        self.assertEqual(case.app.ledger.to_dict(), before)

        case.decisions.append(_action('edit_file', path='note.md',
            old_text='The population is characterized.', new_text='Only supplied observations are characterized.'))
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))
        self.assertEqual((case.root/'workspace/note.md').read_text(), 'Only supplied observations are characterized.\n')
        case._submit(_action('deliver_result', summary='Useful revised observations', artifacts=['note.md'],
            unresolved=['Independent acceptance remains unfinished within the remaining resources.']))
        self.assertEqual(case.app.session.manifest.status, 'partial')
        self.assertEqual(case.app.ledger.to_dict()['entries'][:len(original_ledger)], original_ledger)
