from __future__ import annotations

import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
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
)


class ReportAuditCapabilityTests(unittest.TestCase):
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
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(
                tmp,
                session_id="report-reference-session",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )

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
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(
                tmp,
                session_id="report-figure-session",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )

            result = controller.execute_attempt(
                "report",
                attempt_id="attempt-001",
                request=ReportAssemblyRequest(
                    title="Offline report",
                    template_name="paper",
                    papers=({"id": "paper-1", "title": "Supplied source"},),
                    config=ReportRuntimeConfig(
                        figures=ReportFigureConfig(enabled=True)
                    ),
                    document_plan=ReportDocumentPlan(
                        sections=[
                            ReportSectionPlan(
                                section_id="introduction",
                                heading="Introduction",
                                goal="Orient the reader.",
                                evidence_handles=["paper-1", "paper-2"],
                            )
                        ],
                        visual_intents=[
                            ReportVisualIntent(
                                visual_id="taxonomy",
                                kind="figure",
                                title="Conceptual taxonomy",
                                purpose="Show the organizing concepts.",
                                section_id="introduction",
                                evidence_handles=["paper-1", "paper-2"],
                                view="taxonomy-map",
                            )
                        ],
                    ),
                    sections=(
                        ReportSectionDraft(
                            section_id="introduction",
                            heading="Introduction",
                            draft_markdown="Evidence-backed claim [@paper-1].\n\n- Method A\n- Method B\n- Method C",
                        ),
                    ),
                ),
            )

            self.assertEqual(result.status, "completed")
            self.assertTrue(
                controller.store.resolve(
                    "attempts/attempt-001/figures/figures_manifest.json"
                ).is_file()
            )
            figure_refs = [artifact for artifact in result.artifacts if artifact.kind == "figure"]
            self.assertEqual(len(figure_refs), 1)
            self.assertEqual(figure_refs[0].path, "figures/taxonomy-map.svg")
            self.assertEqual(figure_refs[0].status, "available")
            body = controller.store.read_text("attempts/attempt-001/report_body.md")
            report = controller.store.read_text("attempts/attempt-001/report.md")
            self.assertIn("figures/taxonomy-map.svg", body)
            self.assertIn("figures/taxonomy-map.svg", report)
            self.assertIn("[@paper-1]", body)
            attempt = controller.store.read_attempt_manifest(
                "attempts/attempt-001/attempt_manifest.json"
            )
            self.assertEqual(
                [artifact.path for artifact in attempt.outputs if artifact.kind == "figure"],
                ["figures/taxonomy-map.svg"],
            )

    def test_missing_renderer_output_is_explicitly_partial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(
                tmp,
                session_id="report-missing-figure",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )

            result = controller.execute_attempt(
                "report",
                attempt_id="attempt-001",
                request=ReportAssemblyRequest(
                    title="Offline report",
                    papers=({"id": "p1", "title": "Caption source"},
                            {"id": "p2", "title": "Body source"},
                            {"id": "unused", "title": "Unused source"}),
                    citation_key_map={"P1": "p1", "P2": "p2"},
                    sections=(
                        ReportSectionDraft(
                            section_id="introduction",
                            heading="Introduction",
                            draft_markdown="A short report [@P2].",
                        ),
                    ),
                ),
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
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(
                tmp,
                session_id="report-order-session",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )

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
            registry = CapabilityRegistry()
            registry.register("report_audit", run_report_audit_capability)
            controller = SessionController.create(
                tmp,
                session_id="report-audit-session",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )
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
            registry = CapabilityRegistry()
            registry.register("report_audit", run_report_audit_capability)
            controller = SessionController.create(
                Path(tmp),
                session_id="report-audit-failure",
                topic="offline report",
                profile="paper_audit",
                registry=registry,
            )
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
