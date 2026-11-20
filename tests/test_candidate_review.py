"""Candidate investigation cannot acquire final-delivery authority."""
from copy import deepcopy
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from simple_ar.core.budget import BudgetLedger
from simple_ar.report.delivery import Delivery
from simple_ar.tools.delivery_review import DeliveryReview, prepare_delivery_review
from simple_ar.tools.workspace import DeliveryRequest, WorkspaceToolGateway
from tests.test_delivery_review import ControlledInspector


class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = WorkspaceToolGateway(self.root, {}, BudgetLedger())
        self.workspace.resolve('draft.md').write_text('# Finding\n\nBounded result.\n', encoding='utf-8')
        self.requests = ['Revise the supplied analysis and deliver editable Markdown and matching PDF.']
        self.observations = []
        self.inspector = ControlledInspector(self.root, self.workspace, lambda: self.requests)
        self.review = DeliveryReview(workspace=self.workspace, inspector=self.inspector,
            observations=lambda: self.observations, protocol='task.v5')
        self.publication = SimpleNamespace(check_requested_delivery=Mock(
            side_effect=ValueError('No current manuscript/PDF publication binding')))
        self.owner = Delivery(workspace=self.workspace,
            gateway=SimpleNamespace(delivery_issues=lambda *args, **kwargs: [],
                registry=SimpleNamespace(get=lambda _: None)),
            publication=self.publication, review_owner=self.review,
            config={'delivery_review_protocol':'task.v5',
                'tool_completions':{'review_result':{'kind':'review'}}},
            observations=lambda: self.observations, user_requests=lambda: self.requests)
        self.claim = {'summary':'Current revised argument awaiting PDF', 'artifacts':['draft.md'],
            'report':{'status':'candidate','manuscript':'draft.md','formats':['markdown','pdf']},
            'unresolved':['PDF publication is not finished.']}

    def test_incomplete_candidate_is_inspected_without_publication_and_never_certifies_delivery(self):
        proof = self.owner.review_delivery(self.claim, 'early', may_start=True, candidate=True)
        self.assertFalse(proof.get('error'), proof)
        self.publication.check_requested_delivery.assert_not_called()
        binding = self.inspector.calls[-1][3]
        self.assertEqual(binding['purpose'], 'candidate')
        self.assertEqual(binding['claim']['unresolved'], self.claim['unresolved'])
        self.assertEqual(binding['requests'], self.requests)
        self.assertFalse((self.root/'delivery.json').exists())
        self.observations.append({'tool_name':'review_result','status':'ok',
            'data':deepcopy(self.claim),'metadata':{}})
        pending = self.owner.lifecycle_requirements()
        self.assertEqual(pending['prior_unresolved'], self.claim['unresolved'])
        self.assertNotIn('unfinished_work', {row['kind'] for row in pending['conditions']})
        self.assertIn('export_report', pending['next_tools'])
        self.assertIn('publication', {row['kind'] for row in pending['conditions']})
        self.observations.append({'tool_name':'review_result','status':'ok',
            'data':deepcopy(self.claim),'metadata':{'delivery_review':proof}})
        recovered = self.owner.review_delivery(self.claim, 'early', may_start=False, candidate=True)
        self.assertEqual(recovered, proof)
        self.assertEqual(len(self.inspector.calls), 1)
        complete = {**self.claim,'unresolved':[]}
        request = DeliveryRequest.model_validate(complete)
        resolved = self.workspace.resolve_delivery(request)
        final_binding = prepare_delivery_review(self.workspace,request,resolved['artifacts'],
            lambda:self.requests,protocol='task.v5')
        self.assertNotEqual(binding, final_binding)
        self.assertIsNone(self.review.review(final_binding,'final',may_start=False))
        with self.assertRaisesRegex(ValueError,'publication binding'):
            self.owner.validate_actual(complete,self.observations)
        # A historical complete-delivery invocation keeps its original gate.
        with self.assertRaisesRegex(ValueError,'publication binding'):
            self.owner.review_delivery(complete,'legacy',may_start=True)
        self.assertEqual(len(self.inspector.calls),1)

    def test_scoped_candidate_does_not_become_a_final_claim_when_no_gap_is_listed(self):
        claim = {'summary': 'Check the revised argument', 'artifacts': ['draft.md']}
        proof = self.owner.review_delivery(claim, 'scoped', may_start=True,
            candidate=True, review_scope='candidate')
        self.assertEqual(proof['binding']['purpose'], 'candidate')
        row = {'tool_name': 'review_result', 'status': 'ok', 'record': 'attempts/scoped/native_result.json',
            'data': claim, 'metadata': {'delivery_review': proof}, 'current_request': True}
        self.observations.append(row)
        self.assertIsNone(self.owner.feedback(self.observations, None))
        guidance = self.owner.lifecycle_requirements(observations=self.observations)
        self.assertTrue(guidance['review_followup'])
        self.assertNotIn('independent_review', [item['kind'] for item in guidance['conditions']])

    def test_explicit_delivery_scope_and_historical_claim_keep_final_review(self):
        claim = {'summary': 'Complete explanation', 'artifacts': ['draft.md']}
        explicit = self.owner.review_delivery(claim, 'explicit', may_start=True,
            candidate=True, review_scope='delivery')
        self.assertNotIn('purpose', explicit['binding'])
        with patch.object(self.owner, 'validate_actual', wraps=self.owner.validate_actual) as validate:
            historical = self.owner.review_delivery(claim, 'historical', may_start=True, candidate=True)
        validate.assert_called_once()
        self.assertNotIn('purpose', historical['binding'])

        self.owner.config['delivery_review_selection'] = 'model.v3'
        current = self.owner.review_delivery(claim, 'current', may_start=True, candidate=True)
        self.assertEqual(current['binding']['purpose'], 'candidate')
        final = self.owner.review_delivery(claim, 'final', may_start=True,
            candidate=True, review_scope='delivery')
        self.assertNotIn('purpose', final['binding'])

    def test_candidate_versions_are_rechecked_and_new_investigation_does_not_replay_old_work(self):
        first = self.owner.review_delivery(self.claim,'first',may_start=True,candidate=True)
        self.observations.append({'tool_name':'review_result','status':'ok',
            'metadata':{'delivery_review':first}})
        old_input = (self.root/'tools/inspection/first/input.json').read_bytes()
        self.workspace.resolve('draft.md').write_text('# Revised finding\n',encoding='utf-8')
        second = self.owner.review_delivery(self.claim,'second',may_start=True,candidate=True)
        self.assertFalse(second.get('error'),second)
        self.assertEqual(len(self.inspector.calls),2)
        self.assertEqual((self.root/'tools/inspection/first/input.json').read_bytes(),old_input)
        self.assertNotEqual(first['binding']['artifacts'],second['binding']['artifacts'])

    def test_complete_report_claim_surfaces_known_publication_gap_before_review(self):
        ready = {**self.claim, 'unresolved': [], 'report': {**self.claim['report'], 'status': 'canonical'}}
        with self.assertRaisesRegex(ValueError, 'declared ready.*publication binding'):
            self.owner.review_delivery(ready, 'not-ready', may_start=True, candidate=True)
        self.assertEqual(self.inspector.calls, [])
        with patch.object(self.review, 'review', return_value={'identity': 'saved'}) as recover:
            self.assertEqual(self.owner.review_delivery(ready, 'saved', may_start=False, candidate=True),
                             {'identity': 'saved'})
            recover.assert_called_once()
        payload = {'metadata': {}}
        self.owner.bind_result(DeliveryRequest.model_validate(ready), payload, 'new',
            current_outputs=(), current_result={}, retain=lambda: None,
            review_capacity=nullcontext, may_start=True, candidate=True)
        self.assertIn('publication binding', payload['metadata']['delivery_review_error'])
        self.assertEqual(self.inspector.calls, [])

        # An honestly incomplete candidate can still receive useful feedback.
        partial = {**ready, 'unresolved': ['PDF publication is not finished.']}
        proof = self.owner.review_delivery(partial, 'partial', may_start=True, candidate=True)
        self.assertFalse(proof.get('error'), proof)
        self.assertEqual(self.inspector.calls[-1][3]['purpose'], 'candidate')

    def test_lifecycle_is_derived_guidance_not_a_second_acceptance_state(self):
        status = self.owner.lifecycle_requirements()
        self.assertEqual(status['conditions'][0]['kind'],'candidate_submission')
        self.assertEqual(status['minimum_resources']['llm_requests'],1)
        self.assertIn('review_result',status['next_tools'])
        self.assertFalse((self.root/'delivery.json').exists())

    def test_optional_review_has_room_after_work_and_feedback_without_becoming_mandatory(self):
        self.owner.config.update(delivery_review_selection='model.v1',
            candidate_review_protocol='candidate.v1')
        self.owner.gateway.registry.get = lambda name: (
            SimpleNamespace(permission_level='write_patch') if name == 'edit_file' else None)
        self.assertEqual(self.owner.lifecycle_requirements()['minimum_resources']['llm_requests'], 0)
        self.observations.append({'tool_name': 'edit_file', 'status': 'ok', 'current_request': True})
        self.assertEqual(self.owner.lifecycle_requirements()['minimum_resources']['llm_requests'], 1)
        with patch.object(self.owner, 'observations', side_effect=AssertionError('Saved v1 needs no history scan')):
            self.assertFalse(self.owner.requires_review(DeliveryRequest.model_validate({
                'summary': 'A small note', 'artifacts': ['draft.md'], 'independent_review': False})))

        self.owner.config['tool_completions']['deliver_result'] = {'kind': 'delivery'}
        previous = {'summary': 'Earlier explanation', 'artifacts': ['draft.md']}
        self.observations[:] = [{'tool_name': 'deliver_result', 'status': 'ok', 'data': previous,
                                 'current_request': False}]
        self.assertEqual(self.owner.lifecycle_requirements(accepted_delivery=previous)
                         ['minimum_resources']['llm_requests'], 1)
        self.observations[0]['current_request'] = True
        self.assertEqual(self.owner.lifecycle_requirements(accepted_delivery=previous)
                         ['minimum_resources']['llm_requests'], 0)

    def test_selected_final_review_applies_to_its_work_and_current_request(self):
        self.owner.config.update(delivery_review_selection='model.v3',
            candidate_review_protocol='candidate.v1')
        direct = DeliveryRequest.model_validate({
            'summary': 'A small note', 'artifacts': ['draft.md'], 'independent_review': False})
        binding = {'artifacts': {str(self.workspace.resolve('draft.md')): 'version'}, 'purpose': 'candidate'}
        self.observations.append({'tool_name': 'review_result', 'status': 'ok',
            'metadata': {'delivery_review': {'binding': binding}}, 'current_request': True})
        self.assertFalse(self.owner.requires_review(direct))
        binding.pop('purpose')
        self.assertTrue(self.owner.requires_review(direct))
        self.workspace.resolve('other.md').write_text('Different work', encoding='utf-8')
        self.observations[-1]['arguments'] = {'review_scope': 'delivery'}
        expanded = direct.model_copy(update={'artifacts': ['draft.md', 'other.md']})
        self.assertTrue(self.owner.requires_review(expanded))
        binding['artifacts'][str(self.workspace.resolve('other.md'))] = 'other-version'
        self.assertTrue(self.owner.requires_review(direct))
        binding['artifacts'].pop(str(self.workspace.resolve('other.md')))
        other = direct.model_copy(update={'artifacts': ['other.md']})
        self.assertFalse(self.owner.requires_review(other))
        self.workspace.resolve('replacement.md').write_text('Replacement', encoding='utf-8')
        binding['artifacts'][str(self.workspace.resolve('other.md'))] = 'other-version'
        revised = direct.model_copy(update={'artifacts': ['draft.md', 'replacement.md']})
        self.assertTrue(self.owner.requires_review(revised))
        self.observations[-1]['current_request'] = False
        self.assertFalse(self.owner.requires_review(direct))

    def test_saved_v2_review_selection_keeps_its_original_request_scope(self):
        self.owner.config['delivery_review_selection'] = 'model.v2'
        self.observations.append({'tool_name': 'review_result', 'status': 'ok',
            'arguments': {'review_scope': 'delivery'}, 'current_request': True})
        self.workspace.resolve('other.md').write_text('Different work', encoding='utf-8')
        direct = DeliveryRequest.model_validate({
            'summary': 'Another result', 'artifacts': ['other.md'], 'independent_review': False})
        self.assertTrue(self.owner.requires_review(direct))
        self.observations[0]['current_request'] = False
        self.assertFalse(self.owner.requires_review(direct))

    def test_delivery_review_must_have_started_before_it_is_retained(self):
        self.owner.config.update(delivery_review_selection='model.v3',
            candidate_review_protocol='candidate.v1')
        self.owner.config['tool_completions'].update({
            'deliver_result': {'kind': 'delivery'},
            'write_file': {'kind': 'delivery', 'field': 'delivery'},
        })
        direct = DeliveryRequest.model_validate({
            'summary': 'The result', 'artifacts': ['draft.md'], 'independent_review': False})
        for name, arguments in (
                ('deliver_result', {'summary': 'The result', 'artifacts': ['draft.md']}),
                ('write_file', {'path': 'draft.md', 'delivery': {
                    'summary': 'The result', 'artifacts': ['draft.md']}})):
            with self.subTest(tool=name):
                self.observations[:] = [{'tool_name': name, 'status': 'ok',
                    'arguments': arguments, 'metadata': {'delivery_review': {
                        'binding': {'artifacts': {str(self.workspace.resolve('draft.md')): 'version'}},
                        'result': {'status': 'incomplete'}}},
                    'current_request': True}]
                self.assertTrue(self.owner.requires_review(direct))
                self.observations[0]['metadata']['delivery_review']['error'] = 'The inspection could not start'
                self.assertFalse(self.owner.requires_review(direct))
                self.observations[0]['metadata'] = {'delivery_review_error': 'Review did not complete'}
                self.assertFalse(self.owner.requires_review(direct))
                self.observations[0]['current_request'] = False
                self.assertFalse(self.owner.requires_review(direct))

    def test_caller_required_review_cannot_be_waived_after_a_changed_claim(self):
        self.owner.config.update(delivery_review_selection='model.v3', independent_review_required=True)
        original = {'summary': 'Original claim', 'artifacts': ['draft.md']}
        proof = self.owner.review_delivery(original, 'original', may_start=True,
            candidate=True, review_scope='delivery')
        self.observations.append({'tool_name': 'review_result', 'status': 'ok',
            'metadata': {'delivery_review': proof}, 'current_request': True})
        reuse = DeliveryRequest.model_validate({**original, 'independent_review': False})
        capacity = Mock(side_effect=AssertionError('A retained review needs no new reservation'))
        self.owner.bind_result(reuse, {'metadata': {}}, 'same', current_outputs=(), current_result={},
            retain=lambda: None, review_capacity=capacity, may_start=True)
        capacity.assert_not_called()
        self.assertEqual(len(self.inspector.calls), 1)
        revised = DeliveryRequest.model_validate({'summary': 'A changed claim', 'artifacts': ['draft.md'],
            'independent_review': False})
        payload = {'metadata': {}}
        self.owner.bind_result(revised, payload, 'revised', current_outputs=(), current_result={},
            retain=lambda: None, review_capacity=nullcontext, may_start=True)
        self.assertEqual(len(self.inspector.calls), 2)
        self.assertEqual(payload['metadata']['delivery_review']['identity'], 'revised')

    def test_explicit_final_review_survives_an_inspection_failure(self):
        self.owner.config.update(delivery_review_selection='model.v3',
            candidate_review_protocol='candidate.v1')
        self.owner.config['tool_completions'].update({
            'deliver_result': {'kind': 'delivery'},
            'write_file': {'kind': 'delivery', 'field': 'delivery'},
        })
        direct = DeliveryRequest.model_validate({
            'summary': 'The result', 'artifacts': ['draft.md'], 'independent_review': False})
        binding = {'artifacts': {str(self.workspace.resolve('draft.md')): 'version'}}
        for tool, arguments in (
                ('review_result', {'review_scope': 'delivery'}),
                ('deliver_result', {'independent_review': True}),
                ('write_file', {'delivery': {'independent_review': True}})):
            with self.subTest(tool=tool):
                self.observations[:] = [{'tool_name': tool, 'arguments': arguments,
                    'metadata': {'delivery_review': {'binding': binding, 'error': 'Review did not finish'}},
                    'current_request': True}]
                self.assertTrue(self.owner.requires_review(direct))
                self.workspace.resolve('other.md').write_text('Another result', encoding='utf-8')
                expanded = direct.model_copy(update={'artifacts': ['draft.md', 'other.md']})
                self.assertTrue(self.owner.requires_review(expanded))
                self.assertFalse(self.owner.requires_review(direct.model_copy(update={'artifacts': ['other.md']})))
                self.observations[0]['current_request'] = False
                self.assertFalse(self.owner.requires_review(direct))

        self.observations[:] = [{'tool_name': 'review_result', 'arguments': {'review_scope': 'candidate'},
            'metadata': {'delivery_review': {'binding': {**binding, 'purpose': 'candidate'},
                'error': 'Review did not finish'}}, 'current_request': True}]
        self.assertFalse(self.owner.requires_review(direct))

    def test_explicit_final_review_survives_a_preflight_failure_without_review_receipt(self):
        self.owner.config.update(delivery_review_selection='model.v3',
            candidate_review_protocol='candidate.v1')
        self.owner.config['tool_completions'].update({
            'deliver_result': {'kind': 'delivery'},
            'write_file': {'kind': 'delivery', 'field': 'delivery'},
        })
        ready = {**self.claim, 'unresolved': [],
            'report': {**self.claim['report'], 'status': 'canonical'}}
        direct = DeliveryRequest.model_validate({
            'summary': 'The result', 'artifacts': ['draft.md'], 'independent_review': False})
        for tool, arguments, candidate in (
                ('review_result', {**ready, 'review_scope': 'delivery'}, True),
                ('deliver_result', {**ready, 'independent_review': True}, False),
                ('write_file', {'delivery': {**ready, 'independent_review': True}}, False)):
            with self.subTest(tool=tool):
                self.observations.clear()
                claim = arguments['delivery'] if tool == 'write_file' else {
                    key: value for key, value in arguments.items() if key != 'review_scope'}
                payload = {'status': 'ok', 'metadata': {}}
                self.owner.bind_result(DeliveryRequest.model_validate(claim), payload, 'preflight',
                    current_outputs=(), current_result={}, retain=lambda: None,
                    review_capacity=nullcontext, may_start=True, candidate=candidate,
                    review_scope='delivery' if candidate else None)
                self.assertIn('delivery_review_error', payload['metadata'])
                self.assertNotIn('delivery_review', payload['metadata'])
                self.observations.append({'tool_name': tool, 'arguments': arguments,
                    'metadata': payload['metadata'], 'current_request': True})
                self.assertTrue(self.owner.requires_review(direct))
                self.observations[0]['current_request'] = False
                self.assertFalse(self.owner.requires_review(direct))

        # A local candidate check does not become an obligation for final delivery.
        self.observations[0].update(tool_name='review_result', current_request=True,
            arguments={'review_scope': 'candidate', 'independent_review': True})
        self.assertFalse(self.owner.requires_review(direct))

        # A requested review of a text-only result is still a review choice.
        self.observations[:] = [{'tool_name': 'deliver_result', 'current_request': True,
            'arguments': {'summary': 'Answer', 'artifacts': [], 'independent_review': True},
            'metadata': {'delivery_review_error': 'Review did not start', 'delivery_hashes': {}}}]
        text_only = DeliveryRequest.model_validate({'summary': 'Revised answer', 'artifacts': [],
            'independent_review': False})
        self.assertTrue(self.owner.requires_review(text_only))
        self.observations[0]['arguments'].pop('independent_review')
        self.assertFalse(self.owner.requires_review(text_only))

    def test_feedback_keeps_prior_delivery_without_rechecking_it_as_a_new_candidate(self):
        self.owner.config['tool_completions']['deliver_result'] = {'kind': 'delivery'}
        delivered = {'summary': 'Earlier explanation', 'artifacts': ['draft.md']}
        self.observations.append({'tool_name': 'deliver_result', 'status': 'ok', 'data': delivered,
            'record': 'attempts/delivery/native_result.json', 'current_request': False})
        self.review.issues = Mock(return_value=['Independent check required'])

        lifecycle, feedback = self.owner.working_guidance(delivered, include_lifecycle=True)
        self.review.issues.assert_not_called()
        self.assertIsNone(feedback)
        self.assertIn('previous delivery is recorded', lifecycle['conditions'][0]['message'])
        self.assertEqual(lifecycle['minimum_resources']['llm_requests'], 1)

        self.observations[0]['current_request'] = True
        completed, _ = self.owner.working_guidance(delivered, include_lifecycle=True)
        self.assertEqual(completed['minimum_resources']['llm_requests'], 0)
        self.observations[0]['current_request'] = False

        self.observations.append({'tool_name': 'deliver_result', 'status': 'ok',
            'data': {**delivered, 'summary': 'Revised explanation'},
            'record': 'attempts/revision/native_result.json', 'current_request': True})
        lifecycle, feedback = self.owner.working_guidance(delivered, include_lifecycle=True)
        self.review.issues.assert_called_once()
        self.assertIn('Independent check required', feedback['issues'])

    def test_one_working_view_shares_review_but_later_views_recheck(self):
        self.owner.config['tool_completions']['deliver_result'] = {'kind': 'delivery'}
        claim = {'summary': 'Revised explanation', 'artifacts': ['draft.md']}
        self.observations.append({'tool_name': 'deliver_result', 'status': 'ok', 'data': claim,
            'record': 'attempts/delivery/native_result.json', 'current_request': True})
        self.review.issues = Mock(return_value=['Independent check required'])
        self.review.prepare = Mock(wraps=self.review.prepare)

        lifecycle, feedback = self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 1)
        self.assertEqual(self.review.prepare.call_count, 1)
        self.assertIn('Independent check required', feedback['issues'])
        self.assertIn('Independent check required', [row['message'] for row in lifecycle['conditions']])
        first_binding = deepcopy(self.review.issues.call_args.args[1])

        self.workspace.resolve('draft.md').write_text('# Changed finding\n', encoding='utf-8')
        self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 2)
        self.assertEqual(self.review.prepare.call_count, 2)
        self.assertNotEqual(first_binding['artifacts'], self.review.issues.call_args.args[1]['artifacts'])

        self.observations.append({'tool_name': 'deliver_result', 'status': 'error',
            'data': {**claim, 'summary': 'A different proposal'},
            'record': 'attempts/rejected/native_result.json', 'current_request': True})
        self.review.issues.reset_mock()
        self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 2)
        self.assertNotEqual(self.review.issues.call_args_list[0].args[1], self.review.issues.call_args_list[1].args[1])


class IntegratedCandidateFlowTests(unittest.TestCase):
    def test_optional_review_of_one_delivery_does_not_require_review_of_another(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Complete the requested work.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full', independent_review_required=False)
        case._write('first.md', 'The first candidate.\n')
        case._write('second.md', 'A different candidate.\n')
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent investigation is unfinished.']}
        case._submit(_action('review_result', summary='First', artifacts=['first.md'], review_scope='delivery'))
        checked = len(case.inspection_requests)
        case._submit(_action('deliver_result', summary='Second', artifacts=['second.md'], independent_review=False))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)

    def test_incomplete_delivery_review_cannot_be_waived_by_a_later_delivery(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Check and deliver the explanation.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'The current explanation.\n')
        claim = {'summary': 'The explanation', 'artifacts': ['note.md']}
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent investigation is unfinished.']}
        case._submit(_action('deliver_result', **claim))
        case._not_delivered()
        checked = len(case.inspection_requests)
        self.assertEqual(checked, 1)
        case._submit(_action('deliver_result', **claim, independent_review=False))
        case._not_delivered()
        self.assertEqual(len(case.inspection_requests), checked)
        case.review = case._review_complete_targets
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        proof = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        inspector = case.app.operations.inspection
        with patch.object(inspector, '_receipt', wraps=inspector._receipt) as read_receipt:
            with inspector.presentation_scope():
                first = inspector.feedback_observation(proof['identity'], proof['result'], guidance_only=True)
                second = inspector.feedback_observation(proof['identity'], proof['result'], guidance_only=True)
                self.assertTrue(first['opinion_available'])
                self.assertEqual(first, second)
                self.assertEqual(read_receipt.call_count, 1)
            inspector.feedback_observation(proof['identity'], proof['result'], guidance_only=True)
            self.assertEqual(read_receipt.call_count, 2)  # A later decision reads its own opinion.
        checked = len(case.inspection_requests)
        case._submit(_action('deliver_result', **claim, independent_review=False))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)

    def test_supplied_contract_does_not_cancel_a_selected_final_review(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Complete the requested explanation.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'The current explanation.\n')
        claim = {'summary': 'The explanation', 'artifacts': ['note.md']}
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent investigation is unfinished.']}
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        checked = len(case.inspection_requests)
        case.app.supply(supply_id='execution-access', reason='Authorize later workspace execution',
            changes={'allow_execution': True})
        case._reload()
        self.assertEqual(case.app._work_context(['note.md'])['user_requests'],
                         ['Complete the requested explanation.'])
        case._submit(_action('deliver_result', **claim, independent_review=False))
        case._not_delivered()
        self.assertEqual(len(case.inspection_requests), checked)

        case.app.supply(supply_id='review-optional', reason='Make independent review optional',
            changes={'independent_review_required': False}, replace_existing=True)
        case._reload()
        self.assertFalse(case.app.operations.delivery.requires_review(DeliveryRequest.model_validate({
            **claim, 'independent_review': False})))

    def test_selected_final_review_cannot_be_silently_skipped_or_retried(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Independently check this note before delivery.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        self.assertEqual(case.app.state['config']['delivery_review_selection'], 'model.v3')
        case._write('note.md', 'The current explanation.\n')
        claim = {'summary': 'The explanation', 'artifacts': ['note.md']}
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent investigation is unfinished.']}
        case._submit(_action('review_result', **claim, review_scope='delivery', independent_review=True))
        checked = len(case.inspection_requests)
        case._submit(_action('deliver_result', **claim, independent_review=False))
        case._not_delivered()
        self.assertEqual(len(case.inspection_requests), checked)
        case.review = case._review_complete_targets
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        case._ok('review_result', reviewed=True)
        checked = len(case.inspection_requests)
        case._submit(_action('deliver_result', **claim, independent_review=False))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)

    def test_expanded_delivery_keeps_selected_review_without_reusing_unchecked_work(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Review and deliver the findings.', model='fixture',
            sources={}, services=case.services, max_turns=20, max_tokens=900000, max_attempts=100)
        case.review = case._review_complete_targets
        case._write('note.md', 'Original finding.\n')
        claim = {'summary': 'The findings', 'artifacts': ['note.md']}
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        case._ok('review_result', reviewed=True)
        checked = len(case.inspection_requests)
        case._write('supplement.md', 'Additional finding.\n')
        case._submit(_action('deliver_result', summary='The findings',
            artifacts=['note.md', 'supplement.md'], independent_review=False))
        self.assertGreater(len(case.inspection_requests), checked)
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())

    def test_new_task_review_cannot_be_waived_and_reuses_the_reviewed_candidate(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Independently review the note before delivery.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'A bounded explanation.\n')
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent investigation is unfinished.']}
        claim = {'summary': 'A bounded explanation', 'artifacts': ['note.md']}
        case._submit(_action('deliver_result', **claim, independent_review=False))
        self.assertEqual(len(case.inspection_requests), 1)
        case._not_delivered()
        case._submit(_action('deliver_result', **claim, independent_review=False))
        self.assertEqual(len(case.inspection_requests), 1)
        case._not_delivered()

        # A passed full-scope review of the same claim is adopted, rather than
        # starting another check when the producer repeats the false choice.
        case.review = case._review_complete_targets
        case._submit(_action('review_result', **claim, review_scope='delivery', independent_review=True))
        case._ok('review_result', reviewed=True)
        checked = len(case.inspection_requests)
        case._reload()
        case._submit(_action('deliver_result', **claim, independent_review=False))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)

    def test_review_requirement_can_be_supplied_to_the_original_session_without_resetting_ledger(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Continue the existing work and independently check it.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'Existing work remains available.\n')
        ledger = deepcopy(case.app.ledger.to_dict())
        case.app.supply(supply_id='required-review', reason='Preserve the requested independent review',
            changes={'independent_review_required': True})
        case._reload()
        self.assertEqual(case.app.ledger.to_dict(), ledger)
        self.assertEqual(case.app.operations.delivery.lifecycle_requirements()['minimum_resources']['llm_requests'], 1)
        self.assertTrue(case.app.operations.delivery.requires_review(DeliveryRequest.model_validate({
            'summary': 'Existing work', 'artifacts': ['note.md'], 'independent_review': False})))
        case._submit(_action('deliver_result', summary='Existing work', artifacts=['note.md'],
            independent_review=False, unresolved=['Independent review remains unfinished.']))
        self.assertEqual(case.app.session.manifest.status, 'partial', case.app.result_snapshot())
        self.assertEqual(case.inspection_requests, [])
        # Only a later explicit caller revision, not the producer, can remove it.
        before_revision = deepcopy(case.app.ledger.to_dict())
        case.app.supply(supply_id='review-no-longer-required', reason='Caller revised the review requirement',
            changes={'independent_review_required': False}, replace_existing=True)
        case._reload()
        self.assertEqual(case.app.ledger.to_dict(), before_revision)
        self.assertFalse(case.app.operations.delivery.requires_review(DeliveryRequest.model_validate({
            'summary': 'Existing work', 'artifacts': ['note.md'], 'independent_review': False})))

    def test_review_only_supply_reuses_prior_complete_review_without_a_new_investigation(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Independently check and deliver this note.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'The current editable explanation.\n')
        claim = {'summary': 'The checked explanation', 'artifacts': ['note.md']}
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        proof = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        checked = len(case.inspection_requests)
        before = deepcopy(case.app.ledger.to_dict())
        requests = list(case.app.state['requests'])
        case.app.supply(supply_id='required-review', reason='Retain the requested independent review',
            changes={'independent_review_required': True},
            resource_allowances={'llm_requests': 1, 'total_tokens': 50000})
        case._reload()
        self.assertEqual(case.app.ledger.to_dict()['entries'][:len(before['entries'])], before['entries'])
        self.assertEqual(case.app.ledger.remaining('llm_requests'), 1)
        self.assertEqual(case.app._professional_user_requests(), requests)
        case._submit(_action('deliver_result', reviewed_candidate=proof['identity']))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), checked)
        case.app.supply(supply_id='review-no-longer-required', reason='Caller revised the review condition',
            changes={'independent_review_required': False}, replace_existing=True)
        previous = case.app.operations.delivery.retained_prior_delivery()
        self.assertIsNotNone(previous)
        self.assertEqual([Path(path).name for path in previous['artifacts']], ['note.md'])

    def test_resource_continuation_resumes_partial_work_and_preserves_required_review(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Independently check the existing note.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, independent_review_required=True)
        case._write('note.md', 'The retained editable result.\n')
        case._submit(_action('deliver_result', summary='Note awaiting review', artifacts=['note.md'],
            unresolved=['Independent review remains unfinished.']))
        self.assertEqual(case.app.session.manifest.status, 'partial')
        requests = list(case.app.state['requests'])
        original = (case.root / 'workspace/note.md').read_bytes()
        ledger = deepcopy(case.app.ledger.to_dict())
        claim = case.app.session.store.read_json('delivery.json')
        prior = case.app.operations.delivery.retained_prior_delivery()
        self.assertEqual(prior['artifacts'], claim['artifacts'])
        self.assertEqual(prior['unresolved'], claim['unresolved'])
        guidance, _ = case.app.operations.delivery.working_guidance(claim, include_lifecycle=True)
        self.assertEqual(guidance['minimum_resources']['llm_requests'], 1)
        self.assertEqual(case.app.operations.document.work_context([])['retained_prior_delivery'], prior)

        grant = {'resource_allowances': {'llm_requests': 6, 'total_tokens': 400000},
                 'authorization_id': 'continue-partial', 'authorization_reason': 'Complete the required review'}
        # The original authorization and the exact partial are saved together;
        # a stop before reopening can recover without another grant or request.
        with patch.object(case.app.session, 'continue_with_revision', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                case.app.run(**grant, progress=lambda _: None)
        case._reload()
        saved_ledger = deepcopy(case.app.ledger.to_dict())
        self.assertEqual(saved_ledger['entries'][:len(ledger['entries'])], ledger['entries'])
        case.app.run(progress=lambda _: None, should_stop=lambda: True)
        self.assertEqual(case.app.session.manifest.status, 'paused')
        self.assertEqual(case.app.state['requests'], requests)
        self.assertEqual(case.app.ledger.to_dict(), saved_ledger)
        self.assertEqual((case.root / 'workspace/note.md').read_bytes(), original)

        def review():
            request = case.send.call_args.kwargs.get('request') or case.send.call_args.args[1]
            view = json.JSONDecoder().raw_decode(request['messages'][1]['content'])[0]
            self.assertEqual(view['retained_prior_delivery']['unresolved'], claim['unresolved'])
            return _action('review_result', summary='Checked note', artifacts=['note.md'], review_scope='delivery')

        def deliver():
            proof = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
            return _action('deliver_result', reviewed_candidate=proof['identity'])

        case.decisions.extend([review, deliver])
        case._run(should_stop=lambda: not case.decisions and case.app.state['pending'] is None
            and not any(turn['remaining'] for turn in case.app.state['turns']))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(case.app.state['requests'], requests)
        self.assertTrue(case.inspection_requests)
        self.assertIsNone(case.app.operations.delivery.retained_prior_delivery())
        before_replay = deepcopy(case.app.ledger.to_dict())
        case.app.run(**grant, progress=lambda _: None)
        self.assertEqual(case.app.session.manifest.status, 'completed')
        self.assertEqual(case.app.ledger.to_dict(), before_replay)

    def test_caller_can_spend_the_last_call_on_an_explicit_review(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Review the editable note.', model='fixture',
            sources={}, services=case.services, max_turns=10, max_tokens=900000, max_attempts=100)
        case._write('note.md', 'An editable research note.\n')
        case.app.ledger.limits['llm_requests'] = 1
        case.app.ledger.save()
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent check still needs more evidence.']}
        case._submit(_action('review_result', summary='Inspect this draft',
            artifacts=['note.md'], unresolved=['The note still needs a check.']))
        self.assertEqual(len(case.inspection_requests), 1)
        self.assertEqual(case.app.ledger.remaining('llm_requests'), 0)
        self.assertFalse((case.root / 'delivery.json').exists())

    def test_last_decision_can_spend_the_remaining_call_on_chosen_review(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Review the editable note before delivery.',
            model='fixture', sources={}, services=case.services, max_turns=10,
            max_tokens=900000, max_attempts=100, tool_contracts='full')
        case._write('note.md', 'An editable research note.\n')
        case.app.ledger.limits['llm_requests'] = 2
        case.app.ledger.save()
        case.review = lambda view: {'conclusion': 'incomplete', 'findings': [], 'evidence': [],
            'limitations': ['The independent check still needs more evidence.']}
        case.decisions.append(_action('review_result', summary='Inspect this draft',
            artifacts=['note.md'], unresolved=['The note still needs a check.']))
        case._run(should_stop=lambda: False)
        self.assertEqual(len(case.inspection_requests), 1)
        self.assertEqual(case.app.ledger.remaining('llm_requests'), 0)
        self.assertEqual(case.app.session.manifest.status, 'paused')
        self.assertFalse((case.root / 'delivery.json').exists())

    def test_review_can_observe_parent_execution_without_certifying_changed_inputs(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action, _assessments, _digest

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root,
            'Deliver the checked analysis script and explain any unfinished verification.',
            model='fixture', sources={'measurements': case.inputs}, services=case.services,
            allow_execution=True, max_turns=10, max_tokens=900000, max_attempts=100)
        case._write('analysis.py', 'print("first version ran")\n')
        case._submit(_action('run_process', argv=['python', 'analysis.py'], cwd='workspace',
            timeout_sec=10), approve=lambda call, token: True)
        process = case._ok('run_process')
        receipt = case.root / process['data']['receipt']
        original = receipt.read_bytes()
        case._submit(_action('run_process', argv=['python', '-c', 'print("unrelated")'],
            cwd='workspace', timeout_sec=10), approve=lambda call, token: True)
        script = case.root / 'workspace/analysis.py'
        case._submit(_action('write_file', path='analysis.py',
            content='print("changed version")\n', expected_sha256=_digest(script)))

        # Only a terminal opinion fits. The existing recorded execution should
        # still be inspectable without an extra model turn or new execution.
        case.app.ledger.limits['llm_requests'] = 1
        case.app.ledger.save()
        def review(view):
            offered = {tool['function']['name'] for tool in case.inspection_requests[-1]['tools']}
            self.assertEqual(offered, {'record_inspection'})
            self.assertEqual(view['executed_checks'], [])
            self.assertIn('not independent checks', view['parent_process_receipts']['scope'])
            self.assertEqual(view['parent_process_history']['indexed_executions'], 2)
            observed = next(row for row in view['evidence'] if row.get('path') == str(receipt))
            text = ''.join(window['text'] for window in observed['text_windows'])
            self.assertIn('first version ran', text)
            self.assertIn('analysis.py', text)
            return {'conclusion': 'incomplete', 'findings': [], 'evidence': [observed['subject']],
                'limitations': ['The recorded execution predates the changed script and did not declare inputs.']}

        case.review = review
        case._submit(_action('deliver_result', summary='Analysis script awaiting verification; python analysis.py was run',
            artifacts=['analysis.py']))
        result = case._ok('deliver_result', reviewed=True)
        self.assertEqual(_assessments(result)[0]['verdict'], 'incomplete')
        case._not_delivered()
        self.assertEqual(len(case.inspection_requests), 1)
        self.assertEqual(receipt.read_bytes(), original)
        self.assertFalse(case.app.ledger.over_limit())
        before = case.app.ledger.to_dict()
        case._reload()
        self.assertEqual(case.app.ledger.to_dict(), before)

    def test_inspector_can_copy_and_run_a_workspace_project_in_its_own_area(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'Review the editable analysis project.',
            model='fixture', sources={'measurements':case.inputs}, services=case.services,
            allow_execution=True, max_turns=12, max_tokens=900000, max_attempts=120)
        case._write('analysis.py', 'print("independent check")\n')
        case._write('note.md', 'This project contains an executable analysis.\n')
        original = (case.root/'workspace/analysis.py').read_bytes()
        observed = {}

        def review(view):
            messages = case.inspection_requests[-1]['messages']
            results = [json.loads(row['content']) for row in messages if row['role'] == 'tool']
            copies = [row for row in results if row.get('tool_name') == 'copy_project']
            runs = [row for row in results if row.get('tool_name') == 'run_process']
            if not copies:
                return [_action('copy_project', source='workspace', path='../outside')]
            if len(copies) == 1:
                self.assertEqual(copies[0]['status'], 'blocked')
                return [_action('copy_project', source='workspace', path='proj')]
            copy = copies[-1]
            self.assertEqual(copy['status'], 'ok', copy)
            target = Path(copy['data']['absolute_path'])
            self.assertEqual((target/'analysis.py').read_bytes(), original)
            self.assertNotIn('inspection-work', copy['data']['files'])
            observed['target'] = target
            if not runs:
                return [_action('run_process', argv=['python', 'analysis.py'], cwd=str(target),
                    inputs=['analysis.py'], timeout_sec=10)]
            self.assertEqual(runs[-1]['status'], 'ok', runs[-1])
            self.assertEqual(runs[-1]['data']['returncode'], 0)
            return {'conclusion':'incomplete', 'findings':[], 'evidence':[],
                'limitations':['The independent execution is recorded without claiming full review.']}

        case.review = review
        case._submit(_action('deliver_result', summary='Analysis project', artifacts=['note.md', 'analysis.py']),
            approve=lambda call, token: True)
        self.assertIn('target', observed)
        self.assertTrue(observed['target'].is_relative_to(case.root/'workspace/inspection-work'))
        self.assertEqual((case.root/'workspace/analysis.py').read_bytes(), original)
        self.assertFalse((case.root/'workspace/inspection-work/outside').exists())
        case._not_delivered()

    def test_partial_delivery_hands_review_and_matching_originals_to_resumed_work(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action

        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root, 'State the scope of the result in the editable note.',
            model='fixture', sources={}, services=case.services, max_turns=20,
            max_tokens=900000, max_attempts=100, tool_contracts='on_demand')
        original = 'The sample contains three observations.\nThe entire population has the same mean.\n'
        case._write('note.md', original)

        def review(view):
            reads = case._inspection_missing_reads(view, ['note.md'])
            if reads:
                return reads
            note, = case._inspection_sources(view, 'note.md')
            return {'conclusion': 'incomplete', 'limitations': [], 'findings': [],
                'evidence': [note['subject']], 'investigations': [{
                    'claim': 'The population mean equals the observed sample mean.',
                    'scope': 'The sample description in the editable note.',
                    'basis': 'observed',
                    'method': 'Compare the scope stated in the note with the sample.',
                    'reasoning': 'The sample is observed. There is no population measurement.',
                    'evidence': [note['subject']], 'outcome': 'unresolved'}]}

        case.review = review
        claim = {'summary': 'Scope of the sample result', 'artifacts': ['note.md']}
        case._submit(_action('review_result', **claim, review_scope='delivery'))
        proof = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'incomplete')
        case._submit(_action('deliver_result', **claim, unresolved=['Unresolved independent review.']))
        case._run(feedback='Use the independent opinion and correct the scope.',
            independent_review_required=True, should_stop=lambda: True)
        case._reload()
        before = case.app.ledger.to_dict()

        def view():
            return json.loads(case.app._messages()[1]['content'])

        handoff = view()
        revision = handoff['delivery_conditions']['revision']
        self.assertIn('population mean', revision['open_checks'][0]['claim'])
        self.assertTrue(any(row['observation'].get('text') == original
            and row['observation']['record'] == revision['record']
            and row['version_status'] == 'matching' for row in handoff['working_view']['observations']))

        # Feedback still guides the producer after its file changes. Only the
        # matching original can support an exact edit in the current version.
        (case.root / 'workspace/note.md').write_text('The original text was revised.\n', encoding='utf-8')
        revised = view()
        self.assertEqual(revised['delivery_conditions']['revision'], revision)
        self.assertFalse(any(row['observation'].get('text') == original
            and row['version_status'] == 'matching' for row in revised['working_view']['observations']))
        self.assertEqual(before, case.app.ledger.to_dict())

    def test_fresh_candidate_revision_final_opinion_reuse_and_feedback_reload(self):
        from simple_ar.app.tool_application import ToolApplication
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action
        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app = ToolApplication.create(case.root,
            'Explain the supplied observations in editable Markdown, preserving originals.',
            model='fixture', sources={'measurements':case.inputs}, services=case.services,
            max_turns=30, max_tokens=900000, max_attempts=100, tool_contracts='on_demand')
        case._write('note.md', 'Three supplied observations: 1, 2, and 3.\n')
        candidate = {'summary':'Candidate explanation', 'artifacts':['note.md'],
            'unresolved':['Interpretation still needs revision.']}
        case._submit(_action('review_result', **candidate))
        proof = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['binding']['purpose'], 'candidate')
        case._not_delivered()
        original_input = (case.root/'tools/inspection'/proof['identity']/'input.json').read_bytes()
        case._reload()
        case._write('final.md', 'Three supplied observations: 1, 2, and 3. No population inference.\n')
        final = {'summary':'Explanation with evidence limits', 'artifacts':['final.md']}
        case._submit(_action('review_result', **final, review_scope='delivery'))
        ready = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertNotIn('purpose', ready['binding'])
        inspected = len(case.inspection_requests)
        owner = case.app.operations.delivery
        _, guidance = owner.working_guidance(None, include_lifecycle=True)
        action = guidance['next_action']
        self.assertEqual(action['tool'], 'deliver_result')
        path = case.root/'workspace/final.md'
        original = path.read_bytes()
        path.write_bytes(original + b'Changed after independent review.\n')
        _, stale = owner.working_guidance(None, include_lifecycle=True)
        self.assertNotIn('next_action', stale)
        path.write_bytes(original)
        case._submit(_action(action['tool'], **action['arguments']))
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual(len(case.inspection_requests), inspected)
        before = case.app.ledger.to_dict()
        case._reload()
        case._run()
        self.assertEqual(case.app.ledger.to_dict(), before)
        case.decisions.extend([_action('write_file', path='feedback.md',
            content='Three supplied observations. No population inference; no sampling design.'),
            _action('deliver_result', summary='Clarified evidence limits', artifacts=['feedback.md'])])
        case._run(feedback='Clarify that there was no sampling design; retain the prior explanation.',
            should_stop=lambda: False)  # Continue through the fixture's prior caller boundary.
        self.assertEqual(case.app.session.manifest.status, 'completed', case.app.result_snapshot())
        self.assertEqual((case.root/'tools/inspection'/proof['identity']/'input.json').read_bytes(), original_input)
        self.assertTrue((case.root/'workspace/final.md').exists())
