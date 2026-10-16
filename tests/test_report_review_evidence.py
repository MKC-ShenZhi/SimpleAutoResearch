"""Review anchors locate supplied evidence; they do not certify its meaning."""
import json
import unittest

from tests.report_review_fixtures import document_review_inputs, prompt_payload

from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.editor import review_document
from simple_ar.report.agent import _section_response
from simple_ar.report.review_evidence import validate_evidence_quotes, review_evidence_locator, _resolve_pointer
from simple_ar.report.schema import (
    ReportContext, ReportDraftQuote, ReportEvidenceQuote, ReportFindingCheck, ReportMemory, ReportRuntimeConfig,
    ReportSectionDraft, ReportSectionPlan, ReportSectionReview, ReportToolResult, ReviewerFinding, SourceHandle,
)
from simple_ar.report.templates import load_report_template_bundle


class ReviewEvidenceTests(unittest.TestCase):
    def test_default_document_capacity_keeps_large_evidence_and_client_budget_ownership(self):
        context, memory, config, draft = self.objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        evidence = {"rows": ["complete recorded evidence " * 5000]}
        review_document(client=Client(), **document_review_inputs(memory, config, [draft], metric_summary=evidence))
        self.assertGreater(len(json.dumps(captured[0])), 90_000)
        self.assertEqual(captured[0]["metric_sources"], evidence)
        self.assertEqual(captured[0]["sections"][0]["markdown"], draft.draft_markdown)
        class BudgetClient:
            def ask_json(self, *args, **ignored):
                raise LLMError("existing client/session capacity exhausted")
        with self.assertRaisesRegex(LLMError, "capacity exhausted"):
            review_document(client=BudgetClient(), **document_review_inputs(memory, config, [draft], metric_summary=evidence))

    def test_source_passage_layout_whitespace_is_not_a_changed_scientific_quote(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": "Observed mean\nerror = 0.653;\tcontrol = 0.020."}]}}],
            "supplementary_evidence": [{"tool_name": "search_source_chunks", "content": {
                "chunks": [{"text": "Observed mean\r\nerror = 0.653;  control = 0.020."}]}}]}
        before = json.dumps(view)
        for pointer in ("/source_evidence/0/metadata/evidence_passages/0/text",
                        "/supplementary_evidence/0/content/chunks/0/text"):
            quote = ReportEvidenceQuote(pointer=pointer,
                quote="Observed mean error = 0.653; control = 0.020.", role="recorded_material")
            validate_evidence_quotes([quote], view)
            self.assertEqual(quote.quote, "Observed mean error = 0.653; control = 0.020.")
            with self.assertRaisesRegex(LLMResponseError, "recorded_material"):
                validate_evidence_quotes([quote.model_copy(update={"role": "executor_record"})], view)
        self.assertEqual(json.dumps(view), before)

    def test_source_layout_matching_rejects_non_whitespace_changes_and_window_stitching(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": "Observed mean\nerror = 0.653; control = 0.020. ﬁtted model, high-\nconfidence."},
            {"text": "A different retained window."}]}}]}
        pointer = "/source_evidence/0/metadata/evidence_passages/0/text"
        for absent in ("observed mean error", "error = 0.654", "error = -0.653", "error = 0. 653",
                "error = 0.653, control", "fitted model", "high-confidence",
                "control = 0.020. A different retained window.", "error = 0.653; omitted control = 0.020."):
            with self.subTest(quote=absent), self.assertRaisesRegex(LLMResponseError, "absent or changed"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=absent,
                    role="recorded_material")], view)

    def test_layout_normalization_never_changes_identity_paths_or_scalar_values(self):
        view = {"source_evidence": [{"metadata": {"title": "Two  distinct records"}}],
            "execution_evidence": {"execution_records": [{"cwd": "/a  b", "duration_sec": 3.5}]},
            "metric_sources": {"accuracy": 0.5},
            "supplementary_evidence": [{"tool_name": "get_synthesis_brief", "content": {
                "chunks": [{"text": "Earlier\ninterpretation"}]}}]}
        for pointer, quote, role in (("/source_evidence/0/metadata/title", "Two distinct records", "recorded_material"),
                ("/execution_evidence/execution_records/0/cwd", "/a b", "executor_record"),
                ("/execution_evidence/execution_records/0/duration_sec", "3 .5", "executor_record"),
                ("/metric_sources/accuracy", "0.50", "registered_result"),
                ("/supplementary_evidence/0/content/chunks/0/text", "Earlier interpretation", "derived_context")):
            with self.subTest(pointer=pointer), self.assertRaisesRegex(LLMResponseError, "absent or changed"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote, role=role)], view)

    def test_locator_uses_current_nested_paths_and_roles_without_copying_or_mutating_evidence(self):
        view = {"objective": "Do not independently verify by declaration.",
            "source_evidence": [{"metadata": {"title": "Title", "evidence_passages": [
                {"text": "Original quantitative statement.", "chunk_id": "chunk-3"}]}}],
            "execution_evidence": {"declared_execution_context": {"cwd": "requested"},
                "execution_records": [{"cwd": "observed", "exit_code": 0}],
                "output_evidence": [{"preview": "Producer observation."}]},
            "metric_sources": {"a/b~c": 3}, "response_format_correction": {
                "rejected_response": {"quote": "Never index a rejected allegation."}},
            "revision_context": {"length_observation": {"candidate_tokens": 17},
                "previous_draft": {"draft_markdown": "An earlier draft is not evidence."}},
            "sections": [{"markdown": "Not source evidence."}]}
        before = json.dumps(view, ensure_ascii=False)
        locator = review_evidence_locator(view)
        self.assertEqual(json.dumps(view, ensure_ascii=False), before)
        groups = locator["pointers_by_role"]
        self.assertEqual(groups["recorded_material"]["recorded_material:0"], "/source_evidence/0/metadata/evidence_passages/0/text")
        self.assertIn("/execution_evidence/execution_records/0/cwd", groups["executor_record"].values())
        self.assertIn("/execution_evidence/declared_execution_context/cwd", groups["declaration"].values())
        self.assertIn("/metric_sources/a~1b~0c", groups["registered_result"].values())
        self.assertIn("/revision_context/length_observation/candidate_tokens", groups["derived_context"].values())
        self.assertEqual(locator["omitted_scalar_count"], 0)
        self.assertEqual(locator["coverage"], "complete")
        for role, pointers in groups.items():
            for pointer in pointers.values():
                _, value = _resolve_pointer(view, pointer)
                quote = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote, role=role)], view)
                if role in {"executor_record", "declaration", "producer_output"}:
                    with self.assertRaisesRegex(LLMResponseError, "role is"):
                        validate_evidence_quotes([ReportEvidenceQuote(
                            pointer=pointer, quote=quote, role="recorded_material")], view)
        encoded = json.dumps(locator)
        for absent in ("Original quantitative statement.", "observed", "Never index a rejected allegation.", "markdown", "previous_draft"):
            self.assertNotIn(absent, encoded)
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/source_evidence/0/evidence_passages/0/text",
                quote="Original", role="recorded_material")], view)

    def test_locator_is_bounded_fair_and_does_not_make_omitted_fields_unavailable(self):
        view = {"source_evidence": [{"metadata": {"evidence_passages": [
            {"text": f"Retained passage {i}"} for i in range(100)]}}],
            "execution_evidence": {"execution_records": [{"cwd": "observed"}]}, "objective": "goal"}
        locator = review_evidence_locator(view, max_entries=3)
        self.assertEqual(locator["listed_scalar_count"], 3)
        self.assertEqual(locator["omitted_scalar_count"], 99)
        self.assertEqual(set(locator["pointers_by_role"]), {"recorded_material", "executor_record", "declaration"})
        small = review_evidence_locator(view, max_chars=500)
        self.assertLessEqual(len(json.dumps(small, ensure_ascii=False, separators=(",", ":"))), 500)
        self.assertEqual(small["coverage"], "partial")
        self.assertEqual(small["eligible_scalar_count"], 102)
        self.assertEqual(review_evidence_locator(view, max_entries=0)["listed_scalar_count"], 0)
        self.assertEqual(review_evidence_locator({})["coverage"], "complete")
        with self.assertRaisesRegex(ValueError, "coverage"):
            review_evidence_locator(view, max_chars=1)
        validate_evidence_quotes([ReportEvidenceQuote(
            pointer="/source_evidence/0/metadata/evidence_passages/99/text", quote="Retained passage 99",
            role="recorded_material")], view)

    def test_locator_cannot_cite_itself_and_source_quotations_still_require_correct_ownership(self):
        view = {"source_evidence": [{"summary": "A prior interpretation.", "metadata": {
            "evidence_passages": [{"text": "Original result."}]}}]}
        locator = review_evidence_locator(view)
        view["evidence_locator"] = locator
        self.assertEqual(review_evidence_locator(view), locator)
        self.assertIn("/source_evidence/0/summary", locator["pointers_by_role"]["derived_context"].values())
        with self.assertRaisesRegex(LLMResponseError, "supplied evidence"):
            validate_evidence_quotes([ReportEvidenceQuote(
                pointer="/evidence_locator/pointers_by_role/recorded_material/recorded_material:0",
                quote="/source_evidence/0/metadata/evidence_passages/0/text", role="recorded_material")], view)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/source_evidence/0/summary",
                quote="A prior interpretation.", role="recorded_material")], view)

    def test_document_and_section_reviews_share_actual_locator_with_strict_validation(self):
        from simple_ar.report.agent import _reviewer_context
        context, memory, config, draft = self.objects()
        passage = {"handle": "paper:p", "kind": "paper", "metadata": {
            "reading_artifact": "reading/p.json", "reading_notes": {"method": "Prior interpretation."},
            "bibliography": {"title": "Recorded title"}, "evidence_passages_truncated": True,
            "evidence_passages": [{"text": "Actual retained result."}]}}
        original = json.dumps(passage)
        from simple_ar.report.schema import SourceHandle
        memory.source_handles = [SourceHandle.model_validate(passage)]
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        template = load_report_template_bundle(report_mode="experiment", config=config)
        review_document(client=Client(), template=template, memory=memory, sections=[draft], config=config,
            execution_summary={}, metric_summary={}, source_evidence=[passage])
        document = captured[0]
        section = _reviewer_context(context=context, memory=memory, template=template,
            section=memory.section_plan[0], draft=draft, config=config)
        for view, root in ((document, "source_evidence"), (section, "allowed_sources")):
            metadata = view[root][0]["metadata"]
            self.assertNotIn("reading_notes", metadata)
            self.assertEqual(len(metadata["evidence_passages"]), len(passage["metadata"]["evidence_passages"]))
            for actual, original_passage in zip(metadata["evidence_passages"], passage["metadata"]["evidence_passages"]):
                self.assertEqual({key: actual[key] for key in original_passage}, original_passage)
            self.assertEqual(metadata["bibliography"]["title"], "Recorded title")
            self.assertTrue(metadata["evidence_passages_truncated"])
            self.assertTrue(metadata["derived_reading_notes_on_request"]["omitted_from_this_view"])
            pointer = f"/{root}/0/metadata/evidence_passages/0/text"
            self.assertIn(pointer, view["evidence_locator"]["pointers_by_role"]["recorded_material"].values())
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="Actual retained result.",
                role="recorded_material")], view)
        self.assertEqual(document["source_evidence"][0]["metadata"]["bibliography"], passage["metadata"]["bibliography"])
        self.assertEqual(json.dumps(passage), original)
        self.assertEqual(memory.source_handles[0].metadata, passage["metadata"])

    def test_locator_does_not_cut_evidence_or_reject_an_existing_near_limit_request(self):
        context, memory, config, draft = self.objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append((prompt, json.loads(prompt)))
                return {"section_reviews": []}
        kwargs = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        review_document(**kwargs)
        original = dict(captured[-1][1])
        original.pop("evidence_locator")
        base_size = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")))
        for extra in (0, 150, 350):
            with self.subTest(extra=extra):
                config.max_document_review_prompt_chars = base_size + extra
                review_document(**kwargs)
                prompt, view = captured[-1]
                self.assertLessEqual(len(prompt), base_size + extra)
                locator = view.pop("evidence_locator", None)
                self.assertEqual(view, original)
                if extra == 0:
                    self.assertIsNone(locator)
                elif extra == 150:
                    self.assertEqual(locator["coverage"], "not_listed")

    def test_document_review_accepts_current_heading_quotes_not_frozen_or_joined_fields(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Current Misspelled Heading"
        quote = {"section_id": "setup", "quote": draft.heading}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "warning", "findings": [{
                    "finding_id": "heading", "type": "style", "severity": "minor", "required_action": "revise",
                    "message": "Fix the current heading.", "draft_quotes": [quote]}]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        self.assertEqual(review_document(**common)[0].findings[0].draft_quotes[0].quote, draft.heading)
        for absent in ("Setup", "Current Misspelled", draft.heading + "\n" + draft.draft_markdown, "A superseded heading"):
            quote["quote"] = absent
            with self.assertRaisesRegex(LLMResponseError, "absent"):
                review_document(**common)

    def test_historical_heading_check_uses_current_heading_and_keeps_body_compatible(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Corrected Current Heading"
        old = ReviewerFinding(finding_id="heading", section_id="setup", type="style", severity="minor",
            message="An old heading opinion.")
        quoted = [draft.heading]
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "pass", "finding_checks": [{
                    "finding_id": old.finding_id, "status": "resolved", "explanation": "The current title is corrected.",
                    "draft_quotes": quoted}]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft], historical_findings=[old]))
        self.assertEqual(review_document(**common)[0].finding_checks[0].draft_quotes, quoted)
        quoted[0] = draft.draft_markdown
        self.assertEqual(len(review_document(**common)), 1)
        quoted[0] = "Setup"
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            review_document(**common)

    def test_section_review_quotes_candidate_heading_not_plan_or_previous_heading(self):
        context, memory, config, draft = self.objects()
        draft.heading = "Current Candidate Title"
        quote = {"section_id": "setup", "quote": draft.heading}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_id": "setup", "verdict": "warning", "findings": [{"finding_id": "heading",
                    "type": "style", "severity": "minor", "message": "A heading check.", "draft_quotes": [quote]}]}
        common = dict(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            section=memory.section_plan[0], draft=draft, label="candidate-heading", extra_context=[])
        self.assertEqual(len(_section_response(**common).findings), 1)
        quote["quote"] = memory.section_plan[0].heading
        with self.assertRaisesRegex(LLMError, "absent"):
            _section_response(**common)

    def test_neighbor_quote_sources_are_separate_visible_windows_and_candidate_wins(self):
        from simple_ar.report.review_evidence import review_draft_quote_sources, draft_quote_present
        view = {"document_plan": {"sections": [{"section_id": "neighbor", "heading": "Frozen old heading"}]},
            "narrative_context": {"adopted_sections": [{"section_id": "neighbor", "heading": "Visible heading",
                "prose_windows": [{"text": "Visible head."}, {"text": "Visible tail."}],
                "table_excerpt": {"windows": [{"text": "| observed | 3 |"}]}},
                {"section_id": "candidate", "heading": "Superseded same-ID heading", "prose_windows": [{"text": "Old body"}]}]},
            "draft": {"section_id": "candidate", "heading": "Candidate heading", "draft_markdown": "Candidate body"}}
        sources = review_draft_quote_sources(view)
        for quote in ("Visible heading", "Visible head.", "Visible tail.", "| observed | 3 |"):
            self.assertTrue(draft_quote_present(sources, "neighbor", quote))
        for quote in ("Frozen old heading", "Visible headin", "Hidden middle.", "Visible head.Visible tail.", "Visible heading\nVisible head."):
            self.assertFalse(draft_quote_present(sources, "neighbor", quote))
        self.assertTrue(draft_quote_present(sources, "candidate", "Candidate heading"))
        self.assertFalse(draft_quote_present(sources, "candidate", "Superseded same-ID heading"))
        self.assertFalse(draft_quote_present(sources, "missing", "Candidate body"))

    def test_delivery_counts_and_assembly_prose_have_derived_not_scientific_ownership(self):
        view = {"length_observation": {"known_delivery_markdown_tokens": 536},
            "narrative_context": {"length_observation": {"adopted_token_count": 423}},
            "assembly_owned_content": [{"markdown": "Rechecked supplied rows; validity not verified."}]}
        for pointer, quote in (("/length_observation/known_delivery_markdown_tokens", "536"),
                ("/narrative_context/length_observation/adopted_token_count", "423"),
                ("/assembly_owned_content/0/markdown", "validity not verified")):
            reference = ReportEvidenceQuote(pointer=pointer, quote=quote, role="derived_context")
            validate_evidence_quotes([reference], view)
            with self.assertRaisesRegex(LLMResponseError, "derived_context"):
                validate_evidence_quotes([reference.model_copy(update={"role": "registered_result"})], view)

    def test_derived_delivery_count_supports_style_not_definite_metric_counter_evidence(self):
        from simple_ar.report.review_evidence import validate_finding_anchors
        view = {"length_observation": {"known_delivery_markdown_tokens": 536}}
        finding = ReviewerFinding(finding_id="length", section_id="conclusion", type="style", severity="major",
            required_action="revise", message="Known added content exceeds requested length.",
            evidence_quotes=[ReportEvidenceQuote(pointer="/length_observation/known_delivery_markdown_tokens", quote="536", role="derived_context")])
        review = ReportSectionReview(section_id="conclusion", verdict="revise_required", findings=[finding])
        validate_finding_anchors(review, {"conclusion": "Bounded observations."}, view)
        finding.type = "metric_mismatch"
        finding.draft_quotes = [ReportDraftQuote(section_id="conclusion", quote="Bounded observations.")]
        with self.assertRaisesRegex(LLMResponseError, "non-derived"):
            validate_finding_anchors(review, {"conclusion": "Bounded observations."}, view)

    def test_revision_counts_and_edit_scope_are_derived_not_metric_counter_evidence(self):
        view = {"revision_context": {"length_observation": {"candidate_minus_baseline_tokens": 3}},
            "edit_scope": {"writer_call_unit": "One selected section only"}}
        references = [ReportEvidenceQuote(pointer="/revision_context/length_observation/candidate_minus_baseline_tokens",
            quote="3", role="derived_context"), ReportEvidenceQuote(pointer="/edit_scope/writer_call_unit",
            quote="One selected section", role="derived_context")]
        validate_evidence_quotes(references, view)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([references[0].model_copy(update={"role": "registered_result"})], view)
        from simple_ar.report.review_evidence import validate_finding_anchors
        finding = ReviewerFinding(finding_id="length", section_id="setup", type="style", severity="major",
            required_action="revise", message="Length grew during requested trimming.", evidence_quotes=references)
        review = ReportSectionReview(section_id="setup", verdict="revise_required", findings=[finding])
        validate_finding_anchors(review, {"setup": "Observed only."}, view)
        finding.type = "metric_mismatch"
        finding.draft_quotes = [ReportDraftQuote(section_id="setup", quote="Observed only.")]
        with self.assertRaisesRegex(LLMResponseError, "non-derived"):
            validate_finding_anchors(review, {"setup": "Observed only."}, view)

    def objects(self):
        context = ReportContext(topic="Conditions and observations", report_mode="experiment",
            execution_context="Requested device CPU.", results={"execution_record": {
                "invocation_id": "run-1", "cwd": "/actual-copy", "duration_sec": 12.5,
                "observation_status": "executor_recorded"}})
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="setup", heading="Setup", goal="Conditions")])
        config = ReportRuntimeConfig(max_review_iterations=1)
        draft = ReportSectionDraft(section_id="setup", heading="Setup", draft_markdown="The executor recorded /actual-copy.")
        return context, memory, config, draft

    def view(self):
        return {"execution_evidence": {"declared_execution_context": {"text": "Requested /original-copy."},
            "execution_records": [{"cwd": "/actual-copy", "duration_sec": 12.5}],
            "output_evidence": [{"preview": {"text": "Producer elapsed 12.4."}}]},
            "source_evidence": [{"metadata": {"evidence_passages": [{"text": "Distances beyond 22 are a terminal bin."}],
                "reading_notes": {"method_summary": "A recorded interpretation."}}}],
            "metric_sources": {"rows": [["coverage", 0.91]]},
            "extra_tool_context": [{"tool_name": "get_synthesis_brief", "content": {"text": "Unverified interpretation."}}]}

    def test_bad_pointer_changed_quote_container_and_partial_numeric_value_are_rejected(self):
        view = self.view()
        for pointer, quote in (
            ("/execution_evidence/execution_records/9/cwd", "/actual-copy"),
            ("/execution_evidence/execution_records/0/cwd", "/original-copy"),
            ("/execution_evidence/execution_records/0", "cwd"),
            ("/execution_evidence/execution_records/0/duration_sec", "12"),
            ("/execution_evidence/execution_records/-1/cwd", "/actual-copy"),
            ("file:///somewhere", "/actual-copy"),
            ("/sections/0/markdown", "a prior opinion"),
            ("/execution_evidence/bad~2escape", "value"),
        ):
            with self.assertRaises(LLMResponseError):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote=quote,
                    role="executor_record")], view)
        validate_evidence_quotes([ReportEvidenceQuote(pointer="/execution_evidence/execution_records/0/duration_sec",
            quote="12.5", role="executor_record")], view)

    def test_invalid_location_explains_available_structure_without_guessing_a_reference(self):
        view = self.view()
        reference = ReportEvidenceQuote(pointer="/execution_records/0/cwd", quote="/actual-copy", role="executor_record")
        with self.assertRaisesRegex(LLMResponseError, 'Available keys at /:.*execution_evidence'):
            validate_evidence_quotes([reference], view)
        self.assertEqual(reference.pointer, "/execution_records/0/cwd")
        reference.pointer = "/execution_evidence/execution_records/8/cwd"
        with self.assertRaisesRegex(LLMResponseError, 'has 1 elements'):
            validate_evidence_quotes([reference], view)
        reference.pointer = '/execution_evidence/execution_records/0'
        with self.assertRaisesRegex(LLMResponseError, r'scalar field.*Available keys at /execution_evidence/execution_records/0:.*cwd'):
            validate_evidence_quotes([reference], view)
        self.assertEqual(reference.pointer, '/execution_evidence/execution_records/0')
        reference.pointer = '/execution_evidence/execution_records'
        with self.assertRaisesRegex(LLMResponseError, r'scalar field.*has 1 elements'):
            validate_evidence_quotes([reference], view)

    def test_new_factual_findings_require_current_draft_and_located_metric_counter_evidence(self):
        context, memory, config, draft = self.objects()
        finding = {"finding_id": "mismatch", "type": "metric_mismatch", "severity": "major",
            "section_id": "setup", "message": "A specific recorded mismatch."}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "revise_required", "findings": [finding]}]}
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft]))
        with self.assertRaisesRegex(LLMResponseError, "current-draft quotation"):
            review_document(**common)
        finding["draft_quotes"] = [{"section_id": "setup", "quote": draft.draft_markdown}]
        with self.assertRaisesRegex(LLMResponseError, "counter-evidence"):
            review_document(**common)
        finding["evidence_quotes"] = [{"pointer": "/metric_sources/duration", "quote": "12.5", "role": "registered_result"}]
        common["metric_summary"] = {"duration": 12.5}
        self.assertEqual(len(review_document(**common)[0].findings), 1)
        finding["draft_quotes"][0]["quote"] = "A superseded draft."
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            review_document(**common)

    def test_historical_check_validates_source_location_and_cannot_relabel_declared_as_observed(self):
        context, memory, config, draft = self.objects()
        old = ReviewerFinding(finding_id="old-cwd", section_id="setup", type="evidence_gap", severity="major",
            message="An earlier claim about the execution directory.")
        reference = {"pointer": "/execution_evidence/declared_execution_context/text",
            "quote": "Requested device CPU.", "role": "executor_record"}
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [{"section_id": "setup", "verdict": "pass", "finding_checks": [{
                    "finding_id": old.finding_id, "status": "not_applicable", "explanation": "The literal role differs.",
                    "draft_quotes": [draft.draft_markdown], "evidence_quotes": [reference]}]}]}
        from simple_ar.report.execution_evidence import report_execution_evidence
        common = dict(client=Client(), **document_review_inputs(memory, config, [draft],
            execution_evidence=report_execution_evidence(context), historical_findings=[old]))
        with self.assertRaisesRegex(LLMResponseError, "declaration, not executor_record"):
            review_document(**common)
        reference.update(pointer="/execution_evidence/execution_records/0/cwd", quote="/actual-copy")
        check = review_document(**common)[0].finding_checks[0]
        self.assertEqual(check.evidence_quotes[0].role, "executor_record")

    def test_section_evidence_recheck_uses_one_addressable_payload_and_actual_tools(self):
        context, memory, config, draft = self.objects()
        tools = [ReportToolResult(tool_name="get_neighbor_chunks", content={"chunks": [{"text": "Current original passage."}]})]
        seen = []
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                view, end = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])
                self_view = prompt[prompt.index('{'):]
                assert not self_view[end:].strip(), "No second JSON evidence payload"
                seen.append(view)
                return {"section_id": "setup", "verdict": "revise_required", "findings": [{
                    "finding_id": "gap", "type": "evidence_gap", "severity": "major",
                    "message": "A bounded source question remains.", "draft_quotes": [{"section_id": "setup", "quote": draft.draft_markdown}],
                    "evidence_quotes": [{"pointer": "/extra_tool_context/0/content/chunks/0/text",
                        "quote": "Current original passage.", "role": "recorded_material"}]}]}
        review = _section_response(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            section=memory.section_plan[0], draft=draft, label="section-recheck", extra_context=tools)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["extra_tool_context"][0]["content"], tools[0].content)
        self.assertEqual(review.findings[0].evidence_quotes[0].role, "recorded_material")

    def test_old_brief_cards_stay_out_of_cold_review_but_explicit_requests_can_read_them(self):
        context, memory, config, draft = self.objects()
        source = {"handle": "paper:methods", "kind": "paper", "metadata": {
            "reading_notes": {"method": "An earlier interpretation."},
            "evidence_passages": [{"text": "A literal original method passage."}]}}
        brief = ReportToolResult(tool_name="get_paper_brief", content={"handles": [source]})
        before = brief.model_dump(mode="json")
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        for requested in (None, [brief]):
            review_document(client=Client(), **document_review_inputs(memory, config, [draft],
                supplementary_evidence=[brief], requested_context=requested))
        cold = captured[0]["supplementary_evidence"][0]["content"]["handles"][0]["metadata"]
        requested = captured[1]["supplementary_evidence"][0]["content"]["handles"][0]["metadata"]
        self.assertNotIn("reading_notes", cold)
        self.assertIn("reading_notes", requested)
        self.assertIn("An earlier interpretation.", json.dumps(requested))
        self.assertEqual(brief.model_dump(mode="json"), before)

    def test_section_review_and_format_recovery_keep_primary_evidence_not_prior_cards(self):
        import copy
        from simple_ar.report.narrative import _prompt_handle_view
        context, memory, config, draft = self.objects()
        source = SourceHandle(handle="paper:methods", kind="paper", metadata={
            "reading_notes": {"method": "An earlier interpretation.", "reading_coverage": {
                "available_chunks": 20, "shown_chunk_ids": ["method-1"], "semantic_verification": "not_performed"}},
            "reading_artifact": "reading/methods.json", "reading_notes_truncated": True,
            "evidence_passages": [{"chunk_id": "method-1", "text": "The final bin also includes longer distances."}],
            "evidence_passages_truncated": True, "extraction_status": "parsed"})
        memory.source_handles = [source]
        memory.section_plan[0].evidence_handles = [source.handle]
        before = copy.deepcopy(memory.model_dump(mode="json"))
        seen = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = prompt_payload(prompt)
                seen.append(view)
                if len(seen) == 1:
                    raise LLMResponseError("Malformed outer response")
                return {"section_id": draft.section_id, "verdict": "pass"}
        _section_response(client=Client(), context=context,
            template=load_report_template_bundle(report_mode=context.report_mode, config=config),
            memory=memory, section=memory.section_plan[0], draft=draft, label="review-primary", config=config)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["allowed_sources"], seen[1]["allowed_sources"])
        metadata = seen[0]["allowed_sources"][0]["metadata"]
        self.assertNotIn("reading_notes", metadata)
        self.assertEqual(metadata["evidence_passages"], _prompt_handle_view(source)["metadata"]["evidence_passages"])
        self.assertTrue(metadata["evidence_passages_truncated"])
        marker = metadata["derived_reading_notes_on_request"]
        self.assertEqual(marker["reading_coverage"], source.metadata["reading_notes"]["reading_coverage"])
        self.assertTrue(marker["omitted_from_this_view"])
        self.assertEqual(marker["independent_verification"], "not_performed")
        self.assertEqual(memory.model_dump(mode="json"), before)

    def test_writer_keeps_cards_but_reviewer_must_explicitly_request_them(self):
        from simple_ar.report.agent import _reviewer_context, _writer_recovery_prompt
        context, memory, config, draft = self.objects()
        source = SourceHandle(handle="paper:legacy", kind="paper", metadata={
            "reading_notes": {"method": "Earlier, unverified interpretation."},
            "evidence_passages": [{"text": "Original qualifier contradicts that interpretation."}]})
        memory.source_handles = [source]
        memory.section_plan[0].evidence_handles = [source.handle]
        section = memory.section_plan[0]
        template = load_report_template_bundle(report_mode=context.report_mode, config=config)
        writer = json.loads(_writer_recovery_prompt(context=context, memory=memory, section=section,
            config=config, extra_context=[], previous_draft=None, review=None, draft_mode="section").split("\n\n", 1)[1])
        reviewer = _reviewer_context(context=context, memory=memory, section=section,
            draft=draft, template=template, config=config)
        self.assertIn("reading_notes", writer["source_handles"][0]["metadata"])
        self.assertNotIn("reading_notes", reviewer["allowed_sources"][0]["metadata"])
        brief = ReportToolResult(tool_name="get_paper_brief", content={"handles": [source.model_dump(mode="json")]})
        explicit = _reviewer_context(context=context, memory=memory, section=section,
            draft=draft, template=template, config=config, extra_context=[brief])
        pointer = "/extra_tool_context/0/content/handles/0/metadata/reading_notes/method"
        validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer,
            quote=source.metadata["reading_notes"]["method"], role="derived_context")], explicit)
        with self.assertRaisesRegex(LLMResponseError, "derived_context"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer,
                quote=source.metadata["reading_notes"]["method"], role="recorded_material")], explicit)
        self.assertEqual(brief.content["handles"][0], source.model_dump(mode="json"))

    def test_primary_projection_preserves_legacy_records_and_does_not_rebind_old_note_pointers(self):
        import copy
        from simple_ar.report.narrative import review_source_evidence
        source = {"handle": "paper:legacy", "metadata": {"reading_notes": {
            "method": "Old reading opinion.", "reading_coverage": {"available_chunks": 9}},
            "evidence_passages": [{"text": "Original condition."}], "custom_metadata": {"owner": "user"}}}
        before = copy.deepcopy(source)
        view = {"allowed_sources": review_source_evidence([source])}
        self.assertEqual(review_source_evidence(view["allowed_sources"]), view["allowed_sources"])
        self.assertEqual(view["allowed_sources"][0]["metadata"]["custom_metadata"], {"owner": "user"})
        original = ReportEvidenceQuote(pointer="/allowed_sources/0/metadata/evidence_passages/0/text",
            quote="Original condition.", role="recorded_material")
        validate_evidence_quotes([original], view)
        with self.assertRaisesRegex(LLMResponseError, "absent"):
            validate_evidence_quotes([ReportEvidenceQuote(pointer="/allowed_sources/0/metadata/reading_notes/method",
                quote="Old reading opinion.", role="derived_context")], view)
        for root in ("allowed_sources", "source_evidence"):
            current = {root: view["allowed_sources"]}
            pointer = f"/{root}/0/metadata/derived_reading_notes_on_request/reading_coverage/available_chunks"
            validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="9", role="derived_context")], current)
            with self.assertRaisesRegex(LLMResponseError, "derived_context"):
                validate_evidence_quotes([ReportEvidenceQuote(pointer=pointer, quote="9", role="recorded_material")], current)
        self.assertEqual(source, before)
        atypical = {"metadata": {"reading_notes": "Unstructured legacy input", "evidence_passages": []}}
        self.assertEqual(review_source_evidence([atypical]), [atypical])

    def test_legacy_empty_fields_do_not_change_saved_shapes(self):
        finding = ReviewerFinding(finding_id="old", type="style", message="Recorded old issue.")
        check = ReportFindingCheck(finding_id="old", status="unresolved", explanation="Unknown.")
        self.assertNotIn("draft_quotes", finding.model_dump(mode="json"))
        self.assertNotIn("evidence_quotes", finding.model_dump(mode="json"))
        self.assertNotIn("evidence_quotes", check.model_dump(mode="json"))


if __name__ == "__main__":
    unittest.main()
