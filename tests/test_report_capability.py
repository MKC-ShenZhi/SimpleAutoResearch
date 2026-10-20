from __future__ import annotations

import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from tests.report_review_fixtures import changed_record

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.integrations.llm import LLMResponseError
from simple_ar.report.audit import (
    ReportAuditCapabilityRequest,
    run_report_audit_capability,
)
from simple_ar.report.capability import ReportAssemblyMode, ReportAssemblyRequest, run_report_capability, assemble_report_document, preview_report_document
from simple_ar.report.figures import ReportFigureRecord, ReportFigureResult
from simple_ar.report.schema import (
    ReportContext,
    ReportDocumentPlan,
    ReportFigureConfig,
    ReportMemory,
    ReportRuntimeConfig,
    ReportSectionDraft,
    ReportSectionPlan,
    ReportVisualIntent,
    ReportArgumentPlan,
    ReportArgumentPoint,
    MetricSource,
    ReportTemplateBundle,
    SourceHandle,
)
from simple_ar.report.writing import ARTICLE_OWNER, ReportWritingRequest, inspect_article, resolve_report_writing_config, resolve_report_writing_resume, run_report_writing_capability, validate_report_writing_config


class CompositionArticleOwnerTests(unittest.TestCase):
    def test_reviewed_composition_feedback_recovery_and_evidence_admission(self):
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.writing import WritingToolGateway

        for rounds, pending in ((0, False), (0, True), (1, True)):
            with self.subTest(rounds=rounds, pending=pending), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'values.csv'
                source.write_text('run,score\nfirst,0.75\n', encoding='utf-8')
                task = root / 'task'
                task.mkdir()
                ledger = BudgetLedger()
                workspace = WorkspaceToolGateway(task, {'source0': source}, ledger)
                calls = []
                drafts = []

                class Client:
                    def ask_json(self, system, prompt, *, label='', **options):
                        calls.append((label, options.get('budget_call_id')))
                        view = json.loads(prompt)
                        if label == 'composition-article':
                            drafts.append(view)
                            return {'draft_markdown': 'Recorded score 0.75. '
                                + ('One supplied run; uncertainty unmeasured.' if len(drafts) > 1 else 'Limited observation.')}
                        if label != 'article-independent-review':
                            raise AssertionError('Unexpected model request: ' + label)
                        if pending and len(drafts) == 1:
                            return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                                'findings': [{'finding_id': 'extent', 'type': 'structure', 'severity': 'major',
                                    'required_action': 'revise', 'message': 'Clarify the observation limits.'}]}]}
                        return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass'}]}

                gateway = WritingToolGateway(task, workspace, ledger, 'fixture', 'task',
                    client_factory=lambda _: Client())
                request = {'goal': 'Explain the recorded observation.', 'title': 'Observation',
                    'materials': [{'path': 'source0'}],
                    'professional_review': {'max_revision_rounds': rounds}}
                produced = gateway.call('compose_document', request, identity='reviewed-native')
                self.assertEqual(produced.status, 'ok', produced.error)
                expected = 'needs_revision' if pending and not rounds else 'reviewed'
                self.assertEqual(produced.data['independent_review']['review_status'], expected)
                self.assertEqual(json.loads(workspace.resolve(produced.data['review_path']).read_text()),
                    produced.data['independent_review'])
                self.assertIn(produced.data['review_path'], produced.artifacts)
                self.assertEqual(bool(produced.data['unresolved']), expected == 'needs_revision')
                self.assertEqual(len(calls), 4 if pending and rounds else 2)
                self.assertEqual(calls[0], ('composition-article', 'reviewed-native:article'))
                parent = task / 'tools/writing/reviewed-native'
                original = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
                if pending and rounds:
                    completed = json.loads((parent / 'professional_result.json').read_text())
                    adopted = json.loads((parent / 'writer' / completed['writer_path']).read_text())
                    self.assertIsNotNone(adopted['adopted_revision'])
                with patch.object(gateway, 'client_factory', side_effect=AssertionError('No replay calls')):
                    replay = gateway.call('compose_document', request, identity='reviewed-native')
                self.assertEqual(replay.status, 'ok', replay.error)
                self.assertEqual(replay.data, produced.data)
                feedback = {'goal': 'Clarify the limits using the same observations.', 'title': 'Revised observation',
                    'prior_draft': produced.data['editable_markdown'], 'reuse_prior_inputs': True,
                    'professional_review': {'max_revision_rounds': 0}}
                with patch.object(gateway, '_select_materials', side_effect=AssertionError('No reselection')):
                    revised = gateway.call('compose_document', feedback, identity='feedback-native')
                self.assertEqual(revised.status, 'ok', revised.error)
                self.assertIn('0.75', json.dumps(drafts[-1]['source_handles']))
                self.assertTrue(drafts[-1]['prior_draft'])
                self.assertEqual({p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}, original)
                with patch.object(gateway, 'client_factory', side_effect=AssertionError('No admission calls')) as factory:
                    replay = gateway.call('compose_document', feedback, identity='feedback-native')
                    self.assertEqual(replay.status, 'ok', replay.error)
                    with changed_record(parent / 'writer/review/response.json', None):
                        rejected = gateway.call('compose_document', feedback, identity='broken-review')
                        self.assertEqual(rejected.status, 'error')
                    with changed_record(source, b'run,score\nfirst,9.0\n'):
                        rejected = gateway.call('compose_document', feedback, identity='changed-evidence')
                        self.assertEqual(rejected.status, 'error')
                    factory.assert_not_called()
                self.assertEqual({p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}, original)

    def test_optional_review_preserves_historical_request_shape(self):
        from pydantic import ValidationError
        from simple_ar.tools.writing import ComposeRequest
        request = ComposeRequest(goal='Revise from evidence', title='Article')
        self.assertNotIn('professional_review', request.model_dump(mode='json'))
        revised = ComposeRequest(goal=request.goal, title=request.title, prior_draft='draft.md',
            professional_review={'max_revision_rounds': 1})
        self.assertEqual(revised.professional_review.max_revision_rounds, 1)
        with self.assertRaises(ValidationError):
            ComposeRequest(goal=request.goal, title=request.title,
                professional_review={'max_revision_rounds': 4})

    def test_native_adapter_keeps_exact_inputs_and_shared_owner(self):
        from simple_ar.report.writing import run_composition_article
        config = ReportRuntimeConfig(outline_strategy='template', reviewer='disabled',
            document_review=False, max_review_iterations=0, allow_source_backtracking=False)
        inputs = {'binding': {'request': {'goal': 'Original'}},
            'assembly_config': config.model_dump(mode='json'),
            'article': {'title': 'Original', 'goal': 'Revise', 'target_words': 0,
                'source_handles': [], 'bibliography': [], 'citation_key_map': {},
                'prior_draft': 'Existing draft'}}
        with tempfile.TemporaryDirectory() as tmp, patch(
                'simple_ar.report.writing.run_report_writing_capability') as owner:
            run_composition_article(store=ArtifactStore(Path(tmp)), inputs=inputs, identity='same-call')
        passed = owner.call_args.kwargs
        self.assertIs(passed['request'].composition_inputs, inputs)
        self.assertEqual(passed['request'].prior_draft, 'Existing draft')
        self.assertEqual(passed['context'].attempt.attempt_id, 'same-call')
        self.assertEqual(passed['request'].method, 'article')


class ReportAuditCapabilityTests(unittest.TestCase):
    @staticmethod
    def report_session(root, identity, *, audit=False):
        registry = CapabilityRegistry()
        registry.register("report_audit" if audit else "report",
            run_report_audit_capability if audit else run_report_capability)
        return SessionController.create(root, session_id=identity, topic="offline report",
            profile="paper_audit", registry=registry)

    def test_experiment_figures_are_enabled_by_default_and_can_be_disabled(self):
        self.assertTrue(ReportFigureConfig().enabled)
        self.assertEqual(ReportFigureConfig(mode="off").mode, "off")

    def test_measured_figures_use_declared_groups_and_keep_source_refs(self):
        import xml.etree.ElementTree as ET
        pair = {"seed": 0, "comparability": "declared_match", "baseline": {"status": "passed"},
            "candidate": {"status": "passed"}, "baseline_ref": {"path": "baseline.json"},
            "candidate_ref": {"path": "candidate.json"}, "metrics": [{"name": "accuracy", "baseline": 0.5, "candidate": 1.0}]}
        summary = {"group_id": 0, "metric": "accuracy", "unit": "fraction",
                   "sources": [{"baseline_ref": pair["baseline_ref"], "candidate_ref": pair["candidate_ref"]}]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            request = ReportAssemblyRequest(title="Paired evaluation", sections=(ReportSectionDraft(
                section_id="results", heading="Results", draft_markdown="Measured results."),),
                config=ReportRuntimeConfig(figures=ReportFigureConfig(enabled=True)),
                paired_comparisons=(pair,), paired_summaries=(summary,))
            result = assemble_report_document(request, report_dir=root)
            self.assertEqual(len(result.figures), 1)
            figure = result.figures[0]
            self.assertEqual(figure.source_artifacts, ["baseline.json", "candidate.json"])
            svg = ET.parse(root / figure.path)
            text = " ".join(svg.getroot().itertext())
            self.assertIn("Seed 0", text)
            self.assertIn("baseline", text)
            self.assertIn("candidate", text)
            for suffix in ("pdf", "png"):
                self.assertEqual(figure.exports[suffix], str(Path(figure.path).with_suffix("." + suffix)))
                self.assertTrue((root / figure.exports[suffix]).is_file())
            self.assertIn("fraction", figure.title)
            self.assertIn(figure.path, result.report_markdown)
            self.assertIn(figure.path, result.report_body_markdown)
            from dataclasses import replace
            absent = assemble_report_document(replace(request, paired_summaries=()), report_dir=root / "no-data")
            self.assertEqual(absent.figures, ())
            # Figure ownership follows IDs even after a translated heading;
            # there is no second generic comparisons section in that case.
            owned = replace(request, sections=(ReportSectionDraft(section_id="results", heading="实测表现",
                draft_markdown="Measured results."), ReportSectionDraft(section_id="discussion", heading="Discussion",
                draft_markdown="Interpretation.")), document_plan=ReportDocumentPlan(argument_plan=ReportArgumentPlan(
                question="What was measured?", answer="Recorded pairs.", points=[ReportArgumentPoint(
                    claim="Measured accuracy", section_id="results", metric_ids=["observed-accuracy"])])),
                experiment_context=ReportContext(topic="Measured results", report_mode="experiment", metric_sources=[MetricSource(
                    metric_id="observed-accuracy", name="accuracy", value=1., artifact="candidate.json")]))
            placed = assemble_report_document(owned, report_dir=root / "owned")
            self.assertEqual(placed.figures[0].anchor, "实测表现")
            self.assertNotIn("## Measured comparisons", placed.report_body_markdown)
            self.assertLess(placed.report_body_markdown.index("paired-1.svg"), placed.report_body_markdown.index("## Discussion"))
            ambiguous_plan = owned.document_plan.model_copy(deep=True)
            ambiguous_plan.argument_plan.points.append(ReportArgumentPoint(claim="Interpretation", section_id="discussion",
                metric_ids=["observed-accuracy"]))
            ambiguous = assemble_report_document(replace(owned, document_plan=ambiguous_plan), report_dir=root / "ambiguous")
            self.assertEqual(ambiguous.figures[0].anchor, "Measured comparisons")
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(root / "session", session_id="paired-report", topic="Recorded pairs",
                profile="paper_audit", registry=registry)
            delivery = controller.execute_attempt("report", attempt_id="report-1", request=owned)
            self.assertEqual(delivery.status, "completed")
            exported = {Path(ref.path).suffix for ref in delivery.artifacts if ref.kind == "figure"}
            self.assertEqual(exported, {".svg", ".pdf", ".png"})

    def test_default_paired_figures_keep_task_metric_order_without_domain_bias(self):
        metrics = [
            "rmse", "energy", "yield", "runtime", "accuracy",
        ]
        pair = {
            "seed": 0,
            "comparability": "declared_match",
            "baseline": {"status": "passed"},
            "candidate": {"status": "passed"},
            "baseline_ref": {"path": "baseline.json"},
            "candidate_ref": {"path": "candidate.json"},
            "metrics": [
                {"name": name, "baseline": 0.4, "candidate": 0.5}
                for name in metrics
            ],
        }
        summaries = [
            {
                "group_id": 0,
                "metric": name,
                "unit": "fraction",
                "sources": [{
                    "baseline_ref": pair["baseline_ref"],
                    "candidate_ref": pair["candidate_ref"],
                }],
            }
            for name in metrics
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = assemble_report_document(
                ReportAssemblyRequest(
                    title="Compact paired evaluation",
                    sections=(ReportSectionDraft(
                        section_id="results",
                        heading="Results",
                        draft_markdown="Measured results.",
                    ),),
                    paired_comparisons=(pair,),
                    paired_summaries=tuple(summaries),
                ),
                report_dir=Path(tmp),
            )

        self.assertEqual(len(result.figures), 4)
        titles = " ".join(figure.title for figure in result.figures)
        self.assertIn("rmse", titles)
        self.assertIn("yield", titles)
        self.assertNotIn("accuracy", titles)
        self.assertTrue(result.figures[0].title.startswith("rmse"))

    def test_report_capability_persists_verified_reference_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(tmp, "report-reference-session")

            result = controller.execute_attempt(
                "report",
                attempt_id="attempt-001",
                request=ReportAssemblyRequest(
                    title="Offline report",
                    papers=(
                        {"id": "paper-1", "title": "Unused paper"},
                        {
                            "id": "paper-2",
                            "title": "Used paper",
                            "url": "https://example.test/paper-2",
                        },
                    ),
                    sections=(
                        ReportSectionDraft(
                            section_id="findings",
                            heading="Findings",
                            draft_markdown="The result is supported [@paper-2]. Another claim [@missing].",
                        ),
                    ),
                ),
            )

            self.assertEqual(result.status, "completed")
            attempt_root = Path(tmp) / "attempts" / "attempt-001"
            report = (attempt_root / "report.md").read_text(encoding="utf-8")
            body = (attempt_root / "report_body.md").read_text(encoding="utf-8")
            citation_map = json.loads(
                (attempt_root / "citation_map.json").read_text(encoding="utf-8")
            )

            self.assertIn("## References", report)
            self.assertIn("[1] Used paper", report)
            self.assertNotIn("Unused paper", report)
            self.assertIn("[@paper-2]", body)
            self.assertNotIn("## References", body)
            self.assertEqual(citation_map["entries"][0]["paper_id"], "paper-2")
            self.assertIn(
                "@misc{paper-2",
                (attempt_root / "references.bib").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                [artifact.kind for artifact in result.artifacts[:4]],
                ["report", "report_body", "report_references", "citation_map"],
            )
            self.assertNotIn("@missing", body)
            self.assertEqual(json.loads((attempt_root / "citation_cleanup.json").read_text(encoding="utf-8")),
                             {"removed_citations": ["missing"]})
            refs = {
                ref.kind: controller.store.ref(f"attempts/attempt-001/{ref.path}", kind=ref.kind)
                for ref in result.artifacts
            }
            audit_store = ArtifactStore(Path(tmp) / "audit")
            audit_result = run_report_audit_capability(
                context=CapabilityContext(store=audit_store, attempt=AttemptManifest("audit"),
                                          inputs=tuple(refs.values()), input_store=controller.store),
                request=ReportAuditCapabilityRequest(report_ref=refs["report"], report_body_ref=refs["report_body"],
                    citation_cleanup_ref=refs["citation_cleanup"],
                    context=ReportContext(topic="Offline report", report_mode="research_only",
                                          papers=[{"id": "paper-2", "title": "Used paper"}]), memory=ReportMemory()),
            )
            self.assertEqual(audit_result.status, "failed")
            audit = audit_store.read_json("report_audit.json")
            self.assertIn("missing", audit["citation_audit"]["unknown_citations"])
            # Empty bibliographies use the same citation owner; absence of
            # registered papers must not let invented keys survive assembly.
            unregistered_request = ReportAssemblyRequest(title="Supplied analysis",
                sections=(ReportSectionDraft(section_id="results", heading="Results",
                    draft_markdown="[Dataset](https://example.test/data) [@P1]."),))
            unregistered = preview_report_document(unregistered_request)
            self.assertNotIn("@P1", unregistered.report_body_markdown)
            self.assertIn("https://example.test/data", unregistered.report_body_markdown)
            self.assertEqual(unregistered.removed_citations, ("P1",))
            delivery = controller.execute_attempt("report", attempt_id="attempt-002",
                request=unregistered_request)
            self.assertEqual(delivery.status, "completed")
            self.assertIn("https://example.test/data", controller.store.read_text("attempts/attempt-002/report.md"))
            self.assertEqual(controller.store.read_json("attempts/attempt-002/citation_map.json")["entries"], [])
            self.assertEqual(controller.store.read_text("attempts/attempt-002/report_body.md"), unregistered.report_body_markdown)

    def test_report_capability_renders_only_planned_figures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(tmp, "report-figure-session")
            request = ReportAssemblyRequest(title="Offline report", template_name="paper",
                papers=({"id": "paper-1", "title": "Supplied source"},),
                document_plan=ReportDocumentPlan(visual_intents=[ReportVisualIntent(
                    visual_id="taxonomy", kind="figure", title="Conceptual taxonomy",
                    purpose="Show the organizing concepts.", section_id="stable-id",
                    evidence_handles=["paper-1"], view="taxonomy-map")]),
                sections=(ReportSectionDraft(section_id="stable-id", heading="概念分类",
                    draft_markdown="Evidence-backed claim [@paper-1].\n\n- Method A\n- Method B\n- Method C"),))
            result = controller.execute_attempt("report", attempt_id="attempt-001", request=request)
            self.assertEqual(result.status, "completed")
            refs = [ref for ref in result.artifacts if ref.kind == "figure"]
            self.assertEqual([(ref.path, ref.status) for ref in refs], [("figures/taxonomy-map.svg", "available")])
            manifest = controller.store.read_json("attempts/attempt-001/figures/figures_manifest.json")
            self.assertEqual(manifest["figures"][0]["source_artifacts"], ["paper-1"])
            for name in ("report_body.md", "report.md"):
                self.assertIn(refs[0].path, controller.store.read_text(f"attempts/attempt-001/{name}"))
            self.assertIn("[@paper-1]", controller.store.read_text("attempts/attempt-001/report_body.md"))

    def test_missing_renderer_output_is_explicitly_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(tmp, "report-missing-figure")

            request = ReportAssemblyRequest(
                title="Offline report",
                papers=({"id": "p1", "title": "Caption source"},
                        {"id": "p2", "title": "Body source"},
                        {"id": "unused", "title": "Unused source"}),
                citation_key_map={"P1": "p1", "P2": "p2"},
                sections=(ReportSectionDraft(section_id="introduction", heading="Introduction",
                    draft_markdown="A short report [@P2]."),),
            )
            result = controller.execute_attempt(
                "report", attempt_id="attempt-001", request=request,
                figure_renderer=_MissingFigureRenderer(caption="Caption evidence [@p1]."),
            )

            self.assertEqual(result.status, "partial")
            self.assertTrue(any("missing artifact" in item for item in result.diagnostics))
            figure_refs = [artifact for artifact in result.artifacts if artifact.kind == "figure"]
            self.assertEqual(len(figure_refs), 1)
            self.assertEqual(figure_refs[0].status, "missing")
            self.assertEqual(controller.manifest.status, "running")
            report = controller.store.read_text("attempts/attempt-001/report.md")
            body = controller.store.read_text("attempts/attempt-001/report_body.md")
            entries = controller.store.read_json("attempts/attempt-001/citation_map.json")["entries"]
            self.assertEqual([(row["paper_id"], row["number"], row["model_key"]) for row in entries],
                             [("p1", 1, "P1"), ("p2", 2, "P2")])
            self.assertIn("Caption evidence [1].", report)
            self.assertIn("A short report [2].", report)
            self.assertIn("Caption evidence [@p1].", body)
            self.assertNotIn("Unused source", report)
            bib = controller.store.read_text("attempts/attempt-001/references.bib")
            self.assertIn("@misc{p1", bib)
            self.assertIn("@misc{p2", bib)
            self.assertNotIn("@misc{unused", bib)
            self.assertEqual(result.usage["cited_paper_count"], 2)
            # Check the actual caption-bearing body, not just the Writer's
            # earlier source count. Preview has one source; rendering adds another.
            bounded = replace(request, config=ReportRuntimeConfig(max_cited_sources=1))
            self.assertEqual(len(preview_report_document(bounded).cited_papers), 1)
            with self.assertRaisesRegex(ValueError, 'Final report exceeds max_cited_sources'):
                assemble_report_document(bounded, report_dir=Path(tmp) / 'bounded',
                    figure_renderer=_MissingFigureRenderer(caption="Caption evidence [@p1]."))

    def test_assembly_uses_frozen_title_and_preserves_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            section = ReportSectionDraft(section_id="findings", heading="Findings", draft_markdown="Observed values.")
            request = ReportAssemblyRequest(title="Long task instructions, not an article title", sections=(section,),
                document_plan=ReportDocumentPlan(title="A bounded observation"))
            result = assemble_report_document(request, report_dir=root)
            self.assertTrue(result.report_markdown.startswith("# A bounded observation\n"))
            self.assertTrue(result.report_body_markdown.startswith("# A bounded observation\n"))
            # Mapping inputs, typed inputs, preview and actual assembly use
            # one request normalization boundary; explicit false/zero survive.
            config = {'figures': {'enabled': False}, 'max_section_tokens': 0}
            section_row = section.model_dump(mode='json')
            plan_row = request.document_plan.model_dump(mode='json')
            mapped = replace(request, sections=(section_row,), config=config, document_plan=plan_row)
            self.assertIsInstance(mapped.sections[0], ReportSectionDraft)
            self.assertIsInstance(mapped.config, ReportRuntimeConfig)
            self.assertIsInstance(mapped.document_plan, ReportDocumentPlan)
            rendered = assemble_report_document(mapped, report_dir=root)
            self.assertEqual(preview_report_document(mapped), rendered)
            self.assertEqual(mapped.config.max_section_tokens, 0)
            self.assertFalse(mapped.config.figures.enabled)
            self.assertEqual(config, {'figures': {'enabled': False}, 'max_section_tokens': 0})
            self.assertEqual(section_row, section.model_dump(mode='json'))
            self.assertEqual(plan_row, request.document_plan.model_dump(mode='json'))
            self.assertIs(replace(mapped, title='Another title').config, mapped.config)
            for plan in (None, ReportDocumentPlan.model_validate({"schema_version": "report_document_plan.v1"})):
                legacy = assemble_report_document(replace(request, document_plan=plan), report_dir=root)
                self.assertTrue(legacy.report_markdown.startswith("# Long task instructions, not an article title\n"))

    def test_whole_article_preserves_structure_through_preview_and_delivery(self) -> None:
        code = '````text\n# Literal title\n## References\n```\nvalue with two spaces  \n````'
        article = ('# Authored title\n\n## Findings\n\nSupported claim [@P1].\n\n'
                   '### Calibration\n\n#### Limits\n\n' + code +
                   '\n\n## Discussion\n\nBounded interpretation.\n\n'
                   '## References\n\nObsolete bibliography.')
        request = ReportAssemblyRequest(title="Task instructions", assembly_mode="whole_article",
            document_plan=ReportDocumentPlan(title="Canonical title"),
            sections=(ReportSectionDraft(section_id="arbitrary-unit", heading="Transport label",
                draft_markdown=article, status="revised"),),
            config=ReportRuntimeConfig(section_numbering="academic", figures=ReportFigureConfig(enabled=False)),
            papers=({"id": "paper-1", "title": "Recorded source"},),
            citation_key_map={"P1": "paper-1"})
        preview = preview_report_document(request)
        self.assertEqual(request.assembly_mode, ReportAssemblyMode.WHOLE_ARTICLE)
        self.assertTrue(preview.report_body_markdown.startswith("# Canonical title\n"))
        self.assertEqual(preview.report_body_markdown.count("# Canonical title\n"), 1)
        self.assertNotIn("Authored title", preview.report_body_markdown)
        self.assertNotIn("Transport label", preview.report_body_markdown)
        self.assertIn("## Findings\n", preview.report_body_markdown)
        self.assertIn("### Calibration\n\n#### Limits", preview.report_body_markdown)
        self.assertIn(code, preview.report_body_markdown)
        self.assertIn(code, preview.report_markdown)
        self.assertIn("Supported claim [@paper-1].", preview.report_body_markdown)
        self.assertIn("Supported claim [1].", preview.report_markdown)
        self.assertIn("## 1 Findings\n", preview.report_markdown)
        self.assertIn("### 1.1 Calibration\n\n#### 1.1.1 Limits", preview.report_markdown)
        self.assertIn("## 2 Discussion\n", preview.report_markdown)
        self.assertNotIn("Obsolete bibliography", preview.report_markdown)
        self.assertIn("Recorded source", preview.references_markdown)
        # Neither identity nor a matching transport label determines form.
        for heading in ("Canonical title", "Unrelated label"):
            relabeled = replace(request, sections=(request.sections[0].model_copy(
                update={"section_id": "another-unit", "heading": heading}),))
            self.assertEqual(preview_report_document(relabeled), preview)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(assemble_report_document(request, report_dir=Path(tmp)), preview)
            store = ArtifactStore(Path(tmp) / "delivery")
            delivered = run_report_capability(
                context=CapabilityContext(store=store, attempt=AttemptManifest("article")), request=request)
            self.assertEqual(delivered.status, "completed")
            self.assertEqual(delivered.provenance["assembly"], "whole_article")
            self.assertEqual(store.read_text("report.md"), preview.report_markdown)
            self.assertEqual(store.read_text("report_body.md"), preview.report_body_markdown)
            self.assertEqual(store.read_json("citation_map.json")["entries"][0]["paper_id"], "paper-1")

    def test_whole_article_without_authored_title_and_legacy_defaults(self) -> None:
        section = ReportSectionDraft(section_id="arbitrary-unit", heading="Report",
            draft_markdown="## Findings\n\n### Calibration\n\nAn observation.")
        request = ReportAssemblyRequest(title="Report", sections=(section,),
            config={"section_numbering": "off", "figures": {"enabled": False}})
        self.assertEqual(request.assembly_mode, ReportAssemblyMode.SECTION_DRAFTS)
        legacy = preview_report_document(request).report_body_markdown
        self.assertEqual(legacy, "# Report\n\n### Findings\n\n### Calibration\n\nAn observation.\n")
        whole = preview_report_document(replace(request, assembly_mode="whole_article")).report_body_markdown
        self.assertEqual(whole, "# Report\n\n" + section.draft_markdown + "\n")
        wrapped = preview_report_document(replace(request, sections=(section.model_copy(
            update={"heading": "Results"}),))).report_body_markdown
        self.assertIn("## Results\n\n### Findings", wrapped)
        mapped = replace(request, sections=(section.model_dump(mode="json"),), assembly_mode="whole_article")
        self.assertEqual(preview_report_document(mapped).report_body_markdown, whole)

    def test_whole_article_refuses_incomplete_or_ambiguous_inputs(self) -> None:
        section = ReportSectionDraft(section_id="unit", heading="Label", draft_markdown="## Findings\nEvidence.")
        for sections in ((), (section, section),
                         (section.model_copy(update={"status": "skipped"}),),
                         (section.model_copy(update={"draft_markdown": "  "}),)):
            with self.subTest(sections=sections), self.assertRaises(ValueError):
                ReportAssemblyRequest(title="Report", sections=sections, assembly_mode="whole_article")
        with self.assertRaises(ValueError):
            ReportAssemblyRequest(title="Report", sections=(section,), assembly_mode="unknown")
        for body in ("# Title only", "# Title\n\n## References\nOld bibliography.",
                     "# Title\n\n## Findings\nEvidence.\n\n# Another document\nOther prose."):
            with self.subTest(body=body), self.assertRaises(ValueError):
                preview_report_document(ReportAssemblyRequest(title="Report", assembly_mode="whole_article",
                    sections=(section.model_copy(update={"draft_markdown": body}),)))
        # Failed writer rows are rejected by the existing section schema.
        with self.assertRaises(ValueError):
            ReportAssemblyRequest(title="Report", assembly_mode="whole_article", sections=({
                "section_id": "unit", "heading": "Label", "status": "failed", "draft_markdown": "Evidence."},))

    def test_whole_article_keeps_existing_experiment_evidence_append_owner(self) -> None:
        request = ReportAssemblyRequest(title="Measured report", assembly_mode="whole_article",
            sections=(ReportSectionDraft(section_id="article-unit", heading="Transport label",
                draft_markdown="# Earlier title\n\n## Findings\n\nRecorded values."),),
            config={"section_numbering": "off", "data_tables": "full", "figures": {"enabled": False}},
            experiment_context=ReportContext(topic="Recorded values", report_mode="experiment",
                metric_sources=[MetricSource(metric_id="accuracy-record", name="accuracy", value=0.5,
                    artifact="candidate.json")]))
        with tempfile.TemporaryDirectory() as tmp:
            preview = preview_report_document(request)
            self.assertEqual(preview, assemble_report_document(request, report_dir=Path(tmp)))
        self.assertIn("## Findings\n\nRecorded values.", preview.report_body_markdown)
        self.assertIn("accuracy", preview.report_body_markdown)
        self.assertNotIn("Transport label", preview.report_body_markdown)
        self.assertEqual(len(request.sections), 1)

    def test_report_capability_uses_document_plan_order_without_dropping_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(tmp, "report-order-session")

            result = controller.execute_attempt(
                "report",
                attempt_id="attempt-001",
                request=ReportAssemblyRequest(
                    title="Ordered report",
                    document_plan=ReportDocumentPlan(
                        sections=[
                            ReportSectionPlan(
                                section_id="results",
                                heading="Results",
                                goal="Present results.",
                                final_order=2,
                            ),
                            ReportSectionPlan(
                                section_id="introduction",
                                heading="Introduction",
                                goal="Orient the reader.",
                                final_order=1,
                            ),
                        ]
                    ),
                    sections=(
                        ReportSectionDraft(
                            section_id="results",
                            heading="Results",
                            draft_markdown="The result is reproducible.",
                        ),
                        ReportSectionDraft(
                            section_id="appendix",
                            heading="Appendix",
                            draft_markdown="Additional detail.",
                        ),
                        ReportSectionDraft(
                            section_id="introduction",
                            heading="Introduction",
                            draft_markdown="The task is bounded.",
                        ),
                    ),
                ),
            )

            self.assertEqual(result.status, "completed")
            report = controller.store.read_text("attempts/attempt-001/report.md")
            self.assertLess(report.index("## Introduction"), report.index("## Results"))
            self.assertLess(report.index("## Results"), report.index("## Appendix"))

    def test_session_adapter_reads_explicit_report_inputs_and_preserves_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(tmp, "report-audit-session", audit=True)
            report_ref = controller.store.write_text(
                "input/report.md",
                "# Report\n\nSupported claim [@paper-1].\n",
                kind="report",
            )
            body_ref = controller.store.write_text(
                "input/report-body.md",
                "# Report\n\nSupported claim [@paper-1].\n",
                kind="report_body",
            )

            result = controller.execute_attempt(
                "report_audit",
                attempt_id="attempt-001",
                inputs=(report_ref, body_ref),
                request=ReportAuditCapabilityRequest(
                    report_ref=report_ref,
                    report_body_ref=body_ref,
                    context=ReportContext(
                        topic="offline report",
                        report_mode="research_only",
                        papers=[{"id": "paper-1"}],
                    ),
                    memory=ReportMemory(),
                ),
            )

            self.assertEqual(result.status, "completed")
            output = controller.store.read_json(
                "attempts/attempt-001/report_audit.json"
            )
            self.assertEqual(output["status"], "passed")
            self.assertEqual(output["schema_version"], 1)
            self.assertEqual(
                result.provenance["report_ref"], "input/report.md"
            )

    def test_failed_audit_is_not_reported_as_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = self.report_session(Path(tmp), "report-audit-failure", audit=True)
            report_ref = controller.store.write_text(
                "input/report.md",
                "# Report\n\nUnsupported citation [@missing].\n",
            )

            result = controller.execute_attempt(
                "report_audit",
                attempt_id="attempt-001",
                inputs=(report_ref,),
                request=ReportAuditCapabilityRequest(
                    report_ref=report_ref,
                    context=ReportContext(
                        topic="offline report",
                        report_mode="research_only",
                        papers=[{"id": "paper-1"}],
                    ),
                    memory=ReportMemory(),
                ),
            )

            self.assertEqual(result.status, "failed")
            self.assertTrue(result.diagnostics)
            self.assertEqual(
                controller.store.read_json(
                    "attempts/attempt-001/report_audit.json"
                )["status"],
                "failed",
            )


class ArticleClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            'draft_markdown': '## Observations\n\nRecorded value: 0.75.\n\n## Limits\n\nOne declared condition.'}

    def ask_json(self, system, prompt, **kwargs):
        self.calls.append((system, json.loads(prompt), kwargs))
        return self.response


class StructuredArticleWritingTests(unittest.TestCase):
    def test_article_projection_keeps_exact_current_figure_paths_and_caption_instructions(self):
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.report.writing import ARTICLE_SYSTEM, article_writing_inputs

        request = self.request()
        figures = [SourceHandle(handle=f'figure:{path}', kind='material', artifact=path,
            title=f'input-{index}.png', summary=f'Embed as a standalone Markdown image at {path}.',
            metadata={'evidence_role': 'supplied_figure_not_visually_or_scientifically_verified',
                      **({'caption': 'Exact supplied caption [@P1].'} if index == 1 else {})})
            for index, path in enumerate(('figures/selected-1.png', 'figures/selected-2.svg'), 1)]
        context = request.report_context.model_copy(deep=True)
        context.source_handles.extend(figures)
        binding = {'context': context.model_dump(mode='json'),
            'memory': request.memory.model_dump(mode='json'),
            'template': request.template.model_dump(mode='json')}
        prior = '![Stale caption](figures/old.png)'
        for structured in (False, True):
            with self.subTest(structured=structured):
                inputs = article_writing_inputs(report_context=context, documents=None,
                    binding=binding, config=request.config, title=context.topic,
                    goal=context.goal_markdown, target_words=137,
                    prior_draft=prior, projection_ref=ArtifactRef(path='report_snapshot.json')
                        if structured else None)
                article = inputs['article']
                projected = article['source_handles'][-2:]
                self.assertEqual([row['artifact'] for row in projected],
                    ['figures/selected-1.png', 'figures/selected-2.svg'])
                self.assertEqual([row['summary'] for row in projected], [row.summary for row in figures])
                self.assertEqual(projected[0]['metadata']['caption'], 'Exact supplied caption [@P1].')
                self.assertNotIn('caption', projected[1]['metadata'])
                self.assertEqual(article['prior_draft'], prior)
                self.assertEqual(article['target_words'], 137)
                self.assertNotIn('constraints', article)
                self.assertEqual(context.source_handles[-2:], figures)
        self.assertIn('exact artifact path as the image destination', ARTICLE_SYSTEM)
        self.assertIn('prior-draft image links do not', ARTICLE_SYSTEM)

    def test_section_call_records_bridge_checkpoints_without_replaying_unknown_responses(self):
        from simple_ar.integrations.llm import LLMError
        from simple_ar.report.writing import retained_writing_client
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            provider = Mock()
            provider.ask_json.side_effect = [{'draft_markdown': 'First complete section.'},
                                            LLMError('Interrupted stream; usage unknown')]
            client = retained_writing_client(store=store, identity='sections', client=provider)
            first = client.ask_json('Evidence-bound writing', '{"section":"first"}',
                                    label='section-first', max_output_tokens=1000)
            self.assertEqual(first['draft_markdown'], 'First complete section.')
            options = provider.ask_json.call_args.kwargs
            self.assertEqual(options['label'], 'section-first')
            self.assertEqual(options['max_output_tokens'], 1000)
            self.assertTrue(options['budget_call_id'].startswith('sections:writing:'))
            # A complete response survives a crash before checkpoint commit.
            recovered = retained_writing_client(store=store, identity='sections', client=None)
            self.assertEqual(recovered.ask_json('Evidence-bound writing', '{"section":"first"}',
                label='section-first', max_output_tokens=1000), first)
            self.assertEqual(provider.ask_json.call_count, 1)
            with self.assertRaisesRegex(LLMError, 'usage unknown'):
                client.ask_json('Evidence-bound writing', '{"section":"second"}', label='section-second')
            for restoring in (client, recovered):
                with self.assertRaisesRegex(ValueError, 'no blind replay'):
                    restoring.ask_json('Evidence-bound writing', '{"section":"second"}', label='section-second')
            self.assertEqual(provider.ask_json.call_count, 2)
            record = next(path for path in (store.root / 'model_calls').iterdir()
                          if (path / 'response.json').exists())
            (record / 'response.json').write_text('{"response": {"draft_markdown": "Replaced"}}')
            with self.assertRaisesRegex(ValueError, 'complete bound record'):
                client.ask_json('Evidence-bound writing', '{"section":"first"}',
                                label='section-first', max_output_tokens=1000)
            self.assertEqual(provider.ask_json.call_count, 2)

    def request(self, client=None):
        config = ReportRuntimeConfig(reviewer='disabled', max_review_iterations=0,
            allow_source_backtracking=False, outline_strategy='auto', max_section_tokens=900)
        context = ReportContext(topic='Recorded observations', report_mode='supplied_materials',
            goal_markdown='Explain the observed result and its limits.',
            source_handles=[SourceHandle(handle='material:result', kind='material',
                summary='Recorded result under one declared condition.',
                metadata={'evidence_passages': [{'text': 'Recorded value: 0.75.'}],
                          'evidence_passages_truncated': True})],
            metric_sources=[MetricSource(metric_id='observed', name='score', value=.75,
                artifact='result.json', source_kind='measurement')],
            results={'status': 'partial', 'conditions': ['declared condition']})
        template = ReportTemplateBundle(name='custom', mode='supplied_materials',
            template_path='custom.md', criteria_path='criteria.md',
            template_markdown='# Writing instructions\n\nExplain Observations then Limits.',
            criteria_markdown='Do not claim an inspection occurred.')
        return ReportWritingRequest(context, ReportMemory(limitations=['Input coverage is partial.']),
            config, template, client or ArticleClient(), method='article')

    def test_explicit_section_composition_uses_existing_checkpoint_and_retained_calls(self):
        from simple_ar.integrations.llm import LLMError
        from simple_ar.report.writing import write_section_article
        from simple_ar.research.documents.ingest import DocumentBundle
        from tests.report_review_fixtures import prompt_payload

        for interruption in ('none', 'unknown-response', 'checkpoint-write'):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as tmp:
                owner = ArtifactStore(Path(tmp))
                writer = ArtifactStore(owner.root / 'writer')
                documents = owner.write_json('documents.json', DocumentBundle([], {}, {}, [], []).to_handoff_dict(),
                    kind='documents', schema='document_bundle.v1')
                calls = []
                class Client:
                    def ask_json(self, system, prompt, *, label='', **options):
                        calls.append((label, options))
                        if interruption == 'unknown-response' and len(calls) == 2:
                            raise LLMError('Unknown second section request')
                        section = prompt_payload(prompt)['section']
                        return {'section_id': section['section_id'], 'heading': section['heading'],
                                'draft_markdown': 'Only supplied observations; no independent verification.'}
                context = self.request().report_context
                arguments = dict(store=writer, input_store=owner, documents_ref=documents,
                    identity='section-composition', report_context=context, title='Evidence brief',
                    target_words=3000, required_headings=['Observations', 'Limits'],
                    max_cited_sources=0, prior_draft='Earlier draft requiring correction.')
                if interruption == 'unknown-response':
                    with self.assertRaisesRegex(LLMError, 'Unknown second section'):
                        write_section_article(**arguments, client=Client())
                    checkpoint = writer.read_json('sections.json')
                    self.assertEqual(len(checkpoint['completed']['sections']), 1)
                    self.assertFalse(writer.exists('writer.json'))
                    with self.assertRaisesRegex(LLMError, 'no blind replay'):
                        write_section_article(**arguments, recovering=True)
                elif interruption == 'checkpoint-write':
                    write_json = ArtifactStore.write_json
                    def interrupted_checkpoint(store, path, payload, **options):
                        if path == 'sections.json' and len(payload['completed']['sections']) == 2:
                            raise OSError('Checkpoint commit interrupted after complete response')
                        return write_json(store, path, payload, **options)
                    with patch.object(ArtifactStore, 'write_json', interrupted_checkpoint):
                        with self.assertRaisesRegex(OSError, 'Checkpoint commit interrupted'):
                            write_section_article(**arguments, client=Client())
                    self.assertEqual(len(writer.read_json('sections.json')['completed']['sections']), 1)
                    restored = write_section_article(**arguments, recovering=True)
                    self.assertEqual([row['heading'] for row in restored['sections']], ['Observations', 'Limits'])
                else:
                    result = write_section_article(**arguments, client=Client())
                    self.assertEqual([row['heading'] for row in result['sections']], ['Observations', 'Limits'])
                    restored = write_section_article(**arguments, recovering=True)
                    self.assertEqual(result['sections'], restored['sections'])
                    self.assertFalse(result['reviewer_findings'])
                    # Neither valid-shaped checkpoint edits nor changed runtime
                    # inputs may replace the original complete producer records.
                    original = {path: path.read_bytes() for path in writer.root.rglob('*.json')}
                    retained_writer = writer.read_json('writer.json')
                    retained_writer['sections'][0]['draft_markdown'] = 'Changed completed writer prose.'
                    writer.write_json('writer.json', retained_writer)
                    changed = {path: path.read_bytes() for path in original}
                    with self.assertRaisesRegex(ValueError, 'differ from original response'):
                        write_section_article(**arguments, recovering=True)
                    self.assertEqual({path: path.read_bytes() for path in changed}, changed)
                    (writer.root / 'writer.json').write_bytes(original[writer.root / 'writer.json'])
                    checkpoint = writer.read_json('sections.json')
                    checkpoint['completed']['sections'][0]['draft_markdown'] = 'Altered checkpoint prose.'
                    writer.write_json('sections.json', checkpoint)
                    changed = {path: path.read_bytes() for path in original}
                    with self.assertRaisesRegex(LLMError, 'complete response producers'):
                        write_section_article(**arguments, recovering=True)
                    self.assertEqual({path: path.read_bytes() for path in changed}, changed)
                    (writer.root / 'sections.json').write_bytes(original[writer.root / 'sections.json'])
                    checkpoint = writer.read_json('sections.json')
                    checkpoint['completed']['sections'].append({
                        **checkpoint['completed']['sections'][-1], 'section_id': 'extra', 'heading': 'Extra'})
                    writer.write_json('sections.json', checkpoint)
                    changed = {path: path.read_bytes() for path in original}
                    with self.assertRaisesRegex(ValueError, 'unmatched complete response producers'):
                        write_section_article(**arguments, recovering=True)
                    self.assertEqual({path: path.read_bytes() for path in changed}, changed)
                    (writer.root / 'sections.json').write_bytes(original[writer.root / 'sections.json'])
                    with self.assertRaisesRegex(ValueError, 'Frozen section input changed'):
                        write_section_article(**{**arguments, 'target_words': 4000}, recovering=True)
                    self.assertEqual({path: path.read_bytes() for path in original}, original)
                self.assertEqual([label for label, _ in calls],
                                 ['report-writer-section_1', 'report-writer-section_2'])
                self.assertEqual(len(list((writer.root / 'model_calls').iterdir())), 2)
                self.assertNotIn('prior_draft', [row.handle for row in context.source_handles])

    def test_native_composition_selects_section_granularity_without_changing_legacy_or_frozen_methods(self):
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.writing import ComposeRequest, WritingToolGateway
        from tests.report_review_fixtures import prompt_payload

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'results.csv'
            source.write_text('run,score\nfirst,0.75\n')
            calls = []
            class Client:
                def ask_json(self, system, prompt, *, label='', **options):
                    calls.append(label)
                    view = prompt_payload(prompt)
                    self_evidence = json.dumps(view)
                    if '0.75' not in self_evidence:
                        raise AssertionError('Section Writer lost the original supplied values')
                    section = view['section']
                    return {'section_id': section['section_id'], 'heading': section['heading'],
                            'draft_markdown': 'Recorded score 0.75; one supplied run, not a new measurement.'}
            ledger = BudgetLedger()
            (root / 'task').mkdir()
            workspace = WorkspaceToolGateway(root / 'task', {'source0': source}, ledger)
            gateway = WritingToolGateway(root / 'task', workspace, ledger, 'bound-client', 'task',
                client_factory=lambda _: Client())
            request = {'goal': 'Explain the supplied results and limitations.', 'title': 'Results note',
                'materials': [{'path': 'source0'}], 'required_headings': ['Results', 'Limits'],
                'writing_method': 'sections', 'target_words': 3000}
            legacy = {key: value for key, value in request.items() if key != 'writing_method'}
            self.assertNotIn('writing_method', ComposeRequest.model_validate(legacy).model_dump())
            with patch('simple_ar.report.capability.run_report_capability', side_effect=RuntimeError('assembly interrupted')):
                result = gateway.call('compose_document', request, identity='section-native')
            self.assertEqual(result.status, 'error')
            self.assertIn('assembly interrupted', result.error)
            receipt = ArtifactStore(root / 'task/tools/writing/section-native')
            self.assertEqual(receipt.read_json('request.json')['writing_owner'], 'section_article.v1')
            self.assertTrue(receipt.exists('writer/sections.json'))
            with patch.object(gateway, 'client_factory', side_effect=AssertionError('No model replay')):
                restored = gateway.call('compose_document', request, identity='section-native')
            self.assertEqual(restored.status, 'ok', restored.error)
            body = workspace.resolve(restored.data['editable_markdown']).read_text()
            self.assertIn('## Results', body)
            self.assertIn('## Limits', body)
            self.assertEqual(calls, ['report-writer-section_1', 'report-writer-section_2'])
            self.assertEqual(gateway.call('compose_document', {**request, 'writing_method': 'article'},
                identity='section-native').status, 'error')
            # Complete producers of all supported granularities share one
            # read-only evidence-reuse lifecycle, including publication replay.
            from simple_ar.tools.writing import freeze_report_requirement, material_report_binding
            from tests.report_review_fixtures import changed_record
            for producer in ('section-native', 'professional-article', 'professional-sections'):
                with self.subTest(producer=producer):
                    if producer != 'section-native':
                        method = 'sections' if producer.endswith('sections') else 'article'
                        requirement = freeze_report_requirement({'outline_strategy': 'template',
                            'reviewer': 'disabled', 'document_review': False, 'max_review_iterations': 0,
                            'allow_source_backtracking': False, 'allow_llm_fallback': False,
                            'figures': {'enabled': False}}, method=method)
                        report = material_report_binding(workspace, requirement, mounts=['source0'])
                        gateway.report_inputs = lambda: report
                        def professional(system, prompt, *, label='', **options):
                            calls.append(label)
                            view = prompt_payload(prompt)
                            self.assertIn('0.75', json.dumps(view))
                            if 'section' in view:
                                row = view['section']
                                return {'section_id': row['section_id'], 'heading': row['heading'],
                                    'draft_markdown': 'Recorded 0.75; no independent measurement.'}
                            return {'draft_markdown': '\n\n'.join('## ' + row['heading']
                                + '\n\nRecorded 0.75; no independent measurement.' for row in requirement['outline'])}
                        provider = Mock()
                        provider.ask_json.side_effect = professional
                        gateway.client_factory = lambda _: provider
                        produced = gateway.call('compose_document', {**legacy, 'required_headings': []}, identity=producer)
                        self.assertEqual(produced.status, 'ok', produced.error)
                    else:
                        produced = restored
                    parent = root / 'task/tools/writing' / producer
                    original = {path: path.read_bytes() for path in parent.rglob('*') if path.is_file()}
                    published = workspace.resolve(produced.data['editable_markdown'])
                    published_bytes = published.read_bytes()
                    gateway.report_inputs = None
                    feedback = {'goal': 'Clarify the observation limits.', 'title': 'Revised evidence note',
                        'prior_draft': produced.data['editable_markdown'], 'reuse_prior_inputs': True}
                    new_client = Mock()
                    new_client.ask_json.return_value = {'draft_markdown': 'Recorded 0.75; uncertainty was not measured.'}
                    gateway.client_factory = lambda _: new_client
                    with patch.object(gateway, '_select_materials', side_effect=AssertionError('No reselection')):
                        revised = gateway.call('compose_document', feedback, identity=producer + '-feedback')
                    self.assertEqual(revised.status, 'ok', revised.error)
                    self.assertEqual(new_client.ask_json.call_count, 1)
                    child = ArtifactStore(root / 'task/tools/writing' / (producer + '-feedback'))
                    linkage = child.read_json('request.json')['prior_input_reuse']
                    self.assertEqual(set(linkage['receipt_sha256']),
                        {path.relative_to(parent).as_posix() for path in original})
                    with patch.object(gateway, 'client_factory', side_effect=AssertionError('No model for validation or replay')) as factory:
                        replay = gateway.call('compose_document', feedback, identity=producer + '-feedback')
                        self.assertEqual(replay.artifacts, revised.artifacts, replay.error)
                        response = next(path for path in original if path.name == 'response.json')
                        for path in (parent / 'writer/writer.json', response):
                            with changed_record(path, None):
                                missing_bytes = {p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}
                                rejected = gateway.call('compose_document', feedback, identity=producer + '-reject')
                                self.assertEqual(rejected.status, 'error')
                                self.assertEqual({p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}, missing_bytes)
                        with changed_record(response, response.read_bytes() + b'\n'):
                            rejected = gateway.call('compose_document', feedback, identity=producer + '-feedback')
                            self.assertEqual(rejected.status, 'error')
                        with changed_record(source, b'run,score\nfirst,9.0\n'):
                            self.assertEqual(gateway.call('compose_document', feedback,
                                identity=producer + '-reject').status, 'error')
                        if producer == 'professional-sections':
                            unmarked = json.loads((parent / 'request.json').read_text())
                            self.assertEqual(unmarked.pop('section_response_contract'), 'atomic_complete.v1')
                            with changed_record(parent / 'request.json', unmarked):
                                rejected = gateway.call('compose_document', feedback, identity=producer + '-reject')
                                self.assertIn('no verifiable complete response receipts', rejected.error)
                        factory.assert_not_called()
                    self.assertEqual({p: p.read_bytes() for p in parent.rglob('*') if p.is_file()}, original)
                    self.assertEqual(published.read_bytes(), published_bytes)
            gateway.report_inputs = lambda: {'requirement': {'method': 'article'}}
            with patch.object(gateway, 'client_factory', side_effect=AssertionError('No unauthorized method')):
                blocked = gateway.call('compose_document', request, identity='frozen-method')
            self.assertIn('frozen report writing method', blocked.error)
            inspected = gateway.call('review_document', {**request, 'prior_draft': 'missing.md'}, identity='inspect-method')
            self.assertIn('Independent inspection', inspected.error)

    def test_article_uses_one_shared_request_and_structured_projection_without_section_runtime(self):
        request = self.request()
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            with patch('simple_ar.report.writing.run_report_agent', side_effect=AssertionError('legacy runtime')):
                result = run_report_writing_capability(
                    context=CapabilityContext(store, AttemptManifest('article-1')), request=request)
            self.assertEqual(result.status, 'completed')
            self.assertEqual(len(request.llm_client.calls), 1)
            _, article, options = request.llm_client.calls[0]
            self.assertEqual(options['max_output_tokens'], 900)
            self.assertEqual(options['budget_call_id'], 'article-1:article')
            self.assertEqual(article['template_guidance'], request.template.template_markdown)
            self.assertEqual(article['metric_sources'][0]['value'], .75)
            self.assertEqual(article['results']['status'], 'partial')
            self.assertTrue(article['source_handles'][0]['metadata']['evidence_passages_truncated'])
            # The existing structured snapshot is still exact input data, not
            # the sparse composition contract or a new projection version.
            binding = store.read_json('report_inputs.json')['inputs']['binding']
            self.assertEqual(article['source_handles'], binding['context']['source_handles'])
            self.assertEqual(set(article), {'goal', 'title', 'target_words', 'template_guidance', 'style',
                'sections', 'document_plan', 'source_handles', 'front_matter', 'bibliography',
                'citation_key_map', 'problem', 'synthesis', 'hypothesis', 'evidence_summary',
                'source_comparisons', 'metric_sources', 'execution_context', 'experiment_plan',
                'results', 'limitations', 'open_questions', 'revision_findings', 'citation_contract'})
            self.assertEqual(article['citation_contract']['version'], 'registered_body.v1')
            self.assertIn('[@P1]', article['citation_contract']['body'])
            writer_ref = next(ref for ref in result.artifacts if ref.kind == 'report_writer_result')
            self.assertEqual(writer_ref.schema, ARTICLE_OWNER)
            writer = store.read_json(writer_ref)
            projection = store.read_json(writer['input_snapshot']['path'])
            self.assertEqual(projection['context'], request.report_context.model_dump(mode='json'))
            self.assertEqual(projection['config'], request.config.model_dump(mode='json'))
            self.assertEqual(projection['template'], request.template.model_dump(mode='json'))
            self.assertEqual(projection['memory'], request.memory.model_dump(mode='json'))
            self.assertEqual(writer['snapshot_id'], store.read_json('report_inputs.json')['snapshot_id'])
            self.assertNotIn('report_body', writer)
            self.assertFalse(store.exists('sections.json'))
        # Article validation shares transport, but owns no format-correction call.
        for response in ({'draft_markdown': ''}, {'sections': []}, {'raw_response': 'not JSON'},
                {'draft_markdown': 'Article.', 'source_notes': {}},
                {'draft_markdown': 'Article.', 'extra': 'unrecognized'}):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as tmp:
                store = ArtifactStore(Path(tmp))
                rejected = self.request(ArticleClient(response))
                with self.assertRaisesRegex(ValueError, 'no automatic correction'):
                    run_report_writing_capability(
                        context=CapabilityContext(store, AttemptManifest('invalid')), request=rejected)
                self.assertEqual(len(rejected.llm_client.calls), 1)
                target = ArtifactStore(Path(tmp) / 'rejected-resume')
                ref = store.ref('report_inputs.json', kind='article_snapshot', schema=ARTICLE_OWNER)
                with self.assertRaisesRegex(ValueError, 'no automatic correction'):
                    run_report_writing_capability(context=CapabilityContext(target, AttemptManifest('resume'),
                        inputs=(ref,), input_store=store), request=replace(rejected, resume_ref=ref))
                self.assertFalse(target.root.exists())
                self.assertEqual(store.read_json('response.json'), response)
                self.assertFalse(store.exists('writer.json'))
                with self.assertRaisesRegex(ValueError, 'no automatic correction'):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('invalid')),
                        request=replace(rejected, llm_client=None))
                self.assertEqual(len(rejected.llm_client.calls), 1)

    def test_article_recovery_preserves_original_identity_and_never_calls_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = ArtifactStore(Path(tmp) / 'first')
            request = self.request()
            result = run_report_writing_capability(context=CapabilityContext(first, AttemptManifest('original')),
                request=request)
            original = {name: first.resolve(name).read_bytes() for name in
                ('report_inputs.json', 'response.json', 'writer.json', 'report_snapshot.json')}
            for resume_ref in result.artifacts:
                with self.subTest(kind=resume_ref.kind):
                    output = ArtifactStore(Path(tmp) / resume_ref.kind)
                    with patch('simple_ar.report.writing.run_report_agent', side_effect=AssertionError('legacy runtime')):
                        recovered = run_report_writing_capability(context=CapabilityContext(output,
                            AttemptManifest('recovery'), inputs=(resume_ref,), input_store=first),
                            request=replace(request, llm_client=None, method='sections', resume_ref=resume_ref))
                    self.assertEqual(recovered.status, 'completed')
                    self.assertEqual(output.read_json('writer.json')['source_attempt'], 'original')
                    self.assertEqual(output.read_json('writer.json'), first.read_json('writer.json'))
            # Rejected source/destination records cannot partly transplant an
            # article, erase its original projection or trigger another call.
            resume_ref = result.artifacts[0]
            for corrupt in ('report_snapshot.json', 'writer.json', 'response.json'):
                with self.subTest(corrupt=corrupt):
                    output = ArtifactStore(Path(tmp) / ('blocked-' + corrupt))
                    output.write_json(corrupt, {'changed': True})
                    with self.assertRaises(ValueError):
                        run_report_writing_capability(context=CapabilityContext(output, AttemptManifest('blocked'),
                            inputs=(resume_ref,), input_store=first),
                            request=replace(request, llm_client=None, resume_ref=resume_ref))
                    self.assertEqual([row.name for row in output.root.iterdir()], [corrupt])
            self.assertEqual(len(request.llm_client.calls), 1)
            for name, raw in original.items():
                self.assertEqual(first.resolve(name).read_bytes(), raw)

        # Frozen pre-contract Writer results keep their old validation. They
        # are recoverable, not retroactively certified as publishable reports.
        from simple_ar.report.writing import article_writing_inputs
        def historical_inputs(**kwargs):
            return article_writing_inputs(**{**kwargs, 'enforce_citation_contract': False})
        with tempfile.TemporaryDirectory() as tmp:
            first = ArtifactStore(Path(tmp) / 'historical')
            request = self.request()
            request = replace(request, config=request.config.model_copy(update={'max_cited_sources': 1}),
                report_context=request.report_context.model_copy(update={
                'papers': [{'id': 'old-source', 'title': 'Original source'}],
                'citation_key_map': {'P1': 'old-source'}}))
            with patch('simple_ar.report.writing.article_writing_inputs', side_effect=historical_inputs):
                result = run_report_writing_capability(context=CapabilityContext(first, AttemptManifest('old')),
                    request=request)
            self.assertNotIn('citation_contract', first.read_json('report_inputs.json')['inputs']['article'])
            self.assertIn('constraints', first.read_json('report_inputs.json')['inputs']['article'])
            original = {path: path.read_bytes() for path in first.root.iterdir() if path.is_file()}
            ref = result.artifacts[0]
            target = ArtifactStore(Path(tmp) / 'recovered')
            run_report_writing_capability(context=CapabilityContext(target, AttemptManifest('resume'),
                inputs=(ref,), input_store=first), request=replace(request, resume_ref=ref, llm_client=None))
            self.assertEqual(target.resolve('writer.json').read_bytes(), first.resolve('writer.json').read_bytes())
            recovered = run_report_writing_capability(context=CapabilityContext(first, AttemptManifest('old')),
                request=replace(request, llm_client=None))
            self.assertEqual(recovered.status, result.status)
            self.assertEqual({path: path.read_bytes() for path in original}, original)
            self.assertEqual(len(request.llm_client.calls), 1)

    def test_article_inspection_preserves_draft_and_recovers_both_responses(self):
        case = self
        class Client(ArticleClient):
            def ask_json(self, system, prompt, **options):
                self.calls.append((system, json.loads(prompt), options))
                if options['label'] == 'article-independent-review':
                    view = json.loads(prompt)
                    case.assertIn('0.75', json.dumps(view['metric_sources']))
                    case.assertEqual(view['verified_execution_results']['status'], 'partial')
                    return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                        'findings': [{'finding_id': 'extent', 'type': 'structure', 'severity': 'major',
                            'required_action': 'revise', 'message': 'Clarify the conclusion.'}]}]}
                return self.response
        request = self.request(Client())
        request = replace(request, config=request.config.model_copy(update={'reviewer': 'llm',
            'document_review': True, 'review_scope': 'document', 'max_review_iterations': 0}))
        with tempfile.TemporaryDirectory() as tmp:
            first = ArtifactStore(Path(tmp) / 'first')
            with patch('simple_ar.report.writing.run_report_agent', side_effect=AssertionError('No old writing runtime')):
                result = run_report_writing_capability(context=CapabilityContext(first, AttemptManifest('reviewed')),
                    request=request)
            self.assertEqual(result.status, 'completed')
            self.assertEqual(len(request.llm_client.calls), 2)

            review = ArtifactStore(first.root / 'review')
            records = {name: review.read_json(name) for name in ('request.json', 'response.json')}
            for name, changed in (('request.json', {**records['request.json'], 'prompt': 'Changed'}),
                    ('response.json', None), ('response.json', {'raw_response': 'Rejected JSON'})):
                with self.subTest(review_record=name), changed_record(review.resolve(name), changed):
                    blocked = ArtifactStore(Path(tmp) / 'blocked-review')
                    with self.assertRaises((ValueError, LLMResponseError)):
                        inspect_article(store=blocked, resume_store=review, identity='reviewed',
                            client=request.llm_client, report_context=request.report_context, config=request.config,
                            template=request.template, markdown=first.read_json('writer.json')['sections'][0]['draft_markdown'],
                            target_words=request.config.longform.target_words)
                    self.assertFalse(blocked.root.exists())
            self.assertEqual(len(request.llm_client.calls), 2)
            self.assertEqual(first.read_json('reviewed_writer.json')['sections'], first.read_json('writer.json')['sections'])
            self.assertEqual(first.read_json('reviewed_writer.json')['independent_review']['review_status'], 'needs_revision')
            self.assertEqual(first.read_json('reviewed_writer.json')['memory']['reviewer_findings'][0]['finding_id'], 'extent')
            ref = result.artifacts[0]
            target = ArtifactStore(Path(tmp) / 'recovered')
            recovered = run_report_writing_capability(context=CapabilityContext(target, AttemptManifest('resume'),
                inputs=(ref,), input_store=first), request=replace(request, llm_client=None, resume_ref=ref))
            self.assertEqual(recovered.status, 'completed')
            for name in ('writer.json', 'reviewed_writer.json', 'review/request.json', 'review/response.json'):
                self.assertEqual(target.read_json(name), first.read_json(name))
            self.assertEqual(len(request.llm_client.calls), 2)

    def test_article_independent_format_correction_retains_failure_and_recovers_without_calls(self):
        from simple_ar.report.editor import DOCUMENT_FORMAT_CORRECTION_CONTRACT
        for second_bad in (False, True):
            with self.subTest(second_bad=second_bad), tempfile.TemporaryDirectory() as tmp:
                request = self.request()
                calls = []
                rejected = {'section_reviews': [{'section_id': 'document', 'verdict': 'warning',
                    'findings': [{'finding_id': 'located', 'type': 'style', 'message': 'Inspect the registered value.',
                        'evidence_quotes': [{'pointer': '/metric_sources'}]}]}]}
                case = self
                class Client:
                    def ask_json(self, system, prompt, **options):
                        view = json.loads(prompt)
                        calls.append((view, options))
                        case.assertEqual(view['sections'][0]['markdown'], 'Current original manuscript.')
                        if len(calls) == 1 or second_bad:
                            return rejected
                        correction = view['response_format_correction']
                        case.assertEqual(correction['rejected_response'], rejected)
                        case.assertIn('scalar field', correction['validation_error'])
                        # The reviewer must select the literal scalar itself.
                        return {'section_reviews': [{'section_id': 'document', 'verdict': 'warning',
                            'findings': [{'finding_id': 'located', 'type': 'style', 'message': 'Recorded value.',
                                'evidence_quotes': [{'pointer': '/metric_sources/rows/0/2'}]}]}]}
                first = ArtifactStore(Path(tmp) / 'first')
                arguments = dict(identity='original', report_context=request.report_context,
                    config=request.config, template=request.template, markdown='Current original manuscript.')
                if second_bad:
                    with self.assertRaisesRegex(LLMResponseError, 'scalar field'):
                        inspect_article(store=first, client=Client(), **arguments)
                else:
                    result = inspect_article(store=first, client=Client(), **arguments)
                    self.assertEqual(result['section_reviews'][0]['findings'][0]['evidence_quotes'][0]['quote'], '0.75')
                self.assertEqual(len(calls), 2)
                self.assertEqual([options['budget_call_id'] for _, options in calls],
                    ['original:review', 'original:review-format-correction'])
                self.assertEqual(calls[0][0]['format_correction_contract'], DOCUMENT_FORMAT_CORRECTION_CONTRACT)
                for key in ('sections', 'source_evidence', 'metric_sources', 'execution_evidence'):
                    self.assertEqual(calls[0][0][key], calls[1][0][key])
                self.assertEqual(first.read_json('response.json')['response'], rejected)
                self.assertIn('scalar field', first.read_json('rejection.json')['validation_error'])
                before = {p.relative_to(first.root).as_posix(): p.read_bytes() for p in first.root.rglob('*.json')}
                recovered = ArtifactStore(Path(tmp) / 'recovered')
                with patch.object(Client, 'ask_json', side_effect=AssertionError('No third request')):
                    if second_bad:
                        with self.assertRaisesRegex(LLMResponseError, 'scalar field'):
                            inspect_article(store=recovered, resume_store=first, client=Client(), **arguments)
                    else:
                        self.assertEqual(inspect_article(store=recovered, resume_store=first, client=Client(),
                            **{**arguments, 'identity': 'different-attempt'}), result)
                        for name in before:
                            self.assertEqual(recovered.resolve(name).read_bytes(), first.resolve(name).read_bytes())
                for name, raw in before.items():
                    self.assertEqual(first.resolve(name).read_bytes(), raw)

    def test_article_review_unknown_and_legacy_rejection_never_authorize_new_requests(self):
        for phase in ('initial', 'correction', 'legacy', 'dual'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                request = self.request()
                first = ArtifactStore(Path(tmp) / 'review')
                calls = []
                class Client:
                    def ask_json(self, system, prompt, **options):
                        calls.append(options['label'])
                        if phase in {'legacy', 'dual'}:
                            return {'section_reviews': [{'section_id': 'document', 'verdict': 'pass'}]}
                        if phase == 'correction' and len(calls) == 1:
                            return {'section_reviews': 'bad format'}
                        error = LLMResponseError('Unknown/undecodable provider response')
                        error.raw_response = 'incomplete raw JSON'
                        raise error
                arguments = dict(identity='original', report_context=request.report_context,
                    config=request.config, template=request.template, markdown='Current original manuscript.')
                if phase in {'legacy', 'dual'}:
                    import hashlib
                    inspect_article(store=first, client=Client(), **arguments)
                    saved = first.read_json('request.json')
                    saved.pop('response_record_contract')
                    view = json.loads(saved['prompt'])
                    from simple_ar.report.review_evidence import EVIDENCE_QUOTE_RULES, EVIDENCE_QUOTE_SCHEMA
                    saved.pop('evidence_reference_contract')
                    view.pop('evidence_reference_contract')
                    view['focus'] = [EVIDENCE_QUOTE_RULES[0] if row.startswith('Locate an alleged factual error')
                                     else row for row in view['focus']]
                    view['output_schema']['section_reviews'][0]['findings'][0]['evidence_quotes'] = [EVIDENCE_QUOTE_SCHEMA]
                    if phase == 'legacy':
                        saved.pop('format_correction_contract')
                        saved.pop('budget_identity')
                        view.pop('format_correction_contract')
                    saved['prompt'] = json.dumps(view, ensure_ascii=False, separators=(',', ':'))
                    first.write_json('request.json', saved)
                    response = first.read_json('response.json')['response']
                    first.write_json('response.json', response)
                    if phase == 'dual':
                        first.write_json('response_complete.json', {key: hashlib.sha256(json.dumps(value,
                            sort_keys=True, ensure_ascii=False).encode()).hexdigest() for key, value in
                            (('request_sha256', saved), ('response_sha256', response))})
                    # A valid historical response must remain readable with
                    # exactly its old prompt, without today's contract marker.
                    self.assertEqual(inspect_article(store=first, client=None, **arguments)['review_status'], 'reviewed')
                    first.write_json('response.json', {'section_reviews': 'historical invalid format'})
                else:
                    with self.assertRaisesRegex(LLMResponseError, 'Unknown/undecodable'):
                        inspect_article(store=first, client=Client(), **arguments)
                count = len(calls)
                with patch.object(Client, 'ask_json', side_effect=AssertionError('No replay or legacy migration')):
                    with self.assertRaises((ValueError, LLMResponseError)):
                        inspect_article(store=first, client=Client(), **arguments)
                self.assertEqual(len(calls), count)
                if phase not in {'legacy', 'dual'}:
                    prefix = 'format_correction/' if phase == 'correction' else ''
                    self.assertIn('Unknown/undecodable', first.read_json(prefix + 'response_error.json')['error'])
                    self.assertEqual(first.read_json(prefix + 'response.json'), {'raw_response': 'incomplete raw JSON'})

    def test_article_gap_observation_is_frozen_and_cannot_become_verification(self):
        from simple_ar.report.review_evidence import GAP_EVIDENCE_CONTRACT
        request = self.request()
        context = request.report_context.model_copy(update={'metric_sources': [], 'results': {}})
        calls = []
        class Client:
            def ask_json(self, system, prompt, **options):
                view = json.loads(prompt)
                calls.append(view)
                return {'section_reviews': [{'section_id': 'document', 'verdict': 'warning',
                    'findings': [{'finding_id': 'dispersion', 'type': 'evidence_gap',
                        'required_action': 'verify', 'message': 'No measurement supplied for this inspection.',
                        'draft_quotes': [{'section_id': 'document', 'quote': 'The dispersion was 0.3.'}],
                        'evidence_quotes': [{'pointer': '/metric_sources/rows'}]}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            first = ArtifactStore(Path(tmp) / 'review')
            arguments = dict(identity='original', report_context=context, config=request.config,
                template=request.template, markdown='The dispersion was 0.3.')
            result = inspect_article(store=first, client=Client(), **arguments)
            self.assertEqual(result['review_status'], 'needs_revision')
            self.assertEqual(first.read_json('request.json')['evidence_reference_contract'], GAP_EVIDENCE_CONTRACT)
            self.assertEqual(calls[0]['evidence_reference_contract'], GAP_EVIDENCE_CONTRACT)
            self.assertEqual(len(calls), 1)
            raw = first.read_json('response.json')['response']
            self.assertEqual(raw['section_reviews'][0]['findings'][0]['evidence_quotes'],
                [{'pointer': '/metric_sources/rows'}])
            snapshot = {p.relative_to(first.root).as_posix(): p.read_bytes() for p in first.root.rglob('*.json')}
            resumed = ArtifactStore(Path(tmp) / 'resumed')
            self.assertEqual(inspect_article(store=resumed, resume_store=first, client=None, **arguments), result)
            for name, content in snapshot.items():
                self.assertEqual(first.resolve(name).read_bytes(), content)
                self.assertEqual(resumed.resolve(name).read_bytes(), content)
            self.assertFalse(first.exists('format_correction/request.json'))

    def test_review_recovers_saved_answers_but_never_replays_started_correction(self):
        from tests.report_review_fixtures import CheckpointStop
        for boundary in ('original_complete', 'correction_started', 'correction_complete'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                request = self.request()
                store = ArtifactStore(Path(tmp) / 'review')
                calls = []
                class Client:
                    def ask_json(self, system, prompt, **options):
                        calls.append(options['budget_call_id'])
                        return ({'section_reviews': 'bad format'} if len(calls) == 1 else
                            {'section_reviews': [{'section_id': 'document', 'verdict': 'pass'}]})
                original_write = ArtifactStore.write_json
                def stop(retained, path, *args, **kwargs):
                    result = original_write(retained, path, *args, **kwargs)
                    correcting = retained.root.name == 'format_correction'
                    if ((boundary == 'original_complete' and not correcting and path == 'response.json')
                            or (boundary == 'correction_started' and correcting and path == 'request.json')
                            or (boundary == 'correction_complete' and correcting and path == 'response.json')):
                        raise CheckpointStop()
                    return result
                arguments = dict(identity='original', report_context=request.report_context,
                    config=request.config, template=request.template, markdown='Current manuscript.')
                with patch.object(ArtifactStore, 'write_json', stop), self.assertRaises(CheckpointStop):
                    inspect_article(store=store, client=Client(), **arguments)
                if boundary == 'correction_started':
                    with self.assertRaisesRegex(ValueError, 'no blind replay'):
                        inspect_article(store=store, client=Client(), **arguments)
                    self.assertEqual(calls, ['original:review'])
                else:
                    result = inspect_article(store=store, client=Client(), **arguments)
                    self.assertEqual(result['review_status'], 'reviewed')
                    self.assertEqual(calls, ['original:review', 'original:review-format-correction'])

    def test_article_corrections_use_complete_candidates_and_retained_producers(self):
        for constrained, reject, rounds in ((False, False, 1), (True, False, 1), (False, True, 1), (False, True, 2)):
            with self.subTest(constrained=constrained, reject=reject, rounds=rounds), tempfile.TemporaryDirectory() as tmp:
                calls = []
                initial = 'Initial explanation.' if not constrained else '## Wrong\n\nInitial explanation.'
                revised = 'Corrected explanation.' if not constrained else '## Observations\n\nCorrected explanation.\n\n## Limits\n\nSupplied conditions.'
                class Client:
                    def ask_json(self, system, prompt, *, label='', **options):
                        calls.append(label)
                        view = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                        if label == 'composition-article':
                            if len(calls) > 1:
                                self_outer.assertEqual(view['prior_draft'], initial if len(calls) == 3 else revised)
                                self_outer.assertIn('not source facts', view['revision_findings']['status'])
                            return {'draft_markdown': initial if len(calls) == 1 else revised}
                        body = view['sections'][0]['markdown']
                        self_outer.assertFalse(view['historical_findings_to_check'])
                        needs_edit = body == initial and not constrained or body == revised and reject
                        return {'section_reviews': [{'section_id': 'document',
                            'verdict': 'revise_required' if needs_edit else 'pass',
                            'findings': [{'finding_id': 'prose', 'type': 'style', 'severity': 'major',
                                'required_action': 'revise', 'message': 'Clarify the interpretation.',
                                'draft_quotes': [{'section_id': 'document', 'quote': body}]}] if needs_edit else []}]}
                self_outer = self
                request = self.request(Client())
                request = replace(request, config=request.config.model_copy(update={'reviewer': 'llm',
                    'document_review': True, 'review_scope': 'document', 'draft_scope': 'document',
                    'max_review_iterations': rounds, 'outline_strategy': 'template' if constrained else 'auto'}),
                    memory=request.memory.model_copy(update={'section_plan': [
                        ReportSectionPlan(section_id='observations', heading='Observations', goal='Explain the observations', final_order=1),
                        ReportSectionPlan(section_id='limits', heading='Limits', goal='Explain their limits', final_order=2)]}))
                store = ArtifactStore(Path(tmp) / 'first')
                with patch('simple_ar.report.writing.run_report_agent', side_effect=AssertionError('No old writing runtime')):
                    result = run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('article-edit')), request=request)
                self.assertEqual(result.status, 'failed' if reject else 'completed')
                expected_calls = ['composition-article', 'article-independent-review'] * (rounds + 1)
                self.assertEqual(calls, expected_calls)
                writer = store.read_json(next(ref.path for ref in result.artifacts if ref.kind == 'report_writer_result'))
                self.assertEqual(store.read_json('writer.json')['sections'][0]['draft_markdown'], initial)
                self.assertEqual(writer['sections'][0]['draft_markdown'], initial if reject else revised)
                self.assertEqual(writer['adopted_revision'] is None, reject)
                ref = result.artifacts[0]
                target = ArtifactStore(Path(tmp) / 'resumed')
                recovered = run_report_writing_capability(context=CapabilityContext(target, AttemptManifest('resumed'),
                    inputs=(ref,), input_store=store), request=replace(request, llm_client=None, resume_ref=ref))
                self.assertEqual(recovered.status, result.status)
                self.assertEqual(calls, expected_calls)
                self.assertEqual(target.read_json('writer.json'), store.read_json('writer.json'))
                protected = {path: path.read_bytes() for path in store.root.rglob('*.json')}
                for receipt in ('response.json', 'inspection/response.json'):
                    with self.subTest(receipt=receipt), changed_record(
                            store.resolve('review/revision-1/' + receipt), {'raw_response': 'Changed retained response'}):
                        with self.assertRaises((ValueError, LLMResponseError)):
                            run_report_writing_capability(context=CapabilityContext(ArtifactStore(Path(tmp) / 'corrupt-' / receipt),
                                AttemptManifest('changed'), inputs=(ref,), input_store=store),
                                request=replace(request, llm_client=None, resume_ref=ref))
                self.assertEqual(calls, expected_calls)
                for path, raw in protected.items():
                    self.assertEqual(path.read_bytes(), raw)
                if not reject:
                    current = target.read_json(next(r.path for r in recovered.artifacts if r.kind == 'report_writer_result'))
                    self.assertEqual(current, writer)

    def test_article_revision_rejects_lost_citations_without_review_or_replay(self):
        from simple_ar.literature.verify import CitationError

        client = ArticleClient()
        request = self.request(client)
        request = replace(request, config=request.config.model_copy(update={
            'reviewer': 'llm', 'document_review': True, 'review_scope': 'document',
            'draft_scope': 'document', 'max_review_iterations': 1}),
            report_context=request.report_context.model_copy(update={
                'papers': [{'id': 'source-a', 'title': 'Original source'}],
                'citation_key_map': {'P1': 'source-a'}}))
        responses = [{'draft_markdown': 'Supported prose [@P1].'},
            {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required',
                'findings': [{'finding_id': 'style', 'type': 'style', 'severity': 'major',
                    'required_action': 'revise', 'message': 'Clarify the prose.',
                    'draft_quotes': [{'section_id': 'document', 'quote': 'Supported prose [@P1].'}]}]}]},
            {'draft_markdown': 'Revised prose lost its attribution.'}]
        with tempfile.TemporaryDirectory() as tmp, patch.object(client, 'ask_json', side_effect=responses) as model:
            store = ArtifactStore(Path(tmp))
            context = CapabilityContext(store, AttemptManifest('revision-citations'))
            with self.assertRaisesRegex(CitationError, 'registered body citations'):
                run_report_writing_capability(context=context, request=request)
            recovered = run_report_writing_capability(context=context, request=replace(request, llm_client=None))
            self.assertEqual(recovered.status, 'failed')
            self.assertEqual(model.call_count, 3)
            self.assertEqual(store.read_json('writer.json')['sections'][0]['draft_markdown'], responses[0]['draft_markdown'])
            self.assertEqual(store.read_json('review/revision-1/response.json'), responses[2])
            self.assertFalse(store.exists('review/revision-1/writer.json'))
            self.assertFalse(store.exists('review/revision-1/inspection/request.json'))

    def test_article_candidate_and_verification_checkpoints_resume_without_repeating_calls(self):
        for stage in ('drafted', 'completed'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                calls = []
                class Client:
                    def ask_json(self, system, prompt, *, label='', **options):
                        calls.append(label)
                        if label == 'composition-article':
                            return {'draft_markdown': 'Initial.' if len(calls) == 1 else 'Revised.'}
                        view = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                        pending = view['sections'][0]['markdown'] == 'Initial.'
                        return {'section_reviews': [{'section_id': 'document', 'verdict': 'revise_required' if pending else 'pass',
                            'findings': [{'finding_id': 'style', 'type': 'style', 'severity': 'major', 'required_action': 'revise',
                                'message': 'Improve the prose.', 'draft_quotes': [{'section_id': 'document', 'quote': 'Initial.'}]}] if pending else []}]}
                request = self.request(Client())
                request = replace(request, config=request.config.model_copy(update={'reviewer': 'llm',
                    'document_review': True, 'review_scope': 'document', 'draft_scope': 'document', 'max_review_iterations': 1}))
                store = ArtifactStore(Path(tmp) / 'first')
                original_write = ArtifactStore.write_json
                def interrupt(owner, path, value, **options):
                    result = original_write(owner, path, value, **options)
                    if str(path) == 'review/checkpoint.json' and any(row['status'] == stage and row['action'] ==
                            ('document_joint_revise' if stage == 'drafted' else 'document_joint_verify') for row in value['iterations']):
                        raise RuntimeError('Saved editing stage')
                    return result
                with patch.object(ArtifactStore, 'write_json', interrupt), self.assertRaisesRegex(RuntimeError, 'Saved editing stage'):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('interrupted')), request=request)
                count = len(calls)
                ref = store.ref('report_inputs.json', kind='article_snapshot', schema='direct_article.v1')
                recovered = run_report_writing_capability(context=CapabilityContext(ArtifactStore(Path(tmp) / 'resumed'),
                    AttemptManifest('resume'), inputs=(ref,), input_store=store), request=replace(request, resume_ref=ref))
                self.assertEqual(recovered.status, 'completed')
                self.assertEqual(calls[count:], ['article-independent-review'] if stage == 'drafted' else [])

    def test_interrupted_article_uses_retained_response_but_missing_response_never_replays(self):
        for response_state in ('missing', 'saved', 'raw'):
            with self.subTest(response_state=response_state), tempfile.TemporaryDirectory() as tmp:
                session = ArtifactStore(Path(tmp))
                store = ArtifactStore(session.root / 'attempts' / 'original')
                request = self.request()
                def interrupted(system, prompt, **kwargs):
                    if response_state == 'saved':
                        store.write_json('response.json', request.llm_client.response)
                    error = RuntimeError('interrupted after request')
                    if response_state == 'raw':
                        error.raw_response = 'Original non-JSON bytes'
                    raise error
                with patch.object(request.llm_client, 'ask_json', side_effect=interrupted):
                    with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                        run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')),
                            request=request)
                offline = replace(request, llm_client=None)
                selected, prior = resolve_report_writing_resume(report_context=request.report_context,
                    memory=request.memory, config=request.config, template=request.template,
                    sources=(), store=session, attempts=[AttemptManifest('original', capability='report_write')],
                    method='sections')
                self.assertEqual(selected.path, 'attempts/original/report_inputs.json')
                self.assertEqual(prior, '')
                if response_state == 'saved':
                    recovered = run_report_writing_capability(
                        context=CapabilityContext(store, AttemptManifest('original')), request=offline)
                    self.assertEqual(recovered.status, 'completed')
                elif response_state == 'raw':
                    self.assertEqual(store.read_json('response.json'), {'raw_response': 'Original non-JSON bytes'})
                    with self.assertRaisesRegex(ValueError, 'no automatic correction'):
                        run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')), request=offline)
                else:
                    with self.assertRaisesRegex(ValueError, 'no blind replay'):
                        run_report_writing_capability(
                            context=CapabilityContext(store, AttemptManifest('original')), request=offline)

    def test_resume_selection_shares_binding_and_uses_only_completed_article_body_for_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ArtifactStore(Path(tmp))
            store = ArtifactStore(session.root / 'attempts' / 'original')
            source = session.write_json('material.json', {'value': .75}, kind='material')
            request = replace(self.request(), prior_draft='Previously accepted prose.')
            result = run_report_writing_capability(
                context=CapabilityContext(store, AttemptManifest('original'), inputs=(source,), input_store=session),
                request=request)
            attempt = AttemptManifest('original', capability='report_write', status='completed', outputs=result.artifacts)
            common = dict(report_context=request.report_context, memory=request.memory, config=request.config,
                template=request.template, sources=(source,), store=session, attempts=[attempt], method='article')
            original = {path: path.read_bytes() for path in store.root.iterdir() if path.is_file()}
            selected, prior = resolve_report_writing_resume(**common)
            self.assertEqual(selected.path, 'attempts/original/report_inputs.json')
            self.assertEqual(prior, request.prior_draft)
            changes = {
                'report_context': request.report_context.model_copy(update={'goal_markdown': 'Changed goal'}),
                'memory': request.memory.model_copy(update={'limitations': ['Changed limit']}),
                'config': request.config.model_copy(update={'max_section_tokens': 800}),
                'template': request.template.model_copy(update={'template_markdown': 'Changed template'}),
                'sources': (),
            }
            body = store.read_json('writer.json')['sections'][0]['draft_markdown']
            for key, value in changes.items():
                with self.subTest(binding=key):
                    changed = {**common, key: value}
                    self.assertEqual(resolve_report_writing_resume(**changed), (None, body))
                    self.assertEqual(resolve_report_writing_resume(**{**changed, 'method': 'sections'}), (None, ''))
                    self.assertEqual(resolve_report_writing_resume(**{**changed,
                        'attempts': [replace(attempt, status='failed')]}), (None, ''))
            newer = AttemptManifest('newer', capability='report_write')
            checkpoint = session.write_json('attempts/newer/sections.json', {},
                kind='report_checkpoint', schema='report_checkpoint.v1')
            self.assertEqual(resolve_report_writing_resume(**{**common, 'attempts': [attempt, newer]}),
                (checkpoint, ''))
            self.assertEqual(len(request.llm_client.calls), 1)
            for path, raw in original.items():
                self.assertEqual(path.read_bytes(), raw)

    def test_article_rejects_changed_request_binding(self):
        # Retained-record corruption is owned by the recovery matrix below.
        for field, changes in (('report_context', {'goal_markdown': 'Changed'}),
                               ('config', {'max_section_tokens': 800}),
                               ('template', {'template_markdown': 'Changed'})):
            with self.subTest(binding=field), tempfile.TemporaryDirectory() as tmp:
                store = ArtifactStore(Path(tmp))
                request = self.request()
                run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')), request=request)
                request = replace(request, **{field: getattr(request, field).model_copy(update=changes)})
                with self.assertRaises(ValueError):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')), request=request)
                self.assertEqual(len(request.llm_client.calls), 1)

    def test_article_recovery_validates_records_before_reusing_or_rebuilding_writer(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            request = self.request()
            context = CapabilityContext(store, AttemptManifest('original'))
            run_report_writing_capability(context=context, request=request)
            records = {name: store.read_json(name) for name in
                ('writer.json', 'response.json', 'report_inputs.json')}
            snapshot = records['report_inputs.json']
            cases = [('writer.json', {**records['writer.json'], 'sections': []}),
                     ('writer.json', {**records['writer.json'], 'sections': [{
                         **records['writer.json']['sections'][0], 'draft_markdown': 'Changed'}]}),
                     ('response.json', {'draft_markdown': 'Changed raw response'}),
                     ('report_inputs.json', {**snapshot, 'snapshot_id': '0' * 64}),
                     ('report_inputs.json', None), ('response.json', None)]
            for field, value in (('owner', 'other_article.v1'), ('source_attempt', 'other-attempt'),
                    ('inputs', {**snapshot['inputs'], 'article': {**snapshot['inputs']['article'], 'goal': 'Changed'}}),
                    ('system', ''), ('system', None), ('system', 'Unrecognized contract prompt')):
                changed = {**snapshot, field: value}
                changed['snapshot_id'] = hashlib.sha256(json.dumps(
                    {key: value for key, value in changed.items() if key != 'snapshot_id'},
                    sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                cases.append(('report_inputs.json', changed))
            for name, changed in cases:
                with self.subTest(record=name, changed=changed), changed_record(store.resolve(name), changed):
                    with self.assertRaises(ValueError):
                        run_report_writing_capability(context=context, request=replace(request, llm_client=None))
            # A self-consistent but unknown prompt cannot rebuild a missing Writer.
            with changed_record(store.resolve('report_inputs.json'), cases[-1][1]), \
                    changed_record(store.resolve('writer.json'), None), self.assertRaises(ValueError):
                run_report_writing_capability(context=context, request=replace(request, llm_client=None))
            self.assertEqual(len(request.llm_client.calls), 1)

    def test_new_task_method_admission_preserves_control_intent(self):
        joint = {'document_review': True, 'review_scope': 'document'}
        for controls, expected, reviewed, rounds, draft in (
                ({}, 'article', False, 0, 'section'),
                ({'outline_strategy': 'template'}, 'article', False, 0, 'section'),
                ({'template': 'custom.md', 'outline_strategy': 'auto'}, 'article', False, 0, 'section'),
                ({'outline_strategy': 'adaptive'}, 'sections', False, None, None),
                ({'reviewer': 'llm'}, 'sections', False, None, None),
                ({'document_review': True}, 'article', True, 2, 'document'),
                ({'reviewer': 'disabled', 'max_review_iterations': 0}, 'article', False, 0, 'section'),
                ({'max_review_iterations': 0}, 'article', False, 0, 'section'),
                ({**joint, 'max_review_iterations': 0}, 'article', True, 0, 'section'),
                ({**joint, 'max_review_iterations': 0, 'draft_scope': 'document'}, 'article', True, 0, 'document'),
                ({**joint, 'max_review_iterations': 3}, 'article', True, 3, 'document'),
                ({**joint, 'max_review_iterations': 3, 'max_document_review_prompt_chars': 12000}, 'article', True, 3, 'document'),
                ({**joint, 'max_review_iterations': 0, 'criteria': 'custom.md'}, 'article', True, 0, 'section'),
                ({**joint, 'draft_scope': 'document'}, 'article', True, 2, 'document'),
                ({**joint, 'draft_scope': 'auto'}, 'article', True, 2, 'auto'),
                ({'document_review': True, 'draft_scope': 'auto'}, 'article', True, 2, 'auto'),
                ({**joint, 'draft_scope': 'section'}, 'sections', True, None, None),
                ({**joint, 'outline_strategy': 'adaptive'}, 'sections', True, None, None),
                ({'document_review': True, 'outline_strategy': 'adaptive'}, 'sections', True, None, None),
                ({'document_review': True, 'review_scope': 'section'}, 'sections', True, None, None),
                ({'max_review_iterations': 3}, 'sections', False, None, None)):
            with self.subTest(controls=controls):
                original = controls.copy()
                method, selected = resolve_report_writing_config(controls)
                self.assertEqual(method, expected)
                self.assertEqual(controls, original)
                self.assertIsNot(selected, controls)
                for key, value in controls.items():
                    self.assertEqual(selected[key], value)
                ReportRuntimeConfig.model_validate(selected)
                if expected == 'article':
                    self.assertEqual(selected['reviewer'], 'llm' if reviewed else 'disabled')
                    self.assertIs(selected['document_review'], reviewed)
                    self.assertEqual(selected['max_review_iterations'], rounds)
                    self.assertEqual(selected['draft_scope'], draft)
                    self.assertEqual(selected['review_scope'], 'document' if reviewed else 'section')
                    validate_report_writing_config(ReportRuntimeConfig.model_validate(selected), method=method)
                    self.assertEqual(resolve_report_writing_config(controls, method='article'), (method, selected))
                else:
                    self.assertEqual(selected, controls)
                    with self.assertRaisesRegex(ValueError, 'cannot execute'):
                        resolve_report_writing_config(controls, method='article')
        # Cross-field rules belong to the existing runtime model for both methods.
        for controls in ({'review_scope': 'document'}, {'draft_scope': 'auto'},
                         {**joint, 'draft_scope': 'document', 'source_strategy': 'batch_refine'}):
            for method in (None, 'article', 'sections'):
                with self.subTest(controls=controls, method=method), self.assertRaises(ValueError):
                    resolve_report_writing_config(controls, method=method)

    def test_unsupported_article_controls_fail_before_any_request_or_snapshot(self):
        # These are owner settings, not additions to the public CLI/TOML surface.
        for key, value in [('reviewer', 'llm'), ('document_review', True), ('source_strategy', 'batch_refine'),
                ('allow_source_backtracking', True), ('outline_strategy', 'adaptive'),
                ('allow_llm_fallback', True), ('criteria', 'custom.md'), ('cost_profile', 'fast'),
                ('agent', 'disabled'), ('max_document_review_prompt_chars', 1000),
                ('max_section_sources', 2), ('source_batch_size', 2), ('max_source_batches', 2),
                ('draft_sections', True), ('debug_artifacts', True), ('review_trace', 'full'),
                ('longform', {'target_papers': 30})]:
            with self.subTest(control=key), tempfile.TemporaryDirectory() as tmp:
                request = self.request()
                settings = {**request.config.model_dump(), key: value}
                original = json.dumps(settings, sort_keys=True)
                self.assertEqual(resolve_report_writing_config(settings, explicit_fields={key}), ('sections', settings))
                with self.assertRaisesRegex(ValueError, key):
                    resolve_report_writing_config(settings, method='article', explicit_fields={key})
                self.assertEqual(json.dumps(settings, sort_keys=True), original)
                request = replace(request, config=ReportRuntimeConfig.model_validate(settings))
                with self.assertRaisesRegex(ValueError, key):
                    validate_report_writing_config(request.config, method='article')
                store = ArtifactStore(Path(tmp))
                with self.assertRaisesRegex(ValueError, key):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('new')), request=request)
                self.assertFalse(store.exists('report_inputs.json'))
                self.assertEqual(request.llm_client.calls, [])

    def test_template_article_enforces_final_order_and_allows_omitted_optional_section(self):
        plans = [
            ReportSectionPlan(section_id='limits', heading='3. Limits', goal='Bound the claim.', final_order=3),
            ReportSectionPlan(section_id='details', heading='Details', goal='Add detail.', required=False, final_order=2),
            ReportSectionPlan(section_id='observations', heading='1. Observations', goal='State evidence.', final_order=1),
        ]
        cases = [
            ('## Observations\n\nEvidence.\n\n## Limits\n\nBoundary.', True),
            ('## Observations\n\nEvidence.\n\n## Details\n\nDetail.\n\n## Limits\n\nBoundary.', True),
            ('## 1. Observations\n\nEvidence.\n\n## 3. Limits\n\nBoundary.', True),
            ('## Limits\n\nBoundary.\n\n## Observations\n\nEvidence.', False),
            ('## Observations\n\nEvidence.\n\n## Limits\n\nBoundary.\n\n## Details\n\nDetail.', False),
            ('## Observations\n\nEvidence.', False),
            ('## Observations\n\nEvidence.\n\n## Extra\n\nUnplanned.\n\n## Limits\n\nBoundary.', False),
            ('## Observations\n\nEvidence.\n\n## Observations\n\nRepeated.\n\n## Limits\n\nBoundary.', False),
        ]
        # Both controls produce the same fixed-structure constraints. Own the
        # full validation matrix once; custom-template routing needs one pass
        # and one rejection to prove it reaches that contract.
        scenarios = [({'outline_strategy': 'template'}, markdown, accepted) for markdown, accepted in cases]
        scenarios += [({'template': 'custom.md', 'outline_strategy': 'auto'}, markdown, accepted)
                      for markdown, accepted in (cases[0], cases[3])]
        for controls, markdown, accepted in scenarios:
            with self.subTest(controls=controls, markdown=markdown), tempfile.TemporaryDirectory() as tmp:
                request = self.request(ArticleClient({'draft_markdown': markdown}))
                request = replace(request, memory=request.memory.model_copy(update={'section_plan': plans}),
                    config=request.config.model_copy(update=controls))
                store = ArtifactStore(Path(tmp))
                context = CapabilityContext(store, AttemptManifest('fixed-outline'))
                if accepted:
                    self.assertEqual(run_report_writing_capability(context=context, request=request).status, 'completed')
                    self.assertEqual(store.read_json('writer.json')['sections'][0]['draft_markdown'], markdown)
                else:
                    for offline in (False, True):
                        with self.assertRaisesRegex(ValueError, 'fixed section structure'):
                            run_report_writing_capability(context=context,
                                request=replace(request, llm_client=None) if offline else request)
                    self.assertEqual(store.read_json('response.json'), request.llm_client.response)
                    self.assertFalse(store.exists('writer.json'))
                self.assertEqual(len(request.llm_client.calls), 1)
                self.assertEqual(request.llm_client.calls[0][1]['constraints']['outline'], [
                    {'heading': '1. Observations', 'required': True},
                    {'heading': 'Details', 'required': False},
                    {'heading': '3. Limits', 'required': True}])

    def test_article_citation_cap_counts_source_identity_in_body_only(self):
        from simple_ar.literature.verify import CitationError

        cases = [
            ('Repeated [@P1; @p2] and [P1, P2] and [@source-a].', 1, True, None),
            ('Canonical bare identity [source-a] and alias [P1].', 1, True, None),
            ('Two sources [P1; P3].', 1, True, ValueError),
            ('Two bare canonical identities [source-a; source-b].', 1, True, ValueError),
            ('Two sources @source-a and @source-b .', 1, True, ValueError),
            ('Unknown source [@missing].', 1, True, CitationError),
            # Admission is independent of optional structure/cap controls.
            ('Unattributed source claims.', 0, True, CitationError),
            ('Unbound numeric citation [1] or https://example.org/study.', 0, True, CitationError),
            ('Known [@P1] alongside unknown [@missing].', 0, True, CitationError),
            ('Only one relevant source [@P1].', 0, True, None),
            ('| Claim | Source |\n|---|---|\n| Recorded | [@P1] |', 0, True, None),
            ('# Draft title [@P1]\n\n## Evidence\n\nUnattributed prose.', 0, True, CitationError),
            ('# Draft title [@P1]\n\n## References\n\n[@P1]', 0, True, CitationError),
            ('# Draft title [@missing]\n\n## Evidence\n\nSupported prose [@P1].', 0, True, None),
            ('Local result explanation without bibliography.', 0, False, None),
            ('Invented citation [@missing] without bibliography.', 0, False, CitationError),
        ]
        for prose, cap, has_sources, rejection in cases:
            with self.subTest(prose=prose), tempfile.TemporaryDirectory() as tmp:
                accepted = rejection is None
                markdown = ((prose if prose.startswith('# ') else '## Evidence\n\n' + prose)
                    + '\n\n```markdown\n## Unplanned\n[@missing; @source-b]\n```'
                    + '\n\n## References\n\n[@missing; @source-b]')
                request = self.request(ArticleClient({'draft_markdown': markdown}))
                request = replace(request,
                    config=request.config.model_copy(update={'max_cited_sources': cap}),
                    report_context=request.report_context.model_copy(update={
                        'papers': [{'id': 'source-a', 'title': 'A'}, {'id': 'source-b', 'title': 'B'}] if has_sources else [],
                        'citation_key_map': {'P1': 'source-a', 'P2': 'source-a', 'P3': 'source-b'} if has_sources else {}}))
                if not accepted and cap == 0:
                    request = replace(request, config=request.config.model_copy(update={
                        'reviewer': 'llm', 'document_review': True, 'review_scope': 'document',
                        'draft_scope': 'document', 'max_review_iterations': 1}))
                store = ArtifactStore(Path(tmp))
                context = CapabilityContext(store, AttemptManifest('citation-cap'))
                if accepted:
                    self.assertEqual(run_report_writing_capability(context=context, request=request).status, 'completed')
                    if cap:
                        self.assertEqual(store.read_json('writer.json')['constraints_checked'],
                            {'headings': ['Evidence'], 'distinct_cited_sources': 1})
                    else:
                        self.assertNotIn('constraints_checked', store.read_json('writer.json'))
                else:
                    for offline in (False, True):
                        with self.assertRaisesRegex(rejection, 'original response retained'):
                            run_report_writing_capability(context=context,
                                request=replace(request, llm_client=None) if offline else request)
                    self.assertFalse(store.exists('writer.json'))
                    self.assertFalse(store.exists('review/request.json'))
                self.assertEqual(store.read_json('response.json'), {'draft_markdown': markdown})
                self.assertEqual(len(request.llm_client.calls), 1)

    def test_auto_article_keeps_section_plan_as_soft_guidance(self):
        request = self.request(ArticleClient({'draft_markdown': '## Reader question\n\nA different organization.'}))
        request = replace(request, memory=request.memory.model_copy(update={'section_plan': [
            ReportSectionPlan(section_id='expected', heading='Expected', goal='Suggested organization.')]}))
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            result = run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('auto')), request=request)
            self.assertEqual(result.status, 'completed')
            self.assertNotIn('constraints', request.llm_client.calls[0][1])
            self.assertEqual(request.llm_client.calls[0][1]['sections'][0]['heading'], 'Expected')
            self.assertNotIn('constraints_checked', store.read_json('writer.json'))

    def test_fixed_article_without_section_plan_fails_before_request(self):
        request = self.request()
        request = replace(request, config=request.config.model_copy(update={'outline_strategy': 'template'}))
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            with self.assertRaisesRegex(ValueError, 'requires a section plan'):
                run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('no-plan')), request=request)
            self.assertEqual(request.llm_client.calls, [])
            self.assertFalse(store.exists('report_inputs.json'))

    def test_article_supplies_front_matter_and_source_notes_without_review_or_output_cap(self):
        from types import SimpleNamespace
        from simple_ar.research.contracts import DocumentRecord, DocumentSection, TextChunk
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.report.projection import build_material_report_inputs, apply_report_bibliography

        record = DocumentRecord('doc', 'input paper', 'local_files', source_id='/input/paper.pdf',
            extraction_status='parsed')
        front = DocumentSection('front', 'doc', 'front_matter', 'Front matter',
            'Original study\nAda Example\n2025-04-03')
        documents = DocumentBundle([record], {}, {}, [front], [TextChunk('body', 'doc', 'Recorded observations.')])
        notes = [{'paper_id': 'doc', 'bibliographic_fields': [
            {'field': 'authors', 'value': ['Ada Example'], 'quote': 'Ada Example',
             'section_id': 'front', 'complete': True},
            {'field': 'doi', 'value': '10.1234/invented', 'quote': 'Absent original text',
             'section_id': 'front', 'complete': True}]}]
        request = self.request(ArticleClient({'draft_markdown': 'Article [@P1].', 'source_notes': notes}))
        request = replace(request, config=request.config.model_copy(update={'max_section_tokens': 0}))
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            document_ref = store.write_json('documents.json', documents.to_handoff_dict(), schema='document_bundle.v1')
            report_context, memory = build_material_report_inputs(topic='Explain the study', documents=documents,
                documents_ref=document_ref, assets=[SimpleNamespace(locator=record.source_id, role='paper')], selected_evidence=True)
            request = replace(request, report_context=report_context, memory=memory)
            run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('article'),
                inputs=(document_ref,)), request=request)
            view = request.llm_client.calls[0][1]['front_matter'][0]
            self.assertEqual(view['text'], front.text)
            self.assertEqual(view['section_id'], 'front')
            self.assertNotIn(request.template.criteria_markdown, json.dumps(request.llm_client.calls[0][1]))
            self.assertNotIn('max_output_tokens', request.llm_client.calls[0][2])
            writer = store.read_json('writer.json')
            self.assertEqual(writer['source_notes'], notes)
            self.assertEqual(writer['memory']['outline_planning']['source_notes'], notes)
            self.assertEqual(writer['memory']['reviewer_findings'], [])
            adopted, _ = apply_report_bibliography(report_context, documents=documents, notes=writer['source_notes'])
            self.assertEqual(adopted.papers[0]['authors'], ['Ada Example'])
            self.assertNotEqual(adopted.papers[0].get('doi'), '10.1234/invented')

    def test_sections_default_and_historical_checkpoint_override_article_even_for_input_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp) / 'legacy')
            request = replace(self.request(), method='sections', memory=ReportMemory(section_plan=[
                ReportSectionPlan(section_id='scope', heading='Scope', goal='Explain scope')]))
            self.assertEqual(ReportWritingRequest(request.report_context, request.memory, request.config,
                request.template, request.llm_client).method, 'sections')
            saved = {'sections': [{'section_id': 'scope', 'draft_markdown': 'Saved section.'}]}
            def legacy(**kwargs):
                kwargs['checkpoint_sink'](saved)
                return None
            with patch('simple_ar.report.writing.run_report_agent', side_effect=legacy):
                first = run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('legacy')), request=request)
            checkpoint = next(ref for ref in first.artifacts if ref.kind == 'report_checkpoint')
            for changed in (False, True):
                with self.subTest(changed=changed):
                    output = ArtifactStore(Path(tmp) / str(changed))
                    revised = request.report_context.model_copy(update={'topic': 'Revised'}) if changed else request.report_context
                    with patch('simple_ar.report.writing.run_report_agent', side_effect=legacy) as agent, \
                         patch('simple_ar.report.writing.resolve_report_writing_config',
                               side_effect=AssertionError('Saved checkpoints never reselect the method')):
                        run_report_writing_capability(context=CapabilityContext(output, AttemptManifest('next'),
                            inputs=(checkpoint,), input_store=store), request=replace(request, method='article',
                            resume_ref=checkpoint, report_context=revised))
                    self.assertEqual(agent.call_args.kwargs['completed_checkpoint'], None if changed else saved)
                    self.assertEqual(request.llm_client.calls, [])


class _MissingFigureRenderer:
    name = "missing_fixture"

    def __init__(self, caption=""):
        self.caption = caption

    def render(
        self,
        *,
        report_markdown: str,
        report_dir: Path,
        config: ReportFigureConfig,
        template_name: str = "",
        document_plan: ReportDocumentPlan | None = None,
        emit=None,
    ) -> ReportFigureResult:
        return ReportFigureResult(
            report_markdown=(report_markdown.replace("\n\n", f"\n\n{self.caption}\n\n", 1)
                             if self.caption else report_markdown),
            figures=[
                ReportFigureRecord(
                    figure_id="missing",
                    title="Missing figure",
                    path="figures/missing.svg",
                    anchor="Introduction",
                    caption=self.caption,
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
