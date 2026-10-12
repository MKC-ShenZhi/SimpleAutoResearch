"""Narrative context follows adopted prose without additional model calls or state."""
import json
import unittest
from unittest.mock import patch

from simple_ar.integrations.llm import LLMResponseError
from simple_ar.report.agent import _writer_prompt, _writer_recovery_prompt, _reviewer_prompt, run_report_agent
from simple_ar.report.narrative import adopted_claims, narrative_context, report_objective, evidence_outline_context, _prompt_handle_view
from simple_ar.report.memory import initialize_report_memory
from simple_ar.report.schema import (
    ClaimEvidenceRecord, ReportContext, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportSectionReview, ReviewerFinding,
    SourceHandle,
)
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway


def payload(prompt):
    return json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]


class NarrativeTests(unittest.TestCase):
    def test_length_observation_uses_full_adopted_drafts_not_prompt_excerpts(self):
        plan = ReportSectionPlan(section_id="discussion", heading="Discussion",
                                 goal="Interpret the observed results.", target_words=100)
        draft = ReportSectionDraft(section_id="results", heading="Results",
                                   draft_markdown="observed " * 1500)
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="results", heading="Results", goal="Report observations.", target_words=200), plan,
        ])
        observation = narrative_context(memory, plan, [draft])["length_observation"]
        self.assertEqual(observation["adopted_token_count"], 1500)
        self.assertEqual(observation["planned_total_words"], 300)
        self.assertEqual(observation["this_section_target_words"], 100)
        self.assertIn("including tables", observation["counting_rule"])

    def test_supplied_material_results_are_not_restricted_to_local_experiment_artifacts(self):
        context = ReportContext(topic="Explain an existing draft", report_mode="supplied_materials")
        template = load_report_template_bundle(report_mode=context.report_mode,
            config=ReportRuntimeConfig(template="experiment"))
        memory = initialize_report_memory(context=context, template=template)
        results = next(section for section in memory.section_plan if section.heading == "Results")
        self.assertIn("Use supplied papers and materials", results.goal)
        self.assertNotIn("Use only recorded metrics and experiment artifacts", results.goal)

    def test_material_writing_does_not_starve_results_behind_many_papers(self):
        source = SourceHandle(handle="material:directory", kind="material", metadata={
            "source_directory": {"sections": [
                {"heading": "Methods", "section_id": "methods-1", "chunk_id": "chunk-1", "retained_chunks": 2},
                {"heading": "Methods", "section_id": "methods-2", "chunk_id": "chunk-2", "retained_chunks": 3}],
                "total_sections": 2, "truncated": False},
            "evidence_passages": [{"chunk_id": "chunk-1", "text": "first original"},
                                  {"chunk_id": "chunk-2", "text": "second original"}]})
        before = source.model_dump(mode="json")
        full = _prompt_handle_view(source)
        navigation = _prompt_handle_view(source, navigation=True)
        self.assertEqual(full["metadata"]["source_directory"]["headings"],
            ["Methods"])
        self.assertEqual(full["metadata"]["source_directory"]["total_sections"], 2)
        self.assertEqual(_prompt_handle_view(SourceHandle.model_validate(full))["metadata"]["source_directory"],
                         full["metadata"]["source_directory"])
        self.assertEqual(len(full["metadata"]["evidence_passages"]), 2)
        self.assertEqual(len(navigation["metadata"]["evidence_passages"]), 1)
        self.assertTrue(navigation["metadata"]["evidence_passages_truncated"])
        self.assertEqual(source.model_dump(mode="json"), before)
        handles = [SourceHandle(handle=f"paper:{i}", kind="paper", citation_key=f"P{i}") for i in range(10)]
        handles.extend([SourceHandle(handle="material:results", kind="material"),
                        SourceHandle(handle="material:old-draft", kind="prior_draft", title="Previous report"),
                        SourceHandle(handle="chunk:results", kind="chunk")])
        template = load_report_template_bundle(report_mode="supplied_materials",
                                               config=ReportRuntimeConfig(template="experiment"))
        for budget in (1, 2, 8, 0):
            with self.subTest(budget=budget):
                context = ReportContext(topic="Use existing results", report_mode="supplied_materials",
                                        source_handles=handles, max_section_sources=budget)
                memory = initialize_report_memory(context=context, template=template)
                for section in memory.section_plan:
                    self.assertIn("material:results", section.evidence_handles)
                    if budget != 1:
                        self.assertIn("paper:0", section.evidence_handles)
                    self.assertNotIn("chunk:results", section.evidence_handles)
                    self.assertNotIn("material:old-draft", section.evidence_handles)
                    if budget > 0:
                        self.assertLessEqual(len(section.evidence_handles), budget)
                view = evidence_outline_context(context, memory, ReportRuntimeConfig())
                self.assertNotIn('material:old-draft', view['evidence_handle_choices'])
                self.assertEqual(view['previous_document']['drafts'][0]['handle'], 'material:old-draft')

    def test_template_section_purposes_survive_planning_without_cross_section_leakage(self):
        context = ReportContext(topic="Existing observations", report_mode="supplied_materials")
        config = ReportRuntimeConfig(template="analysis_report")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config).model_copy(update={
            "template_markdown": "# Report\n## Writing Workflow\nDraft order: Results -> Conclusion\n"
                "## Results\nReport all observed values and limitations.\n"
                "## Conclusion\nDo not repeat the full numerical table; identify the next useful step.\n"
                "## References\nGenerated separately.\n"})
        memory = initialize_report_memory(context=context, template=template)
        self.assertEqual(len(memory.section_plan), 2)
        results, conclusion = memory.section_plan
        self.assertIn("Report all observed", results.goal)
        self.assertNotIn("Do not repeat", results.goal)
        self.assertIn("Do not repeat the full numerical table", conclusion.goal)
        view = narrative_context(memory, conclusion, [])
        self.assertIn("identify the next useful step", view["section_purpose"])
        self.assertNotIn("Generated separately", json.dumps(view))

    def test_long_template_guidance_reports_clipping_without_new_state(self):
        context = ReportContext(topic="Bounded", report_mode="supplied_materials")
        template = load_report_template_bundle(report_mode=context.report_mode,
            config=ReportRuntimeConfig(template="analysis_report")).model_copy(update={
                "template_markdown": "## 方法\n" + "界" * 1000})
        memory = initialize_report_memory(context=context, template=template)
        self.assertIn("200 guidance characters omitted", memory.section_plan[0].goal)

    def test_actual_prose_without_optional_claims_is_available_and_not_certified(self):
        plan = ReportSectionPlan(section_id="discussion", heading="Discussion", goal="Interpret uncertainty")
        draft = ReportSectionDraft(section_id="results", heading="Results",
                                   draft_markdown="Observed loss was 0.2; one run only.")
        context = narrative_context(ReportMemory(section_plan=[plan]), plan, [draft])
        row = context["adopted_sections"][0]
        self.assertEqual(row["prose_windows"][0]["text"], draft.draft_markdown)
        self.assertEqual(row["prose_characters_omitted"], 0)
        self.assertEqual(row["declared_claims"], [])
        self.assertIn("not_independently_verified", row["support_status"])

    def test_frozen_responsibilities_have_one_prompt_owner_including_format_retry(self):
        from simple_ar.report.agent import _writer_prompt, _writer_recovery_prompt, _reviewer_prompt
        from simple_ar.report.schema import ReportDocumentPlan
        goal = "Explain the precise recorded scope and qualifications. " * 20
        plans = [ReportSectionPlan(section_id='setup', heading='Setup', goal=goal),
                 ReportSectionPlan(section_id='end', heading='Conclusion', goal='Interpret the setup')]
        memory = ReportMemory(section_plan=plans, document_plan=ReportDocumentPlan(sections=plans))
        context = ReportContext(topic='Shared evidence', report_mode='supplied_materials')
        config = ReportRuntimeConfig()
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        adopted = [ReportSectionDraft(section_id='setup', heading='Setup', draft_markdown='One measured run only.')]
        draft = ReportSectionDraft(section_id='end', heading='Conclusion', draft_markdown='Limited conclusion.')
        common = dict(context=context, memory=memory, section=plans[1], adopted_sections=adopted)
        prompts = [
            _writer_prompt(**common, template=template, config=config, extra_context=[], previous_draft=None,
                           review=None, source_batch_index=1, source_batch_count=1,
                           include_previous_draft=True, draft_mode='section'),
            _writer_recovery_prompt(**common, config=config, previous_draft=None, review=None, draft_mode='section'),
            _reviewer_prompt(**common, template=template, draft=draft),
        ]
        for prompt in prompts[:2]:
            payload = json.loads(prompt[prompt.index('{'):])
            self.assertEqual(payload['document_plan']['sections'][0]['goal'], goal)
            view = payload['narrative_context']
            self.assertEqual(view['responsibilities_source'], 'document_plan.sections')
            self.assertNotIn('section_responsibilities', view)
            row = view['adopted_sections'][0]
            self.assertEqual(row['purpose_section_id'], 'setup')
            self.assertNotIn('purpose', row)
            self.assertEqual(row['prose_windows'][0]['text'], adopted[0].draft_markdown)
            self.assertEqual(prompt.count(goal), 1)
        reviewed = json.loads(prompts[2][prompts[2].index('{'):])
        self.assertNotIn('sections', reviewed['document_plan'])
        self.assertNotIn('goal', reviewed['section'])
        self.assertEqual(prompts[2].count(goal), 0)
        view = reviewed['narrative_context']
        self.assertEqual(view['section_responsibilities'][0]['section_id'], 'setup')
        self.assertNotIn('purpose', view['section_responsibilities'][0])
        self.assertEqual(view['adopted_sections'][0]['prose_windows'][0]['text'], adopted[0].draft_markdown)
        self.assertNotIn('purpose', view['adopted_sections'][0])

    def test_long_unicode_prose_keeps_qualification_and_explicit_coverage(self):
        text = "观测" * 10000 + "NOT independently reproduced."
        plan = ReportSectionPlan(section_id="end", heading="End", goal="Summarize")
        draft = ReportSectionDraft(section_id="method", heading="Method", draft_markdown=text)
        row = narrative_context(ReportMemory(), plan, [draft])["adopted_sections"][0]
        self.assertTrue(row["prose_windows"][-1]["text"].endswith("NOT independently reproduced."))
        self.assertEqual(row["prose_characters_omitted"], len(text) - 12000)
        for window in row["prose_windows"]:
            self.assertEqual(window["text"], text[window["start"]:window["end"]])

    def test_short_document_preserves_middle_and_shares_unused_window(self):
        plan = ReportSectionPlan(section_id='next', heading='Discussion', goal='Interpret without repeating results')
        drafts = [ReportSectionDraft(section_id='short', heading='Short', draft_markdown='S' * 40),
                  ReportSectionDraft(section_id='results', heading='Results',
                    draft_markdown='H' * 900 + 'Actual middle trade-off, not a missing observation.' + 'T' * 1400)]
        original = [row.model_dump() for row in drafts]
        view = narrative_context(ReportMemory(), plan, drafts)
        self.assertEqual([row['prose_characters_omitted'] for row in view['adopted_sections']], [0, 0])
        self.assertIn('Actual middle trade-off', view['adopted_sections'][1]['prose_windows'][0]['text'])
        self.assertEqual([row.model_dump() for row in drafts], original)
        # A larger manuscript keeps every section and shares a bounded total;
        # short sections release their allowance instead of wasting it.
        long = drafts + [ReportSectionDraft(section_id='long', heading='Long', draft_markdown='L' * 30000)]
        projected = narrative_context(ReportMemory(), plan, long)['adopted_sections']
        self.assertEqual(sum(len(window['text']) for row in projected for window in row['prose_windows']), 12000)
        self.assertEqual(projected[0]['prose_characters_omitted'], 0)
        self.assertEqual(projected[1]['prose_characters_omitted'], 0)
        self.assertGreater(projected[2]['prose_characters_omitted'], 0)

    def test_current_section_is_not_its_own_evidence_and_large_plans_show_omissions(self):
        plans = [ReportSectionPlan(section_id=str(i), heading=str(i), goal="Explain") for i in range(30)]
        drafts = [ReportSectionDraft(section_id=str(i), heading=str(i), draft_markdown=str(i)) for i in range(30)]
        view = narrative_context(ReportMemory(section_plan=plans), plans[-1], drafts)
        self.assertEqual(view["responsibilities_omitted"], 6)
        self.assertEqual(view["adopted_sections_omitted"], 17)
        self.assertNotIn("29", [row["section_id"] for row in view["adopted_sections"]])

    def test_claim_projection_preserves_duplicate_ids_and_removes_superseded_records(self):
        guard = ClaimEvidenceRecord(claim_id="guard", claim="Input guard")
        original = ReportSectionDraft(section_id="a", heading="A", claims=[
            ClaimEvidenceRecord(claim_id="same", claim="Old unsupported assertion", status="unsupported")])
        other = ReportSectionDraft(section_id="b", heading="B", claims=[
            ClaimEvidenceRecord(claim_id="same", claim="Different section assertion")])
        replacement = original.model_copy(update={"claims": []})
        rows = adopted_claims([guard], [replacement, other])
        self.assertEqual([row.claim for row in rows], ["Input guard", "Different section assertion"])
        self.assertEqual(len(adopted_claims([], [original, other])), 2)

    def test_writer_review_format_retry_and_resume_consume_same_adopted_prose(self):
        context = ReportContext(topic="Observed results", report_mode="supplied_materials")
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="body", heading="Results", goal="State observations", final_order=1),
            ReportSectionPlan(section_id="summary", heading="Conclusion", goal="Bound conclusions", final_order=2),
        ])
        prompts = {}
        labels = []
        checkpoints = []

        class Client:
            def ask_json(self, system, user, *, label="", **kwargs):
                labels.append(label)
                payload = json.loads(user[user.index("{"):])
                prompts[label] = payload
                sid = payload["section"]["section_id"]
                if "reviewer" in label:
                    return {"section_id": sid, "verdict": "pass"}
                if label == "report-writer-summary":
                    raise LLMResponseError("Invalid outer response")
                return {"section_id": sid, "heading": sid,
                        "draft_markdown": "Only one measured run; improvement is unconfirmed."}

        config = ReportRuntimeConfig(max_review_iterations=0)
        kwargs = dict(client=Client(), context=context, memory=memory, config=config,
                      template=load_report_template_bundle(report_mode="supplied_materials", config=config),
                      gateway=ReportToolGateway(context))
        result = run_report_agent(**kwargs, checkpoint_sink=checkpoints.append)
        for label in ("report-writer-summary", "report-writer-summary-retry", "report-reviewer-summary"):
            rows = prompts[label]["narrative_context"]["adopted_sections"]
            self.assertEqual(rows[0]["section_id"], "body")
            self.assertIn("improvement is unconfirmed", rows[0]["prose_windows"][0]["text"])
        self.assertNotIn("section_digests", checkpoints[-1]["memory"])
        count = len(labels)
        resumed = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(resumed.report_body, result.report_body)
        self.assertEqual(len(labels), count)
        partial = next(row for row in checkpoints if len(row["sections"]) == 1 and row["pending_draft"] is None)
        partial["memory"]["claims_evidence_matrix"] = [
            ClaimEvidenceRecord(claim_id="obsolete", claim="Obsolete superseded assertion").model_dump(mode="json")]
        run_report_agent(**kwargs, completed_checkpoint=partial)
        self.assertNotIn("Obsolete superseded assertion", " ".join(prompts["report-writer-summary"]["prior_claim_notes"]))
        self.assertEqual(labels.count("report-writer-body"), 1)


class ReportObjectiveTests(unittest.TestCase):
    def test_section_metric_evidence_follows_argument_and_draft_not_heading(self):
        import simple_ar.report.agent as agent
        from simple_ar.report.schema import (
            MetricSource, ReportArgumentPlan, ReportArgumentPoint, ReportDocumentPlan,
        )
        context, config, template, memory, section, draft = self.objects()
        memory.metric_sources = [
            MetricSource(metric_id=key, name=name, value=value, artifact="results.json", label=label)
            for key, name, value, label in [
                ("mean", "accuracy.delta_mean", .1, "paired_summary:0"),
                ("seed", "accuracy", .7, "candidate:seed=0"),
                ("task", "accuracy_after_task_1", .6, "candidate:seed=0"),
                ("other", "accuracy", .5, "baseline:seed=0"),
            ]
        ]
        memory.document_plan = ReportDocumentPlan(sections=[section], argument_plan=ReportArgumentPlan(
            question="How did accuracy change?", answer="One recorded comparison.", points=[
                ReportArgumentPoint(claim="Interpret the seed", section_id=section.section_id,
                                    metric_ids=["seed", "not-registered"]),
                ReportArgumentPoint(claim="Other comparison", section_id="elsewhere", metric_ids=["other"]),
            ]))
        draft.metric_ids = ["task"]
        before = memory.model_dump()
        writer_args = dict(context=context, memory=memory, config=config, extra_context=[],
                           previous_draft=None, review=None, adopted_sections=[])
        for heading in ("Results", "实验观察", "Evidence and trade-offs"):
            section.heading = heading
            writer = agent._writer_task_context(**writer_args, section=section)
            reviewer = agent._reviewer_context(context=context, memory=memory, section=section,
                config=config, template=template, draft=draft)
            self.assertEqual([row[0] for row in writer["metric_sources"]["rows"]], ["mean", "seed"])
            self.assertEqual([row[0] for row in reviewer["metric_sources"]["rows"]], ["mean", "seed", "task"])
        # Projection does not rewrite the saved evidence or accepted argument.
        section.heading = before["document_plan"]["sections"][0]["heading"]
        self.assertEqual(memory.model_dump(), before)
        joint = agent._writer_task_context(**writer_args, section=None, document_sections=[section])
        self.assertEqual([row[0] for row in joint["metric_sources"]["rows"]], ["mean", "seed", "other"])
        memory.document_plan = None
        legacy = agent._writer_task_context(**writer_args, section=section)
        self.assertEqual(legacy["metric_sources"], joint["metric_sources"])

    def test_section_review_policy_and_schema_use_one_read_tool_registry(self):
        import simple_ar.report.agent as agent
        from simple_ar.report.tools import report_tool_specs
        context, config, template, memory, section, draft = self.objects()
        read = report_tool_specs()[0].model_copy(update={"name": "another_registered_read"})
        write = read.model_copy(update={"name": "not_a_read", "permissions": ["write"]})
        with patch.object(agent, "report_tool_specs", return_value=[read, write]):
            view = agent._reviewer_context(context=context, template=template, memory=memory,
                section=section, draft=draft, config=config)
        self.assertEqual(view["tool_policy"]["allowed_tools"], [read.name])
        self.assertEqual(view["context_tools"], [{"name": read.name,
            "description": read.description, "input_schema": read.input_schema}])

    def test_normal_and_format_retry_have_one_task_context_owner(self):
        import simple_ar.report.agent as agent
        from simple_ar.report.schema import ReportToolResult
        context, config, template, memory, section, draft = self.objects()
        context.source_comparisons = [{"dimension": "Matched conditions", "relation": "conditional_difference",
            "conditions": ["Subgroup results do not imply full-dataset superiority."]}]
        before = memory.model_dump(mode="json")
        extra = [ReportToolResult(tool_name="search_source_chunks", content={"text": f"passage {i}"})
                 for i in range(9)]
        common = dict(context=context, memory=memory, section=section, config=config,
            extra_context=extra, previous_draft=draft, review=None, adopted_sections=[draft])
        normal = payload(agent._writer_prompt(**common, template=template,
            source_batch_index=1, source_batch_count=1, include_previous_draft=True, draft_mode="section"))
        recovery = payload(agent._writer_recovery_prompt(**common, draft_mode="section"))
        for key in ("objective", "document_plan", "source_comparisons", "comparison_status", "assembly_owned_content", "narrative_context", "section_constraints",
                    "length_requirement", "visual_requirements", "source_handles", "metric_sources",
                    "extra_tool_context", "extra_tool_context_omitted", "review_findings",
                    "review_findings_status", "review_instructions", "revision_preservation_requirement"):
            self.assertEqual(normal[key], recovery[key], key)
        self.assertEqual(normal["extra_tool_context_omitted"], 0)
        self.assertEqual(len(normal["extra_tool_context"]), len(extra))
        self.assertEqual(normal['source_comparisons'], context.source_comparisons)
        self.assertNotIn('source_comparisons', normal['narrative_context'])
        self.assertNotIn("template_markdown", recovery)
        self.assertNotIn("global_research_context", recovery)
        self.assertEqual(memory.model_dump(mode="json"), before)

        source = SourceHandle(handle='material:comparison', kind='material', title='Original comparison',
            metadata={'evidence_passages': [{'chunk_id': 'c1', 'text': 'Same conditions, recorded value 3.'}]})
        memory.source_handles = [source]
        section.evidence_handles = [source.handle]
        duplicate = ReportToolResult(tool_name='get_paper_brief',
            content={'handles': [source.model_dump(mode='json')]})
        changed = duplicate.model_copy(deep=True)
        changed.content['handles'][0]['metadata']['evidence_passages'][0]['text'] = 'New comparison condition.'
        original = duplicate.model_dump(mode='json')
        view = agent._writer_task_context(**{**common, 'extra_context': [duplicate, changed]})
        self.assertEqual(view['extra_tool_context'][0]['content']['handles'],
            [{'handle': source.handle, 'evidence_reference': 'source_handles'}])
        self.assertEqual(view['extra_tool_context'][1]['content']['handles'][0]['metadata']['evidence_passages'][0]['text'],
            'New comparison condition.')
        reviewed = agent._reviewer_context(context=context, template=template, memory=memory,
            section=section, draft=draft, config=config, extra_context=[duplicate])
        self.assertEqual(reviewed['extra_tool_context'][0]['content']['handles'][0]['evidence_reference'], 'allowed_sources')
        self.assertEqual(duplicate.model_dump(mode='json'), original)

    def objects(self):
        # Requirements in the middle and tail must not depend on an excerpt.
        request = ("材料与问题。" * 300 + " Keep all conditions distinct. "
                   + "约束和解释。" * 500 + " Deliver editable sources; do not claim significance.")
        context = ReportContext(topic="A bounded report", report_mode="supplied_materials",
                                problem_markdown=request, goal_markdown=request)
        config = ReportRuntimeConfig(template="analysis_report")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        memory = initialize_report_memory(context=context, template=template)
        section = memory.section_plan[0]
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading,
                                   draft_markdown="Recorded observations only.")
        return context, config, template, memory, section, draft

    def test_original_request_not_summary_is_shared_by_planner_writer_retry_reviewer(self):
        context, config, template, memory, section, draft = self.objects()
        self.assertEqual(len(memory.objective), 1200)
        review = ReportSectionReview(section_id=section.section_id, verdict="revise_required",
            findings=[ReviewerFinding(finding_id="f1", type="missing_limitation", severity="minor",
                required_action="revise", message="Keep the declared limits.")],
            revision_instructions=["Do not omit the requested conditions."])
        common = dict(context=context, memory=memory, section=section)
        prompts = [
            _writer_prompt(**common, template=template, config=config, extra_context=[],
                previous_draft=draft, review=review, source_batch_index=1, source_batch_count=1,
                include_previous_draft=True, draft_mode="section_revision"),
            _writer_recovery_prompt(**common, config=config, previous_draft=draft,
                review=review, draft_mode="section_revision"),
            _reviewer_prompt(**common, template=template, draft=draft,
                revision_review=review, previous_draft=draft),
        ]
        for prompt in prompts:
            self.assertEqual(payload(prompt)["objective"], context.problem_markdown)
            self.assertEqual(prompt.count("Deliver editable sources"), 1)
            self.assertIn("recommended method's own applicability", prompt)
        global_context = payload(prompts[0])["global_research_context"]
        self.assertEqual(global_context["derived_context_status"]["independent_verification"], "not_performed")
        outline = evidence_outline_context(context, memory, config)
        self.assertEqual(outline["objective"]["text"], context.problem_markdown)
        self.assertFalse(outline["objective"]["truncated"])
        self.assertEqual(outline["synthesis_status"]["evidence_role"], "recorded_derived_context")
        self.assertNotIn("problem", outline)  # No second truncated request owner.

    def test_distinct_problem_and_goal_are_preserved_without_source_confusion(self):
        context, _, _, memory, _, _ = self.objects()
        context.goal_markdown = "Goal-only requirement: report the missing input instead of inventing it."
        objective = report_objective(context, memory)
        self.assertIn(context.goal_markdown, objective)
        self.assertIn(context.problem_markdown, objective)
        context.problem_markdown = ""
        self.assertEqual(report_objective(context, memory), context.goal_markdown)

    def test_legacy_context_uses_recorded_objective_without_mutation(self):
        context = ReportContext(topic="Legacy title", report_mode="supplied_materials")
        memory = ReportMemory(objective="Historical request." * 300)
        before = memory.model_dump()
        self.assertEqual(report_objective(context, memory), memory.objective)
        self.assertEqual(memory.model_dump(), before)
        memory.objective = ""
        self.assertEqual(report_objective(context, memory), context.topic)

    def test_whole_document_review_and_completed_restore_keep_full_request(self):
        context, config, template, memory, section, _ = self.objects()
        memory.section_plan = [section, ReportSectionPlan(section_id="end", heading="Limitations",
                                                         goal="Bound the observations.")]
        config = config.model_copy(update={"document_review": True, "max_review_iterations": 0})
        seen, calls, checkpoints = {}, [], []

        class Client:
            def ask_json(self, system, prompt, *, label="", **kwargs):
                calls.append(label)
                seen[label] = payload(prompt)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                planned = seen[label]["section"]
                if "reviewer" in label:
                    return {"section_id": planned["section_id"], "verdict": "pass"}
                return {"section_id": planned["section_id"], "heading": planned["heading"],
                        "draft_markdown": "Recorded observations only."}

        kwargs = dict(client=Client(), context=context, config=config, template=template,
                      memory=memory, gateway=ReportToolGateway(context))
        result = run_report_agent(**kwargs, checkpoint_sink=checkpoints.append)
        self.assertEqual(seen["report-document-reviewer"]["objective"], context.problem_markdown)
        self.assertEqual(seen["report-document-reviewer"]["length_observation"]["total_markdown_tokens"], 6)
        count = len(calls)
        restored = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(restored.report_body, result.report_body)
        self.assertEqual(len(calls), count)

if __name__ == "__main__":
    unittest.main()
