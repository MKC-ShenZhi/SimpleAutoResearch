"""Writing-owner presentation preserves receipts and actionable review feedback."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, PropertyMock, patch

from simple_ar.tools.specs import ToolResult
from simple_ar.tools.writing import ComposeRequest, WritingToolGateway
from simple_ar.tools.workspace import FileWindow, WorkspaceToolGateway
from simple_ar.core.budget import BudgetLedger
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.report.schema import ReportSectionReview, ReviewerFinding
from simple_ar.report.review_evidence import restore_delivery_producers


class ProducerDeclarationReviewTests(unittest.TestCase):
    """Review response contracts only; these fixtures certify no execution."""
    def setUp(self):
        from simple_ar.report.review_evidence import DELIVERY_REVIEW_CONTRACT, DECLARATION_ADOPTION_CONTRACT
        self.group = {'execution_ref': 'records/run/candidate_execution.json',
            'declarations': [{'id': 'run:0', 'statement': 'Artifacts require execution'},
                {'id': 'run:1', 'statement': 'The source supplies no physical unit'}],
            'goal': 'Report the supplied measurements without inventing units',
            'execution': {'status': 'run_succeeded', 'returncode': 0, 'output_sha256': {'table.csv': 'fixture'}}}
        self.snapshot = {'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': [],
            'producer_declarations': [self.group]}
        self.view = {'delivery_review_contract': DELIVERY_REVIEW_CONTRACT,
            'declaration_adoption_contract': DECLARATION_ADOPTION_CONTRACT,
            'delivery_artifacts': [], 'producer_declarations': [self.group],
            'sections': [{'section_id': 'results', 'heading': 'Results',
                'markdown': 'Values retain the unspecified source unit.'}]}
        self.checks = [{'declaration_id': 'run:0', 'status': 'satisfied_by_execution',
            'explanation': 'The executor binds the produced output bytes.',
            'evidence_quotes': [{'pointer': '/producer_declarations/0/execution/status'}]},
            {'declaration_id': 'run:1', 'status': 'accepted_limitation',
             'explanation': 'The goal does not authorize invented units; the manuscript discloses the limit.',
             'draft_quotes': [{'section_id': 'results', 'quote': 'Values retain the unspecified source unit.'}],
             'evidence_quotes': [{'pointer': '/producer_declarations/0/execution/returncode'}]}]

    def _reviews(self, checks, **changes):
        from simple_ar.report.editor import _validate_document_reviews
        from simple_ar.report.schema import ReportSectionDraft
        return _validate_document_reviews({'section_reviews': [{'section_id': 'results',
            'verdict': 'pass', 'findings': [], 'producer_claim_checks': checks, **changes}]},
            sections=[ReportSectionDraft(section_id='results', heading='Results',
                draft_markdown=self.view['sections'][0]['markdown'])], prior_by_key={}, evidence_view=self.view)

    def test_goal_findings_keeps_located_notes_without_per_note_adoption(self):
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import delivery_review_result, resolve_review_evidence
        group = {'prepared_result': 'records/run/implementation/result.json',
            'prepared_sha256': 'retained-preparation', 'source_binding_sha256': 'retained-request',
            'goal': self.group['goal'], 'notes': [row['statement'] for row in self.group['declarations']],
            'execution': self.group['execution']}
        for target in (self.view, self.snapshot):
            target.pop('producer_declarations')
            target.pop('declaration_adoption_contract', None)
            target.update(producer_completion='goal_findings.v1', producer_notes=[group])
        reviews = self._reviews([])
        result = delivery_review_result(self.snapshot, reviews)
        self.assertEqual(result['status'], 'reviewed')
        self.assertEqual(result['producer_notes'], [group])
        self.assertNotIn('declaration_adoption', result)
        checks = json.loads(json.dumps(self.checks))
        checks[0]['evidence_quotes'] = [{'pointer': '/producer_notes/0/execution/status'}]
        checks[1]['evidence_quotes'] = [{'pointer': '/producer_notes/0/execution/returncode'}]
        with self.assertRaisesRegex(LLMResponseError, 'does not accept'):
            self._reviews(checks)
        resolved = resolve_review_evidence({'findings': [{'evidence_quotes': [
            {'pointer': '/producer_notes/0/notes/0'},
            {'pointer': '/producer_notes/0/execution/status'}]}]}, self.view)
        self.assertEqual([row['role'] for row in resolved['findings'][0]['evidence_quotes']],
            ['declaration', 'executor_record'])
        with self.assertRaises(LLMResponseError):
            resolve_review_evidence({'findings': [{'evidence_quotes': [
                {'pointer': '/producer_notes/0/invented-anchor'}]}]}, self.view)

    def test_goal_findings_real_limitation_still_needs_an_anchored_required_finding(self):
        from simple_ar.report.schema import finding_requires_resolution
        self.view.pop('producer_declarations')
        self.view.pop('declaration_adoption_contract')
        self.view.update(producer_completion='goal_findings.v1', producer_notes=[])
        finding = {'finding_id': 'unsupported-unit', 'type': 'evidence_gap', 'severity': 'major',
            'required_action': 'verify', 'message': 'The claimed physical unit lacks original source support.',
            'suggested_action': 'Verify the source or qualify the claim without inventing units.',
            'draft_quotes': [{'section_id': 'results', 'quote': self.view['sections'][0]['markdown']}],
            'evidence_quotes': [{'pointer': '/objective'}]}
        self.view['objective'] = self.group['goal']
        reviews = self._reviews([], verdict='revise_required', findings=[finding])
        self.assertTrue(finding_requires_resolution(reviews[0].findings[0]))
        self.assertEqual(reviews[0].verdict, 'revise_required')
        self.assertFalse(reviews[0].producer_claim_checks)

    def test_joint_inspection_requires_every_statement_and_literal_current_evidence(self):
        import copy
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import delivery_review_result
        reviewed = self._reviews(self.checks)
        result = delivery_review_result(self.snapshot, reviewed)
        self.assertEqual(result['status'], 'reviewed')
        self.assertEqual(result['declaration_adoption']['contract'], 'producer_declarations.v1')
        self.assertEqual(result['declaration_adoption']['producers'], [self.group])
        self.assertEqual(len(result['declaration_adoption']['checks']), 2)
        for mode in ('missing', 'duplicate', 'undisclosed', 'wrong-producer', 'invented-quote'):
            checks = copy.deepcopy(self.checks)
            if mode == 'missing':
                checks.pop()
            elif mode == 'duplicate':
                checks.append(copy.deepcopy(checks[0]))
            elif mode == 'undisclosed':
                checks[1]['draft_quotes'] = []
            elif mode == 'wrong-producer':
                checks[1]['evidence_quotes'][0]['pointer'] = '/producer_declarations/1/execution/status'
            else:
                checks[1]['draft_quotes'][0]['quote'] = 'The unit was independently verified.'
            with self.subTest(mode=mode), self.assertRaises(LLMResponseError):
                self._reviews(checks)
        self.assertEqual(delivery_review_result(self.snapshot, [])['status'], 'incomplete')

    def test_adoption_does_not_suppress_new_required_findings_or_unfinished_work(self):
        import copy
        from simple_ar.report.review_evidence import delivery_review_result
        finding = {'finding_id': 'actual-gap', 'section_id': 'results', 'type': 'evidence_gap',
            'severity': 'major', 'required_action': 'verify', 'message': 'A required comparison is absent',
            'draft_quotes': [{'section_id': 'results', 'quote': 'Values retain the unspecified source unit.'}]}
        checks = copy.deepcopy(self.checks)
        checks[1].update(status='unresolved_work', explanation='Required comparison remains unperformed')
        reviews = self._reviews(checks, verdict='revise_required', findings=[finding])
        self.assertEqual(reviews[0].findings[0].finding_id, 'actual-gap')
        self.assertEqual(delivery_review_result(self.snapshot, reviews)['status'], 'needs_revision')
        # A historical opinion-only request has no authority to adopt producer declarations.
        self.view.pop('declaration_adoption_contract')
        from simple_ar.integrations.llm import LLMResponseError
        with self.assertRaises(LLMResponseError):
            self._reviews(self.checks)

    def test_v2_separates_declaration_identity_from_supplied_execution_evidence(self):
        import copy
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import (
            CLAIM_ADOPTION_V2, delivery_review_result, attachment_review_blocks_editing)
        later = copy.deepcopy(self.group)
        later['declarations'] = []
        later['execution_ref'] = 'records/later/candidate_execution.json'
        self.view['producer_declarations'].append(later)
        checks = copy.deepcopy(self.checks)
        checks[0]['evidence_quotes'] = [{'pointer': '/producer_declarations/1/execution/status'}]
        checks[1]['evidence_quotes'] = []
        with self.assertRaises(LLMResponseError):
            self._reviews(checks)  # Frozen v1 still demands own execution.
        self.view['declaration_adoption_contract'] = CLAIM_ADOPTION_V2
        self.snapshot.update(declaration_adoption_contract=CLAIM_ADOPTION_V2,
            producer_declarations=self.view['producer_declarations'])
        reviews = self._reviews(checks)
        result = delivery_review_result(self.snapshot, reviews)
        self.assertEqual(result['declaration_adoption']['contract'], CLAIM_ADOPTION_V2)
        self.assertEqual(result['status'], 'reviewed')
        self.assertFalse(attachment_review_blocks_editing(result))
        for fault in ('missing', 'duplicate', 'undisclosed', 'non-executor', 'bad-quote'):
            changed = copy.deepcopy(checks)
            if fault == 'missing':
                changed.pop()
            elif fault == 'duplicate':
                changed.append(copy.deepcopy(changed[0]))
            elif fault == 'undisclosed':
                changed[1]['draft_quotes'] = []
            elif fault == 'non-executor':
                changed[0]['evidence_quotes'] = [{'pointer': '/producer_declarations/1/goal'}]
            else:
                changed[0]['evidence_quotes'] = [{'pointer': '/producer_declarations/1/execution/status',
                    'quote': 'invented success', 'role': 'executor_record'}]
            with self.subTest(fault=fault), self.assertRaises(LLMResponseError):
                self._reviews(changed)
        self.view['declaration_adoption_contract'] = 'unknown'
        with self.assertRaises(ValueError):
            self._reviews(checks)
        with self.assertRaises(ValueError):
            delivery_review_result({**self.snapshot, 'declaration_adoption_contract': 'unknown'}, reviews)
        with self.assertRaises(ValueError):
            attachment_review_blocks_editing({'declaration_adoption': {'contract': 'unknown'}})

    def test_v2_accepts_original_statement_and_current_limitation_disclosure(self):
        import copy
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2, delivery_review_result
        checks = copy.deepcopy(self.checks)
        for index, check in enumerate(checks):
            check.update(status='accepted_limitation',
                draft_quotes=copy.deepcopy(self.checks[1]['draft_quotes']),
                evidence_quotes=[{'pointer': f'/producer_declarations/0/declarations/{index}/statement'}])
        with self.assertRaisesRegex(LLMResponseError, 'own execution'):
            self._reviews(checks)
        self.view['declaration_adoption_contract'] = CLAIM_ADOPTION_V2
        self.snapshot['declaration_adoption_contract'] = CLAIM_ADOPTION_V2
        result = delivery_review_result(self.snapshot, self._reviews(checks))
        self.assertEqual(result['status'], 'reviewed')
        self.assertTrue(all(row['evidence_quotes'][0]['role'] == 'declaration'
            for row in result['declaration_adoption']['checks']))

    def test_current_paragraph_selection_persists_exact_disclosure_and_keeps_completeness(self):
        import copy
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import DRAFT_REFERENCE_CONTRACT, delivery_review_result
        self.view['draft_reference_contract'] = DRAFT_REFERENCE_CONTRACT
        self.view['sections'][0]['markdown'] = 'Observed value: 2.5.\r\n \r\nValues retain the unspecified source unit.'
        checks = copy.deepcopy(self.checks)
        checks[1]['draft_quotes'] = [{'section_id': 'results', 'paragraph': 'p2'}]
        raw = copy.deepcopy(checks)
        reviews = self._reviews(checks)
        saved = delivery_review_result(self.snapshot, reviews)
        quote = saved['declaration_adoption']['checks'][1]['draft_quotes'][0]
        self.assertEqual(quote, self.checks[1]['draft_quotes'][0])
        self.assertEqual(checks, raw)
        for incomplete in (checks[:1], checks + [checks[0]]):
            with self.assertRaises(LLMResponseError):
                self._reviews(incomplete)
        self.view['sections'][0]['markdown'] = 'Replacement candidate with a different limitation.'
        restored = ReportSectionReview.model_validate(reviews[0].model_dump(mode='json'))
        self.assertEqual(restored.producer_claim_checks[1].draft_quotes[0].quote, quote['quote'])

    def test_paragraph_contract_rejects_invalid_mixed_and_invented_quotes(self):
        import copy
        from pydantic import ValidationError
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import DRAFT_REFERENCE_CONTRACT
        self.view['draft_reference_contract'] = DRAFT_REFERENCE_CONTRACT
        for reference in (
            {'section_id': 'results', 'paragraph': value}
            for value in ('p0', 'p01', 'p2', 'p-1', 'P1', '', None, 1)
        ):
            checks = copy.deepcopy(self.checks)
            checks[1]['draft_quotes'] = [reference]
            with self.subTest(reference=reference), self.assertRaises(LLMResponseError):
                self._reviews(checks)
        for reference in (
            {'section_id': 'old-section', 'paragraph': 'p1'},
            {'section_id': 'results', 'paragraph': 'p1', 'quote': self.checks[1]['draft_quotes'][0]['quote']},
            {'section_id': 'results', 'paragraph': 'p1', 'pointer': '/sections/0/markdown'},
            {'section_id': 'results', 'anchor': 'p1'},
            {'section_id': 'results', 'quote': 'The unit was verified.'},
            {'section_id': 'results'},
        ):
            checks = copy.deepcopy(self.checks)
            checks[1]['draft_quotes'] = [reference]
            with self.subTest(reference=reference), self.assertRaises((LLMResponseError, ValidationError)):
                self._reviews(checks)
        self.assertTrue(self._reviews(self.checks))  # Exact literals still work.
        checks = copy.deepcopy(self.checks)
        checks[1]['draft_quotes'] = []
        with self.assertRaises(LLMResponseError):
            self._reviews(checks)  # No inferred limitation quote.
        self.view['draft_reference_contract'] = 'future-unknown'
        with self.assertRaisesRegex(LLMResponseError, 'contract'):
            self._reviews(self.checks)

    def test_paragraph_resolution_covers_findings_checks_and_frozen_legacy_requests(self):
        import copy
        from pydantic import ValidationError
        from simple_ar.integrations.llm import LLMResponseError
        from simple_ar.report.review_evidence import DRAFT_REFERENCE_CONTRACT, resolve_review_evidence
        view = {'sections': [{'section_id': 'results', 'markdown': 'First exact paragraph.\n\nSecond exact paragraph.'}],
            'draft_reference_contract': DRAFT_REFERENCE_CONTRACT}
        response = {name: [{'draft_quotes': [{'section_id': 'results', 'paragraph': 'p2'}]}]
            for name in ('findings', 'finding_checks', 'producer_claim_checks')}
        raw = copy.deepcopy(response)
        selected = resolve_review_evidence(response, view)
        for name in response:
            self.assertEqual(selected[name][0]['draft_quotes'],
                [{'section_id': 'results', 'quote': 'Second exact paragraph.'}])
        self.assertEqual(response, raw)
        view.pop('draft_reference_contract')
        self.assertEqual(resolve_review_evidence(response, view), raw)
        # An old request still needs literal quotations under the old schema.
        with self.assertRaises(ValidationError):
            ReportSectionReview.model_validate({'section_id': 'results', 'verdict': 'pass',
                'producer_claim_checks': [{**self.checks[1], **raw['producer_claim_checks'][0]}]})
        view['draft_reference_contract'] = DRAFT_REFERENCE_CONTRACT
        view['sections'][0]['markdown'] = ''
        with self.assertRaises(LLMResponseError):
            resolve_review_evidence(response, view)

    def test_draft_locator_is_bounded_navigation_without_duplicating_markdown(self):
        from simple_ar.report.review_evidence import review_draft_locator
        parts = [f'Paragraph {index}: ' + 'x' * 500 for index in range(30)]
        view = {'sections': [{'section_id': 'results', 'markdown': '\n\n'.join(parts)},
            {'section_id': 'empty', 'markdown': ''}]}
        locator = review_draft_locator(view)
        self.assertEqual(locator['results']['paragraph_count'], 30)
        self.assertEqual(locator['results']['omitted_preview_count'], 6)
        self.assertEqual(len(locator['results']['previews']), 24)
        self.assertEqual(locator['results']['previews']['p1'], parts[0][:80])
        self.assertTrue(all(len(preview) <= 80 for preview in locator['results']['previews'].values()))
        self.assertEqual(locator['empty']['previews'], {})


class WritingCoordinationTests(unittest.TestCase):
    @staticmethod
    def _record_native_result(root, identity, result):
        from simple_ar.core.capabilities import AttemptManifest, CapabilityResult
        store = ArtifactStore(root / 'attempts' / identity)
        ref = store.write_json('native_result.json', result.model_dump(mode='json'), kind='native_observation')
        store.write_attempt_manifest(AttemptManifest(attempt_id=identity, capability='native_tool',
            status='completed', outputs=(ref,)))
        store.write_capability_result(CapabilityResult(status='completed', artifacts=(ref,)))
        session = ArtifactStore(root)
        saved = session.read_json('session_manifest.json') if session.exists('session_manifest.json') else {
            'session_id': 'task', 'budget': {'recorded_attempts': []}}
        if identity not in saved['budget']['recorded_attempts']:
            saved['budget']['recorded_attempts'].append(identity)
        session.write_json('session_manifest.json', saved)
        return store

    def _real_workspace(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        ledger = BudgetLedger({}, storage_path=root / 'ledger.json')
        workspace = WorkspaceToolGateway(root, {'records': root / 'attempts'}, ledger)
        return root, workspace, WritingToolGateway(root, workspace, None, 'unused', 'task')

    def _document_work_fixture(self):
        from types import SimpleNamespace
        root, workspace, writing = self._real_workspace()
        source = root / 'source'
        (source / 'code').mkdir(parents=True)
        (source / 'results').mkdir()
        script, table = source / 'code' / 'calculate.py', source / 'results' / 'record.txt'
        script.write_bytes(b'from pathlib import Path\nprint((Path(__file__).parent / "../results/record.txt").read_text())\n')
        table.write_bytes(b'value=2.5\nscope=saved record; no new execution\n')
        (source / 'unselected.txt').write_bytes(b'This file was not selected.')
        workspace.mounts['source0'] = source
        writing.ledger = workspace.ledger
        writing.document_work = True
        prompts = []

        def answer(system, prompt, **options):
            view = json.loads(prompt)
            label = options['label']
            prompts.append((label, view))
            if label == 'composition-selection-1':
                return {'read_windows': [{'path': str(path)} for path in (script, table)]}
            if label.startswith('composition-selection-'):
                return {'title': 'Recorded value and scope', 'materials': [str(table)],
                    'delivery_artifacts': [str(script), str(table)]}
            if label == 'composition-article':
                body = '## Results\n\nThe supplied record says value=2.5. No new execution was requested.'
                if view.get('edit_contract'):
                    return {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': view['prior_draft'], 'new_text': body}]}
                return {'draft_markdown': body}
            if label == 'article-independent-review':
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass', 'findings': []}]}
            self.fail('Unexpected document-work model step: ' + label)

        client = SimpleNamespace(ask_json=Mock(side_effect=answer))
        writing.client_factory = Mock(return_value=client)
        return root, workspace, writing, source, script, table, client, prompts

    def test_large_table_inspection_preserves_complete_files_and_unread_candidates(self):
        from simple_ar.report.review_evidence import SCOPED_DELIVERY_INSPECTION
        for unsupported in (False, True):
            with self.subTest(unsupported=unsupported):
                root, workspace, writing, source, _, _, client, _ = self._document_work_fixture()
                writing.document_source_contract = 'document_source.v1'
                table = source / 'results' / 'measurements.csv'
                raw = ('id,value\n' + ''.join(f'{i},{i % 7}\n' for i in range(50000))).encode()
                self.assertGreater(len(raw), 256000)
                table.write_bytes(raw)
                extra = source / 'opaque.txt'
                extra.write_bytes(b'\x00\xff\x01uninspected original')
                selected = [str(table), *([str(extra)] if unsupported else [])]
                inspected = []

                def answer(system, prompt, **options):
                    view = json.loads(prompt)
                    label = options['label']
                    if label == 'composition-selection-1':
                        return {'read_windows': [{'path': str(table), 'max_bytes': 1000}]}
                    if label.startswith('composition-selection-'):
                        self.assertIn('Preservation is separate from inspection', system)
                        return {'title': 'Supplied table coverage',
                            'materials': [{'path': str(table), 'max_bytes': 1000}],
                            'delivery_artifacts': selected}
                    if label == 'composition-article':
                        self.assertIn('not full-value or scientific verification', view['attachment_text_scope'])
                        self.assertEqual(next(row for row in view['delivery_review']['artifacts']
                            if row['path'] == str(table))['profile']['row_count'], 50000)
                        return {'draft_markdown': '## Coverage\n\nThe supplied table has 50,000 records. '
                            'Its column meanings and individual values require separate investigation.'}
                    if label == 'article-independent-review':
                        rows = view['delivery_artifacts']
                        row = next(row for row in rows if row['path'] == str(table))
                        self.assertEqual(row['coverage'], 'table_profile')
                        self.assertEqual(row['profile']['row_count'], 50000)
                        self.assertEqual(row['profile']['columns'], ['id', 'value'])
                        self.assertEqual(row['profile']['column_summary'][1]['distinct_nonmissing'], 7)
                        self.assertEqual(row['profile']['source_sha256'], hashlib.sha256(raw).hexdigest())
                        self.assertEqual(len(row['profile']['example_rows']), 5)
                        self.assertNotIn('text', row)
                        inspected.append(rows)
                        return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass', 'findings': []}]}
                    self.fail('Unexpected model operation: ' + label)

                client.ask_json.side_effect = answer
                arguments = {'goal': 'Explain table coverage; retain the complete supplied records',
                    'material_roots': [str(source)], 'publication': {'compile_pdf': False}, 'finish': True}
                result = writing.call('compose_document', arguments, identity='scoped-table')
                self.assertEqual(result.status, 'ok', result)
                self.assertEqual(len(inspected), 1)
                owner = ArtifactStore(root / 'tools/writing/scoped-table')
                snapshot = owner.read_json('request.json')['delivery_review']
                self.assertEqual(snapshot['text_admission']['contract'], SCOPED_DELIVERY_INSPECTION)
                self.assertTrue(any(path.endswith('.md') for path in result.artifacts))
                copies = [row for row in snapshot['artifacts'] if row['path'] in selected]
                for row in copies:
                    self.assertEqual(workspace.resolve(row['copies'][0]['path']).read_bytes(),
                        Path(row['path']).read_bytes())
                final = writing.final_review(result.data)
                review = final['delivery_review']
                self.assertEqual(review['status'], 'incomplete' if unsupported else 'reviewed')
                self.assertEqual(review['profiled_artifacts'][0]['reader_coverage']['row_count'], 50000)
                self.assertIn('all individual values', review['profiled_artifacts'][0]['unverified'])
                self.assertEqual(bool(review['uninspected']), unsupported)
                if unsupported:
                    self.assertNotIn('pending_candidate', result.data,
                        'Incomplete coverage must not be promoted to complete checked negative work')
                    self.assertNotIn('retained_candidate', result.data)
                observed = result.model_dump(mode='json')
                self.assertEqual(writing.continuation(observed, 'scoped-table') is not None, not unsupported)
                calls = client.ask_json.call_count
                replay = writing.call('compose_document', arguments, identity='scoped-table')
                self.assertEqual(replay.model_dump(mode='json'), observed)
                self.assertEqual(client.ask_json.call_count, calls)
                table.write_bytes(raw + b'50000,0\n')
                self.assertIsNone(writing.continuation(observed, 'scoped-table'))

    def test_inspection_views_do_not_depend_on_attachment_order_or_reject_reader_formats(self):
        from simple_ar.report.review_evidence import SCOPED_DELIVERY_INSPECTION, delivery_target_pointer
        _, _, writing, source, script, _, _, _ = self._document_work_fixture()
        script.write_text('#' + 'x' * 249998 + '\n', encoding='utf-8')
        table = source / 'results' / 'small.csv'
        table.write_text('value\n' + '1\n' * 5000, encoding='utf-8')
        paths = [str(table), str(script)]
        first = writing._freeze_delivery_review(paths, 'ordered', text_contract=SCOPED_DELIVERY_INSPECTION)
        second = writing._freeze_delivery_review(paths[::-1], 'reversed', text_contract=SCOPED_DELIVERY_INSPECTION)
        self.assertEqual(first, second)
        self.assertEqual({row['path']: row['coverage'] for row in first['artifacts']},
            {str(script): 'complete', str(table): 'table_profile'})
        records = source / 'results' / 'records.json'
        records.write_text(json.dumps([{'': i, ' ': None} for i in range(2000)]), encoding='utf-8')
        current = writing._freeze_delivery_review([str(records)], 'json-reader',
            text_contract=SCOPED_DELIVERY_INSPECTION)
        row, = current['artifacts']
        self.assertEqual(row['coverage'], 'table_profile')
        self.assertEqual(row['profile']['columns'], ['', ' '])
        self.assertEqual(delivery_target_pointer(row, 0), '/delivery_artifacts/0/profile')

    def test_document_focus_keeps_task_evidence_and_replay_keeps_original_scope(self):
        from types import SimpleNamespace
        root, workspace, writing, source, _, table, _, _ = self._document_work_fixture()
        focused = workspace.workspace / 'new-results'
        focused.mkdir()
        result_file = focused / 'summary.txt'
        result_file.write_text('A derived result; original conditions are in the supplied source.\n', encoding='utf-8')
        request = ComposeRequest(goal='Revise the result explanation against original conditions',
            material_roots=[str(focused)])
        calls = []
        def answer(system, prompt, **options):
            view = json.loads(prompt)
            calls.append(view)
            visible = {row[0] for inventory in view['inventories'] for row in inventory['files']}
            selected = [str(result_file), *([str(table)] if workspace.locator(str(table)) in visible else [])]
            if options['label'] == 'composition-selection-1':
                return {'read_windows': [{'path': path} for path in selected]}
            return {'title': 'Result and original conditions', 'materials': selected, 'delivery_artifacts': []}
        client = SimpleNamespace(ask_json=Mock(side_effect=answer))
        for name, added, expected in (('new-scope', [str(source)], True), ('old-scope', [], False)):
            owner = ArtifactStore(root / 'tools/writing' / name)
            selected, _ = writing._select_materials(request, owner, name, client, '', None,
                document_work=True, task_evidence_roots=added)
            inputs = owner.read_json('selection-input.json')
            self.assertEqual(str(source) in [row['project_path'] for row in inputs['inventories']], expected)
            self.assertEqual(str(table) in [row.path for row in selected.materials], expected)
            original = owner.resolve('selection-input.json').read_bytes()
            before = client.ask_json.call_count
            writing._select_materials(request, owner, name, client, '', None,
                document_work=True, task_evidence_roots=[str(source)])
            self.assertEqual(client.ask_json.call_count, before)
            self.assertEqual(owner.resolve('selection-input.json').read_bytes(), original)
        inaccessible = root / 'not-granted'
        inaccessible.mkdir()
        before = client.ask_json.call_count
        with self.assertRaises(ValueError):
            writing._select_materials(request, ArtifactStore(root / 'tools/writing/refused'),
                'refused', client, '', None, document_work=True, task_evidence_roots=[str(inaccessible)])
        self.assertEqual(client.ask_json.call_count, before)

    def test_document_work_selects_title_packages_real_materials_and_finishes_via_export(self):
        root, workspace, writing, source, script, table, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        # Roles are professional choices, not a PNG/PDF filename convention.
        body, attachment, legacy = source / 'a.png', source / 'b.pdf', source / 'c.svg'
        body.write_bytes(b'\x89PNG\r\n\x1a\nfixture image bytes')
        attachment.write_bytes(b'%PDF-1.4\n% fixture attachment bytes\n')
        legacy.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        selected_figures = [{'path': str(body), 'caption': 'Recorded result.'},
            {'path': str(attachment), 'placement': 'attachment', 'caption': 'Editable companion.'}, str(legacy)]
        original_answer = client.ask_json.side_effect

        def answer(system, prompt, **options):
            reply = original_answer(system, prompt, **options)
            if options['label'] == 'composition-selection-2':
                reply['figures'] = selected_figures
            elif options['label'] == 'composition-article':
                reply['draft_markdown'] += ('\n\n![Recorded result.](figures/selected-1.png)'
                    '\n\n![Legacy body asset](figures/selected-3.svg)')
            return reply

        client.ask_json.side_effect = answer
        arguments = {'goal': 'Explain the saved value and retain the necessary source and record',
            'material_roots': [str(source)], 'publication': {'compile_pdf': False}, 'finish': True}
        self.assertTrue(writing.finishes_request(arguments))
        self.assertFalse(writing.finishes_request({'finish': 1}))
        self.assertFalse(writing.finishes_request({}))
        result = writing.call('compose_document', arguments, identity='document-unit')
        self.assertEqual(result.status, 'ok', result)
        owner = ArtifactStore(root / 'tools/writing/document-unit')
        binding = owner.read_json('request.json')
        self.assertEqual(binding['work_source']['contract'], 'document_source.v1')
        self.assertEqual(binding['work_source']['access_roots'], [str(source)])
        self.assertNotIn('materials', binding['work_source']['intent'])
        self.assertEqual(owner.read_json('selection-input.json')['work_source'], binding['work_source'])
        self.assertEqual(binding['request']['title'], 'Recorded value and scope')
        self.assertEqual(owner.read_json('selection-input.json')['document_work'], 'document_work.v2')
        self.assertEqual(owner.read_json('selection-input.json')['figure_selection_contract'], 'compose_figure.v1')
        self.assertEqual(binding['request']['figures'], selected_figures)
        self.assertEqual(result.data['material_selection']['selected_figures'], selected_figures)
        self.assertEqual(result.data['material_selection']['figure_selection_contract'], 'compose_figure.v1')
        article_prompt = next(prompt for label, prompt in prompts if label == 'composition-article')
        handles = {row['artifact']: row for row in article_prompt['source_handles']
            if row.get('artifact', '').startswith('figures/')}
        self.assertEqual(handles['figures/selected-1.png']['metadata']['placement'], 'body')
        companion = handles['figures/selected-2.pdf']
        self.assertEqual(companion['metadata']['placement'], 'attachment')
        self.assertIn('separately delivered attachment', companion['summary'])
        self.assertNotIn('Embed as one standalone', companion['summary'])
        self.assertEqual(binding['delivery_review']['required_figures'],
            ['figures/selected-1.png', 'figures/selected-3.svg'])
        for number, path in enumerate((body, attachment, legacy), 1):
            target = f'workspace/manuscripts/document-unit/figures/selected-{number}{path.suffix}'
            self.assertIn(target, result.artifacts)
            self.assertEqual(workspace.resolve(target).read_bytes(), path.read_bytes())
            bound = next(row for row in binding['inputs'] if row['path'] == str(path))
            self.assertEqual(bound['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        manuscript = workspace.resolve(result.data['editable_markdown']).read_text(encoding='utf-8')
        self.assertNotIn('](figures/selected-2.pdf)', manuscript)
        manifest = workspace.resolve('workspace/manuscripts/document-unit/figures/figures_manifest.json')
        self.assertEqual({row['path']: row['caption'] for row in json.loads(manifest.read_text())['figures']},
            {'figures/selected-1.png': 'Recorded result.', 'figures/selected-2.pdf': 'Editable companion.'})
        self.assertEqual([label for label, _ in prompts], ['composition-selection-1',
            'composition-selection-2', 'composition-article', 'article-independent-review'])
        rows = binding['document_work']['materials']
        self.assertEqual({row['path'] for row in rows}, {
            'workspace/manuscripts/document-unit/materials/source0/code/calculate.py',
            'workspace/manuscripts/document-unit/materials/source0/results/record.txt'})
        snapshot = {row['path']: row for row in binding['delivery_review']['artifacts']}
        for row in rows:
            self.assertEqual(workspace.resolve(row['path']).read_bytes(), Path(row['source']).read_bytes())
            self.assertIn(row['path'], result.artifacts)
            self.assertEqual(snapshot[row['source']]['sha256'], row['sha256'])
            alias, = snapshot[row['source']]['copies']
            self.assertEqual(alias, {'path': str(workspace.resolve(row['path'])), 'sha256': row['sha256']})
            self.assertNotIn('producer', alias)
            self.assertNotIn(str(workspace.resolve(row['path'])), snapshot)
        copied_script = workspace.resolve(next(row['path'] for row in rows if row['source'] == str(script)))
        copied_table = workspace.resolve(next(row['path'] for row in rows if row['source'] == str(table)))
        self.assertEqual((copied_script.parent / '../results/record.txt').resolve(), copied_table)
        self.assertEqual((copied_script.parent / '../results/record.txt').read_bytes(), table.read_bytes())
        self.assertFalse(any('unselected' in path for path in result.artifacts))
        observed = result.model_dump(mode='json')
        continuation = writing.continuation(observed, 'document-unit')
        self.assertIsNotNone(continuation)
        self.assertEqual(continuation['tool_name'], 'export_report')
        delivered = continuation['arguments']['delivery']
        self.assertEqual(delivered['report'], {'status': 'canonical', 'manuscript': result.data['editable_markdown']})
        self.assertEqual(set(delivered['artifacts']), set(result.artifacts))
        self.assertFalse(continuation['arguments']['compile_pdf'])
        self.assertNotIn('delivery', result.data, 'Composition is not a terminal delivery receipt')
        # Successful presentation cannot hide a failed retained owner result.
        saved = owner.read_json('result.json')
        for change in ({'status': 'error'}, {'error': 'Interrupted owner commit'}):
            with self.subTest(retained_result=change):
                owner.write_json('result.json', {**saved, **change})
                self.assertIsNone(writing.continuation(observed, 'document-unit'))
        owner.write_json('result.json', saved)
        # Historical receipts omitted status; preserve that successful default.
        legacy = {key: value for key, value in saved.items() if key != 'status'}
        owner.write_json('result.json', legacy)
        self.assertIsNotNone(writing.continuation(observed, 'document-unit'))
        owner.write_json('result.json', saved)
        calls = client.ask_json.call_count
        resumed = writing.call('compose_document', arguments, identity='document-unit')
        self.assertEqual(resumed.model_dump(mode='json'), observed)
        self.assertEqual(client.ask_json.call_count, calls)
        # Metadata changes invalidate the bound recipe just like byte changes.
        selection_response = owner.read_json('selection-response.json')
        frozen_response = json.loads(json.dumps(selection_response))
        for field, value in (('placement', 'body'), ('caption', 'Changed caption.')):
            altered = json.loads(json.dumps(frozen_response))
            altered['figures'][1][field] = value
            owner.write_json('selection-response.json', altered)
            refused = writing.call('compose_document', arguments, identity='document-unit')
            self.assertEqual(refused.status, 'error', refused)
            self.assertEqual(client.ask_json.call_count, calls)
            self.assertEqual(workspace.resolve(result.data['editable_markdown']).read_text(encoding='utf-8'), manuscript)
        owner.write_json('selection-response.json', frozen_response)
        raw = attachment.read_bytes()
        attachment.write_bytes(raw + b'changed')
        self.assertIsNone(writing.continuation(observed, 'document-unit'))
        refused = writing.call('compose_document', arguments, identity='document-unit')
        self.assertEqual(refused.status, 'error', refused)
        self.assertEqual(client.ask_json.call_count, calls)
        attachment.write_bytes(raw)
        source_bytes = table.read_bytes()
        table.write_bytes(source_bytes + b'changed original input')
        with self.assertRaisesRegex(ValueError, 'Report source changed'):
            writing.check_delivery(result.data)
        self.assertIsNone(writing.continuation(observed, 'document-unit'))
        table.write_bytes(source_bytes)
        copy = workspace.resolve(rows[0]['path'])
        original = copy.read_bytes()
        copy.write_bytes(original + b'changed')
        self.assertIsNone(writing.continuation(observed, 'document-unit'))
        copy.write_bytes(original)

    def test_material_selection_keeps_figure_roles_through_inventory_merge_and_replay(self):
        from simple_ar.tools.writing import ComposeFigure
        for document_work in (False, True):
            with self.subTest(document_work=document_work):
                root, workspace, writing, source, _, table, client, _ = self._document_work_fixture()
                first, second = source / 'first.svg', source / 'second.svg'
                for path in (first, second):
                    path.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
                extras = [source / f'extra-{number}.svg' for number in range(12)]
                for path in extras:
                    path.write_bytes(first.read_bytes())
                explicit = {'path': str(first), 'caption': 'Explicit caption.', 'placement': 'attachment'}
                figures = [{'path': 'source0/first.svg', 'caption': 'Selector caption.'},
                    {'path': str(second), 'placement': 'attachment'}]
                request = ComposeRequest(goal='Select figures from evidence', title='Existing title',
                    material_roots=[str(source)], figures=[explicit])
                response = {'materials': [str(table)], 'figures': figures}
                if document_work:
                    response['title'] = 'Selected title'
                client.ask_json.side_effect = lambda *args, **kwargs: (
                    {'read_windows': [{'path': str(table)}]}
                    if kwargs['label'] == 'composition-selection-1' else response)
                owner = ArtifactStore(root / 'tools/writing/figure-selection')
                selected, coverage = writing._select_materials(request, owner, 'figure-selection', client,
                    '', None, document_work=document_work)
                self.assertEqual(selected.figures, [ComposeFigure.model_validate(explicit),
                    ComposeFigure.model_validate(figures[1])])
                self.assertEqual(coverage['selected_figures'], figures)
                frozen = owner.resolve('selection-input.json').read_bytes()
                client.ask_json.reset_mock()
                replay, repeated = writing._select_materials(request, owner, 'figure-selection', client,
                    '', None, document_work=document_work)
                self.assertEqual(replay, selected)
                self.assertEqual(repeated, coverage)
                # Cached response consumers must still enforce the same schema,
                # authorized inventory, path uniqueness and combined limit.
                for bad in ([figures[1], figures[1]],
                        [figures[0], {'path': str(first), 'placement': 'attachment'}],
                        [{'path': str(root / 'outside.svg'), 'placement': 'attachment'}],
                        [{'path': str(second), 'placement': 'appendix'}],
                        [{'path': str(second), 'caption': ' '}],
                        [str(second)] * 13, [str(path) for path in extras]):
                    with self.subTest(figures=bad):
                        owner.write_json('selection-response.json', {**response, 'figures': bad})
                        with self.assertRaises(ValueError):
                            writing._select_materials(request, owner, 'figure-selection', client,
                                '', None, document_work=document_work)
                owner.write_json('selection-response.json', response)
                self.assertEqual(owner.resolve('selection-input.json').read_bytes(), frozen)
                saved_input = owner.read_json('selection-input.json')
                owner.write_json('selection-input.json', {**saved_input,
                    'figure_selection_contract': 'compose_figure.unknown'})
                with self.assertRaisesRegex(ValueError, 'Unknown composition figure selection contract'):
                    writing._select_materials(request, owner, 'figure-selection', client,
                        '', None, document_work=document_work)
                owner.write_json('selection-input.json', saved_input)
                client.ask_json.assert_not_called()

    def test_declaration_policy_is_frozen_by_composition_snapshot(self):
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
        for policy in (None, CLAIM_ADOPTION_V2):
            with self.subTest(policy=policy):
                root, workspace, writing, source, _, _, client, _ = self._document_work_fixture()
                writing.declaration_adoption_contract = policy
                arguments = {'goal': 'Explain the retained value', 'material_roots': [str(source)]}
                result = writing.call('compose_document', arguments, identity='policy')
                self.assertEqual(result.status, 'ok', result)
                store = ArtifactStore(root / 'tools/writing/policy')
                original = store.resolve('request.json').read_bytes()
                snapshot = store.read_json('request.json')['delivery_review']
                self.assertEqual(snapshot.get('declaration_adoption_contract'), policy)
                selection = store.read_json('selection-input.json')
                self.assertEqual(selection.get('selection_validation'), 'observed_inputs.v2' if policy else None)
                self.assertEqual(selection.get('producer_unit_contract'),
                    'professional_delivery_unit.v1' if policy else None)
                if policy is None:
                    self.assertNotIn('declaration_adoption_contract', snapshot)
                writing.declaration_adoption_contract = CLAIM_ADOPTION_V2 if policy is None else None
                calls = client.ask_json.call_count
                replay = writing.call('compose_document', arguments, identity='policy')
                self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
                self.assertEqual(client.ask_json.call_count, calls)
                self.assertEqual(store.resolve('request.json').read_bytes(), original)

    def test_pending_candidate_routes_repairs_without_adopting_its_verdict(self):
        root, workspace, writing, source, _, _, _, _ = self._document_work_fixture()
        result = writing.call('compose_document', {'goal': 'Explain the retained value',
            'material_roots': [str(source)]}, identity='pending-review')
        self.assertEqual(result.status, 'ok', result)
        data = dict(result.data)
        final = writing.final_review(data)
        from simple_ar.report.schema import ReportSectionDraft
        sections = [ReportSectionDraft(section_id='results', heading='Results',
            draft_markdown='Retained improvement, awaiting attachment repair.').model_dump(mode='json')]
        body = '# Candidate\n\nRetained improvement, awaiting attachment repair.'
        review = {'review_status': 'needs_revision', 'delivery_review': {'status': 'needs_revision',
            'defects': [{'type': 'delivery_artifact_defect'}]}, 'assembly_identity': {
                'body_sha256': hashlib.sha256(body.encode()).hexdigest(),
                'sections_sha256': hashlib.sha256(json.dumps(sections, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()}}
        candidate = {'sections': sections, 'report_body_markdown': body, 'independent_review': review}
        locator = data['version'] + '/pending_candidate.json'
        path = workspace.resolve(locator, write=True)
        path.write_text(json.dumps(candidate), encoding='utf-8')
        data['pending_candidate'] = locator
        data['artifact_sha256'] = {**data['artifact_sha256'], locator: hashlib.sha256(path.read_bytes()).hexdigest()}
        self.assertEqual(writing.repair_review(data), review)
        self.assertEqual(writing.final_review(data), final)
        self.assertEqual(writing.repair_candidate(data)['report_body_markdown'], body)
        self.assertIsNone(writing.continuation({**result.model_dump(mode='json'), 'data': data}, 'pending-review'))
        path.write_text(json.dumps({**candidate, 'report_body_markdown': 'Tampered'}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Pending candidate changed'):
            writing.repair_review(data)

    def test_professional_final_selection_can_correct_an_uninspected_window(self):
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
        root, workspace, writing, source, script, table, client, prompts = self._document_work_fixture()
        writing.declaration_adoption_contract = CLAIM_ADOPTION_V2
        original_answer = client.ask_json.side_effect

        def answer(system, prompt, **options):
            if options['label'] == 'composition-selection-2':
                view = json.loads(prompt)
                self.assertEqual(view['response_schema']['properties']['sources']['maxItems'], 0)
                return {'title': 'Recorded value and scope', 'sources': [
                    {'document_record': str(path), 'questions': ['Explain the recorded value']}
                    for path in (script, table)]}
            if options['label'] == 'composition-selection-3':
                view = json.loads(prompt)
                self.assertIn('sources', view['response_correction']['diagnostic'])
                self.assertEqual(len(view['observations']), 2, 'A role correction does not retire valid observations')
                return {'title': 'Recorded value and scope', 'materials': [str(source / 'unselected.txt')],
                    'delivery_artifacts': [str(script), str(table)]}
            if options['label'] == 'composition-selection-4':
                view = json.loads(prompt)
                self.assertIn('not inspected', json.dumps(view['read_admission']))
                self.assertEqual(view['observations'], [], 'The unrelated old ranges were archived, not supplied')
                return {'read_windows': [{'path': str(table)}]}
            return original_answer(system, prompt, **options)

        client.ask_json.side_effect = answer
        result = writing.call('compose_document', {'goal': 'Explain the selected original evidence',
            'material_roots': [str(source)]}, identity='selection-correction')
        self.assertEqual(result.status, 'ok', result)
        self.assertEqual(client.ask_json.call_count, 7)
        store = ArtifactStore(root / 'tools/writing/selection-correction')
        self.assertEqual(store.read_json('selection-input.json')['selection_validation'], 'observed_inputs.v2')
        self.assertTrue(store.read_json('selection-steps/2/response-error.json')['correction_allowed'])
        archived = store.read_json('selection-steps/3/input.json')['observations']
        self.assertEqual({row['request']['path'] for row in archived}, {str(script), str(table)})
        active = store.read_json('selection-steps/5/input.json')['observations']
        self.assertEqual([row['request']['path'] for row in active], [str(table)])
        self.assertEqual(active[0]['data']['sha256'], hashlib.sha256(table.read_bytes()).hexdigest())
        self.assertEqual(store.read_json('selection-steps/4/response.json'),
            {'read_windows': [{'path': str(table)}]})
        self.assertNotIn(str(source / 'unselected.txt'),
            [row['path'] for row in store.read_json('request.json')['inputs']])
        calls = client.ask_json.call_count
        replay = writing.call('compose_document', {'goal': 'Explain the selected original evidence',
            'material_roots': [str(source)]}, identity='selection-correction')
        self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)

    def test_source_questions_bind_only_inventoried_original_bundles(self):
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.contracts import DocumentRecord, TextChunk
        root, workspace, writing, source, script, _, client, _ = self._document_work_fixture()
        writing.declaration_adoption_contract = CLAIM_ADOPTION_V2
        original = source / 'method.json'
        bundle = DocumentBundle.from_rows(documents=[DocumentRecord(document_id='method',
            title='Original definition', source='supporting_material').to_row()], chunks=[TextChunk(
            chunk_id='definition', document_id='method', text='Value denotes the saved estimate.',
            line_start=1, line_end=1).to_row()])
        original.write_text(json.dumps(bundle.to_handoff_dict()), encoding='utf-8')
        source_ref = {'document_record': workspace.locator(str(original)), 'questions': ['Value definition']}
        client.ask_json.side_effect = [
            {'sources': [{'document_record': str(script), 'questions': ['Value definition']}]},
            {'sources': [source_ref]}]
        owner = ArtifactStore(root / 'tools/writing/source-roles')
        request = ComposeRequest(goal='Explain the saved estimate', title='Evidence', material_roots=[str(source)])
        selected, coverage = writing._select_materials(request, owner, 'source-roles', client,
            '', None, document_work=True)
        self.assertEqual(selected.sources[0].document_record, source_ref['document_record'])
        self.assertEqual(coverage['selected_sources'][0]['questions'], source_ref['questions'])
        schema = json.loads(client.ask_json.call_args_list[0].args[1])['response_schema']
        self.assertEqual(set(schema['$defs']['InventoriedSource']['properties']['document_record']['enum']),
            {str(original), source_ref['document_record']})
        self.assertTrue(owner.read_json('selection-steps/1/response-error.json')['correction_allowed'])
        client.ask_json.reset_mock()
        replay, _ = writing._select_materials(request, owner, 'source-roles', client,
            '', None, document_work=True)
        self.assertEqual(replay, selected)
        client.ask_json.assert_not_called()
        # A saved historical decision is not reinterpreted or corrected under
        # the new finite-choice schema, even when it contains the old failure.
        binding = owner.read_json('selection-input.json')
        owner.write_json('selection-input.json', {**binding, 'selection_validation': 'observed_windows.v1'})
        owner.write_json('selection-response.json', {'sources': [
            {'document_record': str(script), 'questions': ['Value definition']}]})
        with self.assertRaisesRegex(ValueError, 'authorized original document bundle'):
            writing._select_materials(request, owner, 'source-roles', client, '', None, document_work=True)
        client.ask_json.assert_not_called()

    def test_registered_professional_unit_is_delivered_whole_without_repackaging(self):
        from types import SimpleNamespace
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.tools.writing import freeze_report_requirement
        from tests.test_professional_work_loop import ProfessionalWorkLoopTests
        from simple_ar.tools.producer_declarations import professional_units
        fixture = ProfessionalWorkLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        goal = 'Deliver the recorded result with its reusable program unit'
        requirement = freeze_report_requirement({'outline_strategy': 'auto',
            'reviewer': 'llm', 'document_review': True, 'review_scope': 'document',
            'max_review_iterations': 0,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='article')
        app = ToolApplication.create(fixture.root, goal, model='fixture', sources={},
            services=fixture.services, professional_inputs=True, allow_execution=True,
            report_requirement=requirement, max_turns=10, max_tokens=600000, max_attempts=40,
            process_limits={'process_invocations': 3, 'process_wall_seconds': 30})
        # Exercise the historical whole-text contract, including its pre-author
        # rejection and replay behavior. Current scoped inspection has its own
        # large-table/candidate-preservation acceptance below.
        app.writing.document_source_contract = None
        # Preserve this historical unit-binding/recovery contract; the cooperative
        # publication test owns registered preparation handbacks and physical resume.
        app.writing.publication_inspection = None
        self.assertEqual(app.state['config']['professional_work'], 'requests.v1')
        self.assertTrue(app.writing.professional_inputs)
        self.assertEqual(app._report_inputs()['evidence_contract'], 'materials.v1')
        predecessors = [app.workspace.workspace / name for name in
            ('original.py', 'failed-predecessor.py')]
        for path in predecessors:
            path.write_bytes(b'# Observed design context, superseded by the registered unit.\n')
        predecessor_bytes = {path: path.read_bytes() for path in predecessors}
        # This earlier generated result is scientific material, not a declared
        # attachment. Keep its real producer distinct from the current unit.
        context_root = app.workspace.workspace / 'earlier-results'
        context_root.mkdir()
        context_script, context_csv = context_root / 'summarize.py', context_root / 'key_numbers.csv'
        context_script.write_bytes(b'from pathlib import Path\nPath("key_numbers.csv").write_text("value\\n13\\n")\n')
        context_result = app.workspace.call('run_process', {
            'argv': ['python', str(context_script)], 'cwd': str(context_root), 'timeout_sec': 3,
            'inputs': [str(context_script)], 'outputs': [str(context_csv)]}, identity='context-producer')
        self.assertEqual((context_result.status, context_result.data.get('returncode')), ('ok', 0), context_result)
        context_bytes = {path: path.read_bytes() for path in (context_script, context_csv)}
        context_receipt = fixture.root / context_result.data['receipt']
        context_receipt_bytes = context_receipt.read_bytes()
        context_provenance = app.workspace.artifact_provenance([str(context_csv)])[str(context_csv)]
        self.assertEqual(context_provenance['producer']['invocation_id'], 'context-producer')
        self.assertEqual(app.workspace.resolved_versions(context_provenance['producer']['inputs']),
            {context_script: hashlib.sha256(context_bytes[context_script]).hexdigest()})
        code = ("from pathlib import Path\n"
            "p = Path('bundle'); p.mkdir()\n"
            "(p/'run.py').write_text('from pathlib import Path\\nprint(Path(__file__).with_name(\"records.csv\").read_text())\\n')\n"
            "(p/'records.csv').write_text('value\\n21\\n')\n"
            "(p/'README.md').write_text('说明' * 18000, encoding='utf-8')\n"
            "(p/'reference.txt').write_bytes((p/'README.md').read_bytes())\n"
            "(p/'table.csv').write_text('value\\n21\\n')\n"
            "(p/'inventory').write_text('Saved inputs and program inventory\\n')\n"
            "for index in range(18):\n"
            "    (p/f'aux-{index}.txt').write_text('retained output')\n")
        proposal = fixture._proposal(code)
        proposal['files'].extend({'path': f'library/part-{index}.py',
            'content': '# Retained source unit member.\n'} for index in range(11))
        # An old version explicitly retained IN the unit remains mandatory.
        proposal['files'][1] = {'path': 'library/prior-version.py',
            'content': predecessors[1].read_text(encoding='utf-8')}
        proposal['execution_plan']['outputs'] = ['bundle/run.py', 'bundle/records.csv',
            'bundle/README.md', 'bundle/reference.txt', 'bundle/table.csv', 'bundle/inventory',
            *(f'bundle/aux-{index}.txt' for index in range(18))]
        fixture.responses = [proposal]
        preparation = fixture._prepare()
        preparation['arguments']['source_windows'] = [{'path': str(path), 'full_digest': True}
            for path in predecessors]
        fixture._run(app, actions=[preparation])
        self.assertEqual(fixture._observations(app)[-1]['status'], 'ok', fixture._observations(app)[-1])
        _, token = fixture._pending_execution(app)
        fixture._run(app, approved_execution=token)
        execution = fixture._observations(app)[-1]
        self.assertEqual(execution['tool_name'], 'execute_implementation')
        candidate = Path(execution['data']['cwd'])
        bundle = candidate / 'bundle'
        material_roots = [str(bundle), str(context_root)]
        units = professional_units(app.writing, material_roots, [str(bundle / 'run.py')])
        unit, = units
        self.assertEqual(len(unit['files']), 36, 'All 12 sources plus 24 actual outputs belong to the unit')
        self.assertEqual(set(unit['files']), {*(str(candidate / row['path']) for row in proposal['files']),
            *(str(candidate / name) for name in proposal['execution_plan']['outputs'])})
        provenance = app.workspace.artifact_provenance([str(bundle / 'run.py')])
        consumed = app.workspace.resolved_versions(provenance[str(bundle / 'run.py')]['producer']['inputs'])
        approval = json.loads(app.workspace.resolve(execution['data']['execution_ref']).read_bytes())['approval']['binding']
        guarded = app.workspace.resolved_versions(approval['source_sha256'])
        context = app.workspace.resolved_versions(approval['context_sha256'])
        for path, raw in predecessor_bytes.items():
            self.assertEqual(guarded[path], hashlib.sha256(raw).hexdigest())
            self.assertEqual(context[path], hashlib.sha256(raw).hexdigest())
            self.assertNotIn(path, consumed, 'Observed design context is not an execution-consumption claim')
            self.assertNotIn(str(path), unit['files'])
        retained_version = candidate / 'library/prior-version.py'
        self.assertIn(str(retained_version), unit['files'])
        self.assertEqual(retained_version.read_bytes(), predecessor_bytes[predecessors[1]])
        original = {Path(path): Path(path).read_bytes() for path in unit['files']}
        from simple_ar.tools.producer_declarations import professional_unit_input_owners
        input_owners = professional_unit_input_owners(app.writing, units)
        from simple_ar.tools.producer_declarations import _proof
        self.assertEqual(professional_unit_input_owners(_proof(_proof(app.writing)), units), input_owners)
        for row in proposal['files']:
            owners, = [input_owners[str(candidate / row['path'])]]
            self.assertEqual(len(owners), 1)
            self.assertEqual(owners[0]['invocation_id'], Path(execution['data']['execution_ref']).parent.name)
        self.assertFalse(set(map(str, predecessors)).intersection(input_owners))
        self.assertNotIn(str(bundle / 'table.csv'), input_owners, 'An output is not a registered source input')
        expect_units = True

        def answer(system, prompt, **options):
            view = json.loads(prompt)
            label = options['label']
            if label.startswith('composition-selection'):
                if expect_units:
                    self.assertIn('entire proven source/output unit without recopying', system)
                    self.assertIn('32000-byte-per-file/96000-byte-total', system)
                    self.assertIn('256000-byte total text limit', system)
                else:
                    self.assertNotIn('entire proven source/output unit without recopying', system)
                if not view['observations']:
                    return {'read_windows': [{'path': str(path)} for path in (bundle / 'table.csv', context_csv)]}
                return {'title': 'Provisional selection title',
                    'materials': [str(bundle / 'table.csv'), str(context_csv)],
                    'delivery_artifacts': [str(bundle / 'run.py'), str(bundle / 'table.csv')]}
            if label == 'composition-article':
                if view.get('edit_contract'):
                    return {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': view['prior_draft'],
                            'new_text': '## Results\n\nThe saved value is 21.'}]}
                return {'draft_markdown': '## Results\n\nThe recorded value is 21.'}
            if label == 'article-independent-review':
                subjects = {row['path']: row for row in view['delivery_artifacts']}
                self.assertEqual(set(unit['files']) <= set(subjects), expect_units)
                if expect_units:
                    self.assertEqual(set(subjects), set(unit['files']),
                        'Consumed ancestors do not expand the registered review responsibility')
                self.assertTrue({str(bundle / 'run.py'), str(bundle / 'table.csv')} <= set(subjects))
                for path, raw in original.items():
                    if str(path) in subjects:
                        self.assertEqual(subjects[str(path)]['coverage'], 'complete')
                        self.assertEqual(subjects[str(path)]['text'], raw.decode('utf-8'))
                if expect_units:
                    self.assertEqual(subjects[str(bundle / 'README.md')]['sha256'],
                        subjects[str(bundle / 'reference.txt')]['sha256'])
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass', 'findings': []}]}
            self.fail(label)

        app.writing.client_factory = Mock(return_value=SimpleNamespace(ask_json=Mock(side_effect=answer)))
        arguments = {'goal': goal,
            'material_roots': material_roots, 'publication': {'compile_pdf': False}, 'finish': True}
        result = app.writing.call('compose_document', arguments, identity='whole-unit')
        self.assertEqual(result.status, 'ok', result)
        self.assertTrue(set(unit['files']) <= set(result.artifacts))
        self.assertTrue(set(context_bytes).isdisjoint(app.workspace.resolve(path) for path in result.artifacts))
        self.assertEqual(result.data['document_work']['materials'], [])
        self.assertEqual(result.data['document_work']['producer_units'], units)
        self.assertFalse((app.workspace.resolve(result.data['version']) / 'materials').exists())
        export = app.writing.continuation(result.model_dump(mode='json'), 'whole-unit')
        self.assertEqual(export['arguments']['title'], '', 'Exporter consumes the canonical H1, not the selection title')
        self.assertTrue(set(unit['files']) <= set(export['arguments']['delivery']['artifacts']))
        store = ArtifactStore(app.session.store.root / 'tools/writing/whole-unit')
        binding = store.read_json('request.json')
        self.assertEqual(binding['report_binding'], app._report_inputs())
        self.assertEqual(binding['report_binding']['evidence_contract'], 'materials.v1')
        self.assertEqual(binding['input_contract'], 'owner.v1')
        self.assertEqual(binding['delivery_subject_contract'], 'declared_delivery_subjects.v1')
        self.assertNotIn('delivery_subject_contract', binding['request'])
        self.assertEqual(binding['document_work']['publication_title_contract'], 'canonical_heading.v1')
        self.assertNotIn('figures', arguments, 'Normalization must not change caller intent or caller arguments')
        self.assertEqual(binding['request']['figures'], [])
        self.assertEqual(store.read_json('selection-input.json')['producer_unit_contract'],
            'professional_delivery_unit.v1')
        selection_binding = store.read_json('selection-input.json')
        self.assertEqual(selection_binding['delivery_subject_contract'], 'declared_delivery_subjects.v1')
        self.assertNotIn('delivery_subject_contract', selection_binding['request'])
        self.assertEqual(binding['request']['delivery_artifacts'],
            [str(bundle / 'run.py'), str(bundle / 'table.csv')])
        # A saved unit contract alone cannot upgrade an interrupted historical
        # selection. Absence keeps the old policy and the no-blind-replay gate.
        old_selection = {key: value for key, value in selection_binding.items()
            if key != 'delivery_subject_contract'}
        interrupted_store = ArtifactStore(app.session.store.root / 'tools/writing/whole-unit-old-selection')
        interrupted_store.write_json('selection-input.json', old_selection)
        calls = app.writing.client_factory.return_value.ask_json.call_count
        factories = app.writing.client_factory.call_count
        with patch.object(app.writing, '_select_materials', wraps=app.writing._select_materials) as select:
            interrupted = app.writing.call('compose_document', arguments, identity='whole-unit-old-selection')
        self.assertEqual(interrupted.status, 'error')
        self.assertIn('Interrupted material selection', interrupted.error)
        self.assertTrue(select.call_args.kwargs['adopt_units'])
        self.assertIsNone(select.call_args.kwargs['delivery_subject_contract'])
        self.assertEqual(interrupted_store.read_json('selection-input.json'), old_selection)
        self.assertFalse(interrupted_store.exists('request.json'))
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual(app.writing.client_factory.call_count, factories)
        interrupted_store.write_json('selection-input.json', {
            **old_selection, 'delivery_subject_contract': 'declared_delivery_subjects.unknown'})
        with patch.object(app.writing, '_select_materials') as select:
            rejected = app.writing.call('compose_document', arguments, identity='whole-unit-old-selection')
        self.assertEqual(rejected.status, 'error')
        self.assertIn('Unknown retained delivery subject contract', rejected.error)
        select.assert_not_called()
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual(app.writing.client_factory.call_count, factories)
        # This saved request is the resolved recipe, not a second caller API.
        # Roots retain investigation provenance alongside selected windows.
        self.assertEqual(binding['request']['material_roots'], material_roots)
        self.assertEqual({row['path'] for row in binding['request']['materials']},
            {str(bundle / 'table.csv'), str(context_csv)})
        with self.assertRaisesRegex(ValueError, 'Choose explicit material windows OR material_roots'):
            ComposeRequest.model_validate(binding['request'])
        calls = app.writing.client_factory.return_value.ask_json.call_count
        frozen = {path: path.read_bytes() for path in store.root.rglob('*') if path.is_file()}
        resolved, parent_binding, linkage = app.writing._prior_inputs(ComposeRequest(
            goal='Reuse the same proven evidence recipe', prior_draft=result.data['editable_markdown'],
            reuse_prior_inputs=True), 'whole-unit-reuse')
        self.assertEqual(resolved.materials,
            [FileWindow.model_validate(row) for row in binding['request']['materials']])
        self.assertEqual(resolved.sources, [])
        self.assertEqual(resolved.material_roots, [])
        self.assertEqual(parent_binding, binding)
        self.assertEqual(linkage['owner'], 'whole-unit')
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual({path: path.read_bytes() for path in store.root.rglob('*') if path.is_file()}, frozen)
        self.assertEqual(set(unit['files']) | {str(context_csv)}, {row['path'] for row in binding['inputs']})
        context_input, = [row for row in binding['inputs'] if row['path'] == str(context_csv)]
        self.assertEqual(context_input['sha256'], hashlib.sha256(context_bytes[context_csv]).hexdigest())
        self.assertEqual(set(unit['files']), {row['path'] for row in binding['delivery_review']['artifacts']})
        # Explicitly supplied sources remain subjects even when the unit also
        # consumed them. Small files isolate ownership from admission limits.
        for path in predecessors:
            with self.subTest(explicit_source=path.name):
                paths, _ = app.writing._joint_delivery_paths(
                    [{'path': str(bundle / 'run.py')}, {'path': str(path)}], [str(path)],
                    producer_units=units, delivery_copies=set(), declared_subjects_only=True)
                self.assertEqual({app.workspace.resolve(item) for item in paths},
                    {*(Path(item) for item in unit['files']), path})
                snapshot = app.writing._freeze_delivery_review(paths, 'explicit-' + path.stem,
                    copies=[], producer_units=units)
                subject, = [row for row in snapshot['artifacts'] if row['path'] == str(path)]
                self.assertEqual(subject['coverage'], 'complete')
                self.assertEqual(subject['text'], predecessor_bytes[path].decode('utf-8'))
        with self.assertRaises(ValueError):
            ComposeRequest.model_validate({**arguments, 'delivery_artifacts': list(unit['files'])[:33]})
        self.assertEqual(binding['delivery_review']['text_admission']['contract'], 'professional_unit_text.v2')
        self.assertGreater(sum(len(raw) for raw in original.values()), 96000)
        legacy = app.writing._freeze_delivery_review(list(unit['files']), 'legacy-unit-windows')
        self.assertNotIn('text_admission', legacy)
        self.assertEqual(next(row for row in legacy['artifacts']
            if row['path'] == str(bundle / 'README.md'))['coverage'], 'oversized')
        with self.assertRaisesRegex(ValueError, '32 original subjects'):
            app.writing._freeze_delivery_review(list(unit['files']), 'legacy-unit-count', copies=[])
        with self.assertRaisesRegex(ValueError, '32 files'):
            app.writing._joint_delivery_paths([], list(unit['files']), delivery_copies=set())
        identity = {'composition': result.data['version'], 'artifact_sha256': result.data['artifact_sha256'],
            'receipt_sha256': hashlib.sha256(store.resolve('result.json').read_bytes()).hexdigest()}
        app.writing.check_delivery_set(identity, result.artifacts, set())
        with self.assertRaisesRegex(ValueError, 'complete registered professional unit'):
            app.writing.check_delivery_set(identity, [path for path in result.artifacts
                if path != str(bundle / 'records.csv')], set())
        calls = app.writing.client_factory.return_value.ask_json.call_count
        policy = app.writing.declaration_adoption_contract
        app.writing.declaration_adoption_contract = None
        replay = app.writing.call('compose_document', arguments, identity='whole-unit')
        self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        app.writing.declaration_adoption_contract = policy
        unknown = {**binding, 'delivery_subject_contract': 'declared_delivery_subjects.unknown'}
        store.write_json('request.json', unknown)
        factories = app.writing.client_factory.call_count
        rejected = app.writing.call('compose_document', arguments, identity='whole-unit')
        self.assertEqual(rejected.status, 'error')
        self.assertIn('Unknown retained delivery subject contract', rejected.error)
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual(app.writing.client_factory.call_count, factories)
        store.write_json('request.json', binding)
        unknown = json.loads(json.dumps(binding))
        unknown['delivery_review']['text_admission']['contract'] = 'professional_unit_text.unknown'
        store.write_json('request.json', unknown)
        rejected = app.writing.call('compose_document', arguments, identity='whole-unit')
        self.assertEqual(rejected.status, 'error')
        self.assertIn('Unknown joint inspection text admission contract', rejected.error)
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        store.write_json('request.json', binding)
        # Missing attachment coverage is not a request to regenerate prose.
        # Selection can still observe material, but no author/reviewer call
        # should be charged for an input that cannot be inspected at all.
        freeze = app.writing._freeze_delivery_review
        def incomplete_inputs(*args, **kwargs):
            snapshot = freeze(*args, **kwargs)
            subject = snapshot['artifacts'][0]
            subject['coverage'] = 'incomplete'
            subject.pop('text', None)
            return snapshot
        calls = app.writing.client_factory.return_value.ask_json.call_count
        with patch.object(app.writing, '_freeze_delivery_review', side_effect=incomplete_inputs):
            gap = app.writing.call('compose_document', arguments, identity='whole-unit-gap')
        self.assertEqual(gap.status, 'error')
        self.assertEqual(gap.data['failure_kind'], 'inspection_inputs_incomplete')
        self.assertTrue(gap.data['uninspected'])
        self.assertIn('Rewriting prose cannot resolve', gap.data['next_action'])
        labels = [call.kwargs.get('label') for call in
            app.writing.client_factory.return_value.ask_json.call_args_list[calls:]]
        self.assertTrue(all(label.startswith('composition-selection') for label in labels), labels)
        self.assertFalse(app.writing.can_return_gap(gap.model_dump(mode='json'), 'whole-unit-gap'))
        self.assertFalse((app.session.store.root / 'tools/writing/whole-unit-gap/writer').exists())
        # A completed editorial diagnosis is a revision request, not another
        # evidence-discovery task. Repeated access roots must not force paid
        # reselection; a new user event or evidence gap still must.
        answer_without_revision = app.writing.client_factory.return_value.ask_json.side_effect
        def editorial_review(system, prompt, **options):
            if options['label'] == 'article-independent-review':
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'editorial', 'type': 'repetition', 'severity': 'major',
                        'required_action': 'revise', 'message': 'Remove repeated phrasing.'}]}]}
            return answer_without_revision(system, prompt, **options)
        app.writing.client_factory.return_value.ask_json.side_effect = editorial_review
        prior = app.writing.call('compose_document', arguments, identity='whole-unit-editorial')
        self.assertEqual(prior.status, 'ok', prior)
        self.assertEqual(app.writing.final_review(prior.data)['review_status'], 'needs_revision')
        app.writing.client_factory.return_value.ask_json.side_effect = answer_without_revision
        correction = {**arguments, 'prior_draft': prior.data['editable_markdown']}
        request = ComposeRequest.model_validate(correction)
        current = app._report_inputs()
        prior_directory = str(Path(prior.data['editable_markdown']).parent)
        self.assertIsNotNone(app.writing._document_revision_feedback(request.model_copy(
            update={'material_roots': [prior_directory]}), current))
        self.assertIsNotNone(app.writing._document_revision_feedback(request.model_copy(
            update={'material_roots': [*request.material_roots, prior_directory]}), current))
        extra = app.session.store.root / 'workspace' / 'new-material'
        extra.mkdir()
        self.assertIsNone(app.writing._document_revision_feedback(request.model_copy(
            update={'material_roots': [str(extra)]}), current))
        with patch.object(app.writing, '_inspection_interrupted', return_value=True):
            self.assertIsNone(app.writing._document_revision_feedback(request, current))
            self.assertTrue(app.writing.can_return_gap(prior.model_dump(mode='json'), 'whole-unit-editorial'),
                'A metadata shortcut cannot revoke the independently proved complete negative work')
        self.assertTrue(app.writing._inspection_interrupted({'unresolved': [
            {'type': 'document_revision_unavailable'}]}))
        self.assertFalse(app.writing._inspection_interrupted({'unresolved': [
            {'type': 'document_revision_unresolved'}]}))
        # A declaration still needing prose disclosure does not invalidate
        # observed source bytes. Attachment defects/coverage gaps do.
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
        original_review = app.writing.final_review(prior.data)
        receipt_store, receipt_binding, _ = app.writing._composition_receipt('whole-unit-editorial')
        original_binding = receipt_store.resolve('request.json').read_bytes()
        try:
            changed_binding = json.loads(json.dumps(receipt_binding))
            changed_binding['inputs'][0]['sha256'] = '0' * 64
            receipt_store.write_json('request.json', changed_binding)
            self.assertFalse(app.writing.can_return_gap(prior.model_dump(mode='json'), 'whole-unit-editorial'))
        finally:
            receipt_store.resolve('request.json').write_bytes(original_binding)
        disclosure = {**original_review, 'delivery_review': {
            'status': 'needs_revision', 'defects': [], 'uninspected': [],
            'declaration_adoption': {'contract': CLAIM_ADOPTION_V2}}}
        with patch.object(app.writing, 'final_review', return_value=disclosure), patch(
                'simple_ar.report.writing.validate_document_inspection'):
            self.assertIsNotNone(app.writing._document_revision_feedback(request, current))
            for field in ('defects', 'uninspected'):
                with self.subTest(blocking_attachment=field):
                    disclosure['delivery_review'][field] = [{'path': 'fixture'}]
                    self.assertIsNone(app.writing._document_revision_feedback(request, current))
                    disclosure['delivery_review'][field] = []
        self.assertIsNone(app.writing._document_revision_feedback(request,
            {**current, 'requests': [*current['requests'], 'Include additional original material']}))
        with patch.object(app.writing, 'final_review', return_value={
                'review_status': 'needs_revision', 'delivery_review': {'status': 'reviewed'},
                'section_reviews': [{'findings': [{'type': 'evidence_gap', 'required_action': 'verify'}]}]}):
            self.assertIsNone(app.writing._document_revision_feedback(request, current))
        calls = app.writing.client_factory.return_value.ask_json.call_count
        revised = app.writing.call('compose_document', correction, identity='whole-unit-editorial-fixed')
        self.assertEqual(revised.status, 'ok', revised)
        labels = [call.kwargs.get('label') for call in
            app.writing.client_factory.return_value.ask_json.call_args_list[calls:]]
        self.assertEqual(labels, ['composition-article', 'article-independent-review'])
        fixed = ArtifactStore(app.session.store.root / 'tools/writing/whole-unit-editorial-fixed')
        feedback = fixed.read_json('request.json')['revision_feedback']
        self.assertEqual(feedback['section_reviews'][0]['findings'][0]['finding_id'], 'editorial')
        self.assertIn('revision_feedback', json.dumps(fixed.read_json('writer/report_inputs.json')))
        calls = app.writing.client_factory.return_value.ask_json.call_count
        replay = app.writing.call('compose_document', correction, identity='whole-unit-editorial-fixed')
        self.assertEqual(replay.model_dump(mode='json'), revised.model_dump(mode='json'))
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        # Both calls normalize to figures=[] internally. Only the exact caller
        # actually supplied that field, so it retains explicit-copy semantics.
        expect_units = False
        exact = app.writing.call('compose_document', {**arguments, 'figures': []}, identity='whole-unit-exact')
        self.assertEqual(exact.status, 'ok', exact)
        self.assertNotIn('producer_units', exact.data['document_work'])
        self.assertNotIn('publication_title_contract', exact.data['document_work'])
        self.assertEqual(len(exact.data['document_work']['materials']), 2)
        exact_export = app.writing.continuation(exact.model_dump(mode='json'), 'whole-unit-exact')
        self.assertEqual(exact_export['arguments']['title'], 'Provisional selection title')
        exact_store = ArtifactStore(app.session.store.root / 'tools/writing/whole-unit-exact')
        self.assertNotIn('producer_unit_contract', exact_store.read_json('selection-input.json'))
        self.assertNotIn('delivery_subject_contract', exact_store.read_json('selection-input.json'))
        self.assertNotIn('delivery_subject_contract', exact_store.read_json('request.json'))
        legacy_subjects = {row['path'] for row in exact_store.read_json('request.json')['delivery_review']['artifacts']}
        self.assertFalse({str(path) for path in predecessors}.intersection(legacy_subjects),
            'Explicit-copy writing cannot turn new producer authoring context into physical input edges')
        self.assertTrue({str(context_script), str(context_csv)} <= legacy_subjects,
            'Legacy selection still promotes generated materials and their consumed scripts')
        exact_binding = exact_store.resolve('request.json').read_bytes()
        calls = app.writing.client_factory.return_value.ask_json.call_count
        with patch.object(app.writing, '_joint_delivery_paths', side_effect=AssertionError(
                'Recovery must replay saved subjects without applying new declaration filtering')):
            exact_replay = app.writing.call('compose_document', arguments, identity='whole-unit-exact')
        self.assertEqual(exact_replay.model_dump(mode='json'), exact.model_dump(mode='json'))
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual(exact_store.resolve('request.json').read_bytes(), exact_binding)
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertEqual({path: path.read_bytes() for path in predecessor_bytes}, predecessor_bytes)
        self.assertEqual({path: path.read_bytes() for path in context_bytes}, context_bytes)
        self.assertEqual(context_receipt.read_bytes(), context_receipt_bytes)
        # Material-only ownership does not weaken the evidence/provenance gate.
        # A script change must invalidate delivery even with unchanged CSV bytes.
        calls = app.writing.client_factory.return_value.ask_json.call_count
        factories = app.writing.client_factory.call_count
        for path, raw in context_bytes.items():
            with self.subTest(changed_context=path.name):
                path.write_bytes(raw + b'# changed\n')
                try:
                    if path == context_script:
                        self.assertEqual(context_csv.read_bytes(), context_bytes[context_csv])
                    with self.assertRaises(ValueError):
                        app.writing._joint_delivery_paths(
                            [{'path': str(bundle / 'table.csv')}, {'path': str(context_csv)}],
                            [str(bundle / 'run.py'), str(bundle / 'table.csv')],
                            producer_units=units, delivery_copies=set(), declared_subjects_only=True)
                    with self.assertRaises(ValueError):
                        app.writing.check_delivery(result.data)
                    self.assertIsNone(app.writing.continuation(result.model_dump(mode='json'), 'whole-unit'))
                finally:
                    path.write_bytes(raw)
        self.assertIsNotNone(app.writing.continuation(result.model_dump(mode='json'), 'whole-unit'))
        self.assertEqual(app.writing.client_factory.return_value.ask_json.call_count, calls)
        self.assertEqual(app.writing.client_factory.call_count, factories)
        # Context is not a runtime edge, but its original byte guard still binds
        # the completed owner result and all current writing/publication use.
        for path in predecessors:
            with self.subTest(changed_authoring_context=path.name):
                path.write_bytes(predecessor_bytes[path] + b'# changed\n')
                try:
                    with self.assertRaises(ValueError):
                        professional_units(app.writing, [], [], retained=units)
                    self.assertIsNone(app.writing.continuation(result.model_dump(mode='json'), 'whole-unit'))
                finally:
                    path.write_bytes(predecessor_bytes[path])
        self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)
        altered = bundle / 'records.csv'
        altered.write_bytes(original[altered] + b'changed\n')
        with self.assertRaises(ValueError):
            professional_units(app.writing, [], [], retained=units)
        self.assertIsNone(app.writing.continuation(result.model_dump(mode='json'), 'whole-unit'))

    def test_whole_unit_text_admission_is_total_not_file_layout_or_equal_hash(self):
        from simple_ar.tools.materials import read_bound_material_bytes
        # Isolate admission with an already-validated unit; the preceding
        # integration test exercises the real registration/provenance proof.
        _, workspace, writing = self._real_workspace()
        directory = workspace.resolve('workspace/unit', write=True)
        directory.mkdir()
        whole, first, second, external = [directory / name
            for name in ('whole.py', 'first.py', 'second.py', 'external.txt')]
        whole.write_bytes(b'#' * 256000)
        first.write_bytes(b'#' * 128000)
        second.write_bytes(first.read_bytes())
        external.write_bytes(b'external')
        with patch('simple_ar.tools.producer_declarations.professional_units') as validate, \
                patch('simple_ar.tools.producer_declarations.professional_unit_input_owners', return_value={}):
            def freeze(paths, *, imported=(), text_contract=None):
                unit = {'files': workspace.artifact_hashes(map(str, paths))}
                validate.return_value = [unit]
                contract = {} if text_contract is None else {'text_contract': text_contract}
                return writing._freeze_delivery_review([*map(str, paths), *map(str, imported)],
                    'text-boundary', copies=[], producer_units=[unit], **contract)

            for paths, text_contract in ((paths, contract)
                    for paths in ([whole], [first, second])
                    for contract in (None, 'professional_unit_text.v1')):
                with self.subTest(paths=paths, text_contract=text_contract):
                    result = freeze(paths, text_contract=text_contract)
                    self.assertEqual(sum(len(row['text'].encode('utf-8')) for row in result['artifacts']), 256000)
                    self.assertEqual(result['text_admission']['contract'],
                        text_contract or 'professional_unit_text.v2')
                    self.assertEqual(result['text_admission']['max_text_bytes'], 256000)
                    for subject in result['artifacts']:
                        self.assertEqual(read_bound_material_bytes(workspace, subject['path'],
                            subject['sha256'], max_bytes=256000), subject['text'].encode('utf-8'))
                        with self.assertRaises(ValueError):
                            read_bound_material_bytes(workspace, subject['path'],
                                subject['sha256'], max_bytes=32000)
                        with self.assertRaisesRegex(ValueError, 'changed during complete reading'):
                            read_bound_material_bytes(workspace, subject['path'], '0' * 64, max_bytes=256000)
                    with self.assertRaisesRegex(ValueError, '256000 total text bytes'):
                        freeze(paths, imported=[external], text_contract=text_contract)
            for paths, changed in (([whole], whole), ([first, second], second)):
                with self.subTest(over_limit=paths):
                    original = changed.read_bytes()
                    changed.write_bytes(original + b'#')
                    with self.assertRaisesRegex(ValueError, '256000 total text bytes'):
                        freeze(paths)
                    changed.write_bytes(original)
            # A multi-byte character crossing the old 64k window is decoded
            # only once; invalid UTF-8 remains explicitly uninspected.
            whole.write_bytes(('字' * 30000).encode('utf-8'))
            self.assertEqual(freeze([whole])['artifacts'][0]['text'], '字' * 30000)
            whole.write_bytes(b'invalid\xff')
            row, = freeze([whole])['artifacts']
            self.assertEqual(row['coverage'], 'incomplete')
            self.assertNotIn('text', row)
            # New whole units admit safe UTF-8 by content, including arbitrary
            # extensionless names and unfamiliar suffixes. Saved v1 markers
            # retain their historical suffix admission during recovery.
            for name in ('inventory', 'record.meta', 'current.txt'):
                path = directory / name
                with self.subTest(byte_limit_name=name):
                    path.write_bytes(b'x' * 256000)
                    row, = freeze([path])['artifacts']
                    self.assertEqual(row['coverage'], 'complete')
                    self.assertEqual(row['text'], 'x' * 256000)
                    with self.assertRaisesRegex(ValueError, '256000 total text bytes'):
                        freeze([path], imported=[external])
                    path.write_bytes(b'x' * 256001)
                    with self.assertRaisesRegex(ValueError, '256000 total text bytes'):
                        freeze([path])
                raw = 'Inventory: 字\n\tcomplete\r\n'.encode('utf-8')
                path.write_bytes(raw)
                for text_contract in (None, 'professional_unit_text.v2', 'professional_unit_text.v1'):
                    with self.subTest(name=name, text_contract=text_contract):
                        result = freeze([path], text_contract=text_contract)
                        self.assertEqual(result['text_admission']['contract'],
                            text_contract or 'professional_unit_text.v2')
                        row, = result['artifacts']
                        supported = text_contract != 'professional_unit_text.v1' or name == 'current.txt'
                        self.assertEqual(row['coverage'], 'complete' if supported else 'unsupported')
                        if supported:
                            self.assertEqual(row['text'], raw.decode('utf-8'))
                        else:
                            self.assertNotIn('text', row)
                legacy = writing._freeze_delivery_review([str(path)], 'legacy-suffix')
                self.assertNotIn('text_admission', legacy)
                row, = legacy['artifacts']
                self.assertEqual(row['coverage'], 'complete' if name == 'current.txt' else 'unsupported')
                if name == 'current.txt':
                    self.assertEqual(row['text'], raw.decode('utf-8'))
                else:
                    self.assertNotIn('text', row)
                for raw in (b'binary\x00payload', b'control\x01payload', b'control\x1fpayload', b'invalid\xff'):
                    with self.subTest(name=name, rejected=raw):
                        path.write_bytes(raw)
                        row, = freeze([path])['artifacts']
                        self.assertEqual(row['coverage'], 'incomplete')
                        self.assertNotIn('text', row)
            # Merely accompanying a unit does not upgrade imported material.
            whole.write_bytes(b'# valid')
            external.write_bytes(b'x' * 32001)
            self.assertEqual(freeze([whole], imported=[external])['artifacts'][1]['coverage'], 'oversized')
            # The same new-mode count bound applies during subject selection
            # and snapshot freezing; the request's caller bound is unchanged.
            members = [directory / f'member-{index}.txt' for index in range(201)]
            for path in members:
                path.write_bytes(b'x')
            for count in (200, 201):
                unit = {'files': workspace.artifact_hashes(map(str, members[:count]))}
                if count == 200:
                    paths, _ = writing._joint_delivery_paths([], [], producer_units=[unit], delivery_copies=set())
                    self.assertEqual(set(paths), set(unit['files']))
                    self.assertEqual(len(freeze(members[:count])['artifacts']), count)
                else:
                    with self.assertRaisesRegex(ValueError, '200 files'):
                        writing._joint_delivery_paths([], [], producer_units=[unit], delivery_copies=set())
                    with self.assertRaisesRegex(ValueError, '200 original subjects'):
                        freeze(members[:count])
        # No marker means the old aggregate admission, even for short files.
        short = []
        for index in range(4):
            path = directory / f'short-{index}.txt'
            path.write_bytes(b'x' * 32000)
            short.append(str(path))
        legacy = writing._freeze_delivery_review(short, 'legacy-total')
        self.assertNotIn('text_admission', legacy)
        self.assertEqual([row['coverage'] for row in legacy['artifacts']],
            ['complete', 'complete', 'complete', 'oversized'])

    def test_publication_title_uses_canonical_heading_only_for_new_ordinary_work(self):
        from simple_ar.report.review_evidence import CLAIM_ADOPTION_V2
        for policy, explicit in ((None, False), (CLAIM_ADOPTION_V2, False), (CLAIM_ADOPTION_V2, True)):
            with self.subTest(policy=policy, explicit=explicit):
                _, _, writing, source, _, _, _, _ = self._document_work_fixture()
                writing.declaration_adoption_contract = policy
                arguments = {'goal': 'Explain the retained record', 'material_roots': [str(source)],
                    'publication': {'compile_pdf': False}, 'finish': True}
                if explicit:
                    arguments['title'] = 'Explicit caller title'
                result = writing.call('compose_document', arguments, identity='title-policy')
                self.assertEqual(result.status, 'ok', result)
                export = writing.continuation(result.model_dump(mode='json'), 'title-policy')
                self.assertEqual(export['arguments']['title'], 'Explicit caller title' if explicit else
                    '' if policy else 'Recorded value and scope')

    def test_document_work_feedback_reuses_proven_recipe_and_preserves_original_version(self):
        root, workspace, writing, source, _, _, client, prompts = self._document_work_fixture()
        initial = writing.call('compose_document', {'goal': 'Explain the record',
            'material_roots': [str(source)], 'title': 'Explicit source title'}, identity='document-first')
        self.assertEqual(initial.status, 'ok', initial)
        original = {path: workspace.resolve(path).read_bytes() for path in initial.artifacts}
        calls = len(prompts)
        revised = writing.call('compose_document', {'goal': 'Clarify the scope without new measurements',
            'prior_draft': initial.data['editable_markdown'], 'publication': {'compile_pdf': False},
            'finish': True}, identity='document-feedback')
        self.assertEqual(revised.status, 'ok', revised)
        self.assertEqual([label for label, _ in prompts[calls:]], ['composition-article', 'article-independent-review'])
        binding = ArtifactStore(root / 'tools/writing/document-feedback').read_json('request.json')
        self.assertEqual(binding['request']['title'], 'Explicit source title')
        self.assertTrue(binding['request']['reuse_prior_inputs'])
        self.assertEqual(binding['prior_input_reuse']['owner'], 'document-first')
        self.assertTrue(all(row['path'].startswith('workspace/manuscripts/document-feedback/materials/')
            for row in binding['document_work']['materials']))
        self.assertEqual({path: workspace.resolve(path).read_bytes() for path in original}, original)
        self.assertIsNotNone(writing.continuation(revised.model_dump(mode='json'), 'document-feedback'))
        # Replay the sparse 0044-style recipe through either public locator.
        # All producer/evidence proofs read the real saved fixture; no Writer
        # or selection call is permitted during resolution.
        self._record_native_result(root, 'document-feedback', revised)
        writing.document_source_contract = 'document_source.v1'
        parent = ArtifactStore(root / 'tools/writing/document-feedback')
        self.assertNotIn('material_roots', parent.read_json('request.json')['request'])
        frozen = {path: path.read_bytes() for path in (root / 'tools/writing').rglob('*') if path.is_file()}
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model during source replay')):
            resolutions = []
            admissions = {}
            for key, value in (('prior_result', 'records/document-feedback/native_result.json'),
                    ('prior_draft', revised.data['editable_markdown'])):
                intent = {'goal': 'Clarify this same evidence again', key: value}
                admission = writing._admit_document_source(ArtifactStore(root / 'tools/writing' / key),
                    intent, None, None, None)
                admissions[key] = admission
                resolved = writing._resolve_document_inputs(ComposeRequest.model_validate(intent), key,
                    report=None, original=None, selection=None, document_work=True,
                    caller_fields=frozenset(intent), work_source=admission, normalize_arguments=None)
                self.assertEqual(resolved.recipe.prior_result, '')
                self.assertEqual(resolved.recipe.prior_draft, revised.data['editable_markdown'])
                self.assertEqual(writing._document_access_roots(parent, resolved.binding), [str(source)])
                resolutions.append(resolved)
            self.assertEqual(resolutions[0].recipe, resolutions[1].recipe)
            extra = root / 'new-material'
            extra.mkdir()
            workspace.mounts['new_material'] = extra
            intent = {'goal': 'Investigate this changed scope',
                'prior_draft': revised.data['editable_markdown'], 'material_roots': [str(extra)]}
            admission = writing._admit_document_source(ArtifactStore(root / 'tools/writing/new-scope'),
                intent, None, None, None)
            changed = writing._resolve_document_inputs(ComposeRequest.model_validate(intent), 'new-scope',
                report=None, original=None, selection=None, document_work=True,
                caller_fields=frozenset(intent), work_source=admission, normalize_arguments=None)
            self.assertIsNone(changed.binding)
            self.assertFalse(changed.recipe.reuse_prior_inputs)
            self.assertEqual(changed.recipe.material_roots, [str(extra)])
            self.assertEqual(changed.recipe.materials, [])
            self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)

    def test_equivalent_focus_reuses_evidence_for_reorganization_without_changing_old_resolution(self):
        root, workspace, writing, source, _, table, client, _ = self._document_work_fixture()
        extra_file = source / 'code' / 'additional-context.txt'
        extra_file.write_text('Unselected context that may require new investigation.', encoding='utf-8')
        writing.document_source_contract = 'document_source.v1'
        goal = 'Explain the saved value and scope'
        first = writing.call('compose_document', {'goal': goal,
            'material_roots': [str(source / 'code'), str(source / 'results')]}, identity='focused-first')
        self.assertEqual(first.status, 'ok', first)
        self._record_native_result(root, 'focused-first', first)
        parent = ArtifactStore(root / 'tools/writing/focused-first')
        frozen = {p: p.read_bytes() for p in parent.root.rglob('*') if p.is_file()}
        ledger = writing.ledger.to_dict()
        extra = root / 'extra'
        extra.mkdir()
        workspace.mounts['extra'] = extra
        cases = [([str(source)], False, True), ([str(source)], True, False),
            ([str(source), str(extra)], False, False), ([str(source / 'code')], False, False),
            ([str(source), str(extra_file)], False, False)]
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model during recipe resolution')):
            for index, (roots, historical, reuse) in enumerate(cases):
                with self.subTest(roots=roots, historical=historical):
                    identity = f'reorganized-{index}'
                    args = {'goal': goal, 'prior_draft': first.data['editable_markdown'],
                        'material_roots': roots, 'required_headings': ['Evidence', 'Limits']}
                    source_binding = writing._admit_document_source(ArtifactStore(root / 'tools/writing' / identity),
                        args, None, None, None)
                    if historical:
                        source_binding.pop('resolution_contract')
                    resolved = writing._resolve_document_inputs(ComposeRequest.model_validate(args), identity,
                        report=None, original=None, selection=None, document_work=True,
                        caller_fields=frozenset(args), work_source=source_binding, normalize_arguments=None)
                    self.assertEqual(resolved.recipe.reuse_prior_inputs, reuse)
                    self.assertEqual(resolved.recipe.required_headings, ['Evidence', 'Limits'])
                    if reuse:
                        self.assertEqual(resolved.linkage['owner'], 'focused-first')
                        self.assertEqual(resolved.recipe.materials[0].path, str(table))
                        self.assertIsNone(resolved.candidate)
                    else:
                        self.assertIsNone(resolved.binding)
                        self.assertIsNotNone(resolved.investigation)
            self.assertEqual({p: p.read_bytes() for p in frozen}, frozen)
            self.assertEqual(writing.ledger.to_dict(), ledger)

    def test_sparse_document_recipe_recovers_scope_only_from_hash_bound_parent(self):
        root, _, writing, source, _, _, client, _ = self._document_work_fixture()
        original = ArtifactStore(root / 'tools/writing/native-tool-0039')
        original.write_json('request.json', {'request': {'material_roots': [str(source)]}})
        sparse = ArtifactStore(root / 'tools/writing/native-tool-0044')
        binding = {'request': {'reuse_prior_inputs': True},
            'revision_feedback': {'contract': 'prior_prose_review.v1'},
            'prior_input_reuse': {'owner': 'native-tool-0039', 'receipt_sha256': {
                'request.json': hashlib.sha256(original.resolve('request.json').read_bytes()).hexdigest()}}}
        sparse.write_json('request.json', binding)
        before = {path: path.read_bytes() for path in (original.resolve('request.json'), sparse.resolve('request.json'))}
        with patch.object(client, 'ask_json', side_effect=AssertionError('Source proof must not call a model')):
            self.assertEqual(writing._document_access_roots(sparse, binding), [str(source)])
            self.assertIsNone(writing._document_access_roots(sparse, {'request': {}}))
            self.assertEqual({path: path.read_bytes() for path in before}, before)
            original.write_json('request.json', {'request': {'material_roots': [str(root)]}})
            with self.assertRaisesRegex(ValueError, 'scope.*changed'):
                writing._document_access_roots(sparse, binding)
        writing.client_factory.assert_not_called()

    def test_noncode_source_gap_investigates_with_bound_feedback_and_work_context(self):
        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        writing.work_context = Mock(return_value={'user_requests': ['Explain the interview record and its consent scope.']})
        original_answer = client.ask_json.side_effect
        supplement = False
        policy = source / 'consent.txt'
        policy.write_text('Consent permits reuse of anonymized interview excerpts for this report.', encoding='utf-8')

        def answer(system, prompt, **options):
            view = json.loads(prompt)
            if options['label'].startswith('composition-selection'):
                prompts.append((options['label'], view))
                self.assertIn('work_context', view)
                if supplement:
                    self.assertEqual(view['source_feedback']['review']['review_status'], 'needs_revision')
                    self.assertIn('delivery_producers', view['source_feedback'])
                paths = [table, policy] if supplement else [table]
                observed = {row['request']['path'] for row in view['observations']}
                missing = [path for path in paths if str(path) not in observed]
                if missing:
                    return {'read_windows': [{'path': str(path)} for path in missing]}
                return {'title': 'Interview record and consent scope', 'materials': [str(path) for path in paths],
                    'delivery_artifacts': []}
            if options['label'] == 'article-independent-review' and not supplement:
                prompts.append((options['label'], view))
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'consent', 'type': 'evidence_gap', 'severity': 'major',
                        'required_action': 'verify', 'message': 'Find the supplied consent policy before asserting reuse permission.',
                        'draft_quotes': [{'section_id': 'document', 'quote': view['sections'][0]['markdown']}],
                        'evidence_quotes': [{'pointer': '/objective'}]}]}]}
            if options['label'] == 'composition-article' and supplement:
                self.assertEqual(view['source_feedback']['review']['review_status'], 'needs_revision')
                self.assertIn('delivery_producers', view['source_feedback'])
                self.assertTrue(view['prior_draft'])
                prompts.append((options['label'], view))
                return {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                    'replacements': [{'old_text': view['prior_draft'], 'new_text':
                        '## Results\n\nThe record value is 2.5. Consent permits anonymized excerpt reuse for this report.'}]}
            return original_answer(system, prompt, **options)

        client.ask_json.side_effect = answer
        prior = writing.call('compose_document', {'goal': 'Explain the interview record',
            'material_roots': [str(source)], 'professional_review': {'max_revision_rounds': 0}}, identity='source-negative')
        self.assertEqual(prior.status, 'ok', prior)
        self.assertEqual(writing.final_review(prior.data)['review_status'], 'needs_revision')
        self._record_native_result(root, 'source-negative', prior)
        frozen = {path: path.read_bytes() for path in (root / 'tools/writing/source-negative').rglob('*') if path.is_file()}
        intent = {'goal': 'Complete the consent evidence within the same supplied materials',
            'prior_result': 'records/source-negative/native_result.json'}
        with patch.object(client, 'ask_json', side_effect=AssertionError('Source routing must be model free')):
            admission = writing._admit_document_source(ArtifactStore(root / 'tools/writing/source-routing'),
                intent, None, None, None)
            resolved = writing._resolve_document_inputs(ComposeRequest.model_validate(intent), 'source-routing',
                report=None, original=None, selection=None, document_work=True,
                caller_fields=frozenset(intent), work_source=admission, normalize_arguments=None)
        self.assertFalse(resolved.recipe.reuse_prior_inputs)
        self.assertEqual(resolved.recipe.prior_draft, prior.data['editable_markdown'])
        self.assertEqual(resolved.recipe.material_roots, [str(source)])
        self.assertIsNotNone(resolved.investigation)
        supplement = True
        start = len(prompts)
        result = writing.call('compose_document', intent, identity='source-supplemented')
        self.assertEqual(result.status, 'ok', result)
        self.assertTrue(any(label.startswith('composition-selection') for label, _ in prompts[start:]))
        self.assertEqual(writing.final_review(result.data)['review_status'], 'reviewed')
        self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)
        owner = ArtifactStore(root / 'tools/writing/source-supplemented')
        self.assertEqual(owner.read_json('request.json')['work_source']['diagnosis'], resolved.investigation)
        self.assertEqual(owner.read_json('selection-input.json')['source_feedback_projection'], 'shared_producers.v1')
        self.assertEqual(owner.read_json('request.json')['source_feedback_projection'], 'shared_producers.v1')
        self.assertIn(str(policy), {row['path'] for row in owner.read_json('request.json')['request']['materials']})
        calls = client.ask_json.call_count
        with patch.object(client, 'ask_json', side_effect=AssertionError('No replayed model work')):
            replay = writing.call('compose_document', intent, identity='source-supplemented')
        self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)

    def test_attachment_gap_reaches_actual_producer_consumer_without_rewriting(self):
        from types import SimpleNamespace
        from tests.test_professional_work_loop import ProfessionalWorkLoopTests
        fixture = ProfessionalWorkLoopTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        proposal = fixture._proposal("from pathlib import Path\n"
            "Path('result.csv').write_text('id,value\\n' + ''.join(f'{i},20\\n' for i in range(50000)))\n")
        proposal['execution_plan']['outputs'] = ['result.csv']
        fixture.responses = [proposal]
        app = fixture._create()
        fixture._run(app, actions=[fixture._prepare()])
        _, token = fixture._pending_execution(app)
        fixture._run(app, approved_execution=token)
        prepared = fixture._observations(app)[0]
        candidate = Path(prepared['data']['project_dir'])
        output = candidate / 'result.csv'

        def answer(system, prompt, **options):
            view = json.loads(prompt)
            if options['label'].startswith('composition-selection'):
                if not view['observations']:
                    return {'read_windows': [{'path': str(output), 'max_bytes': 1000}]}
                return {'title': 'Recorded output check', 'materials': [{'path': str(output), 'max_bytes': 1000}],
                    'delivery_artifacts': [str(output)]}
            if options['label'] == 'composition-article':
                return {'draft_markdown': '## Results\n\nThe supplied calculation saved 20.'}
            self.assertEqual(options['label'], 'article-independent-review')
            subjects = view['delivery_artifacts']
            index = next(i for i, row in enumerate(subjects) if row['path'] == str(output))
            return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                'findings': [{'finding_id': 'wrong-total', 'type': 'consistency', 'severity': 'major',
                    'required_action': 'revise', 'message': 'sum(range(7)) is 21; the producer saved 20.',
                    'delivery_targets': [f'/delivery_artifacts/{index}/profile'],
                    'evidence_quotes': [{'pointer': f'/delivery_artifacts/{index}/profile/example_rows/0/value'}]}]}]}

        client = SimpleNamespace(ask_json=Mock(side_effect=answer))
        app.writing.client_factory = Mock(return_value=client)
        prior = app.writing.call('compose_document', {'goal': 'Check the saved sum',
            'material_roots': [str(candidate)]}, identity='producer-negative')
        self.assertEqual(prior.status, 'ok', prior)
        self.assertTrue(prior.data['delivery_review']['defects'])
        self._record_native_result(fixture.root, 'producer-negative', prior)
        ledger = app.ledger.to_dict()
        processes = fixture._process_receipts()
        app.writing.document_source_contract = 'document_source.v1'
        with patch.object(client, 'ask_json', side_effect=AssertionError('Do not rewrite a producer defect')), \
                patch('simple_ar.tools.writing.WritingToolGateway.__init__',
                    side_effect=AssertionError('Producer proof must not instantiate a second Writer')):
            gap = app.writing.call('compose_document', {'goal': 'Resolve the failed output check',
                'prior_result': 'records/producer-negative/native_result.json'}, identity='producer-gap')
            self.assertEqual(gap.status, 'error', gap)
            self.assertEqual(gap.data['owner_stop_reason'], 'producer_repair_required')
            self._record_native_result(fixture.root, 'producer-gap', gap)
            action = app.implementation.consume_outcome(gap.model_dump(mode='json'),
                'attempts/producer-gap/native_result.json')
        self.assertEqual(action['tool_name'], 'prepare_implementation', action)
        self.assertEqual(action['arguments']['repair_review'], 'records/producer-negative/native_result.json')
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(fixture._process_receipts(), processes)
        self.assertFalse((fixture.root / 'tools/writing/producer-gap/request.json').exists())
        origin = fixture.root / 'tools/writing/producer-negative/result.json'
        original = origin.read_bytes()
        origin.write_bytes(original + b'\n')
        self.assertIsNone(app.implementation.consume_outcome(gap.model_dump(mode='json'),
            'attempts/producer-gap/native_result.json'))

    def test_admitted_source_failure_retains_owner_before_resolved_request_exists(self):
        root, _, writing, _, _, _, client, _ = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        arguments = {'goal': 'Continue the saved document', 'prior_result': 'records/missing/native_result.json'}
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model before source proof')):
            result = writing.call('compose_document', arguments, identity='source-failed')
        self.assertEqual(result.status, 'error')
        self.assertEqual(result.data['owner_stop_reason'], 'owner_state_unresolved')
        owner = ArtifactStore(root / 'tools/writing/source-failed')
        self.assertFalse(owner.exists('request.json'))
        self.assertEqual(owner.read_json('selection-input.json')['work_source']['intent']['prior_result'],
            arguments['prior_result'])
        native = self._record_native_result(root, 'source-failed', result)
        stop = writing.stop_after_review(result.model_dump(mode='json'), 'source-failed')['stop']
        self.assertEqual(stop['reason'], 'owner_state_unresolved')
        self.assertEqual(stop['source_sha256'], hashlib.sha256(native.resolve('native_result.json').read_bytes()).hexdigest())
        self.assertNotIn('new correction', result.data['next_action'])
        writing.client_factory.assert_not_called()

    def test_document_work_continues_failed_article_author_without_selection_or_redrafting(self):
        for stage in ('initial', 'revision', 'revision_checkpoint_lag', 'revision_negative'):
            with self.subTest(stage=stage):
                self._article_continuation_case(stage)

    def _article_continuation_case(self, stage):
        from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
        from simple_ar.report.review_evidence import COMPARISON_VERIFICATION_CONTRACT
        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        templates = ArtifactStore(root / 'article-templates')
        templates.write_text('article.md', '# Recorded results\n\n## Results\nExplain the record.\n\n## Scope\nState limits.\n')
        templates.write_text('criteria.md', 'Use the provided record and disclose execution limits.\n')
        requirement = freeze_report_requirement({'template': str(templates.resolve('article.md')),
            'criteria': str(templates.resolve('criteria.md')), 'outline_strategy': 'template',
            'draft_scope': 'document', 'review_scope': 'document',
            'reviewer': 'llm', 'document_review': True, 'max_review_iterations': 0 if stage == 'initial' else 1,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='article')
        goal = 'Explain the saved record without new execution'
        report = material_report_binding(workspace, requirement, mounts=[str(table)], requests=[goal])
        writing.report_inputs = lambda: report
        writing.professional_inputs = True
        original_answer = client.ask_json.side_effect
        accepting = False
        initial_body = '## Results\n\nThe supplied record says value=2.5.\n\n## Scope\n\nNo new execution was requested.'
        body = (initial_body.replace('The supplied record says value=2.5.', 'The saved value is 2.5.')
            if stage != 'initial' else initial_body)
        def answer(system, prompt, **options):
            view = json.loads(prompt)
            if options['label'].startswith('composition-selection-'):
                return original_answer(system, prompt, **options)
            prompts.append((options['label'], view))
            if options['label'] == 'composition-article':
                self.assertFalse(accepting, 'A complete retained author must not be drafted again')
                if view.get('edit_contract'):
                    return {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': initial_body, 'new_text': body}]}
                return {'draft_markdown': initial_body}
            self.assertTrue(options['label'].startswith('article-independent-review'))
            self.assertEqual(view['comparison_verification_contract'], COMPARISON_VERIFICATION_CONTRACT)
            if stage != 'initial' and not accepting and view['sections'][0]['markdown'] == initial_body:
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'style', 'type': 'style', 'severity': 'minor',
                        'required_action': 'revise', 'section_id': 'document', 'message': 'Clarify the sentence.',
                        'draft_quotes': [{'section_id': 'document', 'quote': 'The supplied record says value=2.5.'}]}]}]}
            self.assertEqual(view['sections'][0]['markdown'], body)
            if accepting and stage == 'revision_negative':
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'remaining-style', 'type': 'style', 'severity': 'minor',
                        'required_action': 'revise', 'section_id': 'document',
                        'message': 'Clarify the remaining sentence.',
                        'draft_quotes': [{'section_id': 'document', 'quote': 'The saved value is 2.5.'}]}]}]}
            return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass', 'findings': []}]
                if accepting else 'Malformed even after one correction'}
        client.ask_json.side_effect = answer
        initial_arguments = {'material_roots': [str(source)], 'finish': True,
            'publication': {'compile_pdf': False}}
        failed = writing.call('compose_document', initial_arguments, identity='article-stopped')
        if stage == 'initial':
            self.assertEqual(failed.status, 'error', failed)
            self.assertTrue(failed.data['incomplete'])
            self.assertEqual(failed.data['retained_candidate']['status'], 'unreviewed')
            readable = workspace.resolve(failed.data['retained_candidate']['markdown']).read_text()
            self.assertIn('The supplied record says value=2.5.', readable)
            self.assertIn('No new execution was requested.', readable)
        else:
            self.assertEqual(failed.status, 'ok', failed)
            self.assertTrue(failed.data['unresolved'])
            self.assertTrue(any(row.get('type') == 'document_revision_unavailable'
                for row in failed.data['unresolved']), 'Retain the actual interrupted verification')
            self.assertNotIn('pending_candidate', failed.data,
                'A malformed verifier cannot become a checked negative candidate')
        old = ArtifactStore(root / 'tools/writing/article-stopped')
        if stage == 'revision_checkpoint_lag':
            # The atomic author response/projection committed, but the editor
            # checkpoint did not. Its earlier started event is not authoritative
            # about whether new prose exists and cannot select the old body.
            checkpoint = old.read_json('writer/review/checkpoint.json')
            event = next(row for row in checkpoint['iterations'] if row['action'] == 'document_joint_revise')
            event.update(drafts=[], status='started', adopted=False)
            checkpoint['iterations'] = [event]
            old.resolve('writer/review/checkpoint.json').write_text(json.dumps(checkpoint), encoding='utf-8')
        native = self._record_native_result(root, 'article-stopped', failed)
        observed = failed.model_dump(mode='json')
        before_proof = {p: p.read_bytes() for folder in (old.root, native.root, source)
            for p in folder.rglob('*') if p.is_file()}
        before_calls = client.ask_json.call_count
        before_factories = writing.client_factory.call_count
        writing.document_source_contract = 'document_source.v1'
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model during candidate resolution')):
            candidates = []
            for key, value in (('prior_result', 'records/article-stopped/native_result.json'),
                    ('prior_draft', 'workspace/manuscripts/article-stopped/manuscript.md')):
                intent = {key: value}
                admission = writing._admit_document_source(ArtifactStore(root / 'tools/writing' / key),
                    intent, None, None, writing.report_inputs())
                request = ComposeRequest.model_validate({**intent, 'goal': old.read_json('request.json')['request']['goal']})
                resolved = writing._resolve_document_inputs(request, key, report=writing.report_inputs(),
                    original=None, selection=None, document_work=True, caller_fields=frozenset(intent),
                    work_source=admission, normalize_arguments=None)
                self.assertEqual(resolved.linkage['candidate_contract'], 'retained_article_author.v1')
                candidates.append(resolved.candidate)
            self.assertEqual(candidates[0], candidates[1])
        writing.document_source_contract = None
        # All native registration, owner, author and input proofs consume the
        # saved fixtures; no provider is needed for this eligibility replay.
        with self.subTest(proof='registered native article'):
            self.assertTrue(writing.can_resume_after_budget(observed, 'article-stopped'))
            self.assertIsNone(writing.resume_after_budget({**observed, 'summary': 'changed'}, 'article-stopped'))
            if stage != 'initial':
                limits = dict(writing.ledger.limits)
                for resource in ('llm_requests', 'total_tokens'):
                    writing.ledger.limits[resource] = 0
                    before = writing.ledger.to_dict()
                    with patch('simple_ar.tools.writing.stop_decision',
                            side_effect=lambda owner, observation, identity, reason, detail: {'reason': reason}):
                        self.assertEqual(writing.stop_after_review(observed, 'article-stopped'),
                            {'reason': 'shared_budget_exhausted'})
                    self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
                    self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
                    self.assertEqual(writing.ledger.to_dict(), before)
                    writing.ledger.limits = dict(limits)
                # Explicit resource authorization is the application's boundary.
                # This owner merely reads its resulting current capacity.
                writing.ledger.limits.update(llm_requests=100, total_tokens=1_000_000)
                for status in ('reserved', 'unknown'):
                    from types import SimpleNamespace
                    with patch.object(type(writing.ledger), 'entries', new_callable=PropertyMock,
                            return_value=(*writing.ledger.entries,
                                SimpleNamespace(status=status, attempt_id='another-owner'))):
                        self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
                        self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
                action = writing.resume_after_budget(observed, 'article-stopped')
                self.assertEqual(action, {'tool_name': 'compose_document', 'arguments': {
                    'prior_result': 'records/article-stopped/native_result.json',
                    'goal': old.read_json('request.json')['request']['goal'], 'finish': True,
                    'publication': old.read_json('request.json')['request']['publication']}})
                from simple_ar.report.writing import retained_article_candidate
                candidate = Path(retained_article_candidate(old, old.read_json('request.json'))['path'])
                raw = candidate.read_bytes()
                candidate.unlink()
                self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
                self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
                candidate.write_bytes(raw)
                # A complete negative response beats a stale interrupted
                # checkpoint and never authorizes another same-scope check.
                response_path = candidate.parent / 'inspection/response.json'
                raw = response_path.read_bytes()
                response = json.loads(raw)
                response['response'] = {'section_reviews': [{'section_id': 'document',
                    'verdict': 'revise_required', 'findings': [{'finding_id': 'checked-style',
                        'type': 'style', 'severity': 'minor', 'required_action': 'revise',
                        'section_id': 'document', 'message': 'A completed negative check.',
                        'draft_quotes': [{'section_id': 'document', 'quote': 'The saved value is 2.5.'}]}]}]}
                response['response_sha256'] = hashlib.sha256(json.dumps(response['response'],
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                response_path.write_text(json.dumps(response), encoding='utf-8')
                self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
                self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
                response_path.write_bytes(raw)
                before = table.read_bytes()
                table.write_bytes(b'changed original evidence')
                self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
                self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
                table.write_bytes(before)
        self.assertEqual(client.ask_json.call_count, before_calls)
        self.assertEqual(writing.client_factory.call_count, before_factories)
        self.assertEqual({p: p.read_bytes() for p in before_proof}, before_proof)
        if stage == 'revision':
            # A cached historical owner without the new marker retains its
            # frozen revision ceiling and replays its actual saved result.
            self.assertNotIn('continued_source_review', old.read_json('request.json'))
            replay = writing.call('compose_document', initial_arguments, identity='article-stopped')
            self.assertEqual(replay.model_dump(mode='json'), observed)
            self.assertEqual(client.ask_json.call_count, before_calls)
        old_files = {p: p.read_bytes() for folder in (old.root, native.root, source)
            for p in folder.rglob('*') if p.is_file()}
        arguments = {'prior_result': 'records/article-stopped/native_result.json'}
        calls = client.ask_json.call_count
        path = old.resolve('writer/writer.json')
        tampered = old.read_json('writer/writer.json')
        tampered['sections'][0]['draft_markdown'] = 'Invented replacement author'
        path.write_text(json.dumps(tampered), encoding='utf-8')
        with self.subTest(proof='tampered author'):
            self.assertFalse(writing.can_resume_after_budget(observed, 'article-stopped'))
            self.assertIsNone(writing.resume_after_budget(observed, 'article-stopped'))
        rejected = writing.call('compose_document', arguments, identity='article-tampered')
        self.assertEqual(rejected.status, 'error')
        self.assertEqual(client.ask_json.call_count, calls)
        path.write_bytes(old_files[path])
        accepting = True
        with patch.object(writing, '_select_materials', side_effect=AssertionError('No reselection')):
            accepted = writing.call('compose_document', action['arguments'] if stage != 'initial' else arguments,
                identity='article-continued')
        self.assertEqual(accepted.status, 'ok', accepted)
        self.assertEqual(client.ask_json.call_count, calls + 1)
        current = ArtifactStore(root / 'tools/writing/article-continued')
        self.assertEqual(current.read_json('request.json')['prior_input_reuse']['candidate_contract'],
            'retained_article_author.v1')
        from simple_ar.report.writing import professional_writer_config, retained_article_candidate, completed_composition_writer
        binding = current.read_json('request.json')
        self.assertEqual(binding['continued_source_review'], 'inspect_only.v1')
        self.assertEqual(binding['report_binding']['requirement'], requirement)
        self.assertEqual(professional_writer_config(binding).max_review_iterations, 0)
        historical = {key: value for key, value in binding.items() if key != 'continued_source_review'}
        self.assertEqual(professional_writer_config(historical).max_review_iterations,
            requirement['config']['max_review_iterations'])
        self.assertEqual(old.read_json('writer/report_inputs.json')['inputs']['binding']['config']['max_review_iterations'],
            requirement['config']['max_review_iterations'])
        self.assertEqual([label for label, _ in prompts[calls:]], ['article-independent-review'])
        if stage == 'revision_negative':
            self.assertTrue(accepted.data['unresolved'])
            self.assertEqual(writing.final_review(accepted.data)['review_status'], 'needs_revision')
            self.assertFalse(writing.can_resume_after_budget(accepted.model_dump(mode='json'), 'article-continued'))
            with self.assertRaises(ValueError):
                retained_article_candidate(current, binding, unchecked_revision=True)
        else:
            self.assertFalse(accepted.data.get('unresolved'))
        self.assertEqual(current.read_json('writer/writer.json')['sections'][0]['draft_markdown'], body)
        self.assertEqual({p: p.read_bytes() for p in old_files}, old_files)
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model replay')):
            completed_composition_writer(store=current, binding=binding, identity='article-continued')
            retained_article_candidate(current, binding)
            replay = writing.call('compose_document', action['arguments'] if stage != 'initial' else arguments,
                identity='article-continued')
        self.assertEqual(replay.model_dump(mode='json'), accepted.model_dump(mode='json'))

        # A new work order decides from the saved inspection, not from which
        # public locator happened to be chosen. Completed negative checks may
        # seed revision, never another check-only adoption.
        self._record_native_result(root, 'article-continued', accepted)
        writing.document_source_contract = 'document_source.v1'
        with patch.object(client, 'ask_json', side_effect=AssertionError('No model during completed source replay')):
            intent = {'prior_result': 'records/article-continued/native_result.json'}
            admission = writing._admit_document_source(ArtifactStore(root / 'tools/writing/after-completion'),
                intent, None, None, writing.report_inputs())
            resolved = writing._resolve_document_inputs(ComposeRequest.model_validate({**intent,
                'goal': binding['request']['goal']}), 'after-completion', report=writing.report_inputs(),
                original=None, selection=None, document_work=True, caller_fields=frozenset(intent),
                work_source=admission, normalize_arguments=None)
            if stage == 'revision_negative':
                self.assertIsNotNone(resolved.candidate)
                self.assertTrue(resolved.linkage['checked_work'])
                self.assertFalse(resolved.recipe.reuse_prior_inputs)
            else:
                self.assertIsNotNone(resolved.candidate)
                self.assertEqual(resolved.linkage['candidate_contract'], 'retained_article_author.v1')
            feedback_report = {**writing.report_inputs(),
                'requests': [*writing.report_inputs()['requests'], 'Clarify the saved explanation; keep the evidence.']}
            feedback = writing._resolve_document_inputs(ComposeRequest.model_validate({**intent,
                'goal': binding['request']['goal']}), 'after-feedback', report=feedback_report,
                original=None, selection=None, document_work=True, caller_fields=frozenset(intent),
                work_source=admission, normalize_arguments=None)
            if stage == 'revision_negative':
                self.assertIsNotNone(feedback.candidate)
                self.assertTrue(feedback.linkage['checked_work'])
                self.assertFalse(feedback.recipe.reuse_prior_inputs)
            else:
                self.assertIsNone(feedback.candidate)
                self.assertTrue(feedback.recipe.reuse_prior_inputs)

    def test_document_work_continues_failed_section_candidate_before_any_redraft(self):
        from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
        from simple_ar.report.review_evidence import DRAFT_REFERENCE_CONTRACT

        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        templates = ArtifactStore(root / 'fixture-templates')
        templates.write_text('two-sections.md', '# Recorded value and scope\n\n'
            '## Results\nExplain the saved value.\n\n## Scope\nState the execution limit.\n')
        templates.write_text('criteria.md', 'Check both sections against the supplied record and execution scope.\n')
        requirement = freeze_report_requirement({'template': str(templates.resolve('two-sections.md')),
            'criteria': str(templates.resolve('criteria.md')), 'outline_strategy': 'template',
            'draft_scope': 'document', 'review_scope': 'document', 'reviewer': 'llm',
            'document_review': True, 'max_review_iterations': 0,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='sections')
        self.assertEqual(requirement['outline'], [
            {'heading': 'Results', 'required': True}, {'heading': 'Scope', 'required': True}])
        goal = 'Explain the saved value and execution scope using the supplied record'
        # Freeze actual mounted evidence once, as the controller does. Rebuilding
        # it after a mutation would accidentally authorize the changed bytes.
        report = material_report_binding(workspace, requirement, mounts=[str(table)], requests=[goal])
        writing.report_inputs = lambda: report
        writing.professional_inputs = True
        original_answer = client.ask_json.side_effect
        accepting = False
        bodies = {'results': 'The supplied record says value=2.5.',
            'scope': 'The saved record is supplied evidence. No new execution was requested.'}

        def answer(system, prompt, **options):
            view, _suffix_start = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])
            label = options['label']
            # Fixture usage records model work without contacting a provider.
            reservation = f'section-fixture-{len(prompts)}'
            workspace.ledger.reserve(reservation, {'llm_requests': 1},
                session_id='task', attempt_id='candidate-review' if accepting else 'candidate-author',
                purpose=label)
            workspace.ledger.settle(reservation, {'llm_requests': 1}, actual_source='fixture')
            if label.startswith('composition-selection-'):
                return original_answer(system, prompt, **options)
            prompts.append((label, view))
            if label == 'report-writer-document' and not accepting:
                self.assertEqual([row['section']['section_id'] for row in view['sections']], list(bodies))
                self.assertEqual([row['section']['heading'] for row in view['sections']], ['Results', 'Scope'])
                return {'sections': [{'section_id': sid, 'draft_markdown': body}
                    for sid, body in bodies.items()]}
            if label in {'report-document-reviewer', 'report-document-reviewer-format-correction'}:
                self.assertEqual(view['task'], 'review_document_coherence')
                self.assertEqual({row['section_id']: row['markdown'] for row in view['sections']}, bodies)
                if accepting:
                    self.assertEqual(label, 'report-document-reviewer')
                    self.assertEqual(view['historical_findings_to_check'], [])
                    self.assertFalse(view['response_format_correction'])
                    self.assertEqual(view['draft_reference_contract'], DRAFT_REFERENCE_CONTRACT)
                    for sid in bodies:
                        self.assertEqual(view['draft_locator'][sid]['paragraph_count'], 1)
                        self.assertEqual(view['draft_locator'][sid]['previews']['p1'], bodies[sid][:80])
                    return {'section_reviews': [{'section_id': sid, 'verdict': 'pass', 'findings': []}
                        for sid in bodies]}
                return {'section_reviews': 'invalid even after the one format correction'}
            self.fail('Unexpected selection, initial writing or correction: ' + label)

        client.ask_json.side_effect = answer
        initial_arguments = {'material_roots': [str(source)], 'publication': {'compile_pdf': False}}
        failed = writing.call('compose_document', initial_arguments, identity='section-stopped')
        self.assertEqual(failed.status, 'error', failed)
        self.assertTrue(failed.data['incomplete'])
        self.assertIn('retained_candidate', failed.data, failed.error)
        self.assertEqual(failed.data['retained_candidate']['status'], 'unreviewed')
        self.assertFalse(failed.data['retained_candidate']['adopted'])
        self.assertEqual([label for label, _ in prompts], ['composition-selection-1',
            'composition-selection-2', 'report-writer-document', 'report-document-reviewer',
            'report-document-reviewer-format-correction'])
        old = ArtifactStore(root / 'tools/writing/section-stopped')
        old_binding = old.read_json('request.json')
        old_written = old.read_json('writer/writer.json')
        self.assertEqual(len(old_written['sections']), 2)
        self.assertEqual(len(old_binding['request']['materials']), 1)
        self.assertFalse(old.exists('result.json'))
        self.assertFalse(old.exists('professional_result.json'))
        self.assertFalse(workspace.resolve('workspace/manuscripts/section-stopped/manuscript.md').exists())
        # Controller fixture: persist precisely the gateway's failure, never an
        # invented successful parent result or a developer-exported manuscript.
        native = self._record_native_result(root, 'section-stopped', failed)
        reference = 'records/section-stopped/native_result.json'
        arguments = {'prior_result': reference}
        old_files = {path: path.read_bytes() for directory in
            (old.root, native.root, source, workspace.resolve('workspace/manuscripts/section-stopped'))
            for path in directory.rglob('*') if path.is_file()}
        old_entries = workspace.ledger.to_dict()['entries']
        calls = client.ask_json.call_count

        for fault in ('changed-source', 'changed-evidence', 'missing-section', 'author-body'):
            with self.subTest(fault=fault):
                changed = {}
                try:
                    if fault == 'changed-source':
                        path = old.resolve('writer/report_inputs.json')
                        payload = old.read_json('writer/report_inputs.json')
                        payload['context']['topic'] += ' changed'
                        changed[path] = json.dumps(payload).encode()
                    elif fault == 'changed-evidence':
                        changed[table] = b'value=999\nscope=changed evidence\n'
                    else:
                        # Alter both projections together so a mere checkpoint
                        # equality check cannot stand in for author/plan proof.
                        for name in ('writer/writer.json', 'writer/sections.json'):
                            payload = old.read_json(name)
                            candidate = payload['completed'] if name.endswith('sections.json') else payload
                            if fault == 'missing-section':
                                candidate['sections'].pop()
                            else:
                                candidate['sections'][0]['draft_markdown'] = 'An invented author body.'
                            changed[old.resolve(name)] = json.dumps(payload).encode()
                    for path, raw in changed.items():
                        path.write_bytes(raw)
                    rejected = writing.call('compose_document', arguments, identity='reject-' + fault)
                    self.assertEqual(rejected.status, 'error', rejected)
                    diagnostic = {'changed-source': 'original section owner/checkpoint',
                        'changed-evidence': 'Report source', 'missing-section': 'full ordered plan',
                        'author-body': 'matching complete author response'}[fault]
                    self.assertIn(diagnostic, rejected.error)
                    self.assertEqual(client.ask_json.call_count, calls, rejected)
                    self.assertFalse(ArtifactStore(root / 'tools/writing' / ('reject-' + fault)).exists('result.json'))
                finally:
                    for path in changed:
                        path.write_bytes(old_files[path])

        accepting = True
        with patch.object(writing, '_select_materials', side_effect=AssertionError('No reselection')):
            accepted = writing.call('compose_document', arguments, identity='section-continued')
        self.assertEqual(accepted.status, 'ok', accepted)
        self.assertEqual([label for label, _ in prompts[calls:]], ['report-document-reviewer'])
        self.assertEqual(client.ask_json.call_count, calls + 1)
        self.assertFalse(accepted.data.get('unresolved'))
        self.assertEqual(accepted.data['delivery_review']['status'], 'reviewed')
        current = ArtifactStore(root / 'tools/writing/section-continued')
        binding = current.read_json('request.json')
        self.assertEqual(binding['report_binding']['requirement'], requirement)
        for field in ('materials', 'sources', 'figures', 'publication'):
            self.assertEqual(binding['request'][field], old_binding['request'][field])
        self.assertEqual(binding['candidate_source']['contract'], 'retained_sections.v1')
        self.assertEqual(binding['candidate_source']['owner'], str(old.root.resolve()))
        self.assertEqual(binding['prior_input_reuse']['result_ref'], reference)
        snapshot = current.read_json('writer/report_inputs.json')
        self.assertEqual(snapshot['candidate_source'], binding['candidate_source'])
        self.assertEqual(snapshot['config'], requirement['config'])
        written = current.read_json('writer/writer.json')
        self.assertNotEqual(written['snapshot_id'], old_written['snapshot_id'])
        self.assertEqual({row['section_id']: row['draft_markdown'] for row in written['sections']}, bodies)
        self.assertEqual(written['independent_review']['review_status'], 'reviewed')
        self.assertEqual(sum(row['action'] == 'document_current_review' for row in written['iterations']), 1)
        self.assertFalse(any(row['action'] in {'draft', 'document_joint_revise'} for row in written['iterations']))
        markdown = workspace.resolve(accepted.data['editable_markdown']).read_text(encoding='utf-8')
        for heading, body in zip(('Results', 'Scope'), bodies.values()):
            self.assertIn('## ' + heading, markdown)
            self.assertIn(body, markdown)

        # Admission/recovery and publication consume the newly inspected exact
        # version without reviving the stopped review or paying for another call.
        from simple_ar.report.writing import completed_composition_writer
        with patch.object(client, 'ask_json', side_effect=AssertionError('Replay must be offline')):
            completed_composition_writer(store=current, binding=binding, identity='section-continued')
            publication = writing.publication_source(accepted.data['editable_markdown'], report,
                observation=accepted.model_dump(mode='json'))
            writing.check_delivery_set(publication, accepted.artifacts, set())
            self.assertIsNotNone(writing.continuation(accepted.model_dump(mode='json'), 'section-continued'))
            replayed = writing.call('compose_document', arguments, identity='section-continued')
            self.assertEqual(replayed.model_dump(mode='json'), accepted.model_dump(mode='json'))
            client.ask_json.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in old_files}, old_files)
        self.assertEqual(workspace.ledger.to_dict()['entries'][:len(old_entries)], old_entries)
        fixture_usage = [entry for entry in workspace.ledger.entries if entry.actual_source == 'fixture']
        self.assertEqual(len(fixture_usage), calls + 1)
        self.assertTrue(all(entry.status == 'settled' for entry in fixture_usage))

    def test_document_work_nested_mounts_preserve_layout_and_unchanged_copy_defects(self):
        root, workspace, writing, source, script, table, client, prompts = self._document_work_fixture()
        # The same authorized tree may also have narrower/duplicate aliases.
        # Neither alias insertion order nor a deeper mount may split siblings.
        workspace.mounts['nested_results'] = source / 'results'
        workspace.mounts['source9'] = source
        original_answer = client.ask_json.side_effect
        first_review = True

        def answer(system, prompt, **options):
            nonlocal first_review
            if options['label'] == 'article-independent-review' and first_review:
                first_review = False
                view = json.loads(prompt)
                target = workspace.resolve('workspace/manuscripts/copy-defect-first/materials/source0/results/record.txt')
                index = next(index for index, row in enumerate(view['delivery_artifacts'])
                    if any(workspace.resolve(copy['path']) == target for copy in row.get('copies', [])))
                pointer = f'/delivery_artifacts/{index}/text'
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'copy-unit-missing', 'type': 'evidence_gap',
                        'severity': 'major', 'required_action': 'verify',
                        'message': 'The delivered record omits the recorded physical unit.',
                        'suggested_action': 'Supply the recorded unit in the actual record and inspect the new bytes.',
                        'delivery_targets': [pointer], 'evidence_quotes': [
                            {'pointer': pointer, 'quote': 'value=2.5', 'role': 'recorded_material'}]}]}]}
            return original_answer(system, prompt, **options)

        client.ask_json.side_effect = answer
        arguments = {'goal': 'Explain the saved record with its unit', 'material_roots': [str(source)],
            'publication': {'compile_pdf': False}, 'finish': True}
        first = writing.call('compose_document', arguments, identity='copy-defect-first')
        self.assertEqual(first.status, 'ok', first)
        self.assertEqual(first.data['delivery_review']['status'], 'needs_revision')
        self.assertTrue(writing.can_return_gap(first.model_dump(mode='json'), 'copy-defect-first'))
        with patch('simple_ar.report.writing.validate_document_inspection',
                side_effect=ValueError('Incomplete original response')):
            self.assertFalse(writing.can_return_gap(first.model_dump(mode='json'), 'copy-defect-first'))
        original_defect, = first.data['delivery_review']['defects']
        self.assertIsNone(writing.continuation(first.model_dump(mode='json'), 'copy-defect-first'))
        first_owner = ArtifactStore(root / 'tools/writing/copy-defect-first')
        # A real negative inspection is useful only with its exact successful
        # composition receipt and complete current artifact set.
        saved = first_owner.read_json('result.json')
        for change in ({'status': 'error'}, {'error': 'Writer interrupted'},
                {'artifacts': saved['artifacts'][:-1]}):
            with self.subTest(retained_negative=change):
                altered = {**saved, **change}
                first_owner.write_json('result.json', altered)
                presented = {**first.model_dump(mode='json'), 'artifacts': altered['artifacts']}
                self.assertFalse(writing.can_return_gap(presented, 'copy-defect-first'))
                self.assertIsNone(writing.continuation(presented, 'copy-defect-first'))
        first_owner.write_json('result.json', saved)
        self.assertTrue(writing.can_return_gap(first.model_dump(mode='json'), 'copy-defect-first'))
        # Stop disposition uses the retained review as well as presentation;
        # only the parent native-observation registration boundary is stubbed.
        with patch('simple_ar.tools.writing.stop_decision',
                side_effect=lambda owner, observation, identity, reason, detail: {'reason': reason}):
            self.assertEqual(writing.stop_after_review(first.model_dump(mode='json'),
                'copy-defect-first'), {'reason': 'work_gap'})
            first_owner.write_json('result.json', {**saved, 'status': 'error'})
            self.assertEqual(writing.stop_after_review(first.model_dump(mode='json'),
                'copy-defect-first'), {'reason': 'owner_state_unresolved'})
            first_owner.write_json('result.json', saved)
        calls = client.ask_json.call_count
        for _ in range(2):
            self.assertIsNone(writing.continuation(first.model_dump(mode='json'), 'copy-defect-first'))
        self.assertEqual(client.ask_json.call_count, calls, 'Inspecting a gap must not start corrective work')
        frozen = {path: path.read_bytes() for path in first_owner.root.rglob('*') if path.is_file()}
        frozen.update({workspace.resolve(path): workspace.resolve(path).read_bytes() for path in first.artifacts})

        second = writing.call('compose_document', {'goal': 'Clarify the same record',
            'prior_draft': first.data['editable_markdown'], 'publication': {'compile_pdf': False},
            'finish': True}, identity='copy-defect-second')
        self.assertEqual(second.status, 'ok', second)
        self.assertEqual(second.data['delivery_review']['status'], 'needs_revision',
            'A fresh pass opinion cannot repair the same defective bytes under a new copy path')
        self.assertIsNone(writing.continuation(second.model_dump(mode='json'), 'copy-defect-second'))
        owner = ArtifactStore(root / 'tools/writing/copy-defect-second')
        binding = owner.read_json('request.json')
        history = binding['delivery_review']['prior_defects']
        self.assertIn(original_defect, history)
        rebound, = [row for row in history if row != original_defect]
        self.assertEqual(rebound['finding_ref']['finding'], original_defect['finding'])
        self.assertEqual(rebound['targets'][0]['sha256'], original_defect['targets'][0]['sha256'])
        current_copy = workspace.resolve('workspace/manuscripts/copy-defect-second/materials/source0/results/record.txt')
        self.assertEqual(workspace.resolve(rebound['targets'][0]['path']), table)
        self.assertIn(str(current_copy), [copy['path'] for copy in rebound['targets'][0]['copies']])
        self.assertIsNone(rebound['targets'][0]['producer'])
        rows = binding['document_work']['materials']
        copied_script = workspace.resolve(next(row['path'] for row in rows if row['source'] == str(script)))
        self.assertEqual((copied_script.parent / '../results/record.txt').resolve(), current_copy)
        self.assertEqual((copied_script.parent / '../results/record.txt').read_bytes(), table.read_bytes())
        for pointer in rebound['finding']['delivery_targets']:
            subject = binding['delivery_review']['artifacts'][int(pointer.split('/')[2])]
            self.assertEqual(subject['path'], str(table))
            self.assertIn(str(current_copy), [copy['path'] for copy in subject['copies']])
        # Reconstruct the same rejected version from its saved responses only.
        calls = client.ask_json.call_count
        replay = writing.call('compose_document', {'goal': 'Clarify the same record',
            'prior_draft': first.data['editable_markdown'], 'publication': {'compile_pdf': False},
            'finish': True}, identity='copy-defect-second')
        self.assertEqual(replay.model_dump(mode='json'), second.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)

        unrelated = source / 'same-bytes-unrelated.txt'
        unrelated.write_bytes(table.read_bytes())
        other = writing._document_material_copies([str(unrelated)], identity='copy-unrelated')
        other_snapshot = writing._freeze_delivery_review([other['materials'][0]['path']], 'copy-unrelated')
        writing._document_copy_review_history(other_snapshot, first.data,
            first_owner.read_json('request.json')['document_work'], other)
        self.assertEqual(other_snapshot['prior_defects'], [], 'Equal bytes from an unrelated source are not a lineage')

        table.write_bytes(b'value=2.5\nunit=recorded_units\nscope=saved record; no new execution\n')
        revised = writing.call('compose_document', {**arguments,
            'prior_draft': second.data['editable_markdown']}, identity='copy-defect-revised')
        self.assertEqual(revised.status, 'ok', revised)
        self.assertEqual(revised.data['delivery_review']['status'], 'reviewed')
        self.assertFalse(writing.can_return_gap(revised.model_dump(mode='json'), 'copy-defect-revised'))
        self.assertEqual(revised.data['delivery_review']['defects'], [])
        self.assertIsNotNone(writing.continuation(revised.model_dump(mode='json'), 'copy-defect-revised'))
        self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)

    def test_native_section_work_owns_review_delivery_feedback_and_read_only_reuse(self):
        from simple_ar.report.writing import completed_composition_writer
        from tests.report_review_fixtures import prompt_payload
        root, workspace, writing, source, _, _, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        selection_answer = client.ask_json.side_effect
        edited = False

        def answer(system, prompt, **options):
            label = options['label']
            if label.startswith('composition-selection-'):
                return selection_answer(system, prompt, **options)
            view = prompt_payload(prompt)
            prompts.append((label, view))
            if label.startswith('report-writer-'):
                def body(section):
                    return ('The supplied record reports value=2.5.' if section['heading'] == 'Results' else
                        'The supplied result was not rerun.' if edited else 'No new execution was requested.')
                if 'sections' in view:
                    return {'sections': [{'section_id': row['section']['section_id'],
                        'draft_markdown': body(row['section'])} for row in view['sections']]}
                return {'section_id': view['section']['section_id'], 'draft_markdown': body(view['section'])}
            self.assertEqual(label, 'report-document-reviewer',
                'The shared section owner must inspect the actual assembled document once')
            self.assertEqual(view['delivery_review_contract'], 'delivery_artifacts.v1')
            return {'section_reviews': [{'section_id': row['section_id'], 'verdict': 'pass', 'findings': []}
                for row in view['sections']]}

        client.ask_json.side_effect = answer
        arguments = {'goal': 'Explain the supplied value and its scope', 'material_roots': [str(source)],
            'writing_method': 'sections', 'required_headings': ['Results', 'Scope'], 'finish': True}
        result = writing.call('compose_document', arguments, identity='section-owned')
        self.assertEqual(result.status, 'ok', result)
        owner = ArtifactStore(root / 'tools/writing/section-owned')
        binding = owner.read_json('request.json')
        self.assertEqual(binding['section_delivery_review_contract'], 'section_delivery_review.v1')
        self.assertEqual(result.data['independent_review']['review_status'], 'reviewed')
        self.assertEqual(result.data['delivery_review']['status'], 'reviewed')
        self.assertEqual([label for label, _ in prompts].count('report-document-reviewer'), 1)
        self.assertFalse((owner.root / 'delivery-review/request.json').exists(),
            'No second post-assembly reviewer may repeat the shared Writer review')
        self.assertEqual(writing.continuation(result.model_dump(mode='json'), 'section-owned')['tool_name'], 'deliver_result')
        calls = client.ask_json.call_count
        original = {path: path.read_bytes() for path in owner.root.rglob('*') if path.is_file()}
        with patch.object(client, 'ask_json', side_effect=AssertionError('No paid work during retained reuse')):
            completed_composition_writer(store=owner, binding=binding, identity='section-owned')
            replay = writing.call('compose_document', arguments, identity='section-owned')
        self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)
        edited = True
        changed = writing.call('compose_document', {'goal': 'Make the execution limitation more direct',
            'prior_draft': result.data['editable_markdown'], 'finish': True}, identity='section-feedback')
        self.assertEqual(changed.status, 'ok', changed)
        current = ArtifactStore(root / 'tools/writing/section-feedback')
        self.assertEqual(current.read_json('request.json')['request']['writing_method'], 'sections')
        self.assertEqual(current.read_json('request.json')['request']['required_headings'], ['Results', 'Scope'])
        self.assertEqual([label for label, _ in prompts].count('report-document-reviewer'), 2)
        self.assertIn('The supplied result was not rerun.', workspace.resolve(changed.data['editable_markdown']).read_text())
        self.assertNotEqual(changed.data['editable_markdown'], result.data['editable_markdown'])
        self.assertEqual({path: path.read_bytes() for path in original}, original)
        self.assertIsNotNone(writing.continuation(changed.model_dump(mode='json'), 'section-feedback'))
        self._record_native_result(root, 'section-feedback', changed)
        before = len(prompts)
        checked = writing.call('compose_document', {'goal': current.read_json('request.json')['request']['goal'],
            'prior_result': 'records/section-feedback/native_result.json', 'finish': True}, identity='section-rechecked')
        self.assertEqual(checked.status, 'ok', checked)
        self.assertEqual([label for label, _ in prompts[before:]], ['report-document-reviewer'],
            'A complete section candidate needs its new inspection, not selection or redrafting')
        self.assertIsNotNone(writing.continuation(checked.model_dump(mode='json'), 'section-rechecked'))

    def test_inspected_author_output_gap_can_be_reorganized_without_adopting_failed_work(self):
        from tests.report_review_fixtures import prompt_payload
        from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        select = client.ask_json.side_effect
        repairing = False

        def answer(system, prompt, **options):
            label = options['label']
            if label.startswith('composition-selection-'):
                self.assertFalse(repairing, 'A complete author keeps its proven material recipe')
                return select(system, prompt, **options)
            view = prompt_payload(prompt)
            prompts.append((label, view))
            if label.startswith('report-writer-'):
                previous = view.get('previous_document', {}).get('drafts', [])
                if repairing:
                    self.assertTrue(previous, 'Reorganization must retain the proved old body')
                body = 'The supplied record says value=2.5. No new execution.'
                if 'sections' in view:
                    return {'sections': [{'section_id': row['section']['section_id'], 'draft_markdown': body}
                        for row in view['sections']]}
                return {'section_id': view['section']['section_id'], 'draft_markdown': body}
            self.assertIn(label, {'report-document-reviewer', 'report-document-verifier'})
            return {'section_reviews': [{'section_id': row['section_id'], 'verdict': 'pass', 'findings': []}
                for row in view['sections']]}

        client.ask_json.side_effect = answer
        args = {'goal': 'Explain the recorded value and what was not executed',
            'material_roots': [str(source / 'code'), str(source / 'results')], 'writing_method': 'sections',
            'required_headings': ['Recorded result', 'Execution limits'], 'finish': True}
        requirement = freeze_report_requirement({'outline_strategy': 'auto',
            'draft_scope': 'document', 'review_scope': 'document', 'reviewer': 'llm',
            'document_review': True, 'max_review_iterations': 1,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='sections')
        writing.report_inputs = lambda: material_report_binding(workspace, requirement,
            mounts=args['material_roots'], requests=[args['goal']])
        writing.professional_inputs = True
        # Reproduce a pre-contract template owner, rather than editing any
        # saved receipt: all atomic author/reviewer responses freeze normally.
        def legacy_plan(memory, *_):
            memory.survey_contract = {**memory.survey_contract, 'outline_strategy': 'template'}
            return memory
        with patch('simple_ar.report.writing._fixed_section_memory', side_effect=legacy_plan):
            failed = writing.call('compose_document', args, identity='output-gap')
        self.assertEqual(failed.status, 'error', failed)
        self.assertIn('fixed section structure', failed.error)
        native = self._record_native_result(root, 'output-gap', failed)
        observed = failed.model_dump(mode='json')
        original = ArtifactStore(root / 'tools/writing/output-gap')
        protected = {p: p.read_bytes() for folder in (original.root, native.root, source)
            for p in folder.rglob('*') if p.is_file()}
        ledger = writing.ledger.to_dict()
        with patch.object(client, 'ask_json', side_effect=AssertionError('A work proof cannot call a model')):
            self.assertIsNotNone(writing._author_completion_gap(observed, 'output-gap'))
            self.assertTrue(writing.can_return_gap(observed, 'output-gap'))
            visible = writing.coordination_result(failed, receipt_path='attempts/output-gap/native_result.json')
            self.assertEqual(visible['status'], 'error')
            self.assertEqual(visible['data']['retained_candidate']['status'], 'author_not_adopted')
            self.assertFalse(visible['data']['mechanical_constraints']['adopted'])
            changed = {**observed, 'error': 'An invented failure explanation'}
            self.assertFalse(writing.can_return_gap(changed, 'output-gap'))
            table.write_bytes(b'changed evidence')
            self.assertFalse(writing.can_return_gap(observed, 'output-gap'))
            table.write_bytes(protected[table])
            response = next((original.root / 'writer/model_calls').glob('*/response.json'))
            raw = response.read_bytes()
            response.unlink()
            self.assertFalse(writing.can_return_gap(observed, 'output-gap'))
            response.write_bytes(raw)
            checkpoint_path = original.resolve('writer/sections.json')
            raw_checkpoint = checkpoint_path.read_bytes()
            incomplete = json.loads(raw_checkpoint)
            incomplete['completed']['document_review_done'] = False
            checkpoint_path.write_text(json.dumps(incomplete), encoding='utf-8')
            self.assertFalse(writing.can_return_gap(observed, 'output-gap'))
            checkpoint_path.write_bytes(raw_checkpoint)
        self.assertEqual(writing.ledger.to_dict(), ledger)
        repairing = True
        before = len(prompts)
        revised = writing.call('compose_document', {'goal': args['goal'],
            'prior_result': 'records/output-gap/native_result.json', 'finish': True}, identity='output-reorganized')
        self.assertEqual(revised.status, 'ok', revised)
        self.assertFalse(any(label.startswith('composition-selection-') for label, _ in prompts[before:]))
        self.assertEqual(revised.data['constraints_checked']['headings'], args['required_headings'])
        self.assertIsNotNone(writing.continuation(revised.model_dump(mode='json'), 'output-reorganized'))
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        self._record_native_result(root, 'output-reorganized', revised)
        parent = ArtifactStore(root / 'tools/writing/output-reorganized')
        report = parent.read_json('request.json')['report_binding']
        routed = {**report, 'obligation': {'intent': {'kind': 'report', 'request_index': 0}}}
        intent = {'goal': args['goal'], 'prior_draft': revised.data['editable_markdown'],
            'material_roots': [str(source)]}
        with patch.object(client, 'ask_json', side_effect=AssertionError('Routing metadata is not new evidence')):
            admitted = writing._admit_document_source(ArtifactStore(root / 'tools/writing/routed-work'),
                intent, None, None, routed)
            resolved = writing._resolve_document_inputs(ComposeRequest.model_validate(intent), 'routed-work',
                report=routed, original=None, selection=None, document_work=True,
                caller_fields=frozenset(intent), work_source=admitted, normalize_arguments=None)
        self.assertIsNone(resolved.investigation)
        self.assertEqual(resolved.linkage['owner'], 'output-reorganized')
        self.assertIsNotNone(resolved.candidate)

    def test_checked_section_revision_continues_as_work_without_waiving_its_findings(self):
        from tests.report_review_fixtures import prompt_payload
        root, workspace, writing, source, _, _, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        select = client.ask_json.side_effect
        continuing = False
        # A checked proposal fixes the asset path but still omits the actual
        # execution boundary. Presentation ownership must not waive that gap.
        old_scope = 'This record establishes a general conclusion.\n\n![Overview](outputs/figure/main_figure.png)'
        revised_scope = 'Only the recorded observation is described.\n\n![Overview](figures/selected-1.png)'
        final_scope = ('Only the recorded observation is described; no new experiment was run.\n\n'
            '![Overview](figures/selected-1.png)')
        figure = source / 'main_figure.png'
        figure.write_bytes(b'\x89PNG\r\n\x1a\nfixture image bytes')

        def answer(system, prompt, **options):
            label = options['label']
            if label.startswith('composition-selection-'):
                self.assertFalse(continuing, 'Continue the proven recipe without investigating again')
                reply = select(system, prompt, **options)
                if label == 'composition-selection-2':
                    reply['figures'] = [{'path': str(figure), 'caption': 'Recorded value and scope.'}]
                return reply
            view = prompt_payload(prompt)
            prompts.append((label, view))
            if label.startswith('report-writer-'):
                self.assertFalse(continuing, 'A complete improved author body must not be redrafted')
                rows = view['sections'] if 'sections' in view else [{'section': view['section']}]
                drafted = [{'section_id': row['section']['section_id'],
                    'draft_markdown': 'The supplied record says value=2.5.'
                        if row['section']['heading'] == 'Results' else old_scope}
                    for row in rows]
                return {'sections': drafted} if 'sections' in view else drafted[0]
            if label == 'report-document-joint-reviser':
                if continuing:
                    bodies = [row['previous_draft']['draft_markdown'] for row in view['sections']]
                    self.assertIn(revised_scope, bodies)
                    self.assertNotIn(old_scope, bodies)
                return {'sections': [{'section_id': row['section']['section_id'],
                    'draft_markdown': (final_scope if continuing else revised_scope)
                        if row['section']['heading'] == 'Scope' else 'The supplied record says value=2.5.'}
                    for row in view['sections']]}
            self.assertIn(label, {'report-document-reviewer', 'report-document-verifier'})
            reviews = []
            for row in view['sections']:
                body = row['markdown']
                needs = row['heading'] == 'Scope' and body != final_scope
                finding = {'finding_id': 'F1-record-scope' if body == old_scope
                        else 'remaining_execution_scope', 'type': 'missing_limitation',
                    'severity': 'major',
                    'required_action': 'revise', 'section_id': row['section_id'],
                    'message': 'State that the report describes saved observations and no new experiment was run.',
                    'draft_quotes': [{'section_id': row['section_id'], 'quote': body}]}
                reviews.append({'section_id': row['section_id'],
                    'verdict': 'warning' if needs else 'pass', 'findings': [finding] if needs else []})
            return {'section_reviews': reviews}

        client.ask_json.side_effect = answer
        args = {'goal': 'Explain the saved value and execution scope', 'material_roots': [str(source)],
            'writing_method': 'sections', 'required_headings': ['Results', 'Scope'], 'finish': True}
        # This section request uses a frozen report. Set its local allowance in
        # that requirement, rather than overriding it with professional_review.
        from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
        templates = ArtifactStore(root / 'zero-round-templates')
        templates.write_text('report.md', '# Recorded value and scope\n\n'
            '## Results\nExplain the record.\n\n## Scope\nState the figure and limits.\n')
        templates.write_text('criteria.md', 'Respect the saved observation and execution scope.\n')
        zero_requirement = freeze_report_requirement({'template': str(templates.resolve('report.md')),
            'criteria': str(templates.resolve('criteria.md')), 'outline_strategy': 'template',
            'draft_scope': 'document', 'review_scope': 'document', 'reviewer': 'llm',
            'document_review': True, 'max_review_iterations': 0,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='sections')
        zero_report = material_report_binding(workspace, zero_requirement,
            mounts=[str(source)], requests=[args['goal']])
        writing.report_inputs = lambda: zero_report
        writing.professional_inputs = True
        zero_args = dict(args)
        with patch('simple_ar.report.capability.run_report_capability',
                side_effect=AssertionError('Negative work is not a publication')):
            zero = writing.call('compose_document', zero_args, identity='checked-zero')
        self.assertEqual(zero.status, 'ok', zero)
        self.assertIn(old_scope, workspace.resolve(zero.data['editable_markdown']).read_text())
        self.assertTrue(writing.can_return_gap(zero.model_dump(mode='json'), 'checked-zero'))
        calls = client.ask_json.call_count
        ledger = writing.ledger.to_dict()
        with patch.object(client, 'ask_json', side_effect=AssertionError('Recovery cannot reset local rounds')):
            replay = writing.call('compose_document', zero_args, identity='checked-zero')
        self.assertEqual(replay.model_dump(mode='json'), zero.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)
        self.assertEqual(writing.ledger.to_dict(), ledger)
        writing.report_inputs = None
        writing.professional_inputs = False
        with patch('simple_ar.report.capability.run_report_capability',
                side_effect=AssertionError('Negative work must finish before publication assembly')):
            first = writing.call('compose_document', args, identity='checked-first')
        self.assertEqual(first.status, 'ok', first)
        self.assertIsNone(writing.continuation(first.model_dump(mode='json'), 'checked-first'))
        self.assertTrue(writing.can_return_gap(first.model_dump(mode='json'), 'checked-first'))
        self.assertTrue(first.data['editable_markdown'].endswith('/candidate.md'))
        self.assertIn(revised_scope, workspace.resolve(first.data['editable_markdown']).read_text())
        self.assertFalse(workspace.resolve(first.data['version'] + '/manuscript.md').exists())
        self.assertEqual([row['finding_id'] for row in first.data['unresolved']],
            ['remaining_execution_scope'])
        with self.assertRaisesRegex(ValueError, 'Unresolved work'):
            writing.check_delivery(first.data)
        with self.assertRaisesRegex(ValueError, 'unresolved'):
            writing.publication_source(first.data['editable_markdown'], None,
                observation=first.model_dump(mode='json'))
        self._record_native_result(root, 'checked-first', first)
        retained = writing.coordination_result(first)['data']['retained_candidate']
        self.assertEqual((retained['status'], retained['adopted']), ('checked_not_adopted', False))
        self.assertEqual(retained['prior_result'], 'records/checked-first/native_result.json')
        self.assertEqual(retained['review_status'], 'needs_revision')
        owner = ArtifactStore(root / 'tools/writing/checked-first')
        original_result = owner.resolve('result.json').read_bytes()
        corrupt = owner.read_json('result.json')
        del corrupt['data']['artifact_sha256'][first.data['editable_markdown']]
        corrupt['artifacts'].remove(first.data['editable_markdown'])
        try:
            owner.write_json('result.json', corrupt)
            with self.assertRaisesRegex(ValueError, 'receipt|artifacts'):
                writing._composition_receipt('checked-first')
        finally:
            owner.resolve('result.json').write_bytes(original_result)
        prior_bytes = {path: path.read_bytes() for path in owner.root.rglob('*') if path.is_file()}
        with patch('simple_ar.report.writing.validate_document_inspection',
                side_effect=ValueError('Changed original inspection')):
            self.assertFalse(writing.can_return_gap(first.model_dump(mode='json'), 'checked-first'))
            self.assertNotIn('retained_candidate', writing.coordination_result(first)['data'])
        # Also admit the saved pre-fix failure shape: no composition result,
        # but a complete checked proposal and a registered native error. This
        # changes only this fixture, never historical evidence or ledgers.
        owner.resolve('result.json').unlink()
        legacy = ToolResult(tool_name='compose_document', status='error',
            data={'incomplete': True, 'failure_kind': 'image_reference'},
            error='Missing or out-of-scope report image: outputs/figure/main_figure.png')
        native = self._record_native_result(root, 'checked-first', legacy)
        protected = {path: path.read_bytes() for path in owner.root.rglob('*') if path.is_file()}
        protected[native.resolve('native_result.json')] = native.resolve('native_result.json').read_bytes()
        calls, ledger = client.ask_json.call_count, writing.ledger.to_dict()
        with patch.object(client, 'ask_json', side_effect=AssertionError('A saved complete review is read only')):
            self.assertTrue(writing.can_return_gap(legacy.model_dump(mode='json'), 'checked-first'))
            presented = writing.coordination_result(legacy,
                receipt_path='attempts/checked-first/native_result.json')
        self.assertEqual(presented['status'], 'error')
        self.assertEqual(presented['data']['retained_candidate']['status'], 'checked_not_adopted')
        self.assertEqual(client.ask_json.call_count, calls)
        self.assertEqual(writing.ledger.to_dict(), ledger)
        prior_bytes = protected
        # Reconstruct a pre-contract continuation before its Writer operation.
        # A completed parent check must not silently change that saved request
        # into a new correction contract during same-identity recovery.
        legacy_args = {'goal': args['goal'],
            'prior_result': 'records/checked-first/native_result.json'}
        continue_inputs = writing._continued_document_inputs
        with patch.object(writing, '_continued_document_inputs', side_effect=lambda *a, **kw:
                continue_inputs(*a, **{**kw, 'checked_work_admitted': False})), \
                patch('simple_ar.report.writing.write_section_article',
                    side_effect=ValueError('Legacy Writer boundary; no physical request')):
            historical = writing.call('compose_document', legacy_args, identity='legacy-continued')
        self.assertIn('Legacy Writer boundary', historical.error)
        historical_owner = ArtifactStore(root / 'tools/writing/legacy-continued')
        historical_binding = historical_owner.read_json('request.json')
        historical_binding.pop('work_result_contract', None)
        historical_owner.write_json('request.json', historical_binding)
        self.assertNotIn('continued_source_review', historical_binding)
        historical_bytes = {p: p.read_bytes() for p in historical_owner.root.rglob('*') if p.is_file()}
        with patch.object(writing, '_continued_document_inputs', side_effect=lambda *a, **kw:
                continue_inputs(*a, **{**kw, 'checked_work_admitted': True})), \
                patch.object(client, 'ask_json', side_effect=AssertionError('No historical model work')):
            upgraded = writing.call('compose_document', legacy_args, identity='legacy-continued')
        self.assertIn('Inputs changed', upgraded.error,
            'Applying the new admission to an old request reproduces the compatibility failure')
        self.assertEqual({p: p.read_bytes() for p in historical_bytes}, historical_bytes)
        with patch.object(client, 'ask_json', side_effect=AssertionError('No legacy operation replay')), \
                patch('simple_ar.report.writing.write_section_article',
                    side_effect=ValueError('Legacy Writer boundary; no physical request')):
            recovered = writing.call('compose_document', legacy_args, identity='legacy-continued')
        self.assertIn('Legacy Writer boundary', recovered.error)
        self.assertEqual({p: p.read_bytes() for p in historical_bytes}, historical_bytes)
        self.assertEqual(writing.ledger.to_dict(), ledger)
        continuing = True
        before = len(prompts)
        with patch.object(writing, '_select_materials', side_effect=AssertionError('No reselection')):
            second = writing.call('compose_document', {'goal': 'Resolve the remaining scope wording and deliver this same report',
                'prior_result': 'records/checked-first/native_result.json', 'finish': True}, identity='checked-next')
        self.assertEqual(second.status, 'ok', second)
        self.assertEqual([label for label, _ in prompts[before:]],
            ['report-document-joint-reviser', 'report-document-verifier'])
        self.assertIn(final_scope, workspace.resolve(second.data['editable_markdown']).read_text())
        self.assertIsNotNone(writing.continuation(second.model_dump(mode='json'), 'checked-next'))
        self.assertEqual({path: path.read_bytes() for path in prior_bytes}, prior_bytes)
        from simple_ar.report.writing import continued_checked_candidate
        current = ArtifactStore(root / 'tools/writing/checked-next')
        saved_binding = current.read_json('request.json')
        for fault in ('escape', 'cycle', 'records', 'source'):
            corrupt = json.loads(json.dumps(saved_binding))
            if fault == 'escape':
                corrupt['prior_input_reuse']['owner'] = '../outside'
            elif fault == 'cycle':
                corrupt['prior_input_reuse']['owner'] = 'checked-next'
            elif fault == 'records':
                corrupt['prior_input_reuse']['receipt_sha256'].pop('writer/sections.json')
            else:
                corrupt['candidate_source']['sha256']['writer/writer.json'] = '0' * 64
            with self.subTest(lineage=fault), self.assertRaises(ValueError):
                continued_checked_candidate(current, corrupt)
        self.assertEqual({path: path.read_bytes() for path in prior_bytes}, prior_bytes)

    def test_report_bound_sections_suspend_without_professional_review_override(self):
        from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
        from tests.report_review_fixtures import prompt_payload

        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        templates = ArtifactStore(root / 'fixture-templates')
        templates.write_text('sections.md', '# Recorded value and scope\n\n'
            '## Results\nExplain the saved value.\n\n## Scope\nState the execution limit.\n')
        templates.write_text('criteria.md', 'Check the saved value and disclose the execution scope.\n')
        requirement = freeze_report_requirement({'template': str(templates.resolve('sections.md')),
            'criteria': str(templates.resolve('criteria.md')),
            'outline_strategy': 'template', 'draft_scope': 'document', 'review_scope': 'document',
            'reviewer': 'llm', 'document_review': True, 'max_review_iterations': 0,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}}, method='sections')
        goal = 'Explain the saved value and execution scope'
        report = material_report_binding(workspace, requirement, mounts=[str(table)], requests=[goal])
        writing.report_inputs = lambda: report
        writing.professional_inputs = True
        writing.document_source_contract = 'document_source.v1'
        writing.publication_inspection = 'prepared_document.v1'
        selection = client.ask_json.side_effect

        def answer(system, prompt, **options):
            if options['label'].startswith('composition-selection-'):
                return selection(system, prompt, **options)
            self.assertEqual(options['label'], 'report-writer-document',
                             'The report-bound candidate must hand back before inspection')
            view = prompt_payload(prompt)
            prompts.append((options['label'], view))
            return {'sections': [{'section_id': row['section']['section_id'],
                'draft_markdown': 'The supplied record says value=2.5. No new execution was requested.'}
                for row in view['sections']]}

        client.ask_json.side_effect = answer
        result = writing.call('compose_document', {'goal': goal, 'material_roots': [str(source)],
            'publication': {'compile_pdf': False}}, identity='bound-section-handback')
        self.assertEqual(result.status, 'ok', result)
        self.assertEqual(result.data['work_status'], 'awaiting_publication_preparation')
        self.assertEqual(result.data['publication_stage'], 'records/bound-section-handback/document_work.json')
        owner = ArtifactStore(root / 'tools/writing/bound-section-handback')
        binding = owner.read_json('request.json')
        self.assertEqual(binding['writing_owner'], 'professional_report.v1')
        self.assertIsNone(binding['request'].get('professional_review'))
        self.assertEqual(binding['publication_inspection_contract'], 'prepared_document.v1')
        checkpoint = owner.read_json('writer/sections.json')['completed']
        self.assertEqual([row['heading'] for row in checkpoint['sections']], ['Results', 'Scope'])
        self.assertFalse(checkpoint['document_review_done'])
        stage = json.loads(workspace.resolve(result.data['publication_stage']).read_bytes())
        self.assertEqual(stage['candidate']['sections'], checkpoint['sections'])
        self.assertFalse(result.data['adopted'])
        self._record_native_result(root, 'bound-section-handback', result)
        action = writing.continuation(result.model_dump(mode='json'), 'bound-section-handback')
        self.assertIsNotNone(action)
        self.assertEqual(action['arguments']['candidate_result'], 'records/bound-section-handback/native_result.json')
        self.assertEqual([label for label, _ in prompts],
                         ['composition-selection-1', 'composition-selection-2', 'report-writer-document'])

    def test_review_prepared_reference_revalidates_body_view_and_historical_absence(self):
        from types import SimpleNamespace
        from tests.report_review_fixtures import changed_record
        root, workspace, writing = self._real_workspace()
        writing.publication_inspection = 'prepared_document.v1'
        manuscript = 'workspace/manuscripts/retained/manuscript.md'
        path = workspace.resolve(manuscript, write=True)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'## Results\n\nThe supplied record says value=2.5.\n')
        view_path = workspace.resolve('workspace/publications/retained/publication_view.json', write=True)
        view_path.parent.mkdir(parents=True)
        view_path.write_bytes(b'{"schema_version":"publication_view.v1"}\n')
        prepared = {'contract': 'prepared_publication.v1',
            'prepared_result': 'records/prepared/native_result.json', 'prepared_result_sha256': 'receipt-hash',
            'source': {'assembly_identity': {'body_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}},
            'view_path': workspace.locator(str(view_path)),
            'view_sha256': hashlib.sha256(view_path.read_bytes()).hexdigest(),
            'view': json.loads(view_path.read_bytes()), 'adopted': False, 'visual_reviewed': False, 'pages': []}
        # Publication validation itself is covered by the real cooperative
        # fixture. This port returns current bytes, never a cached frozen view.
        def current(ref):
            self.assertEqual(ref, prepared['prepared_result'])
            raw = view_path.read_bytes()
            return {**prepared, 'view': json.loads(raw), 'view_sha256': hashlib.sha256(raw).hexdigest()}
        writing.prepared_publication = Mock(side_effect=current)
        owner = ArtifactStore(root / 'tools/writing/retained')
        owner.write_json('request.json', {'inputs': [], 'publication_inspection_contract': 'prepared_document.v1'})
        request = owner.write_json('writer/review/format_correction/request.json', {
            'prompt': json.dumps({'prepared_publication': prepared})})
        review = {'review_status': 'needs_revision', 'inspection_source': {
            'request_path': request.path,
            'request_sha256': hashlib.sha256(owner.resolve(request).read_bytes()).hexdigest()}}
        review_path = 'workspace/manuscripts/retained/review.json'
        workspace.resolve(review_path, write=True).write_text(json.dumps(review), encoding='utf-8')
        artifacts = [manuscript, review_path]
        data = {'version': 'workspace/manuscripts/retained', 'editable_markdown': manuscript,
            'inputs': [], 'review_path': review_path, 'artifact_sha256': workspace.artifact_hashes(artifacts)}
        owner.write_json('result.json', {'artifacts': artifacts, 'data': data})
        arguments = {'goal': 'Check the prose independently', 'title': 'Fresh check', 'prior_draft': manuscript}
        client = SimpleNamespace(ask_json=Mock(return_value={'section_reviews': [
            {'section_id': 'document', 'verdict': 'pass', 'findings': []}]}))
        writing.client_factory = Mock(return_value=client)
        with patch('simple_ar.tools.publication.run_process') as process:
            result = writing.call('review_document', arguments, identity='bound-view')
            self.assertEqual(result.status, 'ok', result)
            self.assertEqual(result.data['review_status'], 'reviewed')  # No inherited old verdict.
            review_owner = ArtifactStore(root / 'tools/writing/bound-view')
            binding = review_owner.read_json('request.json')
            self.assertEqual(set(binding['prepared_publication']),
                {'prepared_result', 'prepared_result_sha256', 'view_path', 'view_sha256'})
            self.assertEqual(json.loads(review_owner.read_json('review/request.json')['prompt'])['prepared_publication'], prepared)
            # Bound recovery validates even when the task's admission flag is absent.
            writing.publication_inspection = None
            for target, replacement in ((path, path.read_bytes() + b'changed'),
                    (view_path, view_path.read_bytes() + b' '), (view_path, None),
                    (owner.resolve(request), None)):
                with self.subTest(changed=target.name, missing=replacement is None), changed_record(target, replacement):
                    rejected = writing.call('review_document', arguments, identity='bound-view')
                    self.assertEqual(rejected.status, 'error', rejected)
                    with patch.object(writing, 'publication_inspection', 'prepared_document.v1'):
                        fresh = writing.call('review_document', arguments, identity='changed-view')
                        self.assertEqual(fresh.status, 'error', fresh)
            with patch.object(writing, 'prepared_publication', None):
                self.assertEqual(writing.call('review_document', arguments, identity='bound-view').status, 'error')
            with changed_record(review_owner.resolve('request.json'),
                    {key: value for key, value in binding.items() if key != 'prepared_publication'}):
                rejected = writing.call('review_document', arguments, identity='bound-view')
                self.assertEqual(rejected.status, 'error', rejected)
                self.assertIn('lost its publication reference', rejected.error)
            self.assertEqual(writing.call('review_document', arguments, identity='bound-view'), result)
            client.ask_json.assert_called_once()
            process.assert_not_called()
        # An exact caller body is required independently of the composition hash.
        with self.assertRaisesRegex(ValueError, 'source assembly body'):
            writing._review_publication(manuscript, path.read_text() + 'changed', binding)
        # Historical compositions without a prepared review stay source-only.
        owner.write_json('request.json', {'inputs': []})
        historical = {key: value for key, value in data.items() if key != 'review_path'}
        historical['artifact_sha256'] = workspace.artifact_hashes([manuscript])
        owner.write_json('result.json', {'artifacts': [manuscript], 'data': historical})
        writing.publication_inspection = 'prepared_document.v1'
        writing.prepared_publication.reset_mock()
        legacy = writing.call('review_document', arguments, identity='historical-view')
        self.assertEqual(legacy.status, 'ok', legacy)
        historical_owner = ArtifactStore(root / 'tools/writing/historical-view')
        self.assertNotIn('prepared_publication', historical_owner.read_json('request.json'))
        self.assertNotIn('prepared_publication', json.loads(historical_owner.read_json('review/request.json')['prompt']))
        self.assertIn('not scientific certification or visual inspection', legacy.data['scope'])
        # Absent fields remain absent on replay, even if a prepared receipt is now available.
        owner.write_json('request.json', {'inputs': [], 'publication_inspection_contract': 'prepared_document.v1'})
        owner.write_json('result.json', {'artifacts': artifacts, 'data': data})
        self.assertEqual(writing.call('review_document', arguments, identity='historical-view'), legacy)
        external = workspace.resolve('workspace/external.md', write=True)
        external.write_bytes(path.read_bytes())
        raw = writing.call('review_document', {**arguments, 'prior_draft': str(external)}, identity='external-view')
        self.assertEqual(raw.status, 'ok', raw)
        self.assertNotIn('prepared_publication', ArtifactStore(root / 'tools/writing/external-view').read_json('request.json'))
        writing.prepared_publication.assert_not_called()

    def _assert_prepared_independent_review(self, root, workspace, writing, completed, inspection, table):
        """Exercise the consumer against the already proved cooperative fixture."""
        from types import SimpleNamespace
        manuscript = workspace.resolve(completed.data['editable_markdown'])
        preserved = {path: path.read_bytes() for directory in (
            manuscript.parent, root / 'tools/writing/cooperative-author',
            workspace.resolve(inspection['view_path']).parent)
            for path in directory.rglob('*') if path.is_file()}
        arguments = {'goal': 'Independently check the saved value against the supplied record',
            'title': 'Fresh scientific check', 'prior_draft': completed.data['editable_markdown'],
            'materials': [{'path': str(table)}]}
        client = SimpleNamespace(ask_json=Mock(return_value={'section_reviews': [
            {'section_id': 'document', 'verdict': 'pass', 'findings': []}]}))
        with patch.object(writing, 'client_factory', return_value=client), \
                patch('simple_ar.tools.publication.run_process') as process:
            result = writing.call('review_document', arguments, identity='fresh-prepared-review')
            self.assertEqual(result.status, 'ok', result)
            self.assertEqual(result.data['review_status'], 'reviewed')
            owner = ArtifactStore(root / 'tools/writing/fresh-prepared-review')
            self.assertEqual(owner.read_json('request.json')['prepared_publication'],
                {key: inspection[key] for key in
                 ('prepared_result', 'prepared_result_sha256', 'view_path', 'view_sha256')})
            prompt = json.loads(owner.read_json('review/request.json')['prompt'])
            self.assertEqual(prompt['prepared_publication'], inspection)
            self.assertEqual(prompt['sections'][0]['markdown'], manuscript.read_bytes().decode('utf-8'))
            self.assertEqual(prompt['historical_findings_to_check'], [])
            self.assertIn('not scientific certification or visual inspection', result.data['scope'])
            self.assertEqual(writing.call('review_document', arguments, identity='fresh-prepared-review'), result)
            client.ask_json.assert_called_once()
            process.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)

    def test_cooperative_publication_prepares_before_review_and_resumes_same_author(self):
        import shutil
        from simple_ar.core.capabilities import AttemptManifest, CapabilityContext
        from simple_ar.integrations.llm import LLMError
        from simple_ar.tools.writing import composition_owner
        from simple_ar.tools.publication import PublicationToolGateway
        from tests.report_review_fixtures import changed_record

        for outcome in ('reviewed', 'negative', 'stopped'):
            with self.subTest(outcome=outcome):
                root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
                writing.client_factory = Mock(wraps=writing.client_factory)
                writing.document_source_contract = 'document_source.v1'
                writing.publication_inspection = 'prepared_document.v1'
                publication = PublicationToolGateway(root, workspace, workspace.ledger,
                    report_source=lambda path: writing.publication_source(path, None),
                    candidate_source=writing.preparation_source, owned_execution=True)
                writing.prepared_publication = lambda ref: publication.inspection_source(ref, session_id='task')
                arguments = {'goal': 'Explain the saved value and execution scope',
                    'material_roots': [str(source)], 'professional_review': {'max_revision_rounds': 0},
                    'publication': {'compile_pdf': False, 'citation_style': 'numeric'}, 'finish': True}
                if outcome == 'reviewed':
                    figure = workspace.resolve('workspace/recorded.svg', write=True)
                    figure.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="40">'
                        '<text x="5" y="25">value = 2.5</text></svg>', encoding='utf-8')
                    arguments['figures'] = [{'path': str(figure),
                        'caption': 'Saved observation: value 2.5; no new execution.'}]
                    original_answer = client.ask_json.side_effect
                    def include_figure(system, prompt, **options):
                        answer = original_answer(system, prompt, **options)
                        if options['label'] == 'composition-article':
                            target = next(iter(json.loads(prompt)['provided_figure_captions']))
                            answer['draft_markdown'] += f'\n\n![Saved observation]({target})'
                        return answer
                    client.ask_json.side_effect = include_figure
                first = writing.call('compose_document', arguments, identity='cooperative-author')
                self.assertEqual(first.status, 'ok', first)
                self.assertEqual(first.data['work_status'], 'awaiting_publication_preparation')
                self.assertFalse(first.data['adopted'])
                self.assertEqual([label for label, _ in prompts],
                    ['composition-selection-1', 'composition-selection-2', 'composition-article'])
                self.assertEqual({call.args[0] for call in writing.client_factory.call_args_list},
                                 {'cooperative-author'})
                native = self._record_native_result(root, 'cooperative-author', first)
                observed = first.model_dump(mode='json')
                action = writing.continuation(observed, 'cooperative-author')
                self.assertIsNotNone(action)
                self.assertEqual(action['tool_name'], 'export_report')
                prepare = action['arguments']
                self.assertEqual(prepare['candidate_result'], 'records/cooperative-author/native_result.json')
                self.assertFalse(prepare['compile_pdf'])
                self.assertIsNone(prepare.get('delivery'))
                with patch('simple_ar.tools.publication.run_process') as process:
                    wrong_style = publication.call('export_report', {**prepare, 'citation_style': 'template'},
                                                   identity='cooperative-wrong-style')
                self.assertEqual(wrong_style.status, 'error', wrong_style)
                self.assertIn('Publication options differ', wrong_style.error)
                process.assert_not_called()
                owner = ArtifactStore(root / 'tools/writing/cooperative-author')
                self.assertEqual(first.data['publication_stage'], 'records/cooperative-author/document_work.json')
                stage_path = workspace.resolve(first.data['publication_stage'])
                stage = json.loads(stage_path.read_bytes())
                candidate = workspace.resolve(prepare['markdown'])
                archive = owner.resolve('publication_checkpoint/' + next(iter(stage['checkpoint_sha256'])))
                original = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
                original.update({path: path.read_bytes() for path in candidate.parent.rglob('*') if path.is_file()})
                original.update({path: path.read_bytes() for path in (stage_path, candidate,
                    native.resolve('native_result.json'), owner.resolve('request.json'),
                    *owner.resolve('publication_checkpoint').rglob('*.json'))})
                with self.assertRaisesRegex(ValueError, 'different candidate'):
                    writing.preparation_source(str(table), prepare['candidate_result'])
                with self.assertRaisesRegex(ValueError, 'cannot be adopted'):
                    writing.publication_source(prepare['markdown'], None)
                for path in (candidate, table, archive):
                    with self.subTest(changed_handback=path.name), changed_record(path, path.read_bytes() + b' '):
                        self.assertIsNone(writing.continuation(observed, 'cooperative-author'))
                        with self.assertRaises(ValueError):
                            publication.approval_binding(prepare, identity='cooperative-prepared')
                self.assertEqual(client.ask_json.call_count, 3)
                if not shutil.which('pandoc'):
                    self.assertEqual({path: path.read_bytes() for path in original}, original)
                    return  # Handback checks still run without a renderer.

                context = CapabilityContext(ArtifactStore(root / 'attempts/cooperative-prepared'),
                    AttemptManifest(attempt_id='cooperative-prepared', capability='native_tool'))
                self.assertEqual(publication.execute_tool('export_report', context, prepare).status, 'blocked')
                approval = {'binding': publication.approval_binding(prepare, identity='cooperative-prepared')}
                prepared = publication.execute_tool('export_report', context, prepare, approval=approval)
                self.assertEqual(prepared.status, 'ok', prepared)
                self.assertEqual(prepared.data['candidate_preparation'], {
                    'source_result': prepare['candidate_result'], 'adopted': False, 'delivery_authority': False})
                self.assertNotIn('delivery', prepared.data)
                prepared_native = self._record_native_result(root, 'cooperative-prepared', prepared)
                prepared_ref = 'records/cooperative-prepared/native_result.json'
                inspection = writing.prepared_publication(prepared_ref)
                self.assertFalse(inspection['adopted'])
                with self.assertRaisesRegex(ValueError, 'cannot satisfy final publication delivery'):
                    publication.check_report_delivery([prepared.model_dump(mode='json')], inspection['source'],
                        prepared.artifacts, formats=['tex'], manuscript=prepare['markdown'], proposed=prepare)
                resume = writing.consume_outcome(prepared.model_dump(mode='json'), prepared_ref)
                self.assertEqual(resume, {'tool_name': 'compose_document', 'arguments': {'resume_work': prepared_ref}})
                self.assertEqual(writing.consume_outcome(prepared.model_dump(mode='json'),
                    'attempts/cooperative-prepared/native_result.json'), resume)
                view_path = workspace.resolve(prepared.data['project']) / 'publication_view.json'
                for path in (candidate, table, view_path, native.resolve('native_result.json')):
                    with self.subTest(changed_preparation=path.name), changed_record(path, path.read_bytes() + b' '):
                        with self.assertRaises(ValueError):
                            writing.prepared_publication(prepared_ref)
                # A valid prepared view cannot fork a changed live author checkpoint.
                live = owner.resolve('writer/' + next(iter(stage['checkpoint_sha256'])))
                with changed_record(live, live.read_bytes() + b' '):
                    rejected = writing.call(resume['tool_name'], resume['arguments'], identity='cooperative-bad-resume')
                    self.assertEqual(rejected.status, 'error', rejected)
                    self.assertIn('checkpoint already advanced', rejected.error)
                self.assertEqual(client.ask_json.call_count, 3)
                original[prepared_native.resolve('native_result.json')] = prepared_native.resolve('native_result.json').read_bytes()
                regular = client.ask_json.side_effect

                def inspection_answer(system, prompt, **options):
                    if outcome == 'reviewed' or options['label'] != 'article-independent-review':
                        return regular(system, prompt, **options)
                    view = json.loads(prompt)
                    prompts.append((options['label'], view))
                    if outcome == 'stopped':
                        raise LLMError('Inspection transport stopped before a complete response')
                    return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                        'findings': [{'finding_id': 'clarify-scope', 'type': 'missing_limitation',
                            'severity': 'major', 'required_action': 'revise', 'message': 'Clarify the scope.',
                            'draft_quotes': [{'section_id': 'document', 'quote': view['sections'][0]['markdown']}]}]}]}

                client.ask_json.side_effect = inspection_answer
                bound_clients = writing.client_factory.call_count
                completed = writing.call(resume['tool_name'], resume['arguments'], identity='cooperative-reviewed')
                self.assertEqual([call.args[0] for call in writing.client_factory.call_args_list[bound_clients:]],
                                 ['cooperative-reviewed'])
                link_path = root / 'tools/writing/cooperative-reviewed/resume.json'
                historical = json.loads(link_path.read_bytes())
                historical['prepared_result'] = 'attempts/cooperative-prepared/native_result.json'
                with changed_record(link_path, json.dumps(historical).encode('utf-8')):
                    original_link = link_path.read_bytes()
                    self.assertEqual(composition_owner(root=root, workspace=workspace, session_id='task',
                        identity='cooperative-reviewed').root, owner.root)
                    self.assertEqual(writing.call(resume['tool_name'], resume['arguments'],
                        identity='cooperative-reviewed'), completed)
                    self.assertEqual(link_path.read_bytes(), original_link)
                    self.assertEqual(client.ask_json.call_count, 4)
                if outcome != 'reviewed':
                    self.assertEqual(completed.status, 'error' if outcome == 'stopped' else 'ok', completed)
                    if outcome == 'stopped':
                        # Recovery can reconstruct this failure before native
                        # observation persistence, without another inspection.
                        cached = writing.call(resume['tool_name'], resume['arguments'], identity='cooperative-reviewed')
                        self.assertEqual(cached, completed)
                        self.assertEqual(client.ask_json.call_count, 4)
                    physical = self._record_native_result(root, 'cooperative-reviewed', completed)
                    reference = 'records/cooperative-reviewed/native_result.json'
                    stopped = writing.stop_after_review(completed.model_dump(mode='json'), 'cooperative-reviewed')
                    self.assertIsNotNone(stopped)
                    self.assertEqual(stopped['stop']['source_result'], reference)
                    if outcome == 'negative':
                        self.assertEqual(completed.data['editable_markdown'],
                                         'workspace/manuscripts/cooperative-author/candidate.md')
                        self.assertEqual(writing.final_review(completed.data)['review_status'], 'needs_revision')
                    with changed_record(root / 'tools/writing/cooperative-reviewed/result.json',
                                        None if outcome == 'stopped' else
                                        (root / 'tools/writing/cooperative-reviewed/result.json').read_bytes()):
                        self.assertEqual(composition_owner(root=root, workspace=workspace, session_id='task',
                            identity='cooperative-reviewed').root, owner.root)
                        recipe, _, linkage, _ = writing._continued_document_inputs(
                            ComposeRequest(goal=arguments['goal'], prior_result=reference), 'cooperative-next', None,
                            inspect_only=False, allow_standalone=True)
                    self.assertEqual(recipe.prior_result, reference)
                    self.assertEqual(linkage['owner'], 'cooperative-author')
                    self.assertEqual(linkage['native_owner'], 'cooperative-reviewed')
                    self.assertEqual(linkage['result_sha256'], hashlib.sha256(physical.resolve('native_result.json').read_bytes()).hexdigest())
                    self.assertEqual(client.ask_json.call_count, 4)
                    self.assertEqual({path: path.read_bytes() for path in original}, original)
                    if outcome == 'negative':
                        self._assert_prepared_independent_review(root, workspace, writing, completed, inspection, table)
                    continue
                self.assertEqual(completed.status, 'ok', completed)
                self.assertFalse(completed.data.get('unresolved'))
                self.assertEqual(completed.data['editable_markdown'],
                                 'workspace/manuscripts/cooperative-author/manuscript.md')
                self.assertEqual(writing.final_review(completed.data)['review_status'], 'reviewed')
                self.assertEqual([label for label, _ in prompts], ['composition-selection-1',
                    'composition-selection-2', 'composition-article', 'article-independent-review'])
                review_view = prompts[-1][1]
                self.assertEqual(review_view['prepared_publication'], inspection)
                request_path = owner.resolve('writer/review/request.json')
                self.assertEqual(json.loads(json.loads(request_path.read_bytes())['prompt'])['prepared_publication'], inspection)
                self._record_native_result(root, 'cooperative-reviewed', completed)
                final = writing.continuation(completed.model_dump(mode='json'), 'cooperative-reviewed')
                self.assertIsNotNone(final)
                self.assertEqual(final['tool_name'], 'export_report')
                self.assertFalse(final['arguments'].get('candidate_result'))
                self.assertEqual(final['arguments']['delivery']['report'], {
                    'status': 'canonical', 'manuscript': completed.data['editable_markdown']})
                final_context = CapabilityContext(ArtifactStore(root / 'attempts/cooperative-final'),
                    AttemptManifest(attempt_id='cooperative-final', capability='native_tool'))
                stale = workspace.resolve('workspace/document_work/cooperative-author/unrelated/unused.svg', write=True)
                stale.parent.mkdir(parents=True, exist_ok=True)
                stale.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding='utf-8')
                final_binding = publication.approval_binding(final['arguments'], identity='cooperative-final')
                self.assertFalse(any('unused.svg' in key for key in final_binding['inputs']))
                exported = publication.execute_tool(final['tool_name'], final_context, final['arguments'],
                    approval={'binding': final_binding})
                self.assertEqual(exported.status, 'ok', exported)
                self.assertNotIn('candidate_preparation', exported.data)
                canonical = writing.publication_source(completed.data['editable_markdown'], None)
                self.assertEqual(exported.data['report_identity'], canonical)
                self.assertEqual(canonical['prepared_publication'], {key: inspection[key] for key in
                    ('prepared_result', 'prepared_result_sha256', 'view_path', 'view_sha256')})
                final_view = workspace.resolve(exported.data['project']) / 'publication_view.json'
                self.assertEqual(json.loads(final_view.read_bytes()), inspection['view'])
                self.assertIn('delivery', exported.data)
                self.assertEqual(client.ask_json.call_count, 4)
                self.assertEqual({path: path.read_bytes() for path in original}, original)
                self._assert_prepared_independent_review(root, workspace, writing, completed, inspection, table)

    def test_checked_article_keeps_rejected_author_and_review_before_publication(self):
        root, workspace, writing, source, _, _, client, prompts = self._document_work_fixture()
        writing.document_source_contract = 'document_source.v1'
        select = client.ask_json.side_effect
        figure = source / 'article.png'
        figure.write_bytes(b'\x89PNG\r\n\x1a\nfixture image bytes')
        original = 'Recorded result without its execution boundary.\n\n![Overview](outputs/figure/main_figure.png)'
        corrected_path = 'Recorded result without its execution boundary.\n\n![Overview](figures/selected-1.png)'
        accepted_body = 'Recorded result; no new experiment was run.\n\n![Overview](figures/selected-1.png)'
        accepting = False

        def answer(system, prompt, **options):
            label, view = options['label'], json.loads(prompt)
            if label.startswith('composition-selection-'):
                reply = select(system, prompt, **options)
                if label == 'composition-selection-2':
                    reply['figures'] = [{'path': str(figure), 'caption': 'Exact caption.'}]
                return reply
            prompts.append((label, view))
            if label == 'composition-article':
                if 'edit_contract' in view:
                    return {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': view['prior_draft'],
                            'new_text': accepted_body if accepting else corrected_path}]}
                self.assertFalse(accepting, 'The retained author must not be drafted again')
                return {'draft_markdown': original}
            self.assertEqual(label, 'article-independent-review')
            body = view['sections'][0]['markdown']
            return {'section_reviews': [{'section_id': 'document',
                'verdict': 'pass' if body == accepted_body else 'revise_required',
                'findings': [] if body == accepted_body else [{'finding_id': 'execution-boundary',
                    'type': 'missing_limitation', 'severity': 'major', 'required_action': 'revise',
                    'message': 'State the boundary between saved evidence and new execution.',
                    'draft_quotes': [{'section_id': 'document', 'quote': body}]}]}]}

        client.ask_json.side_effect = answer
        args = {'goal': 'Explain this saved result and its execution scope', 'material_roots': [str(source)]}
        with patch('simple_ar.report.capability.run_report_capability',
                side_effect=AssertionError('Rejected work cannot require publication')):
            first = writing.call('compose_document', args, identity='article-negative')
        self.assertEqual(first.status, 'ok', first)
        self.assertIn(corrected_path, workspace.resolve(first.data['editable_markdown']).read_text())
        self.assertEqual([row['finding_id'] for row in first.data['unresolved']], ['execution-boundary'])
        self.assertTrue(writing.can_return_gap(first.model_dump(mode='json'), 'article-negative'))
        self.assertIsNone(writing.continuation(first.model_dump(mode='json'), 'article-negative'))
        self._record_native_result(root, 'article-negative', first)
        owner = ArtifactStore(root / 'tools/writing/article-negative')
        saved = {path: path.read_bytes() for path in owner.root.rglob('*') if path.is_file()}
        from pydantic import ValidationError
        from simple_ar.tools.publication import ExportRequest, PublicationToolGateway

        markdown = first.data['editable_markdown']
        reference = 'records/article-negative/native_result.json'
        calls = client.ask_json.call_count
        identity = writing.preparation_source(markdown, reference)
        self.assertEqual(identity['kind'], 'publication_candidate')
        self.assertFalse(identity['adopted'])
        self.assertEqual(identity['source_result'], reference)
        native_path = root / 'attempts/article-negative/native_result.json'
        self.assertEqual(identity['source_result_sha256'], hashlib.sha256(native_path.read_bytes()).hexdigest())
        with self.assertRaisesRegex(ValidationError, 'no final delivery authority'):
            ExportRequest.model_validate({'markdown': markdown, 'candidate_result': reference,
                'delivery': {'summary': 'Deliver candidate'}})
        with self.assertRaisesRegex(ValueError, 'unresolved writing'):
            writing.publication_source(markdown, None)
        publication = PublicationToolGateway(root, workspace, workspace.ledger,
            candidate_source=writing.preparation_source, owned_execution=True)
        with self.assertRaisesRegex(ValueError, 'cannot satisfy final publication delivery'):
            publication.check_report_delivery([], identity, [], formats=['tex'], manuscript=markdown,
                proposed={'markdown': markdown, 'candidate_result': reference})
        self.assertEqual(client.ask_json.call_count, calls)
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)
        accepting = True
        start = len(prompts)
        second = writing.call('compose_document', {'goal': 'Correct the retained execution scope and deliver',
            'prior_result': 'records/article-negative/native_result.json', 'finish': True}, identity='article-fixed')
        self.assertEqual(second.status, 'ok', second)
        self.assertEqual([label for label, _ in prompts[start:]], ['composition-article', 'article-independent-review'])
        self.assertIn(accepted_body, workspace.resolve(second.data['editable_markdown']).read_text())
        self.assertTrue(second.data['editable_markdown'].endswith('/manuscript.md'))
        self.assertIsNotNone(writing.continuation(second.model_dump(mode='json'), 'article-fixed'))
        self.assertEqual({path: path.read_bytes() for path in saved}, saved)

    def test_document_work_defaults_to_authorized_sources_and_keeps_old_contracts(self):
        root, workspace, writing, _, _, _, client, prompts = self._document_work_fixture()
        result = writing.call('compose_document', {'goal': 'Explain the saved evidence'}, identity='document-defaults')
        self.assertEqual(result.status, 'ok', result)
        selection = ArtifactStore(root / 'tools/writing/document-defaults').read_json('selection-input.json')
        self.assertEqual(selection['request']['professional_review']['max_revision_rounds'], 1)
        self.assertEqual(selection['inventories'][0]['project_path'], str(workspace.mounts['source0']))
        self.assertFalse(any('attempts' in row['project_path'] for row in selection['inventories']))
        self.assertNotIn('finish', selection['request'])
        writing.document_work = False
        self.assertFalse(writing.finishes_request({'finish': True}))
        rejected = writing.call('compose_document', {'goal': 'Historical request still needs title',
            'material_roots': ['source0']}, identity='historical-title')
        self.assertEqual(rejected.status, 'error')
        self.assertIn('title', rejected.error)
        self.assertNotIn('finish', ComposeRequest(goal='Old shape', title='Existing title').model_dump())
        # The professional owner's omitted-title request must round-trip through
        # its real frozen input, rather than serialize an invalid default value.
        pending = ComposeRequest(goal='Let the material owner select the title')
        self.assertEqual(ComposeRequest.model_validate(pending.model_dump()), pending)

    def test_document_work_finish_delivers_markdown_but_requires_actual_review(self):
        root, workspace, writing, source, script, _, client, prompts = self._document_work_fixture()
        arguments = {'goal': 'Deliver the editable report',
            'material_roots': [str(source)], 'finish': True}
        result = writing.call('compose_document', arguments, identity='document-no-publication')
        self.assertEqual(result.status, 'ok', result)
        observed = result.model_dump(mode='json')
        action = writing.continuation(observed, 'document-no-publication')
        self.assertEqual(action['tool_name'], 'deliver_result')
        self.assertEqual(set(action['arguments']['artifacts']), set(result.artifacts))
        self.assertEqual(action['arguments']['report']['manuscript'], result.data['editable_markdown'])
        calls = client.ask_json.call_count
        self.assertEqual(writing.call('compose_document', arguments,
            identity='document-no-publication').model_dump(mode='json'), observed)
        self.assertEqual(client.ask_json.call_count, calls)
        script.write_text('Changed original evidence', encoding='utf-8')
        self.assertIsNone(writing.continuation(observed, 'document-no-publication'))

        root, workspace, writing, source, _, _, client, prompts = self._document_work_fixture()
        writing.report_inputs = lambda: {'obligation': {'formats': ['markdown', 'pdf']}}
        rejected = writing.call('compose_document', {'goal': 'Deliver the requested PDF',
            'material_roots': [str(source)], 'finish': True}, identity='document-no-publication')
        self.assertEqual(rejected.status, 'error')
        self.assertIn('publication', rejected.error)
        client.ask_json.assert_not_called()
        writing.report_inputs = None
        real_answer = client.ask_json.side_effect

        # An unavailable independent response must never gain finish authority,
        # even though Writer and copied attachment bytes are already retained.
        def failed_review(system, prompt, **options):
            if options['label'] == 'article-independent-review':
                raise ValueError('Independent response unavailable')
            return real_answer(system, prompt, **options)

        client.ask_json.side_effect = failed_review
        failed = writing.call('compose_document', {'goal': 'Explain and publish',
            'material_roots': [str(source)], 'publication': {'compile_pdf': False}, 'finish': True},
            identity='document-no-review')
        self.assertEqual(failed.status, 'error')
        self.assertIsNone(writing.continuation(failed.model_dump(mode='json'), 'document-no-review'))

    def test_document_material_copies_enforce_scope_size_and_retained_bytes(self):
        root, workspace, writing, source, script, table, _, _ = self._document_work_fixture()
        proof = writing._document_material_copies([str(script), str(table)], identity='copy-unit')
        self.assertEqual(writing._document_material_copies([str(script), str(table)],
            identity='copy-unit', retained=proof), proof)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            writing._document_material_copies([str(table), str(table)], identity='copy-duplicate')
        with self.assertRaises(ValueError):
            writing._document_material_copies([str(root / 'ledger.json')], identity='copy-private')
        oversized = source / 'large.dat'
        with oversized.open('wb') as handle:
            handle.truncate(17 * 1024 * 1024)
        with self.assertRaisesRegex(ValueError, '16 MiB'):
            writing._document_material_copies([str(oversized)], identity='copy-oversized')

    def test_document_work_does_not_upgrade_an_interrupted_historical_selection(self):
        root, workspace, writing, source, _, _, client, _ = self._document_work_fixture()
        writing.document_work = False
        arguments = {'goal': 'Keep the original selection contract', 'title': 'Explicit old title',
            'material_roots': [str(source)]}
        client.ask_json.side_effect = ValueError('Interrupted original selection')
        original = writing.call('compose_document', arguments, identity='old-selection')
        self.assertEqual(original.status, 'error')
        owner = ArtifactStore(root / 'tools/writing/old-selection')
        # Model an actual pre-migration receipt, rather than a fresh selection
        # which now freezes the ComposeFigure response schema.
        historical = owner.read_json('selection-input.json')
        historical.pop('figure_selection_contract')
        owner.write_json('selection-input.json', historical)
        original_bytes = owner.resolve('selection-input.json').read_bytes()
        self.assertNotIn('document_work', owner.read_json('selection-input.json'))
        writing.document_work = True
        client.ask_json.reset_mock()
        recovered = writing.call('compose_document', arguments, identity='old-selection')
        self.assertEqual(recovered.status, 'error')
        self.assertIn('Interrupted material selection', recovered.error)
        client.ask_json.assert_not_called()
        self.assertEqual(owner.resolve('selection-input.json').read_bytes(), original_bytes)
        # Both old selection schemas must still accept strings and reject
        # object responses without rewriting the input or contacting a model.
        figure = source / 'legacy.svg'
        figure.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        for document_work in (False, True):
            with self.subTest(document_work=document_work):
                request = ComposeRequest.model_validate(arguments)
                retained_owner = ArtifactStore(root / 'tools/writing' / f'legacy-figures-{document_work}')
                response = {'materials': [str(source / 'results/record.txt')], 'figures': [str(figure)]}
                if document_work:
                    response['title'] = 'Historical title'
                client.ask_json.side_effect = lambda *args, **kwargs: (
                    {'read_windows': [{'path': str(source / 'results/record.txt')}]}
                    if kwargs['label'] == 'composition-selection-1' else response)
                selected, coverage = writing._select_materials(request, retained_owner, 'legacy-figures',
                    client, '', None, document_work=document_work)
                self.assertEqual(selected.figures, [str(figure)])
                self.assertEqual(coverage['selected_figures'], [str(figure)])
                old_input = retained_owner.read_json('selection-input.json')
                old_input.pop('figure_selection_contract')
                retained_owner.write_json('selection-input.json', old_input)
                frozen = retained_owner.resolve('selection-input.json').read_bytes()
                client.ask_json.reset_mock()
                selected, _ = writing._select_materials(request, retained_owner, 'legacy-figures',
                    client, '', None, document_work=document_work)
                self.assertEqual(selected.figures, [str(figure)])
                old_response = retained_owner.read_json('selection-response.json')
                retained_owner.write_json('selection-response.json', {**old_response,
                    'figures': [{'path': str(figure), 'placement': 'attachment'}]})
                with self.assertRaises(ValueError):
                    writing._select_materials(request, retained_owner, 'legacy-figures', client, '', None,
                        document_work=document_work)
                self.assertEqual(retained_owner.resolve('selection-input.json').read_bytes(), frozen)
                client.ask_json.assert_not_called()

    def _declared_document_fixture(self):
        root, workspace, writing, source, _, table, client, prompts = self._document_work_fixture()
        analysis = workspace.workspace / 'analysis'
        sizes = {'code/report.py': 24740, 'record.json': 25470, 'summary.md': 15519,
            'table.csv': 1000, 'inputs/data.txt': 2400, 'inputs/scope.txt': 1200}
        for name, size in sizes.items():
            path = analysis / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name == 'record.json':
                raw = b'{"record":"' + b'x' * (size - len(b'{"record":""}')) + b'"}'
            elif name == 'code/report.py':
                head = b'from pathlib import Path\nprint((Path(__file__).parent / "../inputs/data.txt").read_text())\n#'
                raw = head + b'x' * (size - len(head))
            else:
                raw = b'x' * size
            path.write_bytes(raw)
        figure = analysis / 'figure.svg'
        figure.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>')
        explicit = ['workspace/analysis/' + name for name in sizes]
        # Same resolved explicit file under two legal locator spellings.
        declared = [*explicit, str(analysis / 'code/report.py')]
        original_answer = client.ask_json.side_effect

        def answer(system, prompt, **options):
            view = json.loads(prompt)
            if options['label'].startswith('composition-selection-'):
                self.assertEqual({row['absolute_path'] for row in view['declared_delivery_artifacts']},
                    {str(workspace.resolve(path)) for path in explicit})
                self.assertEqual(len(view['declared_delivery_artifacts']), 6)
                for row in view['declared_delivery_artifacts']:
                    self.assertEqual(row['sha256'], hashlib.sha256(workspace.resolve(row['path']).read_bytes()).hexdigest())
                self.assertEqual(view['explicit_figures'][0]['absolute_path'], str(figure))
                self.assertEqual(view['explicit_figures'][0]['sha256'], hashlib.sha256(figure.read_bytes()).hexdigest())
            if options['label'].startswith('composition-selection-') and options['label'] != 'composition-selection-1':
                prompts.append((options['label'], view))
                return {'title': 'Recorded value and scope', 'materials': [str(table)],
                    'delivery_artifacts': [str(table)]}
            reply = original_answer(system, prompt, **options)
            if options['label'] == 'composition-article' and 'draft_markdown' in reply:
                reply['draft_markdown'] += '\n\n![Supplied report figure](figures/selected-1.svg)'
            return reply

        client.ask_json.side_effect = answer
        arguments = {'goal': 'Explain the recorded result with the complete necessary inputs',
            'material_roots': [str(source)], 'delivery_artifacts': declared,
            'figures': [str(figure)], 'publication': {'compile_pdf': False}, 'finish': True}
        return root, workspace, writing, table, client, prompts, arguments, explicit

    def test_document_explicit_inputs_and_discoveries_share_one_bounded_copy_review(self):
        root, workspace, writing, table, client, prompts, arguments, explicit = self._declared_document_fixture()
        result = writing.call('compose_document', arguments, identity='offered-document')
        self.assertEqual(result.status, 'ok', result)
        self.assertEqual(result.data['delivery_review']['status'], 'reviewed')
        owner = ArtifactStore(root / 'tools/writing/offered-document')
        binding = owner.read_json('request.json')
        self.assertEqual(binding['document_work']['contract'], 'document_work.v2')
        self.assertEqual(binding['request']['delivery_artifacts'], [*explicit, str(table)])
        snapshot = binding['delivery_review']['artifacts']
        byte_count = sum(len(row.get('text', '').encode('utf-8')) for row in snapshot)
        self.assertGreater(byte_count, 70000)
        self.assertLessEqual(byte_count, 96000)
        self.assertGreater(byte_count * 2, 96000, 'The old double-text package must not fit this bound')
        self.assertEqual(len(snapshot), 8)  # six explicit files, original data, one figure
        self.assertTrue(all(row['coverage'] in {'complete', 'visual_uninspected'} for row in snapshot))
        self.assertEqual(sum(len(row['copies']) for row in snapshot), 8)
        self.assertTrue(all(set(copy) == {'path', 'sha256'} for row in snapshot for copy in row['copies']))
        for label, view in prompts:
            if label == 'composition-article':
                supplied = view['delivery_review']['artifacts']
                self.assertEqual(supplied, [{key: value for key, value in row.items() if key != 'text'}
                    for row in snapshot])
                self.assertIn('independent inspector', view['attachment_text_scope'])
            elif label == 'article-independent-review':
                supplied = restore_delivery_producers(view)
                self.assertEqual(supplied, snapshot)
            else:
                continue
        for row in result.data['delivery_review']['artifacts']:
            self.assertIn('copies', row)
            for copy in row['copies']:
                self.assertEqual(workspace.resolve(copy['path']).read_bytes(), workspace.resolve(row['path']).read_bytes())
        script = workspace.resolve('workspace/manuscripts/offered-document/materials/workspace/analysis/code/report.py')
        self.assertEqual((script.parent / '../inputs/data.txt').read_bytes(),
            workspace.resolve('workspace/analysis/inputs/data.txt').read_bytes())
        self.assertIsNotNone(writing.continuation(result.model_dump(mode='json'), 'offered-document'))
        identity = writing.publication_source(result.data['editable_markdown'], None,
            observation=result.model_dump(mode='json'))
        writing.check_delivery_set(identity, result.artifacts, set())
        calls = client.ask_json.call_count
        self.assertEqual(writing.call('compose_document', arguments,
            identity='offered-document').model_dump(mode='json'), result.model_dump(mode='json'))
        self.assertEqual(client.ask_json.call_count, calls)
        for fault in ('missing-alias', 'wrong-source', 'producer-on-copy'):
            altered = json.loads(json.dumps(result.data))
            subject = next(row for row in altered['delivery_review']['artifacts'] if row.get('copies'))
            if fault == 'missing-alias':
                subject['copies'] = []
            elif fault == 'wrong-source':
                subject['path'] = str(table)
            else:
                subject['copies'][0]['producer'] = {'invocation_id': 'invented'}
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                writing._check_document_copy_review(altered)

    def test_document_discoveries_cannot_expand_explicit_scope_or_replace_declared_versions(self):
        for fault in ('outside-discovery', 'changed-declared'):
            with self.subTest(fault=fault):
                root, workspace, writing, _, client, prompts, arguments, explicit = self._declared_document_fixture()
                original_answer = client.ask_json.side_effect

                def answer(system, prompt, **options):
                    response = original_answer(system, prompt, **options)
                    if options['label'] == 'composition-selection-2':
                        if fault == 'outside-discovery':
                            response['delivery_artifacts'] = [explicit[0]]
                        else:
                            path = workspace.resolve(explicit[0])
                            path.write_bytes(path.read_bytes() + b'changed after preflight')
                    return response

                client.ask_json.side_effect = answer
                result = writing.call('compose_document', arguments, identity='declared-scope-' + fault)
                self.assertEqual(result.status, 'error', result)
                self.assertIn('inventory' if fault == 'outside-discovery' else 'changed', result.error)
                self.assertFalse(any(label == 'composition-article' for label, _ in prompts))

    def test_document_equal_hashes_do_not_merge_different_original_subjects(self):
        root, workspace, writing, source, script, _, _, _ = self._document_work_fixture()
        offered = workspace.workspace / 'offered.py'
        offered.write_bytes(script.read_bytes())
        result = writing.call('compose_document', {'goal': 'Explain both actual source locations',
            'material_roots': [str(source)], 'delivery_artifacts': [str(offered)]}, identity='distinct-origins')
        self.assertEqual(result.status, 'ok', result)
        binding = ArtifactStore(root / 'tools/writing/distinct-origins').read_json('request.json')
        subjects = {row['path']: row for row in binding['delivery_review']['artifacts']}
        first, second = subjects[str(script)], subjects[str(offered)]
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertNotEqual(first['copies'], second['copies'])
        self.assertEqual(len(binding['document_work']['materials']), 3)

    def test_document_review_rejects_changed_actual_copy_bytes_even_when_reviewer_passes(self):
        root, workspace, writing, source, _, _, client, _ = self._document_work_fixture()
        original_answer = client.ask_json.side_effect

        def answer(system, prompt, **options):
            response = original_answer(system, prompt, **options)
            if options['label'] == 'article-independent-review':
                copy = json.loads(prompt)['delivery_artifacts'][0]['copies'][0]
                path = workspace.resolve(copy['path'])
                path.write_bytes(path.read_bytes() + b'changed during review')
            return response

        client.ask_json.side_effect = answer
        result = writing.call('compose_document', {'goal': 'Explain the recorded value',
            'material_roots': [str(source)], 'publication': {'compile_pdf': False}, 'finish': True},
            identity='changed-copy-review')
        self.assertEqual(result.status, 'error', result)
        self.assertIn('changed before canonical receipt', result.error)
        self.assertIsNone(writing.continuation(result.model_dump(mode='json'), 'changed-copy-review'))

    def test_document_v1_receipts_keep_double_subject_semantics_on_recovery(self):
        root, workspace, writing, source, _, _, client, _ = self._document_work_fixture()
        arguments = {'goal': 'Preserve the historical unit', 'material_roots': [str(source)],
            'publication': {'compile_pdf': False}, 'finish': True}
        with patch('simple_ar.tools.writing._DOCUMENT_WORK_CONTRACT', 'document_work.v1'):
            result = writing.call('compose_document', arguments, identity='document-v1')
        self.assertEqual(result.status, 'ok', result)
        owner = ArtifactStore(root / 'tools/writing/document-v1')
        frozen = {path: path.read_bytes() for path in owner.root.rglob('*') if path.is_file()}
        snapshot = owner.read_json('request.json')['delivery_review']['artifacts']
        self.assertEqual(len(snapshot), 4)
        self.assertTrue(all('copies' not in row for row in snapshot))
        self.assertEqual(owner.read_json('selection-input.json')['document_work'], 'document_work.v1')
        client.ask_json.reset_mock()
        replay = writing.call('compose_document', arguments, identity='document-v1')
        self.assertEqual(replay.model_dump(mode='json'), result.model_dump(mode='json'))
        client.ask_json.assert_not_called()
        self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)

    def test_selection_reserves_writer_joint_review_and_closing_floor_before_provider(self):
        from simple_ar.integrations.llm import LLMClient, LLMSettings
        for mode, failure_kind in (('exhausted', 'resource_exhausted'),
                ('unknown', 'accounting_unresolved')):
            with self.subTest(mode=mode):
                root, workspace, writing = self._real_workspace()
                ledger = workspace.ledger
                # Exhausted: three requests remain, all needed downstream.
                # Unknown: ample nominal allowance must not permit a send.
                ledger.limits['llm_requests'] = 4 if mode == 'exhausted' else 100
                ledger.reserve('original-call', {'llm_requests': 1},
                    session_id='task', attempt_id='original', purpose='original-work')
                if mode == 'unknown':
                    ledger.mark_unknown('original-call', reason='Provider usage awaits reconciliation')
                else:
                    ledger.settle('original-call', {'llm_requests': 1}, actual_source='provider')
                before, ledger_bytes = ledger.to_dict(), ledger.storage_path.read_bytes()
                material = workspace.workspace / 'measurements.txt'
                material.write_text('Recorded measurement: 2.5\n', encoding='utf-8')
                client = LLMClient(LLMSettings(model='fixture-model', api_key='fixture-only',
                    api_mode='chat', max_output_tokens=256, retry_attempts=1),
                    budget_ledger=ledger, budget_session_id='task', budget_attempt_id='selection-stop')
                writing.ledger = ledger
                writing.joint_delivery = True
                writing.client_factory = Mock(return_value=client)
                ask_json = client.ask_json
                closing_floor, known_calls = 1, 2  # article Writer + joint delivery reviewer

                def selection_request(*args, **kwargs):
                    self.assertGreaterEqual(ledger._remaining_floor('llm_requests'),
                        closing_floor + known_calls)
                    if mode == 'exhausted':
                        self.assertEqual(json.loads(args[1])['decisions_remaining'], 0)
                    return ask_json(*args, **kwargs)

                # Keep real client reservation/error chaining; mock only the
                # provider transport, which must never be reached by this call.
                with patch('simple_ar.integrations.llm._call_provider') as provider, \
                        patch.object(client, 'ask_json', side_effect=selection_request) as selection, \
                        ledger.protect_remaining({'llm_requests': closing_floor}):
                    result = writing.call('compose_document', {'goal': 'Explain the recorded measurement',
                        'title': 'Recorded result', 'material_roots': [str(material)]},
                        identity='selection-stop')
                    selection.assert_called_once()
                    provider.assert_not_called()
                    self.assertEqual(ledger._remaining_floor('llm_requests'), closing_floor)
                self.assertEqual(ledger._remaining_floor('llm_requests'), 0)
                self.assertEqual((result.status, result.data['failure_kind']), ('error', failure_kind), result)
                self.assertTrue(result.data['incomplete'])
                if mode == 'unknown':
                    self.assertIn('Reconcile', result.data['next_action'])
                    self.assertNotIn('append a continuation request', result.data['next_action'])
                    self.assertEqual(ledger.entries[0].status, 'unknown')
                else:
                    self.assertIn('Authorize', result.data['next_action'])
                    self.assertIn('append a continuation request', result.data['next_action'])
                self.assertEqual(ledger.to_dict(), before)
                self.assertEqual(ledger.storage_path.read_bytes(), ledger_bytes)

    def _journal_material(self, root, workspace, raw, **window):
        sources = root / 'originals'
        sources.mkdir(exist_ok=True)
        workspace.mounts['source0'] = sources
        source = sources / 'values.txt'
        source.write_bytes(raw)
        request = {'path': 'source0/values.txt', 'full_digest': True, **window}
        result = workspace.call('read_file', request, identity='parent-read')
        self.assertEqual(result.status, 'ok', result)
        store = ArtifactStore(root / 'attempts' / 'parent-read')
        store.write_json('tool_result.json', result.model_dump(mode='json'))
        reference = {'record': 'records/parent-read/tool_result.json', 'request': request,
            'receipt_sha256': hashlib.sha256(store.resolve('tool_result.json').read_bytes()).hexdigest()}
        return source, result, reference

    def test_journal_content_reuses_relocated_windows_without_moving_source_authority(self):
        from simple_ar.tools.materials import (MaterialSelection, inspect_materials,
            material_inventories, material_observation_seed, selected_material_windows,
            validate_material_observation)
        for window in ({'offset_bytes': 7, 'max_bytes': 6},
                {'start_line': 2, 'max_lines': 1, 'max_bytes': 40}):
            with self.subTest(window=window):
                root, workspace, _ = self._real_workspace()
                raw = 'header\n测量\nunseen tail\n'.encode('utf-8')
                source, result, reference = self._journal_material(root, workspace, raw, **window)
                # Source-side role/provenance labels are not target facts.
                result.data.update(evidence_role='draft_claims_not_evidence',
                    producer={'invocation_id': 'old-producer'}, execution='old-authority')
                journal = ArtifactStore(root / 'attempts' / 'parent-read')
                journal.write_json('tool_result.json', result.model_dump(mode='json'))
                reference['receipt_sha256'] = hashlib.sha256(journal.resolve('tool_result.json').read_bytes()).hexdigest()
                target = workspace.workspace / 'project' / 'renamed.csv'
                target.parent.mkdir()
                target.write_bytes(raw)
                # The target is its own byte owner even when the historical
                # source is subsequently edited. No copy command is inspected.
                source.write_bytes(b'changed original')
                inventory = material_inventories(workspace, [str(target.parent)], identity='selection')
                before = workspace.ledger.to_dict()
                seed = material_observation_seed(workspace=workspace, inventories=inventory,
                    provider=lambda roots: [reference], roots=[str(target.parent)], context={})
                self.assertEqual(len(seed['observations']), 1)
                row, = seed['observations']
                self.assertEqual(row['data']['text'], result.data['text'])
                self.assertNotIn('unseen', row['data']['text'])
                self.assertEqual(row['request']['path'], str(target))
                self.assertEqual(row['data']['absolute_path'], str(target))
                self.assertEqual(row['content_reuse']['source'], reference)
                self.assertEqual(row['content_reuse']['target']['sha256'], result.data['sha256'])
                self.assertEqual(row['content_reuse']['target']['offset_bytes'], 7)
                for key in ('producer', 'evidence_role', 'execution', 'record'):
                    self.assertNotIn(key, row)
                    self.assertNotIn(key, row['data'])
                self.assertEqual(validate_material_observation(workspace, row, identity='resume'), row)
                self.assertEqual(workspace.ledger.to_dict(), before)
                owner = ArtifactStore(root / 'selection')
                client = Mock()
                client.ask_json.return_value = {'materials': [row['request']]}
                response = inspect_materials(workspace=workspace, context={}, instructions='Inspect',
                    inventories=inventory, owner=owner, identity='selection', client=client,
                    response_model=MaterialSelection, label='selection', final_fields=('materials',), seed=seed)
                self.assertEqual(client.ask_json.call_count, 1)
                selected, = selected_material_windows(workspace, MaterialSelection.model_validate(response),
                    owner.read_json('selection-observations.json'), identity='resume')
                self.assertEqual(selected, row)
                with self.assertRaisesRegex(ValueError, 'not inspected'):
                    selected_material_windows(workspace, MaterialSelection(materials=[
                        FileWindow(path=str(target), max_bytes=len(raw))]), [row], identity='tail')
                target.write_bytes(raw.replace(b'unseen', b'UNSEEN'))
                with self.assertRaises(ValueError):
                    validate_material_observation(workspace, row, identity='changed-target')
                target.write_bytes(raw)
                ArtifactStore(root / 'attempts' / 'parent-read').write_json('tool_result.json', {})
                with self.assertRaisesRegex(ValueError, 'receipt changed'):
                    validate_material_observation(workspace, row, identity='changed-receipt')

    def test_journal_content_rejects_different_lossy_unhashed_and_excluded_inputs(self):
        from simple_ar.tools.materials import material_inventories, material_observation_seed
        for mode in ('different-bytes', 'invalid-utf8', 'split-utf8', 'no-full-hash',
                'failed-read', 'changed-receipt', 'excluded-source', 'excluded-target',
                'not-in-inventory', 'outside-mount'):
            with self.subTest(mode=mode):
                root, workspace, _ = self._real_workspace()
                raw = b'head\nvalue=123\ntail\n'
                window = {'offset_bytes': 5, 'max_bytes': 9}
                if mode == 'invalid-utf8':
                    raw = b'head\nvalue=\xff23\ntail\n'
                elif mode == 'split-utf8':
                    raw = 'head\n测量\ntail\n'.encode('utf-8')
                    window = {'offset_bytes': 6, 'max_bytes': 4}
                source, result, reference = self._journal_material(root, workspace, raw, **window)
                target = workspace.workspace / 'copy.txt'
                target.write_bytes(raw if mode != 'different-bytes' else raw.replace(b'tail', b'TAIL'))
                if mode in ('no-full-hash', 'failed-read'):
                    payload = result.model_dump(mode='json')
                    if mode == 'no-full-hash':
                        payload['data']['sha256'] = None
                    else:
                        payload.update(status='error', error='Read failed')
                    record = ArtifactStore(root / 'attempts' / 'parent-read')
                    record.write_json('tool_result.json', payload)
                    reference['receipt_sha256'] = hashlib.sha256(record.resolve('tool_result.json').read_bytes()).hexdigest()
                elif mode == 'changed-receipt':
                    reference['receipt_sha256'] = '0' * 64
                inventory = material_inventories(workspace, [str(target)], identity='selection')
                if mode == 'not-in-inventory':
                    inventory[0]['candidate_files'] = {}
                elif mode == 'outside-mount':
                    inaccessible = root / 'private.txt'
                    inaccessible.write_bytes(raw)
                    inventory[0]['candidate_files'] = {str(inaccessible): len(raw)}
                excluded = ({str(source)} if mode == 'excluded-source' else
                    {str(target)} if mode == 'excluded-target' else set())
                seed = material_observation_seed(workspace=workspace, inventories=inventory,
                    provider=lambda roots: [reference], roots=[str(target)], context={}, excluded_paths=excluded)
                self.assertEqual(seed['observations'], [])

    def test_journal_content_preserves_same_path_shape_and_bounds_validation_and_supply(self):
        from simple_ar.tools.materials import (material_inventories, material_observation_seed,
            validate_material_observation)
        root, workspace, _ = self._real_workspace()
        source, _, reference = self._journal_material(root, workspace, b'value\n', max_bytes=6)
        inventory = material_inventories(workspace, [str(source)], identity='same-path')
        seed = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [reference], roots=[str(source)], context={})
        self.assertEqual(seed['observations'], [validate_material_observation(workspace, reference, identity='old-shape')])
        self.assertNotIn('content_reuse_contract', seed['coverage'])
        # A bounded inventory can contain many byte-identical targets; neither
        # validation nor supplied content may expand without a finite ceiling.
        targets = workspace.workspace / 'many'
        targets.mkdir()
        for index in range(130):
            (targets / f'{index:03}.txt').write_bytes(b'value\n')
        inventory = material_inventories(workspace, [str(targets)], identity='bounded')
        seed = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [reference], roots=[str(targets)], context={})
        self.assertLessEqual(len(seed['observations']), 128)
        self.assertEqual(seed['coverage']['content_validation']['probes'], 128)
        self.assertTrue(any(row['reason'] == 'content_validation_capacity' for row in seed['coverage']['not_admitted']))
        with patch('simple_ar.tools.materials._CONTENT_REUSE_MAX_HASH_BYTES', 18):
            bounded = material_observation_seed(workspace=workspace, inventories=inventory,
                provider=lambda roots: [reference], roots=[str(targets)], context={})
        self.assertEqual(bounded['coverage']['content_validation']['hash_bytes_upper_bound'], 18)
        self.assertEqual(len(bounded['observations']), 3)
        source, _, reference = self._journal_material(root, workspace, b'x' * 64000, max_bytes=64000)
        for path in targets.iterdir():
            path.write_bytes(b'x' * 64000)
        seed = material_observation_seed(workspace=workspace,
            inventories=material_inventories(workspace, [str(targets)], identity='text-capacity'),
            provider=lambda roots: [reference], roots=[str(targets)], context={})
        self.assertEqual(len(seed['observations']), 1)
        self.assertEqual(seed['coverage']['content_validation']['probes'], 1)
        self.assertTrue(any(row['reason'] == 'observation_capacity' for row in seed['coverage']['not_admitted']))

    def test_journal_reference_cross_mount_lookup_is_explicit_and_does_not_expand_inventory(self):
        from simple_ar.tools.materials import journal_material_references
        root, workspace, _ = self._real_workspace()
        _, _, reference = self._journal_material(root, workspace, b'values\n', max_bytes=7)
        store = ArtifactStore(root)
        store.write_json('reply.json', {'tool_calls': [{'id': 'read', 'function': {
            'name': 'read_file', 'arguments': json.dumps(reference['request'])}}]})
        turns = [{'reply': 'reply.json', 'tools': [{'id': 'read',
            'result': 'attempts/parent-read/tool_result.json'}]}]
        self.assertEqual(journal_material_references(workspace, store, turns, ['workspace']), [])
        self.assertEqual(journal_material_references(workspace, store, turns, ['workspace'],
            include_cross_path=True), [reference])

    def test_journal_content_keeps_focus_input_receipt_identity(self):
        from simple_ar.tools.materials import material_inventories, material_observation_seed
        root, workspace, _ = self._real_workspace()
        _, result, reference = self._journal_material(root, workspace, b'values\n', max_bytes=7)
        store = ArtifactStore(root / 'attempts' / 'parent-read')
        store.write_json('tool_result.json', ToolResult(tool_name='focus_work', data={'inputs': [
            {'request': reference['request'], 'data': result.data}]}).model_dump(mode='json'))
        reference.update(input_index=0, receipt_sha256=hashlib.sha256(store.resolve('tool_result.json').read_bytes()).hexdigest())
        target = workspace.workspace / 'copy.csv'
        target.write_bytes(b'values\n')
        inventory = material_inventories(workspace, [str(target)], identity='focus-copy')
        seed = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [reference], roots=[str(target)], context={})
        row, = seed['observations']
        self.assertEqual(row['content_reuse']['source']['input_index'], 0)
        invalid = {**reference, 'input_index': 1}
        rejected = material_observation_seed(workspace=workspace, inventories=inventory,
            provider=lambda roots: [invalid], roots=[str(target)], context={})
        self.assertEqual(rejected['observations'], [])

    def test_producer_reuse_preserves_partial_samehash_and_requires_complete_changed_bytes(self):
        root, workspace, writing = self._real_workspace()
        old, new = workspace.workspace / 'old', workspace.workspace / 'new'
        old.mkdir()
        new.mkdir()
        # Only lineage resolution is stubbed: all file/window reads, bounds,
        # identities and returned reuse recipes below use the real workspace.
        execution = ArtifactStore(root / 'attempts' / 'repair')
        execution.write_json('candidate_execution.json', {'fixture': 'lineage supplied by integration test'})
        execution.write_json('native_result.json', {'fixture': 'not an accepted execution receipt'})
        request = ComposeRequest(goal='Reuse the actual inspected records', title='Recorded result',
            prior_draft='workspace/manuscripts/prior/manuscript.md', reuse_prior_inputs=True,
            producer_repair='records/repair/candidate_execution.json')
        for mode, maximum in (('samehash', 5), ('changed-full', 100), ('changed-too-large', 5),
                ('whole-growth', 100), ('whole-over-reader-bound', 100)):
            with self.subTest(mode=mode):
                original = b'header\nvalue=1\ntail=old\n'
                replacement = original if mode == 'samehash' else original.replace(b'tail=old', b'tail=NEW-EVIDENCE')
                if mode == 'whole-over-reader-bound':
                    replacement = b'x' * 64001
                source, target = old / 'values.csv', new / 'values.csv'
                source.write_bytes(original)
                target.write_bytes(replacement)
                whole = mode.startswith('whole-')
                window = FileWindow(path=str(source), offset_bytes=0 if whole else 7,
                    max_bytes=len(original) if whole else maximum, full_digest=True)
                observed = workspace.call('read_file', window.model_dump(exclude_unset=True), identity='original-window')
                self.assertEqual(observed.status, 'ok', observed)
                data = observed.data
                selection = {key: data[key] for key in ('size_bytes', 'next_offset_bytes', 'has_more', 'start_line') if key in data}
                selection.update(offset_bytes=0 if whole else 7, window_sha256=hashlib.sha256(data['text'].encode()).hexdigest())
                bound = {'path': str(source), 'sha256': data['sha256'], 'selection': selection}
                binding = {'request': {'sources': [], 'materials': [window.model_dump()]}, 'inputs': [bound]}
                linkage = {'owner': 'prior', 'inputs': [bound]}
                frozen = json.dumps([binding, linkage], sort_keys=True)
                repair = {'owner': 'prior', 'arguments': request.model_dump(),
                    'execution_ref': request.producer_repair, 'execution_sha256': hashlib.sha256(execution.resolve('candidate_execution.json').read_bytes()).hexdigest(),
                    'review_ref': 'records/repair/native_result.json', 'old_project': old, 'new_project': new,
                    'source_replacements': {}, 'old_versions': {source: hashlib.sha256(original).hexdigest()},
                    'new_versions': {target: hashlib.sha256(replacement).hexdigest()},
                    'replace': lambda path: str(target) if workspace.resolve(path) == source else path}
                resolved = request.model_copy(update={'materials': [window]})
                with patch.object(writing, '_producer_repair', return_value=repair), \
                        patch.object(workspace, 'call', wraps=workspace.call) as reads:
                    if mode == 'whole-over-reader-bound':
                        with self.assertRaises(ValueError):
                            writing._repaired_prior_inputs(request, resolved, binding, linkage)
                    elif mode == 'changed-too-large':
                        with self.assertRaisesRegex(ValueError, 'Complete repaired material exceeds'):
                            writing._repaired_prior_inputs(request, resolved, binding, linkage)
                    else:
                        reused, evidence, retained = writing._repaired_prior_inputs(request, resolved, binding, linkage)
                        if mode == 'samehash':
                            def normalize(values):
                                return writing._material_report_arguments(values,
                                    {'requests': ['Original request', 'Later feedback']})
                            effective = ComposeRequest.model_validate(normalize(request.model_dump()))
                            effective_resolved = resolved.model_copy(update={'goal': effective.goal})
                            with self.assertRaisesRegex(ValueError, 'Producer reuse differs'):
                                writing._repaired_prior_inputs(effective, effective_resolved, binding, linkage)
                            transported, _, _ = writing._repaired_prior_inputs(effective, effective_resolved, binding,
                                linkage, normalize_arguments=normalize)
                            self.assertEqual(transported.goal, effective.goal)
                            with self.assertRaisesRegex(ValueError, 'Producer reuse differs'):
                                writing._repaired_prior_inputs(effective.model_copy(update={'title': 'Unrelated request'}),
                                    effective_resolved, binding, linkage, normalize_arguments=normalize)
                        current = retained['inputs'][0]
                        self.assertEqual(current['sha256'], hashlib.sha256(replacement).hexdigest())
                        self.assertEqual(current['path'], str(target))
                        if mode == 'samehash':
                            self.assertEqual(reused.materials[0].model_dump(), {**window.model_dump(), 'path': str(target)})
                            self.assertEqual(current['selection'], selection)
                            self.assertEqual(retained['producer_repair']['changed_materials'], [])
                            full_reads = [call for call in reads.call_args_list
                                if call.args[0] == 'read_file' and call.args[1].get('offset_bytes', 0) == 0
                                and call.args[1].get('max_bytes', 16000) >= len(original)]
                            self.assertFalse(full_reads, 'Same-hash relocation must not force whole-file text acquisition')
                        else:
                            self.assertEqual(reused.materials[0].offset_bytes, 0)
                            self.assertEqual(current['selection']['next_offset_bytes'], len(replacement))
                            self.assertFalse(current['selection']['has_more'])
                            self.assertEqual(current['selection']['window_sha256'], hashlib.sha256(replacement).hexdigest())
                            self.assertEqual(retained['producer_repair']['changed_materials'], ['compose-material-1'])
                    if mode not in {'samehash', 'whole-over-reader-bound'}:
                        reads.assert_called_once()
                        self.assertEqual(reads.call_args.args[0], 'read_file')
                        self.assertEqual(reads.call_args.args[1]['offset_bytes'], 0)
                self.assertEqual(json.dumps([binding, linkage], sort_keys=True), frozen)
                self.assertEqual(source.read_bytes(), original)

    def test_joint_subjects_keep_actual_old_script_argv_and_directory_dependencies(self):
        # This isolated argv projection has no accepted implementation unit;
        # full registered input ownership is covered by the real-unit test.
        self.enterContext(patch('simple_ar.tools.producer_declarations.professional_unit_input_owners',
            return_value={}))
        root, workspace, writing = self._real_workspace()
        old, new = workspace.workspace / 'old', workspace.workspace / 'new'
        old.mkdir()
        new.mkdir()
        code = ("from pathlib import Path\nimport sys\n"
            "if len(sys.argv) > 1:\n    assert (Path(sys.argv[1].split('=', 1)[-1]) / Path(__file__).name).read_bytes()\n"
            "Path('values.csv').write_text('value\\n1\\n', encoding='utf-8')\n")
        # Physical execution/provenance are real. Only the small upstream
        # lineage wrapper is projected from their actual argv/cwd/receipt;
        # approval and accepted implementation lineage live in the full chain.
        for suffix, mode in ((suffix, mode) for suffix in ('.py', '.txt', '.json', '')
                for mode in ('current', 'old-script', 'old-directory')):
            with self.subTest(suffix=suffix, mode=mode):
                source, current = old / ('plot' + suffix), new / ('plot' + suffix)
                source.write_text(code, encoding='utf-8')
                current.write_bytes(source.read_bytes())
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                invocation = 'producer-' + mode + '-' + (suffix.lstrip('.') or 'extensionless')
                cwd = new / invocation
                cwd.mkdir()
                output = cwd / 'values.csv'
                argv = ['python', str(source) if mode == 'old-script' else str(current)]
                if mode == 'old-directory':
                    argv.append('--source-dir=' + str(old))
                result = workspace.call('run_process', {'argv': argv, 'cwd': str(cwd), 'timeout_sec': 3,
                    'inputs': [str(source), str(current)], 'outputs': [str(output)]}, identity=invocation)
                self.assertEqual((result.status, result.data.get('returncode')), ('ok', 0), result)
                receipt = root / result.data['receipt']
                receipt_bytes = receipt.read_bytes()
                physical = json.loads(receipt_bytes)
                execution = ArtifactStore(root / 'attempts' / invocation)
                reference = execution.write_json('candidate_execution.json', {
                    'argv': physical['argv'], 'cwd': physical['cwd'], 'receipt': result.data['receipt']})
                repair = {'source_replacements': {str(source): {
                    'old_sha256': digest, 'path': str(current), 'sha256': digest}},
                    'execution_ref': 'records/' + invocation + '/candidate_execution.json',
                    'execution_sha256': hashlib.sha256(execution.resolve(reference).read_bytes()).hexdigest()}
                for units, declared_only in ((units, declared_only)
                        for units in (None, [{'files': {str(output): hashlib.sha256(output.read_bytes()).hexdigest(),
                            str(current): digest}}])
                        for declared_only in (False, True)):
                    with self.subTest(complete_unit=units is not None, declared_subjects_only=declared_only):
                        paths, consumers = writing._joint_delivery_paths([{'path': str(output)}],
                            [str(output)] if declared_only else [],
                            producer_repair=repair, producer_units=units,
                            **({'declared_subjects_only': True} if declared_only else {}))
                        selected = {workspace.resolve(path) for path in paths}
                        self.assertEqual(current in selected,
                            units is not None or suffix == '.py' or declared_only and mode != 'old-script',
                            'Declared argv dependencies ignore suffix; legacy source admission stays unchanged')
                        self.assertIn(output, selected)
                        expected_source = mode != 'current' and (suffix == '.py' or declared_only)
                        self.assertEqual(source in selected, expected_source,
                            'Actual argv file/directory dependencies are subjects under the declared contract; '
                            'unreferenced design context stays outside it and legacy suffix handling is unchanged')
                        if expected_source:
                            self.assertTrue(consumers[str(source)])
                            self.assertEqual(consumers[str(source)][0]['invocation_id'], invocation)
                self.assertEqual(receipt.read_bytes(), receipt_bytes)
                self.assertEqual(source.read_text(encoding='utf-8'), code)

    def test_bound_report_projection_keeps_saved_findings_without_reading_current_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Mock()
            workspace.resolve.side_effect = lambda path: root / path
            gateway = WritingToolGateway(root, workspace, None, 'unused', 'task')
            review = {
                'review_status': 'needs_revision',
                'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                    'findings': [{'finding_id': 'caption', 'severity': 'minor',
                        'type': 'caption_mismatch', 'required_action': 'revise', 'message': 'Caption says four panels; figure has two.',
                        'suggested_action': 'Correct the panel count.',
                        }],
                    'revision_instructions': ['Replace four panels with two panels.'],
                    'context_requests': [], 'notes': 'Do not reinterpret the measurements.'}],
                'mechanical_constraints': {'status': 'needs_revision', 'diagnostic': 'Missing Limitations'},
            }
            review['section_reviews'] = [ReportSectionReview.model_validate(row).model_dump(mode='json')
                for row in review['section_reviews']]
            path = root / 'workspace/manuscripts/draft/review.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(review), encoding='utf-8')
            result = ToolResult(tool_name='compose_document', artifacts=[
                'workspace/manuscripts/draft/manuscript.md', 'workspace/manuscripts/draft/review.json'],
                data={'version': 'workspace/manuscripts/draft',
                    'editable_markdown': 'workspace/manuscripts/draft/manuscript.md',
                    'review_path': 'workspace/manuscripts/draft/review.json',
                    'report_binding': {'requests': ['original request'], 'evidence': 'x' * 20000},
                    'inputs': [{'path': 'data.csv', 'sha256': 'original'}],
                    'material_selection': {'observations': ['x' * 20000]},
                    'artifact_sha256': {'manuscript.md': 'original'},
                    'unresolved': [ReviewerFinding.model_validate(review['section_reviews'][0]['findings'][0]).model_dump(mode='json'),
                        {'type': 'incomplete_independent_review', 'review_status': 'needs_revision'}]})
            original = result.model_dump(mode='json')
            presented = gateway.coordination_result(result)
            self.assertNotIn('independent_review', presented['data'])
            self.assertEqual(presented['data']['review_path'], 'workspace/manuscripts/draft/review.json')
            self.assertEqual(presented['data']['unresolved'], result.data['unresolved'])
            self.assertEqual(presented['artifacts'], result.artifacts)
            self.assertEqual(presented['receipt_path'], 'tools/writing/draft/result.json')
            self.assertLess(len(json.dumps(presented)), len(json.dumps(original)) // 4)
            presented['data']['unresolved'].clear()
            self.assertEqual(result.model_dump(mode='json'), original)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), review)
            workspace.resolve.assert_not_called()
            path.write_text('{"review_status":"reviewed"}', encoding='utf-8')
            self.assertEqual(gateway.coordination_result(result), gateway.coordination_result(ToolResult.model_validate(original)))

            # Producer graphs remain durable evidence, not a repeated payload
            # that can evict the current inspection result from model context.
            result.data['pending_candidate'] = 'workspace/manuscripts/draft/pending_candidate.json'
            result.data['delivery_review'] = {'contract': 'delivery_artifacts.v1',
                'status': 'needs_revision', 'defects': [{'message': 'Attachment and text disagree.',
                    'path': 'artifact.csv', 'required_action': 'revise'}],
                'uninspected': [{'path': 'image.bin', 'reason': 'No visual inspection'}],
                'artifacts': [{'path': 'artifact.csv', 'coverage': 'complete', 'visual_reviewed': False,
                    'producer': {'inputs': {'source': 'x' * 300000}}},
                    {'path': 'image.bin', 'coverage': 'visual_uninspected', 'visual_reviewed': False}],
                'declaration_adoption': {'producers': ['x' * 300000]}}
            result.data['independent_review'] = {**review, 'delivery_review': result.data['delivery_review']}
            saved = result.model_dump(mode='json')
            projected = gateway.coordination_result(result)
            self.assertNotIn('independent_review', projected['data'])
            self.assertEqual(projected['data']['unresolved'], result.data['unresolved'])
            inspection = projected['data']['delivery_review']
            self.assertEqual(inspection['defects'], saved['data']['delivery_review']['defects'])
            self.assertEqual(inspection['uninspected'], saved['data']['delivery_review']['uninspected'])
            self.assertEqual(inspection['artifact_coverage_counts'], {'complete': 1, 'visual_uninspected': 1})
            self.assertEqual(inspection['visually_reviewed_count'], 0)
            self.assertEqual(projected['outcome'], {'delivery_review_status': 'needs_revision',
                'review_status': 'needs_revision',
                'unresolved_count': len(result.data['unresolved']),
                'pending_candidate': result.data['pending_candidate']})
            self.assertEqual(projected['data']['pending_candidate'], result.data['pending_candidate'])
            self.assertLess(len(json.dumps(projected)), len(json.dumps(saved)) // 20)
            inspection['defects'].clear()
            self.assertEqual(result.model_dump(mode='json'), saved)
            workspace.resolve.assert_not_called()

    def test_producer_repair_handoff_uses_candidate_diagnosis_not_adopted_review(self):
        from types import SimpleNamespace
        from simple_ar.tools.implementation_work import _repair_view
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / 'result.json'
            adopted = {'delivery_review': {'status': 'reviewed', 'defects': []},
                'pending_candidate': 'retained-candidate.json'}
            record.write_text(json.dumps({'data': adopted}), encoding='utf-8')
            diagnosis = {'delivery_review': {'status': 'needs_revision', 'defects': [
                {'finding': {'message': 'Producer and candidate disagree.',
                    'evidence_quotes': [{'quote': 'wrong label'}]},
                 'targets': [{'path': 'source.py'}]}]}}
            original = json.loads(json.dumps(diagnosis))
            owner = SimpleNamespace(workspace=SimpleNamespace(resolve=lambda _: record))
            with patch('simple_ar.tools.writing.read_repair_review', return_value=diagnosis) as select:
                projected = _repair_view(owner, 'registered-result',
                    {'tool_name': 'compose_document', 'data': adopted}, {}, [])
            select.assert_called_once_with(owner.workspace, adopted)
            self.assertEqual(projected['delivery_review'], diagnosis['delivery_review'])
            projected['delivery_review']['defects'][0]['finding']['message'] = 'Changed copy'
            self.assertEqual(diagnosis, original)
            self.assertEqual(json.loads(record.read_text())['data'], adopted)
            self.assertEqual(adopted['delivery_review']['defects'], [])

    def test_native_failure_and_historical_review_preserve_diagnostics_and_delivery(self):
        gateway = WritingToolGateway(Path('.'), Mock(), None, 'unused', 'task')
        for result in (
            ToolResult(tool_name='compose_document', status='error', error='Writer interrupted; no blind retry.',
                artifacts=['workspace/manuscripts/partial/manuscript.md'], data={
                    'incomplete': True, 'failure_kind': 'review_incomplete',
                    'retained_candidate': {'markdown': 'workspace/manuscripts/partial/unchecked_candidate.md',
                        'status': 'unreviewed', 'adopted': False},
                    'next_action': 'Correct the retained candidate with original evidence.'}),
            ToolResult(tool_name='review_document', data={
                'review_status': 'needs_revision', 'section_reviews': [ReportSectionReview(
                    section_id='document', verdict='fail',
                    revision_instructions=['Remove the unsupported speedup claim.']).model_dump(mode='json')],
                'delivery': {'unresolved': ['Need independent timing evidence.'],
                    'artifacts': ['report.pdf']}, 'inputs': [{'path': 'original.md'}]}),
        ):
            with self.subTest(tool=result.tool_name):
                original = result.model_dump(mode='json')
                view = gateway.coordination_result(result, receipt_path='attempts/owner/native_result.json')
                self.assertEqual(view['receipt_path'], 'attempts/owner/native_result.json')
                self.assertEqual((view['status'], view['error']), (result.status, result.error))
                self.assertEqual(view['data'], {k: v for k, v in result.data.items() if k != 'inputs'})
                self.assertEqual(result.model_dump(mode='json'), original)
        gateway.workspace.resolve.assert_not_called()
