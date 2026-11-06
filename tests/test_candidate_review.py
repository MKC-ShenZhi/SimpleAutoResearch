"""Candidate investigation cannot acquire final-delivery authority."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
            gateway=SimpleNamespace(delivery_issues=lambda *args, **kwargs: []),
            publication=self.publication, review_owner=self.review,
            config={'delivery_review_protocol':'task.v5'},
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

    def test_lifecycle_is_derived_guidance_not_a_second_acceptance_state(self):
        status = self.owner.lifecycle_requirements()
        self.assertEqual(status['conditions'][0]['kind'],'candidate_submission')
        self.assertEqual(status['minimum_resources']['llm_requests'],1)
        self.assertIn('review_result',status['next_tools'])
        self.assertFalse((self.root/'delivery.json').exists())
