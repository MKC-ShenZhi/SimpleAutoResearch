"""Prior opinions need explicit, draft-grounded decisions, not silent omission."""
import json
import unittest

from simple_ar.integrations.llm import LLMError, LLMResponseError
from simple_ar.report.agent import _dedupe_findings, run_report_agent
from simple_ar.report.editor import coalesce_document_reviews, review_document, rejected_review_context_requests
from simple_ar.report.schema import ReportSectionReview, ReviewerFinding, ReportIterationRecord
from simple_ar.report.tool_gateway import ReportToolGateway
from tests.report_review_fixtures import (
    document_review_inputs, finding_check_objects, opinion_check_response, prompt_payload,
    unresolved_opinion_response,
)


class FindingCheckTests(unittest.TestCase):
    def test_explicit_verify_requests_are_read_before_rewriting_even_with_revision_verdict(self):
        for verdict in ('warning', 'revise_required', 'fail'):
            with self.subTest(verdict=verdict):
                kwargs, completed, _, drafts = finding_check_objects()
                completed['memory']['reviewer_findings'] = []
                completed['reviewer_findings'] = []
                kwargs['context'].synthesis_markdown = 'Retained derived interpretation, not original evidence.'
                kwargs['config'] = kwargs['config'].model_copy(update={'max_review_iterations': 1, 'draft_scope': 'document'})
                labels, views = [], []
                test = self
                class Client:
                    def ask_json(self, system, prompt, *, label='', **ignored):
                        labels.append(label)
                        views.append(json.loads(prompt))
                        if label == 'report-document-reviewer':
                            return {'section_reviews': [{'section_id': 'scope', 'verdict': verdict,
                                'findings': [{'finding_id': 'verify-origin', 'type': 'evidence_gap',
                                    'severity': 'major', 'required_action': 'verify',
                                    'message': 'Locate the retained interpretation before judging this statement.',
                                    'draft_quotes': [{'section_id': 'scope', 'quote': drafts[0].draft_markdown}]}],
                                'context_requests': [{'tool_name': 'get_synthesis_brief', 'arguments': {}}]}]}
                        test.assertEqual(label, 'report-document-reviewer-evidence')
                        return {'section_reviews': []}
                gateway = ReportToolGateway(kwargs['context'])
                result = run_report_agent(client=Client(), gateway=gateway, completed_checkpoint=completed, **kwargs)
                self.assertEqual(labels, ['report-document-reviewer', 'report-document-reviewer-evidence'])
                self.assertEqual(gateway.call_counts['get_synthesis_brief'], 1)
                self.assertEqual(views[-1]['supplementary_evidence'][0]['status'], 'ok')
                self.assertEqual(result.sections, drafts)
                self.assertFalse(result.memory.reviewer_findings)
                self.assertFalse(any(row.action == 'document_joint_revise' for row in result.iterations))

    def test_evidence_first_dispatch_does_not_hide_prose_or_legacy_corrections(self):
        from simple_ar.report.agent import _needs_evidence_recheck
        verify = ReviewerFinding(finding_id='origin', type='evidence_gap', severity='major',
                                 required_action='verify', message='Read original evidence.')
        request = {'tool_name': 'get_synthesis_brief', 'arguments': {}}
        common = dict(section_id='scope', verdict='revise_required', context_requests=[request])
        for findings in ([], [verify.model_copy(update={'required_action': None})],
                         [verify.model_copy(update={'severity': 'minor', 'required_action': 'advisory'})],
                         [verify, verify.model_copy(update={'finding_id': 'prose', 'required_action': 'revise'})]):
            self.assertFalse(_needs_evidence_recheck(ReportSectionReview(**common, findings=findings)))
        self.assertTrue(_needs_evidence_recheck(ReportSectionReview(**common, findings=[verify])))
        self.assertFalse(_needs_evidence_recheck(ReportSectionReview(**{**common, 'context_requests': []}, findings=[verify])))

    def test_accepted_document_lookup_allocates_before_read_and_restores_without_repeating(self):
        for stop_at in ("allocated", "returned_unsaved", "completed"):
            with self.subTest(stop_at=stop_at):
                kwargs, completed, _, drafts = finding_check_objects()
                completed["memory"]["reviewer_findings"] = []
                completed["reviewer_findings"] = []
                kwargs["context"].synthesis_markdown = "Saved interpretation remains derived, not primary evidence."
                labels, views, saved = [], [], []
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        views.append(json.loads(prompt))
                        if label == "report-document-reviewer":
                            return {"section_reviews": [{"section_id": "scope", "verdict": "warning",
                                "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}]}]}
                        if label == "report-document-reviewer-evidence":
                            return {"section_reviews": []}
                        raise AssertionError("No Writer or unrelated review")
                def sink(value):
                    saved.append(value)
                    if not value["iterations"]:
                        return
                    event = value["iterations"][-1]
                    if event["action"] == "document_context" and event["tool_results"]:
                        allocated = event["tool_results"][0]["metadata"].get("lookup_state") == "allocated"
                        if stop_at != "returned_unsaved" and allocated == (stop_at == "allocated"):
                            raise RuntimeError("Interrupted at accepted-read checkpoint")
                gateway = ReportToolGateway(kwargs["context"])
                if stop_at == "returned_unsaved":
                    dispatch = gateway.call
                    def interrupted_read(request):
                        dispatch(request)
                        raise RuntimeError("Interrupted at accepted-read checkpoint")
                    gateway.call = interrupted_read
                with self.assertRaisesRegex(RuntimeError, "accepted-read checkpoint"):
                    run_report_agent(client=Client(), gateway=gateway, completed_checkpoint=completed,
                        checkpoint_sink=sink, **kwargs)
                self.assertEqual(gateway.call_counts["get_synthesis_brief"], int(stop_at != "allocated"))
                self.assertEqual(len(saved[-1]["tool_results"]), 1)
                resumed_gateway = ReportToolGateway(kwargs["context"])
                resumed_gateway.call = lambda request: self.fail("Allocated or completed read must not repeat")
                restored = run_report_agent(client=Client(), gateway=resumed_gateway,
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(labels, ["report-document-reviewer", "report-document-reviewer-evidence"])
                self.assertEqual(resumed_gateway.call_counts["get_synthesis_brief"], 1)
                self.assertEqual(restored.sections, drafts)
                tool = views[-1]["supplementary_evidence"][-1]
                self.assertEqual(tool["status"], "ok" if stop_at == "completed" else "blocked")
                if stop_at != "completed":
                    self.assertNotIn("text", tool["content"])
                else:
                    self.assertEqual(tool["content"]["text"], kwargs["context"].synthesis_markdown)

    def test_rejected_lookup_extraction_binds_targets_and_read_tools_not_verdicts(self):
        read = {"tool_name": "get_synthesis_brief", "arguments": {}, "caller": "writer"}
        def extract(rows, limit=2):
            return rejected_review_context_requests({"section_reviews": rows},
                section_ids={"scope", "limits"}, read_only_tools={"get_synthesis_brief"}, max_requests=limit)
        row = {"section_id": "scope", "findings": "invalid", "context_requests": [read, read]}
        found = extract([row])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].caller, "document_reviewer")
        self.assertFalse(extract([row], 0))
        for targets in ([row, row], [row, {"section_id": "unknown"}], [None], [{"section_id": []}]):
            self.assertFalse(extract(targets))
        for raw in (None, {"tool_name": []}, {"tool_name": {}}, {"tool_name": "write_file"},
                    {"tool_name": "get_synthesis_brief", "arguments": "not a dict"}):
            self.assertFalse(extract([{**row, "context_requests": [raw]}]))
        second = {"section_id": "limits", "context_requests": [
            {**read, "arguments": {"query": "another"}}]}
        self.assertEqual(len(extract([row, second], 1)), 1)
        self.assertEqual(len(extract([row, second], 2)), 2)

    def rejected_lookup_objects(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        kwargs["context"].synthesis_markdown = "Saved interpretation remains derived context."
        rejected = {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
            "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}],
            "findings": [{"finding_id": "forbidden-new-opinion", "section_id": "scope",
                "type": "style", "severity": "major", "required_action": "revise",
                "message": "This discovery is not permitted in a historical check."}],
            "revision_instructions": ["Rewrite the report."]}]}
        labels, views = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                views.append(json.loads(prompt))
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    return rejected
                if label == "report-document-finding-checker-format-correction":
                    return opinion_check_response(old, verdict="pass", status="not_applicable",
                        explanation="The current text explicitly preserves uncertainty.",
                        draft_quotes=[drafts[0].draft_markdown])
                raise AssertionError("No Writer or extra correction is authorized")
        return kwargs, checkpoint, old, drafts, rejected, labels, views, Client()

    def test_rejected_opinion_can_fetch_read_context_without_adopting_its_instructions(self):
        kwargs, checkpoint, old, drafts, rejected, labels, views, client = self.rejected_lookup_objects()
        gateway = ReportToolGateway(kwargs["context"])
        result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
                                  "report-document-finding-checker-format-correction"])
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        correction = views[-1]
        self.assertEqual(correction["response_format_correction"]["rejected_response"], rejected)
        self.assertEqual(correction["supplementary_evidence"][-1]["content"]["text"],
                         kwargs["context"].synthesis_markdown)
        event = next(row for row in result.iterations if row.action == "document_finding_check_rejected")
        self.assertEqual(event.rejected_review["response"], rejected)
        self.assertEqual(event.tool_results[0].status, "ok")
        self.assertEqual(result.sections, drafts)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertIn(old, result.reviewer_findings)
        self.assertFalse(any(row.finding_id == "forbidden-new-opinion" for row in result.reviewer_findings))

    def test_rejected_lookup_allocation_and_completion_resume_without_reread(self):
        for stop_at in ("allocated", "completed"):
            with self.subTest(stop_at=stop_at):
                kwargs, checkpoint, old, drafts, rejected, labels, views, client = self.rejected_lookup_objects()
                saved = []
                def sink(row):
                    saved.append(row)
                    if not row["iterations"]:
                        return  # The frozen plan checkpoint precedes review events.
                    event = row["iterations"][-1]
                    if event["action"] != "document_finding_check_rejected" or not event["tool_results"]:
                        return
                    pending = event["tool_results"][0]["metadata"].get("lookup_state") == "allocated"
                    if pending == (stop_at == "allocated"):
                        raise RuntimeError("Interrupted at lookup checkpoint")
                gateway = ReportToolGateway(kwargs["context"])
                with self.assertRaisesRegex(RuntimeError, "lookup checkpoint"):
                    run_report_agent(client=client, gateway=gateway, completed_checkpoint=checkpoint,
                        checkpoint_sink=sink, **kwargs)
                self.assertEqual(gateway.call_counts.get("get_synthesis_brief", 0), int(stop_at == "completed"))
                before = len(labels)
                resumed_gateway = ReportToolGateway(kwargs["context"])
                resumed_gateway.call = lambda request: self.fail("Allocated/completed lookup cannot be repeated")
                result = run_report_agent(client=client, gateway=resumed_gateway,
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(labels[before:], ["report-document-finding-checker-format-correction"])
                self.assertEqual(resumed_gateway.call_counts["get_synthesis_brief"], 1)
                self.assertEqual(result.sections, drafts)
                tool = views[-1]["supplementary_evidence"][-1]
                self.assertEqual(tool["status"], "blocked" if stop_at == "allocated" else "ok")
                if stop_at == "allocated":
                    self.assertNotIn("text", tool["content"])
                    self.assertEqual(tool["content"]["text_status"]["independent_verification"], "not_performed")

    def test_rejected_inspection_query_reads_registered_original_not_foreign_or_invalid_scope(self):
        from simple_ar.research.contracts import DocumentRecord, TextChunk
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.report.schema import SourceHandle
        for arguments, expected in (({"handle": "paper:p", "query": "measured condition"}, "ok"),
                                    ({"handle": "unregistered", "query": "condition"}, "not_found"),
                                    ({"handle": "paper:p", "query": "condition", "limit": 99}, "error")):
            with self.subTest(expected=expected):
                kwargs, checkpoint, _, drafts = finding_check_objects()
                checkpoint["memory"]["reviewer_findings"] = []
                checkpoint["reviewer_findings"] = []
                kwargs["context"].source_handles = [SourceHandle(handle="paper:p", kind="paper",
                    paper_id="p", metadata={"document_id": "doc"})]
                documents = DocumentBundle(records=[DocumentRecord("doc", "Source", "local", extraction_status="parsed")],
                    fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=[
                        TextChunk("c1", "doc", "The measured condition was 17 units."),
                        TextChunk("foreign", "other", "The measured condition was 99 units.")])
                labels, views = [], []
                response = {"section_reviews": [{"section_id": "scope", "verdict": "warning",
                    "findings": "malformed opinion", "context_requests": [
                        {"tool_name": "search_source_chunks", "arguments": arguments}]}]}
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        views.append(json.loads(prompt))
                        return response if len(labels) == 1 else {"section_reviews": []}
                gateway = ReportToolGateway(kwargs["context"], documents=documents)
                result = run_report_agent(client=Client(), gateway=gateway, completed_checkpoint=checkpoint, **kwargs)
                self.assertEqual(labels, ["report-document-reviewer", "report-document-reviewer-format-correction"])
                tool = views[-1]["supplementary_evidence"][-1]
                self.assertEqual(tool["status"], expected)
                if expected == "ok":
                    self.assertEqual([row["chunk_id"] for row in tool["content"]["chunks"]], ["c1"])
                    self.assertEqual(tool["content"]["chunks"][0]["text"], documents.chunks[0].text)
                else:
                    self.assertNotIn("chunks", tool["content"])
                self.assertEqual(gateway.call_counts["search_source_chunks"], 1)
                self.assertEqual(result.sections, drafts)

    def test_rejected_historical_lookup_cannot_bind_to_unrequested_other_section(self):
        kwargs, checkpoint, old, drafts, rejected, labels, views, client = self.rejected_lookup_objects()
        rejected["section_reviews"][0]["section_id"] = "limits"
        gateway = ReportToolGateway(kwargs["context"])
        result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 0)
        self.assertFalse(views[-1]["supplementary_evidence"])
        self.assertEqual(result.sections, drafts)

    def test_second_rejected_correction_cannot_spend_another_lookup_or_close_opinion(self):
        kwargs, checkpoint, old, drafts, rejected, labels, views, first = self.rejected_lookup_objects()
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label.endswith("-format-correction"):
                    labels.append(label)
                    return rejected
                return first.ask_json(system, prompt, label=label, **ignored)
        saved = []
        gateway = ReportToolGateway(kwargs["context"])
        client = Client()
        result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=checkpoint,
            checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        self.assertEqual(len(labels), 3)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertEqual(result.sections, drafts)
        self.assertEqual(len([row for row in result.iterations
            if row.action == "document_finding_check_rejected"]), 2)
        before = len(labels)
        resumed_gateway = ReportToolGateway(kwargs["context"])
        resumed_gateway.call = lambda request: self.fail("Cannot repeat rejected correction reads")
        resumed = run_report_agent(client=client, gateway=resumed_gateway,
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(len(labels), before)
        self.assertIn(old, resumed.memory.reviewer_findings)

    def test_rejected_lookup_respects_disabled_backtracking_and_exhausted_gateway(self):
        for disabled in (True, False):
            kwargs, checkpoint, old, drafts, rejected, labels, views, client = self.rejected_lookup_objects()
            kwargs["config"].allow_source_backtracking = not disabled
            gateway = ReportToolGateway(kwargs["context"])
            gateway.call_counts["get_synthesis_brief"] = gateway.specs["get_synthesis_brief"].max_calls
            result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=checkpoint, **kwargs)
            event = next(row for row in result.iterations if row.action == "document_finding_check_rejected")
            self.assertEqual(len(event.tool_results), 0 if disabled else 1)
            if not disabled:
                self.assertEqual(event.tool_results[0].status, "blocked")
                self.assertEqual(event.tool_results[0].content, {})
            self.assertEqual(len(labels), 3)
            self.assertEqual(result.sections, drafts)

    def test_rejected_discovery_in_opinion_role_keeps_history_and_cannot_dispatch_writer(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        kwargs["config"].max_review_iterations = 2
        labels, saved = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                if label.startswith("report-document-finding-checker"):
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "findings": [{"finding_id": "injected", "type": "style", "severity": "major",
                            "required_action": "revise", "message": "Rewrite as another method."}],
                        "revision_instructions": ["Rewrite this section."]}]}
                raise AssertionError("Rejected opinion-role discovery cannot dispatch a Writer")
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(result.sections, drafts)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertFalse(any(row.finding_id == "injected" for row in result.memory.reviewer_findings))
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
                                  "report-document-finding-checker-format-correction"])
        rejected = [row for row in result.iterations if row.action == "document_finding_check_rejected"]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(row.rejected_review["response"]["section_reviews"][0]["findings"] for row in rejected))
        before = len(labels)
        resumed = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(len(labels), before)
        self.assertEqual(resumed.sections, drafts)
        self.assertIn(old, resumed.memory.reviewer_findings)

    def test_historical_check_cannot_add_new_findings_or_rewrite_instructions(self):
        kwargs, _, old, drafts = finding_check_objects()
        for extra in ({"findings": [{"finding_id": "new", "type": "style", "message": "A new preference."}]},
                      {"revision_instructions": ["Replace this section with a new argument."]}):
            with self.subTest(extra=extra):
                response = {"section_reviews": [{"section_id": "scope", "verdict": "pass", **extra}]}
                before = json.dumps(response, sort_keys=True)
                rejected = []
                class Client:
                    def ask_json(self, *args, **ignored):
                        return response
                with self.assertRaisesRegex(LLMResponseError, "cannot add"):
                    review_document(client=Client(), **document_review_inputs(
                        kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                        historical_findings=[old], on_invalid_response=lambda row, error: rejected.append((row, error))))
                self.assertEqual(json.dumps(response, sort_keys=True), before)
                self.assertEqual(rejected[0][0], response)
                # Discovery still owns new findings; do not disable that role.
                self.assertEqual(len(review_document(client=Client(), **document_review_inputs(
                    kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"]))), 1)

    def test_historical_check_omits_drafting_directives_not_task_or_evidence(self):
        kwargs, _, old, drafts = finding_check_objects()
        objective = "Inspect all supplied sections and retain the user's conditions. " * 80
        kwargs["memory"].section_plan[0].goal = "An earlier Writer-only instruction."
        source = [{"handle": "material:source", "metadata": {"evidence_passages": [{"text": "Original source qualification."}]}}]
        execution = {"execution_records": [{"cwd": "/observed/project"}]}
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = json.loads(prompt)
                captured.append(view)
                return unresolved_opinion_response(view)
        for prior, correction in ((None, None), ([old], None), ([old], {"validation_error": "An invalid check."})):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"], execution_summary={"metric": 1},
                source_evidence=source, execution_evidence=execution, writing_objective=objective,
                historical_findings=prior, format_correction=correction))
        discovery = captured[0]
        self.assertIn("criteria", discovery)
        self.assertNotIn("section_responsibilities", discovery)
        self.assertNotIn("An earlier Writer-only instruction.", json.dumps(discovery))
        for check in captured[1:]:
            self.assertNotIn("criteria", check)
            self.assertNotIn("section_responsibilities", check)
            for field in ("objective", "sections", "source_evidence", "execution_evidence", "verified_execution_results", "context_tools"):
                self.assertEqual(check[field], discovery[field])
        self.assertEqual(captured[2]["response_format_correction"], {"validation_error": "An invalid check."})
        self.assertEqual(kwargs["memory"].section_plan[0].goal, "An earlier Writer-only instruction.")

    def test_explicit_closure_is_recorded_and_completed_resume_is_call_free(self):
        for status in ("resolved", "not_applicable"):
            with self.subTest(status=status):
                kwargs, checkpoint, old, drafts = finding_check_objects()
                labels, saved = [], []
                test = self
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        view = json.loads(prompt)
                        if label == "report-document-reviewer":
                            test.assertEqual(view["historical_findings_to_check"], [])
                            return {"section_reviews": []}
                        test.assertEqual(label, "report-document-finding-checker")
                        test.assertEqual(view["historical_findings_to_check"][0]["finding_id"], old.finding_id)
                        return opinion_check_response(old, verdict="pass", status=status,
                            explanation="The current draft explicitly retains the uncertainty.",
                            draft_quotes=[drafts[0].draft_markdown])
                result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
                self.assertFalse(result.memory.reviewer_findings)
                checks = [row for row in result.iterations if row.action == "document_finding_check"]
                self.assertEqual(checks[0].finding_checks[0].status, status)
                self.assertIn(old, result.reviewer_findings)
                self.assertEqual(result.sections, drafts)
                before = len(labels)
                resumed = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(resumed.report_body, result.report_body)
                self.assertEqual(len(labels), before)

    def test_opinion_response_contract_projects_only_requested_targets(self):
        kwargs, _, old, drafts = finding_check_objects()
        other = old.model_copy(update={"finding_id": "other-assumption"})
        cross = old.model_copy(update={"finding_id": "different-scope", "section_id": "limits"})
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = json.loads(prompt)
                captured.append((system, view))
                return unresolved_opinion_response(view)
        for prior in (None, [old, other, cross]):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                historical_findings=prior))
        discovery, check = captured
        self.assertEqual(discovery[1]["output_schema"]["section_reviews"][0]["finding_checks"], [])
        target_rows = check[1]["output_schema"]["section_reviews"]
        self.assertEqual(len(target_rows), 1)
        self.assertEqual(len(target_rows[0]["finding_checks"]), 1)
        self.assertIn("historical_findings_to_check", target_rows[0]["section_id"])
        self.assertEqual([(row["section_id"], row["finding_id"])
            for row in check[1]["historical_findings_to_check"]],
            [("scope", old.finding_id), ("scope", other.finding_id), ("limits", cross.finding_id)])
        self.assertTrue(all("findings" not in row and "revision_instructions" not in row for row in target_rows))
        self.assertIn("Check only", check[0])
        self.assertFalse(any(row.startswith("Find contradictions") for row in check[1]["focus"]))
        self.assertEqual(check[1]["sections"], discovery[1]["sections"])
        self.assertEqual(check[1]["source_evidence"], discovery[1]["source_evidence"])

    def test_other_current_sections_are_evidence_not_extra_opinion_targets(self):
        kwargs, _, old, drafts = finding_check_objects()
        class Client:
            def ask_json(self, *args, **ignored):
                return {"section_reviews": [{"section_id": "limits", "verdict": "pass"}]}
        with self.assertRaisesRegex(LLMResponseError, "unrequested target"):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                historical_findings=[old]))

    def test_opinion_check_omits_imperative_remedy_without_changing_original_contract(self):
        kwargs, _, old, drafts = finding_check_objects()
        old.suggested_action = "Replace the observed condition with the requested condition."
        old.evidence_handles = ["material:conditions"]
        before = old.model_dump(mode="json")
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = json.loads(prompt)
                captured.append(view)
                return unresolved_opinion_response(view)
        review_document(client=Client(), **document_review_inputs(
            kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"], historical_findings=[old]))
        view = captured[0]
        projected = view["historical_findings_to_check"][0]
        self.assertEqual(projected["message"], old.message)
        self.assertEqual(projected["evidence_handles"], old.evidence_handles)
        self.assertEqual(projected["draft_quotes"], before["draft_quotes"])
        self.assertNotIn("suggested_action", projected)
        self.assertNotIn("severity", projected)
        self.assertNotIn("required_action", projected)
        self.assertNotIn(old.suggested_action, json.dumps(view))
        self.assertFalse(any(rule.startswith("For each finding") for rule in view["focus"]))
        self.assertEqual(old.model_dump(mode="json"), before)

    def test_opinion_response_shape_does_not_grow_with_number_or_priority_of_opinions(self):
        from simple_ar.report.schema import ReportEvidenceQuote
        kwargs, _, old, drafts = finding_check_objects()
        old.evidence_quotes = [ReportEvidenceQuote(pointer="/objective", quote="old task", role="declaration")]
        originals = [old.model_copy(deep=True, update={"finding_id": f"opinion-{index}",
                     "severity": "critical" if index % 2 else "minor",
                     "required_action": "verify" if index % 2 else "revise"}) for index in range(12)]
        before = [row.model_dump(mode="json") for row in originals]
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = json.loads(prompt)
                captured.append(view)
                return unresolved_opinion_response(view)
        for prior in (originals[:1], originals):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                historical_findings=prior))
        self.assertEqual(captured[0]["output_schema"], captured[1]["output_schema"])
        self.assertEqual(len(captured[1]["historical_findings_to_check"]), 12)
        for original, projected in zip(before, captured[1]["historical_findings_to_check"]):
            for key in ("finding_id", "section_id", "message", "draft_quotes", "evidence_quotes"):
                self.assertEqual(projected[key], original[key])
            self.assertFalse({"severity", "required_action", "suggested_action"} & projected.keys())
        self.assertEqual([row.model_dump(mode="json") for row in originals], before)

    def test_rejected_judgement_is_not_a_required_conclusion_or_new_source_evidence(self):
        kwargs, _, old, drafts = finding_check_objects()
        captured = []
        correction = {"validation_error": "Wrong source role.",
                      "rejected_response": {"claim": "The requested setting was observed."}}
        class Client:
            def ask_json(self, system, prompt, **ignored):
                view = json.loads(prompt)
                captured.append(view)
                return unresolved_opinion_response(view)
        review_document(client=Client(), **document_review_inputs(
            kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
            historical_findings=[old], format_correction=correction))
        view = captured[0]
        self.assertEqual(view["response_format_correction"], correction)
        self.assertEqual(view["correction_role"], "rejected_model_answer_not_evidence")
        self.assertTrue(any("not a rejected factual conclusion" in rule for rule in view["focus"]))
        self.assertEqual(view["historical_findings_to_check"][0]["finding_id"], old.finding_id)

    def test_independent_inspection_retains_new_finding_action_contract(self):
        from simple_ar.report.schema import REVIEW_ACTION_RULES
        kwargs, _, old, drafts = finding_check_objects()
        captured = []
        class Client:
            def ask_json(self, system, prompt, **ignored):
                captured.append(json.loads(prompt))
                return {"section_reviews": []}
        review_document(client=Client(), **document_review_inputs(
            kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"]))
        self.assertTrue(all(rule in captured[0]["focus"] for rule in REVIEW_ACTION_RULES))
        self.assertEqual(captured[0]["historical_findings_to_check"], [])

    def test_unresolved_verification_does_not_itself_authorize_a_prose_correction(self):
        kwargs, checkpoint, old, drafts = finding_check_objects(
            max_review_iterations=2, finding_changes={"required_action": "verify", "suggested_action": "Check the unavailable original observation."})
        labels, saved = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    return opinion_check_response(old, verdict="warning", status="unresolved",
                        explanation="The missing source prevents deciding whether this old allegation applies.")
                raise AssertionError("An unresolved verification is not a request to rewrite the draft")
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(result.sections, drafts)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker"])
        self.assertFalse(any(event.action == "document_revise" for event in result.iterations))
        self.assertEqual(next(event for event in result.iterations
            if event.action == "document_finding_check").status, "warning")
        before = len(labels)
        resumed = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(resumed.sections, drafts)
        self.assertIn(old, resumed.memory.reviewer_findings)
        self.assertEqual(len(labels), before)

    def test_explicit_bounded_correction_of_a_verification_gap_still_runs(self):
        kwargs, checkpoint, old, drafts = finding_check_objects(
            max_review_iterations=1, finding_changes={"required_action": "verify"})
        labels = []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label in {"report-document-reviewer", "report-document-verifier"}:
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    return opinion_check_response(old, verdict="revise_required", status="unresolved",
                        explanation="The current wording needs an explicit bound, not a fabricated source fact.")
                if label == "report-document-reviser-scope":
                    return {"section_id": "scope", "draft_markdown":
                        "The identity was not independently verified. No source identity conclusion is drawn."}
                if label == "report-document-verifier-scope":
                    return {"section_id": "scope", "verdict": "pass"}
                raise AssertionError(label)
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels.count("report-document-reviser-scope"), 1)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertIn(old, result.reviewer_findings)

    def test_separate_correction_cannot_close_a_pending_old_verification(self):
        kwargs, checkpoint, old, drafts = finding_check_objects(
            max_review_iterations=1, finding_changes={"required_action": "verify"})
        fresh = old.model_copy(update={"finding_id": "current-style", "type": "style", "severity": "minor",
            "required_action": "revise", "message": "A current readability correction."})
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "findings": [fresh.model_dump(mode="json")]}]}
                if label == "report-document-finding-checker":
                    return opinion_check_response(old, verdict="warning", status="unresolved",
                        explanation="The source needed to decide this allegation is not available.")
                if label == "report-document-reviser-scope":
                    view = prompt_payload(prompt)
                    test.assertEqual([row["finding_id"] for row in view["review_findings"]], [fresh.finding_id])
                    return {"section_id": "scope", "draft_markdown":
                        "The identity was not independently verified. This account remains provisional."}
                if label == "report-document-verifier-scope":
                    return {"section_id": "scope", "verdict": "pass"}
                test.assertEqual(label, "report-document-verifier")
                return {"section_reviews": []}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(result.memory.reviewer_findings, [old])
        self.assertIn(fresh, result.reviewer_findings)

    def test_pending_opinion_evidence_neither_closes_it_nor_requests_a_prose_rewrite(self):
        for allowed in (False, True):
            with self.subTest(backtracking=allowed):
                kwargs, checkpoint, old, drafts = finding_check_objects(
                    max_review_iterations=2, finding_changes={"required_action": "verify"})
                kwargs["config"].allow_source_backtracking = allowed
                kwargs["config"].max_backtracking_calls = 1
                labels = []
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        if label == "report-document-reviewer":
                            return {"section_reviews": []}
                        if label in {"report-document-finding-checker", "report-document-finding-checker-evidence"}:
                            return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                                "finding_checks": [{"finding_id": old.finding_id, "status": "not_applicable",
                                    "explanation": "A provisional judgement still requesting original evidence.",
                                    "draft_quotes": [drafts[0].draft_markdown]}],
                                "context_requests": [{"tool_name": "get_paper_brief", "arguments": {},
                                                      "caller": "document_reviewer"}]}]}
                        raise AssertionError("Pending opinion evidence must not trigger a Writer")
                result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=checkpoint, **kwargs)
                self.assertEqual(result.sections, drafts)
                self.assertEqual(result.memory.reviewer_findings, [old])
                self.assertNotIn("source_verification_incomplete", [row.type for row in result.reviewer_findings])
                self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker"] +
                    (["report-document-finding-checker-evidence"] if allowed else []))
                self.assertFalse(any(event.action == "document_revise" for event in result.iterations))

    def test_opinion_evidence_is_fetched_before_correction_dispatch_and_can_close_without_writing(self):
        from simple_ar.report.schema import SourceHandle
        kwargs, checkpoint, old, drafts = finding_check_objects(
            max_review_iterations=2, finding_changes={"required_action": "verify"})
        kwargs["config"].allow_source_backtracking = True
        kwargs["config"].max_backtracking_calls = 1
        source = SourceHandle(handle="paper:identity", kind="paper", title="Recorded source",
            metadata={"evidence_passages": [{"text": "The identity was not independently verified."}]})
        kwargs["context"].source_handles = [source]
        checkpoint["memory"]["source_handles"] = [source.model_dump(mode="json")]
        labels = []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "finding_checks": [{"finding_id": old.finding_id, "status": "unresolved",
                            "explanation": "Check the source before deciding whether prose needs work."}],
                        "context_requests": [{"tool_name": "get_paper_brief", "arguments": {"handle": source.handle},
                                              "caller": "document_reviewer"}]}]}
                if label == "report-document-finding-checker-evidence":
                    view = json.loads(prompt)
                    test.assertEqual(view["supplementary_evidence"][-1]["status"], "ok")
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "finding_checks": [{"finding_id": old.finding_id, "status": "not_applicable",
                            "explanation": "Current prose and the recorded passage both retain the uncertainty.",
                            "draft_quotes": [drafts[0].draft_markdown], "evidence_quotes": [{
                                "pointer": "/source_evidence/0/metadata/evidence_passages/0/text",
                                "quote": "The identity was not independently verified.", "role": "recorded_material"}]}]}]}
                raise AssertionError("No correction remains after evidence checking")
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(result.sections, drafts)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertIn(old, result.reviewer_findings)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
                                  "report-document-finding-checker-evidence"])
        self.assertEqual(len([event for event in result.iterations if event.action == "document_finding_context"]), 1)

    def test_unsent_contract_merge_retains_distinct_identities_and_role_records(self):
        from simple_ar.report.schema import ReportToolCall
        _, _, old, _ = finding_check_objects()
        same_prose = old.model_copy(update={"finding_id": "new-identity"})
        lookup = ReportToolCall(tool_name="get_synthesis_brief", arguments={})
        first = ReportSectionReview(section_id="scope", verdict="warning", findings=[same_prose],
            revision_instructions=["Bound the current claim."], context_requests=[lookup])
        second = ReportSectionReview(section_id="scope", verdict="revise_required", findings=[old],
            revision_instructions=["Bound the current claim.", "Retain the counterexample."], context_requests=[lookup])
        other = ReportSectionReview(section_id="limits", verdict="pass")
        before = [row.model_dump(mode="json") for row in (first, second, other)]
        merged = coalesce_document_reviews([first, other, second])
        self.assertEqual([row.section_id for row in merged], ["scope", "limits"])
        self.assertEqual(merged[0].findings, [same_prose, old])
        self.assertEqual(merged[0].revision_instructions, ["Bound the current claim.", "Retain the counterexample."])
        self.assertEqual(merged[0].context_requests, [lookup])
        self.assertEqual(merged[0].verdict, "revise_required")
        merged[0].findings[0].message = "Changed only in the candidate contract."
        self.assertEqual([row.model_dump(mode="json") for row in (first, second, other)], before)

    def test_fresh_and_checked_old_requirements_use_one_candidate_contract(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        kwargs["config"].max_review_iterations = 2
        fresh = old.model_copy(update={"finding_id": "fresh-defect", "message": "A separate current error remains."})
        labels = []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "findings": [fresh.model_dump(mode="json")], "revision_instructions": ["Fix the current claim."]}]}
                if label == "report-document-finding-checker":
                    return opinion_check_response(old, verdict="revise_required", status="unresolved",
                        explanation="The original requirement still applies.")
                if label == "report-document-reviser-scope":
                    view = prompt_payload(prompt)
                    test.assertEqual({f["finding_id"] for f in view["review_findings"]}, {old.finding_id, fresh.finding_id})
                    test.assertEqual(view["review_instructions"], ["Fix the current claim."])
                    return {"section_id": "scope", "draft_markdown": "A bounded corrected account."}
                if label == "report-document-verifier-scope":
                    view = prompt_payload(prompt)
                    test.assertEqual({f["finding_id"] for f in view["revision_context"]["target_findings"]},
                                     {old.finding_id, fresh.finding_id})
                    return {"section_id": "scope", "verdict": "pass"}
                test.assertEqual(label, "report-document-verifier")
                return {"section_reviews": []}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels.count("report-document-reviser-scope"), 1)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertIn(old, result.reviewer_findings)
        self.assertIn(fresh, result.reviewer_findings)
        contract = next(row for row in result.iterations if row.action == "document_review")
        self.assertEqual(set(f.finding_id for f in contract.findings), {old.finding_id, fresh.finding_id})

    def test_omitted_or_unresolved_opinion_remains_active(self):
        for response in ({"section_reviews": []}, {"section_reviews": [{"section_id": "scope",
                "verdict": "revise_required", "finding_checks": [{"finding_id": "old-assumption",
                    "status": "unresolved", "explanation": "The supplied evidence is insufficient."}]}]}):
            kwargs, checkpoint, old, drafts = finding_check_objects()
            class Client:
                def ask_json(self, system, prompt, *, label="", **ignored):
                    return response if label == "report-document-finding-checker" else {"section_reviews": []}
            result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                      completed_checkpoint=checkpoint, **kwargs)
            self.assertIn(old, result.memory.reviewer_findings)
            self.assertEqual(result.sections, drafts)

    def test_missing_opinion_uses_existing_format_correction_not_another_writer_round(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        second = old.model_copy(update={'finding_id': 'second-opinion', 'section_id': 'limits'})
        checkpoint['memory']['reviewer_findings'].append(second.model_dump(mode='json'))
        checkpoint['reviewer_findings'].append(second.model_dump(mode='json'))
        labels, saved = [], []
        test = self
        def answer(opinions):
            return {'section_reviews': [{'section_id': opinion.section_id, 'verdict': 'pass',
                'finding_checks': [{'finding_id': opinion.finding_id, 'status': 'not_applicable',
                    'explanation': 'The current draft explicitly preserves the evidence limitation.',
                    'draft_quotes': [drafts[0].draft_markdown]}]} for opinion in opinions]}
        partial = answer([old])
        class Client:
            def ask_json(self, system, prompt, *, label='', **ignored):
                labels.append(label)
                view = json.loads(prompt)
                if label == 'report-document-reviewer':
                    return {'section_reviews': []}
                if label == 'report-document-finding-checker':
                    return partial
                test.assertEqual(label, 'report-document-finding-checker-format-correction')
                test.assertIn('second-opinion', view['response_format_correction']['validation_error'])
                test.assertEqual(view['response_format_correction']['rejected_response'], partial)
                return answer([old, second])
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs['context']),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(labels, ['report-document-reviewer', 'report-document-finding-checker',
                                 'report-document-finding-checker-format-correction'])
        self.assertEqual(result.sections, drafts)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertEqual(next(row for row in result.iterations if row.action == 'document_finding_check_rejected')
                         .rejected_review['response'], partial)
        before = list(labels)
        run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs['context']),
                         completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(labels, before)

    def test_combined_candidate_resume_keeps_contract_and_new_inspection_issue(self):
        kwargs, checkpoint, old, _ = finding_check_objects()
        kwargs["config"].max_review_iterations = 2
        fresh = old.model_copy(update={"finding_id": "fresh-defect", "message": "An independent current defect."})
        later = old.model_copy(update={"finding_id": "later-defect", "message": "A later inspection's separate concern."})
        labels, saved = [], []
        test = self
        class Client:
            resumed = False
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    finding = later if self.resumed else fresh
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                        "findings": [finding.model_dump(mode="json")]}]}
                if label == "report-document-finding-checker":
                    test.assertFalse(self.resumed)
                    return opinion_check_response(old, verdict="revise_required", status="unresolved",
                        explanation="The original requirement still applies.")
                if label == "report-document-reviser-scope":
                    test.assertFalse(self.resumed, "Reuse the saved candidate")
                    return {"section_id": "scope", "draft_markdown": "A saved corrected candidate."}
                if label == "report-document-verifier-scope":
                    view = prompt_payload(prompt)
                    test.assertEqual({f["finding_id"] for f in view["revision_context"]["target_findings"]},
                                     {old.finding_id, fresh.finding_id})
                    return {"section_id": "scope", "verdict": "pass"}
                test.assertEqual(label, "report-document-verifier")
                return {"section_reviews": []}
        client = Client()
        def interrupt(row):
            saved.append(row)
            if any(event["action"] == "document_revise" for event in row["iterations"]):
                raise RuntimeError("Interrupted after combined candidate save")
        with self.assertRaisesRegex(RuntimeError, "combined candidate"):
            run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=interrupt, **kwargs)
        client.resumed = True
        result = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(labels.count("report-document-reviser-scope"), 1)
        self.assertEqual(sum(row.action == "document_revise" for row in result.iterations), 1)
        self.assertIn(later, result.memory.reviewer_findings)
        self.assertIn(later, result.reviewer_findings)
        self.assertNotIn(old, result.memory.reviewer_findings)
        self.assertNotIn(fresh, result.memory.reviewer_findings)
        self.assertIn("A saved corrected candidate.", result.report_body)

    def test_failed_or_unanchored_check_cannot_clear_history(self):
        for failure in (LLMError("Unavailable"), "The obsolete claim is in an old draft."):
            kwargs, checkpoint, old, drafts = finding_check_objects()
            class Client:
                def ask_json(self, system, prompt, *, label="", **ignored):
                    if label.startswith("report-document-finding-checker"):
                        if isinstance(failure, Exception):
                            raise failure
                        return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                            "finding_checks": [{"finding_id": old.finding_id, "status": "resolved",
                                "explanation": "This is no longer a defect.", "draft_quotes": [failure]}]}]}
                    return {"section_reviews": []}
            result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                      completed_checkpoint=checkpoint, **kwargs)
            self.assertIn(old, result.memory.reviewer_findings)
            self.assertTrue(any(row.finding_id == "document-finding-check-unavailable"
                                for row in result.memory.reviewer_findings))

    def test_new_findings_are_not_erased_by_old_opinion_closure(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        new = old.model_copy(update={"finding_id": "new-defect", "message": "A different issue remains."})
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label == "report-document-finding-checker":
                    return opinion_check_response(old, verdict="pass", status="not_applicable",
                        explanation="The previous premise does not apply to this qualified draft.",
                        draft_quotes=[drafts[0].draft_markdown])
                return {"section_reviews": [{"section_id": "limits", "verdict": "revise_required",
                    "findings": [new.model_copy(update={"section_id": "limits"}).model_dump(mode="json")]}]}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertNotIn(old, result.memory.reviewer_findings)
        self.assertTrue(any(row.finding_id == "new-defect" for row in result.memory.reviewer_findings))

    def test_unknown_duplicate_empty_or_missing_quotes_are_rejected(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        base = {"finding_id": old.finding_id, "status": "resolved",
                "explanation": "The current draft retains uncertainty.", "draft_quotes": [drafts[0].draft_markdown]}
        for checks in ([{**base, "finding_id": "unknown"}], [base, base],
                       [{**base, "draft_quotes": []}], [{**base, "draft_quotes": [""]}],
                       [{**base, "explanation": ""}]):
            class Client:
                def ask_json(self, *args, **ignored):
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass", "finding_checks": checks}]}
            with self.assertRaises(LLMResponseError):
                review_document(client=Client(), **document_review_inputs(
                    kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                    historical_findings=[old]))

    def test_cross_section_quote_requires_explicit_source_and_keeps_opinion_target(self):
        kwargs, _, old, drafts = finding_check_objects()
        drafts[1].draft_markdown = "A different section explicitly records the remaining uncertainty."
        check = {"finding_id": old.finding_id, "status": "not_applicable",
                 "explanation": "Both sections appropriately preserve the limitation.",
                 "draft_quotes": [{"section_id": "scope", "quote": drafts[0].draft_markdown},
                                  {"section_id": "limits", "quote": drafts[1].draft_markdown}]}
        response = {"section_reviews": [{"section_id": "scope", "verdict": "pass", "finding_checks": [check]}]}
        class Client:
            def ask_json(self, *args, **ignored):
                return response
        def run():
            return review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"], historical_findings=[old]))
        review = run()[0]
        self.assertEqual(review.section_id, "scope")
        self.assertEqual(review.finding_checks[0].draft_quotes[1].section_id, "limits")
        for bad in ({"section_id": "unknown", "quote": drafts[1].draft_markdown},
                    {"section_id": "scope", "quote": drafts[1].draft_markdown},
                    {"section_id": "limits", "quote": "A paraphrase rather than the supplied text."},
                    {"section_id": "limits", "quote": ""}, drafts[1].draft_markdown):
            with self.subTest(bad=bad):
                check["draft_quotes"] = [bad]
                with self.assertRaises(LLMResponseError):
                    run()
        check["draft_quotes"] = [{"section_id": "limits", "quote": drafts[1].draft_markdown}]
        response["section_reviews"][0]["section_id"] = "limits"
        with self.assertRaisesRegex(LLMResponseError, "unknown or repeated opinion"):
            run()

    def test_adopting_another_correction_cannot_clear_an_unchecked_old_issue(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        kwargs["config"].max_review_iterations = 1
        new = old.model_copy(update={"finding_id": "new-defect", "message": "A separate issue needs correction."})
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "revise_required",
                                                "findings": [new.model_dump(mode="json")]}]}
                if label in {"report-document-finding-checker", "report-document-finding-checker-format-correction",
                             "report-document-verifier"}:
                    return {"section_reviews": []}
                if label == "report-document-reviser-scope":
                    return {"section_id": "scope", "draft_markdown": "One corrected issue; the other still needs evidence."}
                if label == "report-document-verifier-scope":
                    return {"section_id": "scope", "verdict": "pass"}
                raise AssertionError(label)
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertNotIn(new, result.memory.reviewer_findings)
        self.assertTrue(next(row for row in result.iterations if row.action == "document_revise").adopted)

    def test_old_iteration_shape_does_not_gain_empty_check_fields(self):
        old = ReportIterationRecord(iteration=1, section_id="scope", action="document_review", status="pass")
        self.assertNotIn("finding_checks", old.model_dump(mode="json"))
        self.assertNotIn("rejected_review", old.model_dump(mode="json"))
        self.assertNotIn("requested_findings", old.model_dump(mode="json"))

    def test_rejected_answer_is_retained_without_closing_or_rewriting(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        rejected = {"section_reviews": [{"section_id": "scope", "verdict": "pass",
            "finding_checks": [{"finding_id": old.finding_id, "status": "resolved",
                "explanation": "Claims to have resolved it.", "draft_quotes": ["Only in an obsolete draft."]}]}]}
        labels, saved = [], []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                return rejected if label.startswith("report-document-finding-checker") else {"section_reviews": []}
        client = Client()
        result = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
                                  "report-document-finding-checker-format-correction"])
        event = next(row for row in result.iterations if row.action == "document_finding_check_rejected")
        self.assertEqual(event.rejected_review["response"], rejected)
        self.assertIn("absent from the current section", event.summary)
        self.assertIn(old, result.memory.reviewer_findings)
        self.assertEqual(result.sections, drafts)
        before = len(labels)
        resumed = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(resumed.iterations, result.iterations)
        self.assertEqual(len(labels), before)

    def test_one_format_correction_uses_rejection_without_adding_source_facts(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        rejected = {"section_reviews": [{"section_id": "unknown", "verdict": "pass"}]}
        labels = []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                view = json.loads(prompt)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    return rejected
                test.assertEqual(label, "report-document-finding-checker-format-correction")
                test.assertEqual(view["response_format_correction"]["rejected_response"], rejected)
                test.assertIn("unknown section", view["response_format_correction"]["validation_error"])
                test.assertEqual(view["correction_role"], "rejected_model_answer_not_evidence")
                test.assertEqual(view["historical_findings_to_check"][0]["finding_id"], old.finding_id)
                return opinion_check_response(old, verdict="pass", status="not_applicable",
                    explanation="The current draft preserves the uncertainty.",
                    draft_quotes=[{"section_id": "scope", "quote": drafts[0].draft_markdown}])
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(len(labels), 3)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertEqual(result.sections, drafts)
        correction = next(row for row in result.iterations if row.action == "document_finding_format_correction")
        self.assertEqual(correction.status, "completed")
        self.assertTrue(correction.rejected_review["validated_reviews"])
        self.assertIn(old, result.reviewer_findings)

    def test_correction_restores_rejected_context_and_exact_opinion_set(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        new = old.model_copy(update={"finding_id": "fresh", "section_id": "limits"})
        kwargs["context"].synthesis_markdown = "Saved interpretation, not primary evidence."
        labels, saved = [], []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "limits", "verdict": "revise_required",
                                                "findings": [new.model_dump(mode="json")]}]}
                if label == "report-document-finding-checker":
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}]}]}
                if label == "report-document-finding-checker-evidence":
                    return {"section_reviews": [{"section_id": "unknown", "verdict": "pass"}]}
                test.assertEqual(label, "report-document-finding-checker-evidence-format-correction")
                view = json.loads(prompt)
                test.assertEqual([row["finding_id"] for row in view["historical_findings_to_check"]], [old.finding_id])
                test.assertEqual(view["supplementary_evidence"][-1]["tool_name"], "get_synthesis_brief")
                return opinion_check_response(old, verdict="pass", status="not_applicable",
                    explanation="The adopted text retains the uncertainty.",
                    draft_quotes=[drafts[0].draft_markdown])
        def sink(row):
            saved.append(row)
            if row["iterations"] and row["iterations"][-1]["action"] == "document_finding_check_rejected":
                raise RuntimeError("Interrupted before correction")
        client = Client()
        with self.assertRaisesRegex(RuntimeError, "before correction"):
            run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
        before = len(labels)
        gateway = ReportToolGateway(kwargs["context"])
        gateway.call = lambda request: test.fail("Do not repeat persisted lookup")
        result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(labels[before:], ["report-document-finding-checker-evidence-format-correction"])
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        self.assertIn(new, result.memory.reviewer_findings)
        self.assertNotIn(old, result.memory.reviewer_findings)

    def test_correction_intent_resumes_once_and_completed_response_is_reused(self):
        for interrupt_status in ("started", "completed"):
            with self.subTest(interrupt_status=interrupt_status):
                kwargs, checkpoint, old, drafts = finding_check_objects()
                labels, saved = [], []
                class Client:
                    def ask_json(self, system, prompt, *, label="", **ignored):
                        labels.append(label)
                        if label == "report-document-reviewer":
                            return {"section_reviews": []}
                        if label == "report-document-finding-checker":
                            return {"section_reviews": [{"section_id": "unknown", "verdict": "pass"}]}
                        return opinion_check_response(old, verdict="pass", status="not_applicable",
                            explanation="The current claim is qualified.",
                            draft_quotes=[drafts[0].draft_markdown])
                def sink(row):
                    saved.append(row)
                    if not row["iterations"]:
                        return
                    event = row["iterations"][-1]
                    if event["action"] == "document_finding_format_correction" and event["status"] == interrupt_status:
                        raise RuntimeError("Interrupted at correction checkpoint")
                client = Client()
                with self.assertRaisesRegex(RuntimeError, "correction checkpoint"):
                    run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                        completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
                before = len(labels)
                result = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                    completed_checkpoint=saved[-1], **kwargs)
                self.assertEqual(labels[before:], ["report-document-finding-checker-format-correction"]
                                 if interrupt_status == "started" else [])
                self.assertNotIn(old, result.memory.reviewer_findings)
                correction = next(row for row in result.iterations
                                  if row.action == "document_finding_format_correction" and row.status == "completed")
                self.assertTrue(correction.rejected_review["validated_reviews"])
                self.assertEqual(sum(row.action == "document_finding_format_correction"
                                     for row in result.iterations), 1)

    def test_unparsed_transport_or_budget_failure_has_no_format_retry(self):
        for error in (LLMError("Budget exhausted before request"), LLMResponseError("Response did not contain JSON")):
            kwargs, checkpoint, old, drafts = finding_check_objects()
            labels = []
            class Client:
                def ask_json(self, system, prompt, *, label="", **ignored):
                    labels.append(label)
                    if label == "report-document-reviewer":
                        return {"section_reviews": []}
                    raise error
            result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, **kwargs)
            self.assertEqual(len(labels), 2)
            self.assertFalse(any(row.action == "document_finding_format_correction" for row in result.iterations))
            self.assertIn(old, result.memory.reviewer_findings)

    def test_rejected_answer_cannot_silently_expand_review_window_during_correction(self):
        kwargs, _, old, drafts = finding_check_objects()
        kwargs["config"].max_document_review_prompt_chars = 90_000
        class Client:
            def ask_json(self, *args, **ignored):
                raise AssertionError("Oversized correction must fail before a model call")
        with self.assertRaisesRegex(ValueError, "bounded evidence window"):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                format_correction={"validation_error": "bad structure", "rejected_response": "x" * 90_000}))

    def test_independent_inspection_has_the_same_single_format_correction(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        checkpoint["memory"]["reviewer_findings"] = []
        labels = []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                return {"section_reviews": "not a list"} if len(labels) == 1 else {"section_reviews": []}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-reviewer-format-correction"])
        self.assertEqual(result.sections, drafts)
        self.assertFalse(result.memory.reviewer_findings)

    def test_final_verifier_recovers_its_correction_without_new_inspection(self):
        for status in ("started", "completed"):
            kwargs, checkpoint, old, drafts = finding_check_objects()
            event = ReportIterationRecord(iteration=1, section_id="", action="document_format_correction",
                status=status, summary="Original validation error", rejected_review={
                    "label": "report-document-verifier", "format_correction": True})
            if status == "completed":
                event.rejected_review["validated_reviews"] = [{"section_id": "scope", "verdict": "pass"}]
            checkpoint["iterations"] = [event.model_dump(mode="json")]
            class Client:
                def ask_json(self, *args, **ignored):
                    raise AssertionError("Do not restart an inspection or spend another correction")
            result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                      completed_checkpoint=checkpoint, **kwargs)
            self.assertEqual(result.sections, drafts)
            self.assertIn(old, result.memory.reviewer_findings)
            self.assertEqual(any(row.finding_id == "document-recheck-unavailable"
                for row in result.memory.reviewer_findings), status == "started")

    def test_final_verifier_context_stays_with_that_role_on_resume(self):
        from simple_ar.report.schema import ReportToolResult
        kwargs, checkpoint, old, drafts = finding_check_objects()
        context = ReportToolResult(tool_name="get_synthesis_brief", content={"text": "Saved requested context."})
        checkpoint["tool_results"] = [context.model_dump(mode="json")]
        checkpoint["iterations"] = [ReportIterationRecord(iteration=1, section_id="scope",
            action="document_verifier_context", status="pass", tool_results=[context]).model_dump(mode="json")]
        labels = []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                test.assertEqual(json.loads(prompt)["supplementary_evidence"][-1]["tool_name"], "get_synthesis_brief")
                return {"section_reviews": []}
        gateway = ReportToolGateway(kwargs["context"])
        gateway.call = lambda request: test.fail("Do not repeat the final verifier's lookup")
        result = run_report_agent(client=Client(), gateway=gateway, completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels, ["report-document-verifier-evidence"])
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)
        self.assertEqual(result.sections, drafts)

    def test_opinion_context_resume_keeps_the_saved_request_before_new_issues(self):
        from simple_ar.report.schema import ReportToolResult
        kwargs, checkpoint, old, drafts = finding_check_objects()
        fresh = old.model_copy(update={"finding_id": "fresh", "section_id": "limits"})
        checkpoint["memory"]["reviewer_findings"].append(fresh.model_dump(mode="json"))
        context = ReportToolResult(tool_name="get_synthesis_brief", content={"text": "Saved interpretation."})
        checkpoint["tool_results"] = [context.model_dump(mode="json")]
        checkpoint["iterations"] = [ReportIterationRecord(iteration=1, section_id="limits", action="document_inspection",
            status="revise_required", findings=[fresh], revision_instructions=["Preserve this requested correction."]).model_dump(mode="json"),
            ReportIterationRecord(iteration=2, section_id="scope", action="document_finding_context",
                status="pass", tool_results=[context], requested_findings=[old]).model_dump(mode="json")]
        labels = []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                view = json.loads(prompt)
                test.assertEqual([row["finding_id"] for row in view["historical_findings_to_check"]], [old.finding_id])
                return unresolved_opinion_response(view)
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertEqual(labels, ["report-document-finding-checker-evidence"])
        self.assertIn(fresh, result.memory.reviewer_findings)
        self.assertIn(old, result.memory.reviewer_findings)

    def test_rejected_check_checkpoint_preserves_fresh_inspection_on_interruption(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        new = old.model_copy(update={"section_id": "limits", "finding_id": "fresh-defect"})
        saved = []
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "limits", "verdict": "revise_required",
                                                "findings": [new.model_dump(mode="json")]}]}
                return {"section_reviews": [{"section_id": "unknown", "verdict": "pass"}]}
        def sink(row):
            saved.append(row)
            if row["iterations"] and row["iterations"][-1]["action"] == "document_finding_check_rejected":
                raise RuntimeError("Interrupted after rejected answer")
        with self.assertRaisesRegex(RuntimeError, "rejected answer"):
            run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
        self.assertIn(new.model_dump(mode="json"), saved[-1]["memory"]["reviewer_findings"])
        self.assertIn(old.model_dump(mode="json"), saved[-1]["memory"]["reviewer_findings"])
        event = ReportIterationRecord.model_validate(saved[-1]["iterations"][-1])
        self.assertEqual(event.rejected_review["response"]["section_reviews"][0]["section_id"], "unknown")

    def test_validation_callback_retains_other_malformed_parsed_answers(self):
        kwargs, _, _, drafts = finding_check_objects()
        for response in ({"section_reviews": "wrong shape"},
                         {"section_reviews": [{"section_id": "scope", "verdict": "invalid verdict"}]}):
            rejected = []
            class Client:
                def ask_json(self, *args, **ignored):
                    return response
            with self.assertRaises((LLMResponseError, ValueError)):
                review_document(client=Client(), **document_review_inputs(
                    kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                    on_invalid_response=lambda raw, reason: rejected.append((raw, reason))))
            self.assertEqual(rejected[0][0], response)
            self.assertTrue(rejected[0][1])

    def test_model_cannot_close_controller_failure_as_an_old_opinion(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        control = old.model_copy(update={"finding_id": "scope-document-revision-budget",
                                        "type": "document_revision_unresolved"})
        checkpoint["memory"]["reviewer_findings"].append(control.model_dump(mode="json"))
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                view = json.loads(prompt)
                test.assertEqual([row["finding_id"] for row in view["historical_findings_to_check"]], [old.finding_id])
                return opinion_check_response(old, verdict="pass", status="not_applicable",
                    explanation="The current draft appropriately retains the uncertainty.",
                    draft_quotes=[drafts[0].draft_markdown])
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                  completed_checkpoint=checkpoint, **kwargs)
        self.assertIn(control, result.memory.reviewer_findings)
        self.assertNotIn(old, result.memory.reviewer_findings)
        with self.assertRaisesRegex(ValueError, "controller failures"):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                historical_findings=[control]))

    def test_owner_success_clears_only_its_old_service_failure(self):
        for covered in (True, False):
            kwargs, checkpoint, old, drafts = finding_check_objects()
            controls = [old.model_copy(update={"finding_id": identifier, "section_id": "",
                "type": "document_review_unavailable"}) for identifier in (
                    "document-review-unavailable", "document-finding-check-unavailable", "document-recheck-unavailable")]
            checkpoint["memory"]["reviewer_findings"].extend(row.model_dump(mode="json") for row in controls)
            class Client:
                def ask_json(self, system, prompt, *, label="", **ignored):
                    if label == "report-document-reviewer" or not covered:
                        return {"section_reviews": []}
                    return opinion_check_response(old, verdict="revise_required", status="unresolved",
                        explanation="The evidence is still insufficient.")
            result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
                                      completed_checkpoint=checkpoint, **kwargs)
            current = {row.finding_id for row in result.memory.reviewer_findings}
            self.assertNotIn("document-review-unavailable", current)
            self.assertEqual("document-finding-check-unavailable" in current, not covered)
            self.assertIn("document-recheck-unavailable", current)
            self.assertIn(old, result.memory.reviewer_findings)

    def test_control_deduplication_preserves_owner_not_repeated_payload(self):
        _, _, old, _ = finding_check_objects()
        first = old.model_copy(update={"finding_id": "document-review-unavailable",
            "section_id": "", "type": "document_review_unavailable"})
        second = first.model_copy(update={"finding_id": "document-recheck-unavailable"})
        duplicate_opinion = old.model_copy(update={"finding_id": "same-content-other-id"})
        self.assertEqual(_dedupe_findings([first, second, first, old, duplicate_opinion]),
                         [first, second, old, duplicate_opinion])

    def test_exact_deduplication_retains_changed_action_severity_and_evidence(self):
        _, _, old, _ = finding_check_objects()
        changed = [old.model_copy(update={"required_action": "verify"}),
                   old.model_copy(update={"severity": "minor"}),
                   old.model_copy(update={"draft_quotes": []})]
        self.assertEqual(_dedupe_findings([old, *changed, old, *changed]), [old, *changed])

    def test_collision_handles_preserve_originals_and_avoid_supplied_ids(self):
        from simple_ar.report.editor import historical_opinion_handles
        _, _, old, _ = finding_check_objects()
        second = old.model_copy(update={"message": "A different allegation from another review."})
        reserved = old.model_copy(update={"finding_id": old.finding_id + "::opinion-1"})
        other_section = old.model_copy(update={"section_id": "limits"})
        opinions = [old, second, reserved, other_section]
        before = [row.model_dump(mode="json") for row in opinions]
        handles = historical_opinion_handles(opinions)
        self.assertEqual(len(handles), 4)
        self.assertEqual(list(handles.values()), opinions)
        self.assertNotIn(("scope", old.finding_id), handles)
        self.assertIs(handles[("scope", reserved.finding_id)], reserved)
        self.assertIs(handles[("limits", old.finding_id)], other_section)
        self.assertEqual(handles, historical_opinion_handles(opinions))
        self.assertEqual(before, [row.model_dump(mode="json") for row in opinions])

    def test_colliding_checks_close_only_the_requested_record_and_resume_call_free(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        second = old.model_copy(update={"message": "A separate unresolved claim uses the same model ID."})
        checkpoint["memory"]["reviewer_findings"].append(second.model_dump(mode="json"))
        checkpoint["reviewer_findings"].append(second.model_dump(mode="json"))
        saved, labels = [], []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                test.assertEqual(label, "report-document-finding-checker")
                view = json.loads(prompt)
                opinions = view["historical_findings_to_check"]
                ids = [row["finding_id"] for row in opinions]
                test.assertEqual(len(set(ids)), 2)
                test.assertTrue(all(row["original_finding_id"] == old.finding_id for row in opinions))
                test.assertEqual(len(view["output_schema"]["section_reviews"][0]["finding_checks"]), 1)
                return {"section_reviews": [{"section_id": "scope", "verdict": "warning",
                    "finding_checks": [{"finding_id": handle, "status": status,
                        "explanation": "Only this requested opinion was evaluated.",
                        "draft_quotes": [drafts[0].draft_markdown]}
                        for handle, status in zip(ids, ("not_applicable", "unresolved"))]}]}
        result = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=checkpoint, checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(result.memory.reviewer_findings, [second])
        self.assertIn(old, result.reviewer_findings)
        self.assertIn(second, result.reviewer_findings)
        event = next(row for row in result.iterations if row.action == "document_finding_check")
        self.assertEqual(event.requested_findings, [old, second])
        self.assertEqual(result.sections, drafts)
        before = list(labels)
        resumed = run_report_agent(client=Client(), gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertEqual(labels, before)
        self.assertEqual(resumed.memory.reviewer_findings, [second])

    def test_raw_ambiguous_id_cannot_close_either_collision_member(self):
        kwargs, _, old, drafts = finding_check_objects()
        second = old.model_copy(update={"message": "Another original opinion."})
        class Client:
            def ask_json(self, system, prompt, **ignored):
                return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                    "finding_checks": [{"finding_id": old.finding_id, "status": "resolved",
                        "explanation": "Ambiguous closure must fail.",
                        "draft_quotes": [drafts[0].draft_markdown]}]}]}
        with self.assertRaisesRegex(LLMResponseError, "unknown or repeated opinion"):
            review_document(client=Client(), **document_review_inputs(
                kwargs["memory"], kwargs["config"], drafts, template=kwargs["template"],
                historical_findings=[old, second]))

    def test_collision_handles_survive_rejected_check_and_format_recovery(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        second = old.model_copy(update={"message": "A separate opinion with a reused ID."})
        checkpoint["memory"]["reviewer_findings"].append(second.model_dump(mode="json"))
        labels, saved, handles = [], [], []
        test = self
        class Client:
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": []}
                view = json.loads(prompt)
                ids = [row["finding_id"] for row in view["historical_findings_to_check"]]
                if label == "report-document-finding-checker":
                    handles.extend(ids)
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "finding_checks": [{"finding_id": old.finding_id, "status": "resolved",
                            "explanation": "This ambiguous raw ID must be rejected.",
                            "draft_quotes": [drafts[0].draft_markdown]}]}]}
                test.assertEqual(label, "report-document-finding-checker-format-correction")
                test.assertEqual(ids, handles)
                return {"section_reviews": [{"section_id": "scope", "verdict": "warning",
                    "finding_checks": [{"finding_id": handle, "status": status,
                        "explanation": "This judgement addresses only the identified opinion.",
                        "draft_quotes": [drafts[0].draft_markdown]}
                        for handle, status in zip(ids, ("not_applicable", "unresolved"))]}]}
        def interrupt(row):
            saved.append(row)
            if row["iterations"] and row["iterations"][-1]["action"] == "document_finding_check_rejected":
                raise RuntimeError("Saved rejected collision check")
        client = Client()
        with self.assertRaisesRegex(RuntimeError, "Saved rejected collision"):
            run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=interrupt, **kwargs)
        result = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], checkpoint_sink=saved.append, **kwargs)
        self.assertEqual(labels, ["report-document-reviewer", "report-document-finding-checker",
            "report-document-finding-checker-format-correction"])
        self.assertEqual(result.memory.reviewer_findings, [second])
        event = next(row for row in result.iterations if row.action == "document_finding_check")
        self.assertEqual(event.requested_findings, [old, second])

    def test_interrupted_opinion_lookup_reuses_context_only_for_that_check(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        kwargs["context"].synthesis_markdown = "A saved unverified interpretation."
        labels, saved = [], []
        test = self
        class Client:
            resumed = False
            def ask_json(self, system, prompt, *, label="", **ignored):
                labels.append(label)
                view = json.loads(prompt)
                if label == "report-document-reviewer":
                    test.assertFalse(any(row["tool_name"] == "get_synthesis_brief"
                        for row in view["supplementary_evidence"]))
                    return {"section_reviews": []}
                if label == "report-document-finding-checker":
                    test.assertFalse(self.resumed, "Saved check context should be reused")
                    return {"section_reviews": [{"section_id": "scope", "verdict": "pass",
                        "context_requests": [{"tool_name": "get_synthesis_brief", "arguments": {}}]}]}
                test.assertEqual(label, "report-document-finding-checker-evidence")
                test.assertEqual(view["supplementary_evidence"][-1]["tool_name"], "get_synthesis_brief")
                return opinion_check_response(old, verdict="pass", status="not_applicable",
                    explanation="An interpretation cannot establish the identity and the draft is qualified.",
                    draft_quotes=[drafts[0].draft_markdown])
        def sink(row):
            saved.append(row)
            if row["iterations"] and row["iterations"][-1]["action"] == "document_finding_context":
                raise RuntimeError("Interrupted after opinion lookup")
        client = Client()
        with self.assertRaisesRegex(RuntimeError, "opinion lookup"):
            run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
        client.resumed = True
        gateway = ReportToolGateway(kwargs["context"])
        gateway.call = lambda request: test.fail("Do not fetch saved check context again")
        result = run_report_agent(client=client, gateway=gateway, completed_checkpoint=saved[-1], **kwargs)
        self.assertFalse(result.memory.reviewer_findings)
        self.assertEqual(labels.count("report-document-finding-checker"), 1)
        self.assertEqual(labels.count("report-document-finding-checker-evidence"), 1)
        self.assertEqual(gateway.call_counts["get_synthesis_brief"], 1)

    def test_interrupted_check_does_not_lose_new_findings_to_old_section_passes(self):
        kwargs, checkpoint, old, drafts = finding_check_objects()
        # Earlier per-section passes must not override the later document owner.
        checkpoint["iterations"] = [ReportIterationRecord(iteration=i, section_id=row.section_id,
            action="review", status="pass").model_dump(mode="json") for i, row in enumerate(drafts, start=1)]
        checkpoint["iterations"].append(ReportIterationRecord(iteration=3, section_id="scope",
            action="document_recheck", status="revise_required", findings=[old]).model_dump(mode="json"))
        new = old.model_copy(update={"finding_id": "new-defect", "section_id": "limits",
                                     "message": "A current issue is still present."})
        saved = []
        class Client:
            resumed = False
            def ask_json(self, system, prompt, *, label="", **ignored):
                if self.resumed:
                    raise AssertionError("Accepted inspection and applied opinion checks cannot be replayed")
                if label == "report-document-reviewer":
                    return {"section_reviews": [{
                        "section_id": "limits", "verdict": "revise_required", "findings": [new.model_dump(mode="json")]}]}
                return opinion_check_response(old, verdict="pass", status="not_applicable",
                    explanation="The current statement is qualified.",
                    draft_quotes=[drafts[0].draft_markdown])
        def sink(row):
            saved.append(row)
            if any(event["action"] == "document_finding_check" for event in row["iterations"]):
                raise RuntimeError("Interrupted after saving opinion check")
        client = Client()
        with self.assertRaisesRegex(RuntimeError, "opinion check"):
            run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
                completed_checkpoint=checkpoint, checkpoint_sink=sink, **kwargs)
        self.assertIn(new.model_dump(mode="json"), saved[-1]["memory"]["reviewer_findings"])
        client.resumed = True
        result = run_report_agent(client=client, gateway=ReportToolGateway(kwargs["context"]),
            completed_checkpoint=saved[-1], **kwargs)
        self.assertIn(new, result.memory.reviewer_findings)
        self.assertNotIn(old, result.memory.reviewer_findings)
