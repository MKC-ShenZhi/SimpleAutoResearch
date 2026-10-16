"""Final delivery, not body/forecast, owns the frozen word-budget check."""
import copy
import unittest

from simple_ar.report.audit import build_report_audit
from simple_ar.report.schema import ReportContext, ReportDocumentPlan, ReportMemory


class ReportLengthAuditTests(unittest.TestCase):
    def test_soft_target_counts_without_inventing_a_range_or_clearing_other_findings(self):
        from simple_ar.report.document_plan import validate_length_request, check_document_length
        from simple_ar.report.schema import ReviewerFinding
        quote = 'Deliver approximately 1,200 words of manuscript body, excluding title, headings and references.'
        request = dict(unit='words', scope='manuscript_body', request_quote=quote,
                       constraint='target', target_words=1200, min_words=None, max_words=None)
        self.assertEqual(validate_length_request(request, objective=quote), request)
        context = ReportContext(topic='Soft extent', problem_markdown=quote, report_mode='supplied_materials')
        old = ReviewerFinding(finding_id='unresolved-fact', type='unsupported_claim', severity='major',
                              required_action='verify', message='Still unresolved independent factual concern')
        memory = ReportMemory(document_plan=ReportDocumentPlan(length_budget=request), reviewer_findings=[old])
        for count in (10, 1137, 1200, 1500):
            observed = check_document_length(request, objective=quote, token_count=count)
            self.assertEqual(observed['status'], 'target_only')
            self.assertEqual(observed['target_difference'], count - 1200)
            audit = self.check('# Title\nreport', context, memory, body='word ' * count)
            self.assertFalse(any(f.type == 'delivery_length' for f in audit.reviewer_findings))
            self.assertIn(old, audit.reviewer_findings)
            self.assertEqual(audit.status, 'failed')
        self.assertEqual(check_document_length(request, objective=quote, token_count=None)['status'], 'unavailable')
        for change in ({'min_words': 1080}, {'max_words': 1320}, {'target_words': 1000},
                       {'target_words': True}, {'constraint': 'approximately'}, {'constraint': []},
                       {'scope': 'prose_only'}, {'request_quote': 'approximately 1200 words'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_length_request({**request, **change}, objective=quote)

    def test_soft_target_allocates_on_the_same_plan_and_can_be_smaller_than_required_assembly(self):
        from simple_ar.report.document_plan import reserve_document_words
        from simple_ar.report.narrative import budget_document_plan, delivery_text_observation
        from simple_ar.report.schema import ReportRuntimeConfig, ReportSectionPlan, ReportSectionDraft
        quote = 'A report of about 80 words including title and references.'
        request = dict(unit='words', scope='whole_document', request_quote=quote,
                       constraint='target', target_words=80, min_words=None, max_words=None)
        context = ReportContext(topic='Soft target', problem_markdown=quote, report_mode='supplied_materials')
        section = ReportSectionPlan(section_id='answer', heading='Answer', goal='Use existing evidence')
        memory = ReportMemory(section_plan=[section])
        config = ReportRuntimeConfig()
        plan = budget_document_plan(context, memory, config, ReportDocumentPlan(sections=[section]), request)
        self.assertEqual(plan.length_budget['constraint'], 'target')
        self.assertEqual(plan.target_words, 80)
        memory.document_plan = ReportDocumentPlan.model_validate(plan.model_dump(mode='json'))
        draft = ReportSectionDraft(section_id='answer', heading='Answer', draft_markdown='word ' * 70)
        observed = delivery_text_observation(context, memory, [draft], config)
        self.assertEqual(observed['length_check']['status'], 'target_only')
        oversized = reserve_document_words(plan, request=request,
            forecast={'status': 'known_assembly_forecast', 'known_fixed_markdown_tokens': 90})
        self.assertEqual(oversized.length_budget['model_body_target_words'], 1)
        self.assertEqual(oversized.length_budget['target_words'], 80)
        self.assertIn('retain required evidence', oversized.length_budget['guidance'])

    def test_old_frozen_equal_bounds_do_not_silently_become_a_soft_target(self):
        from simple_ar.report.document_plan import check_document_length
        quote = 'Deliver approximately 1200 words.'
        old = dict(unit='words', scope='whole_document', request_quote=quote,
                   target_words=1200, min_words=1200, max_words=1200)
        observed = check_document_length(old, objective=quote, token_count=1137)
        self.assertEqual(observed['status'], 'below_range')
        self.assertNotIn('constraint', observed)

    def test_body_scope_preview_planning_and_final_audit_agree(self):
        from simple_ar.report.document_plan import validate_length_request, manuscript_body_tokens
        from simple_ar.report.narrative import delivery_text_observation, budget_document_plan
        from simple_ar.report.schema import ReportRuntimeConfig, ReportSectionPlan, ReportSectionDraft
        from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document
        quote = "Deliver 8–12 words of manuscript body, excluding title, headings and references."
        context = ReportContext(topic="A title with many words", problem_markdown=quote, report_mode="supplied_materials")
        section = ReportSectionPlan(section_id="result", heading="A long section heading", goal="Explain the supplied result")
        memory = ReportMemory(section_plan=[section])
        config = ReportRuntimeConfig()
        request = dict(unit="words", scope="manuscript_body", request_quote=quote, min_words=8, max_words=12, target_words=10)
        self.assertEqual(validate_length_request(request, objective=quote)["scope"], "manuscript_body")
        memory.document_plan = budget_document_plan(context, memory, config, ReportDocumentPlan(sections=[section]), request)
        self.assertEqual(memory.document_plan.length_budget["known_fixed_markdown_tokens"], 0)
        self.assertEqual(memory.document_plan.length_budget["model_body_target_words"], 10)
        drafts = [ReportSectionDraft(section_id="result", heading=section.heading, draft_markdown="word " * 10)]
        preview = preview_report_document(ReportAssemblyRequest(title=context.topic, sections=tuple(drafts), config=config, document_plan=memory.document_plan))
        observation = delivery_text_observation(context, memory, drafts, config)
        self.assertEqual(observation["markdown_token_count"], 10)
        self.assertGreater(observation["full_delivery_markdown_token_count"], 12)
        findings = self.check(preview.report_markdown + "\n## References\n" + "reference " * 20,
            context, memory, body=preview.report_body_markdown).reviewer_findings
        self.assertFalse(any(f.type == "delivery_length" for f in findings))
        self.assertEqual(manuscript_body_tokens("# Title\n## Section\n| value | 4 |\nCaption explains result."), 8)
        self.assertTrue(any(f.finding_id == "document-length-outside-budget" for f in
            self.check(preview.report_markdown, context, memory, body="word " * 13).reviewer_findings))
        self.assertTrue(any(f.finding_id == "document-length-unavailable" for f in
            self.check(preview.report_markdown, context, memory, body="").reviewer_findings))
        # Excluding only references is not the same contract as excluding
        # headings as well. Preview and final audit use the same canonical
        # body field, even when bibliography text contains a heading-like title.
        quote = 'Deliver 8–50 words, excluding references but including headings.'
        context.problem_markdown = quote
        request = dict(unit='words', scope='manuscript', request_quote=quote,
            min_words=8, max_words=50, target_words=12)
        memory.document_plan = budget_document_plan(context, memory, config,
            ReportDocumentPlan(sections=[section]), request)
        preview = preview_report_document(ReportAssemblyRequest(title=context.topic,
            sections=tuple(drafts), config=config, document_plan=memory.document_plan))
        observation = delivery_text_observation(context, memory, drafts, config)
        self.assertEqual(observation['markdown_token_count'], len(preview.report_body_markdown.split()))
        self.assertGreater(observation['markdown_token_count'], 10)
        self.assertFalse(any(f.type == 'delivery_length' for f in self.check(
            preview.report_markdown + '\n## References\n' + 'reference ' * 80,
            context, memory, body=preview.report_body_markdown).reviewer_findings))
        self.assertTrue(any(f.finding_id == 'document-length-outside-budget' for f in
            self.check(preview.report_markdown, context, memory, body='word ' * 51).reviewer_findings))

    def objects(self):
        quote = "Deliver 8–12 words for the whole document, including references."
        context = ReportContext(topic="Recorded report", report_mode="supplied_materials",
                                problem_markdown="Keep the source bounds. " + quote)
        memory = ReportMemory(objective="A shortened objective without the numeric requirement.",
            document_plan=ReportDocumentPlan(target_words=10, length_budget={"unit": "words",
                "scope": "whole_document", "request_quote": quote, "min_words": 8, "max_words": 12,
                "target_words": 10, "known_fixed_markdown_tokens": 4, "model_body_target_words": 6}))
        return context, memory

    def check(self, report, context, memory, body="short body"):
        return build_report_audit(report=report, report_body=body, context=context, memory=memory)

    def test_actual_final_document_counts_headings_references_not_body_or_forecast(self):
        context, memory = self.objects()
        report = "# Title\n\n" + "word " * 8 + "\n## References\nName Year URL"
        before = memory.model_dump(mode="json")
        audit = self.check(report, context, memory)
        finding = next(f for f in audit.reviewer_findings if f.finding_id == "document-length-outside-budget")
        self.assertIn(str(len(report.split())) + " whitespace-separated", finding.message)
        self.assertEqual(finding.required_action, "revise")
        self.assertNotEqual(audit.status, "passed")
        self.assertEqual(memory.model_dump(mode="json"), before)

    def test_inclusive_bounds_and_minimum_use_same_recorded_range(self):
        context, memory = self.objects()
        for number in (7, 8, 10, 12, 13):
            with self.subTest(number=number):
                findings = self.check("word " * number, context, memory).reviewer_findings
                self.assertEqual(any(f.finding_id == "document-length-outside-budget" for f in findings), number in (7, 13))

    def test_no_contract_or_generic_target_never_infers_a_gate_from_task(self):
        context, memory = self.objects()
        for plan in (None, memory.document_plan.model_copy(update={"length_budget": {}})):
            with self.subTest(plan=plan):
                memory.document_plan = plan
                findings = self.check("word " * 50, context, memory).reviewer_findings
                self.assertFalse(any(f.type == "delivery_length" for f in findings))

    def test_invalid_contract_is_unchecked_not_silently_dropped_or_reinterpreted(self):
        context, memory = self.objects()
        original = copy.deepcopy(memory.document_plan.length_budget)
        for update in ({"request_quote": "A quotation absent from the task."}, {"unit": "pages"},
                       {"scope": "body_only"}, {"max_words": 15}, {"min_words": True}, {"target_words": 20}):
            with self.subTest(update=update):
                memory.document_plan.length_budget = {**original, **update}
                audit = self.check("word " * 10, context, memory)
                finding = next(f for f in audit.reviewer_findings if f.finding_id == "document-length-contract")
                self.assertEqual(finding.required_action, "verify")
                self.assertNotEqual(audit.status, "passed")
                self.assertEqual(memory.document_plan.length_budget, {**original, **update})

    def test_missing_final_text_does_not_certify_a_valid_body(self):
        context, memory = self.objects()
        audit = self.check("", context, memory, body="word " * 10)
        self.assertTrue(any(f.finding_id == "document-length-unavailable" for f in audit.reviewer_findings))

    def test_length_gate_does_not_close_existing_review_or_certify_semantics(self):
        from simple_ar.report.schema import ReviewerFinding
        context, memory = self.objects()
        old = ReviewerFinding(finding_id="unresolved-fact", type="unsupported_claim", severity="major",
                              required_action="verify", message="Source identity not established.")
        memory.reviewer_findings = [old]
        audit = self.check("word " * 10, context, memory)
        self.assertIn(old, audit.reviewer_findings)
        self.assertEqual(audit.status, "failed")
        self.assertTrue(any("not language-independent" in note for note in audit.notes))

    def candidate_objects(self):
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.schema import ReportRuntimeConfig, ReportSectionDraft, ReportSectionPlan
        from simple_ar.report.templates import load_report_template_bundle
        config = ReportRuntimeConfig(template="material_report", allow_llm_fallback=False)
        context = ReportContext(topic="Measured scope", report_mode="supplied_materials")
        plans = [ReportSectionPlan(section_id=sid, heading=sid, goal="Retain supported evidence.")
                 for sid in ("first", "second")]
        memory = ReportMemory(section_plan=plans, document_plan=ReportDocumentPlan(sections=plans))
        sections = [ReportSectionDraft(section_id=row.section_id, heading=row.heading,
                    draft_markdown="supported " * 10) for row in plans]
        count = delivery_text_observation(context, memory, sections, config)["markdown_token_count"]
        quote = f"Deliver {count - 1}–{count + 1} words for the whole document."
        context.problem_markdown = quote
        memory.document_plan.length_budget = {"unit": "words", "scope": "whole_document", "request_quote": quote,
            "min_words": count - 1, "max_words": count + 1, "target_words": count}
        return dict(context=context, memory=memory, config=config, section=plans[1],
            template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            adopted_sections=sections), count

    def review_candidate(self, objects, candidate):
        from simple_ar.report.agent import _section_response
        from simple_ar.report.schema import ReportSectionReview
        calls = []
        class Client:
            def ask_json(client, system, prompt, **kwargs):
                calls.append(prompt)
                return {"verdict": "pass"}
        reviewed = _section_response(**objects, client=Client(), draft=candidate,
            previous_draft=objects["adopted_sections"][-1],
            revision_review=ReportSectionReview(section_id="second", verdict="revise_required"), label="candidate")
        self.assertEqual(len(calls), 1)  # A valid answer is not a format retry.
        return reviewed

    def test_model_pass_cannot_adopt_candidate_crossing_either_canonical_bound(self):
        for text in ("expanded " * 25, "short"):
            with self.subTest(text=text):
                objects, count = self.candidate_objects()
                before = objects["memory"].model_dump(mode="json")
                old_sections = [row.model_dump(mode="json") for row in objects["adopted_sections"]]
                candidate = objects["adopted_sections"][-1].model_copy(update={"draft_markdown": text})
                reviewed = self.review_candidate(objects, candidate)
                self.assertEqual(reviewed.verdict, "revise_required")
                finding = reviewed.findings[-1]
                self.assertEqual(finding.finding_id, "second-delivery-length-regression")
                self.assertEqual(finding.required_action, "revise")
                self.assertIn(f"from {count} to", finding.message)
                self.assertEqual(objects["memory"].model_dump(mode="json"), before)
                self.assertEqual([row.model_dump(mode="json") for row in objects["adopted_sections"]], old_sections)

    def test_candidate_check_does_not_infer_bounds_or_blame_incomplete_or_already_invalid_delivery(self):
        for variant in ("no_contract", "invalid_contract", "missing_section", "already_over"):
            with self.subTest(variant=variant):
                objects, _ = self.candidate_objects()
                if variant == "no_contract":
                    objects["memory"].document_plan.length_budget = {}
                elif variant == "invalid_contract":
                    objects["memory"].document_plan.length_budget["request_quote"] = "Not the task."
                elif variant == "missing_section":
                    objects["adopted_sections"] = objects["adopted_sections"][1:]
                else:
                    objects["adopted_sections"][0].draft_markdown = "already too long " * 20
                candidate = objects["adopted_sections"][-1].model_copy(update={"draft_markdown": "long " * 30})
                self.assertFalse(self.review_candidate(objects, candidate).findings)

    def test_shared_preview_and_final_check_agree_without_certifying_future_sections(self):
        from simple_ar.report.narrative import delivery_text_observation
        from simple_ar.report.capability import ReportAssemblyRequest, preview_report_document
        objects, _ = self.candidate_objects()
        objects["adopted_sections"][-1].draft_markdown = "expanded " * 25
        context, memory, config = (objects[key] for key in ("context", "memory", "config"))
        preview = preview_report_document(ReportAssemblyRequest(title=context.topic,
            sections=tuple(objects["adopted_sections"]), config=config, document_plan=memory.document_plan,
            template_name=memory.template))
        observed = delivery_text_observation(context, memory, objects["adopted_sections"], config)
        self.assertEqual(observed["length_check"]["status"], "above_range")
        self.assertFalse(observed["pending_draft_sections"])
        self.assertTrue(any(f.finding_id == "document-length-outside-budget"
            for f in self.check(preview.report_markdown, context, memory).reviewer_findings))
        partial = delivery_text_observation(context, memory, objects["adopted_sections"][1:], config)
        self.assertEqual(partial["pending_draft_sections"], ["first"])

    def test_correction_fitting_original_range_is_not_forced_to_shrink(self):
        objects, _ = self.candidate_objects()
        candidate = objects["adopted_sections"][-1].model_copy(update={"draft_markdown": "qualified " * 11})
        reviewed = self.review_candidate(objects, candidate)
        self.assertEqual(reviewed.verdict, "pass")
        self.assertFalse(reviewed.findings)

    def test_native_candidate_rejection_and_resume_share_existing_edit_allowance(self):
        import json
        from simple_ar.report.agent import run_report_agent
        from simple_ar.report.tool_gateway import ReportToolGateway
        from tests.report_review_fixtures import draft_quotes
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt):
                objects, _ = self.candidate_objects()
                objects["config"].document_review = True
                objects["config"].max_review_iterations = 2
                labels, saved = [], []
                class Client:
                    def ask_json(client, system, prompt, *, label="", **kwargs):
                        labels.append(label)
                        if label == "report-document-reviewer":
                            return {"section_reviews": [{"section_id": "second", "verdict": "revise_required",
                                "findings": [{"finding_id": "style", "type": "style", "severity": "minor",
                                    "required_action": "revise", "message": "Qualify the wording.",
                                    "draft_quotes": draft_quotes(prompt, "second")}]}]}
                        if label == "report-document-verifier":
                            return {"section_reviews": []}
                        if "reviewer" in label or "verifier" in label:
                            return {"verdict": "pass"}
                        if label == "report-document-reviser-second":
                            count = sum(row == label for row in labels)
                            if count == 2:
                                view = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                                self.assertTrue(any(row["type"] == "delivery_length" for row in view["review_findings"]))
                            return {"draft_markdown": "expanded " * 25 if count == 1 else "qualified " * 10}
                        return {"draft_markdown": "supported " * 10}
                interrupted = False
                def sink(row):
                    nonlocal interrupted
                    saved.append(row)
                    if interrupt and not interrupted and any(event["action"] == "document_revise" for event in row["iterations"]):
                        interrupted = True
                        raise RuntimeError("Stopped after saving candidate, before its verification.")
                common = {key: objects[key] for key in ("context", "memory", "config", "template")}
                common.update(client=Client(), gateway=ReportToolGateway(objects["context"]), checkpoint_sink=sink)
                if interrupt:
                    with self.assertRaises(RuntimeError):
                        run_report_agent(**common)
                    result = run_report_agent(**common, completed_checkpoint=saved[-1])
                else:
                    result = run_report_agent(**common)
                candidates = [row for row in result.iterations if row.action == "document_revise"]
                self.assertEqual([row.adopted for row in candidates], [False, True])
                self.assertEqual(labels.count("report-document-reviser-second"), 2)
                self.assertIn("qualified", result.report_body)
                self.assertNotIn("expanded", result.report_body)
                before = len(labels)
                run_report_agent(**common, completed_checkpoint=saved[-1])
                self.assertEqual(len(labels), before)
