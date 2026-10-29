"""Delivery scope/receipt contracts, not a test of model scientific judgment.

The inspector below is explicitly controlled: saved inputs/results are real JSON,
while completion/usage/integrity rejection is injected at its _cached boundary.
Workspace resolution, directory expansion, hashing and assessment checks are real.
"""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.delivery_review import DeliveryReview
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

    def feedback_observation(self, identity, claimed):
        # Feedback verifies the original receipt, not the applicability of its
        # old file versions to a revision. Acceptance still uses _cached.
        result = self.store.read_json(self._path(identity, 'result'))
        if result != self.expected[identity] or claimed != result:
            raise ValueError('Feedback differs from actual receipt')
        saved = self.store.read_json(self._path(identity, 'input'))
        return {**saved, 'result': result, 'opinion_available': True,
                'reviewed_subjects': self.workspace.artifact_hashes(saved['arguments']['targets'])}


class DeliveryReviewTests(unittest.TestCase):
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
            user_requests=lambda: self.requests, observations=lambda: self.observations,
            protocol='task.v3')
        self.delivery = {'summary': '交付说明', 'artifacts': ['workspace/bundle'],
            'report': {'status': 'canonical', 'manuscript': 'workspace/bundle/manuscript.md',
                       'formats': ['markdown']}}

    def observe(self, proof):
        observed = {'tool_name': 'deliver_result', 'status': 'ok',
                    'metadata': {'delivery_review': deepcopy(proof)}}
        self.observations.append(observed)
        return observed

    def review(self, identity='parent-1', may_start=True):
        return self.owner.review(self.delivery, identity, may_start=may_start)

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
        self.assertEqual(self.owner.issues(self.observations, self.delivery), [])
        self.assertEqual(self.review(), proof)
        self.assertEqual(len(self.inspector.calls), 1)
        reused = self.review('parent-2')
        self.assertEqual(reused, proof)
        self.assertEqual(len(self.inspector.calls), 1)
        self.assertEqual(self.inspector.cached[-1], 'parent-1')
        self.delivery['artifacts'] = ['bundle/核查代码.py', 'workspace/bundle/appendix.txt',
                                      'bundle/manuscript.md', 'bundle/appendix.txt']
        self.assertEqual(self.review('aliases'), proof)
        self.assertEqual(self.owner.issues(self.observations, self.delivery), [])
        self.assertEqual(len(self.inspector.calls), 1)
        feedback = self.owner.feedback_observation(proof)
        self.assertEqual(feedback['result'], proof['result'])
        self.assertEqual(feedback['reviewed_inventory'], binding['artifacts'])
        self.assertEqual(feedback['reviewed_subjects'], {})
        self.delivery['summary'] = '改变完成主张，应重新绑定'
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
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
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
        self.assertEqual(summary['result']['metadata']['delivery_assessment']['findings'][0]['subjects'], {})
        self.inspector.verdict = 'passed'
        self.delivery['summary'] = '按原问题修正后的直接回答。'
        self.observe(self.review('corrected-answer'))
        self.assertEqual(self.owner.issues(self.observations, self.delivery), [])

    def test_local_pass_or_unbound_legacy_review_cannot_prove_delivery(self):
        proof = self.review()
        local = deepcopy(proof['result'])
        self.assertTrue(self.owner.issues([local], self.delivery))
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
        self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': {
            **proof, 'result': forged}}}], self.delivery))
        self.inspector.store.write_json(path, proof['result'])
        self.inspector.expected['parent-1'] = deepcopy(proof['result'])
        # A well-formed package pass for a subset cannot certify this bundle.
        subset = deepcopy(proof['result'])
        subset['metadata']['delivery_assessment']['artifacts'].pop(next(iter(package['artifacts'])))
        self.inspector.store.write_json(path, subset)
        self.inspector.expected['parent-1'] = deepcopy(subset)
        self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': {
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
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
        rejected = self.review(may_start=False)
        self.assertIn('error', rejected)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_complete_input_scope_does_not_upgrade_or_borrow_historical_review(self):
        self.delivery['report']['formats'] = ['tex', 'markdown']
        histories = []
        for protocol in ('task.v1', 'task.v2'):
            owner = DeliveryReview(self.workspace, self.inspector, lambda: self.requests,
                observations=lambda: self.observations, protocol=protocol)
            proof = owner.review(self.delivery, protocol, may_start=True)
            self.assertEqual(proof['binding']['schema_version'], protocol.replace('task', 'delivery_review'))
            self.assertEqual(self.inspector.calls[-1][1]['targets'], [proof['binding']['manuscript']])
            self.observe(proof)
            histories.append((owner, proof))
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
        proof = self.review('current')
        self.assertEqual(proof['binding']['schema_version'], 'delivery_review.v3')
        self.assertEqual(len(self.inspector.calls), 3)
        for owner, historical in histories:
            self.assertEqual(owner.review(self.delivery, historical['identity'], may_start=False), historical)
            self.assertEqual(self.inspector.store.read_json(self.inspector._path(historical['identity'], 'input'))
                ['delivery_context'], historical['binding'])
        self.observe(proof)
        self.assertFalse(self.owner.issues(self.observations,self.delivery))
        self.delivery['artifacts'] = ['bundle/appendix.txt', 'workspace/bundle/manuscript.md']
        self.delivery['report']['manuscript'] = 'bundle/manuscript.md'
        self.delivery['report']['formats'] = ['markdown', 'tex', 'markdown']
        self.assertEqual(self.owner.issues(self.observations, self.delivery), [])
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
                self.assertTrue(self.owner.issues([observed], self.delivery))
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
                    self.assertTrue(self.owner.issues([self.observe(rejected)], self.delivery))
                self.assertEqual(len(self.inspector.calls), 1)

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
                self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': forged}}], self.delivery))
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
        self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': proof}}], self.delivery))
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
        self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': rejected}}], self.delivery))
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
                self.owner = DeliveryReview(self.workspace, self.inspector, lambda:self.requests,
                    observations=lambda:self.observations, protocol='task.v3')
                for may_start in (False, True):
                    self.assertEqual(self.review(identity, may_start=may_start), proof)
                self.assertEqual(self.owner.issues([observed], self.delivery), ['Delivery review failed: ' + cause])
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
                issues = self.owner.issues([observed], self.delivery)
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
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
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
                self.assertTrue(self.owner.issues(self.observations, self.delivery))
                self.assertIsNone(self.review('next-' + verdict, may_start=False))
                calls = len(self.inspector.calls)
                self.inspector.verdict = 'passed'
                current = self.review('next-' + verdict)
                self.assertEqual(len(self.inspector.calls), calls + 1)
                self.assertNotEqual(current['identity'], prior['identity'])
                self.observe(current)
                self.assertEqual(self.owner.issues(self.observations, self.delivery), [])

    def test_partial_skips_review_and_legacy_plain_scope_is_unchanged(self):
        partial = {**self.delivery, 'unresolved': ['unfinished']}
        self.assertIsNone(self.owner.review(partial, 'partial', may_start=True))
        self.assertEqual(self.owner.issues([], partial), [])
        self.assertEqual(self.inspector.calls, [])
        self.inspector.verdict = 'needs_revision'
        prior = self.review('known-defect')
        self.observe(prior)
        partial['unresolved'].append('The requested explanation is absent.')
        self.assertIsNone(self.owner.review(partial, 'partial-after-review', may_start=True))
        self.assertEqual(self.owner.issues(self.observations, partial), [])
        self.assertEqual(partial['unresolved'], ['unfinished', 'The requested explanation is absent.'])
        feedback = self.owner.feedback_observation(prior)
        self.assertEqual(feedback['result']['metadata']['delivery_assessment']['verdict'], 'needs_revision')
        for protocol in ('task.v1', 'task.v2'):
            owner = DeliveryReview(self.workspace, self.inspector, lambda: self.requests, protocol=protocol)
            for report, unresolved in ((None, []), (self.delivery['report'], ['unfinished']),
                    ({'status': 'candidate'}, []), ({'status': 'canonical'}, [])):
                with self.subTest(protocol=protocol, report=report, unresolved=unresolved):
                    delivery = {**self.delivery, 'report': report, 'unresolved': unresolved}
                    self.assertIsNone(owner.review(delivery, 'unused', may_start=True))
                    self.assertEqual(owner.issues([], delivery), [])
        self.assertEqual(len(self.inspector.calls), 1)


if __name__ == '__main__':
    unittest.main()
