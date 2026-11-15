from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.integrations.llm import LLMError
from simple_ar.research.brief import evidence_pack_from_read
from simple_ar.research.contracts import DocumentRecord, TextChunk
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.research.evidence.reader import ReadRequest, read_documents
from simple_ar.research import SynthesisRequest as PublicSynthesisRequest
from simple_ar.research.synthesis import (
    SynthesisRequest,
    SynthesisResult,
    _bounded_pack_json,
    run_synthesis_capability,
    synthesize_evidence,
)


def _pack() -> dict[str, object]:
    return {
        "schema_version": "evidence_pack.v1",
        "topic": "reliable coding agents",
        "coverage": {"status": "covered", "covered_facets": ["method"]},
        "counts": {"documents": 1, "chunks": 2},
        "papers": [{"id": "paper-1", "title": "Reliable agents"}],
        "paper_cards": [
            {
                "paper_id": "paper-1",
                "title": "Reliable agents",
                "method_summary": "A method improves validation.",
                "evidence_refs": ["paper-1#chunk-1"],
            }
        ],
        "claim_cards": [
            {
                "claim_id": "claim-1",
                "paper_id": "paper-1",
                "claim": "The method improves validation.",
                "evidence_refs": ["paper-1#chunk-1"],
            }
        ],
        "method_cards": [
            {
                "method_id": "method-1",
                "paper_id": "paper-1",
                "name": "validation method",
                "components": ["checker"],
                "evidence_refs": ["paper-1#chunk-1"],
            }
        ],
        "dataset_cards": [
            {
                "dataset_id": "dataset-1",
                "name": "agent benchmark",
                "metrics": ["success"],
                "evidence_refs": ["paper-1#chunk-2"],
            }
        ],
        "limitations": [],
    }


class SynthesisCapabilityTests(unittest.TestCase):
    def test_evidence_review_has_no_innovation_or_experiment_contract(self):
        class Client:
            def ask_json(self, system, user, *, label=""):
                self.system, self.prompt = system, user
                return {"synthesis_markdown": "The source reports validation, with a stated limitation."}
        client = Client()
        for use_llm in (False, True):
            result = synthesize_evidence(SynthesisRequest(evidence_pack=_pack(), purpose="evidence_review",
                use_llm=use_llm, llm_client=client if use_llm else None))
            self.assertEqual(result.status, "ready")
            self.assertFalse(result.ideas)
            self.assertFalse(result.novelty_checks)
            self.assertIsNone(result.experiment_contract)
            self.assertEqual(result.hypothesis_markdown, "")
            self.assertTrue(result.synthesis_markdown)
            self.assertNotIn("Runnable code links", result.gap_summary)
            self.assertNotIn("Dataset and metric evidence is sparse", result.gap_summary)
        self.assertIn("actual reported empirical evidence", client.prompt)
        research = synthesize_evidence(SynthesisRequest(evidence_pack=_pack()))
        self.assertIn("Runnable code links", research.gap_summary)

    def test_compact_handoff_keeps_constraints_and_reading_caveats(self):
        import json
        from simple_ar.research.synthesis import _evidence_notes_markdown
        pack = _pack()
        constraint = "Context. " * 1100 + "Do not change the held-out evaluation split."
        pack["execution_context"] = constraint
        pack["limitations"] = ["Only abstracts were obtained."]
        pack["paper_notes"] = [{"paper_id": "paper-1", "method": "Method detail. " * 40,
            "datasets": ["Reported subset A"], "metrics": ["Paired accuracy"],
            "key_claims": ["Coverage under exchangeability"],
            "limitations": ["Does not guarantee conditional coverage."],
            "open_questions": ["No evidence under distribution shift."], "confidence": "low",
            "evidence_refs": ["paper-1#chunk-1"]}]
        payload = json.loads(_bounded_pack_json(pack))
        self.assertEqual(payload["execution_context"], constraint)
        self.assertEqual(payload["limitations"], pack["limitations"])
        self.assertEqual(payload["paper_notes"][0]["datasets"], ["Reported subset A"])
        self.assertEqual(payload["paper_notes"][0]["metrics"], ["Paired accuracy"])
        notes = _evidence_notes_markdown(pack)
        for text in ("Do not change the held-out evaluation split.", "Does not guarantee conditional coverage.",
                     "No evidence under distribution shift.", "confidence: low", "evidence_refs",
                     "Reported subset A", "Paired accuracy"):
            self.assertIn(text, notes)
        class Client:
            def ask_json(self, system, user, *, label=""):
                self.prompt = user
                return {"synthesis_markdown": "A scoped observation."}
        client = Client()
        synthesize_evidence(SynthesisRequest(evidence_pack=pack, purpose="evidence_review",
                                            use_llm=True, llm_client=client))
        self.assertEqual(client.prompt.count(constraint), 1)
        self.assertEqual(client.prompt.count("Reported subset A"), 1)
        self.assertNotIn("# Evidence Notes", client.prompt)

    def test_bounded_handoff_exposes_omitted_card_rows(self):
        import json
        from simple_ar.research.synthesis import _evidence_notes_markdown
        pack = _pack()
        pack["paper_cards"] = pack["paper_cards"] * 25
        payload = json.loads(_bounded_pack_json(pack))
        self.assertEqual(payload["context_selection"]["paper_cards"], {"included": 24, "available": 25})
        self.assertIn("1 paper_cards rows omitted", _evidence_notes_markdown(pack))

    def test_synthesis_uses_adopted_note_not_superseded_lookup_history(self):
        import copy
        from simple_ar.research.synthesis import _evidence_notes_markdown

        for revised in (False, True):
            pack = _pack()
            followup = {"revision_performed": revised, "pending_queries": ["Unresolved condition?"],
                "scope": "retained_text_only_no_absence_or_support_certification",
                "lookups": [{"query": "Limited comparison", "status": "matches", "omitted_window_count": 2}],
                "passages": [{"chunk_id": "paper-1#chunk-1", "text": "The comparison concerns subgroup A."}],
                "prior_note": {"key_claims": ["SUPERSEDED: every subgroup was compared."],
                               "reading_followup": {"prior_note": {"method": "old " * 1000}}}}
            pack["paper_notes"] = [{"paper_id": "paper-1", "key_claims": ["Only subgroup A was compared."],
                "claim_scopes": [{"claim_id": "paper-1#note-claim-006", "object": "subgroup A",
                                  "conditions": ["Under the recorded budget"],
                                  "evidence_refs": ["paper-1#chunk-1"]}],
                "limitations": ["No supported conclusion for subgroup B."], "reading_followup": followup}]
            before = copy.deepcopy(pack)
            notes = _evidence_notes_markdown(pack)
            for retained in ("Only subgroup A", "Under the recorded budget", "subgroup B", "Unresolved condition?",
                             "omitted_window_count", "The comparison concerns subgroup A."):
                self.assertIn(retained, notes)
            self.assertNotIn("SUPERSEDED", notes)
            self.assertNotIn("prior_note", notes)
            payload = _bounded_pack_json(pack)
            self.assertIn("The comparison concerns subgroup A.", payload)
            self.assertNotIn("SUPERSEDED", payload)
            self.assertNotIn("prior_note", payload)
            self.assertNotIn("paper-1#note-claim-006", payload)
            self.assertIn("paper-1#chunk-1", payload)
            from simple_ar.research.evidence.reader import reading_followup_context
            self.assertNotIn("passages", reading_followup_context(followup))
            self.assertEqual(reading_followup_context(followup, include_passages=True)["passages"], followup["passages"])
            self.assertEqual(pack, before)
        self.assertEqual(reading_followup_context(None), {})
        self.assertEqual(reading_followup_context({"prior_note": {"method": "old"}}), {})

    def test_correction_is_bounded_and_retains_rejected_output(self):
        for corrected in (True, False):
            with self.subTest(corrected=corrected), tempfile.TemporaryDirectory() as tmp:
                calls = []

                class Client:
                    def ask_json(self, system, user, *, label=""):
                        calls.append((label, user))
                        ref = "paper-1#chunk-1" if corrected and len(calls) == 2 else "invented-paper"
                        return {"synthesis_markdown": "Validation evidence.", "hypothesis_markdown": "Test validation.",
                                "idea_candidates": [{"idea_id": "idea-1", "title": "Validation",
                                    "hypothesis": "Validation improves reliability.", "proposed_change": "Add a checker.",
                                    "motivation_refs": [ref]}]}

                context = CapabilityContext(store=ArtifactStore(Path(tmp)), attempt=AttemptManifest("synthesize"))
                result = run_synthesis_capability(context=context,
                    request=SynthesisRequest(evidence_pack=_pack(), use_llm=True, llm_client=Client()))
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1][0], "research-synthesis-correction")
                self.assertIn("unknown motivation refs", calls[1][1])
                self.assertIn("allowed_motivation_refs", calls[1][1])
                self.assertEqual(context.store.read_json("response-1.json")["idea_candidates"][0]["motivation_refs"], ["invented-paper"])
                self.assertIn("unknown motivation refs", context.store.read_json("validation-1.json")["error"])
                self.assertTrue(any(ref.path == "response-1.json" for ref in result.artifacts))
                if corrected:
                    self.assertNotEqual(result.status, "failed")
                    self.assertEqual(context.store.read_json("synthesis_result.json")["ideas"][0]["motivation_refs"], ["paper-1#chunk-1"])
                else:
                    self.assertEqual(result.status, "failed")
                    self.assertFalse(context.store.exists("synthesis_result.json"))
                    self.assertTrue(context.store.exists("validation-2.json"))

    def test_evidence_pack_keeps_bounded_source_snippets_and_search_coverage(self) -> None:
        bundle = DocumentBundle(
            records=[
                DocumentRecord(
                    document_id="paper-1",
                    title="Reliable agents",
                    source="fixture",
                    abstract="Validation improves reliability.",
                )
            ],
            fulltext_manifest={},
            fulltext_extraction={},
            sections=[],
            chunks=[
                TextChunk(
                    chunk_id="paper-1#chunk-1",
                    document_id="paper-1",
                    text="Validation improves reliability under the benchmark.",
                    source_path="paper.md",
                    line_start=4,
                    line_end=4,
                )
            ],
        )
        read = read_documents(ReadRequest(bundle=bundle))

        pack = evidence_pack_from_read(
            "reliable agents",
            read,
            coverage={"status": "covered", "covered_facets": ["method"]},
            source_plan={"sources": ["fixture"]},
            execution_context="Use the prepared fixture benchmark and do not download data.",
        )

        self.assertEqual(pack["coverage"]["status"], "covered")
        self.assertEqual(pack["source_plan"]["sources"], ["fixture"])
        self.assertEqual(pack["evidence_refs"], ["paper-1#chunk-1"])
        self.assertIn("paper-1#chunk-1", pack["evidence_snippets"])
        self.assertIn("paper.md:4", pack["evidence_snippets"] if isinstance(pack["evidence_snippets"], str) else "")
        self.assertIn("prepared fixture benchmark", pack["execution_context"])

    def test_capability_can_add_explicit_llm_synthesis(self) -> None:
        class FakeClient:
            model = "fake-synthesis-model"

            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                self.label = label
                return {
                    "synthesis_markdown": "## Themes\n\nThe evidence describes validation-oriented agents [paper-1].",
                    "hypothesis_markdown": "## Hypothesis\n\nAdding validation should improve accuracy under the fixture metric.",
                }

        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(
                    attempt_id="attempt-001",
                    capability="synthesis",
                ),
            )
            result = run_synthesis_capability(
                context=context,
                request=SynthesisRequest(
                    evidence_pack=_pack(),
                    use_llm=True,
                    llm_client=FakeClient(),
                ),
            )
            payload = context.store.read_json(result.artifacts[0])

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.provenance["mode"], "llm")
        self.assertEqual(payload["generation_mode"], "llm")
        self.assertGreater(len(payload["synthesis_markdown"]), 0)
        self.assertIn("Themes", payload["synthesis_markdown"])
        self.assertIn("Hypothesis", payload["hypothesis_markdown"])

    def test_llm_synthesis_receives_prepared_experiment_boundary(self) -> None:
        class FakeClient:
            def ask_json(self, _system: str, user: str, *, label: str = "") -> dict[str, object]:
                self.user = user
                return {
                    "synthesis_markdown": "The evidence supports a bounded fixture experiment.",
                    "hypothesis_markdown": "The prepared benchmark remains the evaluation authority.",
                }

        client = FakeClient()
        context_text = (
            "Dataset: sklearn digits. Benchmark: python benchmark.py. "
            "Do not substitute another task."
        )
        result = synthesize_evidence(
            SynthesisRequest(
                evidence_pack={
                    **_pack(),
                    "execution_context": context_text,
                },
                use_llm=True,
                llm_client=client,
            )
        )

        self.assertEqual(result.execution_context, context_text)
        self.assertIn("sklearn digits", client.user)
        self.assertIn("do not substitute a dataset or task", client.user.lower())
        synthesize_evidence(SynthesisRequest(
            evidence_pack={**_pack(), "execution_context": "Find public code for continual learning on one 3090."},
            use_llm=True, llm_client=client,
        ))
        self.assertIn("Find public code", client.user)
        self.assertIn("not evidence that code, data or an environment is ready", client.user)
        self.assertNotIn("## Prepared Experiment Boundary (hard)", client.user)

    def test_llm_synthesis_context_exposes_closed_evidence_allowlist(self) -> None:
        import json
        pack = _pack()
        pack["method_cards"][0]["method_id"] = "external-source-id#method-001"
        context = _bounded_pack_json(pack)

        self.assertIn('"allowed_motivation_refs"', context)
        self.assertIn("paper-1#chunk-1", context)
        self.assertNotIn("external-source-id#method-001", context)
        self.assertNotIn("claim-1", json.loads(context)["allowed_motivation_refs"])
        self.assertEqual(pack["method_cards"][0]["method_id"], "external-source-id#method-001")

    def test_llm_accepts_actual_source_chunk_not_repeated_on_a_card(self) -> None:
        class FakeClient:
            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                self.user = user
                return {
                    "synthesis_markdown": "The source motivates a bounded experiment.",
                    "hypothesis_markdown": "Test a bounded change.",
                    "idea_candidates": [{
                        "idea_id": "source-chunk-idea",
                        "title": "Test a bounded change",
                        "hypothesis": "The change improves validation.",
                        "proposed_change": "Change the implementation.",
                        "motivation_refs": ["paper-1#chunk-12"],
                    }],
                }

        pack = {**_pack(), "evidence_refs": ["paper-1#chunk-12"]}
        client = FakeClient()
        result = synthesize_evidence(SynthesisRequest(
            evidence_pack=pack, use_llm=True, llm_client=client,
            user_requests=("Compare the assumptions.", "Also describe limitations."),
        ))
        self.assertEqual(result.ideas[0].motivation_refs, ["paper-1#chunk-12"])
        self.assertIn("paper-1#chunk-12", client.user)
        self.assertIn("Also describe limitations.", client.user)

    def test_llm_can_replace_rule_ideas_with_grounded_candidates(self) -> None:
        class FakeClient:
            model = "fake-synthesis-model"

            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                self.user = user
                return {
                    "synthesis_markdown": "## Themes\n\nValidation is the main theme.",
                    "hypothesis_markdown": "## Hypothesis\n\nA focused checker should improve success.",
                    "idea_candidates": [
                        {
                            "idea_id": "idea-llm-001",
                            "title": "Add a focused validation checker",
                            "hypothesis": "A focused checker improves the success metric.",
                            "motivation_refs": ["paper-1#chunk-1"],
                            "proposed_change": "Add the checker to the validation path.",
                            "expected_outcome": "The success metric increases.",
                            "required_baselines": ["existing baseline"],
                            "required_datasets": ["agent benchmark"],
                            "metrics": ["success"],
                            "feasibility": "high",
                            "risks": "The local fixture may be too small.",
                        }
                    ],
                }

        client = FakeClient()
        result = synthesize_evidence(
            SynthesisRequest(
                evidence_pack=_pack(),
                use_llm=True,
                llm_client=client,
            )
        )

        self.assertEqual(result.ideas[0].idea_id, "idea-llm-001")
        self.assertEqual(result.ideas[0].risks, ["The local fixture may be too small."])
        self.assertEqual(result.experiment_contract.hypothesis, result.ideas[0].hypothesis)
        self.assertEqual(result.novelty_checks[0].idea_id, "idea-llm-001")
        self.assertIn("idea_candidates", client.user)

    def test_llm_candidates_reject_unknown_evidence_references(self) -> None:
        class FakeClient:
            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                return {
                    "synthesis_markdown": "Evidence summary.",
                    "hypothesis_markdown": "Testable hypothesis.",
                    "idea_candidates": [
                        {
                            "idea_id": "idea-llm-001",
                            "title": "Unverifiable idea",
                            "hypothesis": "It improves success.",
                            "motivation_refs": ["invented-paper"],
                            "proposed_change": "Change the implementation.",
                        }
                    ],
                }

        with self.assertRaisesRegex(LLMError, "unknown motivation refs"):
            synthesize_evidence(
                SynthesisRequest(
                    evidence_pack=_pack(),
                    use_llm=True,
                    llm_client=FakeClient(),
                )
            )

    def test_llm_candidates_recover_unique_shortened_evidence_reference(self) -> None:
        class FakeClient:
            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                return {
                    "synthesis_markdown": "Evidence summary.",
                    "hypothesis_markdown": "Testable hypothesis.",
                    "idea_candidates": [
                        {
                            "idea_id": "idea-llm-001",
                            "title": "Use the grounded evidence",
                            "hypothesis": "The bounded change improves success.",
                            "motivation_refs": ["paper-1#chunk-1"],
                            "proposed_change": "Change the implementation.",
                        }
                    ],
                }

        pack = _pack()
        pack["paper_cards"] = [
            {
                "paper_id": "paper-1",
                "title": "Reliable agents",
                "method_summary": "A method improves validation.",
                "evidence_refs": ["bundle-paper-1#chunk-1"],
            }
        ]
        pack["claim_cards"] = []
        pack["method_cards"] = []
        pack["dataset_cards"] = []
        result = synthesize_evidence(
            SynthesisRequest(
                evidence_pack=pack,
                use_llm=True,
                llm_client=FakeClient(),
            )
        )

        self.assertEqual(
            result.ideas[0].motivation_refs,
            ["bundle-paper-1#chunk-1"],
        )

    def test_research_package_keeps_capability_exports_lazy_but_compatible(self) -> None:
        self.assertIs(PublicSynthesisRequest, SynthesisRequest)

    def test_synthesis_returns_existing_structured_handoffs(self) -> None:
        result = synthesize_evidence(
            SynthesisRequest(evidence_pack=_pack(), idea_limit=2)
        )

        self.assertEqual(result.status, "ready")
        self.assertTrue(result.ideas)
        self.assertTrue(result.ideas[0].motivation_refs)
        self.assertEqual(len(result.novelty_checks), len(result.ideas))
        self.assertIsNotNone(result.experiment_contract)
        self.assertEqual(result.to_dict()["schema_version"], "synthesis_result.v1")

    def test_synthesis_can_stop_before_experiment_contract(self) -> None:
        result = synthesize_evidence(
            SynthesisRequest(
                evidence_pack=_pack(),
                include_experiment_contract=False,
            )
        )

        self.assertIsNone(result.experiment_contract)
        self.assertTrue(result.gap_summary.startswith("# Gap Summary"))

    def test_synthesis_handoff_round_trips_without_network_or_llm(self) -> None:
        result = synthesize_evidence(
            SynthesisRequest(evidence_pack=_pack(), idea_limit=2)
        )

        restored = SynthesisResult.from_handoff_dict(result.to_handoff_dict())

        self.assertEqual(restored.status, result.status)
        self.assertEqual(restored.gap_summary, result.gap_summary)
        self.assertEqual(restored.ideas[0].idea_id, result.ideas[0].idea_id)
        self.assertEqual(
            restored.novelty_checks[0].idea_id,
            result.novelty_checks[0].idea_id,
        )
        self.assertEqual(
            restored.experiment_contract.contract_id
            if restored.experiment_contract
            else None,
            "experiment-contract-001",
        )

    def test_synthesis_handoff_rejects_unknown_schema_or_status(self) -> None:
        with self.assertRaises(ValueError):
            SynthesisResult.from_handoff_dict({"schema_version": "other"})
        with self.assertRaises(ValueError):
            SynthesisResult.from_handoff_dict(
                {"schema_version": "synthesis_result.v1", "status": "failed"}
            )

    def test_sparse_pack_is_reviewable_not_silent_success(self) -> None:
        result = synthesize_evidence(
            SynthesisRequest(
                evidence_pack={"topic": "unknown", "counts": {"documents": 0, "chunks": 0}},
            )
        )

        self.assertEqual(result.status, "needs_review")
        self.assertTrue(result.diagnostics)
        self.assertTrue(result.ideas)

    def test_llm_mode_rejects_empty_evidence_instead_of_falling_back(self) -> None:
        class FakeClient:
            def ask_json(self, system: str, user: str, *, label: str = "") -> dict[str, object]:
                raise AssertionError("The model must not be called without evidence.")

        with self.assertRaises(LLMError):
            synthesize_evidence(
                SynthesisRequest(
                    evidence_pack={"topic": "unknown", "counts": {"documents": 0, "chunks": 0}},
                    use_llm=True,
                    llm_client=FakeClient(),
                )
            )

    def test_request_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            SynthesisRequest(evidence_pack={}, idea_limit=0)
        with self.assertRaises(ValueError):
            SynthesisRequest(evidence_pack={}, novelty_backend=" ")

    def test_synthesis_capability_persists_complete_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(
                    attempt_id="attempt-001",
                    capability="synthesis",
                ),
            )
            result = run_synthesis_capability(
                context=context,
                request=SynthesisRequest(evidence_pack=_pack()),
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.artifacts[0].path, "synthesis_result.json")
            payload = context.store.read_json(result.artifacts[0])
            self.assertEqual(payload["schema_version"], "synthesis_result.v1")
            self.assertEqual(payload["ideas"][0]["idea_id"], "idea-001")
            self.assertEqual(
                payload["experiment_contract"]["contract_id"],
                "experiment-contract-001",
            )


if __name__ == "__main__":
    unittest.main()
