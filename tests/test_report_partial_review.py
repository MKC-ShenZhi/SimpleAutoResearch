"""A local bad observation must not erase valid peers or certify a batch."""
import copy
import json
import unittest

from pydantic import ValidationError

from tests.report_review_fixtures import finding_check_objects, prompt_payload
from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.agent import run_report_agent
from simple_ar.report.editor import _validate_document_reviews, _validated_document_review_subset
from simple_ar.report.tool_gateway import ReportToolGateway


class PartialReviewTests(unittest.TestCase):
    def mixed(self):
        good = {"finding_id": "qualified-observation", "section_id": "scope",
            "type": "unsupported_claim", "severity": "major", "required_action": "revise",
            "message": "Keep the observed uncertainty explicit.",
            "draft_quotes": [{"section_id": "scope", "quote": "The identity was not independently verified."}]}
        bad = {"finding_id": "fabricated-heading", "section_id": "scope",
            "type": "style", "severity": "minor", "required_action": "revise",
            "message": "Rewrite a misspelled heading.",
            "draft_quotes": [{"section_id": "scope", "quote": "An invented truncated heading"}]}
        return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
            "findings": [good, bad], "revision_instructions": ["Follow the invented heading opinion."]}]}

    def view(self, drafts):
        return {"sections": [{"section_id": row.section_id, "heading": row.heading,
                              "markdown": row.draft_markdown} for row in drafts],
                "verified_execution_results": {"metrics": {"score": 5}}}

    def test_subset_keeps_strict_evidence_checks_raw_bytes_and_no_parent_instructions(self):
        _, _, _, drafts = finding_check_objects(historical=False)
        response = self.mixed()
        metric = {"finding_id": "wrong-score", "section_id": "scope", "type": "metric_mismatch",
            "severity": "major", "required_action": "revise", "message": "Observed score disagrees.",
            "draft_quotes": [{"section_id": "scope", "quote": drafts[0].draft_markdown}],
            "evidence_quotes": [{"pointer": "/verified_execution_results/metrics/score",
                                 "quote": "5", "role": "registered_result"}]}
        response["section_reviews"][0]["findings"].extend([metric, {**metric, "finding_id": "bad-metric",
            "evidence_quotes": [{"pointer": "/verified_execution_results/metrics/score",
                                 "quote": "6", "role": "registered_result"}]}])
        before = copy.deepcopy(response)
        with self.assertRaises(LLMResponseError):
            _validate_document_reviews(response, sections=drafts, prior_by_key={}, evidence_view=self.view(drafts))
        subset = _validated_document_review_subset(response, sections=drafts, prior_by_key={}, evidence_view=self.view(drafts))
        self.assertEqual([f.finding_id for f in subset[0].findings], ["qualified-observation", "wrong-score"])
        self.assertEqual(subset[0].verdict, "revise_required")
        self.assertFalse(subset[0].revision_instructions)
        self.assertEqual(subset[0].findings[1].evidence_quotes[0].quote, "5")
        self.assertEqual(response, before)

    def test_complete_and_partial_review_share_identity_and_role_rejections(self):
        _, _, old, drafts = finding_check_objects()
        valid = self.mixed()["section_reviews"][0]
        check = {"finding_id": old.finding_id, "status": "resolved", "explanation": "Current uncertainty is explicit.",
                 "draft_quotes": [drafts[0].draft_markdown]}
        cases = [([valid, valid], {}),
                 ([{**valid, "findings": [valid["findings"][0]] * 2}], {}),
                 ([valid, {"section_id": "unknown"}], {}),
                 ([{**valid, "findings": [{**valid["findings"][0], "section_id": "limits"}]}], {}),
                 ([{**valid, "section_id": []}], {}),
                 ([{**valid, "findings": "not a list"}], {}),
                 ([{**valid, "findings": [None]}], {}),
                 ([{**valid, "finding_checks": None}], {}),
                 ([{**valid, "context_requests": "not a list"}], {}),
                 ([{**valid, "finding_checks": [{**check, "finding_id": "foreign"}]}], {}),
                 ([{"section_id": "scope", "finding_checks": [check, check]}], {("scope", old.finding_id): old}),
                 ([valid], {("scope", old.finding_id): old}),
                 ([{"section_id": "scope", "finding_checks": [check], "revision_instructions": ["Rewrite."]}],
                  {("scope", old.finding_id): old}),
                 ([{"section_id": "limits", "finding_checks": []}], {("scope", old.finding_id): old})]
        for rows, prior in cases:
            with self.subTest(rows=rows):
                response = {"section_reviews": rows}
                before = copy.deepcopy(response)
                with self.assertRaises((LLMResponseError, ValidationError)):
                    _validate_document_reviews(response, sections=drafts,
                        prior_by_key=prior, evidence_view=self.view(drafts))
                self.assertEqual(_validated_document_review_subset(response,
                    sections=drafts, prior_by_key=prior, evidence_view=self.view(drafts)), [])
                self.assertEqual(response, before)

    def test_complete_and_partial_review_bind_legacy_finding_target_on_copies(self):
        _, _, old, drafts = finding_check_objects()
        finding = self.mixed()["section_reviews"][0]["findings"][0]
        for target in (None, ""):
            response = {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                "findings": [{**finding, "section_id": target}]}]}
            before = copy.deepcopy(response)
            reviews = _validate_document_reviews(response, sections=drafts, prior_by_key={}, evidence_view=self.view(drafts))
            self.assertEqual(reviews[0].findings[0].section_id, "scope")
            self.assertEqual(_validated_document_review_subset(response, sections=drafts,
                prior_by_key={}, evidence_view=self.view(drafts)), reviews)
            self.assertEqual(response, before)
        # Full validation retains legacy instruction coercion; salvage must
        # not release raw instructions from a rejected historical response.
        response = {"section_reviews": [{"section_id": "scope", "verdict": "pass",
            "revision_instructions": [" "]}]}
        prior = {("scope", old.finding_id): old}
        self.assertTrue(_validate_document_reviews(response, sections=drafts, prior_by_key=prior))
        self.assertEqual(_validated_document_review_subset(response, sections=drafts,
            prior_by_key=prior, evidence_view=self.view(drafts)), [])

    def test_historical_subset_closes_only_exact_valid_opinions(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        other = old.model_copy(update={"finding_id": "other-opinion"})
        for field in (checkpoint["memory"], checkpoint):
            field["reviewer_findings"].append(other.model_dump(mode="json"))
        response = {"section_reviews": [{"section_id": "scope", "verdict": "pass", "finding_checks": [
            {"finding_id": old.finding_id, "status": "resolved", "explanation": "Current draft states uncertainty.",
             "draft_quotes": [drafts[0].draft_markdown]},
            {"finding_id": other.finding_id, "status": "resolved", "explanation": "A fabricated quote cannot close it.",
             "draft_quotes": ["This quote is absent."]}]}]}
        labels, saved = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                return response if label.startswith("report-document-finding-checker") else {"section_reviews": []}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        current = {f.finding_id for f in result.memory.reviewer_findings}
        self.assertNotIn(old.finding_id, current)
        self.assertIn(other.finding_id, current)
        self.assertIn("document-finding-check-unavailable", current)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
                                  "report-document-finding-checker-format-correction"])
        rejections = [event for event in result.iterations if event.action == "document_finding_check_rejected"]
        self.assertEqual(len(rejections), 2)
        self.assertEqual([event.rejected_review["response"] for event in rejections], [response, response])
        self.assertFalse(rejections[0].rejected_review["partial_ready"])
        self.assertTrue(rejections[1].rejected_review["partial_ready"])
        self.assertTrue(saved[-1]["document_review_done"])
        before = len(labels)
        restored = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(len(labels), before)
        self.assertEqual(restored.memory.reviewer_findings, result.memory.reviewer_findings)

    def test_valid_subcontract_reaches_writer_but_bad_record_and_instructions_do_not(self):
        for invalid_final in (False, True):
            with self.subTest(invalid_final=invalid_final):
                kwargs, checkpoint, _, drafts = finding_check_objects(historical=False)
                kwargs["config"].max_review_iterations = 1
                response, labels, saved, requests = self.mixed(), [], [], []
                test = self
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        if label.startswith("report-document-reviewer"):
                            return response
                        if label == "report-document-reviser-scope":
                            view = prompt_payload(prompt)
                            requests.append(view)
                            test.assertEqual([f["finding_id"] for f in view["review_findings"]], ["qualified-observation"])
                            test.assertNotIn("Follow the invented heading opinion.", prompt)
                            return {"section_id": "scope", "draft_markdown": drafts[0].draft_markdown + " No identity conclusion is drawn."}
                        if label == "report-document-verifier-scope":
                            return {"section_id": "scope", "verdict": "pass"}
                        if label.startswith("report-document-verifier"):
                            return response if invalid_final else {"section_reviews": []}
                        raise AssertionError(label)
                result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
                self.assertEqual(len(requests), 1)
                self.assertEqual(result.sections[1], drafts[1])
                self.assertNotEqual(result.sections[0], drafts[0])
                self.assertTrue(next(event for event in result.iterations if event.action == "document_revise").adopted)
                current = {f.finding_id for f in result.memory.reviewer_findings}
                self.assertIn("document-review-unavailable", current)
                self.assertNotIn("fabricated-heading", current)
                if invalid_final:
                    self.assertIn("document-recheck-unavailable", current)
                    self.assertIn("qualified-observation", current)
                else:
                    self.assertNotIn("qualified-observation", current)
                self.assertEqual(labels.count("report-document-reviewer-format-correction"), 1)
                self.assertEqual(labels.count("report-document-reviser-scope"), 1)
                self.assertTrue(saved[-1]["document_review_done"])
                before = len(labels)
                restored = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(len(labels), before)
                self.assertEqual(restored.sections, result.sections)

    def test_interruption_at_rejected_subset_never_repeats_consumed_correction(self):
        for rejection_number in (1, 2):
            with self.subTest(rejection_number=rejection_number):
                kwargs, checkpoint, _, drafts = finding_check_objects(historical=False)
                labels, saved = [], []
                response = self.mixed()
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        if not label.startswith("report-document-reviewer"):
                            raise AssertionError(label)
                        return response
                def sink(value):
                    saved.append(copy.deepcopy(value))
                    if sum(event["action"] == "document_review_rejected" for event in value["iterations"]) == rejection_number:
                        raise RuntimeError("Interrupted after rejection checkpoint")
                with self.assertRaisesRegex(RuntimeError, "rejection checkpoint"):
                    run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                        completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
                before = len(labels)
                result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(labels[before:], ["report-document-reviewer-format-correction"] if rejection_number == 1 else [])
                self.assertEqual(labels.count("report-document-reviewer-format-correction"), 1)
                self.assertEqual(result.sections, drafts)
                self.assertIn("qualified-observation", {f.finding_id for f in result.memory.reviewer_findings})
                self.assertIn("document-review-unavailable", {f.finding_id for f in result.memory.reviewer_findings})

    def test_envelope_role_violation_does_not_release_valid_historical_check(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        response = self.mixed()
        response["section_reviews"][0]["finding_checks"] = [{"finding_id": old.finding_id,
            "status": "resolved", "explanation": "Valid quote but forbidden discovery in same batch.",
            "draft_quotes": [drafts[0].draft_markdown]}]
        labels = []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                return response if label.startswith("report-document-finding-checker") else {"section_reviews": []}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertNotIn("qualified-observation", {f.finding_id for f in result.memory.reviewer_findings})
        self.assertFalse(any(event.rejected_review.get("partial_ready") for event in result.iterations))
        self.assertEqual(len(labels), 3)

    def test_transport_failure_after_first_subset_does_not_spend_it_as_complete(self):
        kwargs, checkpoint, _, drafts = finding_check_objects(historical=False)
        labels, response = [], self.mixed()
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label.endswith("format-correction"):
                    raise LLMError("Unknown provider outcome; do not retry")
                return response
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(len(labels), 2)
        self.assertEqual(result.sections, drafts)
        self.assertNotIn("qualified-observation", {f.finding_id for f in result.memory.reviewer_findings})
        self.assertIn("document-review-unavailable", {f.finding_id for f in result.memory.reviewer_findings})

    def test_valid_other_section_does_not_certify_failed_peer(self):
        _, _, _, drafts = finding_check_objects(historical=False)
        response = self.mixed()
        response["section_reviews"].append({"section_id": "limits", "verdict": "pass"})
        subset = _validated_document_review_subset(response, sections=drafts, prior_by_key={}, evidence_view=self.view(drafts))
        self.assertEqual([(row.section_id, row.verdict) for row in subset], [("scope", "revise_required"), ("limits", "pass")])
        # Multiple independent observations may share a section; only reused
        # identities make the envelope ambiguous, not the shared target itself.
        peer = copy.deepcopy(response["section_reviews"][0])
        peer["findings"] = [{**peer["findings"][0], "finding_id": "independent-peer"}]
        peer["revision_instructions"] = []
        response["section_reviews"].append(peer)
        subset = _validated_document_review_subset(response, sections=drafts, prior_by_key={}, evidence_view=self.view(drafts))
        self.assertEqual([f.finding_id for f in subset[0].findings], ["qualified-observation", "independent-peer"])
        self.assertFalse(subset[0].revision_instructions)

    def test_partial_historical_resume_preserves_closure_and_failed_peer(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        other = old.model_copy(update={"finding_id": "unchecked-peer"})
        for field in (checkpoint["memory"], checkpoint):
            field["reviewer_findings"].append(other.model_dump(mode="json"))
        response = {"section_reviews": [{"section_id": "scope", "verdict": "pass", "finding_checks": [
            {"finding_id": old.finding_id, "status": "resolved", "explanation": "Uncertainty is explicit.",
             "draft_quotes": [drafts[0].draft_markdown]},
            {"finding_id": other.finding_id, "status": "resolved", "explanation": "Bad quote must stay rejected.",
             "draft_quotes": ["Not present."]}]}]}
        labels, saved = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                return response if label.startswith("report-document-finding-checker") else {"section_reviews": []}
        def sink(value):
            saved.append(copy.deepcopy(value))
            if any(event.get("rejected_review", {}).get("partial_ready") for event in value["iterations"]):
                raise RuntimeError("Interrupted before applying partial checks")
        with self.assertRaisesRegex(RuntimeError, "partial checks"):
            run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
        before = len(labels)
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(len(labels), before)
        current = {f.finding_id for f in result.memory.reviewer_findings}
        self.assertNotIn(old.finding_id, current)
        self.assertIn(other.finding_id, current)
        self.assertIn("document-finding-check-unavailable", current)

    def test_partial_review_keeps_original_two_target_allowance(self):
        kwargs, checkpoint, _, drafts = finding_check_objects(historical=False)
        third_plan = kwargs["memory"].section_plan[-1].model_copy(update={"section_id": "third", "heading": "third",
            "draft_order": 3, "final_order": 3})
        kwargs["memory"].section_plan.append(third_plan)
        checkpoint["memory"]["section_plan"].append(third_plan.model_dump(mode="json"))
        third_draft = drafts[-1].model_copy(update={"section_id": "third", "heading": "third"})
        checkpoint["sections"].append(third_draft.model_dump(mode="json"))
        kwargs["config"].max_review_iterations = 1
        rows = []
        for target in ("scope", "limits", "third"):
            row = copy.deepcopy(self.mixed()["section_reviews"][0])
            row["section_id"] = target
            for finding in row["findings"]:
                finding["section_id"] = target
                finding["finding_id"] += "-" + target
                finding["draft_quotes"][0]["section_id"] = target
            rows.append(row)
        labels = []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label.startswith("report-document-reviewer"):
                    return {"section_reviews": rows}
                if label.startswith("report-document-reviser-"):
                    target = label.removeprefix("report-document-reviser-")
                    return {"section_id": target, "draft_markdown": drafts[0].draft_markdown + " No conclusion is drawn."}
                if label.startswith("report-document-verifier-"):
                    return {"section_id": label.removeprefix("report-document-verifier-"), "verdict": "pass"}
                if label == "report-document-verifier":
                    return {"section_reviews": []}
                raise AssertionError(label)
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        candidates = [event for event in result.iterations if event.action == "document_revise"]
        self.assertEqual(len(candidates), 2)
        self.assertEqual([event.section_id for event in candidates], ["scope", "limits"])
        self.assertEqual(result.sections[2], third_draft)
        self.assertIn("qualified-observation-third", {f.finding_id for f in result.memory.reviewer_findings})
        self.assertNotIn("report-document-reviser-third", labels)
