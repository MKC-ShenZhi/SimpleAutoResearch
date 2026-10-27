"""Document producer behavior; task composition and independent review live elsewhere."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.core.budget import BudgetLedger
from simple_ar.research.contracts import DocumentRecord, TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.tools.document_work import DocumentWorkToolGateway, document_work_tool_specs
from simple_ar.tools.materials import journal_read_windows
from simple_ar.tools.sources import SourceToolGateway
from simple_ar.tools.workspace import WorkspaceToolGateway


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class _Client:
    def __init__(self, ledger, identity, decide):
        self._budget_ledger = ledger
        self._budget_session_id = 'session'
        self._budget_attempt_id = identity
        self.decide = decide
        self.calls = []

    def ask_json(self, system, prompt, *, label, budget_call_id):
        view = json.loads(prompt)
        self.calls.append(view)
        self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 100},
            session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
        try:
            response = self.decide(view, len(self.calls))
        except Exception:
            self._budget_ledger.mark_unknown(budget_call_id, reason='interrupted provider response')
            raise
        self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
        return response


class DocumentWorkTests(unittest.TestCase):
    def workspace(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        source = root / 'source'
        source.mkdir()
        ledger = BudgetLedger({'llm_requests': 40, 'total_tokens': 4000}, storage_path=root / 'ledger.json')
        workspace = WorkspaceToolGateway(root, {'source0': source, 'records': root / 'attempts'}, ledger)
        return root, source, ledger, workspace

    def gateway(self, root, source, ledger, workspace, decide, *, context=None, observations=None, sources=None):
        clients = []
        def factory(identity):
            client = _Client(ledger, identity, decide)
            clients.append(client)
            return client
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=factory,
            material_roots=lambda: ['source0'], work_context=lambda targets: context or {},
            material_observations=observations, sources=sources)
        return gateway, clients

    def test_revision_investigates_originals_and_reuses_parent_read_without_fixed_rounds(self):
        root, source, ledger, workspace = self.workspace()
        for number in range(8):
            (source / f'part-{number}.txt').write_text(f'Original condition {number}.\n', encoding='utf-8')
        original = source / 'part-0.txt'
        parent_request = {'path': 'source0/part-0.txt', 'full_digest': True}
        parent = workspace.call('read_file', parent_request, identity='parent-read')
        self.assertTrue(parent.ok)
        old_path = workspace.workspace / 'draft.md'
        old_path.write_text('# Previous\nKeep this context.\n', encoding='utf-8')
        old_sha = sha(old_path.read_bytes())
        original_context = {'user_requests': ['Explain the observed result', 'Clarify the condition'],
            'professional_findings': [{'kind': 'inspection_feedback', 'record': 'records/check/result.json',
                'result': {'findings': [{'text': 'State the actual condition'}]},
                'subjects': [{'path': 'workspace/draft.md', 'reviewed_sha256': old_sha,
                    'current_sha256': old_sha, 'current_matches_review': True}]}],
            'working_notes': [{'note': 'Check original records'}]}

        def decide(view, count):
            if count <= 7:
                return {'action': 'investigate', 'read_windows': [{'path': f'source0/part-{count}.txt'}]}
            self.assertEqual(view['prior_draft']['sha256'], old_sha)
            self.assertEqual(view['work_context'], original_context)
            self.assertEqual(len(view['observations']), 8)
            return {'action': 'finish', 'selected_observation_ids': [f'o{i}' for i in range(1, 9)],
                'manuscript_markdown': '# Revised\nThe original conditions are now stated.\n',
                'gaps': ['The records do not establish an external comparison.']}

        gateway, clients = self.gateway(root, source, ledger, workspace, decide, context=original_context,
            observations=lambda roots: [{'request': parent_request, 'data': parent.data}])
        arguments = {'goal': 'Revise against the original conditions', 'prior_draft': 'workspace/draft.md',
            'prior_sha256': old_sha}
        result = gateway.call('work_document', arguments, identity='revision')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 8)
        self.assertEqual(result.data['sha256'], sha(result.data['markdown'].encode('utf-8')))
        self.assertEqual(workspace.resolve(result.data['manuscript']).read_bytes(), result.data['markdown'].encode('utf-8'))
        self.assertEqual(len(result.data['provenance']['evidence']), 8)
        windows = result.metadata['material_observations']
        self.assertEqual(len(windows), 8)
        self.assertEqual(windows[0], {'request': parent_request, 'data': parent.data})
        self.assertIn('Original condition 7', windows[-1]['data']['text'])
        presented = gateway.coordination_result(result, receipt_path='attempts/revision/result.json')
        self.assertNotIn('material_observations', presented['metadata'])
        self.assertNotIn('markdown', presented['data'])
        self.assertEqual(presented['data']['source_observations']['record'], 'attempts/revision/result.json')
        self.assertEqual(presented['data']['source_observations']['count'], 8)
        self.assertIn('material_observations', result.metadata)
        decoded = journal_read_windows(result.model_dump(mode='json'), arguments)
        self.assertEqual(decoded[0]['request'], windows[0]['request'])
        self.assertEqual(decoded[0]['data'], windows[0]['data'])
        self.assertEqual(decoded[-1]['field'], 'metadata.material_observations.7.data.text')
        self.assertEqual(result.data['prior_draft']['sha256'], old_sha)
        self.assertEqual(old_path.read_text(encoding='utf-8'), '# Previous\nKeep this context.\n')
        self.assertEqual(original.read_text(encoding='utf-8'), 'Original condition 0.\n')
        self.assertIn('external comparison', result.data['gaps'][0])
        entries = [entry.to_dict() for entry in ledger.entries]
        self.assertEqual(gateway.call('work_document', arguments, identity='revision'), result)
        self.assertEqual([entry.to_dict() for entry in ledger.entries], entries)
        self.assertEqual(len(clients), 1)
        self.assertEqual(document_work_tool_specs()[0].name, 'work_document')
        self.assertIsNone(document_work_tool_specs()[0].completion)

    def test_original_bundle_is_queried_by_source_owner_and_change_blocks_replay(self):
        root, source, ledger, workspace = self.workspace()
        bundle = DocumentBundle(
            records=[DocumentRecord(document_id='paper', title='Original paper', source='local_files')],
            fulltext_manifest={}, fulltext_extraction={}, sections=[],
            chunks=[TextChunk('chunk-1', 'paper', 'Calibration depends on the observed population.')])
        document = source / 'original.json'
        document.write_text(json.dumps(bundle.to_handoff_dict()), encoding='utf-8')
        source_gateway = SourceToolGateway(root, workspace, ledger)
        def decide(view, count):
            if count == 1:
                self.assertEqual(len(view['original_source_bundles']), 1)
                return {'action': 'investigate', 'source_queries': [
                    {'document_record': 'source0/original.json', 'query': 'calibration population'}]}
            self.assertEqual(view['observations'][0]['kind'], 'source_passages')
            return {'action': 'finish', 'selected_observation_ids': ['o1'],
                'manuscript_markdown': '# Calibration\nThe source conditions its result on the observed population.\n'}
        gateway, clients = self.gateway(root, source, ledger, workspace, decide, sources=source_gateway)
        args = {'goal': 'Explain the reported condition'}
        result = gateway.call('work_document', args, identity='source-work')
        self.assertTrue(result.ok, result.error)
        provenance = result.data['provenance']['evidence'][0]
        self.assertEqual(provenance['kind'], 'source_passages')
        self.assertEqual(provenance['sha256'], sha(document.read_bytes()))
        self.assertIn('Lexical lookup', provenance['scope'])
        document.write_text(json.dumps({**bundle.to_handoff_dict(), 'changed': True}), encoding='utf-8')
        stale = gateway.call('work_document', args, identity='source-work')
        self.assertFalse(stale.ok)
        self.assertIn('changed', stale.error.lower())
        self.assertEqual(len(clients[0].calls), 2)

    def test_candidate_recovers_after_interrupted_write_without_another_model_call(self):
        root, source, ledger, workspace = self.workspace()
        asset = source / 'figure.png'
        asset.write_bytes(b'original figure bytes')
        def decide(view, count):
            row = next(row for row in view['candidates'] if row['path'] == str(asset))
            return {'action': 'finish', 'attachments': [str(asset)],
                'manuscript_markdown': f"# Result\n![Figure]({row['delivery_path']})\n",
                'gaps': ['The figure requires independent visual inspection.']}
        gateway, clients = self.gateway(root, source, ledger, workspace, decide)
        args = {'goal': 'Prepare an editable document with the supplied figure'}
        real_call = workspace.call
        def interrupt_after_write(name, arguments, **kwargs):
            result = real_call(name, arguments, **kwargs)
            if name == 'write_file':
                raise KeyboardInterrupt()
            return result
        with patch.object(workspace, 'call', side_effect=interrupt_after_write):
            with self.assertRaises(KeyboardInterrupt):
                gateway.call('work_document', args, identity='partial')
        self.assertTrue(gateway.can_reconcile('work_document', 'partial'))
        entries = [entry.to_dict() for entry in ledger.entries]
        result = gateway.call('work_document', args, identity='partial')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(workspace.resolve(result.artifacts[1]).read_bytes(), asset.read_bytes())
        self.assertEqual([entry.to_dict() for entry in ledger.entries], entries)
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(clients[0].calls), 1)
        output = workspace.resolve(result.data['manuscript'])
        output.write_text('User changed the manuscript', encoding='utf-8')
        stale = gateway.call('work_document', args, identity='partial')
        self.assertFalse(stale.ok)
        self.assertEqual(output.read_text(encoding='utf-8'), 'User changed the manuscript')

    def test_revision_preserves_referenced_original_figure_and_bibliography(self):
        root, source, ledger, workspace = self.workspace()
        previous = workspace.workspace / 'previous'
        (previous / 'figures').mkdir(parents=True)
        figure = previous / 'figures' / 'figure.png'
        figure.write_bytes(b'original image bytes')
        bibliography = previous / 'references.bib'
        bibliography.write_text('@article{ref, title={Original}}\n', encoding='utf-8')
        draft = previous / 'manuscript.md'
        draft.write_text('# Before\n![Original](figures/figure.png)\nSee [@ref].\n', encoding='utf-8')
        def decide(view, count):
            self.assertEqual(view['prior_draft']['images'][0]['source'], str(figure))
            self.assertIn('@article{ref', view['prior_draft']['bibliography']['text'])
            return {'action': 'finish',
                'manuscript_markdown': '# Revised\n![Original](figures/figure.png)\nSee [@ref].\n'}
        gateway, clients = self.gateway(root, source, ledger, workspace, decide)
        result = gateway.call('work_document', {'goal': 'Clarify the prose',
            'prior_draft': 'workspace/previous/manuscript.md', 'prior_sha256': sha(draft.read_bytes())},
            identity='with-assets')
        self.assertTrue(result.ok, result.error)
        output = workspace.resolve(result.data['manuscript']).parent
        self.assertEqual((output / 'figures/figure.png').read_bytes(), figure.read_bytes())
        self.assertEqual((output / 'references.bib').read_bytes(), bibliography.read_bytes())
        self.assertFalse(any('Image is not' in gap for gap in result.data['gaps']))
        self.assertEqual(len(clients[0].calls), 1)

    def test_complete_final_response_recovers_before_candidate_is_saved(self):
        root, source, ledger, workspace = self.workspace()
        gateway, clients = self.gateway(root, source, ledger, workspace,
            lambda view, count: {'action': 'finish', 'manuscript_markdown': '# Retained response\n'})
        args = {'goal': 'Draft a short document'}
        with patch.object(gateway, '_candidate', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                gateway.call('work_document', args, identity='response-saved')
        self.assertTrue(gateway.can_reconcile('work_document', 'response-saved'))
        entries = [entry.to_dict() for entry in ledger.entries]
        result = gateway.call('work_document', args, identity='response-saved')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data['markdown'], '# Retained response\n')
        self.assertEqual([entry.to_dict() for entry in ledger.entries], entries)
        self.assertEqual(len(clients), 1)
        self.assertEqual(len(clients[0].calls), 1)

    def test_unknown_model_usage_and_failed_response_are_retained_without_replay(self):
        root, source, ledger, workspace = self.workspace()
        (source / 'condition.txt').write_text('Actual condition before interruption.', encoding='utf-8')
        def fail(view, count):
            if count == 1:
                return {'action': 'investigate', 'read_windows': [{'path': 'source0/condition.txt'}]}
            raise RuntimeError('provider outcome unknown')
        gateway, clients = self.gateway(root, source, ledger, workspace, fail)
        args = {'goal': 'Draft the document'}
        result = gateway.call('work_document', args, identity='unknown')
        self.assertFalse(result.ok)
        self.assertEqual([entry.status for entry in ledger.entries], ['settled', 'unknown'])
        self.assertEqual(result.metadata['material_observations'][0]['data']['text'],
            'Actual condition before interruption.')
        records = root / 'attempts/unknown/document_work'
        self.assertTrue((records / 'decisions/2/failure.json').exists())
        self.assertTrue((records / 'failure.json').exists())
        self.assertFalse(gateway.can_reconcile('work_document', 'unknown'))
        before = [entry.to_dict() for entry in ledger.entries]
        retry = gateway.call('work_document', args, identity='unknown')
        self.assertFalse(retry.ok)
        self.assertIn('no blind replay', retry.error)
        self.assertEqual([entry.to_dict() for entry in ledger.entries], before)
        self.assertEqual(len(clients), 1)

    def test_wrong_prior_version_and_unobserved_evidence_are_not_adopted(self):
        root, source, ledger, workspace = self.workspace()
        draft = workspace.workspace / 'draft.md'
        draft.write_text('# Current\n', encoding='utf-8')
        gateway, clients = self.gateway(root, source, ledger, workspace,
            lambda view, count: {'action': 'finish', 'selected_observation_ids': ['invented'],
                'manuscript_markdown': '# Invalid evidence\n'})
        wrong = gateway.call('work_document', {'goal': 'Revise', 'prior_draft': 'workspace/draft.md',
            'prior_sha256': '0' * 64}, identity='stale')
        self.assertFalse(wrong.ok)
        self.assertEqual(clients, [])
        result = gateway.call('work_document', {'goal': 'Draft'}, identity='unobserved')
        self.assertFalse(result.ok)
        self.assertIn('actually observed', result.error)
        self.assertFalse((workspace.workspace / 'document-work/unobserved/manuscript.md').exists())


if __name__ == '__main__':
    unittest.main()
