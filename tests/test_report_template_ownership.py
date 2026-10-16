"""A frozen adaptive plan owns topology; factual/custom review requirements stay."""
import json
import unittest

from simple_ar.report.agent import _maybe_adapt_outline, _reviewer_context, _evidence_outline_sections
from tests.report_review_fixtures import ReviewPromptCapture
from simple_ar.report.editor import review_document
from simple_ar.report.schema import (
    ReportContext, ReportDocumentPlan, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportToolResult, SourceHandle,
)
from simple_ar.report.templates import drafting_template_guidance, load_report_template_bundle
from simple_ar.report.narrative import evidence_outline_context
from simple_ar.report.templates import planning_template_guidance


class TemplateOwnershipTests(unittest.TestCase):
    def test_initial_writer_reads_are_projected_only_for_cold_section_review(self):
        from simple_ar.report.agent import run_report_agent
        from simple_ar.report.tool_gateway import ReportToolGateway
        config, template, memory, plan, draft = self.objects()
        config = config.model_copy(update={"outline_strategy": "template", "max_review_iterations": 0})
        context = ReportContext(topic="Use the supplied source", report_mode="supplied_materials")
        memory.outline_planning["context_requests"] = [
            {"tool_name": "get_paper_brief", "arguments": {"citation_key": "P1"}},
            {"tool_name": "get_synthesis_brief", "arguments": {}}]
        brief = ReportToolResult(tool_name="get_paper_brief", content={"handles": [
            SourceHandle(handle="paper:one", kind="paper", metadata={
                "reading_notes": {"method": "Earlier reader interpretation"},
                "evidence_passages": [{"text": "Literal original conditions"}]}).model_dump(mode="json")]})
        synthesis = ReportToolResult(tool_name="get_synthesis_brief", content={"text": "Earlier synthesis interpretation"})
        seen = {}
        class Capture:
            def ask_json(self, system, prompt, *, label="", **unused):
                seen[label] = json.loads(prompt[prompt.index("{"):])
                return ({"section_id": plan.section_id, "verdict": "pass"} if "reviewer" in label else
                        {"section_id": plan.section_id, "heading": plan.heading, "draft_markdown": draft.draft_markdown})
        gateway = ReportToolGateway(context)
        gateway.call = lambda request: brief if request.tool_name == "get_paper_brief" else synthesis
        result = run_report_agent(client=Capture(), context=context, template=template, memory=memory,
            config=config, gateway=gateway)
        written = json.dumps(seen[f"report-writer-{plan.section_id}"])
        reviewed = json.dumps(seen[f"report-reviewer-{plan.section_id}"])
        for interpretation in ("Earlier reader interpretation", "Earlier synthesis interpretation"):
            self.assertIn(interpretation, written)
            self.assertNotIn(interpretation, reviewed)
        self.assertIn("Literal original conditions", reviewed)
        for evidence in (brief, synthesis):
            self.assertIn(evidence, result.tool_results)

    def test_section_review_keeps_primary_evidence_without_planning_answers(self):
        from simple_ar.report.schema import ReportArgumentPlan, ReportSectionReview, ReviewerFinding
        config, template, memory, plan, draft = self.objects()
        context = ReportContext(topic="Compare supplied observations", report_mode="supplied_materials",
            goal_markdown="Preserve the original request and discuss observed limitations.",
            source_comparisons=[{"interpretation": "Earlier synthesis answer"}])
        plan.goal = "Unverified planning conclusion"
        plan.evidence_handles = ["paper:one"]
        plan.target_words = 300
        memory.document_plan.argument_plan = ReportArgumentPlan(question="A proposed question",
            answer="Unverified argument answer")
        memory.source_handles = [SourceHandle(handle="paper:one", kind="paper", citation_key="P1",
            metadata={"evidence_passages": [{"text": "Primary passage with its conditions", "chunk_id": "chunk:one"}],
                "reading_notes": {"method": "Earlier reader answer"}})]
        neighbor = ReportSectionDraft(section_id="other", heading="Observations",
            draft_markdown="Previously adopted measured prose, not independently checked.")
        primary = ReportToolResult(tool_name="get_paper_brief", content={"handles": [
            SourceHandle(handle="paper:two", kind="paper", metadata={
                "evidence_passages": [{"text": "Additional original source passage"}],
                "reading_notes": {"method": "Earlier supplementary reader answer"}}).model_dump(mode="json")]})
        derived = [ReportToolResult(tool_name="get_synthesis_brief", content={"text": "Earlier synthesis answer"})
                   for _ in range(7)]
        prior = ReportSectionReview(section_id=plan.section_id, verdict="revise_required", findings=[
            ReviewerFinding(finding_id="old", type="unsupported_claim", severity="major",
                message="A fallible historical allegation", section_id=plan.section_id)])
        original = memory.model_dump(mode="json")
        tools_before = [row.model_dump(mode="json") for row in [primary, *derived]]
        for revision in (False, True):
            checked = _reviewer_context(context=context, template=template, memory=memory, section=plan,
                draft=draft, adopted_sections=[neighbor], extra_context=[primary, *derived], config=config,
                revision_review=prior if revision else None, previous_draft=draft if revision else None)
            encoded = json.dumps(checked)
            for excluded in (plan.goal, "Unverified argument answer", "Earlier reader answer"):
                self.assertNotIn(excluded, encoded)
            for retained in (context.goal_markdown, draft.draft_markdown, neighbor.draft_markdown,
                             "Primary passage with its conditions", "Additional original source passage",
                             "Earlier supplementary reader answer"):
                self.assertIn(retained, encoded)
            self.assertEqual(checked["section_constraints"]["target_words"], 300)
            self.assertEqual(len(checked["extra_tool_context"]), len(tools_before))
            self.assertEqual(checked["extra_tool_context_omitted"], 0)
            self.assertEqual([row["tool_name"] for row in checked["extra_tool_context"]],
                             [row["tool_name"] for row in tools_before])
            self.assertEqual(checked["extra_tool_context"][0]["tool_name"], "get_paper_brief")
            self.assertEqual(checked["extra_tool_context"][1]["content"]["text_status"]["independent_verification"],
                             "not_performed")
            if revision:
                self.assertEqual(checked["revision_context"]["target_findings"],
                                 [row.model_dump(mode="json") for row in prior.findings])
        self.assertEqual(memory.model_dump(mode="json"), original)
        self.assertEqual([row.model_dump(mode="json") for row in [primary, *derived]], tools_before)

    def test_independent_review_preserves_authored_template_not_inferred_goal(self):
        config, template, memory, plan, draft = self.objects()
        config = config.model_copy(update={"template": "project/custom.md", "criteria": "project/review.md"})
        template = template.model_copy(update={"template_markdown": "## Evidence\nDiscuss incompatible conditions explicitly.",
            "criteria_markdown": "Keep the project-specific review requirements."})
        section = _reviewer_context(context=ReportContext(topic="Original task", report_mode="supplied_materials"),
            template=template, memory=memory, section=plan, draft=draft, config=config)
        capture = ReviewPromptCapture()
        review_document(client=capture, template=template, memory=memory, sections=[draft], config=config,
            execution_summary={}, metric_summary={})
        for view in (section, capture.views[0]):
            self.assertEqual(view["authored_template_requirements"], template.template_markdown)
            self.assertNotIn(plan.goal, json.dumps(view))
        self.assertEqual(section["criteria_markdown"], template.criteria_markdown)
        self.assertEqual(capture.views[0]["criteria"], template.criteria_markdown)

    def test_source_handles_are_explicit_without_promoting_passage_identifiers(self):
        config, template, memory, _, _ = self.objects("material_report")
        memory.source_handles = [SourceHandle(handle="paper:registered", kind="paper", citation_key="P1",
            metadata={"document_id": "document", "passages": [{"chunk_id": "chunk:real", "text": "A source passage"}]})]
        view = evidence_outline_context(ReportContext(topic="Explain", report_mode="supplied_materials"),
            memory, config.model_copy(update={"outline_strategy": "adaptive"}), template=template)
        self.assertEqual(view["evidence_handle_choices"], ["paper:registered"])
        response = {"sections": [{"heading": "Methods", "goal": "Explain", "evidence_handles": ["chunk:real"]},
                                 {"heading": "Limits", "goal": "Interpret", "evidence_handles": ["P1"]}]}
        with self.assertRaisesRegex(ValueError, "Methods.*chunk:real.*paper:registered"):
            _evidence_outline_sections(response, memory=memory, config=config)
        self.assertEqual(response["sections"][0]["evidence_handles"], ["chunk:real"])

    def test_adaptive_planner_receives_genre_not_fallback_chapter_assignments(self):
        for name in ("material_report", "analysis_report", "experiment", "survey"):
            with self.subTest(name=name):
                config, template, memory, _, _ = self.objects(name)
                config = config.model_copy(update={"outline_strategy": "adaptive"})
                memory = memory.model_copy(update={"document_plan": None, "outline_planning": {}})
                before = memory.model_dump(mode="json")
                context = ReportContext(topic="A question from the supplied materials", report_mode="supplied_materials")
                for retry in (False, True):
                    view = evidence_outline_context(context, memory, config, template=template, retry=retry)
                    self.assertEqual(view["template_responsibilities"], [])
                    self.assertIn("## Intended Use", view["template_guidance"])
                    self.assertNotIn("Draft order:", view["template_guidance"])
                    self.assertNotIn("Conditional evidence", view["template_guidance"])
                    self.assertEqual(view["sources_omitted"], 0)
                self.assertEqual(memory.model_dump(mode="json"), before)

    def test_fixed_and_custom_planning_keep_author_structure(self):
        config, template, memory, _, _ = self.objects("material_report")
        context = ReportContext(topic="Keep my structure", report_mode="supplied_materials")
        for config in (config.model_copy(update={"outline_strategy": "template"}),
                       config.model_copy(update={"template": "my-project/material_report.md", "outline_strategy": "adaptive"})):
            view = evidence_outline_context(context, memory, config, template=template)
            self.assertEqual(view["template_guidance"], template.template_markdown)
            self.assertEqual(view["template_responsibilities"][0]["heading"], "Conditional evidence")
            self.assertEqual(planning_template_guidance(template, config), template.template_markdown)

    def test_document_roles_omit_drafting_goals_without_changing_saved_plan(self):
        from simple_ar.report.schema import ReviewerFinding
        config, template, memory, plan, draft = self.objects()
        plan.goal = "Include an interpretation named only in the plan, subject to evidence."
        capture = ReviewPromptCapture()
        prior = ReviewerFinding(finding_id="old", type="unsupported_claim", severity="major",
            section_id=plan.section_id, message="A fallible allegation about current prose.")
        memory.reviewer_findings = [prior, ReviewerFinding(finding_id="wording", type="style",
            severity="minor", message="Simplify prose.")]
        before = memory.model_dump(mode="json")
        for label, findings, correction in (("report-document-reviewer", [], {}),
                                           ("report-document-verifier", [], {"error": "Saved format error"}),
                                           ("report-document-finding-checker", [prior], {})):
            review_document(client=capture, template=template, memory=memory, sections=[draft], config=config,
                execution_summary={}, metric_summary={}, historical_findings=findings,
                format_correction=correction, label=label)
        for index, view in enumerate(capture.views):
            self.assertEqual(view["sections"][0]["markdown"], draft.draft_markdown)
            self.assertNotIn("section_responsibilities", view)
            self.assertNotIn("section_responsibilities_status", view)
            self.assertNotIn(plan.goal, json.dumps(view))
            if index == 2:
                self.assertEqual([row["finding_id"] for row in view["historical_findings_to_check"]], ["old"])
                self.assertEqual(view["historical_findings_role"], "prior_model_opinions_not_source_facts")
            else:
                self.assertEqual(view["historical_findings_to_check"], [])
                self.assertIn("criteria", view)
        self.assertEqual(memory.model_dump(mode="json"), before)

    def objects(self, name="survey", strategy="topic_specific_outline"):
        config = ReportRuntimeConfig(template=name)
        template = load_report_template_bundle(report_mode="research_only", config=config)
        plan = ReportSectionPlan(section_id="conditions", heading="Conditional evidence", goal="Explain what the observations establish")
        memory = ReportMemory(section_plan=[plan], document_plan=ReportDocumentPlan(sections=[plan]),
            outline_planning={"strategy": strategy, "status": "adapted"})
        draft = ReportSectionDraft(section_id=plan.section_id, heading=plan.heading, draft_markdown="Bounded source claims.")
        return config, template, memory, plan, draft

    def views(self, config, template, memory, plan, draft):
        section = _reviewer_context(context=ReportContext(topic="Evidence", report_mode="research_only"),
            template=template, memory=memory, section=plan, draft=draft, config=config)
        capture = ReviewPromptCapture()
        review_document(client=capture, template=template, memory=memory, sections=[draft],
            config=config, execution_summary={}, metric_summary={})
        return section["criteria_markdown"], capture.views[0]["criteria"]

    def test_adapted_survey_writer_does_not_receive_another_draft_order(self):
        for name in ("survey", "survey_long"):
            for strategy in ("topic_specific_outline", "evidence_organized_outline"):
                with self.subTest(name=name, strategy=strategy):
                    _, template, memory, _, _ = self.objects(name, strategy)
                    guidance = drafting_template_guidance(template, memory)
                    self.assertIn("## Intended Use", guidance)
                    self.assertNotIn("Draft order:", guidance)
                    self.assertNotIn("## Abstract", guidance)

    def test_adapted_reviews_keep_facts_and_not_default_topology(self):
        for name, fact in (("survey", "Every paper-specific claim"),
                           ("survey_long", "Every paper-specific claim"),
                           ("experiment", "Reported numbers must match recorded metrics")):
            with self.subTest(name=name):
                config, template, memory, plan, draft = self.objects(name)
                before = template.model_dump(mode="json")
                for view in self.views(config, template, memory, plan, draft):
                    self.assertIn(fact, view)
                    self.assertNotIn("## Default Structure", view)
                    self.assertNotIn("The body should use survey-style sections", view)
                    self.assertNotIn("The section structure should cover", view)
                    self.assertNotIn("recognizable Abstract, Introduction, Method", view)
                    self.assertIn("## Output Expectations", view)
                self.assertEqual(template.model_dump(mode="json"), before)

    def test_unadapted_custom_and_legacy_criteria_remain_complete(self):
        config, template, memory, plan, draft = self.objects()
        custom = template.model_copy(update={"criteria_markdown":
            "## Required Checks\nMy fact checks.\n## Default Structure\nMy required custom arrangement.\n"})
        for view in self.views(config.model_copy(update={"criteria": "my-review.md"}), custom, memory, plan, draft):
            self.assertEqual(view, custom.criteria_markdown)
        for state in ({}, {"strategy": "topic_specific_outline", "status": "failed"},
                      {"strategy": "deterministic_outline_with_full_evidence_budget", "status": "adapted"}):
            unadapted = memory.model_copy(update={"outline_planning": state})
            self.assertEqual(drafting_template_guidance(template, unadapted), template.template_markdown)
            self.assertTrue(all(view == template.criteria_markdown
                for view in self.views(config, template, unadapted, plan, draft)))

        template = template.model_copy(update={"criteria_markdown": "Saved legacy instructions, including a fixed heading."})
        self.assertTrue(all(view == template.criteria_markdown
            for view in self.views(config, template, memory, plan, draft)))

    def test_custom_template_with_builtin_filename_is_not_a_builtin(self):
        config, template, memory, plan, draft = self.objects()
        config = config.model_copy(update={"template": "my-project/survey.md"})
        self.assertEqual(drafting_template_guidance(template, memory, config), template.template_markdown)
        self.assertTrue(all(view == template.criteria_markdown
            for view in self.views(config, template, memory, plan, draft)))
        unplanned = memory.model_copy(update={"document_plan": None,
            "survey_contract": {"enabled": True},
            "section_plan": [plan.model_copy(update={"section_id": f"section-{i}"}) for i in range(3)]})
        class NoCalls:
            def ask_json(self, *args, **kwargs):
                raise AssertionError("Custom topology must not be replaced by the built-in planner")
        result = _maybe_adapt_outline(client=NoCalls(), context=ReportContext(topic="Custom survey", report_mode="research_only"),
            template=template, memory=unplanned, config=config, emit=None)
        self.assertIs(result, unplanned)


if __name__ == "__main__":
    unittest.main()
