"""Delivery scope/receipt contracts, not a test of model scientific judgment.

The inspector below is explicitly controlled: saved inputs/results are real JSON,
while completion/usage/integrity rejection is injected at its _cached boundary.
Workspace resolution, directory expansion, hashing and assessment checks are real.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.delivery_review import (DeliveryReview, recorded_delivery_review,
    delivery_requires_review, prepare_delivery_review)
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.workspace import DeliveryRequest, WorkspaceToolGateway


class ControlledInspector:
    def __init__(self, root, workspace, requests):
        self.store = ArtifactStore(root)
        self.workspace, self.requests = workspace, requests
        self.calls, self.cached = [], []
        self.rejected = {}
        self.expected = {}
        self.verdict = 'passed'
        self.coverage = 'complete'
        self.omit_assessment = False
        self.sources = {}

    def _path(self, identity, leaf):
        return f'tools/inspection/{identity}/{leaf}.json'

    def has_started(self, identity):
        return self.store.exists(self._path(identity, 'input'))

    def call(self, name, arguments, identity, *, delivery_context):
        if self.has_started(identity):
            raise AssertionError('Owner must use _cached, never call for started work')
        self.calls.append((name, deepcopy(arguments), identity, deepcopy(delivery_context)))
        self.store.write_json(self._path(identity, 'input'), {
            'identity': identity, 'arguments': arguments,
            'user_requests': list(self.requests()), 'delivery_context': delivery_context})
        package = delivery_context['schema_version'] == 'delivery_review.v3'
        subject = (delivery_context['artifacts'] if package
                   else self.workspace.artifact_hashes(arguments['targets']))
        findings = ([{'severity': 'required', 'message': 'The requested explanation is absent.',
                      'subjects': subject}] if self.verdict == 'needs_revision' else [])
        assessment = {'schema_version': 'delivery_assessment.v1' if package else 'artifact_assessment.v1',
            'artifacts' if package else 'subjects': subject,
            'evidence_sha256': deepcopy(self.sources), 'coverage': self.coverage,
            'verdict': self.verdict, 'findings': findings,
            'limitations': ['Controlled text-review decision; no scientific certification.']}
        result = ToolResult(tool_name=name, metadata={
            'assessments': [] if package or self.omit_assessment else [assessment],
            'inspection_binding': {'controlled_receipt': identity},
            'capability_artifacts': [{'path': 'inspection/assessments.json'}]})
        if package and not self.omit_assessment:
            result.metadata['delivery_assessment'] = assessment
        payload = result.model_dump(mode='json')
        self.store.write_json(self._path(identity, 'result'), payload)
        self.expected[identity] = deepcopy(payload)
        return result

    def _cached(self, identity):
        self.cached.append(identity)
        if identity in self.rejected:
            raise ValueError(self.rejected[identity])
        result = self.store.read_json(self._path(identity, 'result'))
        if result != self.expected[identity]:
            raise ValueError('Retained result tampered; no replay')
        assessments = list(result['metadata']['assessments'])
        if 'delivery_assessment' in result['metadata']:
            assessments.append(result['metadata']['delivery_assessment'])
        for assessment in assessments:
            versions = {**assessment.get('subjects', assessment.get('artifacts', {})),
                        **assessment['evidence_sha256']}
            if self.workspace.artifact_hashes(versions) != versions:
                raise ValueError('Inspection target/evidence version changed; no replay')
        return ToolResult.model_validate(result)

    def feedback_from_observation(self, observed):
        from simple_ar.tools.inspection import InspectionToolGateway
        return InspectionToolGateway.feedback_from_observation(self, observed)

    def feedback_observation(self, identity, claimed, *, expected_arguments=None):
        # Feedback verifies the original receipt, not the applicability of its
        # old file versions to a revision. Acceptance still uses _cached.
        result = self.store.read_json(self._path(identity, 'result'))
        if result != self.expected[identity] or claimed != result:
            raise ValueError('Feedback differs from actual receipt')
        saved = self.store.read_json(self._path(identity, 'input'))
        if expected_arguments is not None and saved['arguments'] != expected_arguments:
            raise ValueError('Feedback differs from accepted arguments')
        return {**saved, 'result': result, 'opinion_available': True,
                'reviewed_subjects': self.workspace.artifact_hashes(saved['arguments']['targets'])}


class DeliveryReviewTests(unittest.TestCase):

    def test_recorded_opinion_exposes_limitations_only_for_the_bound_delivery(self):
        root = self.root
        path = str(root / 'workspace' / 'report.md')
        digest = 'a' * 64
        delivery = {'summary': 'Summary', 'artifacts': ['workspace/report.md'], 'unresolved': []}
        item = {'id': 'call', 'result': 'attempts/native-tool-1/native_result.json',
            'review_result': 'attempts/native-action-1/native_result.json'}
        state = {'pending': None, 'requests': ['Summarize'], 'config': {},
            'turns': [{'reply': 'reply.json', 'tools': [item], 'remaining': [], 'request_count': 1}]}
        manifest = {'status': 'completed', 'session_id': 'recorded-task'}
        binding = {'schema_version': 'delivery_review.v3', 'artifacts': {path: digest},
            'claim': {'summary': 'Summary', 'artifacts': [path], 'unresolved': []},
            'requests': ['Summarize']}
        pending = ToolResult(tool_name='inspect_artifacts', data={'inspection_pending': 'native-tool-1'}).model_dump(mode='json')
        parent = {'tool_name': 'deliver_result', 'status': 'ok', 'data': delivery,
            'metadata': {'delivery_hashes': {'workspace/report.md': digest},
                'delivery_review': {'identity': 'native-tool-1', 'binding': binding, 'result': pending}}}
        records = {'reply.json': {'tool_calls': [{'id': 'call', 'function': {'name': 'deliver_result'}}]},
            item['result']: parent, 'delivery.json': delivery, 'session_manifest.json': manifest,
            'actions.json': state}
        reader = SimpleNamespace(root=root, state=state, manifest=manifest,
            record=records.__getitem__, resolve=lambda locator: root / locator)
        assessment = {'schema_version': 'delivery_assessment.v1', 'artifacts': {path: digest},
            'coverage': 'complete', 'verdict': 'passed', 'findings': [], 'evidence_sha256': {},
            'limitations': ['The source data was unavailable.']}
        receipt = ToolResult(tool_name='inspect_artifacts', metadata={'delivery_assessment': assessment}).model_dump(mode='json')
        with patch('simple_ar.tools.professional_outcomes.accepted_review_journal',
                return_value={'record': item['review_result'], 'receipt': receipt}):
            opinion = recorded_delivery_review(reader)
            self.assertEqual(opinion['limitations'], assessment['limitations'])
            self.assertIn('not proof', opinion['scope'])

            # A later delivery may reuse the same owner-bound opinion. Its
            # visible source is still the original accepted opinion action.
            current = {'id':'call-2', 'result':'attempts/native-tool-2/native_result.json'}
            current_parent = deepcopy(parent)
            current_parent['metadata']['delivery_review']['result'] = deepcopy(receipt)
            records[current['result']] = current_parent
            records['reply-2.json'] = {'tool_calls':[{'id':'call-2',
                'function':{'name':'deliver_result'}}]}
            state['turns'].append({'reply':'reply-2.json', 'tools':[current],
                'remaining':[], 'request_count':1})
            with patch('simple_ar.tools.professional_outcomes.accepted_native_result') as accepted:
                reused = recorded_delivery_review(reader)
                self.assertEqual(reused['record'], item['review_result'])
                accepted.assert_called_once()
                current_parent['metadata']['delivery_review']['result']['data']['invented'] = True
                with self.assertRaisesRegex(ValueError, 'no matching accepted opinion'):
                    recorded_delivery_review(reader)
            state['turns'].pop()

            receipt['metadata']['delivery_assessment']['artifacts'][path] = 'b' * 64
            with self.assertRaisesRegex(ValueError, 'scope differs'):
                recorded_delivery_review(reader)
            state['requests'].append('A later request')
            self.assertIsNone(recorded_delivery_review(reader))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = WorkspaceToolGateway(self.root, {}, BudgetLedger())
        bundle = self.workspace.workspace / 'bundle'
        bundle.mkdir()
        (bundle / 'manuscript.md').write_text('# 工作记录\n\n整理本次交付的工作说明。\n', encoding='utf-8')
        (bundle / 'appendix.txt').write_text('补充说明。\n', encoding='utf-8')
        self.requests = ['请提供中文 Markdown 工作说明及补充材料。']
        self.observations = []
        self.inspector = ControlledInspector(self.root, self.workspace, lambda: self.requests)
        self.owner = DeliveryReview(workspace=self.workspace, inspector=self.inspector,
            observations=lambda: self.observations,
            protocol='task.v3')
        self.delivery = {'summary': '交付说明', 'artifacts': ['workspace/bundle'],
            'report': {'status': 'canonical', 'manuscript': 'workspace/bundle/manuscript.md',
                       'formats': ['markdown']}}

    def observe(self, proof):
        observed = {'tool_name': 'deliver_result', 'status': 'ok',
                    'metadata': {'delivery_review': deepcopy(proof)}}
        self.observations.append(observed)
        return observed

    def binding(self, delivery, protocol='task.v3', *, current_outputs=()):
        request = DeliveryRequest.model_validate(delivery)
        if not delivery_requires_review(request, protocol):
            return None
        resolved = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        return prepare_delivery_review(self.workspace, request, resolved['artifacts'],
            lambda: self.requests, protocol=protocol)

    def issues(self, observations, delivery):
        return self.owner.issues(observations, self.binding(delivery))

    def review(self, identity='parent-1', may_start=True):
        return self.owner.review(self.binding(self.delivery), identity, may_start=may_start)

    def test_completion_claim_and_inventory_are_bound_without_report_or_surrogate_target(self):
        self.delivery.pop('report')
        (self.workspace.workspace / 'bundle' / '核查代码.py').write_text('value = 2\n', encoding='utf-8')
        proof = self.review()
        self.observe(proof)
        binding = proof['binding']
        self.assertEqual(binding['schema_version'], 'delivery_review.v3')
        self.assertEqual(binding['requests'], self.requests)
        expected_claim = DeliveryRequest.model_validate(self.delivery).model_dump(mode='json', exclude_none=True)
        expected_claim['artifacts'] = sorted(binding['artifacts'])
        self.assertEqual(binding['claim'], expected_claim)
        self.assertEqual(set(binding['artifacts']), {
            str(self.workspace.resolve('workspace/bundle/manuscript.md')),
            str(self.workspace.resolve('workspace/bundle/appendix.txt')),
            str(self.workspace.resolve('workspace/bundle/核查代码.py'))})
        self.assertNotIn('manuscript', binding)
        self.assertEqual(binding['artifacts'], self.workspace.artifact_hashes(binding['artifacts']))
        _, arguments, identity, context = self.inspector.calls[0]
        self.assertEqual(identity, 'parent-1')
        self.assertEqual(arguments['targets'], [])
        self.assertEqual(arguments['evidence'], [])
        self.assertEqual(context, binding)
        self.assertEqual(binding['claim']['summary'], self.delivery['summary'])
        self.assertIn('delivery_assessment', proof['result']['metadata'])
        self.assertEqual(proof['result']['metadata']['assessments'], [])
        self.assertEqual(self.issues(self.observations, self.delivery), [])
        self.assertEqual(self.review(), proof)
        self.assertEqual(len(self.inspector.calls), 1)
        reused = self.review('parent-2')
        self.assertEqual(reused, proof)
        self.assertEqual(len(self.inspector.calls), 1)
        self.assertEqual(self.inspector.cached[-1], 'parent-1')
        self.delivery['artifacts'] = ['bundle/核查代码.py', 'workspace/bundle/appendix.txt',
                                      'bundle/manuscript.md', 'bundle/appendix.txt']
        self.assertEqual(self.review('aliases'), proof)
        self.assertEqual(self.issues(self.observations, self.delivery), [])
        self.assertEqual(len(self.inspector.calls), 1)
        feedback = self.owner.feedback_observation(proof)
        self.assertEqual(feedback['result'], proof['result'])
        self.assertEqual(feedback['reviewed_inventory'], binding['artifacts'])
        self.assertEqual(feedback['reviewed_subjects'], {})
        self.delivery['summary'] = '改变完成主张，应重新绑定'
        self.assertTrue(self.issues(self.observations, self.delivery))
        # Mutating the callback list cannot mutate a previously returned binding.
        self.requests.append('请解释补充材料的适用范围。')
        self.assertEqual(len(binding['requests']), 1)
        # A summary-only answer is still a bound completion claim, not a
        # requirement to manufacture a file or a way to skip the review.
        self.delivery = {'summary': '直接回答本次问题。', 'artifacts': []}
        self.inspector.verdict = 'needs_revision'
        summary = self.review('summary-only')
        self.assertEqual(summary['binding']['artifacts'], {})
        self.observe(summary)
        self.assertTrue(self.issues(self.observations, self.delivery))
        self.assertEqual(summary['result']['metadata']['delivery_assessment']['findings'][0]['subjects'], {})
        self.inspector.verdict = 'passed'
        self.delivery['summary'] = '按原问题修正后的直接回答。'
        self.observe(self.review('corrected-answer'))
        self.assertEqual(self.issues(self.observations, self.delivery), [])

        # Just-produced records may be bound before registration. The resulting
        # opinion does not itself grant delivery eligibility on a later check.
        produced = self.workspace.root / 'attempts' / 'producer' / 'output.txt'
        produced.parent.mkdir(parents=True)
        produced.write_text('Actual producer output', encoding='utf-8')
        self.workspace.mounts['records'] = self.workspace.root / 'attempts'
        candidate = {'summary': 'Produced output', 'artifacts': ['records/producer/output.txt']}
        with self.assertRaisesRegex(ValueError, 'unregistered attempt records'):
            self.binding(candidate)
        bound = self.binding(candidate, current_outputs=(produced,))
        self.assertEqual(bound['artifacts'], self.workspace.artifact_hashes([str(produced)]))
        self.owner.review(bound, 'registered-output-review', may_start=True)
        with self.assertRaisesRegex(ValueError, 'unregistered attempt records'):
            self.binding(candidate)

    def test_local_pass_or_unbound_legacy_review_cannot_prove_delivery(self):
        proof = self.review()
        local = deepcopy(proof['result'])
        self.assertTrue(self.issues([local], self.delivery))
        # A cached local file pass is still not a package opinion, even when
        # the inspector input happens to contain the entire delivery binding.
        path = self.inspector._path('parent-1', 'result')
        forged = deepcopy(proof['result'])
        package = forged['metadata'].pop('delivery_assessment')
        forged['metadata']['assessments'] = [{**{key: value for key, value in package.items()
            if key not in {'schema_version', 'artifacts'}}, 'schema_version': 'artifact_assessment.v1',
            'subjects': package['artifacts']}]
        self.inspector.store.write_json(path, forged)
        self.inspector.expected['parent-1'] = deepcopy(forged)
        self.assertTrue(self.issues([{'metadata': {'delivery_review': {
            **proof, 'result': forged}}}], self.delivery))
        self.inspector.store.write_json(path, proof['result'])
        self.inspector.expected['parent-1'] = deepcopy(proof['result'])
        # A well-formed package pass for a subset cannot certify this bundle.
        subset = deepcopy(proof['result'])
        subset['metadata']['delivery_assessment']['artifacts'].pop(next(iter(package['artifacts'])))
        self.inspector.store.write_json(path, subset)
        self.inspector.expected['parent-1'] = deepcopy(subset)
        self.assertTrue(self.issues([{'metadata': {'delivery_review': {
            **proof, 'result': subset}}}], self.delivery))
        self.inspector.store.write_json(path, proof['result'])
        self.inspector.expected['parent-1'] = deepcopy(proof['result'])
        invalid = deepcopy(proof['result'])
        invalid['metadata']['delivery_assessment'].update(verdict='needs_revision', findings=[{
            'severity': 'required', 'message': 'An affected path must belong to the bound inventory.',
            'subjects': {str(self.workspace.workspace/'unbound.txt'): 'f'*64}}])
        self.inspector.store.write_json(path, invalid)
        self.inspector.expected['parent-1'] = deepcopy(invalid)
        self.assertIn('error', self.review(may_start=False))
        self.inspector.store.write_json(path, proof['result'])
        self.inspector.expected['parent-1'] = deepcopy(proof['result'])
        saved = self.inspector.store.read_json(self.inspector._path('parent-1', 'input'))
        saved.pop('delivery_context')
        self.inspector.store.write_json(self.inspector._path('parent-1', 'input'), saved)
        self.observe(proof)
        self.assertTrue(self.issues(self.observations, self.delivery))
        rejected = self.review(may_start=False)
        self.assertIn('error', rejected)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_complete_input_scope_does_not_upgrade_or_borrow_historical_review(self):
        self.delivery['report']['formats'] = ['tex', 'markdown']
        histories = []
        for protocol in ('task.v1', 'task.v2'):
            owner = DeliveryReview(self.workspace, self.inspector,
                observations=lambda: self.observations, protocol=protocol)
            proof = owner.review(self.binding(self.delivery, protocol), protocol, may_start=True)
            expected = {'schema_version': protocol.replace('task', 'delivery_review'),
                'requests': list(self.requests),
                'manuscript': str(self.workspace.workspace / 'bundle' / 'manuscript.md'),
                'formats': ['markdown', 'tex'],
                'authorized_roots': {'workspace': str(self.workspace.workspace)},
                'artifacts': self.workspace.artifact_hashes(sorted([
                    str(self.workspace.workspace / 'bundle' / 'appendix.txt'),
                    str(self.workspace.workspace / 'bundle' / 'manuscript.md')]))}
            # Saved receipt equality includes wire field order and normalization.
            self.assertEqual(json.dumps(proof['binding']), json.dumps(expected))
            self.assertEqual(self.inspector.calls[-1][1]['targets'], [proof['binding']['manuscript']])
            self.observe(proof)
            histories.append((owner, proof))
        self.assertTrue(self.issues(self.observations, self.delivery))
        proof = self.review('current')
        self.assertEqual(proof['binding']['schema_version'], 'delivery_review.v3')
        self.assertEqual(len(self.inspector.calls), 3)
        for owner, historical in histories:
            self.assertEqual(owner.review(self.binding(self.delivery, owner.protocol), historical['identity'], may_start=False), historical)
            self.assertEqual(self.inspector.store.read_json(self.inspector._path(historical['identity'], 'input'))
                ['delivery_context'], historical['binding'])
        self.observe(proof)
        self.assertFalse(self.issues(self.observations,self.delivery))
        self.delivery['artifacts'] = ['bundle/appendix.txt', 'workspace/bundle/manuscript.md']
        self.delivery['report']['manuscript'] = 'bundle/manuscript.md'
        self.delivery['report']['formats'] = ['markdown', 'tex', 'markdown']
        self.assertEqual(self.issues(self.observations, self.delivery), [])
        self.assertEqual(self.review('cached'),proof)
        self.assertEqual(len(self.inspector.calls),3)

    def test_all_bundle_and_requirement_changes_invalidate_proof(self):
        for change in ('report-omitted', 'feedback', 'appendix', 'manuscript', 'new-file', 'claim', 'roots'):
            with self.subTest(change=change):
                proof = self.review('before-' + change)
                observed = self.observe(proof)
                if change == 'report-omitted':
                    self.delivery.pop('report')
                elif change == 'feedback':
                    self.requests.append('补充适用范围。')
                elif change == 'claim':
                    self.delivery['summary'] = '不同完成主张'
                elif change == 'roots':
                    self.workspace.mounts['extra'] = self.workspace.workspace
                elif change == 'new-file':
                    self.workspace.resolve('workspace/bundle/new.txt').write_text('new', encoding='utf-8')
                else:
                    filename = 'appendix.txt' if change == 'appendix' else 'manuscript.md'
                    self.workspace.resolve('workspace/bundle/' + filename).write_text('changed', encoding='utf-8')
                self.assertTrue(self.issues([observed], self.delivery))
                fresh = self.review('after-' + change)
                self.assertNotIn('error', fresh)
                self.assertNotEqual(fresh['binding'], proof['binding'])

    def test_recovery_is_cached_only_and_unknown_or_tampered_never_replays(self):
        self.assertIsNone(self.review(may_start=False))
        self.assertEqual(self.inspector.calls, [])
        proof = self.review()
        self.assertEqual(self.review(may_start=False), proof)
        for reason in ('No completed inspection response; no replay',
                       'Inspection provider usage is pending or unknown; no replay',
                       'Retained result tampered; no replay'):
            with self.subTest(reason=reason):
                self.inspector.rejected['parent-1'] = reason
                for may_start in (False, True):
                    rejected = self.review(may_start=may_start)
                    self.assertEqual(rejected['error'], reason)
                    self.assertEqual(rejected['result'], proof['result'])
                    self.assertTrue(self.issues([self.observe(rejected)], self.delivery))
                self.assertEqual(len(self.inspector.calls), 1)

        # A retained review cannot authorize a path after its mount is withdrawn.
        mount = self.workspace.mounts.pop('workspace')
        try:
            with self.assertRaisesRegex(ValueError, 'authorized mounts'):
                self.review(may_start=False)
            self.assertEqual(len(self.inspector.calls), 1)
        finally:
            self.workspace.mounts['workspace'] = mount

    def test_saved_context_and_outer_result_are_independently_checked(self):
        proof = self.review()
        for field in ('identity', 'binding', 'result', 'saved-context', 'saved-requests'):
            with self.subTest(field=field):
                forged = deepcopy(proof)
                path = self.inspector._path('parent-1', 'input')
                original = self.inspector.store.read_json(path)
                if field.startswith('saved-'):
                    saved = deepcopy(original)
                    saved['delivery_context' if field == 'saved-context' else 'user_requests'] = {}
                    self.inspector.store.write_json(path, saved)
                elif field == 'result':
                    forged['result']['metadata']['delivery_assessment']['limitations'] = []
                else:
                    forged[field] = {} if field == 'binding' else 'not-the-parent'
                self.assertTrue(self.issues([{'metadata': {'delivery_review': forged}}], self.delivery))
                self.inspector.store.write_json(path, original)

    def test_started_without_result_and_actual_result_tampering_are_diagnostic(self):
        self.inspector.store.write_json(self.inspector._path('unfinished', 'input'),
            {'identity': 'unfinished'})
        for may_start in (False, True):
            rejected = self.review('unfinished', may_start=may_start)
            self.assertIn('error', rejected)
            self.assertEqual(rejected['result']['status'], 'blocked')
        self.assertEqual(self.inspector.calls, [])
        proof = self.review()
        path = self.inspector._path('parent-1', 'result')
        tampered = deepcopy(proof['result'])
        tampered['metadata']['delivery_assessment']['limitations'] = []
        self.inspector.store.write_json(path, tampered)
        rejected = self.review(may_start=False)
        self.assertIn('tampered', rejected['error'])
        self.assertTrue(self.issues([{'metadata': {'delivery_review': proof}}], self.delivery))
        self.assertEqual(len(self.inspector.calls), 1)

    def test_fresh_failed_review_preserves_negative_metadata(self):
        self.inspector.verdict = 'needs_revision'
        self.inspector.rejected['parent-1'] = 'Inspection provider usage is unknown; no replay'
        rejected = self.review()
        self.assertIn('unknown', rejected['error'])
        metadata = rejected['result']['metadata']
        self.assertEqual(metadata['delivery_assessment']['verdict'], 'needs_revision')
        self.assertIn('inspection_binding', metadata)
        self.assertIn('capability_artifacts', metadata)
        self.assertTrue(self.issues([{'metadata': {'delivery_review': rejected}}], self.delivery))
        self.review(may_start=False)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_unsuccessful_review_keeps_execution_diagnostic_without_a_final_opinion_or_replay(self):
        from unittest.mock import patch

        original_call = self.inspector.call
        cause = 'Inspection working view cannot fit its model input target'
        self.inspector.verdict = 'needs_revision'
        for status in ('error', 'blocked'):
            with self.subTest(status=status):
                identity = 'failed-' + status
                def fail(name, arguments, key, *, delivery_context):
                    result = original_call(name, arguments, key, delivery_context=delivery_context)
                    result.status, result.error = status, cause
                    result.metadata['usage'] = [
                        {'status':'settled', 'actual':{'llm_requests':1, 'total_tokens':140}},
                        {'status':'settled', 'actual':{'llm_requests':1, 'total_tokens':160}}]
                    payload = result.model_dump(mode='json')
                    self.inspector.store.write_json(self.inspector._path(key, 'result'), payload)
                    self.inspector.expected[key] = deepcopy(payload)
                    return result

                # A missing final opinion must not make the parent call the
                # acceptance checker and replace the actual local failure.
                self.inspector.rejected[identity] = 'No completed inspection response; provider state is unknown. No replay'
                with patch.object(self.inspector, 'call', side_effect=fail):
                    proof = self.review(identity)
                self.assertEqual(proof['error'], cause)
                self.assertEqual(proof['result'], self.inspector.expected[identity])
                self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'needs_revision')
                observed = self.observe(proof)
                calls = len(self.inspector.calls)
                paths = [self.inspector._path(identity, leaf) for leaf in ('input', 'result')]
                original = {path:self.inspector.store.resolve(path).read_bytes() for path in paths}
                ledger = self.workspace.ledger.to_dict()
                # Recreate the parent as on load. A fresh explicit identity may
                # be considered separately; recovery never replays this one.
                self.owner = DeliveryReview(self.workspace, self.inspector,
                    observations=lambda:self.observations, protocol='task.v3')
                for may_start in (False, True):
                    self.assertEqual(self.review(identity, may_start=may_start), proof)
                self.assertEqual(self.issues([observed], self.delivery), ['Delivery review failed: ' + cause])
                self.assertNotIn(identity, self.inspector.cached)
                self.assertEqual(len(self.inspector.calls), calls)
                self.assertEqual(self.workspace.ledger.to_dict(), ledger)
                self.assertEqual({path:self.inspector.store.resolve(path).read_bytes() for path in paths}, original)

    def test_required_partial_incomplete_and_missing_assessments_cannot_pass(self):
        for verdict, coverage, missing in (
                ('needs_revision', 'complete', False), ('incomplete', 'complete', False),
                ('incomplete', 'partial', False), ('passed', 'complete', True)):
            with self.subTest(verdict=verdict, coverage=coverage, missing=missing):
                self.inspector.verdict, self.inspector.coverage = verdict, coverage
                self.inspector.omit_assessment = missing
                proof = self.review(f'review-{verdict}-{coverage}-{missing}')
                # Completed negative opinions remain real results, not thrown-away errors.
                if missing:
                    self.assertIn('error', proof)
                else:
                    self.assertNotIn('error', proof)
                observed = {'metadata': {'delivery_review': proof}}
                issues = self.issues([observed], self.delivery)
                self.assertTrue(issues)
                if verdict == 'needs_revision':
                    self.assertIn('requested explanation is absent', ' '.join(issues))

    def test_changed_source_rejects_same_identity_but_allows_fresh_review(self):
        source = self.workspace.workspace / 'source.txt'
        source.write_text('original source', encoding='utf-8')
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        proof = self.review()
        self.observe(proof)
        source.write_text('changed source', encoding='utf-8')
        self.assertTrue(self.issues(self.observations, self.delivery))
        self.assertIn('error', self.review())
        self.assertEqual(len(self.inspector.calls), 1)
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        fresh = self.review('parent-2')
        self.assertNotIn('error', fresh)
        self.assertEqual(fresh['identity'], 'parent-2')
        self.assertEqual(len(self.inspector.calls), 2)

    def test_new_delivery_can_reassess_an_unfinished_or_negative_opinion(self):
        for verdict in ('needs_revision', 'incomplete'):
            with self.subTest(verdict=verdict):
                self.observations.clear()
                self.inspector.verdict = verdict
                prior = self.review('prior-' + verdict)
                self.observe(prior)
                self.workspace.resolve('workspace/bundle/manuscript.md').write_text(
                    'A revised completion claim: ' + verdict, encoding='utf-8')
                feedback = self.owner.feedback_observation(prior)
                self.assertTrue(feedback['opinion_available'])
                self.assertEqual(feedback['reviewed_inventory'], prior['binding']['artifacts'])
                self.assertEqual(feedback['result']['metadata']['delivery_assessment']['verdict'], verdict)
                self.assertTrue(self.issues(self.observations, self.delivery))
                self.assertIsNone(self.review('next-' + verdict, may_start=False))
                calls = len(self.inspector.calls)
                self.inspector.verdict = 'passed'
                current = self.review('next-' + verdict)
                self.assertEqual(len(self.inspector.calls), calls + 1)
                self.assertNotEqual(current['identity'], prior['identity'])
                self.observe(current)
                self.assertEqual(self.issues(self.observations, self.delivery), [])

    def test_actual_delivery_owner_checks_production_before_starting_review(self):
        from simple_ar.report.delivery import Delivery
        owner = Delivery(workspace=self.workspace,
            gateway=SimpleNamespace(delivery_issues=lambda *args, **kwargs: []),
            publication=SimpleNamespace(check_requested_delivery=lambda *args, **kwargs: None,
                conversion_inputs=lambda *args: set()),
            review_owner=self.owner, config={'delivery_review_protocol': 'task.v3',
                'delivery_files': ['required-output']},
            observations=lambda: self.observations, user_requests=lambda: self.requests)
        with self.assertRaisesRegex(ValueError, 'missing required workspace outputs'):
            owner.review_delivery(self.delivery, 'not-started', may_start=True)
        self.assertEqual(self.inspector.calls, [])
        self.assertFalse(self.inspector.has_started('not-started'))

        with patch.dict(owner.config, {'report_requirement': {}}):
            with self.assertRaisesRegex(ValueError, 'explicit report claim'):
                owner.validate_actual({'summary': 'No report claim', 'artifacts': []}, [])
        self.assertEqual(self.inspector.calls, [])

        # A partial exit retains usable files; it cannot invent missing files.
        partial = {**self.delivery, 'unresolved': ['Required output is unfinished.']}
        resolved = owner.validate_actual(partial, [])
        self.assertEqual(len(resolved['artifacts']), 2)
        self.assertEqual(resolved['unresolved'], partial['unresolved'])
        with self.assertRaisesRegex(ValueError, 'missing workspace file'):
            owner.validate_actual({**partial, 'artifacts': ['absent']}, [])

    def test_reviewed_candidate_reuses_only_the_unchanged_accepted_claim(self):
        from simple_ar.report.delivery import Delivery
        from simple_ar.tools.workspace import ReviewedDeliveryRequest

        proof = self.review()
        self.observe(proof)
        owner = Delivery(workspace=self.workspace,
            gateway=SimpleNamespace(delivery_issues=lambda *args, **kwargs: []),
            publication=SimpleNamespace(check_requested_delivery=lambda *args, **kwargs: None,
                conversion_inputs=lambda *args: set()),
            review_owner=self.owner, config={'delivery_review_protocol': 'task.v3'},
            observations=lambda: self.observations, user_requests=lambda: self.requests)
        reference = {'reviewed_candidate': 'parent-1'}
        claim = owner.resolve_request(reference, self.observations)
        self.assertEqual(claim.summary, self.delivery['summary'])
        self.assertEqual(owner.issues(self.observations, reference), [])
        self.assertEqual(len(self.inspector.calls), 1)
        with self.assertRaises(ValueError):
            ReviewedDeliveryRequest.model_validate({**reference, 'summary': 'A new claim'})

        self.requests.append('A new request')
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            owner.resolve_request(reference, self.observations)
        self.requests.pop()
        (self.workspace.workspace / 'bundle' / 'appendix.txt').write_text('Changed', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'no longer matches'):
            owner.resolve_request(reference, self.observations)

    def test_actual_delivery_owner_binds_registered_current_outputs_and_rechecks_versions(self):
        from simple_ar.report.delivery import Delivery
        owner = Delivery(workspace=self.workspace,
            gateway=SimpleNamespace(delivery_issues=lambda *args, **kwargs: []),
            publication=SimpleNamespace(check_requested_delivery=lambda *args, **kwargs: None,
                conversion_inputs=lambda *args: set()),
            review_owner=self.owner, config={'delivery_review_protocol': 'task.v3'},
            observations=lambda: self.observations, user_requests=lambda: self.requests)
        output = self.root / 'attempts' / 'producer' / 'manuscript'
        output.parent.mkdir(parents=True)
        self.workspace.mounts['records'] = output.parent.parent
        output.write_text('Editable current result.', encoding='utf-8')
        delivery = {'summary': 'Current producer result', 'artifacts': [str(output)],
            'report': {'status': 'canonical', 'manuscript': str(output)}}
        with self.assertRaisesRegex(ValueError, 'registered domain output'):
            owner.validate_actual(delivery, [])
        resolved = owner.validate_actual(delivery, [], current_outputs=(output,))
        self.assertEqual(resolved['artifacts'], [str(output)])
        proof = owner.review_delivery(delivery, 'current-output', may_start=True,
            current_outputs=(output,))
        self.observe(proof)
        self.assertEqual(owner.validate_actual(delivery, self.observations,
            current_outputs=(output,), include_inspection=True), resolved)
        output.write_text('Changed after the independent opinion.', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'independent|review'):
            owner.validate_actual(delivery, self.observations,
                current_outputs=(output,), include_inspection=True)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_partial_skips_review_and_legacy_plain_scope_is_unchanged(self):
        partial = {**self.delivery, 'unresolved': ['unfinished']}
        self.assertIsNone(self.owner.review(self.binding(partial), 'partial', may_start=True))
        self.assertEqual(self.issues([], partial), [])
        self.assertEqual(self.inspector.calls, [])
        self.inspector.verdict = 'needs_revision'
        prior = self.review('known-defect')
        self.observe(prior)
        partial['unresolved'].append('The requested explanation is absent.')
        self.assertIsNone(self.owner.review(self.binding(partial), 'partial-after-review', may_start=True))
        self.assertEqual(self.issues(self.observations, partial), [])
        self.assertEqual(partial['unresolved'], ['unfinished', 'The requested explanation is absent.'])
        feedback = self.owner.feedback_observation(prior)
        self.assertEqual(feedback['result']['metadata']['delivery_assessment']['verdict'], 'needs_revision')
        for protocol in ('task.v1', 'task.v2'):
            owner = DeliveryReview(self.workspace, self.inspector, protocol=protocol)
            for report, unresolved in ((None, []), (self.delivery['report'], ['unfinished']),
                    ({'status': 'candidate'}, []), ({'status': 'canonical'}, [])):
                with self.subTest(protocol=protocol, report=report, unresolved=unresolved):
                    delivery = {**self.delivery, 'report': report, 'unresolved': unresolved}
                    self.assertIsNone(owner.review(self.binding(delivery, protocol), 'unused', may_start=True))
                    self.assertEqual(owner.issues([], self.binding(delivery, protocol)), [])
        self.assertEqual(len(self.inspector.calls), 1)


class FindingsInspector(ControlledInspector):
    """Receipt-backed offline inspector for the explicitly enabled contract."""
    def __init__(self, *args):
        super().__init__(*args)
        self.reassess = False
        self.reply_ids = None
        self.claim_finding = False

    def call(self, name, arguments, identity, *, delivery_context, prior_required=None):
        result = super().call(name, arguments, identity, delivery_context=delivery_context)
        saved = self.store.read_json(self._path(identity, 'input'))
        if prior_required is not None:
            saved['prior_required'] = deepcopy(prior_required)
        self.store.write_json(self._path(identity, 'input'), saved)
        assessment = result.metadata['delivery_assessment']
        if self.claim_finding:
            assessment['findings'].append({'severity': 'required',
                'message': 'The completion claim needs rechecking.', 'subjects': {}})
            assessment['verdict'] = 'needs_revision'
        if self.reply_ids is not None or self.reassess:
            assessment['reassessed_prior'] = (list(self.reply_ids) if self.reply_ids is not None
                else [row['id'] for row in prior_required or []])
        payload = result.model_dump(mode='json')
        self.store.write_json(self._path(identity, 'result'), payload)
        self.expected[identity] = deepcopy(payload)
        return result

    def feedback_observation(self, identity, claimed, *, expected_arguments=None):
        if identity in self.rejected:
            raise ValueError(self.rejected[identity])
        # Intact original receipts remain readable after their source changes.
        result = self.store.read_json(self._path(identity, 'result'))
        if result != self.expected[identity] or claimed != result:
            raise ValueError('Feedback differs from actual receipt')
        saved = self.store.read_json(self._path(identity, 'input'))
        if expected_arguments is not None and saved['arguments'] != expected_arguments:
            raise ValueError('Feedback differs from accepted arguments')
        subjects = {path: digest for row in result['metadata'].get('assessments', [])
                    for path, digest in row['subjects'].items()}
        return {**saved, 'result': deepcopy(result), 'opinion_available': True,
                'reviewed_subjects': subjects}


class DeliveryFindingContractTests(unittest.TestCase):
    binding = DeliveryReviewTests.binding
    review = DeliveryReviewTests.review
    issues = DeliveryReviewTests.issues

    def setUp(self):
        DeliveryReviewTests.setUp(self)
        self.workspace.mounts['records'] = self.root / 'attempts'
        self.inspector.store.write_json('session_manifest.json', {'session_id': 'controlled-session'})
        self.inspector = FindingsInspector(self.root, self.workspace, lambda: self.requests)
        self.owner = DeliveryReview(self.workspace, self.inspector, protocol='task.v3',
            observations=lambda: self.observations, track_findings=True)

    def observe(self, proof):
        observed = {'tool_name': 'deliver_result', 'status': 'ok',
            'record': f'attempts/{proof["identity"]}/native_result.json',
            'metadata': {'delivery_review': deepcopy(proof)}}
        self.inspector.store.write_json(observed['record'],
            {key: value for key, value in observed.items() if key != 'record'})
        self.observations.append(observed)
        return observed

    def local(self, identity='local-1', *, path='bundle/manuscript.md', required=True,
              reassessed=(), evidence=None, coverage='complete'):
        subjects = self.workspace.artifact_hashes([path])
        findings = ([{'severity': 'required', 'message': 'Check the original sample scope.',
                      'subjects': subjects}] if required else [])
        assessment = {'schema_version': 'artifact_assessment.v2', 'review_id': identity + ':target-1',
            'subjects': subjects, 'coverage': coverage, 'verdict': 'needs_revision' if required else 'passed',
            'findings': findings, 'reassessed_prior': list(reassessed),
            'evidence_sha256': evidence or {}, 'limitations': []}
        arguments = {'goal': 'Check this claim', 'targets': [path], 'evidence': []}
        raw = ToolResult(tool_name='inspect_artifacts', metadata={'assessments': [assessment],
            'inspection_binding': {'assessment_path': f'attempts/{identity}/inspection/assessments.json'}}
            ).model_dump(mode='json')
        self.inspector.store.write_json(self.inspector._path(identity, 'input'), {
            'identity': identity, 'arguments': arguments, 'user_requests': list(self.requests)})
        self.inspector.store.write_json(self.inspector._path(identity, 'result'), raw)
        self.inspector.expected[identity] = deepcopy(raw)
        self.inspector.store.write_json(f'attempts/{identity}/native_result.json', raw)
        observed = {**deepcopy(raw), 'record': f'attempts/{identity}/native_result.json',
                    'arguments': arguments}
        self.observations.append(observed)
        return observed

    def concerns(self):
        return self.owner.pending_concerns(self.binding(self.delivery)['artifacts'])

    def pending_envelope(self, proof, *, record=None):
        saved = self.inspector.store.read_json(self.inspector._path(proof['identity'], 'input'))
        saved['execution_protocol'] = 'shared.v1'
        self.inspector.store.write_json(self.inspector._path(proof['identity'], 'input'), saved)
        envelope = {**proof, 'result': ToolResult(tool_name='inspect_artifacts',
            data={'inspection_pending': proof['identity']}).model_dump(mode='json')}
        observed = self.observe(envelope)
        if record is not None:
            observed['review_record'] = record
            self.inspector.store.write_json(record, proof['result'])
        return observed

    def test_revision_view_keeps_related_versions_and_omits_unrelated_findings(self):
        from simple_ar.tools.inspection import InspectionToolGateway

        self.inspector.verdict = 'needs_revision'
        proof = self.review('scoped-guidance')
        assessment = proof['result']['metadata']['delivery_assessment']
        first = str(self.workspace.resolve('bundle/manuscript.md'))
        second = str(self.workspace.resolve('bundle/appendix.txt'))
        versions = assessment['artifacts']
        assessment['findings'] = [
            {'severity':'required', 'message':'First only', 'subjects':{first:versions[first]}},
            {'severity':'advisory', 'message':'Second only', 'subjects':{second:versions[second]}},
            {'severity':'advisory', 'message':'Compare both',
             'subjects':{first:versions[first], second:versions[second]}},
            {'severity':'required', 'message':'Overall claim', 'subjects':{}},
        ]
        assessment['requested_outcomes'] = [{'outcome':'Supply both files', 'status':'supported',
            'artifacts':[first, second], 'explanation':'Both outputs were inspected.'}]
        self.inspector.store.write_json(self.inspector._path('scoped-guidance', 'result'), proof['result'])
        self.inspector.expected['scoped-guidance'] = deepcopy(proof['result'])
        self.observe(proof)
        raw = deepcopy(proof['result'])
        original_feedback = self.inspector.feedback_observation

        def projected_feedback(identity, claimed, **kwargs):
            feedback = original_feedback(identity, claimed, **kwargs)
            feedback['result'] = InspectionToolGateway.coordination_result(
                ToolResult.model_validate(feedback['result']),
                receipt_path=self.inspector._path(identity, 'result'))
            return feedback

        with patch.object(self.inspector, 'feedback_observation', side_effect=projected_feedback), \
             patch.object(self.inspector, 'feedback_view',
                          side_effect=InspectionToolGateway.feedback_view, create=True):
            view, = self.owner.inspection_feedback(['bundle/manuscript.md'])
            self.workspace.resolve('bundle/appendix.txt').write_text('Later edit', encoding='utf-8')
            revised, = self.owner.inspection_feedback(['bundle/manuscript.md'])
            self.workspace.resolve('bundle/unrelated.txt').write_text('Other work', encoding='utf-8')
            claim_only, = self.owner.inspection_feedback(['bundle/unrelated.txt'])
            original_hashes = self.workspace.artifact_hashes

            def inaccessible(paths):
                if second in paths:
                    raise OSError('Related source temporarily unavailable')
                return original_hashes(paths)

            with patch.object(self.workspace, 'artifact_hashes', side_effect=inaccessible):
                unavailable, = self.owner.inspection_feedback(['bundle/manuscript.md'])
            original_resolve = self.workspace.resolve

            def revoked(locator, *args, **kwargs):
                if str(locator) == second:
                    raise ValueError('Related source no longer authorized')
                return original_resolve(locator, *args, **kwargs)

            with patch.object(self.workspace, 'resolve', side_effect=revoked):
                restricted, = self.owner.inspection_feedback(['bundle/manuscript.md'])
        findings = view['result']['metadata']['delivery_opinion']['findings']
        self.assertEqual({row['message'] for row in findings}, {'First only', 'Compare both', 'Overall claim'})
        outcome, = view['result']['metadata']['delivery_opinion']['requested_outcomes']
        self.assertEqual((outcome['outcome'], outcome['status'], outcome['artifact_count']),
            ('Supply both files', 'supported', 2))
        self.assertIn('Both outputs were inspected.', outcome['explanation'])
        self.assertNotIn('artifacts', outcome)
        self.assertEqual(raw['metadata']['delivery_assessment']['requested_outcomes'][0]['artifacts'], [first, second])
        self.assertEqual({row['path'] for row in view['subjects']}, {first, second})
        linked = next(row for row in view['subjects'] if row['path'] == second)
        self.assertEqual(linked['review_role'], 'related_subject')
        self.assertEqual(linked['reviewed_sha256'], versions[second])
        self.assertTrue(linked['current_matches_review'])
        changed = next(row for row in revised['subjects'] if row['path'] == second)
        self.assertFalse(changed['current_matches_review'])
        self.assertEqual(changed['reviewed_sha256'], versions[second])
        self.assertIsNone(next(row for row in unavailable['subjects']
                               if row['path'] == second)['current_matches_review'])
        self.assertIsNone(next(row for row in restricted['subjects']
                               if row['path'] == second)['current_matches_review'])
        self.assertEqual([row['message'] for row in
            claim_only['result']['metadata']['delivery_opinion']['findings']], ['Overall claim'])
        self.assertEqual(proof['result'], raw)

    def test_old_package_pass_then_new_local_required_needs_explicit_new_review(self):
        original = self.review()
        self.observe(original)
        self.local()
        pending = self.concerns()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['record'], 'records/local-1/native_result.json')
        self.assertTrue(self.issues(self.observations, self.delivery))
        self.assertIsNone(self.review('no-start', may_start=False))
        fresh = self.review('fresh')
        self.assertEqual(fresh['identity'], 'fresh')
        self.assertEqual(self.inspector.store.read_json(
            self.inspector._path('fresh', 'input'))['prior_required'], pending)
        self.observe(fresh)  # A historical-style pass cannot silently clear F.
        self.assertEqual(self.concerns(), pending)
        self.inspector.reassess = True
        revised = self.review('reassessed')
        self.observe(revised)
        self.assertEqual(self.concerns(), [])
        self.assertEqual(self.issues(self.observations, self.delivery), [])
        count = len(self.inspector.calls)
        self.assertEqual(self.review('reuse'), revised)
        self.assertEqual(len(self.inspector.calls), count)
        # Repeated original observations are not later new findings.
        self.observations.append(deepcopy(self.observations[1]))
        self.assertEqual(self.concerns(), [])

    def test_changed_target_and_evidence_keep_original_id_and_versions(self):
        source = self.workspace.workspace / 'source.txt'
        source.write_text('original evidence', encoding='utf-8')
        self.local(evidence=self.workspace.artifact_hashes([str(source)]))
        pending = self.concerns()
        self.workspace.resolve('bundle/manuscript.md').write_text('Corrected claim', encoding='utf-8')
        source.write_text('revised evidence', encoding='utf-8')
        self.assertEqual(self.concerns(), pending)
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        self.inspector.reassess = True
        proof = self.review('current-review')
        self.observe(proof)
        self.assertEqual(self.concerns(), [])
        self.assertEqual(self.issues(self.observations, self.delivery), [])

    def test_mixed_package_file_and_claim_findings_have_distinct_stable_ids(self):
        self.inspector.verdict = 'needs_revision'
        self.inspector.claim_finding = True
        proof = self.review()
        self.observe(proof)
        pending = self.concerns()
        self.assertEqual(len(pending), 2)
        self.assertEqual([row['id'] for row in pending], sorted(row['id'] for row in pending))
        claim = next(row for row in pending if not row['subjects'])
        self.assertEqual(claim['record'], 'records/parent-1/native_result.json')
        self.local('local-pass', required=False, reassessed=[row['id'] for row in pending])
        self.assertIn(claim, self.concerns())  # File scope cannot retire claim scope.
        self.inspector.verdict = 'passed'
        self.inspector.claim_finding = False
        self.inspector.reassess = True
        self.observe(self.review('whole-reassessment'))
        self.assertEqual(self.concerns(), [])

    def test_partial_or_stale_reassessment_cannot_retire_concerns(self):
        self.local()
        pending = self.concerns()
        self.inspector.reassess = True
        self.inspector.verdict = 'incomplete'
        self.inspector.coverage = 'partial'
        self.observe(self.review('partial'))
        self.assertEqual(self.concerns(), pending)
        self.inspector.verdict = 'passed'
        self.inspector.coverage = 'complete'
        source = self.workspace.workspace / 'source.txt'
        source.write_text('original', encoding='utf-8')
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        self.observe(self.review('later-stale'))
        source.write_text('changed', encoding='utf-8')
        self.assertEqual(self.concerns(), pending)
        self.assertTrue(self.issues(self.observations, self.delivery))

    def test_unrelated_history_and_withdrawn_mount_do_not_force_review(self):
        proof = self.review()
        self.observe(proof)
        other = self.root / 'withdrawn'
        other.mkdir()
        (other / 'note.txt').write_text('unrelated', encoding='utf-8')
        self.workspace.mounts['withdrawn'] = other
        self.local('unrelated', path=str(other / 'note.txt'))
        self.workspace.mounts.pop('withdrawn')
        self.observations.append({'tool_name': 'write_file', 'status': 'ok', 'metadata': {}})
        self.assertEqual(self.concerns(), [])
        self.assertEqual(self.review('reuse'), proof)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_partial_package_preserves_completed_individual_rechecks(self):
        self.local('finding-1')
        self.local('finding-2')
        pending = self.concerns()
        first, second = pending
        source = self.workspace.workspace / 'source.txt'
        source.write_text('Actual independently read support', encoding='utf-8')
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        self.inspector.reply_ids = [first['id']]
        self.inspector.verdict = 'incomplete'
        self.inspector.coverage = 'partial'
        proof = self.review('partial-with-completed-item')
        self.observe(proof)
        self.assertEqual(self.concerns(), [second])
        self.assertTrue(self.issues(self.observations, self.delivery))
        next_proof = self.review('only-remaining-item')
        self.assertEqual(self.inspector.store.read_json(self.inspector._path(
            'only-remaining-item', 'input'))['prior_required'], [second])
        self.assertIn('not supplied', next_proof['error'])  # Cannot reselect retired F1.
        source.write_text('Evidence changed', encoding='utf-8')
        self.assertEqual(self.concerns(), pending)

    def test_multi_subject_pending_scope_excludes_already_reassessed_path(self):
        self.inspector.verdict = 'needs_revision'
        original = self.review('multi-subject')
        self.observe(original)
        concern = self.concerns()[0]
        path_a = str(self.workspace.resolve('bundle/manuscript.md'))
        path_b = str(self.workspace.resolve('bundle/appendix.txt'))
        self.assertEqual(concern['pending_subjects'], sorted([path_a, path_b]))
        self.local('recheck-a', required=False, reassessed=[concern['id']])
        remaining = self.concerns()[0]
        self.assertEqual(remaining['subjects'], {path_b: concern['subjects'][path_b]})
        self.assertEqual(original['result']['metadata']['delivery_assessment']['findings'][0]['subjects'],
                         concern['subjects'])  # Original receipt/identity remains intact.
        self.assertEqual(remaining['id'], concern['id'])
        self.assertEqual(remaining['pending_subjects'], [path_b])
        source = self.workspace.workspace / 'source.txt'
        source.write_text('Original support for B', encoding='utf-8')
        self.inspector.sources = self.workspace.artifact_hashes([str(source)])
        self.inspector.reply_ids = [concern['id']]
        self.inspector.verdict = 'incomplete'
        self.inspector.coverage = 'partial'
        proof = self.review('recheck-b')
        self.assertEqual(self.inspector.store.read_json(self.inspector._path(
            'recheck-b', 'input'))['prior_required'], [remaining])
        self.observe(proof)
        self.assertEqual(self.concerns(), [])
        self.assertTrue(self.issues(self.observations, self.delivery))  # Package still incomplete.

    def test_fabricated_reassessment_id_is_not_authorized_by_a_pass(self):
        self.local()
        pending = self.concerns()
        self.inspector.reply_ids = ['f' * 64]
        proof = self.review('fabricated')
        self.assertIn('not supplied', proof['error'])
        self.observe(proof)
        self.assertTrue(self.issues(self.observations, self.delivery))
        self.assertTrue(pending)

    def test_unaccepted_pending_reply_never_clears_or_commits(self):
        self.local()
        pending = self.concerns()
        self.inspector.reassess = True
        self.inspector.verdict = 'needs_revision'
        proof = self.review('pending-review')
        observed = self.pending_envelope(proof)
        with patch.object(self.inspector, 'feedback_observation',
                          side_effect=AssertionError('Unaccepted opinion must not be decoded')):
            guidance = self.owner.feedback_from_observation(observed)
        self.assertFalse(guidance['opinion_available'])
        self.assertEqual(guidance['result']['status'], 'blocked')
        self.assertNotIn('The requested explanation is absent.', json.dumps(guidance['result']))
        self.assertEqual(self.concerns(), pending)
        self.assertIn('opinion record', '; '.join(self.issues(self.observations, self.delivery)))
        observed['review_record'] = 'attempts/not-accepted/native_result.json'
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                   side_effect=ValueError('No accepted opinion action')):
            unknown = self.owner.feedback_from_observation(observed)
            self.assertFalse(unknown['opinion_available'])
            self.assertIsNone(unknown['opinion_accepted'])
            with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                self.concerns()
            self.assertTrue(self.issues(self.observations, self.delivery))

        # An accepted action whose retained owner result was changed is not an
        # unaccepted response. It stays unresolved and cannot be silently skipped.
        path = self.inspector.store.resolve(self.inspector._path('pending-review', 'result'))
        original_bytes = path.read_bytes()
        damaged_result = deepcopy(proof['result'])
        damaged_result['metadata'].pop('delivery_assessment')
        self.inspector.store.write_json(self.inspector._path('pending-review', 'result'), damaged_result)
        try:
            with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                       return_value=(None, proof['result'], 'accepted')):
                damaged = self.owner.feedback_from_observation(observed)
                self.assertTrue(damaged['opinion_accepted'])
                self.assertFalse(damaged['opinion_available'])
                with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                    self.concerns()
                other = self.workspace.workspace / 'unrelated.txt'
                other.write_text('Unrelated work', encoding='utf-8')
                # Its reassessment ID cannot be related to the selected scope
                # from the retained events, so do not silently call it unrelated.
                with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                    self.owner.pending_concerns([str(other)])
            unrelated_result = deepcopy(proof['result'])
            unrelated_result['metadata']['delivery_assessment']['reassessed_prior'] = []
            with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                       return_value=(None, unrelated_result, 'accepted')):
                self.assertEqual(self.owner.pending_concerns([str(other)]), [])
            claim_result = deepcopy(unrelated_result)
            claim_result['metadata']['delivery_assessment']['findings'][0]['subjects'] = {}
            with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                       return_value=(None, claim_result, 'accepted')):
                with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                    self.owner.pending_concerns([str(other)])
        finally:
            path.write_bytes(original_bytes)

    def test_accepted_pending_recovery_and_later_findings_use_one_reducer(self):
        self.local()
        self.inspector.reassess = True
        proof = self.review('shared-review')
        observed = self.pending_envelope(proof, record='attempts/accepted-opinion/native_result.json')
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                   return_value=(None, proof['result'], 'accepted')) as accepted:
            self.assertEqual(self.concerns(), [])
            self.assertEqual(self.issues(self.observations, self.delivery), [])
            self.assertTrue(accepted.called)
            self.assertTrue(any(call.args[4] == 'shared-review' for call in accepted.call_args_list))
            self.local('later-required')
            self.assertEqual(len(self.concerns()), 1)
            self.assertTrue(self.issues(self.observations, self.delivery))
            count = len(self.inspector.calls)
            self.assertEqual(self.review('shared-review', may_start=False)['identity'], 'shared-review')
            self.assertEqual(len(self.inspector.calls), count)
            original = self.inspector.feedback_observation
            def damaged(identity, claimed, **kwargs):
                return ({'opinion_available':False} if identity == 'shared-review'
                        else original(identity, claimed, **kwargs))
            with patch.object(self.inspector, 'feedback_observation', side_effect=damaged):
                with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                    self.concerns()
                other = self.workspace.workspace / 'unrelated-after-review.txt'
                other.write_text('Other work', encoding='utf-8')
                # The earlier reassessment ID is not in the selected history;
                # its relevance cannot yet be established from that view.
                with self.assertRaisesRegex(ValueError, 'delivery concern opinion cannot be verified'):
                    self.owner.pending_concerns([str(other)])
        self.assertEqual(observed['review_record'], 'attempts/accepted-opinion/native_result.json')

    def test_accepted_pending_historical_required_survives_target_revision(self):
        self.inspector.verdict = 'needs_revision'
        proof = self.review('historical-shared')
        self.pending_envelope(proof, record='attempts/accepted-opinion/native_result.json')
        self.inspector.store.write_json('session_manifest.json', {'session_id': 'controlled-session'})
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                   return_value=(None, proof['result'], 'accepted')):
            pending = self.concerns()
            self.assertEqual(len(pending), 1)
            self.workspace.resolve('bundle/manuscript.md').write_text('new version', encoding='utf-8')
            self.assertEqual(self.concerns(), pending)


class InspectionOfferCoverageTests(unittest.TestCase):
    """Exercise the ordinary shared inspector with its existing offline provider."""

    def setUp(self):
        self._set_up_case('task.v3')

    def _set_up_case(self, protocol):
        from dataclasses import replace
        from simple_ar.integrations.llm import LLMClient
        from tests.test_professional_work_loop import OperationsWorkLoopTests

        self.case = OperationsWorkLoopTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.services.llm_client = LLMClient(replace(
            self.case.services.llm_client._settings, context_input_tokens=9000))
        self.case._create(goal='Inspect the report and describe its scope.')
        # These existing coverage/priming tests exercise the saved v3 contract,
        # even after ordinary creation moves to v4. Freeze before inspection.
        if self.case.app.state['config']['delivery_review_protocol'] != protocol:
            self.case.app.state['config']['delivery_review_protocol'] = protocol
            self.case.app._save()
            self.case._reload()

    def _long_report(self, rows=1000):
        head = ('本报告记录观察条件及样本结果并说明解释范围' * 20)[:170] + '\n\n'
        text = head + 'record,value,description\n' + ''.join(
            f'{number},2,observed sample\n' for number in range(rows))
        (self.case.root / 'workspace' / 'report.md').write_text(text, encoding='utf-8')
        self.case.review = lambda view: {'decisions': [{'subject':'target-1',
            'conclusion':'complete', 'findings':[], 'evidence':[], 'limitations':[]}]}
        return text

    def test_complete_read_with_unsupplied_tail_is_partial_and_recovers(self):
        from tests.test_professional_work_loop import _assessments
        from simple_ar.integrations.llm import estimate_tokens

        text = self._long_report(rows=2400)
        # One real caller window reads the entire target, while its body alone
        # exceeds the actual model input budget. This isolates read-vs-supply
        # integrity from improvements in adaptive priming and serialization.
        self.assertLessEqual(len(text.encode('utf-8')), 64000)
        self.assertGreater(estimate_tokens(text), self.case.services.llm_client._settings.context_input_tokens)
        result = self.case._inspect([{'path':'report.md', 'max_bytes':64000}], [])
        self.assertEqual(result['status'], 'ok', result)
        identity = Path(result['data']['record']).parts[1]
        store = self.case.app.session.store
        saved = store.read_json(f'tools/inspection/{identity}/input.json')
        trace = store.read_json(f'tools/inspection/{identity}/sources-0.json')
        self.assertGreater(estimate_tokens(text), trace['offer']['projection']['input_token_target'])
        view = json.loads(trace['offer']['messages'][1]['content'])
        bodies = [entry['observation'] for entry in view['working_view']['observations']
            if entry['observation'].get('path') == saved['targets'][0]['path']]
        self.assertTrue(saved['targets'][0]['complete'])
        if bodies:
            self.assertLess(max(row['next_offset_bytes'] for row in bodies), len(text.encode('utf-8')))
        assessment = _assessments(result)[0]
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))
        self.assertIn('not proven supplied', ' '.join(assessment['limitations']))
        self.assertEqual(self.case.app.state['config']['inspection_coverage_policy'], 'offered.v2')
        calls, ledger = self.case.send.call_count, self.case.app.ledger.to_dict()
        self.case._reload()
        cached = self.case.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.model_dump(mode='json'), result)
        self.assertEqual((self.case.send.call_count, self.case.app.ledger.to_dict()), (calls, ledger))

    def test_old_task_without_started_inspection_keeps_its_original_contract(self):
        from tests.test_professional_work_loop import _assessments

        # A task saved before this policy has no marker even if it starts its
        # first inspection after the software is updated.
        self.case.app.state['config'].pop('inspection_coverage_policy')
        self.case.app.state['config'].pop('inspection_finding_policy')
        self.case.app._save()
        self.case._reload()
        self.assertIsNone(self.case.app.operations.inspection.coverage_policy)
        self.assertIsNone(self.case.app.operations.inspection.finding_policy)
        self._long_report()
        result = self.case._inspect(['report.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual((_assessments(result)[0]['coverage'], _assessments(result)[0]['verdict']),
            ('complete', 'passed'))
        identity = Path(result['data']['record']).parts[1]
        saved = self.case.app.session.store.read_json(f'tools/inspection/{identity}/input.json')
        self.case._reload()
        cached = self.case.app.operations.inspection.call('inspect_artifacts', saved['arguments'], identity)
        self.assertEqual(cached.model_dump(mode='json'), result)

    def test_delivery_requires_supply_of_selected_evidence_not_every_artifact(self):
        from tests.test_professional_work_loop import _action

        paths = []
        for number in range(8):
            path = f'record-{number:02}.txt'
            (self.case.root / 'workspace' / path).write_text(
                f'Original record {number}\n' + 'Recorded observation and scope.\n' * 180, encoding='utf-8')
            paths.append(path)
        chosen = []
        def review(view):
            self.assertIn('claim', view['proposed_delivery'])
            candidates = [row for row in view['evidence'] if row.get('observed_ranges')
                and bool(row['text_windows']) == bool(chosen)]
            if chosen and not candidates:
                # The complete evidence set can exceed the initial context.
                # Ask for a real source instead of assuming one was supplied.
                self.assertLessEqual(len(self.case.inspection_requests), 2)
                return [_action('read_file', path=paths[0], offset_bytes=0, max_bytes=8192)]
            self.assertTrue(candidates, 'Fixture needs both supplied and capacity-omitted source windows')
            chosen.append(candidates[0]['subject'])
            return {'conclusion':'complete', 'findings':[], 'evidence':[chosen[-1]], 'limitations':[]}
        self.case.review = review
        delivery = _action('deliver_result', summary='The requested records.', artifacts=paths)
        self.case._submit(delivery)
        first = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['result']
        assessment = first['metadata']['delivery_assessment']
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))
        self.assertIn('not proven supplied', ' '.join(assessment['limitations']))
        self.case._submit(delivery)
        second = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['result']
        assessment = second['metadata']['delivery_assessment']
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('complete', 'passed'))
        self.assertGreater(len(second['data']['evidence']), 1)

    def test_supplied_native_read_completes_target_and_direct_answer_remains_valid(self):
        from tests.test_professional_work_loop import _action, _assessments

        text = 'A bounded observation for ordinary text review.\n' * 30
        (self.case.root / 'workspace' / 'note.md').write_text(text, encoding='utf-8')
        def review(view):
            if 'proposed_delivery' in view:
                return {'conclusion':'complete', 'findings':[], 'evidence':[], 'limitations':[]}
            if len(self.case.inspection_requests) == 1:
                return [_action('read_file', path='note.md', offset_bytes=512, max_bytes=4096)]
            return {'decisions':[{'subject':'target-1', 'conclusion':'complete',
                'findings':[], 'evidence':[], 'limitations':[]}]}
        self.case.review = review
        result = self.case._inspect([{'path':'note.md', 'max_bytes':512}], [])
        assessment = _assessments(result)[0]
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('scoped', 'passed'))
        self.assertEqual(assessment['scope'], {'start': 0, 'end': 512, 'size_bytes': len(text.encode())})
        self.case._submit(_action('deliver_result', summary='The requested brief answer.', artifacts=[]))
        answer = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['result']
        self.assertEqual(answer['metadata']['delivery_assessment']['verdict'], 'passed')

    def test_scoped_pass_does_not_clear_a_whole_file_finding(self):
        from simple_ar.core.assessments import assessment_issues
        from tests.test_professional_work_loop import _assessments

        text = 'A report sentence and its supporting condition.\n' * 30
        (self.case.root / 'workspace' / 'note.md').write_text(text, encoding='utf-8')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'findings':[{'severity':'required', 'message':'A conclusion still lacks its source.'}],
            'evidence':[], 'limitations':[]}]}
        whole = self.case._inspect(['note.md'], [])
        self.assertEqual(_assessments(whole)[0]['verdict'], 'needs_revision')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'findings':[], 'evidence':[], 'limitations':[]}]}
        scoped = self.case._inspect([{'path':'note.md', 'offset_bytes':100, 'max_bytes':200}], [])
        assessment = _assessments(scoped)[0]
        self.assertEqual((assessment['schema_version'], assessment['coverage'], assessment['verdict']),
            ('artifact_assessment.v2', 'scoped', 'passed'))
        self.assertEqual(assessment['scope'], {'start':100, 'end':300, 'size_bytes':len(text.encode())})
        issues = assessment_issues(self.case.app.workspace, [whole, scoped], ['note.md'],
            require_pass=['note.md'])
        self.assertTrue(any('lacks its source' in issue for issue in issues))
        self.assertTrue(any('lacks a complete independent inspection' in issue for issue in issues))
        # Reading the whole file for another question also does not, by itself,
        # establish that the earlier concern was re-examined.
        later = self.case._inspect(['note.md'], [])
        self.assertTrue(assessment_issues(self.case.app.workspace, [whole, scoped, later], ['note.md']))
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'reassessed_prior':True, 'findings':[], 'evidence':[], 'limitations':[]}]}
        checked = self.case._inspect(['note.md'], [])
        self.assertFalse(assessment_issues(self.case.app.workspace, [whole, scoped, later, checked], ['note.md']))

    def test_replayed_opinion_cannot_clear_or_reopen_newer_findings(self):
        from simple_ar.core.assessments import assessment_issues, current_assessments, finding_id

        (self.case.root / 'workspace' / 'note.md').write_text('One version of the work.', encoding='utf-8')
        subjects = self.case.app.workspace.artifact_hashes(['note.md'])
        finding = {'severity':'required', 'message':'Check the underlying data.', 'subjects':subjects}
        def opinion(identity, *, findings=(), reassessed=()):
            row = {'schema_version':'artifact_assessment.v2', 'review_id':identity,
                'subjects':subjects, 'coverage':'complete',
                'verdict':'needs_revision' if findings else 'passed',
                'findings':list(findings), 'reassessed_prior':list(reassessed),
                'evidence_sha256':{}, 'limitations':[]}
            return {'status':'ok', 'metadata':{'assessments':[row]}}
        earlier = opinion('review-a:target-1')
        later = opinion('review-b:target-1', findings=[finding])
        self.assertTrue(assessment_issues(self.case.app.workspace, [later, earlier], ['note.md']))
        self.assertTrue(assessment_issues(self.case.app.workspace, [earlier, later, earlier], ['note.md']))
        reviewed_id = finding_id(later['metadata']['assessments'][0], finding)
        reassessed = opinion('review-c:target-1', reassessed=[reviewed_id])
        self.assertFalse(assessment_issues(self.case.app.workspace, [later, reassessed, later], ['note.md']))
        other = {**finding, 'message':'Check the sample scope.'}
        new_opinion = opinion('review-d:target-1', findings=[other])
        self.assertTrue(any('sample scope' in item for item in assessment_issues(
            self.case.app.workspace, [later, new_opinion, reassessed], ['note.md'])))

        def legacy(*, findings=()):
            return {'status':'ok', 'metadata':{
                'inspection_binding':{'assessment_path':'attempts/legacy/inspection/assessments.json'},
                'assessments':[{'schema_version':'artifact_assessment.v1', 'subjects':subjects,
                    'coverage':'complete', 'verdict':'needs_revision' if findings else 'passed',
                    'findings':list(findings), 'evidence_sha256':{}, 'limitations':[]}]}}
        legacy_pass = legacy()
        self.assertTrue(assessment_issues(self.case.app.workspace, [legacy_pass, later, legacy_pass], ['note.md']))
        legacy_finding = legacy(findings=[finding])
        stored = current_assessments(self.case.app.workspace, [legacy_finding])[0].model_dump(mode='json')
        resolved = opinion('review-e:target-1', reassessed=[finding_id(stored, finding)])
        self.assertFalse(assessment_issues(self.case.app.workspace, [legacy_finding, resolved, legacy_finding], ['note.md']))

    def test_legacy_multi_subject_finding_remains_visible_for_unreviewed_subject(self):
        from simple_ar.core.assessments import finding_id
        from simple_ar.tools.inspection import InspectionToolGateway

        subjects = {'/source/a.txt':'a'*64, '/source/b.txt':'b'*64}
        finding = {'severity':'required', 'message':'Both sources need checking.', 'subjects':subjects}
        legacy = {'schema_version':'artifact_assessment.v1', 'review_id':'legacy',
            'subjects':subjects, 'coverage':'complete', 'verdict':'needs_revision', 'findings':[finding]}
        reviewed = {'schema_version':'artifact_assessment.v2', 'review_id':'later:target-1',
            'subjects':{'/source/a.txt':'a'*64}, 'coverage':'complete', 'verdict':'passed',
            'findings':[], 'reassessed_prior':[finding_id(legacy, finding)]}
        owner = InspectionToolGateway.__new__(InspectionToolGateway)
        owner.prior_assessments = lambda:[legacy, reviewed]
        both = [{'subject':'target-1', 'path':'/source/a.txt', 'sha256':'a'*64},
                {'subject':'target-2', 'path':'/source/b.txt', 'sha256':'b'*64}]
        self.assertEqual([row['subjects'] for row in owner._prior(both, current_policy=True)], [['target-2']])
        self.assertEqual([row['subjects'] for row in owner._prior([both[1]], current_policy=True)], [['target-2']])

    def test_line_window_and_saved_older_coverage_contract(self):
        from tests.test_professional_work_loop import _assessments

        lines = ['第一行。\n', '第二行。\n', '第三行。\n', '第四行。\n']
        (self.case.root / 'workspace' / 'note.md').write_text(''.join(lines), encoding='utf-8')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'findings':[], 'evidence':[], 'limitations':[]}]}
        target = {'path':'note.md', 'start_line':2, 'max_lines':2, 'max_bytes':128}
        result = self.case._inspect([target], [])
        assessment = _assessments(result)[0]
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('scoped', 'passed'))
        self.assertEqual(assessment['scope'], {'start':len(lines[0].encode()),
            'end':len(''.join(lines[:3]).encode()), 'size_bytes':len(''.join(lines).encode())})

        # A previously saved task keeps its own policy, even when resumed on
        # a newer application. Its recorded opinion is never upgraded.
        self.case.app.state['config']['inspection_coverage_policy'] = 'offered.v1'
        self.case.app.state['config'].pop('inspection_finding_policy')
        self.case.app._save()
        self.case._reload()
        old = self.case._inspect([target], [])
        self.assertEqual((_assessments(old)[0]['coverage'], _assessments(old)[0]['verdict']),
            ('partial', 'incomplete'))
        owner = self.case.app.operations.inspection
        identity = Path(old['data']['record']).parts[1]
        saved = owner.store.read_json(owner._path(identity, 'input'))
        owner.coverage_policy = 'offered.v2'
        self.assertEqual(owner.call('inspect_artifacts', saved['arguments'], identity).model_dump(mode='json'), old)

    def test_empty_source_and_frozen_coverage_contract(self):
        from tests.test_professional_work_loop import _assessments

        (self.case.root / 'workspace' / 'empty.txt').write_text('', encoding='utf-8')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'findings':[], 'evidence':[], 'limitations':[]}]}
        result = self.case._inspect(['empty.txt'], [])
        self.assertEqual((_assessments(result)[0]['coverage'], _assessments(result)[0]['verdict']),
            ('complete', 'passed'))
        owner = self.case.app.operations.inspection
        identity = Path(result['data']['record']).parts[1]
        saved = owner.store.read_json(owner._path(identity, 'input'))
        self.assertEqual(saved['coverage_policy'], 'offered.v2')
        owner.coverage_policy = None  # Changing future defaults must not reinterpret this inspection.
        self.assertEqual(owner.call('inspect_artifacts', saved['arguments'], identity).model_dump(mode='json'), result)

    def test_bad_reply_does_not_erase_a_settled_request_supply(self):
        (self.case.root / 'workspace' / 'note.md').write_text('Source text for independent inspection.\n')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1', 'conclusion':'complete',
            'findings':[], 'evidence':[], 'limitations':[]}]}
        provider, changed = self.case.send.side_effect, []
        def one_bad_reply(mode, request, **kwargs):
            response = provider(mode, request, **kwargs)
            if not changed and any(tool['function']['name'] == 'record_inspection'
                    for tool in request.get('tools', [])):
                calls = response['choices'][0]['message']['tool_calls']
                calls.append(deepcopy(calls[0]))  # Duplicate identities reject the reply, not the request.
                changed.append(True)
            return response
        self.case.send.side_effect = one_bad_reply
        result = self.case._inspect(['note.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        owner = self.case.app.operations.inspection
        identity = Path(result['data']['record']).parts[1]
        saved = owner.store.read_json(owner._path(identity, 'input'))
        first = owner.store.read_json(owner._path(identity, 'sources-0'))
        self.assertIn('malformed_calls', first)
        read_round = owner._closed_source_round
        with patch.object(owner, '_closed_source_round', side_effect=lambda s, n, **kw:
                read_round(s, n, **kw) if n == 0 else None):
            supplied = owner._offered_text_ranges(saved)
        target = saved['targets'][0]
        self.assertTrue(owner._range_supplied(supplied[(target['path'], target['sha256'])],
            0, target['windows'][0]['size_bytes']))

    def test_source_coverage_survives_split_input_and_unrelated_large_attachment(self):
        from tests.test_professional_work_loop import _assessments

        root = self.case.root / 'workspace'
        parts = ['实验记录：温度与测量范围。\n' * 45, 'Independent observations and limits.\n' * 45]
        for name, text in [('joined.md', ''.join(parts)), ('part-a.md', parts[0]), ('part-b.md', parts[1]),
                           ('background.md', 'Unrelated background.\n' * 4000)]:
            (root / name).write_text(text, encoding='utf-8')
        self.case.review = lambda view: {'decisions':[{'subject':row['subject'], 'conclusion':'complete',
            'findings':[], 'evidence':[], 'limitations':[]} for row in view['targets']]}
        for targets in (['joined.md'], ['part-a.md', 'part-b.md']):
            with self.subTest(targets=targets):
                result = self.case._inspect(targets, ['background.md'])
                self.assertEqual(result['status'], 'ok', result)
                self.assertTrue(all(row['coverage'] == 'complete' for row in _assessments(result)))

    def test_paginated_history_counts_only_after_the_complete_owner_result_is_supplied(self):
        from tests.test_professional_work_loop import _action

        (self.case.root / 'workspace' / 'note.md').write_text('Check the supplied source.\n', encoding='utf-8')
        def review(view):
            turn = len(self.case.inspection_requests)
            if turn == 1:
                return [_action('read_file', path='measurements/conditions.txt', max_bytes=512)]
            if turn == 2:
                return [_action('read_history', start_turn=0, action=0, max_bytes=80)]
            if turn == 3:
                pages = [json.loads(row['content'])['data'] for row in self.case.inspection_requests[-1]['messages']
                    if row['role'] == 'tool' and json.loads(row['content']).get('tool_name') == 'read_history']
                return [_action('read_history', start_turn=0, action=0,
                    offset_bytes=pages[-1]['next_offset_bytes'], max_bytes=4096)]
            return {'decisions':[{'subject':'target-1', 'conclusion':'complete',
                'findings':[], 'evidence':[], 'limitations':[]}]}
        self.case.review = review
        result = self.case._inspect(['note.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        owner = self.case.app.operations.inspection
        identity = Path(result['data']['record']).parts[1]
        saved = owner.store.read_json(owner._path(identity, 'input'))
        original_round = owner._closed_source_round
        def only_history(saved, number, *, partial=False, **kwargs):
            trace = deepcopy(original_round(saved, number, **kwargs))
            if trace:
                offer = trace.get('offer', {})
                offer.get('projection', {})['selected_records'] = []
                if partial:
                    offer['messages'] = [message for message in offer.get('messages', [])
                        if message.get('role') != 'tool' or json.loads(message['content']).get('tool_name') != 'read_history'
                        or json.loads(message['content']).get('data', {}).get('offset_bytes') == 0]
            return trace
        row = next(row for row in saved['evidence'] if row['path'].endswith('conditions.txt'))
        key = (row['path'], row['sha256'])
        with patch.object(owner, '_closed_source_round', side_effect=only_history):
            self.assertIn(key, owner._offered_text_ranges(saved))
        with patch.object(owner, '_closed_source_round', side_effect=lambda s, n, **kw: only_history(s, n, partial=True, **kw)):
            self.assertNotIn(key, owner._offered_text_ranges(saved))


class ApplicableDeliveryInspectionTests(unittest.TestCase):
    """Real shared receipts and projection; offline replies control judgment only."""

    def setUp(self):
        InspectionOfferCoverageTests._set_up_case(self, 'task.v4')

    def _assessment(self):
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        return proof, proof['result']['metadata']['delivery_assessment']

    def _only_requested_sources(self):
        # These provenance counterexamples intentionally withhold subject bodies
        # so a receipt fragment/process output is the only offered support. Keep
        # this evidence precondition explicit as normal delivery priming improves.
        self.enterContext(patch.object(self.case.app.operations.inspection, '_prime_delivery', return_value=None))
        self.enterContext(patch('simple_ar.tools.materials.material_observation_seed', return_value=None))

    @staticmethod
    def _complete(view):
        return {'conclusion':'complete', 'findings':[], 'limitations':[],
            'evidence':[row['subject'] for row in view['evidence'] if row['text_windows']]}


    def _scientific_delivery(self):
        from tests.test_professional_work_loop import _action

        source = self.case.inputs / 'values.txt'
        source.write_text('1\n2\n3\n', encoding='utf-8')
        output = self.case.root / 'workspace' / 'answer.dat'
        output.write_text('The supplied three values have mean 2.\n', encoding='utf-8')
        (self.case.root / 'workspace' / 'attachment').write_text('UNRELATED-ATTACHMENT', encoding='utf-8')
        phases = []
        def review(view):
            paths = ['answer.dat', 'measurements/values.txt']
            sources = self.case._inspection_sources(view, *paths)
            missing = [_action('read_file', path=path, max_bytes=4096)
                for path, row in zip(paths, sources) if row is None]
            if missing:
                phases.append('investigate')
                return missing
            bodies = {row['path']: ''.join(w['text'] for w in row['text_windows'])
                for row in view['evidence']}
            self.assertIn(output.read_text(encoding='utf-8'), bodies.values())
            values = [float(value) for value in bodies[str(source)].splitlines()]
            self.assertEqual(sum(values) / len(values), 2)
            phases.append('observed')
            return self._complete(view)
        self.case.review = review
        self.case._submit(_action('deliver_result',
            summary='The mean is 2; the conclusion is limited to the supplied values.',
            artifacts=['answer.dat', 'attachment']))
        proof, assessment = self._assessment()
        self.assertEqual(phases, ['investigate', 'observed'])
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertIn(str(source), assessment['evidence_sha256'])
        self.assertIn(str(output), assessment['evidence_sha256'])
        return source, output, proof

    def test_scientific_evidence_and_delivered_bytes_must_remain_current(self):
        source, output, proof = self._scientific_delivery()
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        before = self.case.send.call_count
        for path in (source, output):
            original = path.read_bytes()
            path.write_bytes(original + b'changed\n')
            try:
                result = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
                    delivery_context=saved['delivery_context'])
                self.assertFalse(result.ok)
            finally:
                path.write_bytes(original)
        self.assertEqual(self.case.send.call_count, before)

    def test_selected_evidence_without_proven_supply_cannot_pass(self):
        _, _, proof = self._scientific_delivery()
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        receipt = inspector._receipt(proof['identity'])
        # Intact reads and an actual completed opinion are insufficient if their
        # request-supply proof is absent. All other receipt checks remain real.
        with patch.object(inspector, '_offered_text_ranges', return_value={}):
            assessment, _ = inspector._delivery_assessment(saved, receipt, proof['identity'], '')
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))

    def test_selected_parent_receipt_rechecks_source_authority_and_versions(self):
        from tests.test_professional_work_loop import _action

        source = self.case.inputs / 'archived.txt'
        source.write_text('An original bounded observation.\n', encoding='utf-8')
        self.case._read('measurements/archived.txt')
        (self.case.root / 'workspace' / 'note.dat').write_text('The bounded observation is supplied.\n', encoding='utf-8')
        (self.case.root / 'workspace' / 'attachment').write_text('UNREAD-ATTACHMENT', encoding='utf-8')
        records = []
        def review(view):
            if not records:
                # Navigation is selected by the ordinary capacity projector.
                descriptors = [json.loads(row['observation']['text'])
                    for row in view['working_view']['observations']
                    if row['observation'].get('field') == 'source_history']
                entry = next(row for row in descriptors if row['path'] == str(source))
                records.append(entry['records'][0]['record'])
                return [_action('read_file', path=records[-1], max_bytes=64000),
                    _action('read_file', path=str(source), max_bytes=4096),
                    _action('read_file', path='note.dat', max_bytes=4096)]
            return self._complete(view)
        self.case.review = review
        self.case._submit(_action('deliver_result', summary='The bounded original observation.',
            artifacts=['note.dat', 'attachment']))
        proof, assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertIn(str(source), assessment['evidence_sha256'])
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        before = self.case.send.call_count
        receipt = self.case.app.workspace.resolve(records[0])
        for path in (source, receipt):
            original = path.read_bytes()
            path.write_bytes(original + b'changed\n')
            try:
                result = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
                    delivery_context=saved['delivery_context'])
                self.assertFalse(result.ok)
            finally:
                path.write_bytes(original)
        self.case.app.workspace.mounts.pop('measurements')
        result = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
            delivery_context=saved['delivery_context'])
        self.assertFalse(result.ok)
        self.assertEqual(self.case.send.call_count, before)

    def test_pending_required_concerns_need_explicit_recheck_in_v4(self):
        from tests.test_professional_work_loop import _action
        self._only_requested_sources()

        (self.case.root / 'workspace' / 'note.dat').write_text('The bounded observation.\n', encoding='utf-8')
        self.case._read('note.dat')  # A parent receipt is navigation, not automatic subject support.
        self.case.review = lambda view: {'decisions':[{'subject':'target-1',
            'conclusion':'complete', 'findings':[{'severity':'required',
                'message':'Explain the observation boundary.'}], 'evidence':[], 'limitations':[]}]}
        self.case._inspect(['note.dat'], [])
        (self.case.root / 'workspace' / 'unrelated.txt').write_text('Unrelated readable evidence.\n', encoding='utf-8')
        (self.case.root / 'workspace' / 'attachment').write_text('UNREAD-ATTACHMENT', encoding='utf-8')
        def review(view, *, reassess=False, path='note.dat'):
            if not view['evidence']:
                # A genuine selected window is sufficient provenance; the
                # controlled opinion, not this test, judges its relevance.
                return [_action('read_file', path=path, max_bytes=8)]
            result = self._complete(view)
            self.assertTrue(view['prior_required'])
            if reassess:
                result['reassessed_prior'] = [row['id'] for row in view['prior_required']]
            return result
        delivery = _action('deliver_result', summary='The observation and its boundary.',
            artifacts=['note.dat', 'attachment'])
        self.case.review = review
        self.case._submit(delivery)
        _, assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'incomplete')

        self.case.review = lambda view: review(view, reassess=True, path='unrelated.txt')
        self.case._submit(delivery)
        proof, assessment = self._assessment()
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))
        self.assertEqual(assessment['reassessed_prior'], [])
        self.assertNotEqual(self.case.app.session.manifest.status, 'completed')
        owner = self.case.app.operations.delivery_review
        self.assertTrue(owner.pending_concerns(['note.dat']))
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        self.assertEqual(saved['delivery_context']['scope_policy'], 'applicable.v1')
        self.assertFalse(any(row['path'].endswith('note.dat') for row in saved['evidence']))

        def receipt_fragment(view):
            if not view['evidence']:
                descriptors = [json.loads(row['observation']['text'])
                    for row in view['working_view']['observations']
                    if row['observation'].get('field') == 'source_history']
                entry = next(row for row in descriptors if row['path'].endswith('note.dat'))
                return [_action('read_file', path=entry['records'][0]['record'], max_bytes=32)]
            result = self._complete(view)
            result['reassessed_prior'] = [row['id'] for row in view['prior_required']]
            return result

        self.case.review = receipt_fragment
        self.case._submit(delivery)
        _, assessment = self._assessment()
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))
        self.assertTrue(owner.pending_concerns(['note.dat']))

        self.case.review = lambda view: review(view, reassess=True)
        self.case._submit(delivery)
        proof, assessment = self._assessment()
        self.assertEqual(assessment['verdict'], 'passed')
        self.assertTrue(assessment['reassessed_prior'])
        self.assertFalse(owner.pending_concerns(['note.dat']))
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        self.assertTrue(all(not row['complete'] for row in saved['evidence']))
        bindings = inspector._stored_shared_bindings(saved, inspector._receipt(proof['identity'])['response'])
        self.assertTrue(any(str(self.case.root / 'workspace' / 'note.dat') in scope
            for key, scope in bindings.items() if key.startswith('evidence:')))
        before = self.case.send.call_count
        cached = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
            delivery_context=saved['delivery_context'])
        self.assertEqual(cached.model_dump(mode='json'), proof['result'])
        self.assertEqual(self.case.send.call_count, before)

        receipt = inspector._receipt(proof['identity'])
        with patch.object(inspector, '_offered_text_ranges', return_value={}):
            missing, _ = inspector._delivery_assessment(saved, receipt, proof['identity'], '')
        self.assertEqual(missing['reassessed_prior'], [])
        self.assertEqual(missing['verdict'], 'incomplete')

        # Historical task.v3 inspections retain their original dependency shape.
        historical = deepcopy(saved)
        historical['delivery_context'].pop('scope_policy')
        old_bindings = inspector._shared_final_bindings(historical, receipt['response'])
        self.assertEqual(set(old_bindings), {'delivery'})
        with patch.object(inspector, '_stored_shared_bindings', return_value=old_bindings):
            old, error = inspector._delivery_assessment(historical, receipt, proof['identity'], '',
                check_versions=False)
        self.assertEqual(error, '')
        self.assertEqual(old, assessment)

    def test_recheck_accepts_bound_process_evidence_without_reading_subject_body(self):
        import sys
        from tests.test_professional_work_loop import _action
        self._only_requested_sources()

        note = self.case.root / 'workspace' / 'note.dat'
        note.write_text('A bounded observation.\n', encoding='utf-8')
        (self.case.root / 'workspace' / 'attachment').write_text('UNREAD-ATTACHMENT', encoding='utf-8')
        self.case.review = lambda view: {'decisions':[{'subject':'target-1',
            'conclusion':'complete', 'findings':[{'severity':'required',
                'message':'Check the observation boundary.'}], 'evidence':[], 'limitations':[]}]}
        self.case._inspect(['note.dat'], [])
        delivery = _action('deliver_result', summary='The checked boundary.', artifacts=['note.dat', 'attachment'])
        for bound in (False, True):
            with self.subTest(bound=bound):
                calculation = {}
                def review(view):
                    if not calculation:
                        spec = next(tool['function'] for tool in self.case.inspection_requests[-1]['tools']
                            if tool['function']['name'] == 'run_process')
                        scope = spec['parameters']['properties']['cwd']['description'].split(
                            'workspace/inspection-work/', 1)[1].split('/', 1)[0]
                        work = 'workspace/inspection-work/' + scope
                        calculation['output'] = work + '/checked.txt'
                        # A path embedded in code is not an owner input digest.
                        # Only the second run declares/binds the subject bytes.
                        return [_action('run_process', cwd=work, argv=[sys.executable, '-c',
                            "from pathlib import Path; "
                            f"value=Path({str(note)!r}).read_text(encoding='utf-8'); "
                            "Path('checked.txt').write_text(str(len(value)), encoding='utf-8')"],
                            inputs=['note.dat'] if bound else [], outputs=[calculation['output']], timeout_sec=5)]
                    if not view['evidence']:
                        return [_action('read_file', path=calculation['output'])]
                    result = self._complete(view)
                    result['reassessed_prior'] = [row['id'] for row in view['prior_required']]
                    return result
                self.case.review = review
                self.case._submit(delivery, approve=lambda *_: True)
                proof, assessment = self._assessment()
                self.assertEqual(assessment['verdict'], 'passed' if bound else 'incomplete')
                self.assertEqual(bool(assessment['reassessed_prior']), bound)
                inspector = self.case.app.operations.inspection
                saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
                self.assertTrue(saved['evidence'])
                self.assertTrue(all(row['path'] != str(note) for row in saved['evidence']))
                if bound:
                    self.assertIn(str(note), assessment['evidence_sha256'])
                    before = self.case.send.call_count
                    note.write_text('Changed after the bound check.\n', encoding='utf-8')
                    cached = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
                        delivery_context=saved['delivery_context'])
                    self.assertFalse(cached.ok)
                    self.assertEqual(self.case.send.call_count, before)

    def test_subject_support_requires_each_pending_current_subject_not_inventory(self):
        from simple_ar.tools.delivery_review import reassessment_has_subject_support

        prior = {'subjects':{'a':'0'*64, 'b':'1'*64}, 'pending_subjects':['a', 'b']}
        current = {'a':'2'*64, 'b':'3'*64}
        # These scopes may be direct windows or authenticated process/visual
        # input bindings. They express provenance, never semantic correctness.
        self.assertFalse(reassessment_has_subject_support(prior, current, [{'unrelated':'4'*64}]))
        self.assertFalse(reassessment_has_subject_support(prior, current, [{'a':'2'*64}]))
        self.assertFalse(reassessment_has_subject_support(prior, current, [{'a':'0'*64, 'b':'3'*64}]))
        self.assertTrue(reassessment_has_subject_support(prior, current, [{'a':'2'*64}, {'b':'3'*64}]))
        self.assertTrue(reassessment_has_subject_support(
            {**prior, 'subjects':{'a':'0'*64}, 'pending_subjects':['a']}, current, [{'a':'2'*64}]))
        self.assertFalse(reassessment_has_subject_support({**prior, 'pending_subjects':[]}, current, []))
        self.assertTrue(reassessment_has_subject_support({'subjects':{}, 'pending_subjects':[]}, {}, []))
        self.assertFalse(reassessment_has_subject_support({'subjects':{}, 'pending_subjects':['a']}, current, []))

    def test_native_supply_requires_the_intact_read_reply_not_identical_prose(self):
        from simple_ar.tools.inspection import _DeliveryInspectionRequest

        _, _, proof = self._scientific_delivery()
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        trace = inspector._closed_source_round(saved, 0)
        history = [{'role':'system', 'content':'unused'}, {'role':'user', 'content':'unused'},
            trace['response'], *[{'role':'tool', 'tool_call_id':row['call_id'],
                'content':json.dumps(row['result'])} for row in trace['results']]]
        request = _DeliveryInspectionRequest.model_validate(saved['arguments'])
        _, valid = inspector._working_messages(saved, request, history, [], self.case.services.llm_client)
        native = [row for row in valid['selected_records'] if row['status'] == 'native'
            and row['source'].get('kind') == 'source_text']
        expected = {(f"tools/inspection/{proof['identity']}/sources-0.json#results/{index}",
            f'data.text_windows.{number}.text')
            for index, row in enumerate(trace['results']) if row['tool'] == 'read_file'
            and row['result']['status'] == 'ok'
            for number, _ in enumerate(row['result']['data'].get('text_windows', []))}
        self.assertTrue(expected)
        self.assertEqual({(row['source']['record'], row['source']['field']) for row in native}, expected)
        self.assertTrue(all(row['source']['field'].startswith('data.text_windows.') for row in native))
        altered = deepcopy(history)
        for message in altered[3:]:
            visible = json.loads(message['content'])
            visible['data']['unrelated_note'] = visible['data'].pop('text_windows')
            message['content'] = json.dumps(visible)
        _, projected = inspector._working_messages(saved, request, altered, [], self.case.services.llm_client)
        self.assertFalse(any(row['status'] == 'native' and row['source'].get('kind') == 'source_text'
            for row in projected['selected_records']))

    def test_ordinary_inspection_native_supply_uses_its_saved_contract_and_exact_fields(self):
        from simple_ar.tools.inspection import InspectionRequest
        from tests.test_professional_work_loop import _action

        (self.case.root/'workspace'/'native-note.md').write_text('Current original text.\n', encoding='utf-8')
        reads = []
        def review(view):
            if not reads:
                reads.append(True)
                return [_action('read_file', path='native-note.md')]
            return {'findings':[], 'limitations':[]}
        self.case.review = review
        self.case._inspect(['native-note.md'], [])
        inspector = self.case.app.operations.inspection
        result = self.case._ok('inspect_artifacts')
        identity = Path(result['data']['record']).parts[1]
        saved = inspector.store.read_json(inspector._path(identity, 'input'))
        self.assertEqual(saved['native_supply_policy'], 'turns.v1')
        self.assertEqual(saved['working_set_policy'], 'recent.v4')
        trace = inspector._closed_source_round(saved, 0)
        history = [{'role':'system', 'content':'unused'}, {'role':'user', 'content':'unused'},
            trace['response'], *[{'role':'tool', 'tool_call_id':row['call_id'],
                'content':json.dumps(row['result'])} for row in trace['results']]]
        request = InspectionRequest.model_validate(saved['arguments'])
        ledger = self.case.app.ledger.to_dict()
        original = deepcopy(saved)
        _, supplied = inspector._working_messages(saved, request, history, [], self.case.services.llm_client)
        native = [row for row in supplied['selected_records'] if row['status'] == 'native'
            and row['source'].get('kind') == 'source_text']
        self.assertTrue(native)
        self.assertTrue(all(row['source']['field'].startswith('data.text_windows.') for row in native))
        for field in ('sha256', 'next_offset_bytes', 'text'):
            with self.subTest(field=field):
                altered = deepcopy(history)
                for message in altered[3:]:
                    result = json.loads(message['content'])
                    if field == 'sha256':
                        result['data']['sha256'] = '0'*64
                    else:
                        for window in result['data'].get('text_windows', []):
                            window[field] = -1 if field == 'next_offset_bytes' else 'Changed text'
                    message['content'] = json.dumps(result)
                _, rejected = inspector._working_messages(saved, request, altered, [], self.case.services.llm_client)
                self.assertFalse(any(row['status'] == 'native' and row['source'].get('kind') == 'source_text'
                    for row in rejected['selected_records']))
        historical = {key:value for key,value in saved.items() if key != 'native_supply_policy'}
        _, old = inspector._working_messages(historical, request, history, [], self.case.services.llm_client)
        self.assertFalse(any(row['status'] == 'native' and row['source'].get('field', '').startswith(
            'data.text_windows.') for row in old['selected_records']))
        self.assertEqual(saved, original)
        self.assertEqual(self.case.app.ledger.to_dict(), ledger)

    def test_history_is_grouped_without_reference_limit_and_never_adopted(self):
        from simple_ar.tools.inspection import _DeliveryInspectionRequest

        inspector = self.case.app.operations.inspection
        first, second = self.case.inputs / 'many.txt', self.case.inputs / 'other.txt'
        first.write_text('x' * 2000, encoding='utf-8')
        second.write_text('OTHER-HISTORY-BODY', encoding='utf-8')
        references = [{'request':{'path':str(first), 'offset_bytes':i * 8, 'max_bytes':8}}
            for i in range(200)] + [{'request':{'path':str(second), 'max_bytes':18}}]
        delivery = DeliveryRequest(summary='Explain scope.', artifacts=[])
        binding = prepare_delivery_review(self.case.app.workspace, delivery, [],
            lambda: self.case.app.state['requests'], protocol='task.v4')
        saved = {'identity':'navigation', 'delivery_context':binding, 'arguments':DeliveryReview._arguments(binding),
            'targets':[], 'evidence':[], 'prior_required':[], 'user_requests':binding['requests'],
            'execution_protocol':'shared.v1', 'inspection_context_policy':'retained.v1'}
        remaining = [512000]
        with patch.object(inspector, 'material_observations', return_value=references):
            inspector._reuse_observations(saved, remaining)
        entries = saved['source_history']['entries']
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['requested_byte_ranges'], [(0, 1600)])
        self.assertEqual(saved['evidence'], [])
        self.assertEqual(remaining, [512000])
        request = _DeliveryInspectionRequest.model_validate(saved['arguments'])
        messages, projection = inspector._working_messages(saved, request,
            [{'role':'system', 'content':''}, {'role':'user', 'content':''}], [], self.case.services.llm_client)
        wire = json.dumps(messages)
        self.assertNotIn('OTHER-HISTORY-BODY', wire)
        navigation = [row for row in projection['selected_records'] if row['source'].get('field') == 'source_history']
        self.assertEqual(len(navigation), 2)
        self.assertTrue(all(row['status'] == 'working_view' for row in navigation))
        # Withdrawn source authority must not be revived by a retained index.
        self.case.app.workspace.mounts.pop('measurements')
        _, revoked = inspector._working_messages(saved, request,
            [{'role':'system', 'content':''}, {'role':'user', 'content':''}], [], self.case.services.llm_client)
        self.assertFalse(any(row['source'].get('field') == 'source_history'
            for row in revoked['selected_records']))

    def test_unknown_or_public_policy_cannot_enable_applicable_inspection(self):
        inspector = self.case.app.operations.inspection
        arguments = {'goal':'Explain', 'targets':[], 'evidence':[]}
        for context in ({'schema_version':'delivery_review.v3', 'scope_policy':'unknown'}, None):
            result = inspector.call('inspect_artifacts', {**arguments, 'scope_policy':'applicable.v1'},
                'bad-policy', delivery_context=context)
            self.assertFalse(result.ok)
        self.case.send.assert_not_called()


class DemandDrivenDeliveryInspectionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_professional_work_loop import OperationsWorkLoopTests
        self.case = OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._create(goal='Deliver the requested source and supporting appendix. Independently '
            'verify any empirical or numerical conclusions.', delivery_protocol=None)

    def test_inspector_selects_actual_work_without_automatic_inventory_reading_and_reuses_opinion(self):
        from tests.test_professional_work_loop import _action
        self.case._write('tool.py', 'def identity(value):\n    return value\n')
        unrelated = '# Bundled context, not a requested behavioral claim.\n' * 2000
        self.case._write('appendix.py', unrelated)
        def review(view):
            self.assertEqual(view['proposed_delivery']['scope_policy'], 'applicable.v2')
            self.assertEqual(len(view['proposed_delivery']['artifacts']), 2)
            self.assertNotIn(unrelated, json.dumps(self.case.inspection_requests[-1]['messages']))
            if not view['evidence']:
                return [_action('read_file', path='workspace/tool.py')]
            return {'conclusion':'complete', 'findings':[],
                'evidence':[row['subject'] for row in view['evidence']], 'limitations':[],
                'applicability':'The bounded source behavior is in scope; the appendix adds no behavioral or empirical claim.',
                'investigations':[]}
        self.case.review = review
        request = {'summary':'Source with bundled appendix', 'artifacts':['tool.py','appendix.py']}
        self.case._submit(_action('deliver_result', **request))
        self.assertEqual(self.case.app.session.manifest.status, 'completed')
        self.assertEqual(len(self.case.inspection_requests), 2)
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        self.assertEqual(saved['context_checkpoint_policy'], 'economic.v3')
        self.assertEqual({row['path'] for row in saved['evidence']},
            {str(self.case.root/'workspace/tool.py')})
        calls = self.case.send.call_count
        reused = self.case.app.operations.delivery.review_delivery(request, 'reuse-current-opinion', may_start=True)
        self.assertEqual(reused['identity'], proof['identity'])
        self.assertEqual(self.case.send.call_count, calls)
        (self.case.root/'workspace/tool.py').write_text('def identity(value):\n    return None\n')
        self.assertTrue(self.case.app.operations.delivery.issues(self.case._observations(), request))
        self.assertEqual(self.case.send.call_count, calls)

    def test_unperformed_independent_calculation_remains_incomplete_with_partial_exit(self):
        from tests.test_professional_work_loop import _action
        self.case._write('analysis', 'The observed sample mean is 2.0.\n')
        def review(view):
            if not view['evidence']:
                return [_action('read_file', path='workspace/analysis')]
            evidence = [row['subject'] for row in view['evidence']]
            return {'conclusion':'incomplete', 'findings':[], 'evidence':evidence,
                'limitations':['Independent calculation is unfinished.'],
                'applicability':'The numerical conclusion requires independent calculation from original observations.',
                'investigations':[{'claim':'The sample mean is supported by the observations.',
                    'method':'Read the statement; independent calculation not performed.',
                    'evidence':evidence, 'reasoning':'The producer statement alone does not verify the mean.',
                    'outcome':'unresolved'}]}
        self.case.review = review
        request = {'summary':'Sample mean', 'artifacts':['analysis']}
        self.case._submit(_action('deliver_result', **request))
        self.case._not_delivered()
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'incomplete')
        calls = self.case.send.call_count
        self.case._submit(_action('deliver_result', **request,
            unresolved=['Independent calculation is unfinished.']))
        self.assertEqual(self.case.app.session.manifest.status, 'partial')
        self.assertEqual(self.case.send.call_count, calls)


class IndependentDeliveryApplicabilityTests(unittest.TestCase):
    """Independent applicability on the existing delivery path; not scientific QA."""

    def setUp(self):
        from tests.test_professional_work_loop import OperationsWorkLoopTests
        self.case = OperationsWorkLoopTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case._create(goal='Explain the requested scope and independently verify any empirical '
            'or numerical assertions in the delivered work.')

    def _complete(self, **kwargs):
        from tests.test_professional_work_loop import _action
        return self.case._submit(_action('deliver_result',
            summary='Requested explanation', artifacts=['note.md']), **kwargs)

    def test_candidate_report_enters_one_review_and_retains_owner_conclusion(self):
        from tests.test_professional_work_loop import _action
        from simple_ar.tools.workspace import RetainedDelivery
        # Report identity is explicit, independent of filename extensions.
        delivery = {'summary':'Requested candidate explanation', 'artifacts':['explanation'],
            'report':{'status':'candidate', 'manuscript':'explanation'}}
        with patch.object(self.case.app, '_validated_report_delivery',
                side_effect=AssertionError('Ordinary work must not acquire historical Writer control')):
            self.case._submit(_action('write_file', path='explanation',
                content='Only the supplied observations are in scope.\n', delivery=delivery))
        self.assertEqual(self.case.app.session.manifest.status, 'completed')
        self.assertEqual(len(self.case.inspection_requests), 1)
        saved = self.case.app.session.store.read_json('delivery.json')
        self.assertEqual(saved['report']['status'], 'candidate')
        proof = self.case._last('write_file', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['binding']['claim']['report']['status'], 'candidate')
        self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'passed')
        calls = self.case.send.call_count
        self.case._reload()
        reader = RetainedDelivery(self.case.root)
        self.assertTrue(reader.reusable_materials())
        self.assertEqual(recorded_delivery_review(reader)['verdict'], 'passed')
        self.assertEqual(self.case.send.call_count, calls)
        changed = deepcopy(saved)
        changed['report']['status'] = 'canonical'
        issues = self.case.app.operations.delivery.issues(
            self.case.app._operation_observations(), changed)
        self.assertTrue(issues)  # Relabeling is not a matching owner opinion.
        self.assertEqual(self.case.send.call_count, calls)

    def test_candidate_report_required_finding_blocks_both_edition_labels_but_allows_partial(self):
        from tests.test_professional_work_loop import _action
        self.case._write('explanation', 'The requested explanation is absent.\n')
        self.case.review = lambda view: {'conclusion':'incomplete',
            'findings':[{'severity':'required', 'message':'Supply the requested explanation.',
                'artifacts':[]}], 'evidence':[], 'limitations':[],
            'applicability':'The exact requested explanation has not been delivered.',
            'investigations':[]}
        delivery = {'summary':'Requested candidate explanation', 'artifacts':['explanation'],
            'report':{'status':'candidate', 'manuscript':'explanation'}}
        self.case._submit(_action('deliver_result', **delivery))
        self.case._not_delivered()
        self.assertEqual(len(self.case.inspection_requests), 1)
        calls = self.case.send.call_count
        for edition in ('candidate', 'canonical'):
            with self.subTest(edition=edition):
                claim = {**delivery, 'report':{**delivery['report'], 'status':edition}}
                self.assertTrue(self.case.app.operations.delivery.issues(
                    self.case.app._operation_observations(), claim))
        self.assertEqual(self.case.send.call_count, calls)
        self.case._submit(_action('deliver_result', **delivery,
            unresolved=['Supply the requested explanation.']))
        self.assertEqual(self.case.app.session.manifest.status, 'partial')
        self.assertEqual(len(self.case.inspection_requests), 1)

    def test_edited_process_markdown_reaches_applicable_review_without_publication(self):
        import sys
        from tests.test_professional_work_loop import _action
        self.case._submit(_action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; Path('note.md').write_text('Original wording.\\n')"],
            outputs=['note.md'], timeout_sec=5), approve=lambda *_: True)
        self.case._ok('run_process')
        digest = self.case._read('note.md')['sha256']
        self.case._submit(_action('edit_file', path='note.md', expected_sha256=digest,
            old_text='Original wording.', new_text='Revised explanation.'))
        self.case._ok('edit_file')
        operations = self.case.app.operations
        self.assertEqual(operations.config['professional_operations'], 'work.v5')
        self.assertEqual(operations.config['delivery_review_protocol'], 'task.v4')
        delivery = {'summary':'Requested explanation', 'artifacts':['note.md']}
        # Same real stale process record: saved contracts retain their gate.
        for protocol in ('task.v1', 'task.v2', 'task.v3'):
            with self.subTest(protocol=protocol), patch.dict(operations.config,
                    {'delivery_review_protocol':protocol}):
                issues = operations.delivery.issues(self.case.app._operation_observations(), delivery)
                self.assertTrue(any('outputs changed' in issue for issue in issues), issues)

        def review(view):
            self.assertEqual(view['proposed_delivery']['scope_policy'], 'applicable.v1')
            source, = self.case._inspection_sources(view, 'note.md')
            self.assertIsNotNone(source)
            self.assertIn('Revised explanation.', ''.join(row['text'] for row in source['text_windows']))
            return {'conclusion':'complete', 'findings':[], 'evidence':[source['subject']],
                'limitations':['Controlled prose decision; no scientific certification.'],
                'applicability':'This wording revision contains no empirical or numerical claim.',
                'investigations':[]}
        self.case.review = review
        with (patch.object(operations.publication, 'conversion_inputs',
                    side_effect=AssertionError('task.v4 has no publication exemption')),
              patch.object(operations.workspace, 'check_artifact_producers',
                    wraps=operations.workspace.check_artifact_producers) as producer_check):
            self._complete()
            producer_check.assert_not_called()
        self.assertEqual(self.case.app.session.manifest.status, 'completed')
        self.assertEqual(len(self.case.inspection_requests), 1)
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertEqual(proof['binding']['artifacts'], operations.workspace.artifact_hashes(
            [str(operations.workspace.resolve('note.md'))]))
        self.assertFalse((self.case.root/'tools/publications').exists())
        with self.assertRaisesRegex(ValueError, 'outputs changed'):
            operations.workspace.check_artifact_producers(['note.md'])
        # A changed current version cannot reuse the independent opinion.
        (self.case.root/'workspace/note.md').write_text('Another revision.\n', encoding='utf-8')
        self.assertTrue(operations.delivery.issues(self.case.app._operation_observations(), delivery))

    def test_explicit_source_and_table_adoption_keeps_original_input_closure(self):
        import sys
        from tests.test_professional_work_loop import _action
        from simple_ar.tools.producer_declarations import delivery_evidence
        self.case._write('input.txt', 'original\n')
        self.case._submit(_action('run_process', argv=[sys.executable, '-c',
            "from pathlib import Path; data=Path('input.txt').read_text(); "
            "Path('source.py').write_text('# '+data); Path('table.csv').write_text('value\\n'+data)"],
            inputs=['input.txt'], outputs=['source.py', 'table.csv'], timeout_sec=5),
            approve=lambda *_: True)
        self.case._ok('run_process')
        app = self.case.app
        owner = SimpleNamespace(root=self.case.root, workspace=app.workspace,
            ledger=app.ledger, session_id=app.session.manifest.session_id)
        for path in ('source.py', 'table.csv'):
            self.assertEqual(delivery_evidence(owner, app._operation_observations(),
                {'summary':'Explicitly adopt the computation', 'artifacts':[path]}), ([], []))
        digest = self.case._read('input.txt')['sha256']
        self.case._submit(_action('write_file', path='input.txt', content='changed\n',
            expected_sha256=digest))
        self.case._ok('write_file')
        for path in ('source.py', 'table.csv'):
            with self.subTest(subject=path):
                delivery = {'summary':'Explicitly adopt the computation', 'artifacts':[path]}
                with self.assertRaisesRegex(ValueError, 'producer inputs/outputs changed'):
                    delivery_evidence(owner, app._operation_observations(), delivery)

    def test_current_delivery_is_presupplied_but_does_not_finish_unperformed_science(self):
        from tests.test_professional_work_loop import _action
        self.case._write('note.md', 'The supplied sample mean is 9.0.\n')
        self.case._write('another-object', 'A producer result is not an independent observation.\n')
        def review(view):
            sources = self.case._inspection_sources(view, 'note.md', 'another-object')
            self.assertTrue(all(row is not None for row in sources))
            self.assertIn('9.0', ''.join(window['text'] for window in sources[0]['text_windows']))
            return {'conclusion':'complete', 'findings':[],
                'evidence':[row['subject'] for row in sources], 'limitations':[],
                'applicability':'The numeric assertion requires checking original observations.',
                'investigations':[{'claim':'The sample mean is 9.0.',
                    'method':'Not performed: original observations have not been investigated.',
                    'reasoning':'Supplied producer artifacts cannot independently validate this claim.',
                    'evidence':[], 'outcome':'unresolved'}]}
        self.case.review = review
        self.case._submit(_action('deliver_result', summary='Producer claims a checked result.',
            artifacts=['another-object', 'note.md']))
        self.assertEqual(len(self.case.inspection_requests), 1)
        self.assertNotEqual(self.case.app.session.manifest.status, 'completed')
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        assessment = proof['result']['metadata']['delivery_assessment']
        self.assertEqual((assessment['coverage'], assessment['verdict']), ('partial', 'incomplete'))

    def test_simple_claim_is_scoped_by_one_independent_decision_and_reused(self):
        from tests.test_professional_work_loop import _action
        def review(view):
            self.assertFalse(view['evidence'])
            return {'conclusion':'complete', 'findings':[], 'evidence':[],
                'limitations':[], 'reassessed_prior':[],
                'applicability':'The exact request and direct explanation require no empirical '
                    'assertion or artifact investigation.', 'investigations':[]}
        self.case.review = review
        delivery = _action('deliver_result', summary='Only supplied observations are in scope.', artifacts=[])
        self.case._submit(delivery)
        self.assertEqual(self.case.app.session.manifest.status, 'completed')
        self.assertEqual(len(self.case.inspection_requests), 1)
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        before = self.case.send.call_count
        self.case._reload()
        owner = self.case.app.operations.delivery_review
        reused = owner.review(proof['binding'], 'same-scope', may_start=True)
        self.assertEqual(reused['identity'], proof['identity'])
        self.assertEqual(self.case.send.call_count, before)
        self.assertEqual(reused['result']['metadata']['delivery_assessment']['applicability'],
            proof['result']['metadata']['delivery_assessment']['applicability'])

        # Delivery uses the same owner-verified opinion before requiring a
        # connection for another review. A changed claim is not that opinion.
        from simple_ar.core.capabilities import ArtifactStore
        delivery = self.case.app.session.store.read_json('delivery.json')
        call = {'id':'reuse-call', 'function':{'name':'deliver_result',
            'arguments':json.dumps(delivery, ensure_ascii=False)}}
        payload = ToolResult(tool_name='deliver_result', data=deepcopy(delivery)).model_dump(mode='json')
        store = ArtifactStore(self.case.root/'attempts'/'reuse-probe')
        before_ledger = self.case.app.ledger.to_dict()
        with patch.object(self.case.app, '_client', side_effect=RuntimeError('Connection unavailable')) as client:
            self.case.app._bind_delivery(call, payload, store, start_review=True)
        client.assert_not_called()
        self.assertEqual(payload['metadata']['delivery_review']['identity'], proof['identity'])
        self.assertEqual(self.case.app.ledger.to_dict(), before_ledger)

        changed = {**delivery, 'summary':'A different completion claim'}
        call['function']['arguments'] = json.dumps(changed, ensure_ascii=False)
        other = ToolResult(tool_name='deliver_result', data=changed).model_dump(mode='json')
        with patch.object(self.case.app, '_client', side_effect=RuntimeError('Connection unavailable')) as client:
            self.case.app._bind_delivery(call, other,
                ArtifactStore(self.case.root/'attempts'/'changed-probe'), start_review=True)
        client.assert_called_once()
        self.assertNotIn('delivery_review', other['metadata'])
        self.assertIn('Connection unavailable', other['metadata']['delivery_review_error'])

    def test_unfinished_delivery_inspection_rebinds_without_starting_or_replaying_work(self):
        from tests.test_professional_work_loop import _action

        # Retain the fixture's actual observed manuscript support; an empty
        # evidence declaration cannot certify a nonempty artifact on resume.
        self.case._write('explanation', 'Only supplied observations are in scope.\n')
        self.case._submit(_action('deliver_result', summary='Current candidate explanation',
            artifacts=['explanation'], report={'status':'candidate', 'manuscript':'explanation'}),
            should_stop=lambda: bool((self.case.app.state['pending'] or {}).get('inspection')))
        app = self.case.app
        pending = deepcopy(app.state['pending'])
        self.assertEqual(app.session.manifest.status, 'paused')
        self.assertIsNotNone(pending.get('inspection'))
        self.assertFalse((self.case.root/'delivery.json').exists())
        owner = app.operations.inspection
        self.assertFalse(owner.store.exists(owner._path(pending['inspection'], 'result')))
        original = app.session.store.read_json(f"attempts/{pending['attempt']}/native_result.json")
        self.assertEqual(original['metadata']['delivery_review']['result']['data'],
            {'inspection_pending':pending['inspection']})
        input_before = owner.store.resolve(owner._path(pending['inspection'], 'input')).read_bytes()
        ledger_before, calls = app.ledger.to_dict(), self.case.send.call_count
        with (patch.object(app, '_client', side_effect=AssertionError('Rebinding is not a model call')),
              patch.object(owner, 'advance', side_effect=AssertionError('Do not advance an inspection during rebinding'))):
            rebound = app._bind_delivery(pending['call'], deepcopy(original),
                ArtifactStore(self.case.root/'attempts'/pending['attempt']), start_review=False)
        self.assertEqual(rebound['metadata']['delivery_review'], original['metadata']['delivery_review'])
        self.assertEqual(app.state['pending'], pending)
        self.assertEqual(owner.store.resolve(owner._path(pending['inspection'], 'input')).read_bytes(), input_before)
        self.assertEqual(app.ledger.to_dict(), ledger_before)
        self.assertEqual(self.case.send.call_count, calls)

        # Existing pending work still rejects a changed/unavailable connection
        # before continuing either the inspection or its saved child actions.
        with patch.object(app, '_client', side_effect=ValueError('Named model connection differs')) as client:
            blocked = app.run(progress=lambda _:None)
        self.assertEqual(blocked['status'], 'paused')
        self.assertIn('Named model connection differs', blocked['status_reason'])
        client.assert_called_once()
        self.assertEqual(app.state['pending'], pending)
        self.assertEqual(app.ledger.to_dict(), ledger_before)
        self.assertEqual(self.case.send.call_count, calls)

        self.case._reload()
        self.case._run()
        self.assertEqual(self.case.app.session.manifest.status, 'completed', self.case.app.result_snapshot())
        self.assertEqual(len(self.case.inspection_requests), 1)
        self.assertEqual(self.case.app.ledger.to_dict()['entries'][:len(ledger_before['entries'])],
            ledger_before['entries'])

    def test_producer_empty_checklist_cannot_waive_inspection_or_change_frozen_tools(self):
        from tests.test_professional_work_loop import _action
        frozen = deepcopy(self.case.app.state['config']['tool_completions'])
        self.assertNotIn('complete_work', frozen)
        self.assertIsNotNone(self.case.app.gateway.registry.get('deliver_result'))
        self.assertIsNotNone(self.case.app.gateway.registry.get('write_file').completion)
        self.case._submit(_action('deliver_result', summary='A numerical assertion',
            artifacts=[], checks=[]))
        self.assertNotEqual(self.case.app.session.manifest.status, 'completed')
        self.assertFalse(self.case.inspection_requests)  # Invalid producer schema, not a waiver.
        self.case._reload()
        self.assertEqual(self.case.app.state['config']['tool_completions'], frozen)
        self.case.review = lambda view: {'conclusion':'incomplete', 'findings':[],
            'evidence':[], 'limitations':[], 'reassessed_prior':[],
            'applicability':'The empirical assertion needs original observations; '
                'the producer declaration cannot waive independent verification.',
            'investigations':[{'claim':'The numerical assertion is correct.',
                'method':'Not performed: original observations are still needed.',
                'reasoning':'The declaration supplies no scientific verification.',
                'evidence':[], 'outcome':'unresolved'}]}
        self.case._submit(_action('deliver_result',
            summary='The numerical result is correct. No checks needed; checks=[].', artifacts=[]))
        self.assertEqual(len(self.case.inspection_requests), 1)
        self.assertNotEqual(self.case.app.session.manifest.status, 'completed')
        assessment = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']['result']['metadata']['delivery_assessment']
        self.assertEqual(assessment['investigations'][0]['outcome'], 'unresolved')

    def test_saved_v4_actual_offer_keeps_its_response_contract_without_new_calls(self):
        from tests.test_professional_work_loop import _action
        inspector = self.case.app.operations.inspection
        self.case.review = lambda view: {'conclusion':'complete', 'findings':[],
            'evidence':[], 'limitations':[], 'reassessed_prior':[]}
        # Freeze the previously shipped response offer, not a synthetic task
        # version or a migrated completion map. All journal/usage proofs are real.
        with patch.object(inspector, '_investigation_contract', return_value=False):
            self.case._submit(_action('deliver_result', summary='The requested scope.', artifacts=[]))
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        self.assertFalse(inspector._investigation_contract(saved, offering=True))
        self.assertNotIn('applicability', proof['result']['metadata']['delivery_assessment'])
        calls, ledger = self.case.send.call_count, self.case.app.ledger.to_dict()
        self.case._reload()
        inspector = self.case.app.operations.inspection
        cached = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
            delivery_context=saved['delivery_context'])
        self.assertEqual(cached.model_dump(mode='json'), proof['result'])
        self.assertEqual(self.case.send.call_count, calls)
        self.assertEqual(self.case.app.ledger.to_dict(), ledger)

    def test_independent_investigation_retains_methods_sources_and_unknown_outcome(self):
        self.case._write('note.md', 'The supplied sample mean is 2.\n')
        outcome = ['unresolved']
        calculation = {}

        def review(view):
            import sys
            from tests.test_professional_work_loop import _action
            if not calculation:
                spec = next(tool['function'] for tool in self.case.inspection_requests[-1]['tools']
                    if tool['function']['name'] == 'run_process')
                scope = spec['parameters']['properties']['cwd']['description'].split(
                    'workspace/inspection-work/', 1)[1].split('/', 1)[0]
                work = 'workspace/inspection-work/' + scope
                calculation['output'] = work + '/independent.txt'
                source = str(self.case.inputs / 'observations.csv')
                return [_action('run_process', cwd=work, argv=[sys.executable, '-c',
                    "import csv; from pathlib import Path; "
                    f"rows=list(csv.DictReader(Path({source!r}).open(encoding='utf-8'))); "
                    "values=[float(row['value']) for row in rows]; "
                    "Path('independent.txt').write_text(str(sum(values)/len(values)), encoding='utf-8')"],
                    inputs=['measurements/observations.csv'], outputs=[calculation['output']], timeout_sec=5)]
            sources = self.case._inspection_sources(view, calculation['output'])
            if sources[0] is None:
                return [_action('read_file', path=calculation['output'])]
            evidence = [sources[0]['subject']]
            return {'conclusion':'complete', 'findings':[], 'evidence':evidence,
                'limitations':[], 'reassessed_prior':[], 'investigations':[{
                    'claim':'The supplied sample mean is 2.', 'method':'Independently recompute sum and count from the original CSV in a bound process.',
                    'reasoning':'The observed values are 1, 2, 3; (1+2+3)/3 is 2. '
                        'Controlled test judgment; no scientific correctness certification.',
                    'evidence':evidence, 'outcome':outcome[0]}]}

        self.case.review = review
        self._complete(approve=lambda *_: True)
        self.assertNotEqual(self.case.app.session.manifest.status, 'completed')
        first = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        assessment = first['result']['metadata']['delivery_assessment']
        self.assertEqual(assessment['verdict'], 'incomplete')
        self.assertEqual(assessment['investigations'][0]['outcome'], 'unresolved')
        self.assertTrue(any('Unfinished independent' in text for text in assessment['limitations']))
        outcome[0] = 'supported'
        calculation.clear()
        self._complete(approve=lambda *_: True)
        proof = self.case._last('deliver_result', reviewed=True)['metadata']['delivery_review']
        self.assertNotEqual(first['identity'], proof['identity'])
        self.assertEqual(proof['result']['metadata']['delivery_assessment']['verdict'], 'passed')
        self.assertIn(str(self.case.inputs / 'observations.csv'),
            proof['result']['metadata']['delivery_assessment']['evidence_sha256'])
        self.assertEqual(self.case.app.session.manifest.status, 'completed')
        # Reuse must still verify original source versions without another call.
        inspector = self.case.app.operations.inspection
        saved = inspector.store.read_json(inspector._path(proof['identity'], 'input'))
        (self.case.inputs / 'observations.csv').write_text('sample,value\na,100\n', encoding='utf-8')
        calls = self.case.send.call_count
        result = inspector.call('inspect_artifacts', saved['arguments'], proof['identity'],
            delivery_context=saved['delivery_context'])
        self.assertFalse(result.ok)
        self.assertEqual(self.case.send.call_count, calls)

    def test_schema_opinion_cannot_substitute_for_performed_investigation(self):
        from simple_ar.tools.inspection import InspectionToolGateway
        inspector = self.case.app.operations.inspection
        saved = {'delivery_context': {'schema_version':'delivery_review.v3',
            'scope_policy':'applicable.v1', 'artifacts': {}},
            'finding_policy':'linked.v1', 'prior_required':[], 'targets':[], 'evidence':[]}
        response = {'conclusion':'complete', 'findings':[], 'limitations':[],
            'applicability':'The numerical assertion needs independent verification.',
            'evidence':[], 'reassessed_prior':[], 'investigations':[{
                'claim':'An empirical assertion', 'method':'The files exist.',
                'reasoning':'The producer says it passed.', 'evidence':[], 'outcome':'supported'}]}
        with self.assertRaisesRegex(ValueError, 'actual observed evidence'):
            InspectionToolGateway._delivery_decision(inspector, saved, response)
        response['applicability'] = ' '
        with self.assertRaisesRegex(ValueError, 'Independent applicability'):
            InspectionToolGateway._delivery_decision(inspector, saved, response)


if __name__ == '__main__':
    unittest.main()
