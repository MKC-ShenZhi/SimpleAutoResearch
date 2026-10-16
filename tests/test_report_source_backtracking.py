"""Source rereading is bounded, anchored and uses only registered artifacts."""
import tempfile
import json
import unittest
from unittest.mock import patch

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.report.schema import ReportContext, ReportMemory, ReportRuntimeConfig, ReportToolCall, SourceHandle
from simple_ar.report.templates import load_report_template_bundle
from simple_ar.report.agent import run_report_agent, _section_response, _writer_prompt, _writer_recovery_prompt
from simple_ar.report.schema import ReportSectionDraft, ReportToolResult
from simple_ar.report.schema import ReportSectionPlan
from simple_ar.report.tool_gateway import ReportToolGateway
from simple_ar.report.writing import ReportWritingRequest, run_report_writing_capability
from simple_ar.report.audit import build_report_audit
from simple_ar.integrations.llm import LLMError
from simple_ar.research.contracts import DocumentRecord, TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from tests.report_review_fixtures import draft_quotes


class SourceBacktrackingTests(unittest.TestCase):
    def test_handle_navigation_shares_source_word_number_and_unicode_matching(self):
        from simple_ar.report.retrieval import ReportSourceResolver
        distractor = SourceHandle(handle='paper:background', kind='paper', title='Brisk history in 2014')
        relevant = SourceHandle(handle='paper:measurement', kind='paper', title='Risk at 14 units',
            metadata={'excerpt': '风险控制具有保证'})
        before = relevant.model_dump(mode='json')
        resolver = ReportSourceResolver(ReportContext(topic='Navigation', report_mode='supplied_materials',
                                                     source_handles=[distractor, relevant]))
        self.assertEqual(resolver.search('risk 14'), [relevant])
        self.assertEqual(resolver.search('风险控制保证'), [relevant])
        self.assertEqual(resolver.search('absentword'), [])
        self.assertEqual(relevant.model_dump(mode='json'), before)

    def test_new_and_saved_synthesis_briefs_are_not_primary_verification(self):
        from simple_ar.report.narrative import report_tool_context
        context = ReportContext(topic="Unconfirmed comparison", report_mode="survey",
            synthesis_markdown="Recorded comparison asserts two records describe one study.")
        new = ReportToolGateway(context).call(ReportToolCall(tool_name="get_synthesis_brief"))
        old = ReportToolResult(tool_name="get_synthesis_brief", content={
            "text": context.synthesis_markdown, "truncated": False,
            "total_characters": len(context.synthesis_markdown), "matching_handles": []})
        before = old.model_dump(mode="json")
        self.assertEqual(report_tool_context(old)["content"], new.content)
        self.assertEqual(new.content["text_status"]["evidence_role"], "recorded_derived_context")
        self.assertEqual(new.content["text_status"]["independent_verification"], "not_performed")
        self.assertEqual(old.model_dump(mode="json"), before)
        self.assertEqual(new.content["text"], context.synthesis_markdown)

    def test_reprojecting_new_brief_preserves_coverage_and_author_counts(self):
        from simple_ar.report.narrative import report_tool_context, _prompt_handle_view
        handle = SourceHandle(handle="paper:projected", kind="paper", citation_key="P1", metadata={
            "bibliography": {"authors": [f"Author {i}" for i in range(30)],
                             "notes": [f"Bibliography note {i}" for i in range(7)]},
            "reading_notes": {"method": "long method " * 200,
                              "claim_scopes": [{"claim": f"Claim {i}"} for i in range(8)]}})
        projected = _prompt_handle_view(handle)
        tool = ReportToolResult(tool_name="get_paper_brief", content={"handles": [projected]})
        twice = report_tool_context(tool)["content"]["handles"][0]
        self.assertEqual(twice, projected)
        self.assertEqual(twice["metadata"]["bibliography"]["recorded_authors_count"], 30)
        self.assertEqual(twice["metadata"]["bibliography"]["author_names_omitted_from_prompt"], 24)
        self.assertEqual(twice["metadata"]["bibliography"]["notes_omitted"], 3)
        self.assertTrue(twice["metadata"]["reading_notes_truncated"])
        self.assertEqual(twice["metadata"]["reading_notes"]["claim_scopes_omitted"], 4)
        self.assertEqual(report_tool_context(tool, source_evidence=[projected])["content"]["handles"],
                         [{"handle": handle.handle, "evidence_reference": "source_evidence"}])

    def test_document_brief_references_only_exact_shared_evidence(self):
        from simple_ar.report.narrative import report_tool_context, _prompt_handle_view
        handle = SourceHandle(handle="paper:shared", kind="paper", citation_key="P1",
            metadata={"evidence_passages": [{"chunk_id": "c1", "text": "Original condition."}]})
        tool = ReportToolResult(tool_name="get_paper_brief", content={
            "handles": [handle.model_dump(mode="json")],
            "source_front_matter": {"text": "A separately fetched title."}})
        original = tool.model_dump(mode="json")
        reference = report_tool_context(tool, source_evidence=[_prompt_handle_view(handle)])
        self.assertEqual(reference["content"]["handles"], [{"handle": "paper:shared",
                                                           "evidence_reference": "source_evidence"}])
        self.assertEqual(reference["content"]["source_front_matter"], original["content"]["source_front_matter"])
        different = handle.model_copy(update={"metadata": {"evidence_passages": [
            {"chunk_id": "c2", "text": "Conflicting condition."}]}})
        retained = report_tool_context(tool, source_evidence=[_prompt_handle_view(different)])
        self.assertEqual(retained["content"]["handles"], [_prompt_handle_view(handle)])
        self.assertEqual(tool.model_dump(mode="json"), original)

    def test_briefs_share_bounded_projection_without_losing_source_lookup(self):
        from simple_ar.report.narrative import report_tool_context
        handle = SourceHandle(handle="paper:large", kind="paper", paper_id="large", citation_key="P1",
            metadata={"document_id": "doc", "reading_notes": {"method": "method " * 12000},
                      "evidence_passages": [{"chunk_id": "c3", "text": "Original passage " * 1000}]})
        context = ReportContext(topic="Check evidence", report_mode="survey", source_handles=[handle])
        before = handle.model_dump(mode="json")
        brief = ReportToolGateway(context, documents=self.documents).call(
            ReportToolCall(tool_name="get_paper_brief", arguments={"citation_key": "P1"}))
        projected = brief.content["handles"][0]
        self.assertTrue(projected["metadata"]["reading_notes_truncated"])
        self.assertTrue(projected["metadata"]["evidence_passages"][0]["truncated"])
        self.assertEqual(projected["tool_args"], {"citation_key": "P1"})
        self.assertLess(len(json.dumps(brief.model_dump(mode="json"))), 6000)
        legacy = ReportToolResult(tool_name="get_paper_brief", content={"handles": [before]})
        legacy_before = legacy.model_dump(mode="json")
        self.assertEqual(report_tool_context(legacy)["content"]["handles"], [projected])
        self.assertEqual(legacy.model_dump(mode="json"), legacy_before)
        self.assertEqual(handle.model_dump(mode="json"), before)
        original = ReportToolGateway(context, documents=self.documents).call(ReportToolCall(
            tool_name="get_neighbor_chunks", arguments={"handle": handle.handle, "chunk_id": "c3"}))
        self.assertIn("Original passage 3", json.dumps(original.content))

    def setUp(self):
        self.context = ReportContext(topic="Source check", report_mode="survey", source_handles=[SourceHandle(
            handle="paper:p", kind="paper", paper_id="p", metadata={"document_id": "doc",
                "evidence_passages": [{"chunk_id": "c3", "text": "previously truncated"}]})])
        self.documents = DocumentBundle(records=[DocumentRecord("doc", "Paper", "local", extraction_status="parsed")],
            fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=[
                *[TextChunk(f"c{i}", "doc", f"Original passage {i}", metadata={"extraction_status": "parsed"}) for i in range(5)],
                TextChunk("foreign", "other", "Other paper")])

    def call(self, gateway, **args):
        return gateway.call(ReportToolCall(tool_name="get_neighbor_chunks", arguments={"handle": "paper:p", **args}))

    def test_actual_passages_are_centered_on_cited_anchor(self):
        result = self.call(ReportToolGateway(self.context, documents=self.documents))
        self.assertEqual(result.status, "ok")
        self.assertEqual([r["chunk_id"] for r in result.content["chunks"]], ["c2", "c3", "c4"])
        self.assertEqual(result.content["chunks"][1]["text"], "Original passage 3")
        self.assertEqual(result.content["source_kind"], "persisted_extracted_text")

    def test_requested_anchor_cannot_cross_source_identity(self):
        gateway = ReportToolGateway(self.context, documents=self.documents)
        self.assertEqual(self.call(gateway, chunk_id="foreign").status, "not_found")
        self.assertEqual(self.call(gateway, chunk_id="missing").status, "not_found")
        result = self.call(gateway, chunk_id="c0", before=3, after=0)
        self.assertEqual([r["chunk_id"] for r in result.content["chunks"]], ["c0"])

    def test_neighbors_use_original_positions_after_ingest_front_matter_deferral(self):
        self.documents.chunks[:] = [
            TextChunk("middle", "doc", "Body", source_path="source.txt", line_start=10),
            TextChunk("last", "doc", "Last page", source_path="source.txt", line_start=100),
            TextChunk("first", "doc", "Front matter", source_path="source.txt", line_start=1)]
        result = self.call(ReportToolGateway(self.context, documents=self.documents), chunk_id="first", before=0, after=1)
        self.assertEqual([row["chunk_id"] for row in result.content["chunks"]], ["first", "middle"])
        self.assertIn("do not guarantee contiguous", result.summary)

    def test_output_is_bounded_and_truncation_visible(self):
        bundle = DocumentBundle([], {}, {}, [], [TextChunk(f"c{i}", "doc", "x" * 6000) for i in range(7)])
        result = self.call(ReportToolGateway(self.context, documents=bundle), before=3, after=3)
        self.assertEqual(sum(len(r["text"]) for r in result.content["chunks"]), 4800)
        self.assertTrue(all(r["truncated"] for r in result.content["chunks"]))
        self.assertTrue(all(r["total_characters"] == 6000 for r in result.content["chunks"]))

    def test_missing_original_is_not_claimed_as_source_read(self):
        result = self.call(ReportToolGateway(self.context))
        self.assertEqual(result.content["source_kind"], "metadata_or_cached_excerpt")
        self.assertNotIn("chunks", result.content)

    def search(self, gateway, query, **kwargs):
        return gateway.call(ReportToolCall(tool_name="search_source_chunks",
            arguments={"handle": "paper:p", "query": query, **kwargs}))

    def test_search_finds_unshown_source_passage_without_crossing_identity(self):
        self.documents.chunks[-2] = TextChunk("c4", "doc", "Calibration uncertainty is 2 percent.")
        self.documents.chunks[-1] = TextChunk("foreign", "other", "Calibration uncertainty is 99 percent.")
        gateway = ReportToolGateway(self.context, documents=self.documents)
        result = self.search(gateway, "calibration uncertainty")
        self.assertEqual([row["chunk_id"] for row in result.content["chunks"]], ["c4"])
        self.assertEqual(result.content["semantic_verification"], "not_performed")
        self.assertEqual(result.content["document_chunk_count"], 5)
        neighbor = self.call(gateway, chunk_id=result.content["chunks"][0]["chunk_id"])
        self.assertIn("2 percent", json.dumps(neighbor.content))
        self.assertNotIn("99 percent", json.dumps(neighbor.content))

    def test_search_window_contains_late_hit_and_exposes_original_character_offsets(self):
        for query in ("calibration uncertainty", "温度下降"):
            with self.subTest(query=query):
                text = "ß前言 " + "filler " * 1100 + query + " is recorded here."
                self.documents.chunks[:] = [TextChunk("late", "doc", text)]
                result = self.search(ReportToolGateway(self.context, documents=self.documents), query)
                row = result.content["chunks"][0]
                self.assertIn(query, row["text"])
                self.assertEqual(row["text"], text[row["character_start"]:row["character_end"]])
                self.assertTrue(row["truncated"])
                self.assertLessEqual(len(row["text"]), 4800)

    def test_search_prefers_query_phrase_and_preserves_short_matching_chunks(self):
        overview = "An encoder and decoder use attention."
        detail = "intro " * 150 + "The encoder-decoder attention sublayer attends to encoder outputs."
        self.documents.chunks[:] = [TextChunk("overview", "doc", overview), TextChunk("detail", "doc", detail)]
        result = self.search(ReportToolGateway(self.context, documents=self.documents), "encoder decoder attention")
        self.assertEqual(result.content["chunks"][0]["chunk_id"], "detail")
        self.assertEqual(result.content["chunks"][0]["text"], detail)
        self.assertFalse(result.content["chunks"][0]["truncated"])

    def test_search_missing_original_unmatched_or_invalid_query_is_not_absence_proof(self):
        missing = self.search(ReportToolGateway(self.context), "calibration")
        self.assertFalse(missing.content["search_performed"])
        gateway = ReportToolGateway(self.context, documents=self.documents)
        absent = self.search(gateway, "unknown-condition")
        self.assertEqual(absent.status, "not_found")
        self.assertIn("not proof", absent.summary)
        self.assertEqual(self.search(gateway, "x" * 501).status, "error")
        self.assertEqual(gateway.call(ReportToolCall(tool_name="search_source_chunks",
            arguments={"handle": "/private/path", "query": "key"})).status, "not_found")

    def test_search_reuses_existing_tool_call_budget(self):
        gateway = ReportToolGateway(self.context, documents=self.documents)
        for _ in range(8):
            self.assertEqual(self.search(gateway, "original passage").status, "ok")
        self.assertEqual(self.search(gateway, "original passage").status, "blocked")

    def test_all_authorized_reads_survive_writing_review_and_recovery(self):
        extra = [ReportToolResult(tool_name="search_source_chunks", status="ok",
            summary=f"evidence-{i}", content={"text": f"evidence-{i}"}) for i in range(8)]
        class Client:
            def ask_json(inner, system, prompt, **kwargs):
                self.assertIn("evidence-7", prompt)
                self.assertIn("evidence-0", prompt)
                payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                self.assertEqual(payload["extra_tool_context_omitted"], 0)
                return {"verdict": "pass"}
        kwargs = self.report_kwargs(Client())
        section = kwargs["memory"].section_plan[0]
        _section_response(client=kwargs["client"], context=self.context,
            template=kwargs["template"], memory=kwargs["memory"], section=section,
            draft=ReportSectionDraft(section_id="method", heading="Method", draft_markdown="A claim."),
            config=kwargs["config"], label="review", extra_context=extra)
        prompt = _writer_prompt(context=self.context, template=kwargs["template"],
            memory=kwargs["memory"], section=section, config=kwargs["config"], extra_context=extra,
            previous_draft=None, review=None, source_batch_index=0, source_batch_count=1,
            include_previous_draft=False, draft_mode="initial")
        prompt = json.loads(prompt[prompt.index("{"):])
        self.assertEqual(prompt["extra_tool_context_omitted"], 0)
        self.assertEqual([row["summary"] for row in prompt["extra_tool_context"]],
            [f"evidence-{i}" for i in range(8)])
        recovery = _writer_recovery_prompt(context=self.context, memory=kwargs["memory"],
            section=section, config=kwargs["config"], previous_draft=None, review=None,
            draft_mode="initial", extra_context=extra)
        self.assertEqual(json.loads(recovery[recovery.index("{"):])["extra_tool_context"],
            prompt["extra_tool_context"])

    def report_kwargs(self, client, **settings):
        memory = ReportMemory(source_handles=self.context.source_handles, section_plan=[
            ReportSectionPlan(section_id="method", heading="Method", goal="Check source", evidence_handles=["paper:p"])])
        config = ReportRuntimeConfig(outline_strategy="template", **settings)
        return dict(client=client, context=self.context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="survey", config=config),
            gateway=ReportToolGateway(self.context, documents=self.documents))

    def test_provisional_pass_reads_then_rejudges_and_revises_the_same_draft(self):
        self.documents.chunks[-2] = TextChunk("c4", "doc", "Calibration uncertainty is 2 percent.")
        labels = []
        class Client:
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                labels.append(label)
                if label == "report-reviewer-method":
                    return {"verdict": "pass", "context_requests": [{"tool_name": "search_source_chunks",
                        "arguments": {"handle": "paper:p", "query": "calibration uncertainty"}}]}
                if label == "report-reviewer-method-evidence":
                    self.assertIn("2 percent", prompt)
                    return {"verdict": "revise_required", "findings": [{"finding_id": "wrong-number",
                        "type": "unsupported_claim", "severity": "major", "message": "Source says 2, not 99.",
                        "draft_quotes": draft_quotes(prompt, "method")}]}
                if "reviewer" in label:
                    self.assertIn("2 percent", prompt)
                    return {"verdict": "pass", "findings": []}
                return {"section_id": "method", "heading": "Method",
                    "draft_markdown": "Calibration uncertainty is 2 percent." if "reviser" in label else "Calibration uncertainty is 99 percent."}
        result = run_report_agent(**self.report_kwargs(Client(), max_review_iterations=1))
        self.assertIn("2 percent", result.report_body)
        self.assertNotIn("99 percent", result.report_body)
        self.assertEqual(labels.count("report-reviewer-method-evidence"), 1)
        self.assertEqual(result.memory.reviewer_findings, [])
        self.assertTrue(any(row.action == "review_context" for row in result.iterations))

    def test_pending_or_disabled_lookup_does_not_become_a_verified_pass(self):
        for enabled in (True, False):
            labels = []
            class Client:
                def ask_json(inner, system, prompt, *, label="", **kwargs):
                    labels.append(label)
                    if "reviewer" in label:
                        return {"verdict": "pass", "context_requests": [{"tool_name": "search_source_chunks",
                            "arguments": {"handle": "paper:p", "query": "missing-condition"}}]}
                    return {"section_id": "method", "draft_markdown": "A claim still awaiting evidence."}
            with self.subTest(enabled=enabled):
                result = run_report_agent(**self.report_kwargs(Client(), max_review_iterations=0, allow_source_backtracking=enabled))
                self.assertTrue(any(row.type == "source_verification_incomplete" for row in result.memory.reviewer_findings))
                audit = build_report_audit(report=result.report_body, report_body=result.report_body,
                    context=self.context, memory=result.memory)
                self.assertEqual(audit.status, "failed")
                self.assertEqual(labels.count("report-reviewer-method-evidence"), int(enabled))

    def test_evidence_followup_failure_saves_source_results_and_resumes_without_refetch(self):
        saved = []
        labels = []
        class Client:
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                labels.append(label)
                if label == "report-reviewer-method-evidence":
                    raise LLMError("Provider unavailable")
                if "reviewer" in label:
                    payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                    if payload["extra_tool_context"]:
                        return {"verdict": "pass"}
                    return {"verdict": "pass", "context_requests": [{"tool_name": "search_source_chunks",
                        "arguments": {"handle": "paper:p", "query": "original passage"}}]}
                return {"section_id": "method", "draft_markdown": "Descriptive source check."}
        kwargs = self.report_kwargs(Client(), max_review_iterations=0)
        with self.assertRaises(LLMError):
            run_report_agent(**kwargs, checkpoint_sink=lambda row: saved.append(json.loads(json.dumps(row))))
        self.assertTrue(saved[-1]["tool_results"])
        before = len(labels)
        resumed_kwargs = self.report_kwargs(Client(), max_review_iterations=0)
        with patch.object(resumed_kwargs["gateway"], "call", wraps=resumed_kwargs["gateway"].call) as call:
            result = run_report_agent(**resumed_kwargs, completed_checkpoint=saved[-1])
            call.assert_not_called()
        self.assertNotIn("report-writer-method", labels[before:])
        self.assertEqual(resumed_kwargs["gateway"].call_counts["search_source_chunks"], 1)
        self.assertEqual(len(result.tool_results), 1)

    def test_mixed_lookup_is_saved_before_writer_failure_and_reused_on_resume(self):
        self._check_mixed_lookup_checkpoint(interrupt_at_save=False)

    def test_mixed_lookup_checkpoint_interruption_does_not_refetch_on_resume(self):
        self._check_mixed_lookup_checkpoint(interrupt_at_save=True)

    def _check_mixed_lookup_checkpoint(self, *, interrupt_at_save):
        saved, labels = [], []
        test = self
        class Client:
            resumed = False
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                labels.append(label)
                if "reviewer" in label:
                    if label.endswith("-round-2"):
                        return {"verdict": "pass"}
                    payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{'):])[0]
                    requests = [] if inner.resumed else [{"tool_name": "search_source_chunks",
                        "arguments": {"handle": "paper:p", "query": "original passage"}}]
                    if inner.resumed:
                        test.assertTrue(payload["extra_tool_context"])
                        test.assertIn("Original passage", json.dumps(payload["extra_tool_context"]))
                    return {"verdict": "revise_required", "context_requests": requests,
                        "findings": [{"finding_id": "qualify", "type": "unsupported_claim",
                            "severity": "major", "required_action": "revise", "message": "Use the source condition.",
                            "draft_quotes": draft_quotes(prompt, "method")}]}
                if "reviser" in label:
                    test.assertIn("Original passage", prompt)
                    if not inner.resumed:
                        raise LLMError("Writer cannot send after the lookup")
                    return {"section_id": "method", "draft_markdown": "Qualified source claim."}
                test.assertFalse(inner.resumed, "Saved pending draft must not be redrafted")
                return {"section_id": "method", "draft_markdown": "Unqualified source claim."}
        client = Client()
        def checkpoint(row):
            saved.append(json.loads(json.dumps(row)))
            if interrupt_at_save and row["tool_results"]:
                raise RuntimeError("Interrupted after mixed lookup checkpoint")
        kwargs = self.report_kwargs(client, max_review_iterations=1)
        failure = RuntimeError if interrupt_at_save else LLMError
        with self.assertRaises(failure):
            run_report_agent(**kwargs, checkpoint_sink=checkpoint)
        self.assertEqual(len(saved[-1]["tool_results"]), 1)
        self.assertEqual(saved[-1]["sections"], [])
        self.assertEqual(saved[-1]["pending_draft"]["draft_markdown"], "Unqualified source claim.")
        event = next(row for row in reversed(saved[-1]["iterations"]) if row["action"] == "review_context")
        self.assertEqual(event["tool_results"], saved[-1]["tool_results"])
        self.assertEqual(event["findings"][0]["finding_id"], "qualify")
        if not interrupt_at_save:
            accepted = saved[-1]["iterations"][-1]
            self.assertEqual(accepted["action"], "review")
            self.assertEqual(accepted["findings"], event["findings"])
            self.assertEqual(accepted["tool_results"], event["tool_results"])
        before = len(labels)
        client.resumed = True
        resumed = self.report_kwargs(client, max_review_iterations=1)
        with patch.object(resumed["gateway"], "call", wraps=resumed["gateway"].call) as call:
            result = run_report_agent(**resumed, completed_checkpoint=saved[-1])
            call.assert_not_called()
        self.assertEqual(resumed["gateway"].call_counts["search_source_chunks"], 1)
        self.assertEqual(len(result.tool_results), 1)
        self.assertEqual(result.sections[0].draft_markdown, "Qualified source claim.")
        self.assertNotIn("report-writer-method", labels[before:])
        self.assertEqual(sum("reviser" in label for label in labels[before:]), 1)
        self.assertEqual(sum("reviewer" in label for label in labels[before:]), 1 if not interrupt_at_save else 2)

    def test_resume_does_not_reset_the_source_tool_allowance(self):
        saved = []
        class Client:
            fail = True
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                if label.endswith("-evidence") and inner.fail:
                    raise LLMError("Provider unavailable")
                if "reviewer" in label:
                    return {"verdict": "pass", "context_requests": [{"tool_name": "search_source_chunks",
                        "arguments": {"handle": "paper:p", "query": "original passage"}}] * (8 if inner.fail else 1)}
                return {"section_id": "method", "draft_markdown": "A source claim."}
        client = Client()
        with self.assertRaises(LLMError):
            run_report_agent(**self.report_kwargs(client, max_review_iterations=0),
                checkpoint_sink=lambda row: saved.append(json.loads(json.dumps(row))))
        self.assertEqual(len(saved[-1]["tool_results"]), 8)
        client.fail = False
        kwargs = self.report_kwargs(client, max_review_iterations=0)
        result = run_report_agent(**kwargs, completed_checkpoint=saved[-1])
        self.assertEqual(result.tool_results[-1].status, "blocked")
        self.assertEqual(kwargs["gateway"].call_counts["search_source_chunks"], 9)
        self.assertTrue(any(row.type == "source_verification_incomplete" for row in result.memory.reviewer_findings))

    def test_document_pass_with_requests_rejudges_after_source_lookup(self):
        for pending in (False, True):
            seen = []
            class Client:
                def ask_json(inner, system, prompt, *, label="", **kwargs):
                    seen.append(label)
                    if label in {"report-document-reviewer", "report-document-reviewer-evidence"}:
                        payload = json.loads(prompt)
                        if label.endswith("-evidence"):
                            self.assertIn("Original passage", json.dumps(payload["supplementary_evidence"]))
                            if not pending:
                                return {"section_reviews": []}
                        return {"section_reviews": [{"section_id": "method", "verdict": "pass",
                            "context_requests": [{"tool_name": "search_source_chunks",
                                "arguments": {"handle": "paper:p", "query": "original passage"}}]}]}
                    if "reviewer" in label:
                        return {"verdict": "pass"}
                    return {"section_id": "method" if "method" in label else "conclusion",
                        "draft_markdown": "A descriptive source check."}
            with self.subTest(pending=pending):
                kwargs = self.report_kwargs(Client(), max_review_iterations=0, document_review=True)
                kwargs["memory"].section_plan.append(ReportSectionPlan(section_id="conclusion", heading="Conclusion", goal="Summarize"))
                result = run_report_agent(**kwargs)
                self.assertEqual(seen.count("report-document-reviewer-evidence"), 1)
                self.assertEqual(bool(result.memory.reviewer_findings), pending)
                self.assertTrue(any(row.action == "document_context" for row in result.iterations))

    def test_document_revision_verifier_cannot_adopt_a_pass_awaiting_evidence(self):
        seen = []
        class Client:
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                seen.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "method", "verdict": "revise_required",
                        "findings": [{"finding_id": "f", "type": "unsupported_claim", "severity": "major",
                            "message": "Check source before adopting a rewrite.",
                            "draft_quotes": draft_quotes(prompt, "method")}]}]}
                if label.startswith("report-document-verifier-method"):
                    return {"verdict": "pass", "context_requests": [{"tool_name": "search_source_chunks",
                        "arguments": {"handle": "paper:p", "query": "missing condition"}}]}
                if "reviewer" in label:
                    return {"verdict": "pass"}
                return {"section_id": "method" if "method" in label else "conclusion",
                    "draft_markdown": "Unadopted rewrite." if "reviser" in label else "Original section."}
        kwargs = self.report_kwargs(Client(), max_review_iterations=1, document_review=True)
        kwargs["memory"].section_plan.append(ReportSectionPlan(section_id="conclusion", heading="Conclusion", goal="Summarize"))
        result = run_report_agent(**kwargs)
        candidate = next(row for row in result.iterations if row.action == "document_revise")
        self.assertFalse(candidate.adopted)
        self.assertIn("Original section", result.report_body)
        self.assertNotIn("Unadopted rewrite", result.report_body)
        self.assertTrue(any(row.type == "source_verification_incomplete" for row in result.memory.reviewer_findings))
        self.assertEqual(seen.count("report-document-verifier-method-evidence"), 1)

    def test_final_document_recheck_does_not_accept_pending_evidence(self):
        seen = []
        class Client:
            def ask_json(inner, system, prompt, *, label="", **kwargs):
                seen.append(label)
                if label == "report-document-reviewer":
                    return {"section_reviews": [{"section_id": "method", "verdict": "revise_required",
                        "findings": [{"finding_id": "f", "type": "unsupported_claim", "severity": "major",
                            "message": "Qualify the claim.", "draft_quotes": draft_quotes(prompt, "method")}]}]}
                if label.startswith("report-document-verifier") and "method" not in label:
                    return {"section_reviews": [{"section_id": "method", "verdict": "pass",
                        "context_requests": [{"tool_name": "search_source_chunks",
                            "arguments": {"handle": "paper:p", "query": "missing condition"}}]}]}
                if "reviewer" in label or "verifier" in label:
                    return {"verdict": "pass"}
                return {"section_id": "method" if "method" in label else "conclusion",
                    "draft_markdown": "Qualified description."}
        kwargs = self.report_kwargs(Client(), max_review_iterations=1, document_review=True)
        kwargs["memory"].section_plan.append(ReportSectionPlan(section_id="conclusion", heading="Conclusion", goal="Summarize"))
        result = run_report_agent(**kwargs)
        self.assertTrue(next(row for row in result.iterations if row.action == "document_revise").adopted)
        self.assertEqual(seen.count("report-document-verifier-evidence"), 1)
        self.assertTrue(any(row.type == "source_verification_incomplete" for row in result.memory.reviewer_findings))

    def test_writer_reads_only_registered_bundle_and_old_snapshots_still_work(self):
        config = ReportRuntimeConfig()
        template = load_report_template_bundle(report_mode="survey", config=config)
        request = ReportWritingRequest(self.context, ReportMemory(), config, template, object())
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            ref = store.write_json("documents.json", self.documents.to_handoff_dict(), schema="document_bundle.v1")
            for refs in ((ref,), ()):
                with patch("simple_ar.report.writing.run_report_agent", return_value=None) as run:
                    run_report_writing_capability(context=CapabilityContext(store=store,
                        attempt=AttemptManifest("writer"), inputs=refs), request=request)
                    gateway = run.call_args.kwargs["gateway"]
                    kind = self.call(gateway).content["source_kind"]
                    self.assertEqual(kind, "persisted_extracted_text" if refs else "metadata_or_cached_excerpt")

    def test_revision_and_both_verifiers_share_original_source_window(self):
        seen = []
        class Client:
            def ask_json(client_self, system, prompt, *, label="", **kwargs):
                seen.append(label)
                if label == "report-document-reviewer":
                    payload = json.loads(prompt)
                    spec = next(row for row in payload["context_tools"] if row["name"] == "get_neighbor_chunks")
                    self.assertIn("chunk_id", spec["input_schema"]["properties"])
                    return {"section_reviews": [{"section_id": "method", "verdict": "revise_required",
                        "findings": [{"finding_id": "f", "type": "evidence_gap", "severity": "major",
                                      "message": "Check original passage", "section_id": "method",
                                      "draft_quotes": draft_quotes(prompt, "method")}],
                        "context_requests": [{"tool_name": "get_neighbor_chunks", "arguments": {"handle": "paper:p"}}]}]}
                if label in {"report-document-reviser-method", "report-document-verifier-method"}:
                    self.assertIn("Original passage 3", prompt)
                if label == "report-document-verifier":
                    payload = json.loads(prompt)
                    self.assertEqual(payload["supplementary_evidence"][0]["content"]["source_kind"], "persisted_extracted_text")
                    return {"section_reviews": []}
                if "reviewer" in label or "verifier" in label:
                    return {"verdict": "pass", "findings": []}
                section_id = "method" if "method" in label else "conclusion"
                return {"section_id": section_id, "heading": section_id, "draft_markdown": "Checked content."}
        memory = ReportMemory(source_handles=self.context.source_handles, section_plan=[
            ReportSectionPlan(section_id="method", heading="Method", goal="Check"),
            ReportSectionPlan(section_id="conclusion", heading="Conclusion", goal="Summarize")])
        config = ReportRuntimeConfig(document_review=True, outline_strategy="template")
        result = run_report_agent(client=Client(), context=self.context, memory=memory, config=config,
            template=load_report_template_bundle(report_mode="survey", config=config),
            gateway=ReportToolGateway(self.context, documents=self.documents))
        self.assertTrue(next(row for row in result.iterations if row.action == "document_revise").adopted)
        self.assertIn("report-document-verifier", seen)


if __name__ == "__main__":
    unittest.main()
