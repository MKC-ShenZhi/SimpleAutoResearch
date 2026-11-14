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
        historical = self.owner.review_delivery(claim, 'historical', may_start=True, candidate=True)
        self.assertNotIn('purpose', historical['binding'])

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

        lifecycle, feedback = self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 1)
        self.assertIn('Independent check required', feedback['issues'])
        self.assertIn('Independent check required', [row['message'] for row in lifecycle['conditions']])
        first_binding = deepcopy(self.review.issues.call_args.args[1])

        self.workspace.resolve('draft.md').write_text('# Changed finding\n', encoding='utf-8')
        self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 2)
        self.assertNotEqual(first_binding['artifacts'], self.review.issues.call_args.args[1]['artifacts'])

        self.observations.append({'tool_name': 'deliver_result', 'status': 'error',
            'data': {**claim, 'summary': 'A different proposal'},
            'record': 'attempts/rejected/native_result.json', 'current_request': True})
        self.review.issues.reset_mock()
        self.owner.working_guidance(None, include_lifecycle=True)
        self.assertEqual(self.review.issues.call_count, 2)
        self.assertNotEqual(self.review.issues.call_args_list[0].args[1], self.review.issues.call_args_list[1].args[1])


class IntegratedCandidateFlowTests(unittest.TestCase):
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
        case._submit(_action('review_result', **final))
        ready = case._ok('review_result', reviewed=True)['metadata']['delivery_review']
        self.assertNotIn('purpose', ready['binding'])
        inspected = len(case.inspection_requests)
        case._submit(_action('deliver_result', **final))
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
