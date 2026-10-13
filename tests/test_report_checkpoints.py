"""Writer identity, adopted memory and recovery at the capability boundary."""

from dataclasses import replace
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.report_review_fixtures import draft_quotes

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.report.schema import (
    ReportContext,
    ReportMemory,
    ReportRuntimeConfig,
    ReportSectionDraft,
    ReportSectionPlan,
    SourceHandle,
    ReportToolResult,
)
from simple_ar.report.agent import run_report_agent
from simple_ar.report.editor import review_document
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.writing import ReportWritingRequest, run_report_writing_capability


class ReportCheckpointTests(unittest.TestCase):
    def test_interrupted_document_lookup_reuses_saved_explicit_context(self):
        context = ReportContext(topic="Bounded evidence", report_mode="experiment",
            synthesis_markdown="Saved derived text.")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id=sid, heading=sid,
            goal="Facts and limits") for sid in ("scope", "limits")])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)
        saved, labels = [], []
        test = self
        class Client:
            resumed = False
            def ask_json(self, system, prompt, *, label="", **kwargs):
                labels.append(label)
                if label == "report-document-reviewer":
                    test.assertFalse(self.resumed, "Resume must continue the saved evidence recheck")
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}]}]}
                if label == "report-document-reviewer-evidence":
                    briefs = [row for row in json.loads(prompt)["supplementary_evidence"]
                              if row["tool_name"] == "get_synthesis_brief"]
                    test.assertEqual(len(briefs), 1)
                    test.assertIn("Saved derived text", briefs[0]["content"]["text"])
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass"}]}
                if "reviewer" in label:
                    return {"verdict": "pass"}
                return {"section_id": next(sid for sid in ("scope", "limits") if label.endswith(sid)),
                        "draft_markdown": "Only observed conditions are supported."}
        def checkpoint(row):
            saved.append(row)
            if (row["iterations"] and row["iterations"][-1]["action"] == "document_context"
                    and row["iterations"][-1]["tool_results"][-1]["content"].get("text")):
                raise RuntimeError("Interrupted after the requested context was saved")
        client = Client()
        kwargs = dict(client=client, context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config))
        with self.assertRaisesRegex(RuntimeError, "requested context"):
            run_report_agent(**kwargs, gateway=ReportToolGateway(context), checkpoint_sink=checkpoint)
        client.resumed = True
        gateway = ReportToolGateway(context)
        gateway.call = lambda request: test.fail("Saved lookup must not consume another tool call")
        result = run_report_agent(**kwargs, gateway=gateway, completed_checkpoint=saved[-1])
        self.assertEqual(labels.count("report-document-reviewer"), 1)
        self.assertEqual(labels.count("report-document-reviewer-evidence"), 1)
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        self.assertEqual(sum(row.action == "document_context" for row in result.iterations), 1)
        self.assertFalse(result.memory.reviewer_findings)

    def test_document_owner_supplies_explicitly_requested_summary_only_to_followup(self):
        context = ReportContext(topic="Evidence comparison", report_mode="experiment",
            synthesis_markdown="A derived interpretation, not an original observation.")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id=sid, heading=sid,
            goal="Describe supplied evidence") for sid in ("scope", "limits")])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)
        calls, saved = [], []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **kwargs):
                if label == "report-document-reviewer":
                    view = json.loads(prompt)
                    test.assertFalse(any(row["tool_name"] == "get_synthesis_brief"
                        for row in view["supplementary_evidence"]))
                    calls.append(label)
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}]}]}
                if label == "report-document-reviewer-evidence":
                    view = json.loads(prompt)
                    briefs = [row for row in view["supplementary_evidence"]
                              if row["tool_name"] == "get_synthesis_brief"]
                    test.assertEqual(len(briefs), 1)
                    test.assertIn("derived interpretation", briefs[0]["content"]["text"])
                    test.assertEqual(briefs[0]["content"]["text_status"]["independent_verification"],
                                     "not_performed")
                    calls.append(label)
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass"}]}
                if "reviewer" in label:
                    return {"verdict": "pass"}
                return {"section_id": next(sid for sid in ("scope", "limits") if label.endswith(sid)),
                        "draft_markdown": "Only supplied observations are confirmed."}
        gateway = ReportToolGateway(context)
        result = run_report_agent(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=gateway, checkpoint_sink=saved.append)
        self.assertEqual(calls, ["report-document-reviewer", "report-document-reviewer-evidence"])
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        self.assertEqual(sum(row.action == "document_context" for row in result.iterations), 1)
        self.assertFalse(result.memory.reviewer_findings)
        before = len(calls)
        resumed = run_report_agent(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=ReportToolGateway(context), completed_checkpoint=saved[-1])
        self.assertEqual(resumed.report_body, result.report_body)
        self.assertEqual(len(calls), before)

    def test_document_inspection_keeps_primary_history_and_requests_derived_context(self):
        config = ReportRuntimeConfig(document_review=True)
        primary = ReportToolResult(tool_name="get_neighbor_chunks", content={"chunks": [
            {"text": "Original source conditions.", "chunk_id": "source-1"}]})
        derived = ReportToolResult(tool_name="get_synthesis_brief", content={
            "text": "Earlier derived interpretation.", "matching_handles": []})
        history = [derived, primary]
        before = [row.model_dump(mode="json") for row in history]
        captured = []
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        kwargs = dict(client=Client(), config=config,
            template=load_report_template_bundle(report_mode="survey", config=config),
            memory=ReportMemory(), sections=[ReportSectionDraft(section_id="scope", heading="Scope",
                draft_markdown="One explicitly bounded claim.")],
            execution_summary={}, metric_summary={}, supplementary_evidence=history)
        review_document(**kwargs)
        self.assertEqual([row["tool_name"] for row in captured[-1]["supplementary_evidence"]],
                         ["get_neighbor_chunks"])
        self.assertEqual(captured[-1]["historical_derived_contexts_on_request"], 1)
        review_document(**kwargs, requested_context=[derived, primary])
        visible = captured[-1]["supplementary_evidence"]
        self.assertEqual([row["tool_name"] for row in visible], ["get_neighbor_chunks", "get_synthesis_brief"])
        self.assertEqual(visible[-1]["content"]["text_status"]["independent_verification"], "not_performed")
        self.assertEqual([row.model_dump(mode="json") for row in history], before)

    def test_document_review_accepts_all_known_sections_without_opening_revisions(self):
        context = ReportContext(topic="Four-section report", report_mode="experiment")
        ids = ("scope", "method", "results", "limits")
        memory = ReportMemory(section_plan=[ReportSectionPlan(
            section_id=sid, heading=sid, goal="Describe supplied evidence") for sid in ids])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)
        labels, saved = [], []

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": sid, "verdict": "pass"} for sid in ids]}
                if "reviewer" in label:
                    return {"verdict": "pass"}
                if "reviser" in label:
                    raise AssertionError("Inspection does not authorize a rewrite")
                return {"section_id": next(sid for sid in ids if label.endswith(sid)),
                        "draft_markdown": "A qualified description."}

        kwargs = dict(client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=ReportToolGateway(context))
        result = run_report_agent(**kwargs, checkpoint_sink=saved.append)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertEqual(sum(row.action == "document_review" for row in result.iterations), 4)
        before = len(labels)
        resumed = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
        self.assertEqual(result.report_body, resumed.report_body)
        self.assertEqual(len(labels), before)

    def test_document_review_groups_valid_observations_without_erasing_defects(self):
        config = ReportRuntimeConfig(document_review=True)
        class Client:
            def ask_json(self, *args, **kwargs):
                return {"section_reviews": [
                    {"section_id": "scope", "verdict": "revise_required", "findings": [{
                        "finding_id": "scope", "type": "style", "severity": "minor",
                        "section_id": "scope", "message": "Clarify the sentence.",
                        "draft_quotes": [{"section_id": "scope", "quote": "Original."}]}]},
                    {"section_id": "scope", "verdict": "pass"},
                    {"section_id": "scope", "verdict": "pass"}]}
        reviews = review_document(client=Client(), config=config,
                template=load_report_template_bundle(report_mode="survey", config=config),
                memory=ReportMemory(), sections=[ReportSectionDraft(section_id=sid, heading=sid,
                    draft_markdown="Original.") for sid in ("scope", "method")],
                execution_summary={}, metric_summary={})
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0].verdict, "revise_required")
        self.assertEqual(len(reviews[0].findings), 1)

    def test_final_checkpoint_matches_reconciled_memory_without_document_review(self):
        # A single section cannot enter whole-document review, even when enabled.
        for document_review in (False, True):
            with self.subTest(document_review=document_review):
                context = ReportContext(topic="Bounded result", report_mode="experiment")
                memory = ReportMemory(section_plan=[ReportSectionPlan(
                    section_id="results", heading="Results", goal="Describe observations")])
                config = ReportRuntimeConfig(document_review=document_review,
                                             max_review_iterations=1)
                saved, labels = [], []

                class Client:
                    def ask_json(self, *args, label="", **kwargs):
                        labels.append(label)
                        if "reviewer" in label:
                            return {"verdict": "pass", "findings": []} if "round-2" in label else {
                                "verdict": "revise_required", "findings": [{
                                    "finding_id": "scope", "type": "unsupported_claim",
                                    "severity": "major", "section_id": "results",
                                    "message": "The claim exceeds the observations.",
                                    "draft_quotes": draft_quotes(args[1], "results")}]}
                        return {"section_id": "results", "heading": "Results",
                                "draft_markdown": "Only this setting was observed."
                                if "reviser" in label else "All settings improve."}

                kwargs = dict(client=Client(), context=context, memory=memory, config=config,
                    template=load_report_template_bundle(report_mode="experiment", config=config),
                    gateway=ReportToolGateway(context))
                result = run_report_agent(**kwargs, checkpoint_sink=saved.append)
                self.assertEqual(result.memory.reviewer_findings, [])
                self.assertEqual(saved[-1]["memory"], result.memory.model_dump(mode="json"))
                self.assertTrue(saved[-1]["reviewer_findings"])
                before = len(labels)
                resumed = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
                self.assertEqual(resumed.report_body, result.report_body)
                self.assertEqual(resumed.memory, result.memory)
                self.assertEqual(len(labels), before)

    def test_document_review_refuses_oversized_evidence_before_model_call(self):
        config = ReportRuntimeConfig(document_review=True, max_document_review_prompt_chars=90_000)
        template = load_report_template_bundle(report_mode="survey", config=config)

        class Client:
            def ask_json(self, *args, **kwargs):
                raise AssertionError("Oversized review must not call the model")

        with self.assertRaisesRegex(ValueError, "bounded evidence window"):
            review_document(
                client=Client(), template=template, memory=ReportMemory(), config=config,
                sections=[ReportSectionDraft(section_id="scope", heading="Scope",
                                             draft_markdown="Short draft.")],
                execution_summary={}, metric_summary={"rows": [["x" * 100_000]]},
            )

    def test_document_review_revises_only_targeted_section_and_resumes_without_repeat(self):
        context = ReportContext(topic="Cross-domain study", report_mode="experiment")
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="method", heading="Method", goal="State the method", final_order=1),
            ReportSectionPlan(section_id="results", heading="Results", goal="State observations", final_order=2),
        ])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)
        labels: list[str] = []
        saved: list[dict] = []

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{
                        "section_id": "results", "verdict": "revise_required",
                        "findings": [{"finding_id": "repeated-protocol", "type": "style", "severity": "major",
                                      "section_id": "results", "message": "The protocol duplicates Method.",
                                      "suggested_action": "Keep protocol details in Method; Results should show observations."}],
                        "revision_instructions": ["Remove repeated setup, preserve measured outcomes."],
                    }]}
                if label == "report-document-verifier":
                    return {"section_reviews": []}
                if label == "report-document-reviser-results":
                    return {"section_id": "results", "heading": "Results",
                            "draft_markdown": "Observed results are preliminary."}
                if "reviewer" in label or "verifier" in label:
                    section_id = "results" if "results" in label else "method"
                    return {"section_id": section_id, "verdict": "pass", "findings": []}
                section_id = "results" if "results" in label else "method"
                return {"section_id": section_id, "heading": section_id.title(),
                        "draft_markdown": "The protocol uses one seed and a short run."
                        if section_id == "results" else "The method uses one seed and a short run."}

        client = Client()
        kwargs = dict(client=client, context=context, memory=memory, config=config,
                      template=load_report_template_bundle(report_mode="experiment", config=config),
                      gateway=ReportToolGateway(context))
        result = run_report_agent(**kwargs, checkpoint_sink=saved.append)
        self.assertIn("Observed results are preliminary.", result.report_body)
        self.assertNotIn("The protocol uses one seed", result.report_body)
        self.assertEqual(sum(label == "report-document-reviewer" for label in labels), 1)
        self.assertEqual(sum(label == "report-document-verifier" for label in labels), 1)
        self.assertTrue(saved[-1]["document_review_done"])
        self.assertEqual(result.memory.reviewer_findings, [])
        self.assertTrue(any(row.action == "document_verify" for row in result.iterations))
        before = len(labels)
        resumed = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
        self.assertEqual(resumed.report_body, result.report_body)
        self.assertEqual(len(labels), before)

    def test_document_review_bad_target_preserves_report_and_records_unavailable_review(self):
        context = ReportContext(topic="Review boundary", report_mode="experiment")
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="method", heading="Method", goal="Explain"),
            ReportSectionPlan(section_id="results", heading="Results", goal="Explain"),
        ])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "unknown", "verdict": "pass"}]}
                if "reviewer" in label:
                    return {"verdict": "pass", "findings": []}
                section_id = "results" if "results" in label else "method"
                return {"section_id": section_id, "heading": section_id.title(),
                        "draft_markdown": f"Original {section_id} content."}

        result = run_report_agent(
            client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=ReportToolGateway(context),
        )
        self.assertIn("Original results content.", result.report_body)
        self.assertTrue(any(f.type == "document_review_unavailable"
                            for f in result.memory.reviewer_findings))

    def test_document_review_unresolved_finding_survives_resume_for_survey(self):
        context = ReportContext(topic="Cross-domain literature", report_mode="survey")
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="scope", heading="Scope", goal="Bound the review"),
            ReportSectionPlan(section_id="comparison", heading="Comparison", goal="Compare the evidence"),
        ])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=0)
        saved: list[dict] = []
        labels: list[str] = []

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{
                        "section_id": "comparison", "verdict": "revise_required",
                        "findings": [{"finding_id": "cross-section-claim", "type": "unsupported_claim",
                                      "severity": "major", "section_id": "comparison",
                                      "message": "The comparison claims broader coverage than Scope establishes.",
                                      "draft_quotes": draft_quotes(args[1], "comparison")}],
                        "revision_instructions": ["Bound the comparison to the reviewed sources."],
                    }]}
                if "reviewer" in label:
                    return {"verdict": "pass", "findings": []}
                section_id = "comparison" if "comparison" in label else "scope"
                return {"section_id": section_id, "heading": section_id.title(),
                        "draft_markdown": f"Original {section_id} content."}

        kwargs = dict(
            client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="survey", config=config),
            gateway=ReportToolGateway(context),
        )
        result = run_report_agent(**kwargs, checkpoint_sink=saved.append)
        self.assertTrue(any(f.finding_id == "cross-section-claim"
                            for f in result.memory.reviewer_findings))
        before = len(labels)
        resumed = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
        self.assertEqual(len(labels), before)
        self.assertEqual(resumed.memory.reviewer_findings, result.memory.reviewer_findings)

    def test_document_revision_is_rejected_even_if_verdict_passes_with_factual_issue(self):
        context = ReportContext(topic="Evidence comparison", report_mode="experiment", results={"metrics": {"score": 2.0}})
        memory = ReportMemory(section_plan=[
            ReportSectionPlan(section_id="method", heading="Method", goal="Describe protocol"),
            ReportSectionPlan(section_id="results", heading="Results", goal="Describe evidence"),
        ])
        config = ReportRuntimeConfig(document_review=True, max_review_iterations=1)

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                if label == "report-document-reviewer":
                    return {"section_reviews": [{
                        "section_id": "results", "verdict": "revise_required",
                        "findings": [{"finding_id": "inconsistent-result", "type": "metric_mismatch",
                                      "severity": "minor", "message": "The result disagrees with the measurement.",
                                      "draft_quotes": draft_quotes(args[1], "results"),
                                      "evidence_quotes": [{"pointer": "/verified_execution_results/metrics/score",
                                          "quote": "2.0", "role": "registered_result"}]}],
                    }]}
                if label == "report-document-reviser-results":
                    return {"section_id": "results", "heading": "Results",
                            "draft_markdown": "Revised text with a new unsupported explanation."}
                if label == "report-document-verifier-results":
                    return {"section_id": "results", "verdict": "pass", "findings": [{
                        "finding_id": "new-unsupported-explanation", "type": "unsupported_claim",
                        "severity": "minor", "message": "The explanation is not in the evidence.",
                        "draft_quotes": draft_quotes(args[1], "results"),
                    }]}
                if "reviewer" in label:
                    return {"verdict": "pass", "findings": []}
                section_id = "results" if "results" in label else "method"
                return {"section_id": section_id, "heading": section_id.title(),
                        "draft_markdown": f"Original {section_id} text." + (" Score 99." if section_id == "results" else "")}

        result = run_report_agent(
            client=Client(), context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="experiment", config=config),
            gateway=ReportToolGateway(context),
        )
        self.assertIn("Original results text.", result.report_body)
        self.assertNotIn("new unsupported explanation", result.report_body)
        self.assertEqual(
            {finding.type for finding in result.memory.reviewer_findings},
            {"metric_mismatch", "unsupported_claim"},
        )

    def test_minor_factual_finding_is_revised_within_existing_limit(self):
        context = ReportContext(topic="Calibration", report_mode="experiment",
            results={"metrics": {"definition": "Accuracy measures classification correctness."}})
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")])
        for kind in (
            "metric_mismatch",
            "unsupported_claim",
            "citation_misuse",
            "missing_limitation",
        ):
            with self.subTest(kind=kind):
                calls = []

                class Client:
                    def ask_json(self, *args, label="", **kwargs):
                        calls.append(label)
                        if "reviewer" in label:
                            return {"section_id": "method", "verdict": "pass", "findings": [
                                {"finding_id": "wording", "type": kind, "severity": "minor",
                                 "message": "Accuracy is not a likelihood-based metric.",
                                 "draft_quotes": draft_quotes(args[1], "method"),
                                 "evidence_quotes": [{"pointer": "/verified_execution_results/metrics/definition",
                                     "quote": "Accuracy measures classification correctness.", "role": "registered_result"}]}]}
                        return {"section_id": "method", "heading": "Method",
                                "draft_markdown": "Accuracy measures classification correctness."
                                if "reviser" in label else "Accuracy is likelihood-based."}

                config = ReportRuntimeConfig(allow_llm_fallback=False, max_review_iterations=1)
                result = run_report_agent(client=Client(), context=context, memory=memory, config=config,
                    template=load_report_template_bundle(report_mode="experiment", config=config),
                    gateway=ReportToolGateway(context))
                revise = kind != "missing_limitation"
                self.assertEqual(sum("reviser" in label for label in calls), int(revise))
                self.assertEqual("classification correctness" in result.report_body, revise)

    def test_writer_revision_prompts_preserve_all_review_guidance(self):
        from simple_ar.report.agent import _writer_prompt, _writer_recovery_prompt
        from simple_ar.report.schema import ReportSectionReview, ReviewerFinding

        context = ReportContext(topic="Calibration", report_mode="experiment")
        memory = ReportMemory()
        section = ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")
        config = ReportRuntimeConfig()
        template = load_report_template_bundle(report_mode="experiment", config=config)
        instructions = [f"Explicit instruction {index}." for index in range(7)]
        finding_action = "Remove the unsupported generalization."
        review = ReportSectionReview(
            section_id="method",
            verdict="revise_required",
            findings=[ReviewerFinding(
                finding_id="unsupported",
                type="unsupported_claim",
                message="The claim is broader than the evidence.",
                suggested_action=finding_action,
            )],
            revision_instructions=instructions,
        )

        common = dict(context=context, memory=memory, section=section, config=config,
            extra_context=[], previous_draft=None, review=review, draft_mode="section_revision",
            source_front_matter=[{"paper_id": "paper", "section_id": "front", "text": "Original byline"}])
        for prompt in (_writer_prompt(**common, template=template, source_batch_index=1,
                source_batch_count=1, include_previous_draft=False), _writer_recovery_prompt(**common)):
            view = json.loads(prompt.split("\n\n", 1)[1])
            self.assertEqual(view["review_instructions"], [*instructions, finding_action])
            self.assertEqual(view["review_findings"], [row.model_dump(mode="json") for row in review.findings])
            self.assertEqual(view["source_front_matter"], common["source_front_matter"])
            self.assertIn('source_notes', view['output_schema'])
            rules = view['response_rules'] if 'response_rules' in view else view['style_rules']
            self.assertEqual(sum('Return source_notes in this same response' in row for row in rules), 1)

    def test_final_audit_uses_latest_review_without_erasing_history_or_failed_review(self):
        from simple_ar.report.audit import build_report_audit
        context = ReportContext(topic="Calibration", report_mode="experiment")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")])
        config = ReportRuntimeConfig(allow_llm_fallback=True, max_review_iterations=1)
        finding = {"finding_id": "unsupported", "type": "unsupported_claim", "severity": "critical",
                   "section_id": "method", "message": "Unsupported performance claim"}
        for final_review in ("pass", "fail", "unavailable"):
            with self.subTest(final_review=final_review):
                class Client:
                    reviews = 0

                    def ask_json(self, *args, label="", **kwargs):
                        if "reviewer" not in label:
                            return {"section_id": "method", "heading": "Method", "draft_markdown": "No empirical validation is claimed."}
                        self.reviews += 1
                        if self.reviews > 1 and final_review == "unavailable":
                            raise LLMError("review unavailable")
                        passed = self.reviews > 1 and final_review == "pass"
                        finding["draft_quotes"] = draft_quotes(args[1], "method")
                        return {"section_id": "method", "verdict": "pass" if passed else "revise_required",
                                "findings": [] if passed else [finding]}

                result = run_report_agent(client=Client(), context=context, memory=memory, config=config,
                                          template=load_report_template_bundle(report_mode="experiment", config=config),
                                          gateway=ReportToolGateway(context))
                self.assertTrue(any(f.severity == "critical" for f in result.reviewer_findings))
                self.assertTrue(any(f.severity == "critical" for f in result.iterations[1].findings))
                audit = build_report_audit(report=result.report_body, report_body=result.report_body,
                                           context=context, memory=result.memory)
                self.assertEqual(audit.status, "passed" if final_review == "pass" else "failed")
                self.assertEqual(audit.semantic_review_status, "semantic_unchecked")

    def test_format_retry_does_not_repeat_transport_or_budget_failure(self):
        from simple_ar.integrations.llm import LLMResponseError
        context = ReportContext(topic="Calibration", report_mode="experiment")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")])
        config = ReportRuntimeConfig(allow_llm_fallback=False, max_review_iterations=0)
        template = load_report_template_bundle(report_mode="experiment", config=config)
        for phase in ("writer", "reviewer", "joint"):
            for error in (LLMError("provider unavailable"), LLMError("budget exhausted"), LLMResponseError("invalid JSON")):
                with self.subTest(phase=phase, error=str(error)):
                    calls = []

                    class Client:
                        def ask_json(self, *args, label="", **kwargs):
                            if f"report-{'writer' if phase == 'joint' else phase}" in label:
                                calls.append(label)
                                if len(calls) == 1:
                                    raise error
                            if "reviewer" in label:
                                return {"section_id": "method", "verdict": "pass", "findings": []}
                            if phase == "joint":
                                return {"sections": [{"section_id": sid, "draft_markdown": "No measurements were taken."}
                                    for sid in ("method", "limits")]}
                            return {"section_id": "method", "heading": "Method", "draft_markdown": "No measurements were taken."}

                    plans = memory.model_copy(deep=True)
                    runtime = config.model_copy(deep=True)
                    if phase == "joint":
                        plans.section_plan.append(ReportSectionPlan(section_id="limits", heading="Limits", goal="Bound claims"))
                        runtime.draft_scope = "document"
                        runtime.reviewer = "disabled"
                    kwargs = dict(client=Client(), context=context, memory=plans, config=runtime,
                                  template=template, gateway=ReportToolGateway(context))
                    if isinstance(error, LLMResponseError):
                        result = run_report_agent(**kwargs)
                        self.assertEqual(len(result.sections), 2 if phase == "joint" else 1)
                        self.assertEqual(len(calls), 2)
                    else:
                        with self.assertRaisesRegex(LLMError, str(error)):
                            run_report_agent(**kwargs)
                        self.assertEqual(len(calls), 1)

    def test_reviewer_failure_preserves_draft_without_repeating_writer(self):
        context = ReportContext(topic="Calibration", report_mode="experiment")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")])
        config = ReportRuntimeConfig(allow_llm_fallback=False, max_review_iterations=0)
        template = load_report_template_bundle(report_mode="experiment", config=config)
        saved = []

        class Client:
            fail_review = True
            writes = 0

            def ask_json(self, *args, label="", **kwargs):
                if "reviewer" in label:
                    if self.fail_review:
                        raise LLMError("review unavailable")
                    return {"section_id": "method", "verdict": "pass", "findings": []}
                self.writes += 1
                return {"section_id": "method", "heading": "Method", "draft_markdown": "No empirical validation was performed."}

        client = Client()
        kwargs = dict(client=client, context=context, memory=memory, config=config,
                      template=template, gateway=ReportToolGateway(context), checkpoint_sink=saved.append)
        with self.assertRaises(LLMError):
            run_report_agent(**kwargs)
        self.assertEqual(client.writes, 1)
        self.assertEqual(saved[-1]["sections"], [])
        self.assertEqual(saved[-1]["pending_draft"]["section_id"], "method")
        client.fail_review = False
        result = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
        self.assertEqual(client.writes, 1)
        self.assertEqual(result.sections[0].draft_markdown, "No empirical validation was performed.")
        self.assertIsNone(saved[-1]["pending_draft"])

    def test_prompt_metrics_preserve_all_conditions_without_storage_metadata(self):
        from simple_ar.report.agent import _prompt_metrics
        from simple_ar.report.schema import MetricSource
        metrics = [MetricSource(metric_id=f"m{i}", name="accuracy", value=i / 20,
                                label="baseline" if i < 10 else "candidate",
                                artifact="results.json", protocol_fingerprint="a" * 64)
                   for i in range(20)]
        table = _prompt_metrics(ReportMemory(metric_sources=metrics))
        rows = [dict(zip(table["columns"], row)) for row in table["rows"]]
        self.assertEqual(len(rows), 20)
        self.assertEqual([row["value"] for row in rows], [metric.value for metric in metrics])
        self.assertEqual(rows[-1]["label"], "candidate")
        self.assertTrue(all("protocol_fingerprint" not in row and "artifact" not in row for row in rows))
        self.assertEqual(metrics[0].artifact, "results.json")
        for actual, metric in zip(rows, metrics):
            self.assertEqual(actual, metric.model_dump(include=set(table["columns"])))

    def test_prompt_metrics_compact_paired_task_level_measurements(self):
        from simple_ar.report.agent import _prompt_metrics
        from simple_ar.report.schema import MetricSource
        metrics = [
            MetricSource(metric_id="raw", name="accuracy", value=0.5,
                         label="baseline:seed=0", artifact="results.json"),
            MetricSource(metric_id="task", name="accuracy_after_task_1_on_task_1", value=0.6,
                         label="baseline:seed=0", artifact="results.json"),
            MetricSource(metric_id="summary", name="accuracy.delta_mean", value=0.1,
                         label="paired_summary:0", artifact="paired.json", source_kind="derived_summary"),
            MetricSource(metric_id="summary_task", name="accuracy_after_task_1_on_task_1.delta_mean", value=0.2,
                         label="paired_summary:0", artifact="paired.json", source_kind="derived_summary"),
        ]
        table = _prompt_metrics(ReportMemory(metric_sources=metrics))
        rows = [dict(zip(table["columns"], row)) for row in table["rows"]]
        self.assertEqual([row["metric_id"] for row in rows], ["summary", "raw"])
        self.assertTrue(all("_after_task_" not in row["name"] for row in rows))

    def test_report_prompt_projects_repeated_protocol_and_source_metadata(self):
        from simple_ar.report.agent import _writer_prompt

        section = ReportSectionPlan(
            section_id="method",
            heading="Method",
            goal="Describe the executed method.",
            evidence_handles=["paper:p1"],
        )
        source = SourceHandle(
            handle="paper:p1",
            kind="paper",
            citation_key="P1",
            paper_id="p1",
            title="A paper",
            summary="S" * 2000,
            section="Section " + "x" * 500,
            metadata={"method": "M" * 500, "full_text": "X" * 5000},
            claim="Relevant evidence.",
        )
        protocol = {
            "contract_id": "contract-1",
            "dataset": "CIFAR-100",
            "metrics": ["accuracy"],
            "proposed_change": "Use the approved loss.",
            "raw_protocol_detail": "R" * 5000,
        }
        context = ReportContext(
            topic="Calibration",
            report_mode="experiment",
            experiment_plan={
                "hypothesis": "The change improves retention.",
                "paired_protocols": [
                    {"seed": 0, "condition": "baseline", "artifact": "b.json", "protocol": protocol},
                    {"seed": 0, "condition": "candidate", "artifact": "c.json", "protocol": protocol},
                ],
            },
        )
        memory = ReportMemory(section_plan=[section], source_handles=[source])
        config = ReportRuntimeConfig()
        template = load_report_template_bundle(report_mode="experiment", config=config)
        prompt = _writer_prompt(
            context=context,
            template=template,
            memory=memory,
            section=section,
            config=config,
            extra_context=[],
            previous_draft=None,
            review=None,
            source_batch_index=1,
            source_batch_count=1,
            include_previous_draft=False,
            draft_mode="initial",
        )
        payload = json.loads(prompt[prompt.find("{"):])
        plan = payload["global_research_context"]["experiment_plan"]
        self.assertEqual(len(plan["paired_runs"]), 2)
        self.assertEqual(len(plan["paired_protocols"]), 1)
        self.assertNotIn("raw_protocol_detail", json.dumps(plan))
        projected_source = payload["source_handles"][0]
        self.assertEqual(len(projected_source["summary"]), 800)
        self.assertEqual(len(projected_source["section"]), 240)
        self.assertNotIn("full_text", projected_source["metadata"])
        self.assertEqual(len(projected_source["metadata"]["method"]), 160)

    def test_writer_and_reviewer_preserve_fulltext_evidence_not_just_abstract(self):
        from simple_ar.report.agent import _handles_for_section
        source = SourceHandle(handle="paper:p1", kind="paper", paper_id="p1", citation_key="P1",
            summary="Abstract without numbers", metadata={"extraction_status": "parsed",
                "reading_notes_kind": "model_interpretation_not_source_text",
                "reading_notes": {"datasets": ["Public data"], "key_claims": ["Reported error 0.12"],
                    "reading_coverage": {"available_chunks": 50, "shown_chunk_ids": ["p1#results"], "semantic_verification": "not_performed"}},
                "evidence_passages": [{"chunk_id": "p1#results", "text": "Reported error 0.12", "truncated": False}]})
        section = ReportSectionPlan(section_id="results", heading="Results", goal="Review evidence", evidence_handles=[source.handle])
        projected = _handles_for_section(ReportMemory(source_handles=[source]), section)[0]["metadata"]
        self.assertEqual(projected["extraction_status"], "parsed")
        self.assertEqual(projected["reading_notes"]["datasets"], ["Public data"])
        self.assertEqual(projected["reading_notes"]["reading_coverage"]["semantic_verification"], "not_performed")
        self.assertEqual(projected["evidence_passages"][0]["text"], "Reported error 0.12")
        self.assertFalse(projected["evidence_passages"][0]["truncated"])

    def test_whole_document_review_receives_source_passages(self):
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                payload = json.loads(prompt)
                self.payload = payload
                return {"section_reviews": []}
        client = Client()
        config = ReportRuntimeConfig(document_review=True)
        template = load_report_template_bundle(report_mode="survey", config=config)
        review_document(client=client, template=template, memory=ReportMemory(),
            sections=[ReportSectionDraft(section_id="results", heading="Results", draft_markdown="A reported value.")],
            config=config, execution_summary={}, metric_summary={},
            source_evidence=[{"extraction_status": "parsed", "text": "Reported value 0.12"}])
        self.assertEqual(client.payload["source_evidence"][0]["text"], "Reported value 0.12")

    def test_document_inspection_does_not_treat_old_opinions_as_evidence(self):
        from simple_ar.report.schema import ReviewerFinding
        class Client:
            def ask_json(self, system, prompt, **kwargs):
                self.payload = json.loads(prompt)
                if self.payload["historical_findings_to_check"]:
                    return {"section_reviews": [{"section_id": "setup", "verdict": "revise_required",
                        "finding_checks": [{"finding_id": "unverified-setup", "status": "unresolved",
                            "explanation": "The fixture provides no independent verification."}]}]}
                return {"section_reviews": []}
        client = Client()
        config = ReportRuntimeConfig(document_review=True)
        memory = ReportMemory(reviewer_findings=[ReviewerFinding(
            finding_id="unverified-setup", type="unsupported_claim", severity="major",
            section_id="setup", message="Configured conditions are not independently verified."),
            ReviewerFinding(finding_id="wording", type="style", severity="minor", message="Simplify prose.")])
        kwargs = dict(client=client,
            template=load_report_template_bundle(report_mode="experiment", config=config), memory=memory,
            sections=[ReportSectionDraft(section_id="setup", heading="Setup", draft_markdown="The configured setting.")],
            config=config, execution_summary={}, metric_summary={})
        review_document(**kwargs)
        self.assertEqual(client.payload["historical_findings_to_check"], [])
        review_document(**kwargs, historical_findings=[memory.reviewer_findings[0]])
        self.assertEqual([row["finding_id"] for row in client.payload["historical_findings_to_check"]],
                         ["unverified-setup"])
        self.assertEqual(client.payload["historical_findings_role"], "prior_model_opinions_not_source_facts")

    def test_reviewer_failure_obeys_explicit_fallback_setting(self):
        context = ReportContext(topic="Calibration", report_mode="experiment")
        memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe evidence")])

        class Client:
            def ask_json(self, *args, label="", **kwargs):
                if "reviewer" in label:
                    raise LLMError("review provider unavailable")
                return {"section_id": "method", "heading": "Method", "draft_markdown": "No empirical validation was performed."}

        for allow_fallback in (False, True):
            with self.subTest(allow_fallback=allow_fallback):
                config = ReportRuntimeConfig(allow_llm_fallback=allow_fallback, max_review_iterations=0)
                kwargs = dict(client=Client(), context=context, memory=memory, config=config,
                              template=load_report_template_bundle(report_mode="experiment", config=config),
                              gateway=ReportToolGateway(context))
                if not allow_fallback:
                    with self.assertRaisesRegex(LLMError, "review provider unavailable"):
                        run_report_agent(**kwargs)
                else:
                    result = run_report_agent(**kwargs)
                    self.assertEqual(result.reviewer_findings[0].type, "review_agent_fallback")
                    self.assertIn("fallback", result.reviewer_findings[0].message)

    def test_only_matching_inputs_reuse_completed_sections(self):
        config = ReportRuntimeConfig()
        events = []
        request = ReportWritingRequest(
            ReportContext(topic="Calibration", report_mode="experiment"), ReportMemory(), config,
            load_report_template_bundle(report_mode="experiment", config=config), object(),
            emit=events.append,
        )
        saved = {"sections": [{"section_id": "method", "draft_markdown": "Saved method."}]}
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            first = CapabilityContext(store=store, attempt=AttemptManifest("writer-1"))

            def interrupted(**kwargs):
                kwargs["emit"]("writer callback reached")
                kwargs["checkpoint_sink"](saved)
                return None

            with patch("simple_ar.report.writing.run_report_agent", side_effect=interrupted):
                result = run_report_writing_capability(context=first, request=request)
            self.assertEqual(result.status, "failed")
            self.assertEqual(events, ["writer callback reached"])
            checkpoint = next(ref for ref in result.artifacts if ref.kind == "report_checkpoint")
            for change in ("none", "location", "topic", "template", "criteria"):
                changed = change in {"topic", "template", "criteria"}
                with self.subTest(change=change):
                    output = ArtifactStore(Path(tmp) / change)
                    retry = CapabilityContext(store=output, attempt=AttemptManifest("writer-2"),
                                              inputs=(checkpoint,), input_store=store)
                    context = request.report_context.model_copy(update={"topic": "Changed"}) if change == "topic" else request.report_context
                    updates = {"template_path": "another/template.md", "criteria_path": "another/criteria.md"} if change == "location" else (
                        {f"{change}_markdown": "Changed writing instructions"} if change in {"template", "criteria"} else {}
                    )
                    resumed = replace(request, report_context=context, template=request.template.model_copy(update=updates), resume_ref=checkpoint)
                    with patch("simple_ar.report.writing.run_report_agent", side_effect=interrupted) as writer:
                        run_report_writing_capability(context=retry, request=resumed)
                    self.assertEqual(writer.call_args.kwargs["completed_checkpoint"], None if changed else saved)
                    lineage = output.read_json("sections.json")["resume_ref"]
                    self.assertEqual(lineage, None if changed else checkpoint.to_dict())

            from simple_ar.report.schema import AgentReportResult, ReviewerFinding
            history = ReviewerFinding(finding_id="old", type="unsupported_claim", severity="major", message="Old draft issue.")
            pending = ReviewerFinding(finding_id="current", type="style", severity="minor", required_action="revise", message="Current draft needs shortening.")
            for findings in ([], [pending]):
                delivered = AgentReportResult(report_body="Saved method.", sections=[ReportSectionDraft(
                    section_id="method", heading="Method", draft_markdown="Saved method.")],
                    memory=ReportMemory(reviewer_findings=findings), reviewer_findings=[history, *findings])
                with patch("simple_ar.report.writing.run_report_agent", return_value=delivered):
                    result = run_report_writing_capability(context=first, request=request)
                self.assertEqual(result.status, "completed")
                self.assertEqual(bool(result.diagnostics), bool(findings))
                if findings:
                    self.assertIn("1 unresolved", result.diagnostics[0])
                    self.assertEqual(events[-1], result.diagnostics[0])


class CurrentReportNotesTests(unittest.TestCase):
    def inputs(self, *, document_review=False, reviewer="llm", count=2):
        context = ReportContext(topic="Evidence boundaries", report_mode="supplied_materials")
        memory = ReportMemory(limitations=["Declared input constraint."],
            open_questions=["Unanswered input question."], section_plan=[
                ReportSectionPlan(section_id=sid, heading=sid, goal="Explain bounded observations",
                    draft_order=index, final_order=index)
                for index, sid in enumerate(("scope", "conclusion")[:count], start=1)])
        config = ReportRuntimeConfig(document_review=document_review, reviewer=reviewer,
            max_review_iterations=1, allow_llm_fallback=False)
        return dict(context=context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode=context.report_mode, config=config))

    def test_section_revision_replaces_notes_before_the_next_writer(self):
        kwargs = self.inputs()
        saved, views = [], {}

        class Client:
            def ask_json(self, system, prompt, *, label="", **unused):
                view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
                views[label] = view
                sid = view["section"]["section_id"]
                if "reviewer" in label:
                    if sid == "scope" and "round-2" not in label:
                        return {"verdict": "revise_required", "revision_instructions": ["Qualify the account."],
                            "findings": [{"finding_id": "scope", "type": "style", "severity": "major",
                                "required_action": "revise", "section_id": sid, "message": "Qualify the account.",
                                "draft_quotes": draft_quotes(prompt, sid)}]}
                    return {"verdict": "pass"}
                revised = "reviser" in label
                return {"section_id": sid, "heading": sid, "draft_markdown": "Only supplied observations are described.",
                    "limitations": (["Current section qualification."] if revised else ["Superseded model assertion."])
                        if sid == "scope" else [],
                    "open_questions": (["Current section question."] if revised else ["Superseded model question."])
                        if sid == "scope" else []}

        result = run_report_agent(**kwargs, client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            checkpoint_sink=saved.append)
        self.assertEqual(views["report-writer-conclusion"]["limitations"],
            ["Declared input constraint.", "Current section qualification."])
        # The original scope draft remains in history, not the current note view.
        self.assertIn("Superseded model assertion.", saved[-1]["iterations"][0]["draft"]["limitations"])
        self.assertEqual(result.memory.open_questions, ["Unanswered input question.", "Current section question."])
        self.assertNotIn("Superseded model assertion.", result.memory.limitations)
        self.assertEqual(kwargs["memory"].limitations, ["Declared input constraint."])

    def test_completed_legacy_resume_drops_only_notes_with_recorded_draft_owners(self):
        kwargs = self.inputs(reviewer="disabled", count=1)
        saved = []

        class OriginalClient:
            def ask_json(self, *args, **unused):
                return {"section_id": "scope", "heading": "Scope", "draft_markdown": "Original.",
                    "limitations": ["Old assertion."], "open_questions": ["Old question."]}

        run_report_agent(**kwargs, client=OriginalClient(), gateway=ReportToolGateway(kwargs["context"]),
            checkpoint_sink=saved.append)
        checkpoint = copy.deepcopy(saved[-1])
        checkpoint["sections"][0].update(draft_markdown="Replaced.", limitations=[], open_questions=[])
        checkpoint["memory"]["limitations"].append("Legacy note with unknown owner.")
        checkpoint["memory"]["open_questions"].append("Legacy question with unknown owner.")
        rejected = copy.deepcopy(checkpoint["iterations"][0])
        rejected.update(action="document_revise", adopted=False)
        rejected["draft"].update(limitations=["Rejected candidate note."], open_questions=["Rejected question."])
        checkpoint["iterations"].append(rejected)
        checkpoint["memory"]["limitations"].append("Rejected candidate note.")
        checkpoint["memory"]["open_questions"].append("Rejected question.")
        before = copy.deepcopy(checkpoint)

        class NoCalls:
            def ask_json(self, *args, **unused):
                raise AssertionError("Completed restore cannot spend another model call")

        result = run_report_agent(**kwargs, client=NoCalls(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint)
        self.assertEqual(result.memory.limitations,
            ["Declared input constraint.", "Legacy note with unknown owner."])
        self.assertEqual(result.memory.open_questions,
            ["Unanswered input question.", "Legacy question with unknown owner."])
        self.assertEqual(checkpoint, before)

        # A literal shared with a deleted draft cannot erase an original constraint.
        kwargs["memory"].limitations.append("Old assertion.")
        again = run_report_agent(**kwargs, client=NoCalls(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint)
        self.assertIn("Old assertion.", again.memory.limitations)

    def test_pending_draft_is_not_adopted_and_cannot_pollute_resumed_review(self):
        kwargs = self.inputs(count=1)
        saved = []

        class Client:
            def ask_json(self, *args, label="", **unused):
                if "reviewer" in label:
                    return {"verdict": "pass"}
                return {"section_id": "scope", "heading": "Scope", "draft_markdown": "Candidate.",
                    "limitations": ["Pending candidate note."], "open_questions": ["Pending question."]}

        def interrupt(row):
            saved.append(row)
            if row["pending_draft"] is not None:
                raise RuntimeError("Stopped before review")

        with self.assertRaisesRegex(RuntimeError, "before review"):
            run_report_agent(**kwargs, client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                checkpoint_sink=interrupt)
        checkpoint = copy.deepcopy(saved[-1])
        checkpoint["memory"]["limitations"].append("Pending candidate note.")
        checkpoint["memory"]["open_questions"].append("Pending question.")
        test = self

        class Resumed:
            def ask_json(self, system, prompt, *, label="", **unused):
                test.assertIn("reviewer", label)
                view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
                test.assertEqual(view["known_limitations"], ["Declared input constraint."])
                return {"verdict": "pass"}

        result = run_report_agent(**kwargs, client=Resumed(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint)
        self.assertIn("Pending candidate note.", result.memory.limitations)
        self.assertIn("Pending question.", result.memory.open_questions)

    def test_document_adoption_updates_notes_before_immediate_whole_document_recheck(self):
        kwargs = self.inputs(document_review=True)
        views, saved = {}, []

        class Client:
            def ask_json(self, system, prompt, *, label="", **unused):
                view = json.JSONDecoder().raw_decode(prompt[prompt.index("{"):])[0]
                views[label] = view
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "revision_instructions": ["Qualify this paragraph."], "findings": [{
                            "finding_id": "qualify", "type": "style", "severity": "major", "required_action": "revise",
                            "section_id": "scope", "message": "Qualify this paragraph.",
                            "draft_quotes": draft_quotes(prompt, "scope")}]}]}
                if label == "report-document-verifier":
                    return {"section_reviews": []}
                if "verifier" in label or "reviewer" in label:
                    return {"verdict": "pass"}
                sid = view["section"]["section_id"]
                revised = "document-reviser" in label
                return {"section_id": sid, "heading": sid, "draft_markdown": "Current bounded account.",
                    "limitations": ["New scope note."] if revised else (["Old scope note."] if sid == "scope" else []),
                    "open_questions": ["New scope question."] if revised else (["Old scope question."] if sid == "scope" else [])}

        result = run_report_agent(**kwargs, client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            checkpoint_sink=saved.append)
        self.assertEqual(views["report-document-verifier"]["known_limitations"],
            ["Declared input constraint.", "New scope note."])
        self.assertEqual(result.memory.open_questions, ["Unanswered input question.", "New scope question."])
        self.assertTrue(any(row["action"] == "document_revise" and row["adopted"] for row in saved[-1]["iterations"]))
        self.assertEqual(result.memory.model_dump(mode="json"), saved[-1]["memory"])


    def test_shared_current_notes_survive_and_projection_does_not_change_history(self):
        from simple_ar.report.narrative import adopted_memory_notes
        from simple_ar.report.schema import ReportSectionDraft, ReportIterationRecord
        initial = ReportMemory(limitations=["Input boundary."], open_questions=["Input unknown."])
        old = ReportSectionDraft(section_id="scope", heading="Scope", limitations=["Shared restriction."],
            open_questions=["Shared question."])
        replacement = old.model_copy(update={"limitations": [], "open_questions": []})
        other = old.model_copy(update={"section_id": "other"})
        recorded = ReportMemory(limitations=["Input boundary.", "Shared restriction."],
            open_questions=["Input unknown.", "Shared question."])
        history = [ReportIterationRecord(iteration=1, section_id="scope", action="draft", status="drafted", draft=old)]
        before = [row.model_dump(mode="json") for row in (initial, recorded, old, replacement, other, *history)]
        self.assertEqual(adopted_memory_notes(initial, recorded, [replacement, other], history),
            {"limitations": ["Input boundary.", "Shared restriction."],
             "open_questions": ["Input unknown.", "Shared question."]})
        self.assertEqual(adopted_memory_notes(initial, recorded, [replacement], history),
            {"limitations": ["Input boundary."], "open_questions": ["Input unknown."]})
        self.assertEqual([row.model_dump(mode="json") for row in (initial, recorded, old, replacement, other, *history)], before)
