"""Document producer behavior; task composition and independent review live elsewhere."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
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
        self._settings = SimpleNamespace(context_input_tokens=24000)

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
    def test_report_receives_only_selected_source_passage_and_its_real_range(self):
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.report.document_writer import _observed_documents
        from simple_ar.report.projection import build_material_report_inputs

        root, _, _, _ = self.workspace()
        original = DocumentBundle([DocumentRecord(document_id='paper', title='Paper', source='test',
            metadata={'paper_id': 'paper', 'unobserved_analysis': 'Unread metadata finding.'},
            extraction_status='parsed')], {}, {}, [], [
                TextChunk(chunk_id='paper-1', document_id='paper', text='Unread before. Read this. Unread after.')])
        path = root / 'source.json'
        path.write_text(json.dumps(original.to_handoff_dict()), encoding='utf-8')
        selected, _ = _observed_documents([{'id': 'o1', 'kind': 'source_passages', 'data': {
            'document_path': str(path), 'document_record': str(path), 'document_sha256': sha(path.read_bytes()),
            'matches': [{'document_id': 'paper', 'chunk_id': 'paper-1',
                'text': 'Read this.', 'character_start': 15, 'character_end': 25,
                'total_characters': 39, 'truncated': True}]}}])
        store = ArtifactStore(root / 'report')
        ref = store.write_json('documents.json', selected.to_handoff_dict(), kind='documents', schema='document_bundle.v1')
        context, _ = build_material_report_inputs(topic='Question', documents=selected,
            documents_ref=ref, assets=[], selected_evidence=True)
        passage = context.source_handles[0].metadata['evidence_passages'][0]
        self.assertEqual((passage['text'], passage['character_start'], passage['character_end'],
            passage['total_characters'], passage['truncated']), ('Read this.', 15, 25, 39, True))
        self.assertNotIn('Unread after.', json.dumps(context.model_dump(mode='json')))
        self.assertNotIn('Unread metadata finding.', json.dumps(context.model_dump(mode='json')))

    def test_common_report_retains_cited_original_and_bibliography(self):
        from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
        from tests.report_review_fixtures import prompt_payload

        root, source, ledger, workspace = self.workspace()
        bundle = DocumentBundle([DocumentRecord(document_id='paper', title='Original paper', source='local_files',
            authors=['Researcher Example'], extraction_status='parsed', metadata={'paper_id': 'paper'})], {}, {}, [], [
                TextChunk('chunk-1', 'paper', 'Calibration depends on the observed population.')])
        (source / 'original.json').write_text(json.dumps(bundle.to_handoff_dict()), encoding='utf-8')
        config = ReportRuntimeConfig(reviewer='disabled', max_review_iterations=0, allow_source_backtracking=False)
        template = ReportTemplateBundle(name='brief', mode='supplied_materials', template_path='',
            criteria_path='', template_markdown='## Findings\n', criteria_markdown='')
        requirement = {'method': 'article', 'config': config.model_dump(mode='json'),
            'template': template.model_dump(mode='json'), 'outline': []}

        class Client(_Client):
            def ask_json(self, system, prompt, *, label='', budget_call_id='', **options):
                self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 10},
                    session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
                if label == 'document-work-1':
                    response = {'action': 'investigate', 'source_queries': [
                        {'document_record': 'source0/original.json', 'query': 'calibration population'}]}
                elif label == 'document-work-2':
                    response = {'action': 'write', 'title': 'Calibration', 'selected_observation_ids': ['o1']}
                elif label == 'composition-article':
                    view = prompt_payload(prompt)
                    self_test.assertIn('observed population', json.dumps(view))
                    response = {'draft_markdown': '# Calibration\n\nThe observed population matters [@P1].'}
                else:
                    raise AssertionError(label)
                self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
                return response

        self_test = self
        client = Client(ledger, 'citation', None)
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=lambda _: client,
            material_roots=lambda: ['source0'], sources=SourceToolGateway(root, workspace, ledger),
            context_policy='working.v3', report_requirement=requirement)
        result = gateway.call('work_document', {'goal': 'Explain the source'}, identity='citation')
        self.assertTrue(result.ok, result.error)
        bibliography = workspace.resolve('workspace/document-work/citation/references.bib').read_text(encoding='utf-8')
        self.assertIn('Original paper', bibliography)
        self.assertIn('Researcher Example', bibliography)
        self.assertFalse(any('citation references were removed' in gap for gap in result.data['gaps']))

    def test_common_section_writer_can_read_an_unselected_passage_of_selected_source(self):
        from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
        from tests.report_review_fixtures import prompt_payload

        root, source, ledger, workspace = self.workspace()
        bundle = DocumentBundle([
            DocumentRecord(document_id='paper', title='Paper', source='local_files', extraction_status='parsed'),
            DocumentRecord(document_id='other', title='Other', source='local_files', extraction_status='parsed'),
        ], {}, {}, [], [
            TextChunk('initial', 'paper', 'Calibration is described here.'),
            TextChunk('conditions', 'paper', 'The treatment condition was twelve days.'),
            TextChunk('foreign', 'other', 'The treatment condition was ninety days.'),
        ])
        (source / 'original.json').write_text(json.dumps(bundle.to_handoff_dict()), encoding='utf-8')
        config = ReportRuntimeConfig(outline_strategy='template', reviewer='disabled', max_review_iterations=0,
            allow_source_backtracking=True)
        template = ReportTemplateBundle(name='brief', mode='supplied_materials', template_path='',
            criteria_path='', template_markdown='## Findings\n', criteria_markdown='')
        requirement = {'method': 'sections', 'config': config.model_dump(mode='json'),
            'template': template.model_dump(mode='json'),
            'outline': [{'heading': 'Findings', 'required': True}]}
        test = self

        class Client(_Client):
            def ask_json(self, system, prompt, *, label='', budget_call_id='', **options):
                view = prompt_payload(prompt)
                self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 10},
                    session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
                if label == 'document-work-1':
                    response = {'action': 'investigate', 'source_queries': [
                        {'document_record': 'source0/original.json', 'query': 'calibration'}]}
                elif label == 'document-work-2':
                    response = {'action': 'write', 'title': 'Findings', 'selected_observation_ids': ['o1']}
                elif label == 'report-source-selection':
                    test.assertNotIn('twelve days', json.dumps(view))
                    handle = view['sources'][0]['handle']
                    response = {'context_requests': [
                        {'tool_name': 'search_source_chunks', 'arguments': {'handle': handle, 'query': 'treatment condition'}},
                        {'tool_name': 'get_neighbor_chunks', 'arguments': {'handle': handle}}]}
                elif label.startswith('report-writer-'):
                    evidence = json.dumps(view)
                    test.assertIn('twelve days', evidence)
                    test.assertNotIn('ninety days', evidence)
                    response = {'section_id': view['section']['section_id'],
                        'heading': view['section']['heading'], 'draft_markdown': 'The treatment lasted twelve days [@P1].'}
                else:
                    raise AssertionError(label)
                self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
                return response

        client = Client(ledger, 'backtrack', None)
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=lambda _: client,
            material_roots=lambda: ['source0'], sources=SourceToolGateway(root, workspace, ledger),
            context_policy='working.v3', report_requirement=requirement)
        result = gateway.call('work_document', {'goal': 'Explain the source'}, identity='backtrack')
        self.assertTrue(result.ok, result.error)
        self.assertIn('twelve days', result.data['markdown'])
        record = root / result.data['provenance']['writer_source_reads']
        observed = json.loads(record.read_text(encoding='utf-8'))['tool_results']
        lookup = next(row for row in observed if row['tool_name'] == 'search_source_chunks')
        self.assertEqual(lookup['content']['search_scope'], 'persisted_extracted_text')
        self.assertEqual([row['chunk_id'] for row in lookup['content']['chunks']], ['conditions'])
        neighbor = next(row for row in observed if row['tool_name'] == 'get_neighbor_chunks')
        self.assertEqual(neighbor['content']['anchor_chunk_id'], 'initial')

    def test_common_report_runs_configured_review_revision_and_recovery(self):
        from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
        from tests.report_review_fixtures import draft_quotes, prompt_payload

        root, source, ledger, workspace = self.workspace()
        (source / 'result.txt').write_text('Observed value: 2.\n', encoding='utf-8')
        config = ReportRuntimeConfig(outline_strategy='template', reviewer='llm',
            document_review=True, review_scope='document', draft_scope='document',
            max_review_iterations=1, max_section_tokens=128, allow_source_backtracking=False)
        template = ReportTemplateBundle(name='controlled', mode='supplied_materials',
            template_path='', criteria_path='', template_markdown='## Observations\n\n## Limits\n', criteria_markdown='')
        requirement = {'method': 'sections', 'config': config.model_dump(mode='json'),
            'template': template.model_dump(mode='json'), 'outline': [
                {'heading': 'Observations', 'required': True}, {'heading': 'Limits', 'required': True}]}
        calls = []

        class Client(_Client):
            def ask_json(self, system, prompt, *, label='', budget_call_id='', **options):
                view = prompt_payload(prompt)
                calls.append((label, options, view))
                self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 10},
                    session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
                if label == 'document-work-1':
                    response = {'action': 'investigate', 'read_windows': [{'path': 'source0/result.txt'}]}
                elif label == 'document-work-2':
                    response = {'action': 'write', 'title': 'Observed result',
                        'selected_observation_ids': [view['observations'][0]['id']]}
                elif label in {'report-writer-document', 'report-document-joint-reviser'}:
                    response = {'sections': [{'section_id': row['section']['section_id'],
                        'heading': row['section']['heading'], 'draft_markdown':
                        ('Revised' if 'reviser' in label else 'Original') + ' observed value 2.'}
                        for row in view['sections']]}
                elif label.startswith('report-writer-'):
                    response = {'section_id': view['section']['section_id'],
                        'heading': view['section']['heading'], 'draft_markdown': 'Original observed value 2.'}
                elif label in {'report-document-reviewer', 'report-document-verifier'}:
                    revise = label == 'report-document-reviewer'
                    response = {'section_reviews': [{'section_id': row['section_id'],
                        'verdict': 'revise_required' if revise else 'pass',
                        'findings': [{'finding_id': row['section_id'] + '-scope', 'type': 'style',
                            'severity': 'major', 'required_action': 'revise', 'message': 'Clarify scope.',
                            'draft_quotes': draft_quotes(prompt, row['section_id'])}] if revise else []}
                        for row in view['sections']]}
                else:
                    raise AssertionError(label)
                self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
                return response

        client = Client(ledger, 'report', None)
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=lambda _: client,
            material_roots=lambda: ['source0'], work_context=lambda _: {'user_requests': ['Explain the observed result']},
            context_policy='working.v3', report_requirement=requirement)
        from simple_ar.core.capabilities import ArtifactStore
        write = ArtifactStore.write_json
        def interrupt_assembly(owner, path, *args, **kwargs):
            if path == 'decisions/2/authored.json':
                raise KeyboardInterrupt('Stop after the completed writer, before candidate registration')
            return write(owner, path, *args, **kwargs)
        with patch.object(ArtifactStore, 'write_json', interrupt_assembly), self.assertRaises(KeyboardInterrupt):
            gateway.call('work_document', {'goal': 'Explain the observation'}, identity='report')
        saved_usage = ledger.to_dict()
        saved_calls = len(calls)
        self.assertTrue(gateway.can_reconcile('work_document', 'report'))
        result = gateway.call('work_document', {'goal': 'Explain the observation'}, identity='report')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(ledger.to_dict(), saved_usage)
        self.assertEqual(len(calls), saved_calls)
        self.assertIn('Revised observed value 2.', result.data['markdown'])
        labels = [row[0] for row in calls]
        self.assertEqual(labels.count('report-document-reviewer'), 1)
        self.assertEqual(labels.count('report-document-joint-reviser'), 1)
        self.assertEqual(labels.count('report-document-verifier'), 1)
        self.assertTrue(all(row[1].get('max_output_tokens') == 128 for row in calls
            if row[0].startswith('report-writer-') or row[0] == 'report-document-joint-reviser'))
        self.assertEqual(len([row for row in calls if row[0].startswith('report-writer-')]), 1)
        self.assertTrue(any('Observed value: 2.' in json.dumps(row[2]) for row in calls
            if row[0].startswith('report-writer-')))
        self.assertIn('workspace/document-work/report/references.bib', result.artifacts)
        saved_usage = ledger.to_dict()
        self.assertTrue(gateway.call('work_document', {'goal': 'Explain the observation'}, identity='report').ok)
        self.assertEqual(ledger.to_dict(), saved_usage)
        self.assertEqual([row[0] for row in calls], labels)

    def test_common_report_uses_configured_section_writer_and_output_limit(self):
        from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
        from tests.report_review_fixtures import prompt_payload

        root, source, ledger, workspace = self.workspace()
        config = ReportRuntimeConfig(outline_strategy='template', reviewer='disabled',
            max_review_iterations=0, max_section_tokens=96, allow_source_backtracking=False)
        template = ReportTemplateBundle(name='sections', mode='supplied_materials', template_path='',
            criteria_path='', template_markdown='## Observations\n\n## Limits\n', criteria_markdown='')
        requirement = {'method': 'sections', 'config': config.model_dump(mode='json'),
            'template': template.model_dump(mode='json'), 'outline': [
                {'heading': 'Observations', 'required': True}, {'heading': 'Limits', 'required': True}]}
        calls = []

        class Client(_Client):
            def ask_json(self, system, prompt, *, label='', budget_call_id='', **options):
                view = prompt_payload(prompt)
                calls.append((label, options))
                self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 10},
                    session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
                if label == 'document-work-1':
                    response = {'action': 'write', 'title': 'Brief'}
                elif label.startswith('report-writer-'):
                    response = {'section_id': view['section']['section_id'], 'heading': view['section']['heading'],
                        'draft_markdown': 'Bounded information.'}
                else:
                    raise AssertionError(label)
                self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
                return response

        client = Client(ledger, 'sections', None)
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=lambda _: client,
            material_roots=lambda: ['source0'], context_policy='working.v3', report_requirement=requirement)
        from simple_ar.core.capabilities import ArtifactStore
        write = ArtifactStore.write_json
        def interrupt_checkpoint(owner, path, payload, **options):
            result = write(owner, path, payload, **options)
            if path == 'sections.json' and len(payload.get('completed', {}).get('sections', [])) == 1:
                raise KeyboardInterrupt('Stop after the first complete section checkpoint')
            return result
        with patch.object(ArtifactStore, 'write_json', interrupt_checkpoint), self.assertRaises(KeyboardInterrupt):
            gateway.call('work_document', {'goal': 'Write the requested brief'}, identity='sections')
        self.assertTrue(gateway.can_reconcile('work_document', 'sections'))
        initial_labels = [row[0] for row in calls]
        result = gateway.call('work_document', {'goal': 'Write the requested brief'}, identity='sections')
        self.assertTrue(result.ok, result.error)
        self.assertEqual([row[0] for row in calls[:len(initial_labels)]], initial_labels)
        self.assertIn('## Observations', result.data['markdown'])
        self.assertIn('## Limits', result.data['markdown'])
        writers = [row for row in calls if row[0].startswith('report-writer-')]
        self.assertEqual(len(writers), 2)
        self.assertTrue(all(options['max_output_tokens'] == 96 for _, options in writers))

    def test_common_article_feedback_keeps_unaffected_prior_work(self):
        from simple_ar.report.schema import ReportRuntimeConfig, ReportTemplateBundle
        from tests.report_review_fixtures import prompt_payload

        root, source, ledger, workspace = self.workspace()
        previous = source / 'draft.md'
        previous.write_text('# Existing\n\nOld claim.\n\nUnaffected text.\n', encoding='utf-8')
        config = ReportRuntimeConfig(reviewer='disabled', max_review_iterations=0,
            max_section_tokens=256, allow_source_backtracking=False)
        template = ReportTemplateBundle(name='brief', mode='supplied_materials', template_path='',
            criteria_path='', template_markdown='## Findings\n', criteria_markdown='')
        requirement = {'method': 'article', 'config': config.model_dump(mode='json'),
            'template': template.model_dump(mode='json'), 'outline': []}
        calls = []

        class Client(_Client):
            def ask_json(self, system, prompt, *, label='', budget_call_id='', **options):
                view = prompt_payload(prompt)
                calls.append((label, view, options))
                self._budget_ledger.reserve(budget_call_id, {'llm_requests': 1, 'total_tokens': 10},
                    session_id='session', attempt_id=self._budget_attempt_id, purpose='document')
                if label == 'document-work-1':
                    response = {'action': 'write', 'title': 'Existing'}
                elif label == 'composition-article':
                    response = {'prior_draft_sha256': view['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': 'Old claim.', 'new_text': 'Qualified claim.'}]}
                else:
                    raise AssertionError(label)
                self._budget_ledger.settle(budget_call_id, {'llm_requests': 1, 'total_tokens': 10})
                return response

        client = Client(ledger, 'revision', None)
        gateway = DocumentWorkToolGateway(root, workspace, ledger, 'session', client_factory=lambda _: client,
            material_roots=lambda: ['source0'], work_context=lambda _: {'user_requests': ['Qualify the claim']},
            context_policy='working.v3', report_requirement=requirement)
        result = gateway.call('work_document', {'goal': 'Qualify the claim', 'prior_draft': 'source0/draft.md',
            'prior_sha256': sha(previous.read_bytes())}, identity='revision')
        self.assertTrue(result.ok, result.error)
        self.assertIn('Qualified claim.', result.data['markdown'])
        self.assertIn('Unaffected text.', result.data['markdown'])
        self.assertEqual(calls[1][2]['max_output_tokens'], 256)

    def test_shared_feedback_can_be_selected_and_recalled_without_becoming_evidence(self):
        root, source, ledger, workspace = self.workspace()
        (source / 'result.txt').write_text('Observed value: 2.\n', encoding='utf-8')
        context = {'user_requests': ['Revise the result'], 'working_notes': [
            {'text': 'Older working note. ' * 4000, 'records': []},
            {'text': 'Retain the observed sample count.', 'records': []}], 'review_observations': [
            {'record': 'records/review-large/result.json', 'subjects': [],
             'result': {'message': 'Older guidance. ' * 5000}},
            {'record': 'records/review-current/result.json',
             'subjects': [{'current_matches_review': True}],
             'result': {'message': 'Explain the observed value.'}}]}
        review = root / 'attempts' / 'review-large' / 'result.json'
        review.parent.mkdir(parents=True)
        review.write_text(json.dumps(context['review_observations'][0]['result']), encoding='utf-8')

        def decide(view, count):
            handoff = view['work_context']
            self.assertEqual(handoff['user_requests'], context['user_requests'])
            self.assertIn('records/review-large/result.json', json.dumps(view['material_supply']))
            self.assertIn('records/review-current/result.json', json.dumps(view['material_supply']))
            self.assertIn('Explain the observed value.', json.dumps(handoff))
            self.assertIn('Retain the observed sample count.', json.dumps(handoff))
            self.assertNotIn('Older guidance. ' * 5000, json.dumps(view))
            self.assertNotIn('Older working note. ' * 4000, json.dumps(view))
            if count == 1:
                return {'action': 'investigate', 'read_windows': [
                    {'path': handoff['work_context_archive']['record'], 'max_bytes': 512},
                    {'path': 'records/review-large/result.json', 'max_bytes': 512},
                    {'path': 'source0/result.txt', 'max_bytes': 512}]}
            observations = {row['kind']: row['id'] for row in view['observations']}
            self.assertIn('work_context', observations)
            self.assertIn('material_window', observations)
            self.assertTrue(any('Older guidance.' in row['observation']['data']['text']
                for row in view['observations'] if row['kind'] == 'work_context'))
            if count == 2:
                return {'action': 'finish', 'manuscript_markdown': '# Result\nThe observed value is 2.\n',
                    'selected_observation_ids': [observations['work_context'], observations['material_window']]}
            self.assertEqual(view['read_results'][0]['status'], 'candidate_selection_rejected')
            return {'action': 'finish', 'manuscript_markdown': '# Result\nThe observed value is 2.\n',
                'selected_observation_ids': [observations['material_window']]}

        gateway, clients = self.gateway(root, source, ledger, workspace, decide, context=context)
        gateway.context_policy = 'working.v2'
        from simple_ar.core.capabilities import ArtifactStore
        write = ArtifactStore.write_json
        def interrupt_candidate(owner, path, *args, **kwargs):
            if path == 'candidate.json':
                raise KeyboardInterrupt('Stop after the accounted final response')
            return write(owner, path, *args, **kwargs)
        with patch.object(ArtifactStore, 'write_json', interrupt_candidate), self.assertRaises(KeyboardInterrupt):
            gateway.call('work_document', {'goal': 'Revise the result'}, identity='handoff')
        charges = ledger.to_dict()
        result = gateway.call('work_document', {'goal': 'Revise the result'}, identity='handoff')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 3)
        self.assertEqual(len(result.metadata['material_observations']), 1)
        self.assertEqual(len(result.data['provenance']['evidence']), 1)
        self.assertEqual(ledger.to_dict(), charges)
        self.assertTrue(gateway.call('work_document', {'goal': 'Revise the result'}, identity='handoff').ok)
        self.assertEqual(ledger.to_dict(), charges)
        self.assertEqual(len(clients[0].calls), 3)

    def test_shared_document_input_respects_the_callers_remaining_ledger(self):
        ledger = BudgetLedger({'total_tokens': 3200})
        client = SimpleNamespace(_settings=SimpleNamespace(context_input_tokens=12000),
            _budget_ledger=ledger, effective_output_limit=lambda: 500)
        prompt = {'goal': 'Use the current result', 'work_context': {'user_requests': ['Summarize']},
                  'coverage': 'Actual observations only.', 'read_results': [], 'observations': []}
        with ledger.protect_remaining({'total_tokens': 400}):
            view, selection = DocumentWorkToolGateway._working_input(
                prompt, [], client, 'bounded', 'Work on the request.', project_feedback=True)
        self.assertEqual(selection['input_token_target'], 2300)
        self.assertEqual(view['work_context']['user_requests'], ['Summarize'])

    def test_large_related_feedback_is_admitted_by_the_consumers_model_capacity(self):
        from simple_ar.app.tool_application import ToolApplication

        reviews = [{'record': f'records/review-{number}/result.json',
                    'result': {'message': 'Relevant evidence and revision guidance. ' * 350}}
                   for number in range(20)]
        app = SimpleNamespace(state={'requests': ['Revise the supplied work'], 'config': {}},
            operations=None, _work_feedback=lambda targets: {'review_observations': reviews},
            _retained_working_inputs=lambda targets: {'notes': []})
        context = ToolApplication._work_context(app, ['workspace/project'])
        self.assertGreater(len(json.dumps(context).encode()), 256000)
        prompt = {'goal': 'Revise the supplied work', 'work_context': context,
                  'coverage': 'Actual observations only.', 'read_results': [], 'observations': []}
        client = SimpleNamespace(_settings=SimpleNamespace(context_input_tokens=128000))
        presented, selection = DocumentWorkToolGateway._working_input(prompt, [], client, 'work', 'Work on the request.')
        self.assertEqual(presented['work_context']['review_observations'], reviews)
        self.assertLessEqual(selection['estimated_input_tokens'], 128000)

    def test_shared_context_keeps_large_observation_history_without_a_cumulative_read_gate(self):
        root, source, ledger, workspace = self.workspace()
        for number in range(1, 6):
            (source / f'part-{number}.txt').write_text(str(number) * 40000, encoding='utf-8')

        def decide(view, count):
            self.assertLessEqual(view['working_context']['estimated_input_tokens'], 24000)
            if count <= 5:
                return {'action': 'investigate', 'read_windows': [
                    {'path': f'source0/part-{count}.txt', 'max_bytes': 40000}]}
            self.assertLess(len(view['observations']), 5)
            if count == 6:
                self.assertIn('o5', [row['id'] for row in view['observations']])
                return {'action': 'investigate', 'read_windows': [{'path': 'source0/part-1.txt', 'max_bytes': 40000}]}
            self.assertIn('o1', [row['id'] for row in view['observations']])
            return {'action': 'finish', 'manuscript_markdown': '# Account\nFive original records retained.\n',
                'selected_observation_ids': [f'o{i}' for i in range(1, 6)]}

        gateway, clients = self.gateway(root, source, ledger, workspace, decide)
        gateway.context_policy = 'working.v1'
        from simple_ar.core.capabilities import ArtifactStore
        write = ArtifactStore.write_json
        def interrupt_candidate(owner, path, *args, **kwargs):
            if path == 'candidate.json':
                raise KeyboardInterrupt('Stop after the complete paid response, before saving the candidate')
            return write(owner, path, *args, **kwargs)
        with patch.object(ArtifactStore, 'write_json', interrupt_candidate), self.assertRaises(KeyboardInterrupt):
            gateway.call('work_document', {'goal': 'Account for the five supplied original records'}, identity='large')
        charges = ledger.to_dict()
        result = gateway.call('work_document', {'goal': 'Account for the five supplied original records'}, identity='large')
        self.assertTrue(result.ok, result)
        self.assertEqual(len(clients[0].calls), 7)
        self.assertEqual(ledger.to_dict(), charges)
        self.assertEqual(len(result.metadata['material_observations']), 5)
        retained = json.loads((root/'attempts/large/document_work/observations.json').read_text())
        self.assertEqual(sum(len(row['observation']['data']['text']) for row in retained), 200000)
        self.assertEqual(len(result.data['provenance']['evidence']), 5)
        calls, charges = len(clients[0].calls), ledger.to_dict()
        self.assertTrue(gateway.call('work_document', {'goal': 'Account for the five supplied original records'}, identity='large').ok)
        self.assertEqual(len(clients[0].calls), calls)
        self.assertEqual(ledger.to_dict(), charges)

    def test_required_document_input_is_checked_against_model_capacity_before_purchase(self):
        root, source, ledger, workspace = self.workspace()
        gateway, clients = self.gateway(root, source, ledger, workspace,
            lambda *_: self.fail('An overflowing required input cannot buy a response'))
        gateway.context_policy = 'working.v1'
        factory = gateway.client_factory
        def constrained(identity):
            client = factory(identity)
            client._settings.context_input_tokens = 1000
            return client
        gateway.client_factory = constrained
        result = gateway.call('work_document', {'goal': 'Original requested condition. ' * 400}, identity='overflow')
        self.assertFalse(result.ok)
        self.assertIn('saved model input capacity', result.error)
        self.assertFalse(clients[0].calls)
        self.assertFalse(ledger.entries)

        # A provider-aware count can exceed the cheap character estimate even
        # for a short prompt; the sender must stop before buying a response.
        def provider_counted(identity):
            client = constrained(identity)
            client.estimate_json_input_tokens = lambda system, user: 1001
            return client
        gateway.client_factory = provider_counted
        result = gateway.call('work_document', {'goal': 'Check the supplied work'}, identity='provider-overflow')
        self.assertFalse(result.ok)
        self.assertIn('saved model input capacity', result.error)
        self.assertFalse(clients[-1].calls)
        self.assertFalse(ledger.entries)

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
        self.assertEqual(presented['data']['markdown'], result.data['markdown'])
        self.assertEqual(presented['data']['measurements']['sha256'], result.data['sha256'])
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

    def test_preview_repairs_local_requirements_without_writing_or_rereading_sources(self):
        root, source, ledger, workspace = self.workspace()
        original = source / 'conditions.txt'
        original.write_text('Use the supplied observations only.\n', encoding='utf-8')
        long = '# Observations\n\n' + 'Actual observation. ' * 20
        revised = '# Observations\n\nThe supplied mean is 2. No population inference.\n'

        def decide(view, step):
            if step == 1:
                return {'action': 'investigate', 'read_windows': [{'path': 'source0/conditions.txt'}]}
            if step == 2:
                return {'action': 'preview', 'manuscript_markdown': long}
            preview = view['candidate_preview']
            self.assertEqual(preview['manuscript_markdown'], long)
            self.assertEqual(preview['measurements']['whitespace_words'], len(long.split()))
            self.assertEqual(preview['measurements']['sha256'], sha(long.encode()))
            self.assertEqual(preview['measurements']['headings'], ['Observations'])
            self.assertFalse(list(workspace.workspace.rglob('manuscript.md')))
            self.assertEqual(len(view['observations']), 1)
            return {'action': 'finish', 'manuscript_markdown': revised, 'selected_observation_ids': ['o1']}

        owner, clients = self.gateway(root, source, ledger, workspace, decide,
            context={'user_requests': ['Write a short explanation; preserve the observation scope.']})
        result = owner.call('work_document', {'goal': 'Explain the observations briefly'}, identity='local-revision')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 3)
        self.assertEqual(result.data['markdown'], revised)
        self.assertEqual(result.data['measurements']['whitespace_words'], len(revised.split()))
        self.assertEqual(len(result.metadata['material_observations']), 1)
        self.assertEqual(original.read_text(), 'Use the supplied observations only.\n')
        self.assertEqual(len(list(workspace.workspace.rglob('manuscript.md'))), 1)
        before = ledger.to_dict()
        self.assertEqual(owner.call('work_document', {'goal': 'Explain the observations briefly'},
            identity='local-revision'), result)
        self.assertEqual(ledger.to_dict(), before)

    def test_saved_v1_response_recovers_without_new_measurements_or_provider_calls(self):
        root, source, ledger, workspace = self.workspace()
        asset = source/'legacy.txt'
        asset.write_text('Original legacy asset.\n')
        owner, clients = self.gateway(root, source, ledger, workspace,
            lambda *_: {'action': 'finish', 'manuscript_markdown': '# Saved\nOriginal candidate.\n',
                'attachments': [str(asset)]})
        admit = owner._admit

        def legacy(*args, **kwargs):
            binding = admit(*args, **kwargs)
            binding['contract'] = 'document_work_operation.v1'
            binding['context'].pop('prior_asset_links')
            args[0].write_json('input.json', binding)
            return binding

        # Reproduce the saved v1 boundary, then recover with today's owner.
        with patch.object(owner, '_admit', side_effect=legacy), patch.object(owner, '_candidate',
                side_effect=KeyboardInterrupt('final response saved')):
            with self.assertRaises(KeyboardInterrupt):
                owner.call('work_document', {'goal': 'Saved work'}, identity='legacy-final')
        result = owner.call('work_document', {'goal': 'Saved work'}, identity='legacy-final')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 1)
        self.assertNotIn('measurements', result.data)
        self.assertNotIn('markdown', owner.coordination_result(result)['data'])
        self.assertEqual(result.data['provenance']['attachments'][0]['source'], str(asset))

    def test_selection_repair_retains_draft_reads_and_inventory_owned_attachment_identity(self):
        root, source, ledger, workspace = self.workspace()
        original = source/'observations.txt'
        original.write_text('Supplied observations only.\n')
        draft = '# Review\n\nRetain the scope of the supplied observations.\n'

        def decide(view, step):
            material, = view['candidates']
            if step == 1:
                return {'action': 'investigate', 'read_windows': [{'path': material['path']}]}
            if step == 3:
                self.assertEqual(view['candidate_preview']['manuscript_markdown'], draft)
                self.assertEqual(len(view['observations']), 1)
                self.assertEqual(view['read_results'][0]['status'], 'candidate_selection_rejected')
                self.assertFalse(list(workspace.workspace.rglob('manuscript.md')))
            return {'action': 'finish', 'manuscript_markdown': draft,
                'selected_observation_ids': ['o1'],
                'attachments': ['unlisted-material'] if step == 2 else [material['delivery_path']]}

        owner, clients = self.gateway(root, source, ledger, workspace, decide)
        result = owner.call('work_document', {'goal': 'Review the supplied observation scope'}, identity='selection-repair')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 3)
        self.assertEqual(result.data['markdown'], draft)
        self.assertEqual(len(result.metadata['material_observations']), 1)
        asset, = result.data['provenance']['attachments']
        self.assertEqual(asset['source'], str(original))
        self.assertEqual(workspace.resolve(asset['path']).read_bytes(), original.read_bytes())
        prefix = ledger.to_dict()
        self.assertEqual(owner.call('work_document', {'goal': 'Review the supplied observation scope'},
            identity='selection-repair'), result)
        self.assertEqual(ledger.to_dict(), prefix)

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

    def test_research_source_handoff_remains_readable_through_document_work(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext

        root, source, ledger, workspace = self.workspace()
        (source / 'paper.txt').write_text(
            '# Results\nCalibration depends on the observed population.\n', encoding='utf-8')
        sources = SourceToolGateway(root, workspace, ledger)
        context = CapabilityContext(ArtifactStore(root / 'attempts' / 'ingest'),
            AttemptManifest('ingest', capability='native_tool'))
        ingested = sources.execute_tool('research', context,
            {'operation': 'ingest', 'question': 'What affects calibration?',
             'materials': ['source0/paper.txt']})
        self.assertTrue(ingested.ok, ingested.error)
        locator = ingested.data['document_record']
        lookup = sources.call('find_source_passages',
            {'document_record': locator, 'query': 'calibration population'}, identity='lookup')
        self.assertTrue(lookup.ok, lookup.error)
        self.assertIn('observed population', lookup.data['matches'][0]['text'])
        retained = sources.retained_passages([{'data': lookup.data, 'turn': 1,
            'record': 'records/lookup/native_result.json'}], before_turn=2)
        self.assertEqual(retained[0]['passage'], lookup.data['matches'][0])

        def decide(view, count):
            self.assertEqual(len(view['original_source_bundles']), 1)
            if count == 1:
                return {'action': 'investigate', 'source_queries': [
                    {'document_record': locator, 'query': 'calibration population'}]}
            self.assertIn('observed population', view['observations'][0]['data']['matches'][0]['text'])
            return {'action': 'finish', 'selected_observation_ids': ['o1'],
                'manuscript_markdown': '# Result\nCalibration depends on the observed population.\n'}

        writer, clients = self.gateway(root, source, ledger, workspace, decide, sources=sources)
        args = {'goal': 'Explain the supplied paper', 'material_roots': [locator]}
        result = writer.call('work_document', args, identity='source-work')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.data['provenance']['evidence'][0]['kind'], 'source_passages')
        self.assertEqual(len(clients[0].calls), 2)

        generated = workspace.workspace / 'invented-bundle.json'
        generated.write_bytes(workspace.resolve(locator).read_bytes())
        blocked = sources.call('find_source_passages',
            {'document_record': 'workspace/invented-bundle.json'}, identity='unbound')
        self.assertFalse(blocked.ok)
        workspace.resolve(locator).write_text('{}', encoding='utf-8')
        self.assertFalse(sources.call('find_source_passages',
            {'document_record': locator}, identity='changed').ok)
        self.assertFalse(writer.call('work_document', args, identity='source-work').ok)
        self.assertEqual(len(clients[0].calls), 2)

    def test_candidate_recovers_after_interrupted_write_without_another_model_call(self):
        root, source, ledger, workspace = self.workspace()
        asset = source / 'figure.png'
        asset.write_bytes(b'original figure bytes')
        def decide(view, count):
            row = next(row for row in view['candidates'] if row['path'] == str(asset))
            return {'action': 'finish', 'attachments': [row['delivery_path']],
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

    def test_revision_preserves_commonmark_file_links_and_stable_asset_ids(self):
        root, source, ledger, workspace = self.workspace()
        previous = source/'previous'
        assets = {'assets/测量 结果.csv': b'value\n2\n', 'scripts/parse.py': b'print(2)\n',
            'figures/chart.svg': b'<svg/>', 'references.bib': b'@article{x}\n'}
        for name, raw in assets.items():
            path = previous/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        body = ('# Recorded data\n[Data][rows] and [code](scripts/parse.py).\n\n'
            '[rows]: <assets/测量%20结果.csv#rows> "Observed rows"\n\n'
            '![Plot](figures/chart.svg)\n[Section](#recorded-data) [Self](manuscript.md#recorded-data) '
            '[Assets](assets/) '
            '[External](https://example.org/paper)\n`[example](missing.csv)`\n'
            '```markdown\n[example](missing.csv)\n```\n')
        draft = previous/'manuscript.md'
        draft.write_text(body, encoding='utf-8')
        def author(view, _count):
            row = next(row for row in view['candidates']
                if row['path'] == str(previous/'assets/测量 结果.csv'))
            self.assertEqual(row['delivery_path'], 'assets/测量 结果.csv')
            return {'action': 'finish', 'manuscript_markdown': body+'\nScope clarified.\n',
                'attachments': [row['delivery_path']]}
        owner, clients = self.gateway(root, source, ledger, workspace, author)
        args = {'goal': 'Clarify only the prose and retain the referenced work.',
            'prior_draft': str(draft), 'prior_sha256': sha(draft.read_bytes())}
        result = owner.call('work_document', args, identity='linked-revision')
        self.assertTrue(result.ok, result.error)
        output = workspace.resolve(result.data['manuscript']).parent
        self.assertEqual(len(result.artifacts), 5)  # One copy, even when selected and inherited.
        for name, raw in assets.items():
            self.assertEqual((output/name).read_bytes(), raw)
            self.assertEqual((previous/name).read_bytes(), raw)
        self.assertEqual(draft.read_text(encoding='utf-8'), body)
        self.assertEqual(result.data['markdown'], body+'\nScope clarified.\n')
        self.assertEqual(owner.call('work_document', args, identity='linked-revision'), result)
        self.assertEqual(len(clients[0].calls), 1)
        (previous/'scripts/parse.py').write_bytes(b'changed source')
        rejected = owner.call('work_document', args, identity='linked-revision')
        self.assertEqual(rejected.status, 'blocked')
        self.assertEqual((output/'scripts/parse.py').read_bytes(), assets['scripts/parse.py'])

    def test_local_link_repair_preserves_the_draft_without_reading_unmounted_paths(self):
        root, source, ledger, workspace = self.workspace()
        (source/'data.csv').write_text('value\n2\n')
        unmounted = root/'private.txt'
        unmounted.write_text('UNMOUNTED_TEST_MATERIAL')
        bad = ('# Data\n[Data](assets/not-selected.csv) [Outside](../private.txt)\n'
            '```markdown\n[example](missing.csv)\n```\n')
        def author(view, count):
            row = next(row for row in view['candidates'] if row['path'] == str(source/'data.csv'))
            if count == 1:
                body = bad
            else:
                self.assertEqual(view['candidate_preview']['manuscript_markdown'], bad)
                self.assertEqual(view['read_results'][0]['status'], 'candidate_selection_rejected')
                self.assertFalse(list(workspace.workspace.rglob('*.csv')))
                self.assertNotIn('UNMOUNTED_TEST_MATERIAL', json.dumps(view))
                body = '# Data\n[Data]('+row['delivery_path']+')\n```markdown\n[example](missing.csv)\n```\n'
            return {'action': 'finish', 'manuscript_markdown': body, 'attachments': [row['delivery_path']]}
        owner, clients = self.gateway(root, source, ledger, workspace, author)
        result = owner.call('work_document', {'goal': 'Keep a usable data link.'}, identity='link-repair')
        self.assertTrue(result.ok, result.error)
        self.assertEqual(len(clients[0].calls), 2)
        self.assertEqual(unmounted.read_text(), 'UNMOUNTED_TEST_MATERIAL')
        self.assertEqual(len(result.artifacts), 2)

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
        self.assertEqual(len(clients[0].calls), 2)  # Same rejected selection stops, without dropping evidence.
        self.assertFalse((workspace.workspace / 'document-work/unobserved/manuscript.md').exists())


if __name__ == '__main__':
    unittest.main()
