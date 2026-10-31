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

from simple_ar.app.operation_tools import delivery_requires_review, prepare_delivery_review
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.delivery_review import DeliveryReview, recorded_delivery_review
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
        manifest = {'status': 'completed'}
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
        with patch('simple_ar.tools.materials.accepted_review_journal',
                return_value={'record': item['review_result'], 'receipt': receipt}):
            opinion = recorded_delivery_review(reader)
            self.assertEqual(opinion['limitations'], assessment['limitations'])
            self.assertIn('not proof', opinion['scope'])
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
        proof = self.review('pending-review')
        observed = self.pending_envelope(proof)
        self.assertEqual(self.concerns(), pending)
        self.assertIn('opinion record', '; '.join(self.issues(self.observations, self.delivery)))
        observed['review_record'] = 'attempts/not-accepted/native_result.json'
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion',
                   side_effect=ValueError('No accepted opinion action')):
            self.assertEqual(self.concerns(), pending)
            self.assertTrue(self.issues(self.observations, self.delivery))

    def test_accepted_pending_recovery_and_later_findings_use_one_reducer(self):
        self.local()
        self.inspector.reassess = True
        proof = self.review('shared-review')
        observed = self.pending_envelope(proof, record='attempts/accepted-opinion/native_result.json')
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion') as accepted:
            self.assertEqual(self.concerns(), [])
            self.assertEqual(self.issues(self.observations, self.delivery), [])
            self.assertTrue(accepted.called)
            self.assertEqual(accepted.call_args.kwargs['expected'], proof['result'])
            self.local('later-required')
            self.assertEqual(len(self.concerns()), 1)
            self.assertTrue(self.issues(self.observations, self.delivery))
            count = len(self.inspector.calls)
            self.assertEqual(self.review('shared-review', may_start=False)['identity'], 'shared-review')
            self.assertEqual(len(self.inspector.calls), count)
        self.assertEqual(observed['review_record'], 'attempts/accepted-opinion/native_result.json')

    def test_accepted_pending_historical_required_survives_target_revision(self):
        self.inspector.verdict = 'needs_revision'
        proof = self.review('historical-shared')
        self.pending_envelope(proof, record='attempts/accepted-opinion/native_result.json')
        self.inspector.store.write_json('session_manifest.json', {'session_id': 'controlled-session'})
        with patch('simple_ar.tools.professional_outcomes.accepted_scoped_opinion'):
            pending = self.concerns()
            self.assertEqual(len(pending), 1)
            self.workspace.resolve('bundle/manuscript.md').write_text('new version', encoding='utf-8')
            self.assertEqual(self.concerns(), pending)


class InspectionOfferCoverageTests(unittest.TestCase):
    """Exercise the ordinary shared inspector with its existing offline provider."""

    def setUp(self):
        from dataclasses import replace
        from simple_ar.integrations.llm import LLMClient
        from tests.test_professional_work_loop import OperationsWorkLoopTests

        self.case = OperationsWorkLoopTests('runTest')
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.services.llm_client = LLMClient(replace(
            self.case.services.llm_client._settings, context_input_tokens=9000))
        self.case._create(goal='Inspect the report and describe its scope.')

    def _long_report(self):
        head = ('本报告记录观察条件及样本结果并说明解释范围' * 20)[:170] + '\n\n'
        text = head + 'record,value,description\n' + ''.join(
            f'{number},2,observed sample\n' for number in range(1000))
        (self.case.root / 'workspace' / 'report.md').write_text(text, encoding='utf-8')
        self.case.review = lambda view: {'decisions': [{'subject':'target-1',
            'conclusion':'complete', 'findings':[], 'evidence':[], 'limitations':[]}]}
        return text

    def test_complete_read_with_unsupplied_tail_is_partial_and_recovers(self):
        from tests.test_professional_work_loop import _assessments

        text = self._long_report()
        result = self.case._inspect(['report.md'], [])
        self.assertEqual(result['status'], 'ok', result)
        identity = Path(result['data']['record']).parts[1]
        store = self.case.app.session.store
        saved = store.read_json(f'tools/inspection/{identity}/input.json')
        trace = store.read_json(f'tools/inspection/{identity}/sources-0.json')
        view = json.loads(trace['offer']['messages'][1]['content'])
        bodies = [entry['observation'] for entry in view['working_view']['observations']
            if entry['observation'].get('path') == saved['targets'][0]['path']]
        self.assertTrue(saved['targets'][0]['complete'])
        self.assertTrue(bodies)
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


if __name__ == '__main__':
    unittest.main()
