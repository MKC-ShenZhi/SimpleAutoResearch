"""Assembly-owned prose stays visible without changing drafts or saved data."""
import copy
import json
import unittest
from unittest.mock import Mock

from simple_ar.report.agent import _reviewer_prompt, _writer_prompt, _writer_recovery_prompt
from simple_ar.report.editor import review_document
from simple_ar.report.document_plan import supplied_figure_sources
from simple_ar.report.narrative import _compact_execution_results, evidence_outline_context
from simple_ar.result_analysis.table import TableSpec, describe_table
from simple_ar.report.schema import (
    ReportContext, ReportDocumentPlan, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportVisualIntent, SourceHandle,
)
from simple_ar.report.templates import load_report_template_bundle


def prompt_view(prompt):
    return json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]


class DataDeliveryTests(unittest.TestCase):
    def objects(self, *, full=False, enabled=True):
        section = ReportSectionPlan(section_id="observations", heading="Observations", goal="Compare supplied values",
            evidence_handles=["material:data"], target_words=300)
        memory = ReportMemory(section_plan=[section], document_plan=ReportDocumentPlan(sections=[section]))
        memory.document_plan.visual_intents = [ReportVisualIntent(visual_id="main", kind="figure",
            title="Supplied values", purpose="Compare observations", section_id=section.section_id,
            evidence_handles=["material:data"], view="supplied-data", figure_paths=["figures/bar.svg"])]
        result = {"document_id": "data", "row_count": 2, "evidence_role": "recomputed_from_user_supplied_data",
            "records": [{"group": "A", "column": "score", "count": 2, "missing": 0,
                         "mean": 4.0, "sample_std": 1.0, "min": 3.0, "max": 5.0}],
            "spec": {"mode": "observations", "observation_unit": "one supplied row", "value_unit": "points",
                     "missing": "reject", "plot": "bar"},
            "figures": [{"path": "figures/bar.svg", "caption": "Supplied values in points; not a new experiment."}]}
        context = ReportContext(topic="Write a bounded report", report_mode="supplied_materials",
            results={"supplied_analyses": [result]}, source_handles=[SourceHandle(handle="material:data",
                kind="material", title="Data", metadata={"document_id": "data"})])
        config = ReportRuntimeConfig(data_tables="full" if full else "linked", figures={"enabled": enabled})
        return context, memory, section, config

    def test_all_writing_roles_receive_exact_additions_and_do_not_change_input(self):
        context, memory, section, config = self.objects(full=True)
        original = copy.deepcopy((context.model_dump(), memory.model_dump()))
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Observed values only.")
        common = dict(context=context, memory=memory, section=section, template=template, config=config)
        outline = evidence_outline_context(context, memory, config)
        scope = outline["results"]["supplied_analyses"][0]["records_scope"]
        self.assertIn("different nonmissing rows", scope)
        self.assertIn("not the difference of marginal column means", scope)
        expected = outline["assembly_owned_content"]
        views = [outline,
            prompt_view(_writer_prompt(**common, previous_draft=None, review=None, extra_context=[],
                source_batch_index=1, source_batch_count=1, include_previous_draft=True, draft_mode="section")),
            prompt_view(_writer_recovery_prompt(context=context, memory=memory, section=section, config=config, previous_draft=None, review=None,
                extra_context=[], draft_mode="section")),
            prompt_view(_reviewer_prompt(**common, draft=draft))]
        self.assertEqual(expected[0]["section_id"], "observations")
        self.assertIn("| A | score | 2 | 0 | 4 |", expected[0]["markdown"])
        self.assertIn("not a new experiment", expected[0]["markdown"])
        for view in views[1:3]:
            self.assertEqual(view["assembly_owned_content"], expected)
            self.assertNotIn("assembly_owned_content", view["narrative_context"])
        self.assertEqual(views[-1]["narrative_context"]["assembly_owned_content"], expected)
        self.assertEqual((context.model_dump(), memory.model_dump()), original)

    def test_explicit_inline_selection_preserves_linked_package_and_legacy_behavior(self):
        from simple_ar.report.data_delivery import supplied_data_delivery
        from simple_ar.report.schema import ReportVisualIntent
        context, memory, section, config = self.objects()
        result = context.results["supplied_analyses"][0]
        result["figures"].append({"path": "figures/paired.svg", "caption": "Matched differences, not marginal means."})
        original = copy.deepcopy(result)
        intent = ReportVisualIntent(visual_id="main", kind="figure", title="Comparison", purpose="Interpret pairs",
            section_id=section.section_id, evidence_handles=["material:data"], view="supplied-data",
            figure_paths=["figures/paired.svg"])
        memory.document_plan.visual_intents = [intent]
        for selected, expected in ((["figures/paired.svg"], 1), ([], 0), (None, 2)):
            with self.subTest(selected=selected):
                intent.figure_paths = selected
                restored = ReportDocumentPlan.model_validate_json(memory.document_plan.model_dump_json())
                self.assertEqual(restored.visual_intents[0].figure_paths, selected)
                block = supplied_data_delivery(context, config=config, plan=memory.document_plan,
                    section_ids=[section.section_id])[0]
                self.assertEqual(block["markdown"].count("!["), expected)
                self.assertIn("analysis.md", block["markdown"])
                self.assertIn("analysis.json", block["markdown"])
                self.assertEqual(supplied_data_delivery(context, config=config, plan=restored,
                    section_ids=[section.section_id]), [block])
                if selected:
                    self.assertNotIn("figures/bar.svg", block["markdown"])
                    self.assertIn("figures/paired.svg", block["markdown"])
        intent.figure_paths = ["../unregistered.svg"]
        with self.assertRaisesRegex(ValueError, "registered analysis package"):
            supplied_data_delivery(context, config=config, plan=memory.document_plan, section_ids=[section.section_id])
        self.assertEqual(result, original)

        # A validated script package uses the same placement/delivery path,
        # but must never inherit table arithmetic certification.
        import tempfile
        from pathlib import Path
        from simple_ar.core.artifacts import write_text, write_json
        from simple_ar.result_analysis.script_project import copy_code_analysis_package
        from simple_ar.core.capabilities import CapabilityRegistry
        from simple_ar.core.session import SessionController
        from simple_ar.report.capability import ReportAssemblyRequest, run_report_capability
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            for name, text in {"analysis.py": "import json\nfrom pathlib import Path\nfrom src.panels import total\nroot = Path(__file__).parent\nvalue = total(root / 'data/input.csv')\n(root / 'outputs/results.json').write_text(json.dumps({'observations': [value]}))\nprint(value)\n", "README.md": "Supplied project",
                               "src/panels/__init__.py": "import csv\ndef total(path):\n    with path.open() as stream:\n        return sum(int(row['value']) for row in csv.DictReader(stream))\n",
                               "src/layout.dot": "digraph { input -> output }",
                               "outputs/report.md": "Recorded values, not a fresh experiment.",
                               "data/input.csv": "value\n2\n", "tests/verify_delivery.py": "# retained verifier",
                               "outputs/figure.png": "PNG fixture", "outputs/figure.svg": "<svg/>"}.items():
                write_text(project / name, text)
            caption = 'Observed values (units); marks show the supplied records.'
            write_json(project / "outputs/results.json", {"observations": [2], 'figures': [
                {'path': 'outputs/figure.svg', 'caption': caption}]})
            package = root / "package"
            recorded = copy_code_analysis_package(project, package, workspace=True)
            self.assertEqual(recorded['figures'][0]['caption'], caption)
            # Output shape follows the task, including tables without a plot
            # and several separately named figures. Legacy figure.* stays valid.
            (project / "outputs/figure.png").unlink()
            (project / "outputs/figure.svg").unlink()
            write_text(project / "outputs/summary.csv", "count\n2\n")
            tabular = copy_code_analysis_package(project, root / "tabular", workspace=True)
            self.assertEqual(tabular["figures"], [])
            self.assertIn("outputs/summary.csv", tabular["files"])
            copy_code_analysis_package(root / "tabular/analysis.json", root / "tabular-moved")
            for name in ("left.svg", "right.png", "right.pdf"):
                write_text(project / "outputs" / name, "attachment fixture")
            write_json(project / "outputs/results.json", {'observations': [2], 'figures': [
                'outputs/left.svg',
                {'path': 'outputs/right.pdf', 'caption': 'Alternate-format authored caption.'},
                {'path': '../unregistered.svg', 'caption': 'Must not attach.'}]})
            multi = copy_code_analysis_package(project, None, workspace=True)
            self.assertEqual([row['caption'] for row in multi['figures']],
                ['Figure: left.', 'Alternate-format authored caption.'])
            self.assertEqual({row["path"] for row in multi["figures"]},
                             {"outputs/left.svg", "outputs/right.png"})
            self.assertEqual(next(row for row in multi["figures"] if row["path"].endswith("right.png"))["exports"],
                             {"pdf": "outputs/right.pdf"})
            import shutil
            shutil.rmtree(project)
            moved = root / "moved"
            copy_code_analysis_package(package / "analysis.json", moved)
            import subprocess
            import sys
            (moved / 'outputs/results.json').unlink()
            rebuilt = subprocess.run([sys.executable, str(moved / "analysis.py")], cwd=root,
                capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(rebuilt.stdout.strip(), "2")
            self.assertEqual(json.loads((moved / 'outputs/results.json').read_text()), {'observations': [2]})
            self.assertEqual((moved / "src/layout.dot").read_text(), "digraph { input -> output }")
            self.assertEqual((moved / "data/input.csv").read_text(), "value\n2\n")
            context.results["supplied_analyses"] = [{**recorded, "document_id": "data"}]
            intent.figure_paths = ["outputs/figure.png"]
            block = supplied_data_delivery(context, config=config, plan=memory.document_plan,
                                           section_ids=[section.section_id])[0]
            self.assertIn("did not independently recompute", block["markdown"])
            self.assertNotIn("Arithmetic was rechecked", block["markdown"])
            self.assertIn("outputs/figure.png", block["markdown"])
            self.assertIn(caption, block['markdown'])
            registry = CapabilityRegistry()
            registry.register("report", run_report_capability)
            controller = SessionController.create(root / "session", session_id="script-report", topic="Supplied results",
                                                  profile="survey", registry=registry)
            supplied = controller.store.write_json("input/analysis.json", recorded, kind="analysis_package", schema="code_analysis.v1")
            copy_code_analysis_package(moved / "analysis.json", controller.store.root / "input")
            from simple_ar.research.documents.ingest import DocumentIngestRequest, DocumentBundle, run_document_ingest_capability
            from simple_ar.research.contracts import SourcePlan
            from simple_ar.report.projection import build_material_report_inputs
            registry.register("document_ingest", run_document_ingest_capability)
            citation_map = {"schema_version": "citation_map.v1", "entries": [{
                "paper_id": "recorded-dataset", "model_key": "P1", "title": "Recorded input dataset",
                "url": "https://example.test/data", "bibliography": {"authors": [], "year": "", "doi": ""}}]}
            results_path = moved / "outputs/results.json"
            results_path.write_text(json.dumps({**json.loads(results_path.read_text()), "citation_map": citation_map,
                "stratified": {"control": {"mean": 1}, "comparison": {"mean": 2}}}))
            acquired = controller.execute_attempt("document_ingest", attempt_id="ingest-1", request=DocumentIngestRequest(
                papers=(), source_plan=SourcePlan(queries=["supplied results"], sources=["local_files"],
                    local_documents=[str(moved / "analysis.json")]), extraction_dir=root / "extract",
                analysis_paths=(moved / "analysis.json",)))
            self.assertEqual(acquired.status, "completed", acquired.diagnostics)
            document_ref = next(row for row in acquired.artifacts if row.schema == "document_bundle.v1")
            document_ref = controller.store.ref(Path("attempts/ingest-1") / document_ref.path,
                                                kind=document_ref.kind, schema=document_ref.schema)
            documents = DocumentBundle.from_handoff_dict(controller.store.read_json(document_ref))
            projected, _ = build_material_report_inputs(topic="Interpret existing results", documents=documents,
                                                        documents_ref=document_ref, assets=[])
            self.assertEqual(projected.results["supplied_analyses"][0]["schema_version"], "code_analysis.v1")
            self.assertEqual(projected.citation_key_map, {"P1": "recorded-dataset"})
            self.assertEqual(projected.papers[0]["title"], "Recorded input dataset")
            self.assertEqual(projected.papers[0]["authors"], [])
            reference = next(h for h in projected.source_handles if h.handle == 'reference:recorded-dataset')
            self.assertNotIn('document_id', reference.metadata)
            projected_plan = memory.document_plan.model_copy(deep=True)
            projected_plan.sections[0].evidence_handles = [projected.source_handles[0].handle]
            projected_plan.visual_intents[0].evidence_handles = [projected.source_handles[0].handle]
            projected_delivery = supplied_data_delivery(projected, config=config, plan=projected_plan,
                section_ids=[section.section_id])[0]
            self.assertIn('analyses/analysis-001/outputs/figure.png', projected_delivery['markdown'])
            self.assertIn(caption, projected_delivery['markdown'])
            self.assertIn("observations", " ".join(row.text for row in documents.chunks))
            numeric = next(row for row in documents.sections if row.heading == "Recorded script outputs: stratified")
            self.assertEqual(json.loads(numeric.text), {"stratified": {"control": {"mean": 1}, "comparison": {"mean": 2}}})
            self.assertTrue(numeric.source_path.endswith("outputs/results.json"))
            self.assertTrue(any(row.metadata.get("heading") == numeric.heading for row in documents.chunks))
            compact = _compact_execution_results(projected.results)
            self.assertEqual(compact["supplied_analyses"][0]["evidence_role"],
                             "validated_script_output_not_independently_recomputed")
            self.assertNotIn("records_scope", compact["supplied_analyses"][0])
            result_delivery = controller.execute_attempt("report", attempt_id="report-1",
                request=ReportAssemblyRequest(title="Supplied results", sections=(ReportSectionDraft(
                    section_id=section.section_id, heading=section.heading, draft_markdown="The supplied result is 2."),),
                    config=config, document_plan=memory.document_plan, table_analyses=(supplied,),
                    analysis_handles={supplied.path: "material:data"}), inputs=(supplied,))
            self.assertEqual(result_delivery.status, "completed", result_delivery.diagnostics)
            delivered_text = controller.store.resolve('attempts/report-1/report.md').read_text()
            self.assertIn(caption, delivered_text)
            figure_paths = [row.path for row in result_delivery.artifacts if row.kind == "figure"]
            self.assertEqual({Path(path).suffix for path in figure_paths}, {".png", ".svg"})
            attachments = [row for row in result_delivery.artifacts if row.kind == "analysis_attachment"]
            self.assertTrue(any(row.path.endswith("data/input.csv") for row in attachments))
            malformed = {**recorded, "files": [*recorded["files"], "../outside.py"]}
            write_json(moved / "analysis.json", malformed)
            with self.assertRaisesRegex(ValueError, "package-local"):
                copy_code_analysis_package(moved / "analysis.json", root / "rejected")

    def test_unselected_figures_remain_linked_instead_of_filling_the_body(self):
        from simple_ar.report.data_delivery import supplied_data_delivery
        context, memory, section, config = self.objects()
        memory.document_plan.visual_intents = []
        before = copy.deepcopy(context.model_dump())
        block = supplied_data_delivery(context, config=config, plan=memory.document_plan,
            section_ids=[section.section_id])[0]
        self.assertNotIn("![", block["markdown"])
        self.assertIn("analysis.md", block["markdown"])
        self.assertIn("analysis.json", block["markdown"])
        self.assertEqual(context.model_dump(), before)

    def test_document_review_counts_additions_separately_and_preserves_draft(self):
        context, memory, section, config = self.objects()
        from simple_ar.report.data_delivery import supplied_data_delivery
        additions = supplied_data_delivery(context, config=config, plan=memory.document_plan, section_ids=[section.section_id])
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Two observed values.")
        client = Mock(spec=['ask_json'])
        client.ask_json.return_value = {"section_reviews": []}
        review_document(client=client, template=load_report_template_bundle(report_mode=context.report_mode, config=config), memory=memory,
            sections=[draft], config=config, execution_summary={}, metric_summary={}, assembly_owned_content=additions)
        view = json.loads(client.ask_json.call_args.args[1])
        length = view["length_observation"]
        self.assertEqual(length["total_markdown_tokens"], 3)
        self.assertEqual(length["known_delivery_markdown_tokens"], 3 + len(additions[0]["markdown"].split()))
        self.assertEqual(view["assembly_owned_content"], additions)
        self.assertEqual(draft.draft_markdown, "Two observed values.")

    def test_planning_reserve_matches_real_assembly_overhead_not_only_model_body(self):
        from simple_ar.report.narrative import budget_document_plan, delivery_text_observation
        from simple_ar.report.document_plan import resolve_document_plan
        context, memory, section, config = self.objects(full=True)
        context.problem_markdown = "Write 350-500 words."
        second = section.model_copy(update={"section_id": "discussion", "heading": "Discussion", "target_words": 100})
        plan = resolve_document_plan(sections=[section, second], contract={}, config=config, title="Recorded values")
        original = copy.deepcopy((context.model_dump(), memory.model_dump(), plan.model_dump()))
        request = {"unit": "words", "scope": "whole_document", "request_quote": context.problem_markdown,
            "min_words": 350, "max_words": 500, "target_words": 430}
        budgeted = budget_document_plan(context, memory, config, plan, request)
        words = ["Alpha beta gamma.", "Delta epsilon."]
        drafts = [ReportSectionDraft(section_id=row.section_id, heading=row.heading, draft_markdown=text)
                  for row, text in zip(plan.sections, words)]
        observed = delivery_text_observation(context, memory.model_copy(update={"document_plan": budgeted}), drafts, config)
        fixed = observed["markdown_token_count"] - sum(len(text.split()) for text in words)
        self.assertEqual(budgeted.length_budget["known_fixed_markdown_tokens"], fixed)
        self.assertEqual(sum(row.target_words for row in budgeted.sections), 430 - fixed)
        self.assertNotIn("SARPlanningPlaceholder", json.dumps(budgeted.model_dump()))
        self.assertEqual((context.model_dump(), memory.model_dump(), plan.model_dump()), original)
        context.results["supplied_analyses"][0].pop("row_count")
        with self.assertRaisesRegex(ValueError, "known assembly forecast"):
            budget_document_plan(context, memory, config, plan, request)

    def test_planning_unknown_future_references_are_not_reported_as_complete_zero_cost(self):
        from simple_ar.report.narrative import budget_document_plan, delivery_text_observation
        context, memory, section, config = self.objects()
        context.problem_markdown = "Write 350-500 words."
        context.papers = [{"id": "paper-1", "title": "A reference whose selection has not been made"}]
        plan = memory.document_plan
        request = {"unit": "words", "scope": "whole_document", "request_quote": context.problem_markdown,
            "min_words": 350, "max_words": 500, "target_words": 450}
        budgeted = budget_document_plan(context, memory, config, plan, request)
        self.assertIn("cited_reference_selection_and_display", budgeted.length_budget["unresolved_future_components"])
        self.assertFalse(budgeted.length_budget["rendering_performed"])
        self.assertEqual(context.papers[0]["id"], "paper-1")
        observed = delivery_text_observation(context, memory.model_copy(update={"document_plan": budgeted}), [], config)
        self.assertEqual(observed["preview_status"], "unavailable")
        self.assertIsNone(observed["markdown_token_count"])

    def test_ambiguous_placement_and_figure_off_keep_provenance_without_fake_owner(self):
        context, memory, section, config = self.objects(enabled=False)
        memory.document_plan.visual_intents = []
        from simple_ar.report.data_delivery import supplied_data_delivery
        second = section.model_copy(update={"section_id": "discussion", "heading": "Discussion"})
        memory.document_plan.sections.append(second)
        additions = supplied_data_delivery(context, config=config, plan=memory.document_plan,
            section_ids=[section.section_id, second.section_id])
        self.assertEqual(additions[0]["section_id"], "supplied_analysis_1")
        self.assertNotIn("![", additions[0]["markdown"])
        self.assertIn("Arithmetic was rechecked", additions[0]["markdown"])
        self.assertIn("scientific validity were not independently verified", additions[0]["markdown"])
        self.assertIn("analysis.json", additions[0]["markdown"])

    def test_addition_text_is_not_silently_truncated_for_full_review(self):
        context, memory, section, config = self.objects()
        client = Mock(spec=['ask_json'])
        with self.assertRaisesRegex(ValueError, "bounded source window"):
            review_document(client=client, template=load_report_template_bundle(report_mode=context.report_mode, config=config), memory=memory,
                sections=[ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Short.")],
                config=config, execution_summary={}, metric_summary={},
                assembly_owned_content=[{"markdown": "caption " * 9000, "markdown_token_count": 9000}])
        client.ask_json.assert_not_called()

    def test_partial_saved_package_does_not_claim_zero_delivery_length(self):
        context, memory, section, config = self.objects()
        context.results["supplied_analyses"][0].pop("row_count")
        from simple_ar.report.data_delivery import supplied_data_delivery
        additions = supplied_data_delivery(context, config=config, plan=memory.document_plan, section_ids=[section.section_id])
        self.assertEqual(additions[0]["preview_status"], "unavailable")
        self.assertEqual(additions[0]["missing_field"], "row_count")

    def test_text_preview_matches_canonical_delivery_including_headings_and_references(self):
        from pathlib import Path
        from simple_ar.report.capability import ReportAssemblyRequest, assemble_report_document, preview_report_document
        from simple_ar.report.data_delivery import attach_delivery_block, supplied_data_delivery
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.figures import ReportFigureResult
        context, memory, section, config = self.objects()
        context.papers = [{"id": "paper-1", "title": "A deliberately long reference title", "authors": ["Example Author"]},
                          {"id": "unused", "title": "Not cited in the prose"}]
        context.citation_key_map = {"P1": "paper-1"}
        memory.document_plan.title = "Frozen Reader Facing Title"
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
            draft_markdown="## Observations\n\nTwo observed values [@P1].\n\n## References\n\nIgnore this draft reference.")
        before = copy.deepcopy((context.model_dump(), memory.model_dump(), draft.model_dump()))
        blocks = supplied_data_delivery(context, config=config, plan=memory.document_plan, section_ids=[section.section_id])
        sections = attach_delivery_block([draft], blocks[0])
        request = ReportAssemblyRequest(title=context.topic, sections=sections, config=config,
            document_plan=memory.document_plan, papers=tuple(context.papers), citation_key_map=context.citation_key_map)
        preview = preview_report_document(request)
        renderer = Mock()
        renderer.render.side_effect = lambda **kwargs: ReportFigureResult(report_markdown=kwargs["report_markdown"], figures=[])
        actual = assemble_report_document(request, report_dir=Path("unused-no-files"), figure_renderer=renderer)
        self.assertEqual(preview.report_markdown, actual.report_markdown)
        self.assertIn("Frozen Reader Facing Title", preview.report_markdown)
        self.assertIn("A deliberately long reference title", preview.report_markdown)
        self.assertNotIn("Not cited in the prose", preview.report_markdown)
        self.assertNotIn("Ignore this draft reference", preview.report_markdown)
        self.assertIn(blocks[0]["markdown"], preview.report_body_markdown)
        observed = delivery_text_observation(context, memory, [draft], config)
        self.assertEqual(observed["markdown_token_count"], len(actual.report_markdown.split()))
        self.assertEqual(observed["references"]["markdown"], actual.references_markdown)
        self.assertEqual(actual.citation_numbers, {"paper-1": 1})
        self.assertEqual(observed["references"]["model_keys"], {"P1": "paper-1"})
        self.assertNotIn("Ignore this draft reference", observed["references"]["markdown"])
        self.assertFalse(observed["rendering_performed"])
        self.assertEqual((context.model_dump(), memory.model_dump(), draft.model_dump()), before)

    def test_missing_attachment_or_uncited_registered_sources_leave_count_unknown(self):
        from simple_ar.report.narrative import delivery_text_observation
        context, memory, section, config = self.objects()
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Supplied values.")
        empty = draft.model_copy(update={"draft_markdown": ""})
        self.assertIsNone(delivery_text_observation(context, memory, [empty], config)["markdown_token_count"])
        context.papers = [{"id": "paper-1", "title": "Registered but uncited"}]
        preview = delivery_text_observation(context, memory, [draft], config)
        self.assertEqual(preview["preview_status"], "unavailable")
        self.assertIsNone(preview["markdown_token_count"])
        self.assertIsNone(preview["references"])
        context.papers = []
        context.results["supplied_analyses"][0].pop("row_count")
        self.assertIsNone(delivery_text_observation(context, memory, [draft], config)["markdown_token_count"])

    def test_experiment_appendix_is_counted_without_rendering_or_altering_draft(self):
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document
        from simple_ar.report.schema import MetricSource
        context, memory, section, config = self.objects()
        context.results = {}
        context.report_mode = "experiment"
        context.metric_sources = [MetricSource(metric_id="observed-loss", name="loss", value=2.0, artifact="results.json")]
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="One recorded result.")
        canonical = preview_report_document(ReportAssemblyRequest(title=context.topic,
            sections=(draft,), config=config, document_plan=memory.document_plan, experiment_context=context))
        preview = delivery_text_observation(context, memory, [draft], config)
        self.assertEqual(preview["markdown_token_count"], len(canonical.report_markdown.split()))
        self.assertIn("Experiment Records", canonical.report_markdown)
        self.assertNotIn("Metric Provenance", canonical.report_markdown)
        self.assertEqual(draft.draft_markdown, "One recorded result.")

    def test_section_review_counts_candidate_not_superseded_adopted_version(self):
        from simple_ar.report.narrative import delivery_text_observation
        context, memory, section, config = self.objects()
        other = section.model_copy(update={"section_id": "discussion", "heading": "Discussion", "evidence_handles": []})
        memory.section_plan.append(other)
        memory.document_plan.sections.append(other)
        adopted = [ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Existing supplied data."),
                   ReportSectionDraft(section_id=other.section_id, heading=other.heading, draft_markdown="superseded " * 80)]
        candidate = adopted[-1].model_copy(update={"draft_markdown": "Current short interpretation."})
        before = copy.deepcopy([row.model_dump() for row in adopted])
        view = prompt_view(_reviewer_prompt(context=context, memory=memory, section=other, config=config,
            template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            draft=candidate, adopted_sections=adopted))
        current = delivery_text_observation(context, memory, [adopted[0], candidate], config)
        self.assertEqual(view["narrative_context"]["delivery_text_observation"], current)
        self.assertNotEqual(current["markdown_token_count"], delivery_text_observation(context, memory, adopted, config)["markdown_token_count"])
        self.assertEqual([row.model_dump() for row in adopted], before)

    def test_pending_frozen_attachment_owner_is_visible_not_silently_omitted(self):
        from simple_ar.report.narrative import delivery_text_observation, narrative_context
        from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document
        from simple_ar.report.data_delivery import attach_delivery_block, supplied_data_delivery
        context, memory, owner, config = self.objects()
        first = owner.model_copy(update={"section_id": "purpose", "heading": "Purpose", "evidence_handles": []})
        memory.section_plan.insert(0, first)
        memory.document_plan.sections.insert(0, first)
        draft = ReportSectionDraft(section_id=first.section_id, heading=first.heading, draft_markdown="Scope of existing data.")
        pending = ReportSectionDraft(section_id=owner.section_id, heading=owner.heading, draft_markdown="")
        blocks = supplied_data_delivery(context, config=config, plan=memory.document_plan,
            section_ids=[first.section_id, owner.section_id])
        with self.assertRaisesRegex(ValueError, "absent section"):
            attach_delivery_block([draft], blocks[0])
        expected = preview_report_document(ReportAssemblyRequest(title=context.topic,
            sections=attach_delivery_block([draft, pending], blocks[0]), config=config, document_plan=memory.document_plan))
        observed = narrative_context(memory, owner, [draft], context=context, config=config)["delivery_text_observation"]
        self.assertEqual(observed["pending_owner_sections"], [owner.section_id])
        self.assertEqual(observed["markdown_token_count"], len(expected.report_markdown.split()))
        self.assertIn(blocks[0]["markdown"], expected.report_body_markdown)
        # The section reviewer includes its initial candidate even when no
        # adopted draft has that id yet; the data-only placeholder then ends.
        candidate = pending.model_copy(update={"draft_markdown": "Describe the supplied records."})
        view = prompt_view(_reviewer_prompt(context=context, memory=memory, section=owner, config=config,
            template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            draft=candidate, adopted_sections=[draft]))
        self.assertEqual(view["narrative_context"]["delivery_text_observation"]["pending_owner_sections"], [])
        self.assertEqual(view["narrative_context"]["delivery_text_observation"]["markdown_token_count"],
                         delivery_text_observation(context, memory, [draft, candidate], config)["markdown_token_count"])

    def test_full_review_uses_canonical_count_or_unknown_not_competing_partial_sum(self):
        context, memory, section, config = self.objects()
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Two supplied values.")
        client = Mock(spec=['ask_json'])
        client.ask_json.return_value = {"section_reviews": []}
        for observation in ({"preview_status": "pre_render_text_preview", "markdown_token_count": 125, "counting_rule": "Canonical text."},
                            {"preview_status": "unavailable", "markdown_token_count": None, "counting_rule": "Unknown text."}):
            review_document(client=client, template=load_report_template_bundle(report_mode=context.report_mode, config=config),
                memory=memory, sections=[draft], config=config, execution_summary={}, metric_summary={},
                delivery_text_observation=observation)
            view = json.loads(client.ask_json.call_args.args[1])
            self.assertEqual(view["length_observation"]["known_delivery_markdown_tokens"], observation["markdown_token_count"])
            self.assertEqual(view["length_observation"]["delivery_count_scope"], observation["preview_status"])

    def test_reference_preview_tracks_current_citations_not_registry_or_superseded_draft(self):
        from simple_ar.report.narrative import delivery_text_observation
        context, memory, section, config = self.objects()
        context.papers = [{"id": "paper-1", "title": "First source"},
                          {"id": "paper-2", "title": "Second source"},
                          {"id": "unused", "title": "Uncited source"}]
        context.citation_key_map = {"P1": "paper-1", "P2": "paper-2", "P3": "unused"}
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
            draft_markdown="Second [@P2], then first [@P1].")
        before = copy.deepcopy((context.model_dump(), memory.model_dump(), draft.model_dump()))
        original = delivery_text_observation(context, memory, [draft], config)["references"]
        self.assertEqual(original["citation_numbers"], {"paper-2": 1, "paper-1": 2})
        self.assertIn("[1] Second source", original["markdown"])
        self.assertIn("[2] First source", original["markdown"])
        self.assertNotIn("Uncited source", original["markdown"])
        candidate = draft.model_copy(update={"draft_markdown": "First source alone [@P1]."})
        changed = delivery_text_observation(context, memory, [candidate], config)["references"]
        self.assertEqual(changed["citation_numbers"], {"paper-1": 1})
        self.assertEqual(changed["model_keys"], {"P1": "paper-1"})
        self.assertNotIn("Second source", changed["markdown"])
        self.assertEqual((context.model_dump(), memory.model_dump(), draft.model_dump()), before)

    def test_absent_bibliography_is_known_empty_not_unavailable_or_fenced_example(self):
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.assembler import split_report_references, strip_report_references
        context, memory, section, config = self.objects()
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
            draft_markdown="An example:\n\n```markdown\n## References\nNot a bibliography.\n```\n\nObserved data only.")
        observed = delivery_text_observation(context, memory, [draft], config)
        self.assertEqual(observed["references"], {"markdown": "", "citation_numbers": {}, "model_keys": {}})
        body, references = split_report_references(draft.draft_markdown + "\n\n## References\n\nReal entry.\n")
        self.assertIn("Not a bibliography.", body)
        self.assertEqual(references, "## References\n\nReal entry.\n")
        self.assertEqual(strip_report_references(draft.draft_markdown + "\n\n## References\n\nReal entry.\n"), body)

    def test_document_review_receives_complete_read_only_references_within_same_window(self):
        from simple_ar.report.narrative import delivery_text_observation
        context, memory, section, config = self.objects()
        context.papers = [{"id": "paper-1", "title": "Current source"}]
        context.citation_key_map = {"P1": "paper-1"}
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
            draft_markdown="Known source [@P1].")
        observed = delivery_text_observation(context, memory, [draft], config)
        client = Mock(spec=['ask_json'])
        client.ask_json.return_value = {"section_reviews": []}
        review_document(client=client, template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            memory=memory, sections=[draft], config=config, execution_summary={}, metric_summary={},
            delivery_text_observation=observed)
        view = json.loads(client.ask_json.call_args.args[1])
        self.assertEqual(view["delivery_text_observation"]["references"], observed["references"])
        self.assertIn("assembly-owned references", " ".join(view["edit_scope"]["read_only_components"]))
        self.assertTrue(view["length_observation"]["assembly_preview_complete"])
        client.reset_mock()
        oversized = {**observed, "references": {**observed["references"], "markdown": "entry " * 12000}}
        with self.assertRaisesRegex(ValueError, "bounded source window"):
            review_document(client=client, template=load_report_template_bundle(report_mode=context.report_mode, config=config),
                memory=memory, sections=[draft], config=config, execution_summary={}, metric_summary={},
                delivery_text_observation=oversized)
        client.ask_json.assert_not_called()

    def test_planning_and_shared_writer_reviewer_results_keep_actual_plot_encoding(self):
        result = describe_table([{'value': value} for value in (1, 2, 100)], TableSpec(('value',), 'specimen'))
        # Rendering/export is checked in table figures; this owner checks consumer projection.
        figures = [{'path': 'figures/value.svg', 'exports': {'pdf': 'figures/value.pdf', 'png': 'figures/value.png'},
                    'encoding': {'plot': 'bar', 'statistics_shown': ['mean'], 'role': 'marginal'}}]
        analysis = {**result, 'figures': figures, 'document_id': 'analysis-1',
                    'evidence_role': 'recomputed_from_user_supplied_data'}
        context = ReportContext(topic='Explain data', report_mode='supplied_materials',
            results={'supplied_analyses': [analysis]},
            source_handles=[SourceHandle(handle='S1', kind='material', title='Measurements',
                metadata={'document_id': 'analysis-1'})])
        planned = supplied_figure_sources(context)[0]
        payload = evidence_outline_context(context, ReportMemory(source_handles=context.source_handles), ReportRuntimeConfig())
        compact = payload['results']['supplied_analyses'][0]
        self.assertEqual(planned['document_id'], compact['document_id'])
        self.assertEqual(planned['figure_count'], 1)
        self.assertNotIn('figures', planned)
        self.assertNotIn('captions', planned)
        self.assertEqual(compact['figures'][0]['encoding'], figures[0]['encoding'])
        self.assertEqual(json.dumps(payload).count('"statistics_shown"'), 1)
        self.assertEqual(compact['figures_omitted'], 0)
        context.results['supplied_analyses'][0]['figures'] *= 13
        compact = _compact_execution_results(context.results)['supplied_analyses'][0]
        self.assertEqual(len(compact['figures']), 12)
        self.assertEqual(compact['figures_omitted'], 1)

    def test_old_unknown_encoding_is_not_inferred_from_filename_or_caption(self):
        result = describe_table([{'value': 1}], TableSpec(('value',), 'specimen'))
        context = ReportContext(topic='Old input', report_mode='supplied_materials',
            results={'supplied_analyses': [{**result, 'document_id': 'old', 'evidence_role': 'supplied',
                'figures': [{'path': 'box.svg', 'caption': 'Looks like variation'}]}]},
            source_handles=[SourceHandle(handle='S1', kind='material', title='Old', metadata={'document_id': 'old'})])
        projected = _compact_execution_results(context.results)['supplied_analyses'][0]
        self.assertNotIn('encoding', projected['figures'][0])
