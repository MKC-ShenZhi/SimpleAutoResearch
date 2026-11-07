"""Current-work investigation is not a declaration of final delivery."""
from copy import deepcopy
import hashlib
import os
import unittest

from simple_ar.app.tool_application import ToolApplication
from simple_ar.tools.workspace import validate_workspace_arguments
from tests import test_professional_work_loop as fixture
from tests.test_professional_work_loop import _action


class CandidateReviewTests(unittest.TestCase):
    def setUp(self):
        c = self.case = fixture.OperationsWorkLoopTests()
        c.setUp()
        self.addCleanup(c.doCleanups)
        c.app = ToolApplication.create(c.root,
            'Explain the supplied observations in an editable note and a PDF of that note.',
            model='fixture', sources={'measurements': c.inputs}, services=c.services,
            allow_execution=True, max_turns=24, max_tokens=900000, tool_contracts='full')
        c._reload()

    def drain(self, **kwargs):
        c = self.case
        return c._run(should_stop=lambda: not c.decisions and c.app.state['pending'] is None
            and not any(turn['remaining'] for turn in c.app.state['turns']), **kwargs)

    def candidate(self):
        c = self.case
        c.decisions.append(_action('write_file', path='note.md',
            content='# Observations\n\nOnly the supplied observations are characterized.\n'))
        self.drain()
        c.decisions.append(_action('review_result', summary='The current bounded explanation.',
            artifacts=['note.md'], unresolved=['The requested PDF is not built yet.']))
        self.drain()
        return c._last('review_result', reviewed=True)

    def test_current_files_are_inspected_before_publication_without_final_acceptance(self):
        c = self.case
        result = self.candidate()
        self.assertEqual(c.app.state['config']['candidate_review_protocol'], 'candidate.v1')
        self.assertEqual(result['status'], 'ok', result)
        self.assertEqual(result['metadata']['delivery_review']['binding']['purpose'], 'candidate')
        self.assertTrue(c.inspection_requests)
        self.assertFalse((c.root/'delivery.json').exists())
        self.assertFalse(list((c.root/'workspace').rglob('*.pdf')))
        schemas = {r['function']['name']: r['function']['parameters'] for r in c.app.schemas}
        self.assertNotIn('report', schemas['review_result']['properties'])
        self.assertNotIn('summary_source', schemas['review_result']['properties'])
        self.assertIn('report', schemas['deliver_result']['properties'])
        with self.assertRaises(ValueError):
            validate_workspace_arguments('review_result', {'summary': 'Premature completion',
                'artifacts': ['note.md'], 'report': {'status': 'canonical'}},
                candidate_review_protocol='candidate.v1')
        before = len(c.inspection_requests)
        c._reload()
        c.decisions.append(_action('deliver_result', summary='Final note and PDF', artifacts=['note.md'],
            report={'status': 'candidate', 'manuscript': 'note.md', 'formats': ['markdown', 'pdf']}))
        self.drain()
        self.assertEqual(c._last('deliver_result')['status'], 'blocked')
        self.assertEqual(len(c.inspection_requests), before)
        self.assertFalse((c.root/'delivery.json').exists())

    def test_absent_saved_contract_keeps_legacy_review_after_reload_and_supply(self):
        c = self.case
        c.app.state['config'].pop('candidate_review_protocol')
        c.app._save()
        c._reload()
        schema = c.app.gateway.registry.get('review_result').input_schema
        self.assertIn('report', schema['properties'])
        before = deepcopy(c.app.ledger.to_dict())
        c.app.supply(supply_id='more-source', reason='Expose another authorized material alias',
            changes={'sources': {'extra': c.inputs}})
        c._reload()
        self.assertNotIn('candidate_review_protocol', c.app.state['config'])
        self.assertEqual(c.app.gateway.registry.get('review_result').input_schema, schema)
        self.assertEqual(c.app.ledger.to_dict(), before)
        self.assertEqual(validate_workspace_arguments('review_result', {'summary': 'Historical claim',
            'report': {'status': 'candidate'}}).report.status, 'candidate')

    def test_same_version_edit_reuses_observed_hash_and_still_refuses_concurrent_change(self):
        c = self.case
        c.decisions.append(_action('write_file', path='note.md', content='Original note.\n'))
        self.drain()
        digest = c._ok('write_file')['data']['sha256']
        c.decisions.append(_action('read_file', path='measurements/conditions.txt'))
        self.drain()
        c._reload()
        c.decisions.append(_action('edit_file', path='note.md', expected_sha256=digest,
            replacements=[{'old_text': 'Original note.', 'new_text': 'Revised note.'}]))
        self.drain()
        c._ok('edit_file')
        prior = hashlib.sha256((c.root/'workspace/note.md').read_bytes()).hexdigest()
        (c.root/'workspace/note.md').write_text('Concurrent change.\n', encoding='utf-8')
        c._reload()
        c.decisions.append(_action('edit_file', path='note.md', expected_sha256=prior,
            replacements=[{'old_text': 'Revised note.', 'new_text': 'Stale replacement.'}]))
        self.drain()
        self.assertEqual(c._last('edit_file')['status'], 'blocked')
        self.assertEqual((c.root/'workspace/note.md').read_text(), 'Concurrent change.\n')

    @unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1',
        'Real compilation runs in the server publication integration phase')
    def test_early_investigation_then_real_publication_and_final_same_version_delivery(self):
        c = self.case
        self.candidate()
        c.decisions.append(_action('export_report', markdown='note.md', compile_pdf=True))
        self.drain(approve=lambda *_: True)
        exported = c._ok('export_report')
        project = c.app.workspace.resolve(exported['data']['project'])
        checks = len(c.inspection_requests)
        c.decisions.append(_action('deliver_result', summary='Final bounded explanation and PDF.',
            artifacts=['note.md', *exported['artifacts']],
            report={'status': 'candidate', 'manuscript': 'note.md', 'formats': ['markdown', 'pdf']}))
        self.drain()
        self.assertEqual(c.app.session.manifest.status, 'completed', c.app.result_snapshot())
        self.assertGreater(len(c.inspection_requests), checks)
        from simple_ar.tools.delivery_versions import DeliveryVersions
        version = DeliveryVersions(c.root).select(1)
        self.assertIn(str((project/'main.pdf').relative_to(c.root)), version['files'])
        self.assertEqual(version['files']['workspace/note.md']['sha256'],
            hashlib.sha256((c.root/'workspace/note.md').read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
