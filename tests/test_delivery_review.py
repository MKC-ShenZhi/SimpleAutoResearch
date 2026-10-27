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
from simple_ar.tools.workspace import WorkspaceToolGateway


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
        subject = self.workspace.artifact_hashes(arguments['targets'])
        findings = ([{'severity': 'required', 'message': 'The requested explanation is absent.',
                      'subjects': subject}] if self.verdict == 'needs_revision' else [])
        assessment = {'schema_version': 'artifact_assessment.v1', 'subjects': subject,
            'evidence_sha256': deepcopy(self.sources), 'coverage': self.coverage,
            'verdict': self.verdict, 'findings': findings,
            'limitations': ['Controlled text-review decision; no scientific certification.']}
        result = ToolResult(tool_name=name, metadata={
            'assessments': [] if self.omit_assessment else [assessment],
            'inspection_binding': {'controlled_receipt': identity},
            'capability_artifacts': [{'path': 'inspection/assessments.json'}]})
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
        for assessment in result['metadata']['assessments']:
            versions = {**assessment['subjects'], **assessment['evidence_sha256']}
            if self.workspace.artifact_hashes(versions) != versions:
                raise ValueError('Inspection target/evidence version changed; no replay')
        return ToolResult.model_validate(result)


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
            user_requests=lambda: self.requests, observations=lambda: self.observations)
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

    def test_full_inventory_and_requests_frozen_without_summary_requirements(self):
        proof = self.review()
        self.observe(proof)
        binding = proof['binding']
        self.assertEqual(binding['schema_version'], 'delivery_review.v1')
        self.assertEqual(binding['requests'], self.requests)
        self.assertEqual(binding['formats'], ['markdown'])
        self.assertEqual(set(binding['artifacts']), {
            str(self.workspace.resolve('workspace/bundle/manuscript.md')),
            str(self.workspace.resolve('workspace/bundle/appendix.txt'))})
        self.assertIn(binding['manuscript'], binding['artifacts'])
        self.assertEqual(binding['artifacts'], self.workspace.artifact_hashes(binding['artifacts']))
        _, arguments, identity, context = self.inspector.calls[0]
        self.assertEqual(identity, 'parent-1')
        self.assertEqual(arguments['targets'], [binding['manuscript']])
        self.assertEqual(arguments['evidence'], [])
        self.assertEqual(context, binding)
        self.assertNotIn('summary', binding)
        self.assertEqual(self.owner.issues(self.observations, self.delivery), [])
        self.assertEqual(self.review(), proof)
        self.assertEqual(len(self.inspector.calls), 1)
        self.delivery['summary'] = '不同措辞，不是新用户反馈'
        reused = self.review('parent-2')
        self.assertEqual(reused, proof)
        self.assertEqual(len(self.inspector.calls), 1)
        self.assertEqual(self.inspector.cached[-1], 'parent-1')
        # Mutating the callback list cannot mutate a previously returned binding.
        self.requests.append('请解释补充材料的适用范围。')
        self.assertEqual(len(binding['requests']), 1)

    def test_local_pass_or_unbound_legacy_review_cannot_prove_delivery(self):
        proof = self.review()
        local = deepcopy(proof['result'])
        self.assertTrue(self.owner.issues([local], self.delivery))
        saved = self.inspector.store.read_json(self.inspector._path('parent-1', 'input'))
        saved.pop('delivery_context')
        self.inspector.store.write_json(self.inspector._path('parent-1', 'input'), saved)
        self.observe(proof)
        self.assertTrue(self.owner.issues(self.observations, self.delivery))
        rejected = self.review(may_start=False)
        self.assertIn('error', rejected)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_all_bundle_and_requirement_changes_invalidate_proof(self):
        for change in ('feedback', 'appendix', 'manuscript', 'new-file', 'formats', 'roots'):
            with self.subTest(change=change):
                proof = self.review('before-' + change)
                observed = self.observe(proof)
                if change == 'feedback':
                    self.requests.append('补充适用范围。')
                elif change == 'formats':
                    self.delivery['report']['formats'].append('tex')
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
                    forged['result']['metadata']['assessments'][0]['limitations'] = []
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
        tampered['metadata']['assessments'][0]['limitations'] = []
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
        self.assertEqual(metadata['assessments'][0]['verdict'], 'needs_revision')
        self.assertIn('inspection_binding', metadata)
        self.assertIn('capability_artifacts', metadata)
        self.assertTrue(self.owner.issues([{'metadata': {'delivery_review': rejected}}], self.delivery))
        self.review(may_start=False)
        self.assertEqual(len(self.inspector.calls), 1)

    def test_required_partial_incomplete_and_missing_assessments_cannot_pass(self):
        for verdict, coverage, missing in (
                ('needs_revision', 'complete', False), ('incomplete', 'complete', False),
                ('incomplete', 'partial', False), ('passed', 'complete', True)):
            with self.subTest(verdict=verdict, coverage=coverage, missing=missing):
                self.inspector.verdict, self.inspector.coverage = verdict, coverage
                self.inspector.omit_assessment = missing
                proof = self.review(f'review-{verdict}-{coverage}-{missing}')
                # Completed negative opinions remain real results, not thrown-away errors.
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
                self.assertTrue(self.owner.issues(self.observations, self.delivery))
                self.assertIsNone(self.review('next-' + verdict, may_start=False))
                calls = len(self.inspector.calls)
                self.inspector.verdict = 'passed'
                current = self.review('next-' + verdict)
                self.assertEqual(len(self.inspector.calls), calls + 1)
                self.assertNotEqual(current['identity'], prior['identity'])
                self.observe(current)
                self.assertEqual(self.owner.issues(self.observations, self.delivery), [])

    def test_partial_plain_candidate_and_missing_manuscript_do_not_trigger(self):
        for report, unresolved in ((None, []), (self.delivery['report'], ['unfinished']),
                ({'status': 'candidate'}, []), ({'status': 'canonical'}, [])):
            with self.subTest(report=report, unresolved=unresolved):
                delivery = {**self.delivery, 'report': report, 'unresolved': unresolved}
                self.assertIsNone(self.owner.review(delivery, 'unused', may_start=True))
                self.assertEqual(self.owner.issues([], delivery), [])
        self.assertEqual(self.inspector.calls, [])


if __name__ == '__main__':
    unittest.main()
