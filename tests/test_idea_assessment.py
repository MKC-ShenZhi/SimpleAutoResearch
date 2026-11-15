from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from simple_ar.core import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.research.assessment import (
    IdeaAssessmentRequest,
    assess_ideas,
    run_idea_assessment_capability,
)
from simple_ar.research.contracts import ClaimCard, IdeaCandidate, NoveltyCheck, TextChunk
from simple_ar.integrations.llm import LLMError
from simple_ar.app.research_application import _idea_assessment_execution_boundary


class IdeaAssessmentTests(unittest.TestCase):
    def test_assessment_receives_bounded_active_config_and_edit_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "exp" / "active.toml"
            config.parent.mkdir()
            config.write_text("[model]\nshare_training_batches = false\n", encoding="utf-8")
            execution = {
                "code_task": {"code_root": str(root), "allowed_patterns": ["model.py"],
                              "protected_patterns": ["exp/**"]},
                "protocol": {"comparison_conditions": {"source_config": "exp/active.toml"}},
            }
            boundary = _idea_assessment_execution_boundary(execution)
            self.assertEqual(boundary["active_source_config"]["status"], "observed")
            self.assertIn("share_training_batches = false",
                          boundary["active_source_config"]["excerpts"][0]["text"])
            self.assertEqual(boundary["edit_scope"]["protected_patterns"], ["exp/**"])

            candidate = IdeaCandidate(idea_id="a", title="Candidate", hypothesis="Effect",
                                      proposed_change="Share batches", expected_outcome="Improvement",
                                      motivation_refs=["c"], metrics=["accuracy"])
            class Client:
                def ask_json(self, system, user, **kwargs):
                    self.system = system
                    self.payload = json.loads(user)
                    return {"assessments": [{"idea_id": "a", "relevance": "Relevant",
                             "differentiation": "Unknown", "feasibility": "Needs design",
                             "cost": "One run", "falsifiability": "No gain", "recommendation": "Inspect",
                             "supporting_evidence_refs": ["c"], "counter_evidence_refs": [],
                             "unknowns": []}], "recommended_idea_id": "a",
                            "recommendation_reason": "Further design audit is required."}
            client = Client()
            assess_ideas(IdeaAssessmentRequest(
                candidates=(candidate,), available_evidence_refs=("c",),
                evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
                constraints={"execution_boundary": boundary}, llm_client=client,
            ))
            self.assertEqual(client.payload["constraints"]["execution_boundary"], boundary)
            self.assertIn("active configuration", client.system)

    def test_assessment_does_not_read_source_config_outside_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (Path(tmp) / "private.toml").write_text("[private]\ntoken = 'do-not-read'\n", encoding="utf-8")
            boundary = _idea_assessment_execution_boundary({
                "code_task": {"code_root": str(root)},
                "protocol": {"comparison_conditions": {"source_config": "../private.toml"}},
            })
            self.assertEqual(boundary["active_source_config"]["status"], "unavailable")
            self.assertNotIn("excerpts", boundary["active_source_config"])

    def test_comparison_accepts_source_backed_card_but_not_unresolved_card(self):
        candidate = IdeaCandidate(idea_id="a", title="Candidate", hypothesis="Effect",
                                  proposed_change="Change training", expected_outcome="Improvement",
                                  motivation_refs=["claim-a"], metrics=["accuracy"])
        class Client:
            def ask_json(self, system, user, **kwargs):
                self.payload = json.loads(user)
                self.call_kwargs = kwargs
                return dict(assessments=[dict(
                    idea_id="a", relevance="Relevant", differentiation="Unknown",
                    feasibility="Small", cost="One run", falsifiability="No gain",
                    recommendation="Validate", supporting_evidence_refs=["claim-a"],
                    counter_evidence_refs=[], unknowns=[])], recommended_idea_id="a",
                    recommendation_reason="Bounded validation")
        client = Client()
        from dataclasses import replace
        request = IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("claim-a", "chunk-a"),
            evidence_chunks=(TextChunk(chunk_id="chunk-a", document_id="p", text="Source"),),
            evidence_cards=(ClaimCard(claim_id="claim-a", paper_id="p", claim="Scoped finding",
                                      evidence_refs=["chunk-a"]),), llm_client=client,
            user_requests=("Compare methods under a small compute budget.", "Include the failure cases."))
        result = assess_ideas(request)
        self.assertEqual(result.recommended_idea_id, "a")
        self.assertEqual(client.payload["user_requests"], list(request.user_requests))
        self.assertNotIn("max_output_tokens", client.call_kwargs)
        self.assertIn("Scoped finding", client.payload["evidence"][-1]["text"])
        self.assertEqual(result.model_context[-1]["source_chunk_ids"], ["chunk-a"])
        invalid = assess_ideas(replace(request, evidence_cards=(
            replace(request.evidence_cards[0], evidence_refs=["missing"]),)))
        self.assertIsNone(invalid.recommended_idea_id)
        self.assertIn("no research candidate was recommended", invalid.recommendation_reason)
        self.assertIn("outside the supplied context", invalid.diagnostics[-1])

    def test_model_comparison_uses_shared_sources_without_upgrading_readiness(self):
        candidate = IdeaCandidate(
            idea_id="a", title="Candidate", hypothesis="A testable effect",
            proposed_change="Change training", expected_outcome="Less error",
            motivation_refs=["a1", "a29"],  # No metric: model cannot approve readiness.
        )
        chunks = tuple(TextChunk(chunk_id=f"a{i}", document_id="paper-a", text="First paper.") for i in range(30))
        chunks += (TextChunk(chunk_id="b1", document_id="paper-b", text="Counter evidence."),)
        row = dict(
            idea_id="a", relevance="Within scope", differentiation="Not established",
            feasibility="Needs a controlled run", cost="One paired run",
            falsifiability="No gain on held-out data", recommendation="Define metric first",
            supporting_evidence_refs=["a1"], counter_evidence_refs=["b1"], unknowns=["Unknown effect"],
        )

        class Client:
            def ask_json(self, system, user, **kwargs):
                self.payload = json.loads(user)
                return dict(assessments=[row], recommended_idea_id="a", recommendation_reason="Smallest informative experiment")

        client = Client()
        request = IdeaAssessmentRequest(candidates=(candidate,), available_evidence_refs=("a1", "a29", "b1"), evidence_chunks=chunks, llm_client=client)
        result = assess_ideas(request)
        self.assertEqual(result.generation_mode, "llm")
        self.assertEqual(result.assessments[0].status, "needs_evidence")
        self.assertEqual(result.assessments[0].counter_evidence_refs, ("b1",))
        self.assertEqual(result.recommended_idea_id, "a")
        self.assertIn("b1", [chunk["chunk_id"] for chunk in client.payload["evidence"]])
        self.assertIn("a29", [chunk["chunk_id"] for chunk in client.payload["evidence"]])
        self.assertEqual(list(result.model_context), client.payload["evidence"])

        # An omitted chunk remains in storage but is not model-visible evidence.
        visible = {entry["chunk_id"] for entry in client.payload["evidence"]}
        omitted = next(chunk.chunk_id for chunk in chunks if chunk.chunk_id not in visible)
        row["supporting_evidence_refs"] = [omitted]
        invalid = assess_ideas(request)
        self.assertEqual(invalid.generation_mode, "deterministic_fallback")
        self.assertIsNone(invalid.recommended_idea_id)
        self.assertIn("outside the supplied context", invalid.diagnostics[-1])

    def test_model_unavailable_retains_honest_partial_assessment(self):
        class Client:
            def ask_json(self, *args, **kwargs):
                raise LLMError("provider timeout")

        candidate = IdeaCandidate(idea_id="a", title="Candidate", hypothesis="Effect",
                                  proposed_change="Change", expected_outcome="Improvement",
                                  motivation_refs=["c"], metrics=["accuracy"])
        result = assess_ideas(IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("c",),
            evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
            llm_client=Client(),
        ))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.assessments[0].source_kind, "deterministic_readiness")
        self.assertIsNone(result.recommended_idea_id)
        self.assertIn("no research candidate was recommended", result.recommendation_reason)
        self.assertIn("provider timeout", result.diagnostics[-1])

    def test_model_comparison_repairs_one_structural_response(self):
        class Client:
            calls = 0

            def ask_json(self, _system, _user, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    return {"idea_id": "a", "relevance": "Relevant"}
                self.asserted_label = kwargs["label"]
                return {"assessments": [{
                    "idea_id": "a", "relevance": "Relevant", "differentiation": "Uncertain",
                    "feasibility": "Small", "cost": "One run", "falsifiability": "No gain",
                    "recommendation": "Check effect", "supporting_evidence_refs": ["c"],
                    "counter_evidence_refs": [], "unknowns": [],
                }], "recommended_idea_id": "a", "recommendation_reason": "Bounded test"}

        client = Client()
        candidate = IdeaCandidate(
            idea_id="a", title="Candidate", hypothesis="Effect", proposed_change="Change",
            expected_outcome="Improvement", motivation_refs=["c"], metrics=["accuracy"],
        )
        result = assess_ideas(IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("c",),
            evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
            llm_client=client,
        ))
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.asserted_label, "research-idea-assessment-correction")
        self.assertEqual(result.generation_mode, "llm")
        self.assertEqual(result.recommended_idea_id, "a")

    def test_pre_experiment_abstention_gets_one_model_review_without_forced_selection(self):
        candidate = IdeaCandidate(
            idea_id="a", title="Candidate", hypothesis="Effect",
            proposed_change="Change", expected_outcome="Improvement",
            motivation_refs=["c"], metrics=["accuracy"],
        )
        row = dict(
            idea_id="a", relevance="Relevant", differentiation="Uncertain",
            feasibility="Bounded", cost="One paired run", falsifiability="No gain",
            recommendation="Test the hypothesis", supporting_evidence_refs=["c"],
            counter_evidence_refs=[], unknowns=["Effect unmeasured"],
        )

        class Client:
            calls = 0

            def ask_json(self, system, user, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.asserted_system = system
                    return dict(assessments=[row], recommended_idea_id=None,
                                recommendation_reason="Baseline and candidate have not been measured.")
                self.asserted_label = kwargs["label"]
                self.asserted_review = user
                return dict(assessments=[row], recommended_idea_id="a",
                            recommendation_reason="Prior evidence warrants one bounded test.")

        client = Client()
        result = assess_ideas(IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("c",),
            evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
            llm_client=client,
        ))
        self.assertEqual(client.calls, 2)
        self.assertEqual(client.asserted_label, "research-idea-assessment-selection-review")
        self.assertIn("pre-experiment choice", client.asserted_system)
        self.assertIn("outcomes to obtain", client.asserted_review)
        self.assertEqual(result.recommended_idea_id, "a")
        self.assertIsNone(result.model_initial_response["recommended_idea_id"])
        self.assertEqual(result.model_response["recommended_idea_id"], "a")

        class PersistentAbstention(Client):
            def ask_json(self, system, user, **kwargs):
                self.calls += 1
                return dict(assessments=[row], recommended_idea_id=None,
                            recommendation_reason="The source contradicts this mechanism.")

        abstaining = PersistentAbstention()
        retained = assess_ideas(IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("c",),
            evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
            llm_client=abstaining,
        ))
        self.assertEqual(abstaining.calls, 2)
        self.assertIsNone(retained.recommended_idea_id)
        self.assertEqual(retained.generation_mode, "llm")

    def test_invalid_selection_review_keeps_original_model_abstention(self):
        candidate = IdeaCandidate(
            idea_id="a", title="Candidate", hypothesis="Effect",
            proposed_change="Change", expected_outcome="Improvement",
            motivation_refs=["c"], metrics=["accuracy"],
        )
        row = dict(
            idea_id="a", relevance="Relevant", differentiation="Uncertain",
            feasibility="Bounded", cost="One run", falsifiability="No gain",
            recommendation="Investigate", supporting_evidence_refs=["c"],
            counter_evidence_refs=[], unknowns=[],
        )

        class Client:
            calls = 0

            def ask_json(self, *_args, **_kwargs):
                self.calls += 1
                return dict(
                    assessments=[row],
                    recommended_idea_id=None if self.calls == 1 else "unknown",
                    recommendation_reason="Need a test." if self.calls == 1 else "Invalid idea.",
                )

        client = Client()
        result = assess_ideas(IdeaAssessmentRequest(
            candidates=(candidate,), available_evidence_refs=("c",),
            evidence_chunks=(TextChunk(chunk_id="c", document_id="d", text="Evidence"),),
            llm_client=client,
        ))
        self.assertEqual(client.calls, 2)
        self.assertEqual(result.generation_mode, "llm")
        self.assertIsNone(result.recommended_idea_id)
        self.assertIn("unknown or blocked", result.diagnostics[-1])

    def test_assessment_keeps_readiness_and_unknowns_explicit(self) -> None:
        strong = IdeaCandidate(
            idea_id="idea-strong",
            title="Validate a small change",
            hypothesis="Validation improves accuracy.",
            motivation_refs=["chunk-1"],
            proposed_change="Add validation.",
            expected_outcome="Accuracy improves.",
            required_baselines=["baseline"],
            required_datasets=["digits"],
            metrics=["accuracy"],
            feasibility="medium",
        )
        weak = IdeaCandidate(
            idea_id="idea-weak",
            title="A broad direction",
            hypothesis="The direction may help.",
            motivation_refs=["missing-chunk"],
            proposed_change="Explore the direction.",
            expected_outcome="The result is clearer.",
        )

        result = assess_ideas(
            IdeaAssessmentRequest(
                candidates=(strong, weak),
                novelty_checks=(
                    NoveltyCheck(
                        idea_id="idea-strong",
                        status="local_risk_hint",
                        similar_work_refs=["paper-2"],
                    ),
                ),
                available_evidence_refs=("chunk-1",),
            )
        )

        self.assertEqual(result.status, "partial")
        by_id = {item.idea_id: item for item in result.assessments}
        self.assertEqual(by_id["idea-strong"].status, "ready")
        self.assertEqual(by_id["idea-strong"].relevance, "evidence_grounded")
        self.assertEqual(by_id["idea-strong"].differentiation, "overlap_risk")
        self.assertEqual(by_id["idea-weak"].status, "needs_evidence")
        self.assertIn("No evaluation metric is specified.", by_id["idea-weak"].unknowns)
        self.assertIn("idea-weak has unresolved evidence refs", result.diagnostics[0])

    def test_duplicate_candidate_ids_are_rejected(self) -> None:
        candidate = IdeaCandidate(
            idea_id="same",
            title="Candidate",
            hypothesis="A hypothesis.",
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            assess_ideas(IdeaAssessmentRequest(candidates=(candidate, candidate)))

    def test_capability_persists_json_and_review_markdown(self) -> None:
        candidate = IdeaCandidate(
            idea_id="idea-001",
            title="Bounded validation",
            hypothesis="Validation improves accuracy.",
            motivation_refs=["chunk-1"],
            proposed_change="Add validation.",
            expected_outcome="Accuracy improves.",
            required_baselines=["baseline"],
            required_datasets=["digits"],
            metrics=["accuracy"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            context = CapabilityContext(
                store=ArtifactStore(Path(tmp)),
                attempt=AttemptManifest(
                    attempt_id="assessment-001",
                    capability="assess_ideas",
                ),
            )
            result = run_idea_assessment_capability(
                context=context,
                request=IdeaAssessmentRequest(
                    candidates=(candidate,),
                    available_evidence_refs=("chunk-1",),
                ),
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                [artifact.kind for artifact in result.artifacts],
                ["idea_assessment", "idea_comparison"],
            )
            payload = context.store.read_json(result.artifacts[0])
            self.assertEqual(payload["schema_version"], "idea_assessment.v1")
            self.assertEqual(payload["assessments"][0]["status"], "ready")
            markdown = context.store.read_text(result.artifacts[1])
            self.assertIn("execution-readiness only", markdown)
            self.assertIn("prepare_bounded_validation", markdown)


if __name__ == "__main__":
    unittest.main()
