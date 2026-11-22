"""Evidence-organized documents reuse the existing report plan and lifecycle."""
from __future__ import annotations

import unittest
import json
from unittest.mock import Mock

from simple_ar.integrations.llm import LLMError, LLMResponseError, LLMDeadlineError
from simple_ar.report.agent import (
    _evidence_outline_sections, _maybe_adapt_outline, _resolve_document_plan,
    _validated_outline_delivery, _writer_prompt, run_report_agent,
)
from simple_ar.report.document_plan import outline_section_keys, resolve_argument_plan
from simple_ar.report.editor import document_revision_limit
from simple_ar.report.narrative import evidence_outline_context, narrative_context, outline_evidence_labels, map_evidence_labels
from simple_ar.report.schema import (
    MetricSource, ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionPlan, SourceHandle,
)
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.research.synthesis import SynthesisResult, _parse_evidence_comparisons


class EvidenceOutlineTests(unittest.TestCase):
    def setUp(self):
        self.context = ReportContext(topic="Observed trade-off", report_mode="experiment",
            results={"metrics": {"coverage": 0.9}},
            experiment_plan={"resource_budget": {"device": "requested_gpu"}})
        self.memory = ReportMemory(template="reproduction", report_mode="experiment",
            objective="Explain this finite reproduction, not a new algorithm.",
            source_handles=[SourceHandle(handle="run:1", kind="experiment", title="Measured run")],
            metric_sources=[MetricSource(metric_id="m:1", name="coverage", value=0.9,
                artifact="results.json", source_kind="measurement")],
            section_plan=[ReportSectionPlan(section_id="findings", heading="Findings",
                goal="Explain observed measurements.", evidence_handles=["run:1"], target_words=500)],
            limitations=["Only one selected condition was measured."])
        self.response = {"sections": [
            {"heading": "Scope", "goal": "Name the limited claim.", "evidence_handles": []},
            {"heading": "Coverage and cost", "goal": "Explain the measured trade-off; no significance claim.",
             "evidence_handles": ["run:1"], "target_words": 300},
        ]}

    def plan(self, response=None, config=None):
        return _evidence_outline_sections(response or self.response, memory=self.memory,
            config=config or ReportRuntimeConfig())

    def adapt(self, client, config):
        template = load_report_template_bundle(report_mode="experiment", config=config)
        return _maybe_adapt_outline(client=client, context=self.context, memory=self.memory,
            template=template, config=config, emit=None)

    def test_non_survey_auto_and_template_make_no_extra_calls(self):
        for strategy in ("auto", "template"):
            client = Mock()
            result = self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy=strategy))
            self.assertIs(result, self.memory)
            client.ask_json.assert_not_called()

    def test_source_survey_auto_plans_without_a_legacy_survey_contract(self):
        from simple_ar.report.agent import _resolve_document_plan
        task = "Compare the two supplied sources in 350-500 words."
        context = self.context.model_copy(update={"report_mode": "research_only", "problem_markdown": task})
        memory = self.memory.model_copy(update={"template": "survey", "survey_contract": {},
            "section_plan": [ReportSectionPlan(section_id=str(i), heading=f"Fallback {i}", goal="Compare sources.")
                             for i in range(3)]})
        client = Mock()
        client.ask_json.return_value = {**self.response, "title": "Contrasting reliability objectives",
            "length_request": {"unit": "words", "scope": "whole_document", "request_quote": task,
                "constraint": "bounds", "min_words": 350, "max_words": 500, "target_words": 425}}
        config = ReportRuntimeConfig(template="survey")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        planned = _maybe_adapt_outline(client=client, context=context, memory=memory,
                                       template=template, config=config, emit=None)
        frozen = _resolve_document_plan(planned, config=config, context=context)
        self.assertEqual(frozen.document_plan.title, "Contrasting reliability objectives")
        self.assertEqual(frozen.document_plan.target_words, 425)
        self.assertEqual(len(frozen.section_plan), 2)
        self.assertIs(_maybe_adapt_outline(client=client, context=context, memory=frozen,
            template=template, config=config, emit=None), frozen)
        client.ask_json.assert_called_once()

    def test_frozen_evidence_plan_does_not_reintroduce_template_topology(self):
        from simple_ar.report.agent import _resolve_document_plan
        from simple_ar.report.templates import drafting_template_guidance
        config = ReportRuntimeConfig(template='reproduction', outline_strategy='adaptive')
        template = load_report_template_bundle(report_mode='experiment', config=config)
        client = Mock()
        client.ask_json.return_value = map_evidence_labels(self.response, {"run:1": "source_1"})
        frozen = _resolve_document_plan(self.adapt(client, config), config=config)
        prompt = client.ask_json.call_args.args[1]
        prompt_payload = json.loads(prompt[prompt.index('{'):])
        self.assertEqual(prompt_payload["evidence_handle_choices"], ["source_1"])
        self.assertEqual(frozen.section_plan[1].evidence_handles, ["run:1"])
        guidance = drafting_template_guidance(template, frozen)
        self.assertIn('Describe the declared reproduction scope', guidance)
        self.assertIn('## Writing Principles', guidance)
        self.assertIn('Compare source and local conditions', guidance)
        self.assertNotIn('## Ablation Matrix', guidance)
        self.assertNotIn('Draft order:', guidance)
        self.assertEqual(frozen.document_plan.sections[1].heading, 'Coverage and cost')
        from simple_ar.report.agent import _writer_prompt
        prompt = _writer_prompt(context=self.context, template=template, memory=frozen,
            section=frozen.section_plan[1], config=config, extra_context=[], previous_draft=None,
            review=None, source_batch_index=1, source_batch_count=1, include_previous_draft=False,
            draft_mode='initial')
        payload = json.loads(prompt[prompt.find('{'):])
        self.assertEqual(payload['template_markdown'], guidance)
        self.assertNotIn('Writing Principles', [section.heading for section in self.memory.section_plan])
        self.assertEqual(payload['document_plan']['sections'][1]['heading'], 'Coverage and cost')
        restored = ReportMemory.model_validate(frozen.model_dump(mode='json'))
        self.assertEqual(drafting_template_guidance(template, restored), guidance)
        self.assertEqual(drafting_template_guidance(template, self.memory), template.template_markdown)
        custom = template.model_copy(update={'name': 'custom', 'template_markdown': '## Custom responsibilities'})
        self.assertEqual(drafting_template_guidance(custom, frozen), custom.template_markdown)
        for strategy in ('deterministic_outline_with_full_evidence_budget',):
            kept = frozen.model_copy(update={'outline_planning': {'strategy': strategy, 'status': 'adapted'}})
            self.assertEqual(drafting_template_guidance(template, kept), template.template_markdown)

    def test_explicit_adaptive_reuses_existing_plan(self):
        client = Mock()
        client.ask_json.return_value = self.response
        result = self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
        self.assertEqual(len(result.section_plan), 2)
        self.assertEqual(result.outline_planning["strategy"], "evidence_organized_outline")
        self.assertEqual(result.outline_planning["attempts"], 1)
        self.assertEqual(result.metric_sources, self.memory.metric_sources)
        self.assertEqual(result.limitations, self.memory.limitations)

    def test_title_is_resolved_with_outline_without_an_extra_call(self):
        from simple_ar.report.agent import _resolve_document_plan
        self.response["title"] = "Observed coverage and region-size trade-offs"
        client = Mock()
        client.ask_json.return_value = self.response
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        result = _resolve_document_plan(self.adapt(client, config), config=config)
        self.assertEqual(result.document_plan.title, self.response["title"])
        self.assertEqual(client.ask_json.call_count, 1)

    def test_supplied_figure_ownership_is_validated_and_frozen_in_same_plan(self):
        from simple_ar.report.agent import _resolve_document_plan
        from simple_ar.report.document_plan import visual_plan_for_renderer, visual_requirements
        self.memory.source_handles[0].metadata = {"document_id": "data-1"}
        self.context.source_handles = self.memory.source_handles
        self.context.results["supplied_analyses"] = [{"document_id": "data-1", "evidence_role": "recomputed_from_user_supplied_data",
            "spec": {}, "records": [], "figures": [{"path": "figures/chart.svg"}]}]
        self.response["sections"][0]["evidence_handles"] = ["run:1"]
        self.response["visual_intents"] = [{"kind": "figure", "view": "supplied-data",
            "section_heading": "Coverage and cost", "evidence_handles": ["run:1"],
            "figure_paths": ["figures/chart.svg"],
            "title": "Observed comparison", "purpose": "Present the supplied coordinates"}]
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        client = Mock()
        client.ask_json.return_value = self.response
        frozen = _resolve_document_plan(self.adapt(client, config), config=config, context=self.context)
        intent = frozen.document_plan.visual_intents[0]
        self.assertEqual(intent.figure_paths, ["figures/chart.svg"])
        self.assertEqual(intent.section_id, frozen.document_plan.sections[1].section_id)
        self.assertEqual(visual_plan_for_renderer(frozen.document_plan), [])
        self.assertTrue(visual_requirements(frozen.document_plan, frozen.document_plan.sections[1])["figures"][0]["assembly_owned"])
        self.assertEqual(client.ask_json.call_count, 1)
        self.assertIs(_resolve_document_plan(frozen, config=config, context=ReportContext(topic="Moved", report_mode="supplied_materials")), frozen)
        # Omitted/null chart selection in a new outline means links only,
        # whereas an already frozen legacy intent keeps its original value.
        for missing in (True, False):
            with self.subTest(missing=missing):
                if missing:
                    self.response["visual_intents"][0].pop("figure_paths", None)
                else:
                    self.response["visual_intents"][0]["figure_paths"] = None
                planned = _resolve_document_plan(self.adapt(client, config), config=config, context=self.context)
                self.assertEqual(planned.document_plan.visual_intents[0].figure_paths, [])
        calls = client.ask_json.call_count
        self.memory = frozen
        self.assertIs(self.adapt(client, config), frozen)
        self.assertEqual(client.ask_json.call_count, calls)

    def test_invalid_supplied_visual_owner_uses_only_existing_one_correction(self):
        for visual in (
            {"kind": "figure", "view": "supplied-data", "section_heading": "Coverage and cost",
             "title": "Unregistered", "purpose": "No actual figures", "evidence_handles": ["run:1"]},
            "not an intent",
        ):
            client = Mock()
            client.ask_json.return_value = {**self.response, "visual_intents": [visual]}
            with self.assertRaises(LLMError):
                self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
            self.assertEqual(client.ask_json.call_count, 2)

    def test_unregistered_inline_figure_uses_existing_outline_correction(self):
        self.memory.source_handles[0].metadata = {"document_id": "data-1"}
        self.context.source_handles = self.memory.source_handles
        self.context.results["supplied_analyses"] = [{"document_id": "data-1",
            "evidence_role": "recomputed_from_user_supplied_data", "spec": {}, "records": [],
            "figures": [{"path": "figures/chart.svg"}]}]
        self.response["sections"][0]["evidence_handles"] = ["run:1"]
        self.response["visual_intents"] = [{"kind": "figure", "view": "supplied-data",
            "section_heading": "Coverage and cost", "evidence_handles": ["run:1"],
            "title": "Comparison", "purpose": "Interpret values", "figure_paths": ["invented.svg"]}]
        client = Mock()
        client.ask_json.return_value = self.response
        with self.assertRaises(LLMError):
            self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
        self.assertEqual(client.ask_json.call_count, 2)

    def test_invalid_title_uses_the_existing_bounded_retry_contract(self):
        for title in (["not text"], "# heading", "one\ntwo", "x" * 241):
            with self.subTest(title=title):
                client = Mock()
                client.ask_json.return_value = {**self.response, "title": title}
                with self.assertRaises(LLMError):
                    self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
                self.assertEqual(client.ask_json.call_count, 2)

    def test_no_survey_citation_quota_or_subsection_filler(self):
        sections = self.plan()
        self.assertEqual(sections[1].evidence_handles, ["run:1"])
        self.assertEqual(sections[1].target_words, 300)
        self.assertEqual(sections[1].min_citations, 0)
        self.assertEqual(sections[1].subsections, [])

    def test_unknown_handle_is_not_silently_removed(self):
        self.response["sections"][1]["evidence_handles"] = ["invented:trial"]
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.plan()

    def test_invalid_handles_get_only_one_correction(self):
        client = Mock()
        client.ask_json.return_value = {"sections": [{"heading": "Bad", "goal": "Bad"}]}
        with self.assertRaises(LLMError):
            self.adapt(client, ReportRuntimeConfig(template="analysis_report", outline_strategy="adaptive"))
        self.assertEqual(client.ask_json.call_count, 2)
        # A provider failure has no outline to correct. Keep the same retry
        # ownership as Writer/Reviewer, but still repair malformed responses.
        for error in (LLMError("provider unavailable"), LLMError("budget exhausted"),
                      LLMDeadlineError("wall-clock deadline"), LLMResponseError("invalid JSON")):
            with self.subTest(error=type(error).__name__):
                client = Mock()
                client.ask_json.side_effect = error
                with self.assertRaises(LLMError):
                    self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
                self.assertEqual(client.ask_json.call_count, 2 if isinstance(error, LLMResponseError) else 1)

    def test_explicit_fallback_preserves_template_evidence(self):
        client = Mock()
        client.ask_json.return_value = {}
        result = self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive",
            allow_llm_fallback=True))
        self.assertEqual(result.section_plan, self.memory.section_plan)
        self.assertEqual(result.outline_planning["status"], "fallback")
        self.assertEqual(client.ask_json.call_count, 2)

    def test_invalid_structure_and_reference_section_rejected(self):
        for raw in ([{}], [self.response["sections"][0], {}],
                    [self.response["sections"][0], {"heading": "References", "goal": "Bibliography"}]):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                self.plan({"sections": raw})

    def test_explicit_source_budget_not_silently_truncated(self):
        self.memory.source_handles.append(SourceHandle(handle="paper:2", kind="paper"))
        self.response["sections"][1]["evidence_handles"] = ["run:1", "paper:2"]
        with self.assertRaisesRegex(ValueError, "source limit"):
            self.plan(config=ReportRuntimeConfig(max_section_sources=1))

    def test_no_metrics_does_not_create_result_or_novelty(self):
        self.memory.metric_sources = []
        self.context.results = {}
        payload = evidence_outline_context(self.context, self.memory, ReportRuntimeConfig())
        self.assertEqual(payload["recorded_metrics"]["rows"], [])
        self.assertEqual(payload["results"], {})
        self.assertIn("Only one selected condition", payload["limits"][0])
        self.assertIn("do not invent novelty", " ".join(payload["planning_rules"]))

    def test_projection_keeps_declared_conditions_distinct(self):
        payload = evidence_outline_context(self.context, self.memory, ReportRuntimeConfig())
        self.assertEqual(payload["execution_evidence"]["execution_records"], [])
        self.assertEqual(payload["execution_evidence"]["declared_protocol"]["resource_budget"]["device"], "requested_gpu")

    def test_projection_reports_shortened_inputs(self):
        self.context.problem_markdown = self.context.goal_markdown = ""
        self.memory.objective = "x" * 4000
        self.memory.source_handles.extend(SourceHandle(handle=f"paper:{i}", kind="paper") for i in range(50))
        payload = evidence_outline_context(self.context, self.memory, ReportRuntimeConfig())
        self.assertFalse(payload["objective"]["truncated"])
        self.assertEqual(payload["objective"]["text"], self.memory.objective)
        self.assertEqual(payload["objective"]["total_characters"], 4000)
        self.assertEqual(payload["sources_omitted"], 11)
        canonical = "material:local-d71f11fe6edf246dfe9e"
        self.memory.source_handles.append(SourceHandle(handle=canonical, kind="material"))
        self.memory.source_handles.append(SourceHandle(handle="source_1", kind="material"))
        payload["input_claims"] = [{"claim": canonical, "evidence_handles": [canonical]}]
        projected, labels = outline_evidence_labels(payload, self.memory.source_handles)
        alias = next(label for label, handle in labels.items() if handle == canonical)
        self.assertNotEqual(alias, "source_1")
        self.assertEqual(projected["input_claims"][0]["evidence_handles"], [alias])
        self.assertEqual(projected["input_claims"][0]["claim"], canonical)
        response = {"sections": [{"evidence_handles": [alias]}],
            "argument_plan": {"counterevidence_handles": [alias]},
            "context_requests": [{"arguments": {"handle": alias, "query": alias}}],
            "visual_intents": [{"evidence_handles": [alias]}]}
        decoded = map_evidence_labels(response, labels)
        self.assertEqual(decoded["sections"][0]["evidence_handles"], [canonical])
        self.assertEqual(decoded["argument_plan"]["counterevidence_handles"], [canonical])
        self.assertEqual(decoded["context_requests"][0]["arguments"], {"handle": canonical, "query": alias})
        self.assertEqual(decoded["visual_intents"][0]["evidence_handles"], [canonical])
        for unknown in (alias + "x", canonical[:-1], "source_999"):
            rejected = map_evidence_labels({"sections": [
                {"heading": "Scope", "goal": "Define scope", "evidence_handles": []},
                {"heading": "Findings", "goal": "Use evidence", "evidence_handles": [unknown]}]}, labels)
            with self.subTest(unknown=unknown), self.assertRaises(ValueError):
                self.plan(rejected)

    def test_custom_template_remains_authoritative(self):
        client = Mock()
        config = ReportRuntimeConfig(outline_strategy="adaptive")
        template = load_report_template_bundle(report_mode="experiment", config=config).model_copy(update={"name": "user_article"})
        result = _maybe_adapt_outline(client=client, context=self.context, memory=self.memory,
            template=template, config=config, emit=None)
        self.assertIs(result, self.memory)
        client.ask_json.assert_not_called()

    def test_custom_path_with_builtin_basename_is_not_adapted(self):
        client = Mock()
        template = load_report_template_bundle(report_mode="experiment", config=ReportRuntimeConfig())
        config = ReportRuntimeConfig(template="my-templates/experiment.md", outline_strategy="adaptive")
        result = _maybe_adapt_outline(client=client, context=self.context, memory=self.memory,
            template=template, config=config, emit=None)
        self.assertIs(result, self.memory)
        client.ask_json.assert_not_called()

    def test_completed_adaptive_checkpoint_does_not_replan_or_rewrite(self):
        self.response["title"] = "Finite observations"
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive", reviewer="disabled")
        calls, checkpoints = [], []
        response = self.response

        class Client:
            def ask_json(self, system, prompt, *, label="", **kwargs):
                import json
                calls.append(label)
                if label == "report-outline-planner":
                    return response
                payload = json.loads(prompt.partition("\n\n")[2])
                section = payload["section"]
                return {"section_id": section["section_id"], "heading": section["heading"],
                        "draft_markdown": "A bounded description of the supplied observations."}

        kwargs = dict(client=Client(), context=self.context, memory=self.memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=ReportToolGateway(self.context))
        result = run_report_agent(**kwargs, checkpoint_sink=checkpoints.append)
        before = len(calls)
        resumed = run_report_agent(**kwargs, completed_checkpoint=checkpoints[-1])
        self.assertEqual(len(calls), before)
        self.assertEqual(resumed.memory.document_plan, result.memory.document_plan)
        self.assertEqual(resumed.report_body, result.report_body)
        self.assertTrue(result.report_body.startswith("# Finite observations\n"))
        self.assertEqual(resumed.memory.document_plan.title, "Finite observations")

    def test_older_plan_without_title_still_loads(self):
        from simple_ar.report.schema import ReportDocumentPlan
        self.assertEqual(ReportDocumentPlan.model_validate({"sections": []}).title, "")

    def test_whole_word_request_reserves_assembly_and_freezes_one_plan(self):
        from simple_ar.report.agent import _resolve_document_plan, _writer_prompt, _writer_recovery_prompt, _reviewer_prompt
        from simple_ar.report.schema import ReportSectionDraft
        from simple_ar.report.editor import review_document
        self.context.problem_markdown = "Write a 350-500 word report. Keep the observed limits."
        self.response["title"] = "Observed coverage and cost"
        self.response["length_request"] = {"unit": "words", "scope": "whole_document",
            "request_quote": "Write a 350-500 word report.", "min_words": 350, "max_words": 500, "target_words": 450}
        client = Mock()
        client.ask_json.return_value = self.response
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        template = load_report_template_bundle(report_mode="experiment", config=config)
        frozen = _resolve_document_plan(self.adapt(client, config), config=config, context=self.context)
        plan = frozen.document_plan
        budget = plan.length_budget
        self.assertGreater(budget["known_fixed_markdown_tokens"], 0)
        self.assertEqual(plan.target_words, 450)
        self.assertEqual(sum(row.target_words for row in plan.sections) + budget["known_fixed_markdown_tokens"], 450)
        self.assertNotIn("length_request", frozen.outline_planning)
        self.assertEqual(client.ask_json.call_count, 1)
        restored = ReportMemory.model_validate(frozen.model_dump(mode="json"))
        self.assertIs(_resolve_document_plan(restored, config=config, context=None), restored)
        changed_request = self.context.model_copy(update={"problem_markdown": "Write 10,000 words."})
        self.assertIs(_resolve_document_plan(restored, config=config, context=changed_request), restored)
        section = frozen.section_plan[0]
        draft = ReportSectionDraft(section_id=section.section_id, heading=section.heading, draft_markdown="Bounded observed result.")
        common = dict(context=self.context, template=template, memory=frozen, section=section, config=config)
        prompts = [_writer_prompt(**common, previous_draft=None, review=None, extra_context=[], source_batch_index=1,
            source_batch_count=1, include_previous_draft=False, draft_mode="initial"),
            _writer_recovery_prompt(context=self.context, memory=frozen, section=section, config=config,
                previous_draft=None, review=None, extra_context=[], draft_mode="initial")]
        for prompt in prompts:
            view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
            self.assertEqual(view["document_plan"]["length_budget"], budget)
        prompt = _reviewer_prompt(**common, draft=draft)
        view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
        self.assertIn(self.context.problem_markdown, view["objective"])
        self.assertNotIn("argument_plan", view["document_plan"])
        reviewer = Mock()
        reviewer.prepared_publication_for_review = None
        reviewer.ask_json.return_value = {"section_reviews": []}
        review_document(client=reviewer, template=template, memory=frozen, sections=[draft], config=config,
            execution_summary={}, metric_summary={}, writing_objective=self.context.problem_markdown)
        self.assertEqual(json.loads(reviewer.ask_json.call_args.args[1])["document_length_budget"], budget)

    def test_length_request_without_exact_anchor_or_supported_scope_is_not_adopted(self):
        from simple_ar.report.document_plan import validate_length_request
        objective = "Write 350-500 words, not 3 pages."
        valid = {"unit": "words", "scope": "whole_document", "request_quote": objective,
            "min_words": 350, "max_words": 500, "target_words": 425}
        self.assertEqual(validate_length_request(valid, objective=objective), valid)
        for change in ({"request_quote": "Write 350-500 words."}, {"max_words": 700}, {"target_words": 600},
                       {"unit": "pages"}, {"scope": "body_without_references"}, {"target_words": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_length_request({**valid, **change}, objective=objective)
        self.assertEqual(validate_length_request(None, objective=objective), {})
        upper = {**valid, "min_words": 0, "max_words": 500, "request_quote": "500 words"}
        self.assertEqual(validate_length_request(upper, objective="At most 500 words"), upper)
        exact = {**valid, "min_words": 350, "max_words": 350, "target_words": 350, "request_quote": "350 words"}
        self.assertEqual(validate_length_request(exact, objective="Write 350 words"), exact)
        # An invalid optional annotation must not discard valid organization
        # or become a different user requirement. The original task survives.
        from simple_ar.report.agent import _resolve_document_plan, _writer_prompt
        self.context.problem_markdown = objective
        self.response["length_request"] = {**valid, "request_quote": "An invented quotation."}
        client = Mock()
        client.ask_json.return_value = self.response
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        adapted = self.adapt(client, config)
        self.assertNotIn("length_request", adapted.outline_planning)
        self.assertIn("rejected_length_request", adapted.outline_planning)
        frozen = _resolve_document_plan(adapted, config=config, context=self.context)
        self.assertFalse(frozen.document_plan.length_budget)
        self.assertTrue(any("original task" in note for note in frozen.document_plan.notes))
        client.ask_json.assert_called_once()
        prompt = _writer_prompt(context=self.context, memory=frozen, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            section=frozen.section_plan[0], previous_draft=None, review=None, extra_context=[],
            source_batch_index=1, source_batch_count=1, include_previous_draft=False, draft_mode="initial")
        self.assertIn(objective, prompt)

    def test_adaptive_soft_target_survives_outline_projection_freezing_and_recovery(self):
        from simple_ar.report.agent import _resolve_document_plan
        self.context.problem_markdown = 'Deliver a comparative report of about 800 words.'
        request = dict(unit='words', scope='whole_document', request_quote=self.context.problem_markdown,
                       constraint='target', target_words=800, min_words=None, max_words=None)
        client = Mock()
        client.ask_json.return_value = {**self.response, 'length_request': request}
        config = ReportRuntimeConfig(template='reproduction', outline_strategy='adaptive')
        frozen = _resolve_document_plan(self.adapt(client, config), config=config, context=self.context)
        self.assertEqual(frozen.document_plan.length_budget['constraint'], 'target')
        self.assertIsNone(frozen.document_plan.length_budget['max_words'])
        restored = ReportMemory.model_validate(frozen.model_dump(mode='json'))
        self.assertEqual(restored.document_plan.length_budget, frozen.document_plan.length_budget)
        client.ask_json.assert_called_once()
        prompt = client.ask_json.call_args.args[1]
        payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
        self.assertIn('constraint', payload['output_schema']['length_request'])

    def test_invalid_length_budget_gets_only_the_existing_outline_correction(self):
        self.context.problem_markdown = "Write 5 words."
        self.response["title"] = "This title alone is already too long for five words"
        self.response["length_request"] = {"unit": "words", "scope": "whole_document",
            "request_quote": "Write 5 words.", "min_words": 5, "max_words": 5, "target_words": 5}
        client = Mock()
        client.ask_json.return_value = self.response
        with self.assertRaises(LLMError):
            self.adapt(client, ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive"))
        self.assertEqual(client.ask_json.call_count, 2)

    def test_survey_planner_keeps_complete_task_and_freezes_task_scoped_length(self):
        from simple_ar.report.agent import _resolve_document_plan, _outline_planner_prompt
        from simple_ar.report.document_plan import LENGTH_REQUEST_SCHEMA
        task = "Compare the supplied evidence. " + "Keep the scope explicit. " * 100 + "Write a 350-500 word review."
        context = self.context.model_copy(update={"report_mode": "survey", "problem_markdown": task, "goal_markdown": task})
        memory = self.memory.model_copy(update={"template": "survey", "report_mode": "survey",
            "objective": task[:1200], "survey_contract": {"enabled": True,
                "expected_coverage": {"target_words": 12000}},
            "section_plan": [ReportSectionPlan(section_id=str(i), heading=f"Fallback {i}", goal="Compare sources.") for i in range(3)]})
        config = ReportRuntimeConfig(template="survey", outline_strategy="adaptive", reviewer="disabled")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        response = {"sections": [{"heading": "Contrasting measurements", "goal": "Compare the recorded conditions."},
                                  {"heading": "What the evidence leaves open", "goal": "State unresolved limitations."}],
                    "length_request": {"unit": "words", "scope": "whole_document",
                        "request_quote": "Write a 350-500 word review.", "min_words": 350, "max_words": 500, "target_words": 425}}
        payload = json.loads(_outline_planner_prompt(context=context, template=template, memory=memory, config=config).split("\n\n", 1)[1])
        self.assertEqual(payload["objective"], task)
        self.assertEqual(payload["output_schema"]["length_request"], LENGTH_REQUEST_SCHEMA)
        self.assertEqual(payload["objective"].count("Write a 350-500 word review."), 1)
        client = Mock()
        client.ask_json.return_value = response
        adapted = _maybe_adapt_outline(client=client, context=context, memory=memory, template=template, config=config, emit=None)
        frozen = _resolve_document_plan(adapted, config=config, context=context)
        self.assertEqual(len(frozen.section_plan), 2)
        self.assertEqual([r.heading for r in frozen.section_plan], [r["heading"] for r in response["sections"]])
        self.assertTrue(all(not r.subsections for r in frozen.section_plan))
        self.assertEqual(frozen.document_plan.target_words, 425)
        self.assertEqual(sum(r.target_words for r in frozen.section_plan) + frozen.document_plan.length_budget["known_fixed_markdown_tokens"], 425)
        self.assertEqual(client.ask_json.call_count, 1)
        restored = ReportMemory.model_validate(frozen.model_dump(mode="json"))
        changed = context.model_copy(update={"problem_markdown": "Write 10000 words."})
        self.assertIs(_resolve_document_plan(restored, config=config, context=changed), restored)
        self.assertIs(_maybe_adapt_outline(client=client, context=changed, memory=restored, template=template, config=config, emit=None), restored)
        self.assertEqual(client.ask_json.call_count, 1)

    def test_survey_keeps_proposed_structure_without_keyword_rewrites(self):
        context = self.context.model_copy(update={"report_mode": "survey"})
        memory = self.memory.model_copy(update={"template": "survey", "report_mode": "survey",
            "survey_contract": {"enabled": True, "expected_coverage": {"target_words": 12000}},
            "section_plan": [ReportSectionPlan(section_id=str(i), heading=f"Fallback {i}",
                goal="Compare sources.") for i in range(3)]})
        config = ReportRuntimeConfig(template="survey", outline_strategy="adaptive")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        proposals = [
            [{"heading": "Observed conditions", "goal": "Explain the measured setting."},
             {"heading": "Decision", "goal": "Explain what follows from these observations."}],
            [{"heading": heading, "goal": f"Explain supplied evidence for {heading}."}
             for heading in ("Conceptual Foundations and Taxonomy", "Methods and System Construction",
                 "Applications and Use Cases", "Evaluation Benchmarks and Evidence Quality",
                 "Related Surveys and Positioning", "Challenges and Future Directions")],
            [{"heading": "Introduction", "goal": "Define the question.",
              "subsections": ["Assumptions", "Inputs"]},
             {"heading": "Conclusion", "goal": "Answer within the observed conditions.",
              "subsections": ["Recommendation", "Unresolved checks"]}],
        ]
        for proposal in proposals:
            with self.subTest(headings=[row["heading"] for row in proposal]):
                before = json.loads(json.dumps(proposal))
                client = Mock()
                client.ask_json.return_value = {"sections": proposal}
                adapted = _maybe_adapt_outline(client=client, context=context, memory=memory,
                    template=template, config=config, emit=None)
                frozen = _resolve_document_plan(adapted, config=config, context=context)
                self.assertEqual([row.heading for row in frozen.section_plan],
                                 [row["heading"] for row in proposal])
                self.assertEqual([row.goal for row in frozen.section_plan],
                                 [row["goal"] for row in proposal])
                self.assertEqual([row.subsections for row in frozen.section_plan],
                                 [row.get("subsections", []) for row in proposal])
                self.assertEqual(proposal, before)
                client.ask_json.assert_called_once()
                restored = ReportMemory.model_validate(frozen.model_dump(mode="json"))
                self.assertIs(_maybe_adapt_outline(client=client, context=context, memory=restored,
                    template=template, config=config, emit=None), restored)
                client.ask_json.assert_called_once()
        for count in (1, 13):
            with self.subTest(invalid_section_count=count):
                client = Mock()
                client.ask_json.return_value = {"sections": [
                    {"heading": f"Section {i}", "goal": "Explain the evidence."}
                    for i in range(count)]}
                with self.assertRaises(LLMError):
                    _maybe_adapt_outline(client=client, context=context, memory=memory,
                        template=template, config=config, emit=None)
                self.assertEqual(client.ask_json.call_count, 2)

    def test_invalid_survey_length_annotation_preserves_task_without_a_new_loop(self):
        task = "Write 350-500 words."
        context = self.context.model_copy(update={"report_mode": "survey", "problem_markdown": task})
        memory = self.memory.model_copy(update={"template": "survey", "report_mode": "survey",
            "survey_contract": {"enabled": True},
            "section_plan": [ReportSectionPlan(section_id=str(i), heading=f"Fallback {i}", goal="Compare sources.") for i in range(3)]})
        config = ReportRuntimeConfig(template="survey", outline_strategy="adaptive")
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        for change in ({"unit": "pages"}, {"max_words": 900}, {"request_quote": "Write 350-500 words please."}):
            with self.subTest(change=change):
                client = Mock()
                client.ask_json.return_value = {**self.response, "length_request": {
                    "unit": "words", "scope": "whole_document", "request_quote": task,
                    "min_words": 350, "max_words": 500, "target_words": 425, **change}}
                adapted = _maybe_adapt_outline(client=client, context=context, memory=memory,
                    template=template, config=config, emit=None)
                self.assertTrue(adapted.section_plan)
                self.assertNotIn("length_request", adapted.outline_planning)
                self.assertIn("rejected_length_request", adapted.outline_planning)
                self.assertEqual(context.problem_markdown, task)
                self.assertEqual(client.ask_json.call_count, 1)

    def test_absent_word_request_keeps_legacy_plan_and_serialization(self):
        from simple_ar.report.agent import _resolve_document_plan
        client = Mock()
        client.ask_json.return_value = self.response
        config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        frozen = _resolve_document_plan(self.adapt(client, config), config=config, context=self.context)
        self.assertEqual(frozen.document_plan.target_words, 0)
        self.assertEqual(frozen.document_plan.sections[1].target_words, 300)
        self.assertNotIn("length_budget", frozen.document_plan.model_dump(mode="json"))


class ArgumentPlanTests(unittest.TestCase):
    def setUp(self):
        self.config = ReportRuntimeConfig(template="reproduction", outline_strategy="adaptive")
        self.context = ReportContext(topic="Measured trade-off", report_mode="experiment")
        self.memory = ReportMemory(template="reproduction", section_plan=[
            ReportSectionPlan(section_id="old", heading="Findings", goal="Explain results")],
            source_handles=[SourceHandle(handle="source:a", title="Method A", kind="paper"),
                            SourceHandle(handle="source:b", title="Method B", kind="paper")])
        self.response = {"title": "Measured coverage and cost", "sections": [
            {"heading": "Comparison", "goal": "Explain the measured trade-off", "evidence_handles": ["source:a", "source:b"]},
            {"heading": "Interpretation", "goal": "Explain what the trade-off means", "evidence_handles": ["source:a"]},
            {"heading": "Scope", "goal": "Conditions and unresolved issues", "evidence_handles": []}],
            "argument_plan": {"question": "What improved and at what cost?",
                "answer": "Coverage improved with larger regions under the observed protocol.",
                "document_kind": "finite reproduction report",
                "points": [{"claim": "There is a coverage-size trade-off", "section_heading": "Comparison",
                    "evidence_handles": ["source:a"], "reasoning": "Neither coverage nor size alone captures the trade-off.",
                    "qualifications": ["Only the supplied dataset was measured"], "counterevidence_handles": ["source:b"]}],
                "scope_section_heading": "Scope", "technical_details_placement": "appendix"}}

    def adapt(self, response=None):
        client = Mock()
        client.ask_json.return_value = response or self.response
        template = load_report_template_bundle(report_mode="experiment", config=self.config)
        proposed = _maybe_adapt_outline(client=client, context=self.context, template=template,
            memory=self.memory, config=self.config, emit=None)
        return _resolve_document_plan(proposed, config=self.config, context=self.context), client, template

    def test_same_planner_call_freezes_argument_for_writer_and_recovery(self):
        memory, client, template = self.adapt()
        self.assertEqual(client.ask_json.call_count, 1)
        self.assertEqual(memory.document_plan.argument_plan.points[0].section_id, "comparison")
        restored = ReportMemory.model_validate(memory.model_dump(mode="json"))
        self.assertEqual(restored.document_plan, memory.document_plan)
        self.assertIs(_resolve_document_plan(restored, config=self.config, context=self.context), restored)
        prompt = _writer_prompt(context=self.context, template=template, memory=restored,
            section=restored.section_plan[0], config=self.config, extra_context=[], previous_draft=None,
            review=None, source_batch_index=1, source_batch_count=1, include_previous_draft=False, draft_mode="initial")
        self.assertIn("What improved and at what cost?", prompt)
        self.assertIn("Neither coverage nor size alone", prompt)
        view = narrative_context(restored, restored.section_plan[1], [])
        self.assertNotIn("argument_task", view)
        self.assertEqual(view["argument_plan_source"], "document_plan.argument_plan")
        self.assertEqual(view["argument_section_id"], "interpretation")
        self.assertEqual(prompt.count("What improved and at what cost?"), 1)
        self.assertEqual(prompt.count("Neither coverage nor size alone"), 1)
        self.assertEqual(restored.document_plan.argument_plan.scope_section_id, "scope")

    def test_new_plan_supports_cross_section_corrections_legacy_scope_unchanged(self):
        memory, _, _ = self.adapt()
        self.assertEqual(document_revision_limit(memory), 3)
        restored = ReportMemory.model_validate(memory.model_dump(mode="json"))
        self.assertEqual(document_revision_limit(restored), 3)
        restored.document_plan.argument_plan = None
        self.assertEqual(document_revision_limit(restored), 2)

    def test_missing_argument_is_not_invented_for_legacy_response(self):
        response = dict(self.response)
        response.pop("argument_plan")
        memory, client, _ = self.adapt(response)
        self.assertIsNone(memory.document_plan.argument_plan)
        self.assertEqual(client.ask_json.call_count, 1)

    def test_source_and_owner_binding_never_repairs_into_different_evidence(self):
        memory, _, _ = self.adapt()
        for changes in ({"section_heading": "Unknown section"}, {"evidence_handles": ["unknown"]}, {"metric_ids": ["invented"]}):
            value = json.loads(json.dumps(self.response["argument_plan"]))
            value["points"][0].update(changes)
            with self.assertRaises(ValueError):
                resolve_argument_plan(value, sections=memory.section_plan,
                    evidence_handles=["source:a", "source:b"], metric_ids=[])

    def test_heading_presentation_normalizes_without_semantic_guessing(self):
        response = json.loads(json.dumps(self.response))
        response['sections'][0]['heading'] = '## 2 Comparison'
        response['argument_plan']['points'][0]['section_heading'] = '## 2   Comparison'
        memory, client, _ = self.adapt(response)
        self.assertEqual(client.ask_json.call_count, 1)
        self.assertEqual(memory.document_plan.argument_plan.points[0].section_id, 'comparison')

    def keyed_response(self):
        response = json.loads(json.dumps(self.response))
        for row, key in zip(response['sections'], ('comparison-key', 'interpretation-key', 'scope-key')):
            row['section_key'] = key
        point = response['argument_plan']['points'][0]
        point.pop('section_heading')
        point['section_key'] = 'comparison-key'
        response['argument_plan'].pop('scope_section_heading')
        response['argument_plan']['scope_section_key'] = 'scope-key'
        return response

    def test_literal_keys_separate_display_titles_from_argument_ownership(self):
        response = self.keyed_response()
        response['sections'][0]['heading'] = 'Evidence and its interpretation'
        memory, client, _ = self.adapt(response)
        self.assertEqual(client.ask_json.call_count, 1)
        self.assertEqual(memory.document_plan.argument_plan.points[0].section_id, 'evidence_and_its_interpretation')
        self.assertEqual(memory.document_plan.argument_plan.scope_section_id, 'scope')
        self.assertNotIn('section_key', memory.document_plan.argument_plan.model_dump_json())
        self.assertEqual(ReportMemory.model_validate(memory.model_dump()).document_plan, memory.document_plan)

    def test_unknown_duplicate_bad_keys_or_conflicting_heading_never_guess_an_owner(self):
        baseline = self.keyed_response()
        for defect in ('unknown', 'duplicate', 'bad-type', 'space', 'heading-conflict', 'scope-conflict'):
            with self.subTest(defect=defect):
                response = json.loads(json.dumps(baseline))
                if defect == 'unknown':
                    response['argument_plan']['points'][0]['section_key'] = 'absent'
                elif defect == 'duplicate':
                    response['sections'][1]['section_key'] = 'comparison-key'
                elif defect == 'bad-type':
                    response['sections'][0]['section_key'] = []
                elif defect == 'space':
                    response['sections'][0]['section_key'] = 'comparison key'
                elif defect == 'heading-conflict':
                    response['argument_plan']['points'][0]['section_heading'] = 'Interpretation'
                else:
                    response['argument_plan']['scope_section_heading'] = 'Comparison'
                memory, _, _ = self.adapt()
                with self.assertRaises(ValueError):
                    keys = outline_section_keys(response['sections'], memory.section_plan)
                    resolve_argument_plan(response['argument_plan'], sections=memory.section_plan,
                        evidence_handles=['source:a', 'source:b'], metric_ids=[], section_keys=keys)

    def test_visual_keys_bind_to_the_same_canonical_section_and_keep_input_immutable(self):
        response = self.keyed_response()
        response['visual_intents'] = [{'kind': 'table', 'section_key': 'comparison-key',
            'title': 'Measured trade-off', 'purpose': 'Compare outcomes', 'columns': ['A', 'B']}]
        memory, _, template = self.adapt()
        original = json.loads(json.dumps(response))
        result = _validated_outline_delivery(response, sections=memory.section_plan, context=self.context,
            memory=self.memory, config=self.config)
        self.assertEqual(result[1]['visual_candidates'][0]['section_id'], 'comparison')
        self.assertNotIn('section_key', result[1]['visual_candidates'][0])
        self.assertEqual(response, original)
        for owner in ('absent', ['bad']):
            response['visual_intents'][0]['section_key'] = owner
            with self.assertRaises(ValueError):
                _validated_outline_delivery(response, sections=memory.section_plan, context=self.context,
                    memory=self.memory, config=self.config)

    def test_planning_correction_receives_the_actual_error_and_valid_owners(self):
        response = json.loads(json.dumps(self.response))
        response['argument_plan']['points'][0]['section_heading'] = 'No such heading'
        client = Mock()
        client.ask_json.side_effect = [response, self.response]
        template = load_report_template_bundle(report_mode='experiment', config=self.config)
        memory = _maybe_adapt_outline(client=client, context=self.context, template=template,
            memory=self.memory, config=self.config, emit=None)
        correction = client.ask_json.call_args_list[1].args[1]
        self.assertIn('"validation_error"', correction)
        self.assertIn('No such heading', correction)
        self.assertIn('Comparison', correction)
        parsed = json.loads(correction[correction.index('{'):])
        self.assertEqual(map_evidence_labels(parsed['rejected_response'],
            {"source_1": "source:a", "source_2": "source:b"}), response)
        self.assertIn('metric_id', parsed['retry_instruction'])
        self.assertEqual(len(memory.section_plan), 3)

    def test_unknown_metric_correction_identifies_bad_ids_without_guessing_replacements(self):
        response = json.loads(json.dumps(self.response))
        response['argument_plan']['points'][0]['metric_ids'] = ['coverage_display_name']
        client = Mock()
        client.ask_json.side_effect = [response, self.response]
        template = load_report_template_bundle(report_mode='experiment', config=self.config)
        _maybe_adapt_outline(client=client, context=self.context, template=template,
            memory=self.memory, config=self.config, emit=None)
        prompt = client.ask_json.call_args_list[1].args[1]
        correction = json.loads(prompt[prompt.index('{'):])
        self.assertIn('coverage_display_name', correction['validation_error'])
        self.assertEqual(map_evidence_labels(correction['rejected_response'],
            {"source_1": "source:a", "source_2": "source:b"}), response)
        self.assertEqual(client.ask_json.call_count, 2)

    def test_comparisons_are_consumed_as_interpretations_not_proof(self):
        memory, _, _ = self.adapt()
        self.context.source_comparisons = [{"dimension": "coverage", "relation": "not_comparable"}]
        view = narrative_context(memory, memory.section_plan[0], [], context=self.context, config=self.config)
        self.assertEqual(view["source_comparisons"], self.context.source_comparisons)
        self.assertEqual(view["comparison_status"]["independent_verification"], "not_performed")


class EvidenceComparisonTests(unittest.TestCase):
    def setUp(self):
        self.pack = {"paper_cards": [{"paper_id": "p:a"}, {"paper_id": "p:b"}]}
        self.row = {"dimension": "evaluation setting", "relation": "conditional_difference",
            "evidence_refs": ["p:a", "p:b"], "observations": ["Different populations were measured"],
            "conditions": ["Dataset shift"], "interpretation": "Not a direct performance comparison",
            "unresolved_question": "No common-condition evaluation is available"}

    def test_comparison_survives_shared_synthesis_handoff(self):
        rows = _parse_evidence_comparisons([self.row], self.pack)
        result = SynthesisResult(status="ready", gap_summary="", ideas=(), novelty_checks=(), comparisons=rows)
        restored = SynthesisResult.from_handoff_dict(result.to_handoff_dict())
        self.assertEqual(restored.comparisons, rows)
        self.assertEqual(restored.to_dict()["comparison_count"], 1)
        self.assertEqual(_parse_evidence_comparisons(None, self.pack), ())

    def test_unknown_sources_are_not_accepted_as_a_conflict(self):
        with self.assertRaises(LLMError):
            _parse_evidence_comparisons([{**self.row, "evidence_refs": ["invented"]}], self.pack)
        with self.assertRaises(LLMError):
            _parse_evidence_comparisons([{**self.row, "relation": "verified"}], self.pack)

    def test_comparison_prose_normalizes_without_changing_claim_and_error_names_the_actual_field(self):
        result = _parse_evidence_comparisons([{**self.row, 'observations': 'One supported observation',
            'conditions': 'One explicit condition'}], self.pack)
        self.assertEqual(result[0]['observations'], ['One supported observation'])
        self.assertEqual(result[0]['conditions'], ['One explicit condition'])
        with self.assertRaisesRegex(LLMError, r'comparisons\[0\].conditions'):
            _parse_evidence_comparisons([{**self.row, 'conditions': {'invented': 'field'}}], self.pack)
        with self.assertRaisesRegex(LLMError, r'unknown IDs.*invented-section'):
            _parse_evidence_comparisons([{**self.row, 'evidence_refs': ['invented-section']}], self.pack)
        with self.assertRaisesRegex(LLMError, 'relation must be ONE string'):
            _parse_evidence_comparisons([{**self.row, 'relation': ['agreement', 'conditional_difference']}], self.pack)


if __name__ == "__main__":
    unittest.main()
