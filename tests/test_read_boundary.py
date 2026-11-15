from __future__ import annotations

import unittest
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_ar.research.contracts import DocumentRecord, SourcePlan, TextChunk
from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.research.documents.ingest import DocumentBundle, build_document_bundle, build_local_document_bundle, merge_document_bundles
from simple_ar.research.evidence.reader import (
    ReadRequest,
    ReadResult,
    format_bundle_evidence_snippets,
    query_evidence,
    read_documents,
    new_source_queries,
    merge_read_results,
    run_read_capability,
    select_representative_chunks,
    select_reading_chunks,
)


class ReadBoundaryTests(unittest.TestCase):
    def test_retained_source_history_does_not_spend_current_acquisition_budget(self):
        from simple_ar.research.documents.ingest import document_report_rows
        def bundle(name, size):
            report = {'schema_version': 'research_fulltext_manifest.v1',
                'budget': {'max_pdf_mb': size}, 'document_count': 1,
                'cached_count': 1, 'fetch_attempt_count': 1,
                'documents': [{'document_id': name, 'hints': [{'status': 'cached'}]}]}
            return DocumentBundle(records=[DocumentRecord(document_id=name, title=name, source='local_files')],
                fulltext_manifest=report, fulltext_extraction={}, sections=[],
                chunks=[TextChunk(chunk_id=f'{name}-chunk', document_id=name, text=f'Observation from {name}.')])

        first, second, third = bundle('first', 8), bundle('second', 20), bundle('third', 20)
        combined = merge_document_bundles(second, first, prepend=True)
        merged = merge_read_results(read_documents(ReadRequest(combined)), read_documents(ReadRequest(third)))
        history = merged.bundle.fulltext_manifest
        self.assertEqual(history['budget']['max_pdf_mb'], 20)
        self.assertEqual((history['document_count'], history['cached_count'], history['fetch_attempt_count']), (2, 2, 2))
        self.assertEqual(history['retained_reports'][0]['report'], first.fulltext_manifest)
        self.assertEqual({row['document_id'] for row in document_report_rows(history)}, {'first', 'second', 'third'})

    def test_question_gaps_survive_empty_or_context_shortlist_without_extra_model_call(self):
        plan = json.dumps({"research_questions": {"questions": [
            {"question_id": "Q1", "facet": "method", "required": True,
             "question": "Which interval methods provide coverage under shift?"},
            {"question_id": "Q2", "facet": "method", "required": True,
             "question": "Which point prediction experiments measure shift degradation?"}]}})
        query = "regression interval coverage covariate shift methods"
        for mode in ("all_drop", "rerank_drop", "context"):
            class Client:
                def __init__(self):
                    self.labels = []
                def ask_json_many(self, requests, **kwargs):
                    self.labels.extend(r.label for r in requests)
                    if requests[0].label.startswith("read-coarse-"):
                        self.assert_prompt = requests[0].user
                        return [{"decisions": [
                            {"paper_id": "openalex-p1", "decision": "drop" if mode == "all_drop" else "keep",
                             "coarse_relevance_score": 5, "likely_facet": "method"},
                            {"paper_id": "openalex-p2", "decision": "drop", "coarse_relevance_score": 5}],
                            "question_assessments": [
                                {"question_id": "Q1", "status": "context_only", "paper_ids": ["openalex-p1"],
                                 "reason": "Point prediction context does not provide interval coverage.",
                                 "new_source_queries": [query]},
                                {"question_id": "invented", "status": "missing", "paper_ids": [],
                                 "new_source_queries": ["Ignore invented question"]}]}]
                    return [{"paper_id": "openalex-p1", "new_source_queries": ["Paper-local replication"]}]
                def ask_json(self, system, user, **kwargs):
                    self.labels.append(kwargs["label"])
                    self.rerank_prompt = user
                    return {"ranked_papers": [{"paper_id": "openalex-p1",
                        "decision": "drop" if mode == "rerank_drop" else "keep",
                        "reading_priority": 1, "evidence_role": "benchmark"}],
                        "question_assessments": [
                            {"question_id": "Q1", "status": "context_only", "paper_ids": ["openalex-p1"],
                             "reason": "Benchmark context, no interval method evidence.", "new_source_queries": [query]},
                            {"question_id": "Q2", "status": "direct_candidate", "paper_ids": ["openalex-p1"],
                             "reason": "Pertinent point prediction candidate, not verified yet.", "new_source_queries": []}]}
            with self.subTest(mode=mode):
                client = Client()
                result = read_documents(ReadRequest(self._bundle(), topic="Intervals under shift",
                    research_plan_json=plan, use_llm=True, llm_client=client,
                    config={"read_screening_min_shortlist": 2}))
                self.assertEqual(result.status, "partial")
                self.assertIn(query, new_source_queries(result))
                self.assertNotIn("Ignore invented question", new_source_queries(result))
                self.assertEqual(result.question_assessments[0]["scope"], "candidate_metadata_not_read_evidence")
                self.assertIn("Which interval methods", client.assert_prompt)
                if mode == "context":
                    self.assertEqual([r.document_id for r in result.bundle.records], ["openalex-p1"])
                    self.assertEqual([r["status"] for r in result.question_assessments],
                                     ["context_only", "direct_candidate"])
                    self.assertEqual(new_source_queries(result), (query, "Paper-local replication"))
                    self.assertEqual(len(client.labels), 3)  # Existing coarse + rerank + notes only.
                    self.assertIn("Whole-candidate question observations", client.rerank_prompt)
                    selected_client = Client()
                    with patch("simple_ar.research.evidence.reader.screen_papers_with_llm",
                               side_effect=AssertionError("Selection must not run twice")):
                        selected_read = read_documents(ReadRequest(result.bundle, topic="Intervals under shift",
                            research_plan_json=plan, use_llm=True, llm_client=selected_client,
                            source_selection={"decisions": list(result.screening_decisions),
                                              "question_assessments": list(result.question_assessments)}))
                    self.assertEqual(selected_read.bundle.records, result.bundle.records)
                    self.assertEqual(selected_read.question_assessments, result.question_assessments)
                    self.assertFalse(any(label.startswith("read-coarse-") or label == "read-rerank"
                                         for label in selected_client.labels))
                    self.assertEqual(selected_client.labels, [])  # No new text beyond screened metadata.
                    self.assertEqual(selected_read.paper_notes, ())
                else:
                    self.assertEqual(result.bundle.records, [])
                    self.assertEqual(result.paper_notes, ())
                    self.assertEqual(result.question_assessments[0]["status"], "missing")
                    self.assertEqual(len(client.labels), 1 if mode == "all_drop" else 2)
                saved = result.to_handoff_dict()
                restored = ReadResult.from_handoff_dict(saved, bundle=result.bundle)
                self.assertEqual(restored.question_assessments, result.question_assessments)
                self.assertEqual(new_source_queries(restored), new_source_queries(result))
                saved.pop("question_assessments")
                legacy = ReadResult.from_handoff_dict(saved, bundle=result.bundle)
                self.assertEqual(legacy.question_assessments, ())
                if mode != "context":
                    previous = read_documents(ReadRequest(self._bundle()))
                    merged = merge_read_results(previous, restored)
                    self.assertEqual(merged.bundle, previous.bundle)
                    self.assertEqual(merged.paper_cards, previous.paper_cards)
                    self.assertEqual(new_source_queries(merged), (query,))
        # No existing screening call to reuse for a genuinely empty input bundle.
        empty = replace(self._bundle(), records=[], chunks=[])
        client = Client()
        result = read_documents(ReadRequest(empty, use_llm=True, llm_client=client, research_plan_json=plan))
        self.assertEqual(client.labels, [])
        self.assertEqual(result.status, "empty")

    def test_question_assessment_combines_batches_without_mistaking_local_absence_for_global_gap(self):
        from simple_ar.research.evidence.screening import _rerank_input_papers, screen_papers_with_llm
        # Comparing metadata is not limited to twice the acquisition shortlist.
        # A lower-scored, potentially complementary source still reaches rerank.
        candidates = [{"paper_id": f"p{i:02}"} for i in range(60)]
        coarse = [{"paper_id": p["paper_id"], "coarse_relevance_score": 5 if i < 6 else 3}
                  for i, p in enumerate(candidates)]
        ranked = _rerank_input_papers(list(reversed(candidates)), coarse)
        self.assertEqual(len(ranked), 48)
        self.assertIn(candidates[6], ranked)
        self.assertEqual(ranked, _rerank_input_papers(candidates, coarse))
        class Client:
            def ask_json_many(self, requests, **kwargs):
                return [{"decisions": [{"paper_id": pid, "decision": "keep"}],
                    "question_assessments": [{"question_id": "Q", "status": status, "paper_ids": ids,
                                              "new_source_queries": queries}]}
                    for pid, status, ids, queries in (
                        ("p", "missing", [], ["batch-local gap"]),
                        ("q", "direct_candidate", ["q"], []))]
            def ask_json(self, *args, **kwargs):
                return {}  # Existing coarse fallback preserves assessment and shortlist.
        assessments = []
        decisions = screen_papers_with_llm(Client(), topic="Methods", problem_markdown="",
            research_plan_json=json.dumps({"research_questions": {"questions": [
                {"question_id": "Q", "question": "Which method?"}]}}),
            papers=[{"paper_id": "p"}, {"paper_id": "q"}],
            config={"read_screening_batch_size": 1}, question_assessments=assessments)
        self.assertEqual(len(decisions), 2)
        self.assertEqual(assessments[0]["status"], "direct_candidate")
        self.assertEqual(assessments[0]["paper_ids"], ["q"])
        self.assertEqual(assessments[0]["new_source_queries"], [])
        with patch("simple_ar.research.evidence.screening._coarse_screen",
                   side_effect=AssertionError("Bounded metadata does not need coarse calls")), patch(
                "simple_ar.research.evidence.screening._rerank",
                return_value=[{"paper_id": "q", "decision": "keep"}]) as rerank:
            direct = screen_papers_with_llm(Client(), topic="Methods", problem_markdown="",
                research_plan_json="{}", papers=[{"paper_id": "p"}, {"paper_id": "q"}],
                config={"read_screening_max_shortlist": 1}, metadata_pool=True)
        rerank.assert_called_once()
        self.assertEqual([r["paper_id"] for r in direct if r["decision"] == "keep"], ["q"])

    def _source_batches(self, root, budgets=None):
        bundles = []
        for name, budget in zip(("first", "second"), budgets or ({}, {})):
            path = root / f"{name}.md"
            path.write_text(f"# {name}\n\n## Methods\nMeasured source observations.\n"
                            "\n## Results\nObserved values and stated limitations.\n", encoding="utf-8")
            bundles.append(build_document_bundle(papers=[], source_plan=SourcePlan(
                queries=["local documents"], sources=["local_files"], local_documents=[str(path)],
                require_fulltext=True, allow_pdf_download=False, budget=budget),
                cache_dir=root / "cache", extraction_dir=root / "extraction"))
        return bundles

    def test_fresh_remaining_budget_does_not_conflict_with_original_total(self):
        with TemporaryDirectory() as directory:
            first, second = self._source_batches(Path(directory), budgets=(
                {"max_fulltext_documents": 4, "max_fulltext_fetch_attempts": 8},
                {"max_fulltext_documents": 3, "max_fulltext_fetch_attempts": 7}))
            old_budget = dict(first.fulltext_manifest["budget"])
            fresh_before = json.dumps(second.to_handoff_dict(), sort_keys=True)
            merged = merge_read_results(read_documents(ReadRequest(first)), read_documents(ReadRequest(second)))
            manifest = merged.bundle.fulltext_manifest
            self.assertEqual(manifest["budget"], old_budget)
            self.assertEqual(second.fulltext_manifest["budget"]["max_fulltext_documents"], 3)
            self.assertEqual(second.fulltext_manifest["budget"]["max_fulltext_fetch_attempts"], 7)
            self.assertEqual(manifest["cache_dir"], first.fulltext_manifest["cache_dir"])
            self.assertEqual(manifest["documents"], first.fulltext_manifest["documents"] + second.fulltext_manifest["documents"])
            self.assertIn("original total limits", " ".join(manifest["notes"]))
            self.assertEqual(json.dumps(second.to_handoff_dict(), sort_keys=True), fresh_before)
            disabled = build_document_bundle(papers=[], source_plan=SourcePlan(
                queries=["local documents"], sources=["local_files"],
                local_documents=[str(Path(directory) / "first.md")], require_fulltext=False,
                allow_pdf_download=False, budget={"max_fulltext_documents": 4, "max_fulltext_fetch_attempts": 8}),
                cache_dir=Path(directory) / "cache", extraction_dir=Path(directory) / "extraction")
            enabled = merge_read_results(read_documents(ReadRequest(disabled)), read_documents(ReadRequest(second)))
            self.assertFalse(disabled.fulltext_manifest["enabled"])
            self.assertTrue(enabled.bundle.fulltext_manifest["enabled"])
            self.assertTrue(enabled.bundle.fulltext_extraction["enabled"])
            # Policy changes remain conflicts even though remaining allowances can change.
            for policy, manifest in (
                ("allow_pdf_download", {**second.fulltext_manifest, "allow_pdf_download": True}),
                ("parser_backend", {**second.fulltext_manifest, "budget": {
                    **second.fulltext_manifest["budget"], "parser_backend": "unstructured"}}),
            ):
                bad = replace(second, fulltext_manifest=manifest)
                with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, policy):
                    merge_read_results(read_documents(ReadRequest(first)), read_documents(ReadRequest(bad)))

    def test_cumulative_real_bundles_preserve_questions_and_deduplicate(self):
        from simple_ar.research.evidence.cards import build_code_links
        document = replace(self._bundle().records[0], abstract=(
            'Code: "https://example.test/a.b?q=1&v=2"; '
            'data https://data.test/. Next https://example.test/a%2E.'))
        self.assertEqual([link.url for link in build_code_links(documents=[document], chunks=[])],
                         ["https://example.test/a.b?q=1&v=2", "https://data.test/",
                          "https://example.test/a%2E"])
        with TemporaryDirectory() as directory:
            first, second = self._source_batches(Path(directory))
            old = read_documents(ReadRequest(first))
            owner = first.records[0].document_id
            note = {"paper_id": owner, "open_questions": ["Unresolved instrument conditions"],
                    "followup_queries": ["Instrument appendix"],
                    "reading_followup": {"pending_queries": ["Instrument appendix"]},
                    "new_source_queries": ["Independent instrument validation"]}
            old = replace(old, status="partial", paper_notes=(note,), notes_markdown="Old unresolved notes",
                          screening_decisions=({"paper_id": owner, "decision": "keep"},),
                          diagnostics=("Original uncertainty remains",))
            fresh = read_documents(ReadRequest(second))
            new_owner = second.records[0].document_id
            fresh = replace(fresh, paper_notes=({"paper_id": new_owner, "open_questions": ["New question"]},),
                            screening_decisions=({"paper_id": new_owner, "decision": "keep"},),
                            notes_markdown="New reading notes")
            before = json.dumps(first.to_handoff_dict(), sort_keys=True)
            merged = merge_read_results(old, fresh)
            self.assertEqual(merged.bundle.records, first.records + second.records)
            self.assertEqual(merged.bundle.chunks, first.chunks + second.chunks)
            self.assertEqual(merged.bundle.sections, first.sections + second.sections)
            for name in ("paper_cards", "claim_cards", "method_cards", "dataset_cards", "code_links",
                         "paper_notes", "screening_decisions"):
                self.assertEqual(getattr(merged, name), getattr(old, name) + getattr(fresh, name))
            self.assertEqual(merged.paper_notes[0]["open_questions"], ["Unresolved instrument conditions"])
            self.assertEqual(merged.paper_notes[0]["reading_followup"]["pending_queries"], ["Instrument appendix"])
            self.assertIn("Original uncertainty remains", merged.diagnostics)
            self.assertIn("Old unresolved notes", merged.notes_markdown)
            self.assertEqual(merged.status, "partial")
            for name, schema in (("fulltext_manifest", "research_fulltext_manifest.v1"),
                                 ("fulltext_extraction", "fulltext_extraction.v1")):
                value = getattr(merged.bundle, name)
                self.assertEqual(value["schema_version"], schema)
                self.assertEqual(value["documents"], getattr(first, name)["documents"] + getattr(second, name)["documents"])
                self.assertEqual(value["document_count"], 2)
                self.assertEqual(sum(value["status_counts"].values()), 2)
            self.assertEqual(merged.bundle.fulltext_manifest["cached_count"], 2)
            self.assertEqual(merged.bundle.fulltext_extraction["parsed_count"], 2)
            self.assertEqual(json.dumps(first.to_handoff_dict(), sort_keys=True), before)
            # Exact replay and replay of a batch inside the cumulative bundle add nothing.
            self.assertEqual(merge_read_results(old, old), old)
            self.assertEqual(merge_read_results(merged, old), merged)
            empty = read_documents(ReadRequest(build_local_document_bundle([], extraction_dir=Path(directory) / "empty")))
            self.assertIs(merge_read_results(old, empty), old)

    def test_cumulative_identity_conflicts_are_rejected(self):
        with TemporaryDirectory() as directory:
            first, _ = self._source_batches(Path(directory))
            old = read_documents(ReadRequest(first))
            conflicts = (
                replace(old, bundle=replace(first, records=[replace(first.records[0], title="Changed source")])),
                replace(old, bundle=replace(first, chunks=[replace(first.chunks[0], text="Changed passage")])),
                replace(old, bundle=replace(first, sections=[replace(first.sections[0], text="Changed section")])),
                replace(old, paper_cards=(replace(old.paper_cards[0], title="Changed card"),)),
                replace(old, bundle=replace(first, fulltext_extraction={**first.fulltext_extraction,
                    "documents": [{**first.fulltext_extraction["documents"][0], "reason": "Changed extraction"}]})),
            )
            for fresh in conflicts:
                with self.subTest(fresh=fresh), self.assertRaisesRegex(ValueError, "Conflicting read evidence identity"):
                    merge_read_results(old, fresh)
            note = {"paper_id": first.records[0].document_id, "open_questions": ["Old question"]}
            decisions = ({"paper_id": "candidate", "decision": "drop", "reason": "Earlier shortlist"},
                         {"paper_id": "candidate", "decision": "keep", "reason": "New question"})
            merged = merge_read_results(replace(old, screening_decisions=(decisions[0],)),
                                        replace(old, screening_decisions=(decisions[1],)))
            self.assertEqual(merged.screening_decisions, decisions)
            self.assertEqual(merged.bundle, first)
            with self.assertRaisesRegex(ValueError, "paper_id"):
                merge_read_results(replace(old, paper_notes=(note,)),
                                   replace(old, paper_notes=({**note, "open_questions": []},)))

    def test_previous_capability_delivers_restorable_cumulative_bundle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = self._source_batches(root)
            previous = read_documents(ReadRequest(first))
            context = CapabilityContext(ArtifactStore(root / "read-attempt"), AttemptManifest("read-2"))
            outcome = run_read_capability(context=context, request=ReadRequest(second, previous=previous))
            bundle_ref = next(ref for ref in outcome.artifacts if ref.kind == "document_bundle")
            read_ref = next(ref for ref in outcome.artifacts if ref.kind == "read_result")
            bundle = DocumentBundle.from_handoff_dict(context.store.read_json(bundle_ref))
            restored = ReadResult.from_handoff_dict(context.store.read_json(read_ref), bundle=bundle)
            expected = merge_read_results(previous, read_documents(ReadRequest(second)))
            self.assertEqual(restored, expected)
            self.assertEqual(len(query_evidence(restored.bundle)), len(first.chunks) + len(second.chunks))
            legacy = previous.to_handoff_dict()  # No bundle/new-query fields required in old handoffs.
            self.assertEqual(ReadResult.from_handoff_dict(legacy, bundle=first), previous)

            # A failed batch retains completed reads without accepting a
            # partial technical failure as a successful evidence handoff.
            from simple_ar.integrations.llm import LLMClient, LLMSettings, LLMRequest, LLMError
            client = LLMClient(LLMSettings(api_key="checkpoint-secret", model="checkpoint-model"))
            binding = {"profile": "reader", "settings": {"model": "checkpoint-model", "temperature": 0.1}}
            session = ArtifactStore(root / "session")
            input_ref = session.write_text("source.txt", "Original source")
            requests = [LLMRequest("system", value, label=value) for value in ("first", "second", "third")]
            sent = []

            def respond(system, user, *, label=""):
                sent.append(user)
                if sent == ["first", "second"]:
                    raise LLMError("temporary failure")
                return {"value": user}

            def read_batch(request):
                responses = request.llm_client.ask_json_many(requests, max_workers=1)
                self.assertEqual([row["value"] for row in responses], [r.user for r in requests])
                return previous

            def context_for(number, inputs=(input_ref,)):
                return CapabilityContext(ArtifactStore(session.root / "attempts" / f"read-{number}"),
                    AttemptManifest(f"read-{number}", capability="read", trigger="application:read", inputs=inputs),
                    inputs=inputs, input_store=session)

            with patch.object(client, "connection_binding", return_value=binding), patch.object(
                    client, "ask_json", side_effect=respond), patch(
                    "simple_ar.research.evidence.reader.read_documents", side_effect=read_batch):
                with self.assertRaises(LLMError):
                    run_read_capability(context=context_for(1), request=ReadRequest(first, use_llm=True, llm_client=client))
                saved = (session.root / "attempts/read-1/read_progress.json").read_bytes()
                self.assertNotIn(b"checkpoint-secret", saved)
                self.assertFalse((session.root / "attempts/read-1/read_result.json").exists())
                binding["settings"]["request_timeout_sec"] = 1200
                run_read_capability(context=context_for(2), request=ReadRequest(first, use_llm=True, llm_client=client))
                self.assertEqual(sent, ["first", "second", "second", "third"])
                self.assertEqual((session.root / "attempts/read-1/read_progress.json").read_bytes(), saved)
                # Changed registered input and changed prompt each invalidate
                # reuse; successful ordering and source association remain.
                changed = session.write_text("other.txt", "Different source")
                run_read_capability(context=context_for(3, (changed,)), request=ReadRequest(first, use_llm=True, llm_client=client))
                self.assertEqual(sent[-3:], ["first", "second", "third"])
                requests[0] = LLMRequest("system", "changed", label="first")
                run_read_capability(context=context_for(4), request=ReadRequest(first, use_llm=True, llm_client=client))
                self.assertEqual(sent[-1], "changed")
                binding["settings"]["temperature"] = 0.8
                run_read_capability(context=context_for(5), request=ReadRequest(first, use_llm=True, llm_client=client))
                self.assertEqual(sent[-3:], ["changed", "second", "third"])

    def test_initial_selection_uses_source_positions_not_ingest_priority(self) -> None:
        chunks = [TextChunk(chunk_id=f"unrelated-id-{30-index}", document_id="p",
            source_path="paper.md", line_start=10 * index + 1, line_end=10 * index + 5,
            text="Calibration heteroscedasticity." if index == 14 else "Ordinary source prose.",
            metadata={"section": "body", "section_id": "body"}) for index in range(30)]
        body_first = chunks[10:] + chunks[:10]
        shuffled = chunks[::2] + chunks[1::2]
        for focus in ("", "Calibration heteroscedasticity"):
            expected = select_reading_chunks(chunks, max_chunks=6, focus=focus)
            for stored in (body_first, shuffled):
                before = list(stored)
                actual = select_reading_chunks(stored, max_chunks=6, focus=focus)
                self.assertEqual(actual, expected)
                self.assertEqual(stored, before)
                self.assertEqual([row.line_start for row in actual], sorted(row.line_start for row in actual))
            self.assertEqual(len(expected), 6)
        focused = select_reading_chunks(body_first, max_chunks=6, focus="Calibration heteroscedasticity")
        self.assertIn(chunks[14], focused)
        self.assertIn(chunks[13], focused)

    def test_initial_source_order_is_per_document_with_legacy_position_fallback(self) -> None:
        chunks = [TextChunk(chunk_id=f"{document}-{index}", document_id=document,
            source_path=f"{document}.md", line_start=index + 1, text="Ordinary source prose.")
            for index in range(12) for document in ("p", "q")]
        reverse_within_sources = list(reversed(chunks))
        selected = select_reading_chunks(reverse_within_sources, max_chunks=6)
        self.assertEqual(len(selected), 6)
        for document in ("p", "q"):
            original = [row for row in chunks if row.document_id == document]
            self.assertEqual([row for row in selected if row.document_id == document],
                list(select_representative_chunks(original, max_chunks=3)))
        for rows in (
            [TextChunk(chunk_id="z", document_id="p", source_path="p.md", line_start=20, text="Late."),
             TextChunk(chunk_id="a", document_id="p", source_path="p.md", text="Unlocated.")],
            [TextChunk(chunk_id="z", document_id="p", source_path="one.md", line_start=20, text="Late."),
             TextChunk(chunk_id="a", document_id="p", source_path="two.md", line_start=1, text="Other file.")],
        ):
            self.assertEqual(select_reading_chunks(rows, max_chunks=12), tuple(rows))

    def test_initial_focus_neighbors_do_not_cross_source_files(self) -> None:
        chunks = [TextChunk(chunk_id=f"c-{index}", document_id="p", source_path="one.md",
            text="Ordinary source prose.", metadata={"section_id": f"s-{index}"}) for index in range(30)]
        chunks[14] = TextChunk(chunk_id="target", document_id="p", source_path="one.md",
            text="Calibration heteroscedasticity.", metadata={"section_id": "target"})
        chunks[13] = TextChunk(chunk_id="foreign-neighbor", document_id="p", source_path="two.md",
            text="Unrelated file.", metadata={"section": "body", "section_id": "foreign"})
        with patch("simple_ar.research.evidence.reader.select_representative_chunks",
                   wraps=select_representative_chunks) as overview:
            selected = select_reading_chunks(chunks, max_chunks=6, focus="Calibration heteroscedasticity")
        self.assertEqual(overview.call_args.kwargs["required_chunk_ids"], ("target", "c-15"))
        self.assertIn(chunks[14], selected)
        self.assertIn(chunks[15], selected)

    def test_task_focus_retrieves_unsampled_passage_without_expanding_budget(self) -> None:
        chunks = [TextChunk(chunk_id=f"c-{index}", document_id="p", text=f"General discussion {index}.")
                  for index in range(80)]
        baseline = {chunk.chunk_id for chunk in select_representative_chunks(chunks, max_chunks=12)}
        target = next(index for index in range(1, 79) if f"c-{index}" not in baseline)
        chunks[target] = TextChunk(chunk_id=f"c-{target}", document_id="p",
                                   text="Calibration uncertainty depends on heteroscedastic noise.")
        selected = select_reading_chunks(chunks, max_chunks=12, focus="Explain heteroscedastic calibration uncertainty")
        self.assertEqual(len(selected), 12)
        self.assertIn(f"c-{target}", {chunk.chunk_id for chunk in selected})
        self.assertTrue(any(chunk.chunk_id in {f"c-{target-1}", f"c-{target+1}"} for chunk in selected))
        self.assertEqual(selected, select_reading_chunks(chunks, max_chunks=12, focus="Explain heteroscedastic calibration uncertainty"))

    def test_focus_no_matches_keeps_overview_and_ignores_bibliography(self) -> None:
        chunks = [TextChunk(chunk_id=f"c-{index}", document_id="p", text="Ordinary content.") for index in range(30)]
        chunks.append(TextChunk(chunk_id="ref", document_id="p", text="Heteroscedastic noise.", metadata={"section": "references"}))
        self.assertEqual(select_reading_chunks(chunks, max_chunks=6, focus="Heteroscedastic noise"),
                         select_representative_chunks(chunks, max_chunks=6))
        self.assertEqual(select_reading_chunks(chunks, max_chunks=2, focus="noise"),
                         select_representative_chunks(chunks, max_chunks=2))

    def test_chinese_focus_and_source_scoping(self) -> None:
        bundle = self._bundle(with_chunks=False)
        bundle.chunks.extend(TextChunk(chunk_id=f"c-{index}", document_id="openalex-p1", text="普通正文内容。") for index in range(40))
        bundle.chunks[7] = TextChunk(chunk_id="c-7", document_id="openalex-p1", text="校准误差与仪器噪声影响置信区间。")
        bundle.chunks.append(TextChunk(chunk_id="other", document_id="openalex-p2", text="校准误差属于另一个来源。"))
        excerpts = format_bundle_evidence_snippets(bundle, document_id="openalex-p1", focus="校准误差", max_chunks=6)
        self.assertIn("[c-7]", excerpts)
        self.assertNotIn("[other]", excerpts)
        self.assertIn("does not establish complete coverage", excerpts)

    def test_default_overview_keeps_ingest_sized_chunks_and_labels_further_clipping(self) -> None:
        bundle = self._bundle(with_chunks=False)
        bundle.chunks.append(TextChunk(chunk_id="complete", document_id="openalex-p1",
            text="x" * 1000 + " A late qualification of the main conclusion."))
        self.assertIn("late qualification", format_bundle_evidence_snippets(bundle))
        clipped = format_bundle_evidence_snippets(bundle, max_chars=900)
        self.assertNotIn("late qualification", clipped)
        self.assertIn("1 chunk excerpt(s) shortened", clipped)
        self.assertIn("not evidence of absence", clipped)
        with self.assertRaises(ValueError):
            format_bundle_evidence_snippets(bundle, max_chars=0)

    def test_bounded_reading_covers_late_sections_and_multiple_documents(self) -> None:
        bundle = self._bundle(with_chunks=False)
        bundle.chunks.extend(TextChunk(
            chunk_id=f"p1-{index}", document_id="openalex-p1", text=f"Body {index}",
            metadata={"section": "body", "section_id": "p1-body", "heading": "Main text"},
        ) for index in range(30))
        bundle.chunks.extend(TextChunk(
            chunk_id=f"p1-ref-{index}", document_id="openalex-p1", text=f"Citation {index}",
            metadata={"section": "references", "section_id": "p1-references"},
        ) for index in range(20))
        bundle.chunks.extend(TextChunk(
            chunk_id=f"p2-{index}", document_id="openalex-p2", text=f"Other {index}",
            metadata={"section": "method", "section_id": "p2-method"},
        ) for index in range(3))
        selected = select_representative_chunks(bundle.chunks, max_chunks=6)
        ids = {row.chunk_id for row in selected}
        self.assertEqual(len(selected), 6)
        self.assertTrue(any(row.document_id == "openalex-p2" for row in selected))
        self.assertIn("p1-0", ids)
        self.assertIn("p1-29", ids)
        self.assertFalse(any("ref" in row.chunk_id for row in selected))
        snippets = format_bundle_evidence_snippets(bundle, max_chunks=6)
        self.assertIn("p1-29", snippets)
        self.assertIn("6 of 53 chunks", snippets)

    def test_section_overview_preserves_method_experiment_and_result_evidence(self) -> None:
        kinds = ["abstract"] + ["body"] * 8 + ["method"] + ["body"] * 7
        kinds += ["experiments", "body", "results", "references"]
        chunks = [TextChunk(
            chunk_id=f"section-{index}", document_id="paper", text=f"Section {kind} {index}",
            metadata={"section": kind, "section_id": f"paper-{index}"},
        ) for index, kind in enumerate(kinds)]
        selected = select_representative_chunks(chunks, max_chunks=4)
        self.assertEqual({chunk.metadata["section"] for chunk in selected},
                         {"abstract", "method", "experiments", "results"})

    def test_overview_covers_each_section_boundary_before_spreading_interiors(self) -> None:
        chunks = [TextChunk(chunk_id=f"s-{section}-{index}", document_id="p", source_path="p.md",
            line_start=section * 100 + index + 1, text="Ordinary retained text.",
            metadata={"section_id": f"s-{section}", "section": "body"})
            for section in range(3) for index in range(15)]
        for cap in (3, 6, 9):
            selected = select_representative_chunks(chunks, max_chunks=cap)
            self.assertEqual(len(selected), cap)
            for section in range(3):
                indices = {int(row.chunk_id.rsplit("-", 1)[-1]) for row in selected
                           if row.metadata["section_id"] == f"s-{section}"}
                self.assertEqual(len(indices), cap // 3)
                self.assertIn(0, indices)
                if cap >= 6:
                    self.assertIn(14, indices)
                if cap == 9:
                    self.assertIn(7, indices)
            self.assertEqual(selected, select_representative_chunks(list(reversed(chunks)), max_chunks=cap))

    def test_task_pins_count_toward_section_and_document_coverage(self) -> None:
        chunks = [TextChunk(chunk_id=f"{document}-{section}-{index}", document_id=document,
            source_path=f"{document}.md", line_start=section * 100 + index + 1, text="Retained text.",
            metadata={"section_id": f"{document}-{section}", "section": "body"})
            for document in ("p", "q") for section in range(2) for index in range(8)]
        pins = ("p-0-0", "p-0-1", "p-0-2")
        selected = select_representative_chunks(chunks, max_chunks=8, required_chunk_ids=pins)
        ids = {row.chunk_id for row in selected}
        self.assertTrue(set(pins) <= ids)
        self.assertEqual(len(selected), 8)
        self.assertEqual(sum(row.document_id == "p" for row in selected), 4)
        self.assertIn("p-1-0", ids)
        for section in range(2):
            self.assertIn(f"q-{section}-0", ids)
            self.assertIn(f"q-{section}-7", ids)
        for cap in (0, 1, 2):
            bounded = select_representative_chunks(chunks, max_chunks=cap, required_chunk_ids=pins)
            self.assertEqual(len(bounded), cap)

    def test_pins_do_not_reintroduce_excluded_bibliography(self) -> None:
        chunks = [TextChunk(chunk_id="body", document_id="p", text="Substantive source."),
                  TextChunk(chunk_id="ref", document_id="p", text="Bibliography.",
                            metadata={"section": "references"})]
        self.assertEqual(select_representative_chunks(chunks, max_chunks=5,
            required_chunk_ids=("body", "ref", "unknown")), (chunks[0],))

    def test_unequal_sections_keep_cap_pins_and_comparable_source_order(self) -> None:
        for document_count in (1, 2, 3):
            chunks = [TextChunk(chunk_id=f"{document}-{section}-{index}", document_id=str(document),
                source_path=f"{document}.md", line_start=section * 100 + index + 1, text="Retained text.",
                metadata={"section_id": f"{document}-{section}"})
                for document in range(document_count) for section in range(3) for index in range(section + 2)]
            # Document discovery order is retained, especially when the budget
            # cannot cover all sources. Only reverse comparable positions within
            # each source; global document permutation is a different policy.
            rearranged = [chunk for document in range(document_count)
                          for chunk in reversed(chunks) if chunk.document_id == str(document)]
            for cap in range(len(chunks) + 2):
                for pins in ((), tuple(row.chunk_id for row in chunks[1:4])):
                    selected = select_representative_chunks(chunks, max_chunks=cap, required_chunk_ids=pins)
                    ids = {row.chunk_id for row in selected}
                    self.assertEqual(len(selected), min(cap, len(chunks)))
                    self.assertEqual(len(ids), len(selected))
                    self.assertTrue(set(pins[:cap]) <= ids)
                    self.assertEqual(selected, select_representative_chunks(rearranged,
                        max_chunks=cap, required_chunk_ids=pins))
                    if not pins:
                        counts = [sum(row.document_id == str(document) for row in selected)
                                  for document in range(document_count)]
                        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_paper_notes_receive_only_their_own_source_excerpts(self) -> None:
        class NoteClient:
            def ask_json_many(self, requests, *, max_workers):
                self.prompts = [request.user for request in requests]
                return [
                    {"paper_id": paper_id, "title": f"Paper {index}"}
                    for index, paper_id in enumerate(("openalex-p1", "openalex-p2"), start=1)
                ]

        bundle = self._bundle()
        bundle.chunks.append(TextChunk(
            chunk_id="openalex-p2#chunk-001", document_id="openalex-p2",
            text="A distinct second-paper method.",
        ))
        client = NoteClient()
        result = read_documents(ReadRequest(
            bundle=bundle, use_llm=True, llm_client=client,
            topic="Investigate measurement uncertainty",
            problem_markdown="Explain limitations relevant to my task.",
            config={"read_screening": "deterministic"},
        ))
        self.assertEqual(len(result.paper_notes), 2)
        self.assertEqual(result.paper_notes[0]["reading_coverage"]["shown_chunk_ids"], ["openalex-p1#chunk-001"])
        self.assertEqual(result.paper_notes[0]["reading_coverage"]["semantic_verification"], "not_performed")
        self.assertIn("openalex-p1#chunk-001", client.prompts[0])
        self.assertNotIn("openalex-p2#chunk-001", client.prompts[0])
        self.assertIn("openalex-p2#chunk-001", client.prompts[1])
        self.assertNotIn("openalex-p1#chunk-001", client.prompts[1])
        for prompt in client.prompts:
            self.assertIn("Investigate measurement uncertainty", prompt)
            self.assertIn("Explain limitations relevant to my task.", prompt)
            self.assertIn("user request, not source evidence", prompt)

    def test_model_cannot_relabel_a_note_as_another_paper(self) -> None:
        class WrongIdentityClient:
            def ask_json_many(self, requests, *, max_workers):
                return [{"paper_id": "another-paper"} for _ in requests]

        with self.assertRaisesRegex(ValueError, "Paper note identity mismatch"):
            read_documents(ReadRequest(bundle=self._bundle(), use_llm=True,
                llm_client=WrongIdentityClient(), config={"read_screening": "deterministic"}))

    def test_incomplete_note_batch_cannot_silently_drop_sources(self) -> None:
        class IncompleteClient:
            def ask_json_many(self, requests, *, max_workers):
                return []

        with self.assertRaisesRegex(ValueError, "incomplete response set"):
            read_documents(ReadRequest(bundle=self._bundle(), use_llm=True,
                llm_client=IncompleteClient(), config={"read_screening": "deterministic"}))

    def test_restored_notes_validate_passage_ownership_not_just_existence(self) -> None:
        bundle = self._bundle()
        bundle.chunks.append(TextChunk(chunk_id="p2-c1", document_id="openalex-p2", text="Another paper."))
        handoff = read_documents(ReadRequest(bundle=bundle)).to_handoff_dict()
        handoff["paper_notes"] = [{"paper_id": "openalex-p1", "evidence_refs": ["p2-c1", "missing"]}]
        restored = ReadResult.from_handoff_dict(handoff, bundle=bundle)
        self.assertEqual(restored.status, "partial")
        self.assertTrue(any("cross-document" in message for message in restored.diagnostics))
        self.assertEqual(restored.paper_notes[0]["evidence_refs"], ["p2-c1", "missing"])
        handoff["paper_notes"] = [{"paper_id": "openalex-p1", "evidence_refs": ["openalex-p1", "openalex-p1#chunk-001"]}]
        self.assertEqual(ReadResult.from_handoff_dict(handoff, bundle=bundle).status, "completed")
        handoff["paper_notes"] = [{"paper_id": "missing-paper", "evidence_refs": []}]
        self.assertEqual(ReadResult.from_handoff_dict(handoff, bundle=bundle).status, "partial")

    def test_document_query_filters_other_documents(self) -> None:
        bundle = self._bundle()
        bundle.chunks.append(TextChunk(chunk_id="p2-c1", document_id="openalex-p2", text="Other paper"))
        refs = query_evidence(bundle, document_id="openalex-p1", adjacent_chunks=1)
        self.assertEqual([ref.document_id for ref in refs], ["openalex-p1"])
        self.assertNotIn("Other paper", refs[0].context_text)
        handoff = read_documents(ReadRequest(bundle=bundle)).to_handoff_dict()
        self.assertEqual(handoff["source_spans"][0]["evidence_id"], refs[0].evidence_id)
        self.assertIn("extraction_status", handoff["source_spans"][0])
        self.assertNotIn("text", handoff["source_spans"][0])
        with self.assertRaisesRegex(ValueError, "does not belong"):
            query_evidence(bundle, document_id="openalex-p1", chunk_ids=("p2-c1",))

    def _bundle(self, *, with_chunks: bool = True) -> DocumentBundle:
        records = [
            DocumentRecord(
                document_id="openalex-p1",
                source_id="p1",
                title="Paper one",
                source="openalex",
                abstract="A method improves accuracy on a benchmark.",
            ),
            DocumentRecord(
                document_id="openalex-p2",
                source_id="p2",
                title="Paper two",
                source="openalex",
                abstract="A second method studies a task.",
            ),
        ]
        chunks = []
        if with_chunks:
            chunks.append(
                TextChunk(
                    chunk_id="openalex-p1#chunk-001",
                    document_id="openalex-p1",
                    text="A method improves accuracy on a benchmark.",
                )
            )
        return DocumentBundle(
            records=records,
            fulltext_manifest={},
            fulltext_extraction={},
            sections=[],
            chunks=chunks,
        )

    def test_paper_id_selection_matches_source_id(self) -> None:
        result = read_documents(
            ReadRequest(bundle=self._bundle(), paper_ids=("p1",))
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual([record.source_id for record in result.bundle.records], ["p1"])
        self.assertEqual([chunk.document_id for chunk in result.bundle.chunks], ["openalex-p1"])
        self.assertEqual(result.to_dict()["paper_card_count"], 1)

    def test_empty_selection_does_not_fall_back_to_all_documents(self) -> None:
        result = read_documents(ReadRequest(bundle=self._bundle(), paper_ids=()))

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.bundle.records, [])
        self.assertEqual(result.bundle.chunks, [])
        self.assertTrue(result.diagnostics)

    def test_model_dropping_every_paper_does_not_restore_the_input(self) -> None:
        class DropAllClient:
            model = "scripted-drop-all"

            def ask_json(self, *args, **kwargs):
                return {"ranked_papers": []}

            def ask_json_many(self, requests, *, max_workers):
                return [{"decisions": [
                    {"paper_id": paper_id, "decision": "drop",
                     "coarse_relevance_score": 0, "reason": "Outside the topic."}
                    for paper_id in ("openalex-p1", "openalex-p2")
                ]} for _ in requests]

        result = read_documents(ReadRequest(
            bundle=self._bundle(), topic="unrelated topic",
            use_llm=True, llm_client=DropAllClient(),
        ))
        self.assertEqual(result.bundle.records, [])
        self.assertEqual(result.paper_notes, ())
        self.assertTrue(result.screening_decisions)
        self.assertTrue(all(row["decision"] == "drop" for row in result.screening_decisions))

    def test_metadata_only_read_is_partial(self) -> None:
        result = read_documents(ReadRequest(bundle=self._bundle(with_chunks=False)))

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.bundle.records[0].document_id, "openalex-p1")
        self.assertEqual(result.bundle.chunks, [])

    def test_query_evidence_preserves_source_identity_and_real_adjacent_context(self) -> None:
        record = DocumentRecord(
            document_id="doc-1",
            source_id="paper-1",
            title="Paper one",
            source="fixture",
            content_hash="sha256:abc",
            extraction_status="parsed",
        )
        bundle = DocumentBundle(
            records=[record],
            fulltext_manifest={},
            fulltext_extraction={},
            sections=[],
            chunks=[
                TextChunk(
                    chunk_id="doc-1#chunk-001",
                    document_id="doc-1",
                    text="Introduction evidence.",
                    source_path="paper.md",
                    line_start=3,
                    line_end=4,
                ),
                TextChunk(
                    chunk_id="doc-1#chunk-002",
                    document_id="doc-1",
                    text="The measured result is 0.75.",
                    source_path="paper.md",
                    line_start=6,
                    line_end=7,
                ),
                TextChunk(
                    chunk_id="doc-1#chunk-003",
                    document_id="doc-1",
                    text="The limitation is a small fixture.",
                    source_path="paper.md",
                    line_start=9,
                    line_end=10,
                ),
            ],
        )

        refs = query_evidence(
            bundle,
            document_id="doc-1",
            chunk_ids=("doc-1#chunk-002",),
            adjacent_chunks=1,
        )

        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual(ref.evidence_id, "doc-1#chunk-002")
        self.assertEqual(ref.source_id, "paper-1")
        self.assertEqual(ref.document_revision, "sha256:abc")
        self.assertEqual(ref.source_path, "paper.md")
        self.assertEqual((ref.line_start, ref.line_end), (6, 7))
        self.assertEqual(ref.extraction_status, "parsed")
        self.assertEqual(ref.text, "The measured result is 0.75.")
        self.assertEqual(
            ref.adjacent_chunk_ids,
            ("doc-1#chunk-001", "doc-1#chunk-003"),
        )
        self.assertIn("Introduction evidence.", ref.context_text)
        self.assertIn("The limitation is a small fixture.", ref.context_text)

        # Supporting webpages retain independent evidence identity, not their
        # parent paper's chunk identity, and survive the existing handoff.
        from dataclasses import replace
        bundle.records[0] = replace(record, source="supporting_material",
            url="https://example.test/official",
            metadata={"kind": "supporting_material", "parent_document_id": "parent-paper"})
        restored = DocumentBundle.from_handoff_dict(bundle.to_handoff_dict())
        webpage_ref = query_evidence(restored, document_id="doc-1",
                                     chunk_ids=("doc-1#chunk-002",))[0]
        self.assertEqual(webpage_ref.document_id, "doc-1")
        self.assertEqual(restored.records[0].metadata["parent_document_id"], "parent-paper")
        self.assertEqual(webpage_ref.text, ref.text)

        with self.assertRaisesRegex(ValueError, "Unknown evidence chunk ID"):
            query_evidence(bundle, chunk_ids=("missing",))

    def test_model_read_records_screening_and_notes_in_the_handoff(self) -> None:
        class FakeClient:
            model = "fake-read-model"

            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                self.rerank_label = label
                return {
                    "ranked_papers": [
                        {
                            "paper_id": "openalex-p1",
                            "decision": "keep",
                            "reading_priority": 1,
                            "relevance_score": 5,
                            "quality_score": 4,
                            "evidence_role": "benchmark",
                            "reason": "Matches the evaluation topic.",
                            "synthesis_hint": "Use the benchmark evidence.",
                            "confidence": "medium",
                        }
                    ]
                }

            def ask_json_many(self, requests: list[object], *, max_workers: int) -> list[dict[str, object]]:
                labels = [str(getattr(request, "label", "")) for request in requests]
                if labels and all(label.startswith("read-coarse-") for label in labels):
                    return [
                        {
                            "decisions": [
                                {
                                    "paper_id": "openalex-p1",
                                    "decision": "keep",
                                    "coarse_relevance_score": 5,
                                    "likely_facet": "benchmark",
                                    "reason": "Relevant benchmark metadata.",
                                    "confidence": "medium",
                                },
                                {
                                    "paper_id": "openalex-p2",
                                    "decision": "drop",
                                    "coarse_relevance_score": 0,
                                    "likely_facet": "other",
                                    "reason": "Outside the topic.",
                                    "confidence": "medium",
                                },
                            ]
                        }
                    ]
                self.note_users = [str(getattr(request, "user", "")) for request in requests]
                return [
                    {
                        "paper_id": "openalex-p1",
                        "title": "Paper one",
                        "problem": "Study reliable agents.",
                        "method": "A validation method.",
                        "limitation": "Small fixture.",
                        "relation_to_topic": "Directly relevant.",
                        "synthesis_hint": "Use as the benchmark anchor.",
                        "confidence": "medium",
                    }
                    for _ in requests
                ]

        client = FakeClient()
        result = read_documents(
            ReadRequest(
                bundle=self._bundle(),
                topic="reliable coding agents",
                problem_markdown="# Problem\n\nStudy reliable coding agents.",
                research_plan_json='{"research_questions": []}',
                use_llm=True,
                llm_client=client,
                config={"read_screening_max_shortlist": 1},
            )
        )

        self.assertEqual([record.document_id for record in result.bundle.records], ["openalex-p1"])
        self.assertEqual(result.screening_decisions[1]["decision"], "drop")
        self.assertEqual(result.paper_notes[0]["paper_id"], "openalex-p1")
        self.assertIn("Paper one", result.notes_markdown)
        self.assertIn("A method improves accuracy on a benchmark.", client.note_users[0])
        restored = ReadResult.from_handoff_dict(
            result.to_handoff_dict(),
            bundle=result.bundle,
        )
        self.assertEqual(restored.paper_notes, result.paper_notes)
        self.assertEqual(restored.screening_decisions, result.screening_decisions)


if __name__ == "__main__":
    unittest.main()
