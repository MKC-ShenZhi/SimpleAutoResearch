from __future__ import annotations

import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    ReportTemplateBundle,
    SourceHandle,
)
from simple_ar.report.writing import ARTICLE_OWNER, ReportWritingRequest, resolve_report_writing_resume, run_report_writing_capability


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


class ArticleClient:
    def __init__(self, response=None):
        self.calls = []
        self.response = response if response is not None else {
            'draft_markdown': '## Observations\n\nRecorded value: 0.75.\n\n## Limits\n\nOne declared condition.'}

    def ask_json(self, system, prompt, **kwargs):
        self.calls.append((system, json.loads(prompt), kwargs))
        return self.response


class StructuredArticleWritingTests(unittest.TestCase):
    def request(self, client=None):
        config = ReportRuntimeConfig(reviewer='disabled', max_review_iterations=0,
            allow_source_backtracking=False, outline_strategy='template', max_section_tokens=900)
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
                'results', 'limitations', 'open_questions', 'revision_findings'})
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

    def test_interrupted_article_uses_retained_response_but_missing_response_never_replays(self):
        for response_saved in (False, True):
            with self.subTest(response_saved=response_saved), tempfile.TemporaryDirectory() as tmp:
                session = ArtifactStore(Path(tmp))
                store = ArtifactStore(session.root / 'attempts' / 'original')
                request = self.request()
                def interrupted(system, prompt, **kwargs):
                    if response_saved:
                        store.write_json('response.json', request.llm_client.response)
                    raise RuntimeError('interrupted after request')
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
                if response_saved:
                    recovered = run_report_writing_capability(
                        context=CapabilityContext(store, AttemptManifest('original')), request=offline)
                    self.assertEqual(recovered.status, 'completed')
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

    def test_article_rejects_changed_binding_unknown_owner_and_changed_writer(self):
        for change in ('goal', 'config', 'template', 'owner', 'system', 'writer', 'identity'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                store = ArtifactStore(Path(tmp))
                request = self.request()
                run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')), request=request)
                if change == 'goal':
                    request = replace(request, report_context=request.report_context.model_copy(update={'goal_markdown': 'Changed'}))
                elif change == 'config':
                    request = replace(request, config=request.config.model_copy(update={'max_section_tokens': 800}))
                elif change == 'template':
                    request = replace(request, template=request.template.model_copy(update={'template_markdown': 'Changed'}))
                elif change == 'writer':
                    writer = store.read_json('writer.json')
                    writer['sections'][0]['draft_markdown'] = 'Changed'
                    store.write_json('writer.json', writer)
                else:
                    snapshot = store.read_json('report_inputs.json')
                    snapshot[{'identity': 'source_attempt'}.get(change, change)] = 'Changed'
                    store.write_json('report_inputs.json', snapshot)
                with self.assertRaises(ValueError):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('original')), request=request)
                self.assertEqual(len(request.llm_client.calls), 1)

    def test_unsupported_article_controls_fail_before_any_request_or_snapshot(self):
        for key, value in [('reviewer', 'llm'), ('document_review', True), ('source_strategy', 'batch_refine'),
                ('allow_source_backtracking', True), ('outline_strategy', 'adaptive'),
                ('max_cited_sources', 2), ('allow_llm_fallback', True), ('criteria', 'custom.md'),
                ('max_section_sources', 2), ('max_source_batches', 2)]:
            with self.subTest(control=key), tempfile.TemporaryDirectory() as tmp:
                request = self.request()
                request = replace(request, config=request.config.model_copy(update={key: value}))
                store = ArtifactStore(Path(tmp))
                with self.assertRaisesRegex(ValueError, key):
                    run_report_writing_capability(context=CapabilityContext(store, AttemptManifest('new')), request=request)
                self.assertFalse(store.exists('report_inputs.json'))
                self.assertEqual(request.llm_client.calls, [])

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
        request = self.request(ArticleClient({'draft_markdown': 'Article.', 'source_notes': notes}))
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
                    with patch('simple_ar.report.writing.run_report_agent', side_effect=legacy) as agent:
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
