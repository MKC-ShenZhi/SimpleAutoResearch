"""Whole-document composition, review, joint edits and recovery behavior."""
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from simple_ar.core.capabilities import ArtifactRef, ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.agent import run_report_agent
from simple_ar.report.editor import edit_joint_document
from simple_ar.report.schema import (
    ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionPlan,
    ReportSectionDraft, ReportSectionReview, ReviewerFinding, ReportIterationRecord,
    SourceHandle, ReportToolResult, ReportDocumentPlan, ReportVisualIntent,
)
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.report.writing import ReportWritingRequest, run_report_writing_capability
from simple_ar.research.contracts import TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from tests.report_review_fixtures import (
    CheckpointStop, agent_inputs, draft_quotes, interrupt_checkpoint, prompt_payload,
)


class DocumentFirstTests(unittest.TestCase):
    def test_explicit_outline_owns_joint_writing_revision_and_checkpoint_recovery(self):
        from simple_ar.report.assembler import assemble_report_sections, article_section_headings
        from simple_ar.report.writing import _check_article_constraints

        headings = ['Purpose and inputs', 'Findings', 'Interpretation and limits',
                    'Conclusion and follow-up options']
        outline = [{'heading': heading, 'required': True} for heading in headings]
        kwargs = self.inputs(draft_scope='document')
        kwargs['config'].outline_strategy = 'adaptive'
        original_memory = kwargs['memory'].model_dump(mode='json')
        labels, views = [], []

        class Client:
            def ask_json(inner, system, prompt, *, label='', **unused):
                labels.append(label)
                view = prompt_payload(prompt)
                views.append((label, copy.deepcopy(view)))
                if label in {'report-writer-document', 'report-document-joint-reviser'}:
                    return {'sections': [{'section_id': row['section']['section_id'],
                        'heading': row['section']['heading'],
                        'draft_markdown': ('Reconciled ' if 'reviser' in label else 'Original ')
                            + row['section']['heading']} for row in view['sections']]}
                if label == 'report-document-reviewer':
                    return {'section_reviews': [{'section_id': row['section_id'],
                        'verdict': 'revise_required' if index < 2 else 'pass',
                        'findings': [{'finding_id': 'repeat-' + row['section_id'],
                            'type': 'style', 'severity': 'major', 'required_action': 'revise',
                            'message': 'Reconcile repetition across these sections.',
                            'draft_quotes': draft_quotes(prompt, row['section_id'])}]
                            if index < 2 else []} for index, row in enumerate(view['sections'])]}
                if label == 'report-document-verifier':
                    return {'section_reviews': [{'section_id': row['section_id'], 'verdict': 'pass'}
                        for row in view['sections']]}
                raise AssertionError(label)

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            feedback = {'contract': 'prior_prose_review.v1', 'path': str(Path(tmp) / 'old-review.json'),
                'sha256': 'a' * 64, 'scope': 'Old version opinions, not primary evidence or permission.',
                'section_reviews': [{'section_id': 'section_1', 'verdict': 'revise_required',
                    'revision_instructions': ['Reorganize the old conclusion under the requested structure.']}]}
            kwargs['context'].results['revision_feedback'] = feedback
            kwargs['context'].source_handles.append(SourceHandle(handle='old-body', kind='prior_draft',
                title='Old body', metadata={'draft_markdown': 'Old conclusion with bounded claims.'}))
            request = ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                kwargs['template'], Client(), output_outline=outline)
            capability = CapabilityContext(store, AttemptManifest('fixed-outline'))
            result = run_report_writing_capability(context=capability, request=request)
            self.assertEqual(result.status, 'completed', result.diagnostics)
            written = store.read_json('writer.json')
            self.assertEqual([row['heading'] for row in written['sections']], headings)
            self.assertEqual([row['heading'] for row in written['memory']['document_plan']['sections']], headings)
            self.assertTrue(written['sections'][0]['draft_markdown'].startswith('Reconciled '))
            self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer',
                                      'report-document-joint-reviser', 'report-document-verifier'])
            for label, view in views:
                old_review = (view['delivery_text_observation']['prior_document_revision']
                    if label in {'report-document-reviewer', 'report-document-verifier'}
                    else view['prior_document_revision'])
                self.assertEqual(old_review['feedback'], feedback)
                self.assertIn('fallible_allegations', old_review['review_opinions_status']['scope'])
                self.assertNotIn('revision_feedback', view.get('verified_execution_results', {}))
            self.assertEqual(views[0][1]['review_findings'], [])
            final_view = views[-1][1]['delivery_text_observation']['prior_document_revision']
            self.assertEqual(final_view['previous_document']['drafts'][0]['retained_body'],
                             'Old conclusion with bounded claims.')
            from simple_ar.report.narrative import evidence_outline_context
            from simple_ar.report.agent import _outline_planner_prompt, _reviewer_context
            seed = ReportMemory.model_validate(store.read_json('report_inputs.json')['memory'])
            for view in (evidence_outline_context(kwargs['context'], seed, kwargs['config']),
                         prompt_payload(_outline_planner_prompt(context=kwargs['context'], memory=seed,
                             config=kwargs['config'], template=kwargs['template'])),
                         _reviewer_context(context=kwargs['context'], memory=seed, config=kwargs['config'],
                             template=kwargs['template'], section=seed.section_plan[0],
                             draft=ReportSectionDraft.model_validate(written['sections'][0]))):
                self.assertEqual(view['prior_document_revision']['feedback'], feedback)
            body = assemble_report_sections(title='Report', sections=[
                ReportSectionDraft.model_validate(row) for row in written['sections']])
            self.assertEqual(article_section_headings(body), headings)
            article = {'constraints': {'outline': outline, 'max_cited_sources': 0},
                       'bibliography': [], 'citation_key_map': {}}
            _check_article_constraints(article, body)
            with self.assertRaisesRegex(ValueError, 'fixed section structure'):
                _check_article_constraints(article, body.replace(headings[-1], 'Conclusion and options'))
            checkpoint = store.ref('sections.json', kind='report_checkpoint', schema='report_checkpoint.v1')
            retained = {path.name: path.read_bytes() for path in store.root.glob('*.json')}
            with patch.object(request.llm_client, 'ask_json', side_effect=AssertionError('No paid request replay')):
                resumed = run_report_writing_capability(context=CapabilityContext(store,
                    AttemptManifest('fixed-outline'), inputs=(checkpoint,)),
                    request=replace(request, resume_ref=checkpoint, output_outline=()))
            self.assertEqual(resumed.status, 'completed', resumed.diagnostics)
            for name in ('report_inputs.json', 'writer.json'):
                self.assertEqual(store.resolve(name).read_bytes(), retained[name])
            before_checkpoint = json.loads(retained['sections.json'])['completed']
            self.assertEqual(store.read_json('sections.json')['completed'], before_checkpoint)
            retained = {path.name: path.read_bytes() for path in store.root.glob('*.json')}
            changed = copy.deepcopy(outline)
            changed[-1]['heading'] = 'Another conclusion'
            with self.assertRaisesRegex(ValueError, 'inputs changed'):
                run_report_writing_capability(context=capability, request=replace(request, output_outline=changed))
            self.assertEqual({path.name: path.read_bytes() for path in store.root.glob('*.json')}, retained)
        self.assertEqual(kwargs['memory'].model_dump(mode='json'), original_memory)

    def test_structured_article_explicit_headings_are_not_replaced_by_template(self):
        from simple_ar.report.writing import article_writing_inputs, _writing_binding

        kwargs = self.inputs()
        binding = _writing_binding(report_context=kwargs['context'], memory=kwargs['memory'],
            config=kwargs['config'], template=kwargs['template'], sources=())
        headings = ['Question', 'Evidence', 'Scope', 'Next decisions']
        arguments = dict(report_context=kwargs['context'], documents=None,
            binding=binding, config=kwargs['config'], title='Report', goal='Explain', target_words=0,
            projection_ref=ArtifactRef('report_snapshot.json', 'report_snapshot', 'report_snapshot.v1'))
        bound_outline = [{'heading': heading, 'required': True} for heading in headings]
        self.assertEqual(article_writing_inputs(**arguments, required_headings=headings)[
            'article']['constraints']['outline'], bound_outline)
        binding['memory']['outline_planning']['output_outline'] = bound_outline
        kwargs['config'].outline_strategy = 'auto'
        self.assertEqual(article_writing_inputs(**arguments)['article']['constraints']['outline'], bound_outline)
        with self.assertRaisesRegex(ValueError, 'frozen Writer output outline'):
            article_writing_inputs(**arguments, required_headings=['Different structure'])

    def test_native_sections_bind_feedback_and_fixed_outline_without_recovery_migration(self):
        from simple_ar.report.writing import write_section_article
        from simple_ar.report.narrative import PRIOR_REVIEW_CONTEXT_CONTRACT

        kwargs = self.inputs(draft_scope='document')
        kwargs['config'].outline_strategy = 'adaptive'
        kwargs['context'].results['revision_feedback'] = {'contract': 'prior_prose_review.v1',
            'path': 'old-review.json', 'sha256': 'b' * 64, 'scope': 'Old prose opinions.',
            'section_reviews': [{'section_id': 'old-conclusion', 'verdict': 'revise_required'}]}
        headings = ['Question', 'Observation', 'Scope', 'Follow-up choices']
        labels, views = [], []
        regular = self.client(labels)

        class Client:
            def ask_json(inner, system, prompt, **options):
                views.append(prompt_payload(prompt))
                return regular.ask_json(system, prompt, **options)

        with tempfile.TemporaryDirectory() as tmp:
            owner = ArtifactStore(Path(tmp))
            store = ArtifactStore(owner.root / 'writer')
            documents = owner.write_json('documents.json',
                DocumentBundle([], {}, {}, [], []).to_handoff_dict(),
                kind='documents', schema='document_bundle.v1')
            arguments = dict(store=store, input_store=owner, documents_ref=documents,
                identity='native-feedback', report_context=kwargs['context'], title='Report',
                target_words=0, required_headings=headings, max_cited_sources=0,
                prior_draft='Earlier complete body to reorganize.', review_config=kwargs['config'])
            written = write_section_article(**arguments, client=Client())
            self.assertEqual([row['heading'] for row in written['sections']], headings)
            frozen = store.read_json('report_inputs.json')
            self.assertEqual(frozen['memory']['survey_contract']['revision_feedback_contract'],
                             PRIOR_REVIEW_CONTEXT_CONTRACT)
            self.assertEqual(frozen['memory']['outline_planning']['output_outline'],
                             [{'heading': heading, 'required': True} for heading in headings])
            self.assertIn('prior_document_revision', views[0])
            self.assertIn('prior_document_revision', views[-1]['delivery_text_observation'])
            retained = {path.relative_to(store.root): path.read_bytes()
                        for path in store.root.rglob('*') if path.is_file()}
            recovered = write_section_article(**arguments, recovering=True)
            self.assertEqual(recovered, written)
            with self.assertRaisesRegex(ValueError, 'Frozen section input changed'):
                write_section_article(**{**arguments, 'required_headings': ['Different structure']}, recovering=True)
            self.assertEqual({path.relative_to(store.root): path.read_bytes()
                              for path in store.root.rglob('*') if path.is_file()}, retained)
            self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])

    def test_historical_unused_feedback_does_not_change_atomic_requests_on_resume(self):
        from simple_ar.report.writing import retained_writing_client

        kwargs = self.inputs(draft_scope='document')
        kwargs['context'].results['revision_feedback'] = {'contract': 'prior_prose_review.v1',
            'path': 'old-review.json', 'sha256': 'c' * 64, 'scope': 'Old opinions.',
            'section_reviews': [{'section_id': 'results', 'verdict': 'revise_required'}]}
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            labels = []
            client = retained_writing_client(store=store, identity='historical-feedback',
                client=self.client(labels), contract='atomic_complete.v2')
            request = ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                kwargs['template'], client)
            # Build the historical producer that retained the field but never
            # projected it. Recovery must not upgrade those paid requests.
            with patch('simple_ar.report.writing._section_feedback_memory',
                       side_effect=lambda memory, context, frozen_memory: memory):
                result = run_report_writing_capability(context=CapabilityContext(store,
                    AttemptManifest('historical-feedback')), request=request)
            self.assertEqual(result.status, 'completed', result.diagnostics)
            self.assertNotIn('revision_feedback_contract',
                             store.read_json('report_inputs.json')['memory']['survey_contract'])
            calls = {path.relative_to(store.root): path.read_bytes()
                     for path in store.root.rglob('*') if path.is_file() and 'model_calls' in path.parts}
            for path in store.resolve('model_calls').glob('*/request.json'):
                view = prompt_payload(json.loads(path.read_text(encoding='utf-8'))['prompt'])
                self.assertNotIn('prior_document_revision', view)
                self.assertNotIn('prior_document_revision', view.get('delivery_text_observation', {}))
            checkpoint = store.ref('sections.json', kind='report_checkpoint', schema='report_checkpoint.v1')
            replay = retained_writing_client(store=store, identity='historical-feedback',
                client=None, contract='atomic_complete.v2')
            resumed = run_report_writing_capability(context=CapabilityContext(store,
                AttemptManifest('historical-feedback'), inputs=(checkpoint,)),
                request=replace(request, llm_client=replay, resume_ref=checkpoint))
            self.assertEqual(resumed.status, 'completed', resumed.diagnostics)
            self.assertEqual({path.relative_to(store.root): path.read_bytes()
                for path in store.root.rglob('*') if path.is_file() and 'model_calls' in path.parts}, calls)
            self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])

    def test_composition_caption_ownership_selection_publication_and_recovery(self):
        from unittest.mock import Mock
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.writing import ComposeRequest, WritingToolGateway

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sources = root / 'inputs'
            sources.mkdir()
            table = sources / 'observations.csv'
            table.write_text('group,count\ncontrol,4\nvariant,3\n', encoding='utf-8')
            image = sources / 'comparison.svg'
            original = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
            image.write_bytes(original)
            caption = 'Observed *group counts*; supplied descriptive results. ' + (
                'Scope and original measurement conditions remain part of the caption. ' * 16)
            ledger = BudgetLedger()
            workspace = WorkspaceToolGateway(root, {'source0': sources}, ledger)
            client = Mock()
            request = {'goal': 'Explain the recorded observations.', 'title': 'Group comparison',
                'figures': [{'path': str(image), 'caption': caption}]}
            draft = {'draft_markdown': f'## Observations\n\n![{caption}](figures/selected-1.svg)\n\nInterpretation remains limited.'}
            request['materials'] = [{'path': str(table)}]
            client.ask_json.return_value = draft
            gateway = WritingToolGateway(root, workspace, ledger, 'retained-test-model', 'test',
                client_factory=lambda identity: client)
            identity = 'caption-composition'
            with patch('simple_ar.report.capability.run_report_capability',
                       side_effect=RuntimeError('assembly interrupted')):
                interrupted = gateway.call('compose_document', request, identity=identity)
            self.assertEqual(interrupted.status, 'error')
            self.assertIn('assembly interrupted', interrupted.error)
            owner = root / 'tools/writing' / identity
            binding = json.loads((owner / 'request.json').read_text(encoding='utf-8'))
            self.assertEqual(binding['figure_caption_contract'], 'pandoc_image.v1')
            self.assertEqual(binding['inputs'][-1]['caption'], caption)
            self.assertEqual(binding['inputs'][-1]['sha256'], hashlib.sha256(original).hexdigest())
            snapshot = json.loads((owner / 'writer/report_inputs.json').read_text(encoding='utf-8'))
            self.assertEqual(snapshot['owner'], 'direct_article.v1')
            retained_writer_bytes = {name: (owner / 'writer' / name).read_bytes()
                for name in ('report_inputs.json', 'response.json', 'writer.json')}
            self.assertEqual(json.loads(retained_writer_bytes['response.json']), draft)
            self.assertFalse((owner / 'writer/sections.json').exists())
            figure = next(row for row in snapshot['inputs']['article']['source_handles'] if row['handle'].startswith('figure:'))
            self.assertEqual(figure['metadata']['caption'], caption)
            # Metadata binds the supplied caption; summary carries the writer's
            # embedding instructions. Both describe one publication caption.
            self.assertIn('alt is the sole publication caption', figure['summary'])
            self.assertIn('do not repeat it in an adjacent paragraph', figure['summary'])
            self.assertIn('or assign a figure number', figure['summary'])
            self.assertTrue(figure['summary'].endswith(caption))
            prompt = json.loads(client.ask_json.call_args.args[1])
            self.assertEqual([row for row in prompt['source_handles']
                if row['handle'].startswith('figure:')], [figure])
            self.assertEqual(client.ask_json.call_count, 1)
            with patch.object(gateway, 'client_factory', side_effect=AssertionError('no model replay')):
                composed = gateway.call('compose_document', request, identity=identity)
            self.assertEqual(composed.status, 'ok', composed.error)
            self.assertEqual({name: (owner / 'writer' / name).read_bytes()
                for name in retained_writer_bytes}, retained_writer_bytes)
            manuscript = workspace.resolve(composed.data['editable_markdown'])
            manifest = manuscript.parent / 'figures/figures_manifest.json'
            self.assertEqual(json.loads(manifest.read_text(encoding='utf-8')),
                {'figures': [{'path': 'figures/selected-1.svg', 'caption': caption}]})
            self.assertIn(str(manifest.relative_to(root)).replace('\\', '/'), composed.artifacts)
            body = manuscript.read_text(encoding='utf-8')
            self.assertIn(f'![{caption}](figures/selected-1.svg)', body)
            self.assertEqual(body.count(caption), 1)
            self.assertEqual((manuscript.parent / 'figures/selected-1.svg').read_bytes(), original)
            self.assertEqual(image.read_bytes(), original)
            with self.assertRaises(ValueError):
                workspace.resolve(str(image), write=True)
            # Partial publication is reconciled only against the retained
            # assembly, including the caption manifest's exact bytes.
            receipt = owner / 'result.json'
            receipt_bytes = receipt.read_bytes()
            receipt.unlink()
            manifest_bytes = manifest.read_bytes()
            manifest.write_text('{}', encoding='utf-8')
            with patch.object(gateway, 'client_factory', side_effect=AssertionError('no model replay')):
                rejected = gateway.call('compose_document', request, identity=identity)
                self.assertEqual(rejected.status, 'error')
                self.assertIn('Retained publication changed', rejected.error)
                manifest.unlink()
                restored = gateway.call('compose_document', request, identity=identity)
            self.assertEqual(restored.status, 'ok', restored.error)
            self.assertEqual(manifest.read_bytes(), manifest_bytes)
            self.assertEqual(receipt.read_bytes(), receipt_bytes)
            changed = copy.deepcopy(request)
            changed['figures'][0]['caption'] = 'Changed caption.'
            self.assertEqual(gateway.call('compose_document', changed, identity=identity).status, 'error')
            self.assertEqual(ledger.entries, ())
        # The legacy request still serializes bare paths, not synthetic captions.
        legacy = ComposeRequest.model_validate({'goal': 'Explain', 'title': 'Report', 'figures': ['old.png']})
        self.assertEqual(legacy.model_dump()['figures'], ['old.png'])
        for figures in ([{'path': 'old.png', 'caption': '   '}],
                        ['old.png', {'path': 'old.png', 'caption': 'Caption'}]):
            with self.assertRaises(ValidationError):
                ComposeRequest.model_validate({'goal': 'Explain', 'title': 'Report', 'figures': figures})

    def inputs(self, count=2, scope="document", **updates):
        context = ReportContext(topic="A bounded account", report_mode="supplied_materials")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id=sid, heading=sid,
            goal="Explain the evidence", draft_order=i, final_order=i)
            for i, sid in enumerate(("results", "limits")[:count], 1)])
        config = ReportRuntimeConfig(outline_strategy="template", document_review=True,
            review_scope=scope, max_review_iterations=1, **updates)
        return agent_inputs(context, memory, config)

    def client(self, labels, *, fail=False):
        class Client:
            def ask_json(self, system, prompt, *, label="", **unused):
                labels.append(label)
                view = prompt_payload(prompt)
                if label == "report-document-reviewer":
                    if fail:
                        raise LLMError("Document review unavailable")
                    return {"section_reviews": []}
                if label == "report-document-verifier":
                    return {"section_reviews": []}
                if "reviewer" in label or "verifier" in label:
                    return {"verdict": "pass"}
                if label == "report-writer-document":
                    return {"sections": [{"section_id": row["section"]["section_id"],
                        "heading": row["section"]["heading"], "draft_markdown": "A bounded observation."}
                        for row in view["sections"]]}
                sid = view["section"]["section_id"]
                return {"section_id": sid, "heading": sid,
                    "draft_markdown": "Revised bounded result." if "reviser" in label else "Only recorded observations are described."}
        return Client()

    def test_complete_body_precedes_review_and_finished_resume_spends_nothing(self):
        kwargs, labels, saved = self.inputs(), [], []
        client = self.client(labels)
        result = run_report_agent(**kwargs, client=client, checkpoint_sink=saved.append)
        self.assertFalse(any(row.action in {"review", "review_revision"} for row in result.iterations))
        self.assertTrue(saved[-1]["document_review_done"])
        call_count = len(labels)
        resumed = run_report_agent(**kwargs, client=client, completed_checkpoint=saved[-1])
        self.assertEqual(result.report_body, resumed.report_body)
        self.assertEqual(len(labels), call_count)
        for topic in ("A lengthy task " * 80, "Question\nConstraints"):
            with self.subTest(topic=topic[:30]):
                task = self.inputs()
                task['context'].topic = topic
                report = run_report_agent(**task, client=self.client([]))
                self.assertEqual(report.memory.document_plan.title, 'Supplied Material Report')
                self.assertTrue(report.report_body.startswith('# Supplied Material Report\n'))

        # Auto uses the same two composition paths, with a stable full-plan
        # decision. Unknown or long plans preserve per-section recovery.
        for target, expected in ((0, ['report-writer-results', 'report-writer-limits']),
                                 (1000, ['report-writer-document']),
                                 (1001, ['report-writer-results', 'report-writer-limits'])):
            with self.subTest(target=target):
                automatic = self.inputs(draft_scope='auto')
                for section in automatic['memory'].section_plan:
                    section.target_words = target
                calls = []
                run_report_agent(**automatic, client=self.client(calls))
                self.assertEqual(calls, [*expected, 'report-document-reviewer'])

    def test_interrupted_drafting_keeps_completed_prefix_and_waits_for_whole_body(self):
        kwargs, labels, saved = self.inputs(draft_scope='auto'), [], []
        for section in kwargs['memory'].section_plan:
            section.target_words = 1500
        def interrupt(row):
            saved.append(row)
            if len(row["sections"]) == 1:
                raise RuntimeError("Stopped after first draft")
        client = self.client(labels)
        with self.assertRaisesRegex(RuntimeError, "first draft"):
            run_report_agent(**kwargs, client=client, checkpoint_sink=interrupt)
        before = copy.deepcopy(saved[-1])
        self.assertIsNone(before["pending_draft"])
        self.assertFalse(before["document_review_done"])
        result = run_report_agent(**kwargs, client=client, completed_checkpoint=saved[-1])
        self.assertEqual(saved[-1], before)
        self.assertEqual(labels, ["report-writer-results", "report-writer-limits", "report-document-reviewer"])
        self.assertEqual(len(result.sections), 2)

    def test_first_writer_failure_keeps_plan_without_claiming_any_review_or_replanning(self):
        kwargs, saved = self.inputs(), []
        from unittest.mock import Mock
        client = Mock()
        client.ask_json.side_effect = LLMError("Provider unavailable before first section")
        with patch("simple_ar.report.agent._maybe_adapt_outline", side_effect=lambda **values: values["memory"]) as planner:
            with self.assertRaisesRegex(LLMError, "Provider unavailable"):
                run_report_agent(**kwargs, client=client, checkpoint_sink=saved.append)
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["sections"], [])
        self.assertEqual(saved[0]["iterations"], [])
        self.assertEqual(saved[0]["reviewer_findings"], [])
        self.assertFalse(saved[0]["document_review_done"])
        labels = []
        with patch("simple_ar.report.agent._maybe_adapt_outline", side_effect=AssertionError("Do not bill a second plan")):
            result = run_report_agent(**kwargs, client=self.client(labels), completed_checkpoint=saved[0])
        self.assertEqual(len(result.sections), 2)
        self.assertEqual(labels[0], "report-writer-results")

    def test_refused_section_revision_resumes_saved_opinion_without_review_replay(self):
        # A single-document section uses the section reviewer. Its completed
        # opinion must survive a refused revision without another review call.
        kwargs, interrupted, calls = self.inputs(count=1, scope="section"), [], []
        kwargs['config'] = kwargs['config'].model_copy(update={'allow_llm_fallback': False})
        regular = self.client(calls)
        class InterruptedRevision:
            def ask_json(self, system, prompt, *, label='', **options):
                if label == 'report-reviewer-results':
                    calls.append(label)
                    return {'verdict': 'revise_required', 'revision_instructions': ['Clarify the observation.']}
                if 'reviser' in label:
                    raise LLMError('Budget refused before sending revision')
                return regular.ask_json(system, prompt, label=label, **options)
        with self.assertRaises(LLMError):
            run_report_agent(**kwargs, client=InterruptedRevision(),
                checkpoint_sink=lambda row: interrupted.append(copy.deepcopy(row)))
        self.assertEqual(interrupted[-1]['iterations'][-1]['action'], 'review')
        self.assertEqual(interrupted[-1]['iterations'][-1]['revision_instructions'], ['Clarify the observation.'])
        resumed_calls = []
        restored = run_report_agent(**kwargs, client=self.client(resumed_calls),
            completed_checkpoint=interrupted[-1])
        self.assertEqual(resumed_calls[0], 'report-reviser-results-round-1')
        self.assertNotIn('report-reviewer-results', resumed_calls)
        self.assertEqual(sum(row.action == 'review' for row in restored.iterations), 1)
        self.assertIn('Revised bounded result', restored.report_body)

    def test_normal_revision_recheck_and_finished_resume_share_current_reference_owner(self):
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document
        kwargs, saved, views = self.inputs(), [], []
        kwargs["context"].papers = [{"id": "first", "title": "Original cited source"},
                                    {"id": "second", "title": "Replacement cited source"}]
        kwargs["context"].citation_key_map = {"P1": "first", "P2": "second"}
        class Client:
            def ask_json(self, system, prompt, *, label="", **unused):
                view = prompt_payload(prompt)
                views.append((label, view))
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "results", "verdict": "revise_required",
                        "revision_instructions": ["Use the replacement source."], "findings": [{
                            "finding_id": "attribution", "type": "style", "severity": "major",
                            "required_action": "revise", "section_id": "results", "message": "Revise the attribution."}]}]}
                if "reviewer" in label or "verifier" in label:
                    return {"section_reviews": []} if "section" not in view else {"verdict": "pass"}
                sid = view["section"]["section_id"]
                text = "Revised statement [@P2]." if "reviser" in label else (
                    "Initial statement [@P1]." if sid == "results" else "A bounded limitation.")
                return {"section_id": sid, "heading": sid, "draft_markdown": text}
        client = Client()
        result = run_report_agent(**kwargs, client=client, checkpoint_sink=saved.append)
        inspected = [view for label, view in views if label in {"report-document-reviewer", "report-document-verifier"}]
        self.assertEqual(inspected[0]["delivery_text_observation"]["references"]["citation_numbers"], {"first": 1})
        self.assertEqual(inspected[-1]["delivery_text_observation"]["references"]["citation_numbers"], {"second": 1})
        observed = delivery_text_observation(kwargs["context"], result.memory, result.sections, kwargs["config"])
        canonical = preview_report_document(ReportAssemblyRequest(title=kwargs["context"].topic,
            sections=tuple(result.sections), config=kwargs["config"], document_plan=result.memory.document_plan,
            papers=tuple(kwargs["context"].papers), citation_key_map=kwargs["context"].citation_key_map))
        self.assertEqual(observed["references"]["markdown"], canonical.references_markdown)
        before = copy.deepcopy(saved[-1])
        call_count = len(views)
        restored = run_report_agent(**kwargs, client=client, completed_checkpoint=saved[-1])
        self.assertEqual(restored.report_body, result.report_body)
        self.assertEqual(len(views), call_count)
        self.assertEqual(saved[-1], before)

    def test_document_failure_is_a_finding_not_a_fabricated_pass(self):
        kwargs = self.inputs()
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / 'first')
            request = ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                kwargs['template'], self.client([], fail=True))
            failed = run_report_writing_capability(context=CapabilityContext(store=store,
                attempt=AttemptManifest('write-1'), inputs=(
                    ArtifactRef('brief-r1.json', kind='research_brief'),
                    ArtifactRef('measurement-1.json', kind='experiment_measurement'))), request=request)
            self.assertEqual(failed.status, 'failed')
            checkpoint = next(row for row in failed.artifacts if row.kind == 'report_checkpoint')
            # Simulate a pre-normalization fingerprint; full saved content is
            # still required to adopt its unfinished work.
            previous = store.read_json(checkpoint)
            self.assertFalse(previous['completed']['document_review_done'])
            self.assertTrue(any(row['type'] == 'document_review_unavailable'
                for row in previous['completed']['memory']['reviewer_findings']))
            previous['snapshot_id'] = 'legacy-revision-location-fingerprint'
            store.write_json(checkpoint.path, previous)
            labels = []
            resumed = run_report_writing_capability(context=CapabilityContext(
                store=ArtifactStore(Path(directory) / 'second'), attempt=AttemptManifest('write-2'),
                inputs=(checkpoint, ArtifactRef('brief-r2.json', kind='research_brief'),
                        ArtifactRef('measurement-1.json', kind='experiment_measurement')), input_store=store),
                request=replace(request, llm_client=self.client(labels), resume_ref=checkpoint))
            self.assertEqual(resumed.status, 'completed')
            self.assertEqual(labels, ['report-document-reviewer'])
            writer_ref = next(row for row in resumed.artifacts if row.kind == 'report_writer_result')
            restored = ArtifactStore(Path(directory) / 'second').read_json(writer_ref)
            self.assertEqual(restored['sections'], previous['completed']['sections'])
            self.assertFalse(any(row['type'] == 'document_review_unavailable'
                for row in restored['memory']['reviewer_findings']))
            with patch('simple_ar.report.writing.run_report_agent', return_value=None) as writer:
                run_report_writing_capability(context=CapabilityContext(
                    store=ArtifactStore(Path(directory) / 'changed'), attempt=AttemptManifest('write-3'),
                    inputs=(checkpoint, ArtifactRef('measurement-2.json', kind='experiment_measurement')),
                    input_store=store), request=replace(request, resume_ref=checkpoint))
            self.assertIsNone(writer.call_args.kwargs['completed_checkpoint'])

    def test_single_current_document_and_legacy_default_select_their_review_owner(self):
        for count, scope, draft_scope in ((1, "document", "section"),
                                          (1, "document", "document"), (2, "section", "section")):
            with self.subTest(count=count, scope=scope, draft_scope=draft_scope):
                labels = []
                run_report_agent(**self.inputs(count=count, scope=scope, draft_scope=draft_scope), client=self.client(labels))
                current_document = scope == 'document' and draft_scope == 'document'
                self.assertIn('report-document-reviewer' if current_document else 'report-reviewer-results', labels)
                if current_document:
                    self.assertNotIn('report-reviewer-results', labels)
                elif count == 1:
                    self.assertNotIn('report-document-reviewer', labels)
        self.assertEqual(ReportRuntimeConfig().review_scope, "section")


    def test_pre_option_checkpoint_identity_survives_default_but_not_scope_change(self):
        objects = self.inputs(scope="section")
        request = ReportWritingRequest(objects["context"], objects["memory"], objects["config"], objects["template"], object())
        saved = {"sections": [{"section_id": "results", "draft_markdown": "Saved draft."}]}
        def interrupted(**kwargs):
            kwargs["checkpoint_sink"](saved)
            return None
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory))
            first = CapabilityContext(store=store, attempt=AttemptManifest("write-1"))
            with patch("simple_ar.report.writing.run_report_agent", side_effect=interrupted):
                result = run_report_writing_capability(context=first, request=request)
            snapshot = store.read_json("report_inputs.json")
            identity = {key: value for key, value in snapshot.items() if key != "snapshot_id"}
            identity["template"] = {key: value for key, value in identity["template"].items() if key not in {"template_path", "criteria_path"}}
            identity["config"] = {key: value for key, value in identity["config"].items() if key not in {"review_scope", "draft_scope"}}
            old_identity = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self.assertEqual(snapshot["snapshot_id"], old_identity)
            checkpoint = next(ref for ref in result.artifacts if ref.kind == "report_checkpoint")
            for scope in ("section", "document"):
                with self.subTest(scope=scope):
                    retry = CapabilityContext(store=ArtifactStore(Path(directory) / scope),
                        attempt=AttemptManifest("write-2"), inputs=(checkpoint,), input_store=store)
                    resumed = replace(request, config=request.config.model_copy(update={"review_scope": scope}), resume_ref=checkpoint)
                    with patch("simple_ar.report.writing.run_report_agent", side_effect=interrupted) as writer:
                        run_report_writing_capability(context=retry, request=resumed)
                    self.assertEqual(writer.call_args.kwargs["completed_checkpoint"], saved if scope == "section" else None)


class JointDraftingTests(unittest.TestCase):
    def inputs(self):
        context = ReportContext(topic='A scientific comparison', report_mode='supplied_materials',
            goal_markdown='Write a coherent account using all supplied evidence, with conditions and limitations.')
        plans = [ReportSectionPlan(section_id=sid, heading=sid.title(), goal='Answer a distinct part of the question',
            evidence_handles=[f'material:{sid}'], draft_order=i, final_order=i, target_words=100)
            for i, sid in enumerate(('comparison', 'interpretation', 'scope'), 1)]
        memory = ReportMemory(section_plan=plans, source_handles=[SourceHandle(handle=f'material:{row.section_id}',
            kind='material', title=row.heading, summary=f'Original evidence for {row.section_id}') for row in plans])
        config = ReportRuntimeConfig(outline_strategy='template', document_review=True, review_scope='document',
            draft_scope='document', max_review_iterations=0)
        return agent_inputs(context, memory, config)

    def client(self, labels, requests, *, bad=None):
        class Client:
            def ask_json(self, system, prompt, *, label='', **kwargs):
                labels.append(label)
                view = prompt_payload(prompt)
                requests.append(view)
                if label == 'report-document-reviewer':
                    return {'section_reviews': []}
                if label == 'report-writer-document' and bad:
                    if isinstance(bad, Exception):
                        raise bad
                    return bad
                sections = [row['section'] for row in view['sections']]
                return {'sections': [{'section_id': row['section_id'], 'heading': row['heading'],
                    'draft_markdown': f"Distinct bounded account for {row['section_id']}.",
                    'used_sources': row['evidence_handles']} for row in reversed(sections)]}
        return Client()

    def test_one_composition_contains_all_owners_then_independent_review(self):
        kwargs, labels, requests, saved = self.inputs(), [], [], []
        comparisons = [{"dimension": "Matched subset", "relation": "conditional_difference",
            "observations": ["Method A and B were measured on the same subset."],
            "conditions": ["This does not establish a comparison on the unmatched full datasets."],
            "interpretation": "Use the matched comparison, not a source-by-source summary."}]
        kwargs['context'].source_comparisons = comparisons
        original = kwargs['memory'].model_dump(mode='json')
        result = run_report_agent(**kwargs, client=self.client(labels, requests), checkpoint_sink=saved.append)
        self.assertEqual([row.section_id for row in result.sections], ['comparison', 'interpretation', 'scope'])
        writer = requests[0]
        self.assertEqual(writer['source_comparisons'], comparisons)
        self.assertEqual(writer['comparison_status']['independent_verification'], 'not_performed')
        self.assertNotIn('source_comparisons', requests[1])  # Independent review is not anchored to this interpretation.
        self.assertNotIn('section', writer)
        self.assertNotIn('narrative_context', writer)  # No competing single-section edit scope.
        self.assertEqual({row['handle'] for row in writer['source_handles']},
            {'material:comparison', 'material:interpretation', 'material:scope'})
        self.assertIn('shared limitations', ' '.join(writer['response_rules']))
        self.assertNotIn('Return the outer section object', ' '.join(writer['style_rules']))
        self.assertEqual(kwargs['memory'].model_dump(mode='json'), original)
        self.assertEqual([len(row['sections']) for row in saved[:2]], [0, 3])
        self.assertFalse(any(row.action in {'review', 'review_revision'} for row in result.iterations))
        self.assertTrue(saved[-1]['document_review_done'])

    def test_missing_or_duplicate_set_is_corrected_without_partial_adoption(self):
        for bad in ({'sections': [{'section_id': 'comparison', 'draft_markdown': 'Partial'}]},
                    {'sections': [{'section_id': 'comparison'}, {'section_id': 'comparison'}]},
                    {'sections': [{'section_id': 'another'}]},
                    LLMResponseError('Completed response is not JSON')):
            with self.subTest(bad=bad):
                labels, requests, saved = [], [], []
                run_report_agent(**self.inputs(), client=self.client(labels, requests, bad=bad), checkpoint_sink=saved.append)
                self.assertEqual(labels[:2], ['report-writer-document', 'report-writer-document-retry'])
                self.assertEqual(len(saved[0]['sections']), 0)
                self.assertEqual(len(saved[1]['sections']), 3)
                self.assertEqual('rejected_response' in requests[1], not isinstance(bad, Exception))
                self.assertIn('validation_error', requests[1])

    def test_joint_scope_is_built_without_single_section_projection_or_table_leakage(self):
        kwargs, labels, requests = self.inputs(), [], []
        kwargs['memory'].document_plan = ReportDocumentPlan(sections=kwargs['memory'].section_plan,
            visual_intents=[ReportVisualIntent(visual_id='comparison-table', kind='table',
                title='Supported comparison', purpose='Compare the supplied evidence', section_id='comparison')])
        with patch('simple_ar.report.agent.narrative_context', side_effect=AssertionError('Joint calls have no single-section scope')):
            run_report_agent(**kwargs, client=self.client(labels, requests))
        writer = requests[0]
        self.assertNotIn('visual_requirements', writer)
        self.assertTrue(writer['sections'][0]['visual_requirements']['tables'])
        self.assertFalse(writer['sections'][1]['visual_requirements']['tables'])
        rules = ' '.join(writer['style_rules'])
        self.assertIn('designated section', rules)
        self.assertNotIn('table in this section', rules)
        self.assertNotIn('long survey', rules)

    def test_argument_plan_is_shared_fallible_intent_not_a_requirement_to_assert(self):
        from simple_ar.report.schema import ReportArgumentPlan, ReportArgumentPoint
        kwargs, labels, requests = self.inputs(), [], []
        argument = ReportArgumentPlan(question='Does the evidence justify the proposed explanation?',
            answer='A provisional explanation that primary evidence may contradict.',
            points=[ReportArgumentPoint(claim='An unconfirmed mechanism', section_id='interpretation')])
        kwargs['memory'].document_plan = ReportDocumentPlan(sections=kwargs['memory'].section_plan,
            argument_plan=argument)
        before = kwargs['memory'].model_dump(mode='json')
        run_report_agent(**kwargs, client=self.client(labels, requests))
        self.assertEqual(requests[0]['document_plan']['argument_plan'], argument.model_dump(mode='json'))
        self.assertNotIn('argument_plan', requests[1]['document_plan'])
        self.assertNotIn(argument.answer, json.dumps(requests[1]))
        for view in requests:
            self.assertEqual(view['document_plan']['planning_status']['independent_verification'], 'not_performed')
            self.assertEqual(view['document_plan']['planning_status']['scope'],
                             'organizational_intent_not_current_prose_or_scientific_support')
            self.assertIn('not scientific truth', ' '.join(view['document_plan']['interpretation_rules']))
            self.assertIn('Correct or omit', ' '.join(view['document_plan']['interpretation_rules']))
        self.assertEqual(requests[0]['document_plan']['interpretation_rules'],
            requests[1]['document_plan']['interpretation_rules'])
        self.assertEqual(kwargs['memory'].model_dump(mode='json'), before)

    def test_genre_rules_follow_template_without_rewriting_custom_guidance(self):
        from simple_ar.report.agent import _writer_payload, _reviewer_context
        from simple_ar.report.schema import ReportSectionDraft
        for name in ('material_report', 'reproduction', 'survey', 'survey_long', 'custom', 'custom-survey-name'):
            with self.subTest(template=name):
                kwargs = self.inputs()
                template = kwargs['template'].model_copy(update={'name': 'survey_long' if name == 'custom-survey-name' else name,
                    'template_markdown': 'Custom authored content and structure.'})
                if name == 'custom-survey-name':
                    kwargs['config'] = kwargs['config'].model_copy(update={'template': 'user/survey_long.md'})
                for section, selected in ((kwargs['memory'].section_plan[0], None), (None, kwargs['memory'].section_plan)):
                    view = _writer_payload(context=kwargs['context'], memory=kwargs['memory'], template=template,
                        section=section, document_sections=selected, config=kwargs['config'], extra_context=[],
                        previous_draft=None, review=None, source_batch_index=1, source_batch_count=1,
                        include_previous_draft=False, draft_mode='document' if selected else 'section')
                    rules = ' '.join(view['style_rules'])
                    self.assertEqual('Synthesize the source set' in rules, name in {'survey', 'survey_long'})
                    self.assertEqual('construction, applications' in rules, name == 'survey_long')
                    self.assertEqual(view['template_markdown'], template.template_markdown)
                    self.assertIn('not an observation', rules)
                plan = kwargs['memory'].section_plan[0]
                review = _reviewer_context(context=kwargs['context'], memory=kwargs['memory'], template=template,
                    section=plan, draft=ReportSectionDraft(section_id=plan.section_id, heading=plan.heading,
                        draft_markdown='Bounded scientific explanation.'), config=kwargs['config'])
                self.assertEqual('Does the survey synthesize' in ' '.join(review['review_focus']), name in {'survey', 'survey_long'})
                self.assertEqual('does the long survey cover' in ' '.join(review['review_focus']), name == 'survey_long')


    def test_saved_joint_body_resumes_review_without_writing_again(self):
        kwargs, labels, requests, saved = self.inputs(), [], [], []
        client = self.client(labels, requests)
        def stop(row):
            saved.append(row)
            if len(row['sections']) == 3:
                raise RuntimeError('Saved entire joint body')
        with self.assertRaisesRegex(RuntimeError, 'entire joint body'):
            run_report_agent(**kwargs, client=client, checkpoint_sink=stop)
        prior = copy.deepcopy(saved[-1])
        result = run_report_agent(**kwargs, client=client, completed_checkpoint=prior)
        self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])
        self.assertEqual(prior, saved[-1])
        complete = {**prior, 'memory': result.memory.model_dump(mode='json'), 'document_review_done': True}
        class Offline:
            def ask_json(self, *args, **kwargs):
                raise AssertionError('No model on completed restore')
        self.assertEqual(run_report_agent(**kwargs, client=Offline(), completed_checkpoint=complete).report_body, result.report_body)
        # Intent was persisted but no correction response returned (e.g. the
        # reservation was refused). Explicit resume checks the same body once.
        pending = copy.deepcopy(prior)
        rejection_iteration = max(row['iteration'] for row in pending['iterations']) + 1
        pending['iterations'] += [
            {'iteration': rejection_iteration, 'section_id': '', 'action': 'document_review_rejected',
             'status': 'rejected', 'summary': 'Invalid review structure',
             'rejected_review': {'label': 'report-document-reviewer', 'response': {'reviews': []}}},
            {'iteration': rejection_iteration + 1, 'section_id': '', 'action': 'document_format_correction',
             'status': 'started', 'summary': 'Invalid review structure',
             'rejected_review': {'label': 'report-document-reviewer', 'format_correction': True,
                                'rejection_iteration': rejection_iteration}},
        ]
        labels.clear()
        correction_labels = []
        class CorrectionClient:
            def ask_json(self, *args, label='', **options):
                correction_labels.append(label)
                return client.ask_json(*args, label='report-document-reviewer', **options)
        restored = run_report_agent(**kwargs, client=CorrectionClient(), completed_checkpoint=pending)
        self.assertEqual(correction_labels, ['report-document-reviewer-format-correction'])
        self.assertEqual(restored.report_body, result.report_body)


    def test_writer_reads_before_composing_and_keeps_one_shared_trace(self):
        kwargs, labels, prompts, saved = self.inputs(), [], [], []
        regular = self.client(labels, prompts)
        call = {'tool_name': 'search_source_chunks', 'arguments': {'handle': 'material:comparison', 'query': 'ranking conditions'}}
        class Reader:
            def ask_json(self, system, prompt, *, label='', **options):
                payload = prompt_payload(prompt)
                if label == 'report-writer-document' and payload['source_reading']['available']:
                    self.payload = payload
                    return {'context_requests': [call]}
                return regular.ask_json(system, prompt, label=label, **options)
        client = Reader()
        evidence = ReportToolResult(tool_name=call['tool_name'], status='ok', content={'text': 'Actual ranking condition'})
        with patch.object(kwargs['gateway'], 'call', return_value=evidence) as read:
            result = run_report_agent(**kwargs, client=client, checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
        read.assert_called_once()
        self.assertTrue(client.payload['source_reading']['available'])
        writer = prompts[0]
        self.assertEqual(labels, ['report-writer-document-evidence', 'report-document-reviewer'])
        self.assertFalse(writer['source_reading']['available'])
        self.assertEqual(writer['extra_tool_context'][0]['content']['text'], 'Actual ranking condition')
        self.assertEqual(len(result.tool_results), 1)
        event = next(row for row in result.iterations if row.action == 'writer_context')
        self.assertEqual(event.tool_requests[0].arguments, call['arguments'])
        self.assertTrue(any(row['iterations'][-1].get('tool_results', [{}])[0].get('metadata', {}).get('lookup_state') == 'allocated'
                            for row in saved if row['iterations'] and row['iterations'][-1]['action'] == 'writer_context'
                            and row['iterations'][-1]['tool_results']))
        # Newly navigable excerpted primary text selects evidence separately
        # from composition. Both a read and an accepted empty batch survive
        # interruption without another selection call or refreshed allowance.
        for kind, selected in (("paper", [call]), ("material", [call]),
                               ("paper", []), ("material", [])):
            kwargs, saved, labels, prompts = self.inputs(), [], [], []
            kwargs['memory'].source_handles[0] = kwargs['memory'].source_handles[0].model_copy(update={
                'kind': kind, 'metadata': {'source_directory': {'sections': []},
                                             'evidence_passages_truncated': True}})
            class Selector:
                def ask_json(self, system, prompt, *, label='', **options):
                    self_payload = prompt_payload(prompt)
                    self.assert_contract = self_payload
                    if label != 'report-source-selection':
                        raise AssertionError('Composition must follow saved selection')
                    return {'context_requests': selected}
            selector = Selector()
            save_selection = interrupt_checkpoint(saved, lambda row: any(
                event['action'] == 'writer_context' and event['status'] == 'completed'
                for event in row['iterations']))
            with patch.object(kwargs['gateway'], 'call', return_value=evidence) as read:
                with self.assertRaises(CheckpointStop):
                    run_report_agent(**kwargs, client=selector, checkpoint_sink=save_selection)
                self.assertEqual(read.call_count, len(selected))
            self.assertNotIn('sections', selector.assert_contract)
            self.assertIn('tool_name', selector.assert_contract['context_request_schema']['properties'])
            with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Do not replay reads')):
                resumed = run_report_agent(**kwargs, client=self.client(labels, prompts),
                                           completed_checkpoint=saved[-1])
            self.assertNotIn('report-source-selection', labels)
            self.assertEqual(len(resumed.tool_results), len(selected))

    def planning_inputs(self, *, scope='document'):
        kwargs = self.inputs()
        kwargs['config'] = kwargs['config'].model_copy(update={'outline_strategy': 'adaptive', 'draft_scope': scope})
        kwargs['memory'].source_handles[0].metadata['document_id'] = 'measurement'
        kwargs['context'].source_handles = kwargs['memory'].source_handles
        bundle = DocumentBundle(records=[], sections=[], fulltext_manifest={}, fulltext_extraction={}, chunks=[
            TextChunk(chunk_id='definition', document_id='measurement', text='The calibrated rate equals raw counts divided by exposure time. Density normalization instead divides by bin width.',
                source_path='measurement.txt', line_start=12)])
        kwargs['gateway'] = ReportToolGateway(kwargs['context'], documents=bundle)
        plan = {'sections': [{'heading': row.heading, 'goal': row.goal, 'evidence_handles': row.evidence_handles,
                              'target_words': row.target_words} for row in kwargs['memory'].section_plan],
                'context_requests': [{'tool_name': 'search_source_chunks', 'arguments': {
                    'handle': 'material:comparison', 'query': 'calibrated rate counts exposure density normalization'}}]}
        return kwargs, plan

    def planned_client(self, plan, labels, prompts, *, fail=False):
        class Client:
            def ask_json(self, system, prompt, *, label='', **unused):
                labels.append(label)
                payload = prompt_payload(prompt)
                prompts.append(payload)
                if label.startswith('report-outline-planner'):
                    return plan
                if fail:
                    raise LLMError('Writer transport stopped after planning reads')
                if label == 'report-document-reviewer':
                    return {'section_reviews': []}
                rows = [row['section'] for row in payload['sections']] if 'sections' in payload else [payload['section']]
                drafts = [{'section_id': row['section_id'], 'heading': row['heading'],
                           'draft_markdown': 'Counts and rate are different quantities.', 'used_sources': row['evidence_handles']} for row in rows]
                return {'sections': drafts} if 'sections' in payload else drafts[0]
        return Client()

    def test_planned_definition_reads_reach_both_draft_scopes_and_document_review(self):
        # Scope dataflow is shared after planning; auto only changes planner eligibility.
        for scope, strategy in (('document', 'adaptive'), ('section', 'adaptive'),
                                ('document', 'auto')):
            with self.subTest(scope=scope, strategy=strategy):
                kwargs, plan = self.planning_inputs(scope=scope)
                kwargs['config'] = kwargs['config'].model_copy(update={'outline_strategy': strategy})
                kwargs['context'].results['supplied_analyses'] = [{
                    'schema_version': 'code_analysis.v1', 'document_id': 'measurement',
                    'evidence_role': 'recorded_script_results',
                    'figures': [{'path': 'outputs/comparison.png', 'caption': 'Observed counts and rates.'}]}]
                plan['visual_intents'] = [{'kind': 'figure', 'view': 'supplied-data',
                    'section_heading': kwargs['memory'].section_plan[0].heading,
                    'title': 'Counts and rates', 'purpose': 'Show the observed comparison',
                    'evidence_handles': ['material:comparison'],
                    'figure_paths': ['outputs/comparison.png']}]
                labels, prompts, saved = [], [], []
                original = copy.deepcopy(kwargs['gateway'].documents.to_handoff_dict())
                result = run_report_agent(**kwargs, client=self.planned_client(plan, labels, prompts),
                    checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
                self.assertIn('context_requests', prompts[0]['output_schema'])
                self.assertEqual(labels.count('report-outline-planner'), 1)
                self.assertEqual(kwargs['gateway'].call_counts['search_source_chunks'], 1)
                self.assertEqual(len(result.tool_results), 1)
                self.assertEqual(result.memory.document_plan.visual_intents[0].figure_paths,
                                 ['outputs/comparison.png'])
                for payload in prompts[1:-1]:
                    self.assertIn('divided by exposure time', json.dumps(payload['extra_tool_context']))
                self.assertIn('divided by exposure time', json.dumps(prompts[-1]['supplementary_evidence']))
                self.assertEqual(prompts[1].get('source_reading', {}).get('available', False), scope == 'document')
                self.assertEqual(kwargs['gateway'].documents.to_handoff_dict(), original)
                self.assertTrue(saved[0]['memory']['outline_planning']['context_requests'])
                self.assertEqual(saved[0]['tool_results'], [])
        # No figures, disabled figures, explicit templates and accepted plans
        # do not create a new planning obligation on the direct writing path.
        for mode in ('text', 'disabled', 'off', 'template'):
            kwargs, _ = self.planning_inputs()
            kwargs['config'].outline_strategy = 'template' if mode == 'template' else 'auto'
            if mode != 'text':
                kwargs['context'].results['supplied_analyses'] = [{
                    'schema_version': 'code_analysis.v1', 'document_id': 'measurement',
                    'evidence_role': 'recorded_script_results',
                    'figures': [{'path': 'outputs/comparison.png', 'caption': 'Observed counts and rates.'}]}]
            if mode == 'disabled':
                kwargs['config'].figures.enabled = False
            elif mode == 'off':
                kwargs['config'].figures.mode = 'off'
            labels, prompts = [], []
            run_report_agent(**kwargs, client=self.client(labels, prompts))
            self.assertNotIn('report-outline-planner', labels)

    def test_planned_read_failure_restore_uses_same_plan_and_confirmed_results(self):
        kwargs, plan = self.planning_inputs()
        labels, prompts, saved = [], [], []
        with self.assertRaisesRegex(LLMError, 'after planning reads'):
            run_report_agent(**kwargs, client=self.planned_client(plan, labels, prompts, fail=True),
                checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
        self.assertEqual(kwargs['gateway'].call_counts['search_source_chunks'], 1)
        labels, prompts = [], []
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Do not replay confirmed reads')):
            result = run_report_agent(**kwargs, client=self.planned_client(plan, labels, prompts), completed_checkpoint=saved[-1])
        self.assertNotIn('report-outline-planner', labels)
        self.assertIn('divided by exposure time', json.dumps(prompts[0]['extra_tool_context']))
        self.assertEqual(len(result.tool_results), 1)
        # Excerpted sources need their conditions before planning, not only
        # after an answer has been proposed. A planning transport failure
        # retains those reads without freezing an unevidenced fallback plan.
        kwargs, plan = self.planning_inputs()
        kwargs['memory'].source_handles[0].metadata.update(
            source_directory={'sections': []}, evidence_passages_truncated=True)
        kwargs['context'].source_handles = kwargs['memory'].source_handles
        requests = plan.pop('context_requests')
        labels, prompts, saved = [], [], []
        delegate = self.planned_client(plan, labels, prompts)
        class BeforePlanner:
            def ask_json(inner, system, prompt, *, label='', **options):
                payload = prompt_payload(prompt)
                if label == 'report-source-selection':
                    labels.append(label)
                    return {'context_requests': requests}
                if label == 'report-outline-planner':
                    labels.append(label)
                    self.assertIn('divided by exposure time', json.dumps(payload['extra_tool_context']))
                    self.assertNotIn('context_requests', payload['output_schema'])
                    raise LLMError('Planning transport stopped after source selection')
                raise AssertionError('Do not compose before evidence-informed planning')
        with self.assertRaisesRegex(LLMError, 'Outline planner failed'):
            run_report_agent(**kwargs, client=BeforePlanner(),
                checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
        self.assertEqual(labels, ['report-source-selection', 'report-outline-planner'])
        self.assertIsNone(saved[-1]['memory']['document_plan'])
        self.assertEqual(kwargs['gateway'].call_counts['search_source_chunks'], 1)
        labels.clear()
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Do not replay source selection')):
            result = run_report_agent(**kwargs, client=delegate, completed_checkpoint=saved[-1])
        self.assertNotIn('report-source-selection', labels)
        self.assertEqual(labels.count('report-outline-planner'), 1)
        self.assertIn('divided by exposure time', json.dumps(prompts[0]['extra_tool_context']))
        self.assertEqual(len(result.tool_results), 1)

        # Old checkpoints shared the planning and Writer batch. Preserve their
        # spent allowance, rather than replaying it under the new outline owner.
        legacy = copy.deepcopy(saved[-1])
        for event in legacy['iterations']:
            if event['action'] == 'writer_context' and event['summary'] == 'outline_evidence':
                event['summary'] = 'initial_document'
        labels, prompts = [], []
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Do not replay legacy reads')):
            run_report_agent(**kwargs, client=self.planned_client(plan, labels, prompts), completed_checkpoint=legacy)
        self.assertFalse(prompts[0]['source_reading']['available'])

    def test_outline_read_does_not_consume_independent_writer_gap_lookup(self):
        kwargs, plan = self.planning_inputs()
        plan['context_requests'] = [{'tool_name': 'search_source_chunks', 'arguments': {
            'handle': 'material:comparison', 'query': query}} for query in
            ('calibrated rate', 'raw counts', 'exposure time', 'density normalization', 'bin width', 'normalization')]
        labels, prompts = [], []
        regular = self.planned_client(plan, labels, prompts)
        class Client:
            def ask_json(self, system, prompt, *, label='', **options):
                payload = prompt_payload(prompt)
                if label == 'report-writer-document' and payload['source_reading']['available']:
                    self.assert_initial = 'divided by exposure time' in json.dumps(payload['extra_tool_context'])
                    return {'context_requests': [{'tool_name': 'search_source_chunks', 'arguments': {
                        'handle': 'material:comparison', 'query': 'density normalization bin width'}}]}
                return regular.ask_json(system, prompt, label=label, **options)
        client = Client()
        result = run_report_agent(**kwargs, client=client)
        self.assertTrue(client.assert_initial)
        self.assertEqual(kwargs['gateway'].call_counts['search_source_chunks'], 7)
        self.assertEqual([row.summary for row in result.iterations if row.action == 'writer_context'],
                         ['outline_evidence', 'initial_document'])
        self.assertEqual(len(result.tool_results), 7)
        self.assertFalse(prompts[1]['source_reading']['available'])
        self.assertEqual(len(prompts[1]['extra_tool_context']), 7)
        self.assertEqual(prompts[1]['extra_tool_context'][0]['content'], result.tool_results[0].content)

    def test_planned_read_allocation_interruption_does_not_replan_or_replay(self):
        kwargs, plan = self.planning_inputs()
        saved = []
        checkpoint = interrupt_checkpoint(saved, lambda row: row['tool_results'])
        with patch.object(kwargs['gateway'], 'call') as read:
            with self.assertRaises(CheckpointStop):
                run_report_agent(**kwargs, client=self.planned_client(plan, [], []), checkpoint_sink=checkpoint)
            read.assert_not_called()
        labels, prompts = [], []
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Allocated read must not replay')):
            result = run_report_agent(**kwargs, client=self.planned_client(plan, labels, prompts), completed_checkpoint=saved[-1])
        self.assertNotIn('report-outline-planner', labels)
        self.assertEqual(result.tool_results[0].status, 'blocked')
        self.assertTrue(prompts[0]['source_reading']['available'])
        self.assertIn('not source evidence', result.tool_results[0].summary)

    def test_disabled_or_unregistered_planning_reads_stay_in_existing_correction_allowance(self):
        from simple_ar.report.agent import _validated_outline_delivery
        for requests, disabled in (([{'tool_name': 'shell', 'arguments': {}}], False),
                                   ([{'tool_name': 'get_paper_brief'}] * 7, False),
                                   ([{'tool_name': 'get_paper_brief'}], True)):
            kwargs, plan = self.planning_inputs()
            kwargs['config'] = kwargs['config'].model_copy(update={'allow_source_backtracking': not disabled})
            plan['context_requests'] = requests
            with self.assertRaises(ValueError):
                _validated_outline_delivery(plan, sections=kwargs['memory'].section_plan, context=kwargs['context'],
                    memory=kwargs['memory'], config=kwargs['config'])

    def test_post_read_transport_failure_resumes_without_replay_or_replanning(self):
        kwargs, saved = self.inputs(), []
        class Interrupted:
            def ask_json(self, system, prompt, *, label='', **options):
                if label == 'report-writer-document':
                    return {'context_requests': [{'tool_name': 'search_source_chunks', 'arguments': {'handle': 'material:comparison', 'query': 'measurement'}}]}
                raise LLMError('transport stopped after confirmed read')
        evidence = ReportToolResult(tool_name='search_source_chunks', status='not_found', summary='Retained text has no lexical match, not source absence.')
        with patch.object(kwargs['gateway'], 'call', return_value=evidence) as read:
            with self.assertRaises(LLMError):
                run_report_agent(**kwargs, client=Interrupted(), checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
            read.assert_called_once()
        labels, prompts = [], []
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Must not replay retained read')):
            result = run_report_agent(**kwargs, client=self.client(labels, prompts), completed_checkpoint=saved[-1])
        self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])
        self.assertFalse(prompts[0]['source_reading']['available'])
        self.assertEqual(prompts[0]['extra_tool_context'][0]['status'], 'not_found')
        self.assertEqual(len(result.tool_results), 1)

    def test_interruption_after_allocation_retains_unconfirmed_read_as_unavailable(self):
        kwargs, saved = self.inputs(), []
        regular = self.client([], [])
        class Reader:
            def ask_json(self, system, prompt, *, label='', **options):
                if label == 'report-writer-document':
                    return {'context_requests': [{'tool_name': 'get_paper_brief', 'arguments': {'handle': 'material:comparison'}}]}
                return regular.ask_json(system, prompt, label=label, **options)
        checkpoint = interrupt_checkpoint(saved, lambda row: row['tool_results'])
        with patch.object(kwargs['gateway'], 'call') as read:
            with self.assertRaises(CheckpointStop):
                run_report_agent(**kwargs, client=Reader(), checkpoint_sink=checkpoint)
            read.assert_not_called()
        labels, prompts = [], []
        with patch.object(kwargs['gateway'], 'call', side_effect=AssertionError('Allocated read cannot be replayed')):
            result = run_report_agent(**kwargs, client=self.client(labels, prompts), completed_checkpoint=saved[-1])
        self.assertEqual(result.tool_results[0].status, 'blocked')
        self.assertFalse(prompts[0]['source_reading']['available'])
        self.assertIn('not source evidence', result.tool_results[0].summary.lower())

    def test_invalid_read_scope_never_executes_tools_and_can_correct_format_once(self):
        for invalid in ({'context_requests': [{'tool_name': 'shell', 'arguments': {}}]},
                        {'context_requests': [{'tool_name': 'get_paper_brief'}] * 7},
                        {'context_requests': [{'tool_name': 'get_paper_brief'}], 'sections': [{}]}):
            with self.subTest(invalid=invalid):
                kwargs, labels, prompts = self.inputs(), [], []
                with patch.object(kwargs['gateway'], 'call') as read:
                    result = run_report_agent(**kwargs, client=self.client(labels, prompts, bad=invalid))
                    read.assert_not_called()
                self.assertEqual(len(result.sections), 3)
                self.assertEqual(labels, ['report-writer-document', 'report-writer-document-retry', 'report-document-reviewer'])
        kwargs = self.inputs()
        kwargs['config'] = kwargs['config'].model_copy(update={'allow_source_backtracking': False})
        labels, prompts = [], []
        result = run_report_agent(**kwargs, client=self.client(labels, prompts))
        self.assertFalse(prompts[0]['source_reading']['available'])
        self.assertNotIn('context_requests', prompts[0]['output_schema'])


class JointRevisionTests(unittest.TestCase):
    def test_canonical_length_drives_joint_edit_even_when_model_review_passes(self):
        for status, complete, corrected, expect_edit in (
            ('above_range', True, True, True), ('below_range', True, True, True),
            ('above_range', True, False, True), ('target_only', True, False, False),
            ('above_range', False, False, False), ('unavailable', True, False, False),
        ):
            with self.subTest(status=status, complete=complete, corrected=corrected):
                kwargs, labels = self.inputs(), []
                kwargs['config'] = kwargs['config'].model_copy(update={'max_review_iterations': 1})

                class Client:
                    def ask_json(self, system, prompt, *, label='', **unused):
                        labels.append(label)
                        view = prompt_payload(prompt)
                        if label in ('report-writer-document', 'report-document-joint-reviser'):
                            revised = label == 'report-document-joint-reviser'
                            return {'sections': [{'section_id': row['section']['section_id'],
                                'heading': row['section']['heading'],
                                'draft_markdown': ('Revised ' if revised else 'Original ') + row['section']['section_id']}
                                for row in view['sections']]}
                        if label == 'report-document-verifier':
                            return {'section_reviews': [{'section_id': row['section_id'], 'verdict': 'pass',
                                'notes': 'Current section satisfies the supplied task.'} for row in view['sections']]}
                        if label == 'report-document-reviewer':
                            return {'section_reviews': []}
                        raise AssertionError(label)

                def observation(context, memory, sections, config, **unused):
                    fits = corrected and all(row.draft_markdown.startswith('Revised') for row in sections)
                    return {'preview_status': 'pre_render_text_preview' if complete else 'unavailable',
                        'markdown_token_count': 900 if fits else 1067,
                        'length_check': {'status': 'within_range' if fits else status, 'scope': 'whole_document',
                            'min_words': 700, 'max_words': 1000, 'markdown_token_count': 900 if fits else 1067}}

                with patch('simple_ar.report.agent.delivery_text_observation', side_effect=observation):
                    result = run_report_agent(**kwargs, client=Client())
                self.assertEqual('report-document-joint-reviser' in labels, expect_edit)
                self.assertNotIn('report-document-finding-checker', labels)
                self.assertTrue(all(row.draft_markdown.startswith('Revised' if expect_edit and corrected else 'Original')
                                    for row in result.sections))
                if expect_edit and not corrected:
                    self.assertTrue(result.memory.reviewer_findings)
                elif expect_edit:
                    self.assertFalse(result.memory.reviewer_findings)

    def test_document_edit_scope_is_explicit_bounded_and_saved(self):
        for scope, action, expected in (
            ('document', 'revise', {'method', 'result', 'scope'}),
            ('section', 'revise', {'result'}),
            ('document', 'verify', {'result'}),
            ('document', 'advisory', {'result'}),
        ):
            with self.subTest(scope=scope, action=action):
                sections = [ReportSectionDraft(section_id=sid, heading=sid,
                    draft_markdown='Original ' + sid) for sid in ('method', 'result', 'scope')]
                finding = ReviewerFinding(finding_id='overall-organization', section_id='result',
                    type='style', severity='major', required_action=action,
                    message='Distributed repetition needs a coordinated correction.')
                review = ReportSectionReview(section_id='result', revision_scope=scope,
                    verdict='revise_required', findings=[finding])
                iterations = []

                def draft(event, baseline):
                    self.assertEqual({row.section_id for row in event.section_reviews}, expected)
                    self.assertEqual(sum(len(row.findings) for row in event.section_reviews), 1)
                    return [row.model_copy() for row in baseline if row.section_id in expected]

                def interrupt(*args):
                    raise RuntimeError('Saved candidate')

                kwargs = dict(memory=ReportMemory(), config=ReportRuntimeConfig(max_review_iterations=1),
                    sections=sections, iterations=iterations, all_findings=[], checkpoint=lambda: None)
                with self.assertRaisesRegex(RuntimeError, 'Saved candidate'):
                    edit_joint_document(**kwargs, reviews=[review], draft=draft, inspect=interrupt)
                # Fresh review scope cannot expand an already saved candidate.
                review.revision_scope = 'document'
                with self.assertRaisesRegex(RuntimeError, 'Saved candidate'):
                    edit_joint_document(**kwargs, reviews=[review],
                        draft=lambda *args: self.fail('Recovery must reuse saved drafts'), inspect=interrupt)
                self.assertEqual({row.section_id for row in iterations[0].section_reviews}, expected)

    def inputs(self, *, historical=False):
        context = ReportContext(topic='Supported scientific interpretation', report_mode='supplied_materials')
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id=sid, heading=sid,
            goal='Own a distinct argumentative step', draft_order=i, final_order=i)
            for i, sid in enumerate(('method', 'result', 'scope'), 1)])
        config = ReportRuntimeConfig(outline_strategy='template', document_review=True,
            review_scope='document', draft_scope='document', max_review_iterations=2)
        if historical:
            memory.reviewer_findings = [ReviewerFinding(finding_id='old-scope-opinion', section_id='scope',
                type='style', severity='major', required_action='revise',
                message='Earlier model incorrectly required rewriting the supported scope.',
                suggested_action='Replace the supported scope with an unconditional conclusion.',
                draft_quotes=[{'section_id': 'scope', 'quote': 'Original scope'}])]
        return agent_inputs(context, memory, config)

    def client(self, labels, *, reject=False, current_pass=False):
        class Client:
            def ask_json(self, system, prompt, *, label='', **unused):
                labels.append(label)
                view = prompt_payload(prompt)
                if label == 'report-writer-document':
                    return {'sections': [{'section_id': row['section']['section_id'],
                        'heading': row['section']['heading'], 'draft_markdown': 'Original ' + row['section']['section_id']}
                        for row in view['sections']]}
                if label == 'report-writer-scope':
                    return {'section_id': 'scope', 'heading': view['section']['heading'], 'draft_markdown': 'Original scope'}
                if label == 'report-document-reviewer':
                    assert f"{len(view['sections'])} frozen target sections" in ' '.join(view['focus'])
                    assert 'revision_concerns' not in view and not view['historical_findings_to_check']
                    if current_pass:
                        return {'section_reviews': [{'section_id': row['section_id'], 'verdict': 'pass'}
                            for row in view['sections']]}
                    return {'section_reviews': [{'section_id': sid, 'verdict': 'revise_required',
                        'revision_instructions': ['Reconcile explanation and interpretation.'],
                        'findings': [{'finding_id': sid + '-repeat', 'type': 'style', 'severity': 'major',
                            'required_action': 'revise', 'message': 'Substantive cross-section repetition.',
                            'draft_quotes': draft_quotes(prompt, sid)}]} for sid in ('method', 'result')]
                        + [{'section_id': 'scope', 'verdict': 'pass'}]}
                if label.startswith('report-document-joint-reviser'):
                    assert view['task'] == 'jointly_revise_report_sections'
                    expected = {'result'} if reject and labels.count('report-document-joint-reviser') > 1 else {'method', 'result'}
                    assert {row['section']['section_id'] for row in view['sections']} == expected
                    assert all(row['previous_draft']['draft_markdown'] for row in view['sections'])
                    assert all(row['revision_request']['findings'] for row in view['sections'])
                    return {'sections': [{'section_id': row['section']['section_id'], 'heading': row['section']['heading'],
                        'draft_markdown': 'Reconciled ' + row['section']['section_id']}
                        for row in view['sections']]}
                if label == 'report-document-verifier':
                    assert [row['markdown'] for row in view['sections']] == ['Reconciled method', 'Reconciled result', 'Original scope']
                    assert 'revision_concerns' not in view and not view['historical_findings_to_check']
                    passes = [{'section_id': sid, 'verdict': 'pass',
                        'notes': 'Current prose independently addresses organization against task and evidence.'}
                        for sid in ('method', 'result', 'scope')]
                    return {'section_reviews': [passes[0], passes[2], {'section_id': 'result', 'verdict': 'revise_required',
                        'findings': [{'finding_id': 'new-defect', 'type': 'style', 'severity': 'major',
                            'message': 'Candidate introduced an inconsistent transition.',
                            'draft_quotes': draft_quotes(prompt, 'result')}]}]} if reject else {'section_reviews': passes}
                raise AssertionError(label)
        return Client()

    def test_current_review_retires_wrong_old_opinion_without_rewriting_supported_prose(self):
        for scope, single in (('document', False), ('auto', False), ('document', True)):
            with self.subTest(scope=scope, single=single):
                kwargs, labels, saved = self.inputs(historical=True), [], []
                if single:
                    kwargs['memory'].section_plan = kwargs['memory'].section_plan[-1:]
                kwargs['config'] = kwargs['config'].model_copy(update={'draft_scope': scope})
                for plan in kwargs['memory'].section_plan:
                    plan.target_words = 100
                old = kwargs['memory'].reviewer_findings[0].model_copy(deep=True)
                result = run_report_agent(**kwargs, client=self.client(labels, current_pass=True),
                    checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
                self.assertEqual(labels, ['report-writer-scope' if single else 'report-writer-document', 'report-document-reviewer'])
                self.assertTrue(all(row['document_review_contract'] == 'current_draft.v1' for row in saved))
                self.assertTrue(saved[-1]['document_review_done'])
                expected = ['scope'] if single else ['method', 'result', 'scope']
                self.assertEqual([row.draft_markdown for row in result.sections], ['Original ' + sid for sid in expected])
                self.assertFalse(result.memory.reviewer_findings)
                self.assertIn(old, result.reviewer_findings)
                initial = next(row for row in result.iterations if row.action == 'document_current_review')
                self.assertEqual(initial.status, 'completed')
                self.assertEqual(initial.drafts, result.sections)
                self.assertEqual(initial.requested_findings, [old])
                self.assertEqual({row.section_id for row in initial.section_reviews}, set(expected))
                self.assertTrue(all(row.verdict == 'pass' and not row.findings for row in initial.section_reviews))
                self.assertFalse(any(row.finding_checks for row in initial.section_reviews))
                resume_client = self.client([])
                with patch.object(resume_client, 'ask_json', side_effect=AssertionError('No completed review replay')):
                    resumed = run_report_agent(**kwargs, client=resume_client, completed_checkpoint=saved[-1])
                self.assertEqual(resumed.sections, result.sections)
                self.assertFalse(resumed.memory.reviewer_findings)

    def test_current_review_keeps_control_failure_outside_retired_model_opinions(self):
        kwargs, labels = self.inputs(historical=True), []
        old = kwargs['memory'].reviewer_findings[0].model_copy(deep=True)
        control = ReviewerFinding(finding_id='document-finding-check-unavailable', section_id='scope',
            type='document_review_unavailable', severity='major', message='Historical checker transport failed.')
        kwargs['memory'].reviewer_findings.append(control)
        result = run_report_agent(**kwargs, client=self.client(labels, current_pass=True))
        self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])
        self.assertEqual(result.memory.reviewer_findings, [control])
        initial = next(row for row in result.iterations if row.action == 'document_current_review')
        self.assertEqual(initial.requested_findings, [old])
        self.assertIn(old, result.reviewer_findings)

    def test_current_review_prompt_selects_paragraph_and_persists_exact_text(self):
        from simple_ar.report.review_evidence import DRAFT_REFERENCE_CONTRACT
        kwargs, labels = self.inputs(), []
        client = self.client(labels, current_pass=True)
        original_ask = client.ask_json

        def ask(system, prompt, *, label='', **options):
            if label == 'report-document-reviewer':
                labels.append(label)
                view = prompt_payload(prompt)
                self.assertEqual(view['draft_reference_contract'], DRAFT_REFERENCE_CONTRACT)
                self.assertEqual(view['draft_locator']['scope']['previews'], {'p1': 'Original scope'})
                schema = view['output_schema']['section_reviews'][0]['findings'][0]['draft_quotes'][0]
                self.assertEqual(set(schema), {'section_id', 'paragraph'})
                return {'section_reviews': [{'section_id': sid, 'verdict': 'warning' if sid == 'scope' else 'pass',
                    'findings': [{'finding_id': 'selected-current-text', 'type': 'style', 'severity': 'minor',
                        'required_action': 'advisory', 'message': 'A bounded style observation.',
                        'draft_quotes': [{'section_id': 'scope', 'paragraph': 'p1'}]}] if sid == 'scope' else []}
                    for sid in ('method', 'result', 'scope')]}
            return original_ask(system, prompt, label=label, **options)

        client.ask_json = ask
        result = run_report_agent(**kwargs, client=client)
        initial = next(row for row in result.iterations if row.action == 'document_current_review')
        finding = next(f for row in initial.section_reviews for f in row.findings
            if f.finding_id == 'selected-current-text')
        self.assertEqual(finding.draft_quotes[0].model_dump(mode='json'),
            {'section_id': 'scope', 'quote': 'Original scope'})
        self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])

    def test_retained_unchecked_manuscript_uses_saved_candidate_after_unavailable_verifier(self):
        from simple_ar.report.writing import retained_unchecked_manuscript
        kwargs, labels = self.inputs(), []
        client = self.client(labels)
        original_ask = client.ask_json

        def ask(system, prompt, *, label='', **options):
            if label == 'report-document-verifier':
                labels.append(label)
                raise LLMError('Verifier unavailable after saving candidate')
            return original_ask(system, prompt, label=label, **options)

        client.ask_json = ask
        result = run_report_agent(**kwargs, client=client)
        payload = result.model_dump(mode='json')
        before = copy.deepcopy(payload)
        manuscript = retained_unchecked_manuscript(payload, title='Unchecked candidate')
        self.assertIn('Reconciled method', manuscript)
        self.assertIn('Reconciled result', manuscript)
        self.assertIn('Original scope', manuscript)
        self.assertNotIn('Original method', manuscript)
        self.assertNotIn('Original result', manuscript)
        self.assertEqual(payload, before)
        self.assertEqual([row.draft_markdown for row in result.sections],
            ['Original method', 'Original result', 'Original scope'])
        self.assertFalse(any(row.adopted is True for row in result.iterations
            if row.action == 'document_joint_revise'))
        self.assertTrue(any(row.status == 'drafted' and row.drafts for row in result.iterations
            if row.action == 'document_joint_revise'))
        self.assertTrue(any(row.type in {'document_review_unavailable', 'document_revision_unavailable'}
            for row in result.memory.reviewer_findings))
        self.assertFalse(any(row.status == 'completed' for row in result.iterations
            if row.action == 'document_joint_verify'))

    def test_failed_or_incomplete_current_review_keeps_old_opinion_and_original(self):
        for failure in ('transport', 'missing_section', 'context_request', 'partial'):
            with self.subTest(failure=failure):
                kwargs, labels, saved = self.inputs(historical=True), [], []
                old = kwargs['memory'].reviewer_findings[0].model_copy(deep=True)
                kwargs['config'] = kwargs['config'].model_copy(update={'allow_source_backtracking': False})
                client = self.client(labels, current_pass=True)
                original_ask = client.ask_json

                def ask(system, prompt, *, label='', **options):
                    if label.startswith('report-document-reviewer'):
                        labels.append(label)
                        view = prompt_payload(prompt)
                        self.assertFalse(view['historical_findings_to_check'])
                        if failure == 'transport':
                            raise LLMError('Current review unavailable')
                        rows = [{'section_id': sid, 'verdict': 'pass'} for sid in ('method', 'result', 'scope')]
                        if failure == 'missing_section':
                            rows.pop()
                        elif failure == 'context_request':
                            rows[-1]['context_requests'] = [{'tool_name': 'get_synthesis_brief', 'arguments': {}}]
                        else:
                            rows[-1]['findings'] = [{'finding_id': 'unanchored', 'type': 'style', 'severity': 'major',
                                'required_action': 'revise', 'message': 'Invalid observation quotes absent prose.',
                                'draft_quotes': [{'section_id': 'scope', 'quote': 'Absent current draft text'}]}]
                        return {'section_reviews': rows}
                    return original_ask(system, prompt, label=label, **options)

                client.ask_json = ask
                result = run_report_agent(**kwargs, client=client,
                    checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
                self.assertIn(old, result.memory.reviewer_findings)
                self.assertTrue(any(row.type == 'document_review_unavailable' for row in result.memory.reviewer_findings))
                self.assertEqual([row.draft_markdown for row in result.sections],
                                 ['Original method', 'Original result', 'Original scope'])
                self.assertFalse(any(row.action in {'document_current_review', 'document_joint_revise'}
                                     for row in result.iterations))
                self.assertFalse(any('finding-checker' in label or 'reviser' in label for label in labels))
                self.assertEqual(saved[-1]['document_review_contract'], 'current_draft.v1')
                self.assertIn(old.model_dump(mode='json'), saved[-1]['memory']['reviewer_findings'])

    def test_missing_contract_resumes_legacy_opinion_check_and_unknown_contract_is_rejected(self):
        for checking in (False, True):
            with self.subTest(checking=checking):
                kwargs, labels = self.inputs(historical=True), []
                kwargs['config'] = kwargs['config'].model_copy(update={'max_review_iterations': 0})
                old = kwargs['memory'].reviewer_findings[0].model_copy(deep=True)
                drafts = [ReportSectionDraft(section_id=sid, heading=sid, draft_markdown='Original ' + sid)
                    for sid in ('method', 'result', 'scope')]
                checkpoint = {'memory': kwargs['memory'].model_dump(mode='json'),
                    'sections': [row.model_dump(mode='json') for row in drafts], 'iterations': [],
                    'reviewer_findings': [old.model_dump(mode='json')], 'tool_results': [],
                    'pending_draft': None, 'document_review_done': False}
                if checking:
                    checkpoint['iterations'] = [ReportIterationRecord(iteration=1, section_id='scope',
                        action='document_finding_context', status='pass', requested_findings=[old]).model_dump(mode='json')]
                prior, saved = copy.deepcopy(checkpoint), []
                client = self.client(labels, current_pass=True)
                original_ask = client.ask_json

                def ask(system, prompt, *, label='', **options):
                    if label == 'report-document-finding-checker':
                        labels.append(label)
                        self.assertEqual(prompt_payload(prompt)['historical_findings_to_check'][0]['finding_id'], old.finding_id)
                        return {'section_reviews': [{'section_id': 'scope', 'verdict': 'revise_required',
                            'finding_checks': [{'finding_id': old.finding_id, 'status': 'unresolved',
                                'explanation': 'Legacy recovery cannot retire this opinion by omission.'}]}]}
                    return original_ask(system, prompt, label=label, **options)

                client.ask_json = ask
                result = run_report_agent(**kwargs, client=client, completed_checkpoint=checkpoint,
                    checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
                self.assertEqual(labels, ([] if checking else ['report-document-reviewer']) + ['report-document-finding-checker'])
                self.assertIn(old, result.memory.reviewer_findings)
                self.assertEqual(result.sections, drafts)
                self.assertFalse(any(row.action == 'document_current_review' for row in result.iterations))
                self.assertTrue(all('document_review_contract' not in row for row in saved))
                self.assertEqual(checkpoint, prior)
                invalid = {**prior, 'document_review_contract': 'current_draft.v999'}
                before = list(labels)
                with self.assertRaises(ValueError):
                    run_report_agent(**kwargs, client=client, completed_checkpoint=invalid)
                self.assertEqual(labels, before)

    def test_both_sections_adopt_only_after_whole_candidate_checks(self):
        kwargs, labels, saved = self.inputs(historical=True), [], []
        old = kwargs['memory'].reviewer_findings[0].model_copy(deep=True)
        result = run_report_agent(**kwargs, client=self.client(labels), checkpoint_sink=lambda row: saved.append(copy.deepcopy(row)))
        initial = next(row for row in result.iterations if row.action == 'document_current_review')
        self.assertEqual(initial.status, 'completed')
        self.assertEqual(initial.requested_findings, [old])
        self.assertEqual([row.draft_markdown for row in initial.drafts],
                         ['Original method', 'Original result', 'Original scope'])
        self.assertEqual([row.section_id for row in initial.section_reviews], ['method', 'result', 'scope'])
        self.assertEqual([finding.finding_id for row in initial.section_reviews for finding in row.findings],
                         ['method-repeat', 'result-repeat'])
        candidate = next(row for row in saved if any(event['action'] == 'document_joint_revise'
            and event.get('drafts') for event in row['iterations']))
        self.assertEqual([row['draft_markdown'] for row in candidate['sections']], ['Original method', 'Original result', 'Original scope'])
        self.assertEqual([row.draft_markdown for row in result.sections], ['Reconciled method', 'Reconciled result', 'Original scope'])
        self.assertTrue(next(row for row in result.iterations if row.action == 'document_joint_revise').adopted)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertIn(old, result.reviewer_findings)
        # An adopted joint candidate legitimately differs from the saved initial
        # manuscript. A requested new pass must inspect that adopted prose.
        completed = copy.deepcopy(saved[-1])
        completed['document_review_done'] = False
        before = list(labels)
        client = self.client(labels, current_pass=True)
        original_ask = client.ask_json

        def ask(system, prompt, *, label='', **options):
            self.assertEqual(label, 'report-document-reviewer')
            self.assertEqual([row['markdown'] for row in prompt_payload(prompt)['sections']],
                             ['Reconciled method', 'Reconciled result', 'Original scope'])
            return original_ask(system, prompt, label=label, **options)

        client.ask_json = ask
        resumed = run_report_agent(**kwargs, client=client, completed_checkpoint=completed)
        self.assertEqual(labels[len(before):], ['report-document-reviewer'])
        self.assertEqual(resumed.sections, result.sections)
        self.assertFalse(resumed.memory.reviewer_findings)

    def test_unquoted_or_nonediting_neighbors_do_not_expand_joint_targets(self):
        for action, target, quote in (
            ('advisory', 'method', 'Original method'), ('verify', 'method', 'Original method'),
            ('revise', 'unknown', 'Original method'), ('revise', 'method', 'Absent text'),
        ):
            with self.subTest(action=action, target=target):
                sections = [ReportSectionDraft(section_id=sid, heading=sid, draft_markdown='Original ' + sid)
                            for sid in ('method', 'result')]
                finding = ReviewerFinding(finding_id='shared', section_id='result', type='style',
                    severity='minor', required_action=action, message='Review the shared explanation.',
                    draft_quotes=[{'section_id': target, 'quote': quote}])
                seen = []

                def draft(event, baseline):
                    seen.append({row.section_id for row in event.section_reviews})
                    return [sections[1].model_copy(update={'draft_markdown': 'Candidate result'})]

                edit_joint_document(memory=ReportMemory(), config=ReportRuntimeConfig(max_review_iterations=1),
                    sections=sections, iterations=[], all_findings=[], checkpoint=lambda: None,
                    reviews=[ReportSectionReview(section_id='result', verdict='revise_required', findings=[finding])],
                    draft=draft, inspect=lambda *args: [])
                self.assertEqual(seen, [{'result'}])

    def test_cross_section_opinion_edits_quoted_neighbor_without_cloning_the_opinion(self):
        labels = []
        client = self.client(labels)
        original_ask = client.ask_json

        def ask(system, prompt, *, label='', **kwargs):
            view = prompt_payload(prompt)
            if label.startswith('report-document-joint-reviser'):
                labels.append(label)
                targets = {row['section']['section_id']: row for row in view['sections']}
                self.assertEqual(set(targets), {'method', 'result'})
                self.assertFalse(targets['method']['revision_request']['findings'])
                self.assertTrue(targets['method']['revision_request']['revision_instructions'])
                return {'sections': [{'section_id': sid, 'heading': row['section']['heading'],
                    'draft_markdown': 'Reconciled ' + sid} for sid, row in targets.items()]}
            response = original_ask(system, prompt, label=label, **kwargs)
            if label == 'report-document-reviewer':
                response['section_reviews'] = response['section_reviews'][1:]
                opinion = response['section_reviews'][0]['findings'][0]
                opinion['draft_quotes'].extend(draft_quotes(prompt, 'method'))
            return response

        client.ask_json = ask
        result = run_report_agent(**self.inputs(), client=client)
        self.assertEqual([row.draft_markdown for row in result.sections],
                         ['Reconciled method', 'Reconciled result', 'Original scope'])
        self.assertFalse(result.memory.reviewer_findings)

    def test_current_review_candidate_and_verification_resume_without_repeating_work(self):
        for stage, action in (('current_review', 'document_current_review'),
                              ('candidate', 'document_joint_revise'),
                              ('verification', 'document_joint_verify')):
            with self.subTest(stage=stage):
                labels, saved = [], []
                checkpoint = interrupt_checkpoint(saved, lambda row: any(
                    event['action'] == action and event['status'] == ('drafted' if stage == 'candidate' else 'completed')
                    for event in row['iterations']))
                with self.assertRaises(CheckpointStop):
                    run_report_agent(**self.inputs(historical=True), client=self.client(labels), checkpoint_sink=checkpoint)
                prior = copy.deepcopy(saved[-1])
                self.assertEqual(prior['document_review_contract'], 'current_draft.v1')
                self.assertFalse(prior['document_review_done'])
                if stage == 'current_review':
                    for corruption in ('missing_marker', 'mixed_history', 'changed_original', 'section_scope'):
                        with self.subTest(corruption=corruption):
                            invalid = copy.deepcopy(prior)
                            task = self.inputs(historical=True)
                            if corruption == 'missing_marker':
                                invalid.pop('document_review_contract')
                            elif corruption == 'mixed_history':
                                invalid['iterations'].append(ReportIterationRecord(iteration=99, section_id='scope',
                                    action='document_finding_check', status='pass').model_dump(mode='json'))
                            elif corruption == 'changed_original':
                                invalid['iterations'][-1]['drafts'][0]['draft_markdown'] = 'Tampered original'
                            else:
                                task['config'] = task['config'].model_copy(update={'draft_scope': 'section'})
                            with self.assertRaises(LLMError if corruption == 'changed_original' else ValueError):
                                run_report_agent(**task, client=self.client(labels), completed_checkpoint=invalid)
                    self.assertEqual(labels, ['report-writer-document', 'report-document-reviewer'])
                before = list(labels)
                result = run_report_agent(**self.inputs(historical=True), client=self.client(labels), completed_checkpoint=prior)
                expected = {'current_review': ['report-document-joint-reviser', 'report-document-verifier'],
                            'candidate': ['report-document-verifier'], 'verification': []}
                self.assertEqual(labels[len(before):], expected[stage])
                self.assertEqual(labels.count('report-document-joint-reviser'), 1)
                self.assertEqual(labels.count('report-document-reviewer'), 1)
                self.assertEqual(labels.count('report-document-verifier'), 1)
                self.assertNotIn('report-document-finding-checker', labels)
                self.assertEqual([row.draft_markdown for row in result.sections],
                                 ['Reconciled method', 'Reconciled result', 'Original scope'])
                self.assertEqual(prior, saved[-1])
                self.assertEqual(sum(row.action == 'document_current_review' for row in result.iterations), 1)
                self.assertFalse(result.memory.reviewer_findings)
                if stage != 'current_review':
                    # An old joint candidate/check has no new method or initial
                    # inspection event, but keeps its saved adoption contract.
                    legacy = copy.deepcopy(prior)
                    legacy.pop('document_review_contract')
                    legacy['iterations'] = [row for row in legacy['iterations']
                        if row['action'] != 'document_current_review']
                    calls = []
                    historical = run_report_agent(**self.inputs(historical=True), client=self.client(calls),
                        completed_checkpoint=legacy)
                    self.assertEqual(calls, expected[stage])
                    self.assertEqual(historical.sections, result.sections)

    def test_unaccepted_candidates_do_not_mix_or_reset_round_allowance(self):
        labels = []
        result = run_report_agent(**self.inputs(), client=self.client(labels, reject=True))
        self.assertEqual(labels.count('report-document-joint-reviser'), 2)
        self.assertEqual([row.draft_markdown for row in result.sections], ['Original method', 'Original result', 'Original scope'])
        self.assertTrue(all(not row.adopted for row in result.iterations if row.action == 'document_joint_revise'))
        self.assertIn('joint-revision-unresolved', [row.finding_id for row in result.memory.reviewer_findings])

    def test_failed_generation_uses_a_round_and_keeps_original(self):
        sections = [ReportSectionDraft(section_id='method', heading='method', draft_markdown='Original method')]
        review = ReportSectionReview(section_id='method', verdict='revise_required', findings=[ReviewerFinding(
            finding_id='rewrite', type='style', severity='major', section_id='method', message='Revise organization.')])
        iterations, saved = [], []
        config = ReportRuntimeConfig(max_review_iterations=1)
        memory = ReportMemory()
        def unavailable(event, baseline):
            raise LLMError('Provider unavailable')
        values = dict(memory=memory, config=config, sections=sections, iterations=iterations, reviews=[review],
            all_findings=[], checkpoint=lambda: saved.append(copy.deepcopy(iterations)), draft=unavailable,
            inspect=lambda *args: (_ for _ in ()).throw(AssertionError('No inspection of missing draft')))
        edit_joint_document(**values)
        edit_joint_document(**values)
        self.assertEqual(len([row for row in iterations if row.action == 'document_joint_revise']), 1)
        self.assertEqual(sections[0].draft_markdown, 'Original method')
        self.assertEqual(iterations[0].status, 'unavailable')

    def test_rejected_candidate_findings_are_history_not_original_manuscript_defects(self):
        sections = [ReportSectionDraft(section_id='method', heading='Method', draft_markdown='Original')]
        original = ReviewerFinding(finding_id='organization', section_id='method', type='style',
            severity='major', message='Clarify organization.')
        candidate_issue = ReviewerFinding(finding_id='invented-result', section_id='method', type='factual',
            severity='major', message='Candidate invents a result.')
        reviews = [ReportSectionReview(section_id='method', verdict='revise_required', findings=[original])]
        memory, history, iterations = ReportMemory(), [], []
        def inspect(candidate, prior):
            self.assertIn(original, prior)
            return [ReportSectionReview(section_id='method', verdict='revise_required', findings=[candidate_issue])]
        edit_joint_document(memory=memory, config=ReportRuntimeConfig(max_review_iterations=1), sections=sections,
            iterations=iterations, reviews=reviews, all_findings=history, checkpoint=lambda: None,
            draft=lambda event, baseline: [baseline[0].model_copy(update={'draft_markdown': 'Candidate'})], inspect=inspect)
        self.assertEqual(sections[0].draft_markdown, 'Original')
        self.assertIn(original, memory.reviewer_findings)
        self.assertNotIn(candidate_issue, memory.reviewer_findings)
        self.assertIn(candidate_issue, history)
        self.assertTrue(any(candidate_issue in review.findings for event in iterations for review in event.section_reviews))
        # Resuming after rejection follows the latest complete inspection;
        # resolved instructions remain history rather than being reissued.
        def correct(event, baseline):
            pending = [finding for review in event.section_reviews for finding in review.findings]
            self.assertEqual(pending, [candidate_issue])
            self.assertEqual(baseline[0].draft_markdown, 'Candidate')
            return [baseline[0].model_copy(update={'draft_markdown': 'Corrected candidate'})]
        edit_joint_document(memory=memory, config=ReportRuntimeConfig(max_review_iterations=2), sections=sections,
            iterations=iterations, reviews=reviews, all_findings=history, checkpoint=lambda: None,
            draft=correct, inspect=lambda *args: [ReportSectionReview(section_id='method', verdict='pass')])
        self.assertEqual(sections[0].draft_markdown, 'Corrected candidate')
        self.assertNotIn(original, memory.reviewer_findings)
        self.assertNotIn(candidate_issue, memory.reviewer_findings)
        self.assertTrue(iterations[-2].adopted)

    def test_legacy_rounds_and_unchecked_old_findings_are_not_erased(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                sections = [ReportSectionDraft(section_id='method', heading='method', draft_markdown='Original')]
                review = ReportSectionReview(section_id='method', verdict='revise_required', findings=[ReviewerFinding(
                    finding_id='required', type='style', severity='major', section_id='method', message='Required correction.')])
                iterations = [ReportIterationRecord(iteration=1, section_id='method', action='document_revise', status='rejected')] if legacy else []
                calls = []
                def draft(event, baseline):
                    calls.append('draft')
                    return [ReportSectionDraft(section_id='method', heading='method', draft_markdown='Candidate')]
                def inspect(candidate, prior):
                    calls.append('inspect')
                    return []  # Omission is not proof the historical allegation was fixed.
                edit_joint_document(memory=ReportMemory(), config=ReportRuntimeConfig(max_review_iterations=1),
                    sections=sections, iterations=iterations, reviews=[review], all_findings=[],
                    checkpoint=lambda: None, draft=draft, inspect=inspect)
                self.assertEqual(sections[0].draft_markdown, 'Original')
                self.assertEqual(calls, [] if legacy else ['draft', 'inspect'])


    def test_integrated_length_replay_preserves_editorial_block_and_attachment_proof(self):
        from simple_ar.report.editor import review_document, replay_document_reviews, _validate_document_reviews
        from simple_ar.report.review_evidence import delivery_review_result

        kwargs = self.inputs()
        sections = [ReportSectionDraft(section_id=row.section_id, heading=row.heading,
            draft_markdown='Current bounded account.') for row in kwargs['memory'].section_plan]
        response = {'section_reviews': [{'section_id': row.section_id, 'verdict': 'pass'} for row in sections]}
        views, observations = [], []

        class Client:
            def ask_json(self, system, prompt, **options):
                views.append(json.loads(prompt))
                return response

        snapshot = {'contract': 'delivery_artifacts.v1', 'artifacts': []}
        live = review_document(client=Client(), template=kwargs['template'], memory=kwargs['memory'],
            sections=sections, config=kwargs['config'], execution_summary={}, metric_summary={},
            complete_candidate=True, delivery_review=snapshot, delivery_assembly={'identity': {}},
            delivery_text_observation={'preview_status': 'pre_render_text_preview',
                'length_check': {'status': 'above_range', 'markdown_token_count': 1200,
                    'scope': 'whole_document', 'min_words': 700, 'max_words': 900}},
            on_delivery_review=observations.append)
        replay = replay_document_reviews(response, sections=sections, evidence_view=views[0])
        self.assertEqual(replay, live)
        self.assertEqual(observations[0]['review_status'], 'needs_revision')
        self.assertTrue(any(finding.finding_id == 'canonical-delivery-length'
            for row in replay for finding in row.findings))
        raw = _validate_document_reviews(response, sections=sections, prior_by_key={}, evidence_view=views[0])
        self.assertEqual(delivery_review_result(snapshot, raw), delivery_review_result(snapshot, replay))
        legacy_view = copy.deepcopy(views[0])
        legacy_view['delivery_text_observation'].pop('review_postprocessing')
        self.assertEqual(replay_document_reviews(response, sections=sections, evidence_view=legacy_view), raw)

    def _integrated_delivery_case(self, root, labels, *, reject=False, delivery_defect=False):
        kwargs = self.inputs()
        kwargs['context'].results.update(
            section_delivery_review={'contract': 'section_delivery_review.v1',
                'assembly': {'title': kwargs['context'].topic, 'assembly_mode': 'section_drafts',
                             'template_name': ''}},
            delivery_review={'contract': 'delivery_artifacts.v1', 'artifacts': [
                {'path': 'figure.svg', 'sha256': 'a' * 64, 'coverage': 'visual_uninspected'}]})
        if delivery_defect:
            kwargs['context'].results['delivery_review']['artifacts'].append({
                'path': 'analysis.py', 'sha256': 'b' * 64, 'coverage': 'complete',
                'text': 'raise RuntimeError("unfinished analysis")', 'producer': 'analysis'})
        regular = self.client(labels, reject=reject)
        case = self

        class RecordedClient:
            last_inspection_source = None

            def ask_json(inner, system, prompt, *, label='', **options):
                response = regular.ask_json(system, prompt, label=label, **options)
                if label in {'report-document-reviewer', 'report-document-verifier'}:
                    view = prompt_payload(prompt)
                    case.assertEqual(view['delivery_review_contract'], 'delivery_artifacts.v1')
                    case.assertEqual(view['delivery_artifacts'][0]['coverage'], 'visual_uninspected')
                    assembly = view['delivery_text_observation']['canonical_assembly']
                    case.assertEqual(assembly['identity']['body_sha256'], hashlib.sha256(
                        assembly['report_body_markdown'].encode('utf-8')).hexdigest())
                    if delivery_defect and label == 'report-document-verifier':
                        response = {'section_reviews': [{'section_id': row['section_id'],
                            'verdict': 'revise_required' if row['section_id'] == 'result' else 'pass',
                            'findings': [{'finding_id': 'unfinished-analysis', 'type': 'factual',
                                'severity': 'major', 'required_action': 'revise',
                                'message': 'The delivered analysis script always raises instead of computing.',
                                'delivery_targets': ['/delivery_artifacts/1/text']}]
                                if row['section_id'] == 'result' else []}
                            for row in view['sections']]}
                    source = {}
                    for name, value in (('request', view), ('response', response)):
                        path = root / f'{len(labels)}-{name}.json'
                        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
                        source[name + '_path'] = str(path)
                        source[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
                    inner.last_inspection_source = source
                return response

        return kwargs, RecordedClient()

    def test_integrated_delivery_verdict_matches_adopted_or_rejected_candidate(self):
        from simple_ar.report.agent import _section_delivery_assembly

        for reject in (False, True):
            with self.subTest(reject=reject), tempfile.TemporaryDirectory() as tmp:
                labels, saved = [], []
                kwargs, client = self._integrated_delivery_case(Path(tmp), labels, reject=reject)
                result = run_report_agent(**kwargs, client=client, checkpoint_sink=saved.append)
                if reject:
                    self.assertIsNotNone(result.pending_candidate)
                    self.assertEqual(result.pending_candidate['independent_review']['review_status'], 'needs_revision')
                    self.assertFalse(result.pending_candidate['independent_review']['delivery_review']['defects'])
                else:
                    self.assertIsNone(result.pending_candidate)
                    self.assertNotIn('pending_candidate', result.model_dump(mode='json'))
                review = result.independent_review
                self.assertIsNotNone(review)
                self.assertEqual(review['assembly_identity'], _section_delivery_assembly(
                    kwargs['context'], result.memory, result.sections, kwargs['config'])['identity'])
                self.assertEqual(review['review_status'], 'needs_revision' if reject else 'reviewed')
                self.assertEqual(len(review['delivery_review']['visual_pending']), 1)
                source = review['inspection_source']
                for name in ('request', 'response'):
                    self.assertEqual(source[name + '_sha256'], hashlib.sha256(
                        Path(source[name + '_path']).read_bytes()).hexdigest())
                request = json.loads(Path(source['request_path']).read_text(encoding='utf-8'))
                for name in ('request', 'response'):
                    payload = json.loads(Path(source[name + '_path']).read_text(encoding='utf-8'))
                    self.assertEqual(review['inspection_payload_sha256'][name], hashlib.sha256(
                        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                                   allow_nan=False).encode('utf-8')).hexdigest())
                self.assertEqual([row['markdown'] for row in request['sections']],
                                 [row.draft_markdown for row in result.sections])
                retained = [row for row in result.iterations if row.independent_review is not None]
                self.assertEqual(len(retained), 3 if reject else 2)
                self.assertEqual(retained[0].action, 'document_current_review')
                self.assertTrue(all(row.action == 'document_joint_verify' for row in retained[1:]))
                self.assertEqual(labels.count('report-document-reviewer'), 1)
                self.assertEqual(labels.count('report-document-verifier'), 2 if reject else 1)
                self.assertEqual(labels.count('report-document-joint-reviser'), 2 if reject else 1)
                for index, event in enumerate(result.iterations):
                    if event.action == 'document_joint_revise':
                        self.assertEqual(result.iterations[index + 1].action, 'document_joint_verify')
                self.assertNotEqual(retained[0].independent_review['assembly_identity'],
                                    retained[1].independent_review['assembly_identity'])
                self.assertEqual(saved[-1]['section_delivery_review_contract'], 'section_delivery_review.v1')
                before = list(labels)
                with patch.object(client, 'ask_json', side_effect=AssertionError('No completed review replay')):
                    resumed = run_report_agent(**kwargs, client=client, completed_checkpoint=saved[-1])
                self.assertEqual(resumed.independent_review, review)
                self.assertEqual(labels, before)
                if not reject:
                    interrupted_labels, interrupted_saved = [], []
                    fresh, recorded = self._integrated_delivery_case(Path(tmp), interrupted_labels)
                    stop = interrupt_checkpoint(interrupted_saved, lambda row: any(
                        event['action'] == 'document_joint_verify' for event in row['iterations']))
                    with self.assertRaises(CheckpointStop):
                        run_report_agent(**fresh, client=recorded, checkpoint_sink=stop)
                    with patch.object(recorded, 'ask_json', side_effect=AssertionError('Saved verification must not replay')):
                        recovered = run_report_agent(**fresh, client=recorded,
                            completed_checkpoint=interrupted_saved[-1])
                    self.assertEqual(recovered.independent_review['review_status'], 'reviewed')
                    self.assertEqual(recovered.independent_review['inspection_source'],
                        next(event['independent_review']['inspection_source']
                            for event in reversed(interrupted_saved[-1]['iterations'])
                            if event['action'] == 'document_joint_verify'))

        # Exercise the real receipt producer, capability normalization callback
        # and downstream proof consumer together, using the same section case.
        with self.subTest(transport='atomic_complete.v2'), tempfile.TemporaryDirectory() as tmp:
            from simple_ar.report.writing import retained_writing_client, completed_composition_writer
            from simple_ar.report.writing import validate_document_inspection

            owner = ArtifactStore(Path(tmp))
            writer = ArtifactStore(owner.root / 'writer')
            documents = owner.write_json('documents.json',
                DocumentBundle([], {}, {}, [], []).to_handoff_dict(),
                kind='documents', schema='document_bundle.v1')
            labels = []
            kwargs, _ = self._integrated_delivery_case(owner.root, labels)
            regular = self.client(labels)
            client = retained_writing_client(store=writer, identity='integrated-sections',
                client=regular, contract='atomic_complete.v2')
            request = ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                kwargs['template'], client, method='sections')
            result = run_report_writing_capability(context=CapabilityContext(writer,
                AttemptManifest('integrated-sections'), inputs=(documents,), input_store=owner), request=request)
            self.assertEqual(result.status, 'completed', result.diagnostics)
            produced = next(row for row in result.artifacts if row.kind == 'report_writer_result')
            review = writer.read_json(produced)['independent_review']
            self.assertEqual(review['review_status'], 'reviewed')
            self.assertEqual(labels.count('report-document-verifier'), 1)
            source = review['inspection_source']
            self.assertTrue(source['request_path'].startswith('writer/model_calls/'))
            self.assertFalse(Path(source['request_path']).is_absolute())
            transport_request = owner.read_json(source['request_path'])
            self.assertEqual(transport_request['response_record_contract'], 'atomic_complete.v2')
            validate_document_inspection(owner,
                {'delivery_review': kwargs['context'].results['delivery_review']},
                {'delivery_review': review['delivery_review']}, review_result=review)
            before = list(labels)
            checkpoint = owner.ref('writer/sections.json', kind='report_checkpoint', schema='report_checkpoint.v1')
            replay_client = retained_writing_client(store=writer, identity='integrated-sections',
                client=None, contract='atomic_complete.v2')
            with patch.object(regular, 'ask_json', side_effect=AssertionError('No completion API replay')):
                replayed = run_report_writing_capability(context=CapabilityContext(writer,
                    AttemptManifest('integrated-sections'), inputs=(documents, checkpoint), input_store=owner),
                    request=replace(request, llm_client=replay_client, resume_ref=checkpoint))
            self.assertEqual(replayed.status, 'completed', replayed.diagnostics)
            self.assertEqual(writer.read_json(produced)['independent_review'], review)
            self.assertEqual(labels, before)
            validate_document_inspection(owner,
                {'delivery_review': kwargs['context'].results['delivery_review']},
                {'delivery_review': review['delivery_review']}, review_result=review)
            frozen = writer.read_json('report_inputs.json')
            binding = {'writing_owner': 'professional_report.v1', 'section_response_contract': 'atomic_complete.v2',
                'request': {'target_words': 0, 'max_cited_sources': kwargs['config'].max_cited_sources},
                'report_binding': {'requirement': {'method': 'sections',
                    'config': frozen['config'], 'template': frozen['template']}}}
            owner.write_json('professional_result.json', {'writer_path': produced.path,
                'writer_sha256': hashlib.sha256(writer.resolve(produced).read_bytes()).hexdigest()})
            retained = {path.relative_to(writer.root).as_posix(): path.read_bytes()
                        for path in writer.root.rglob('*') if path.is_file()}
            with patch.object(regular, 'ask_json', side_effect=AssertionError('No completed producer API replay')):
                completed_composition_writer(store=owner, binding=binding, identity='integrated-sections')
            self.assertEqual({path.relative_to(writer.root).as_posix(): path.read_bytes()
                              for path in writer.root.rglob('*') if path.is_file()}, retained)
            self.assertEqual(labels, before)

    def test_declared_report_images_require_real_markdown_nodes(self):
        from simple_ar.report.editor import _apply_document_delivery_observations
        from simple_ar.report.schema import ReportSectionDraft, ReportSectionReview
        observed = {'required_figures': ['figures/main plot.png']}
        def inspect(body):
            sections = [ReportSectionDraft(section_id='results', heading='Results', draft_markdown=body)]
            return _apply_document_delivery_observations([
                ReportSectionReview(section_id='results', verdict='pass')], sections=sections,
                observed=observed, joint_revision=True)
        for body in ('See Figure 1.', '```markdown\n![main](figures/main%20plot.png)\n```',
                     '[Download](figures/main%20plot.png)'):
            self.assertTrue(any(f.finding_id == 'required-report-figures'
                for row in inspect(body) for f in row.findings))
        self.assertFalse(any(row.findings for row in inspect(
            '![Measured comparison][plot]\n\n[plot]: <figures/main plot.png>')))
        self.assertFalse(any(row.findings for row in inspect(
            '![Measured comparison](figures/main%20plot.png)')))

    def test_pending_delivery_candidate_preserves_negative_proof_and_recovery(self):
        from simple_ar.report.agent import _pending_section_delivery_candidate

        for stop_at in ('drafted', 'verified', None):
            with self.subTest(stop_at=stop_at), tempfile.TemporaryDirectory() as tmp:
                labels, saved = [], []
                kwargs, client = self._integrated_delivery_case(Path(tmp), labels, delivery_defect=True)
                if stop_at:
                    def should_stop(row):
                        return any(event['action'] == ('document_joint_revise' if stop_at == 'drafted'
                            else 'document_joint_verify') and event['status'] == (
                                'drafted' if stop_at == 'drafted' else 'failed') for event in row['iterations'])
                    with self.assertRaises(CheckpointStop):
                        run_report_agent(**kwargs, client=client,
                            checkpoint_sink=interrupt_checkpoint(saved, should_stop))
                    if stop_at == 'drafted':
                        checkpoint = saved[-1]
                        self.assertIsNone(_pending_section_delivery_candidate(kwargs['context'],
                            ReportMemory.model_validate(checkpoint['memory']), kwargs['config'],
                            [ReportIterationRecord.model_validate(row) for row in checkpoint['iterations']],
                            adopted_identity={}))
                        result = run_report_agent(**kwargs, client=client,
                            completed_checkpoint=checkpoint, checkpoint_sink=saved.append)
                    else:
                        with patch.object(client, 'ask_json', side_effect=AssertionError('Reuse saved negative proof')):
                            result = run_report_agent(**kwargs, client=client,
                                completed_checkpoint=saved[-1], checkpoint_sink=saved.append)
                else:
                    result = run_report_agent(**kwargs, client=client, checkpoint_sink=saved.append)
                pending = result.pending_candidate
                self.assertIsNotNone(pending)
                self.assertEqual([row['draft_markdown'] for row in pending['sections']],
                    ['Reconciled method', 'Reconciled result', 'Original scope'])
                self.assertEqual([row.draft_markdown for row in result.sections],
                    ['Original method', 'Original result', 'Original scope'])
                review = pending['independent_review']
                self.assertEqual(review['review_status'], 'needs_revision')
                self.assertEqual(pending['assembly_identity'], review['assembly_identity'])
                self.assertEqual(hashlib.sha256(pending['report_body_markdown'].encode('utf-8')).hexdigest(),
                    review['assembly_identity']['body_sha256'])
                self.assertNotEqual(review['assembly_identity'], result.independent_review['assembly_identity'])
                self.assertFalse(result.independent_review['delivery_review']['defects'])
                self.assertEqual(review['delivery_review']['defects'][0]['targets'][0]['path'], 'analysis.py')
                proposal = next(row for row in result.iterations if row.iteration == pending['revision_iteration'])
                verification = next(row for row in result.iterations if row.iteration == pending['verification_iteration'])
                self.assertEqual((proposal.status, proposal.adopted), ('rejected', False))
                self.assertEqual(review, verification.independent_review)
                source = review['inspection_source']
                request = json.loads(Path(source['request_path']).read_text(encoding='utf-8'))
                self.assertEqual([row['markdown'] for row in request['sections']],
                    [row['draft_markdown'] for row in pending['sections']])
                self.assertEqual(request['delivery_text_observation']['canonical_assembly']['report_body_markdown'],
                    pending['report_body_markdown'])
                for name in ('request', 'response'):
                    self.assertEqual(source[name + '_sha256'], hashlib.sha256(
                        Path(source[name + '_path']).read_bytes()).hexdigest())
                with patch.object(client, 'ask_json', side_effect=AssertionError('No completed inspection replay')):
                    recovered = run_report_agent(**kwargs, client=client, completed_checkpoint=saved[-1])
                self.assertEqual(recovered.pending_candidate, pending)
                self.assertEqual(recovered.independent_review, result.independent_review)
                # Incomplete or mismatched saved proof must not opt into producer repair.
                for change in ('receipt', 'coverage', 'identity', 'defects'):
                    events = copy.deepcopy(result.iterations)
                    check = next(row for row in events if row.iteration == verification.iteration)
                    if change == 'receipt':
                        check.independent_review.pop('inspection_source')
                    elif change == 'coverage':
                        check.section_reviews.pop()
                    elif change == 'identity':
                        check.independent_review['assembly_identity']['body_sha256'] = '0' * 64
                    else:
                        check.independent_review['delivery_review']['defects'] = []
                    self.assertIsNone(_pending_section_delivery_candidate(kwargs['context'], result.memory,
                        kwargs['config'], events, adopted_identity=result.independent_review['assembly_identity']))

        with tempfile.TemporaryDirectory() as tmp:
            owner = ArtifactStore(Path(tmp))
            writer = ArtifactStore(owner.root / 'writer')
            from simple_ar.report.writing import retained_writing_client
            labels = []
            kwargs, raw_client = self._integrated_delivery_case(owner.root, labels, delivery_defect=True)
            client = retained_writing_client(store=writer, identity='pending-sections',
                client=raw_client, contract='atomic_complete.v2')
            documents = owner.write_json('documents.json', DocumentBundle([], {}, {}, [], []).to_handoff_dict(),
                kind='documents', schema='document_bundle.v1')
            outcome = run_report_writing_capability(context=CapabilityContext(writer,
                AttemptManifest('pending-sections'), inputs=(documents,), input_store=owner),
                request=ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                    kwargs['template'], client, method='sections'))
            self.assertEqual(outcome.status, 'completed', outcome.diagnostics)
            produced = next(row for row in outcome.artifacts if row.kind == 'report_writer_result')
            payload = writer.read_json(produced)
            pending = payload['pending_candidate']
            self.assertTrue(pending['independent_review']['delivery_review']['defects'])
            self.assertNotEqual(pending['independent_review'], payload['independent_review'])
            self.assertTrue(pending['independent_review']['inspection_source']['request_path'].startswith(
                'writer/model_calls/'))
            checkpoint = owner.ref('writer/sections.json', kind='report_checkpoint', schema='report_checkpoint.v1')
            replay_client = retained_writing_client(store=writer, identity='pending-sections',
                client=None, contract='atomic_complete.v2')
            with patch.object(raw_client, 'ask_json', side_effect=AssertionError('No capability recovery API replay')):
                replayed = run_report_writing_capability(context=CapabilityContext(writer,
                    AttemptManifest('pending-sections'), inputs=(documents, checkpoint), input_store=owner),
                    request=ReportWritingRequest(kwargs['context'], kwargs['memory'], kwargs['config'],
                        kwargs['template'], replay_client, method='sections', resume_ref=checkpoint))
            self.assertEqual(replayed.status, 'completed', replayed.diagnostics)
            recovered_payload = writer.read_json(produced)
            self.assertEqual(recovered_payload['pending_candidate'], pending)
            self.assertEqual(recovered_payload['independent_review'], payload['independent_review'])

    def test_integrated_delivery_requires_transport_source_and_legacy_checkpoint_does_not_upgrade(self):
        kwargs = self.inputs()
        saved, labels = [], []
        legacy = run_report_agent(**kwargs, client=self.client(labels), checkpoint_sink=saved.append)
        checkpoint = copy.deepcopy(saved[-1])
        with tempfile.TemporaryDirectory() as tmp:
            integrated, client = self._integrated_delivery_case(Path(tmp), [])
            with patch.object(client, 'ask_json', side_effect=AssertionError('Legacy completion must not replay')):
                resumed = run_report_agent(**integrated, client=client, completed_checkpoint=checkpoint)
            self.assertIsNone(resumed.independent_review)
            self.assertEqual(resumed.sections, legacy.sections)
            self.assertNotIn('independent_review', resumed.model_dump(mode='json'))
            self.assertNotIn('pending_candidate', resumed.model_dump(mode='json'))
            missing = run_report_agent(**integrated, client=self.client([]))
            self.assertIsNone(missing.independent_review)
            self.assertTrue(any(row.type == 'document_review_unavailable'
                                for row in missing.memory.reviewer_findings))


if __name__ == "__main__":
    unittest.main()
