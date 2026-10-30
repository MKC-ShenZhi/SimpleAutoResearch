from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from simple_ar.integrations.llm import LLMError
from simple_ar.result_analysis import AnalysisContext, run_result_analysis
from simple_ar.result_analysis.service import compact_project_results_for_prompt


class FakeAnalysisClient:
    def __init__(self, payload: dict | str) -> None:
        self.payload = payload

    def ask(self, system: str, user: str, *, label: str = "") -> str:
        self.system = system
        self.user = user
        self.label = label
        if isinstance(self.payload, str):
            return self.payload
        import json

        return json.dumps(self.payload)

    def ask_json(self, system: str, user: str, *, label: str = "") -> dict:
        self.system = system
        self.user = user
        self.label = label
        if isinstance(self.payload, dict):
            return self.payload
        raise ValueError("not json")


class ResultAnalysisTests(unittest.TestCase):
    def test_failed_execution_carries_diagnosis_into_analysis_prompt(self) -> None:
        compact = compact_project_results_for_prompt({
            "execution_result": {"status": "failed", "metrics": {}},
            "failure_diagnosis": {
                "summary": "Run exited early.",
                "stderr_tail": "TypeError: unexpected argument type",
            },
        }, {})
        self.assertIn("TypeError", compact["failure_diagnosis"]["stderr_tail"])
        self.assertEqual(compact["execution_result"]["status"], "failed")

    def test_goal_judgment_is_separate_and_requires_resolvable_evidence(self):
        from simple_ar.result_analysis.service import parse_goal_assessment
        context = AnalysisContext(metrics={"accuracy": 0.7})
        tables = {"result_tables": {"all_metric_rows": [{"evidence_id": "accuracy:test:candidate"}]}}
        for refs, expected in ((["accuracy"], "met"), (["accuracy:test:candidate"], "met"),
                               (["invented"], "inconclusive"), ([], "inconclusive")):
            with self.subTest(refs=refs):
                result = parse_goal_assessment({"task_type": "reproduction", "status": "met",
                    "reason": "Matches the stated reproduction tolerance.", "evidence_refs": refs}, context, tables)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.task_type, "reproduction")
        self.assertEqual(parse_goal_assessment(None, context, tables).status, "inconclusive")
        client = FakeAnalysisClient({"goal_assessment": {"task_type": "improvement", "status": "not_met",
            "reason": "The desired improvement is absent.", "evidence_refs": ["accuracy"]}})
        result = run_result_analysis(context, client=client, use_llm=True)
        self.assertEqual(result.goal_assessment.status, "not_met")
        self.assertIn("Reproduction does not require beating a baseline", client.user)

    def test_invalid_goal_evidence_gets_one_correction_without_changing_verdict(self):
        import json
        from unittest.mock import Mock

        context = AnalysisContext(
            metrics={"accuracy": 0.8},
            metadata={"research_goal": "Improve accuracy", "remaining_authorized_rounds": 0},
        )
        recommendation = {
            "action": "stop", "task_disposition": "stop",
            "reason": "The measured evidence is insufficient for a robust claim.",
        }
        client = Mock()
        client.ask.side_effect = [
            json.dumps({"recommendation": recommendation, "goal_assessment": {
                "task_type": "improvement", "status": "inconclusive",
                "reason": "Only one condition was observed.", "evidence_refs": ["invented"]}}),
            json.dumps({"recommendation": recommendation, "goal_assessment": {
                "task_type": "improvement", "status": "inconclusive",
                "reason": "Only one condition was observed.", "evidence_refs": ["accuracy"]}}),
        ]
        result = run_result_analysis(context, client=client, use_llm=True)
        self.assertEqual(client.ask.call_count, 2)
        self.assertEqual(result.goal_assessment.status, "inconclusive")
        self.assertEqual(result.goal_assessment.evidence_refs, ["accuracy"])
        self.assertIn("Invalid refs", client.ask.call_args.args[1])
        self.assertIn("accuracy", client.ask.call_args.args[1])

    def test_malformed_supplement_gets_one_model_correction(self) -> None:
        import json
        from unittest.mock import Mock

        context = AnalysisContext(task_id="followup", metrics={"accuracy": 0.8},
            metadata={"execution_protocol": {"can_extend_seed_condition": True}})
        client = Mock()
        client.ask.side_effect = [
            json.dumps({"recommendation": {"action": "supplement", "supplement": "repeat measurement"}}),
            json.dumps({"recommendation": {"action": "supplement", "supplement": {"seed": 9, "gap": "seed sensitivity"}}}),
        ]
        result = run_result_analysis(context, client=client, use_llm=True)
        self.assertEqual(result.recommendation.supplement["seed"], 9)
        self.assertEqual(client.ask.call_count, 2)
        self.assertIn("explicit integer seed", client.ask.call_args.args[1])

    def test_missing_or_unknown_recommendation_is_undecided(self) -> None:
        from simple_ar.result_analysis.schema import AnalysisRecommendation
        from simple_ar.result_analysis.service import parse_recommendation

        fallback = AnalysisRecommendation(action="request_input", reason="offline")
        missing = parse_recommendation(None, fallback=fallback)
        unknown = parse_recommendation({"action": "invented"}, fallback=fallback)
        self.assertEqual(missing.action, "request_input")
        self.assertEqual(unknown.action, "request_input")
        self.assertEqual(missing.task_disposition, "undecided")
        self.assertIn("no scientific stop was inferred", missing.reason.lower())

    def test_research_stop_without_task_level_basis_is_repaired_once(self) -> None:
        import json
        from unittest.mock import Mock

        context = AnalysisContext(task_id="negative-result", metrics={"accuracy": 0.4},
            metadata={"research_goal": "Improve accuracy", "remaining_authorized_rounds": 1})
        client = Mock()
        client.ask.side_effect = [
            json.dumps({"recommendation": {"action": "stop", "reason": "The current candidate failed."}}),
            json.dumps({"recommendation": {"action": "stop", "task_disposition": "stop",
                "reason": "The current candidate failed and no supported alternative remains.",
                "alternatives_considered": ["The only other supplied candidate lacks the required dataset."]}}),
        ]
        result = run_result_analysis(context, client=client, use_llm=True)
        self.assertEqual(client.ask.call_count, 2)
        self.assertEqual(result.recommendation.task_disposition, "stop")
        self.assertIn("task-level stop", client.ask.call_args.args[1])

    def test_fixed_command_cannot_turn_a_seed_suggestion_into_execution(self) -> None:
        from unittest.mock import Mock

        context = AnalysisContext(task_id="fixed", metrics={"accuracy": 0.8},
            metadata={"execution_protocol": {"can_extend_seed_condition": False}})
        client = Mock()
        client.ask.return_value = '{"recommendation":{"action":"supplement","supplement":{"seed":9}}}'
        with self.assertRaisesRegex(LLMError, "after one correction"):
            run_result_analysis(context, client=client, use_llm=True)
        self.assertEqual(client.ask.call_count, 2)

    def test_fixed_command_still_allows_evidence_based_candidate_revision(self) -> None:
        client = FakeAnalysisClient({
            "summary": {"method": "Measured", "results": "Candidate comparison available.", "limitations": "One fixed seed."},
            "claims": [],
            "analysis_audit": {},
            "recommendation": {
                "action": "revise_candidate",
                "task_disposition": "continue",
                "reason": "The measured candidate leaves a documented objective gap.",
                "evidence_refs": ["attempts/experiment-0010/results.json", "attempts/experiment-0012/results.json"],
                "revision_intent": "Test one distinct candidate direction against the observed results.",
                "revision_purpose": "method_change",
                "revision_constraints": ["Keep the accepted command, data and evaluator unchanged."],
                "revision_base": "candidate",
                "supplement": {},
            },
        })
        context = AnalysisContext(
            task_id="fixed-command-revision",
            title="Choose one further candidate only after reviewing the current comparison.",
            metrics={"accuracy": 0.824444},
            metadata={
                "research_goal": "Measure the initial candidate, then choose one authorized alternative.",
                "remaining_authorized_rounds": 1,
                "execution_protocol": {"can_extend_seed_condition": False, "condition_mode": "fixed_command"},
                "analysis_checkpoint": {
                    "stage": "post_measurement_decision",
                    "remaining_authorized_rounds": 1,
                    "current_candidate": {
                        "role": "current_candidate",
                        "artifact_ref": {"path": "attempts/experiment-0012/results.json"},
                        "implementation_ref": {"path": "attempts/implement-0011/implementation.json"},
                    },
                    "measurements": [
                        {"role": "baseline", "artifact_ref": {"path": "attempts/experiment-0010/results.json"}},
                        {"role": "current_candidate", "artifact_ref": {"path": "attempts/experiment-0012/results.json"}},
                    ],
                },
            },
        )

        result = run_result_analysis(context, client=client, use_llm=True)

        self.assertEqual(result.recommendation.action, "revise_candidate")
        self.assertEqual(result.recommendation.revision_purpose, "method_change")
        self.assertIn("does not prohibit revise_candidate", client.user)
        self.assertIn("future candidate or comparison", client.user)
        self.assertIn("attempts/experiment-0012/results.json", client.user)

    def test_verification_only_revision_is_corrected_before_decision(self) -> None:
        import json
        from unittest.mock import Mock

        context = AnalysisContext(
            task_id="verification-only", metrics={"accuracy": 0.8},
            metadata={"research_goal": "Improve accuracy", "remaining_authorized_rounds": 1},
        )
        client = Mock()
        client.ask.side_effect = [
            json.dumps({"recommendation": {
                "action": "revise_candidate", "task_disposition": "continue",
                "reason": "Record an extra debug value.", "revision_purpose": "verification_only",
                "revision_intent": "Add a log line without changing the candidate.",
            }}),
            json.dumps({"recommendation": {
                "action": "stop", "task_disposition": "stop",
                "reason": "No authorized additional measurement or distinct supported candidate remains.",
                "alternatives_considered": ["The supplied alternatives lack implementation evidence."],
            }}),
        ]

        result = run_result_analysis(context, client=client, use_llm=True)

        self.assertEqual(result.recommendation.action, "stop")
        self.assertEqual(client.ask.call_count, 2)
        self.assertIn("cannot justify another identical training run", client.ask.call_args.args[1])

    def test_prompt_preserves_canonical_comparison_without_condition_tables(self) -> None:
        import json
        from simple_ar.result_analysis.service import build_prompt, build_metric_summary

        comparison = {"comparability": "unknown", "reasons": ["Protocol incomplete"],
                      "baseline": {"metrics": {"accuracy": 0.7}},
                      "candidate": {"metrics": {"accuracy": 0.9}},
                      "baseline_ref": {"path": "baseline/results.json"}}
        execution = {"status": "passed", "metrics": {"accuracy": 0.9},
                     "comparisons": [comparison], "execution": {"stdout": "large process log"},
                     "guard": {"status": "failed", "issues": [{"code": "missing_required_metric"}]},
                     "diagnosis": {"status": "failed", "summary": "Partial measurement"},
                     "output_evidence": [{"name": "resource_record", "status": "available",
                         "artifact": "outputs/resources.json", "preview": "A resource deadline stopped later conditions."}]}
        context = AnalysisContext(task_id="comparison", metrics={"accuracy": 0.9},
                                  project_results={"execution_result": execution})
        prompt = build_prompt(context, build_metric_summary(context), run_result_analysis(context))
        payload = json.loads(prompt.split("\n\n")[-1])
        projected = payload["context"]["project_results"]["execution_result"]
        self.assertEqual(projected["comparisons"], [comparison])
        for key in ("guard", "diagnosis", "output_evidence"):
            self.assertEqual(projected[key], execution[key])
        self.assertNotIn("execution", projected)

    def test_prompt_carries_the_accepted_seed_extension_boundary(self) -> None:
        import json
        from simple_ar.result_analysis.service import build_prompt, build_metric_summary

        context = AnalysisContext(
            task_id="seed-boundary",
            metrics={"accuracy": 0.8},
            metadata={"execution_protocol": {
                "declared_seeds": [0, 1],
                "seed_flag": "--seed",
                "can_extend_seed_condition": True,
                "condition_mode": "paired_seed",
            }},
        )

        prompt = build_prompt(context, build_metric_summary(context), run_result_analysis(context))
        payload = json.loads(prompt.split("\n\n")[-1])

        self.assertTrue(payload["context"]["metadata"]["execution_protocol"]["can_extend_seed_condition"])
        self.assertIn("Never derive a seed from the iteration", prompt)

    def test_llm_recommendation_is_normalized_and_keeps_analysis_context(self) -> None:
        context = AnalysisContext(
            task_id="decision-context",
            metrics={"accuracy": 0.8},
            metadata={"research_history": [{"action": "analysis", "status": "completed"}]},
        )
        client = FakeAnalysisClient({
            "summary": {"method": "Measured", "results": "Observed.", "limitations": "Small."},
            "claims": [],
            "analysis_audit": {},
            "recommendation": {
                "action": "revise",
                "reason": "The error pattern identifies a bounded candidate change.",
                "evidence_refs": ["attempts/a/analysis.json"],
                "revision_intent": "Change the candidate rule for the observed error class.",
                "revision_purpose": "method_change",
                "revision_constraints": ["Keep the evaluator unchanged."],
                "revision_base": "candidate",
                "supplement": {"command": ["do-not-execute"], "seed": 9},
            },
        })

        result = run_result_analysis(context, client=client, use_llm=True)

        self.assertEqual(result.recommendation.action, "revise_candidate")
        self.assertEqual(result.recommendation.supplement, {"seed": 9})
        self.assertEqual(result.recommendation.revision_base, "candidate")
        self.assertEqual(result.recommendation.revision_purpose, "method_change")
        self.assertEqual(result.decision_context, context.metadata)
        self.assertIn("recommendation", client.user)

    def test_prompt_preserves_implementation_lineage_for_paired_results(self) -> None:
        import json
        from simple_ar.result_analysis.service import build_prompt, build_metric_summary

        implementation = {
            "status": "validated",
            "steps": [{"name": "patch", "status": "passed"}],
            "artifact_refs": {"patch": {"path": "attempts/implement/code_task/patch.diff"}},
        }
        context = AnalysisContext(
            task_id="paired-implementation",
            metrics={"accuracy": 0.93},
            project_results={
                "execution_result": {
                    "status": "passed",
                    "implementation_ref": {"path": "attempts/implement/implementation.json"},
                    "candidate_revision": 0,
                },
                "implementation": implementation,
            },
        )

        prompt = build_prompt(context, build_metric_summary(context), run_result_analysis(context))
        payload = json.loads(prompt.split("\n\n")[-1])
        projected = payload["context"]["project_results"]
        self.assertEqual(projected["implementation"]["status"], "validated")
        self.assertEqual(
            projected["execution_result"]["implementation_ref"]["path"],
            "attempts/implement/implementation.json",
        )

    def test_llm_mode_without_client_does_not_fallback(self) -> None:
        with self.assertRaisesRegex(LLMError, "refusing deterministic fallback"):
            run_result_analysis(
                AnalysisContext(task_id="T0", metrics={"score": 1.0}),
                use_llm=True,
            )

    def test_metric_summary_marks_missing_and_all_zero(self) -> None:
        context = AnalysisContext(
            task_id="T1",
            expected_metrics=[{"name": "accuracy", "direction": "maximize"}, {"name": "f1", "direction": "maximize"}],
            metrics={"accuracy": 0.0},
        )

        result = run_result_analysis(context)

        self.assertIn("f1", result.audit.missing_required_metrics)
        self.assertIn("all comparable metrics are zero", result.audit.weak_metric_signals)

    def test_paired_summary_does_not_infer_a_hypothesis_verdict(self) -> None:
        context = AnalysisContext(
            task_id="paired",
            hypotheses=[{
                "id": "h1",
                "statement": "The candidate lowers forgetting.",
                "metric_refs": ["forgetting"],
                "evidence": [{"source": "paper-1"}],
            }],
            expected_metrics=[{"name": "forgetting", "direction": "lower"}],
            metrics={"forgetting": 0.6},
            project_results={
                "execution_result": {"status": "passed"},
                "paired_summary": [{
                    "metric": "forgetting",
                    "n": 3,
                    "baseline_mean": 0.7,
                    "candidate_mean": 0.6,
                    "delta_mean": -0.1,
                }],
            },
        )

        for statement in ("The candidate lowers forgetting.", "The candidate increases forgetting."):
            with self.subTest(statement=statement):
                hypothesis = {**context.hypotheses[0], "statement": statement}
                result = run_result_analysis(context.model_copy(update={"hypotheses": [hypothesis]}))
                claim = result.claims[0]
                self.assertEqual(claim.verdict, "not_evaluated")
                evidence = next(item for item in claim.evidence if item.get("source") == "paired_summary:forgetting")
                self.assertEqual(evidence["delta"], -0.1)
                self.assertTrue(evidence["favorable"])
                self.assertIn("no significance test", claim.limitations[0])
                self.assertTrue(any("do not determine" in item for item in claim.limitations))
        for count in (0, -1, True, 3.0, "3"):
            with self.subTest(invalid_pair_count=count):
                summary = {**context.project_results["paired_summary"][0], "n": count}
                results = {**context.project_results, "paired_summary": [summary]}
                claim = run_result_analysis(context.model_copy(update={"project_results": results})).claims[0]
                self.assertEqual(claim.verdict, "not_evaluated")
                self.assertNotIn("paired_summary:forgetting", {item.get("source") for item in claim.evidence})

    def test_missing_metric_keeps_paired_hypothesis_unevaluated(self) -> None:
        context = AnalysisContext(
            task_id="paired-incomplete",
            hypotheses=[{
                "id": "h1",
                "statement": "The candidate lowers forgetting and improves accuracy.",
                "metric_refs": ["forgetting", "accuracy"],
            }],
            expected_metrics=[
                {"name": "forgetting", "direction": "lower"},
                {"name": "accuracy", "direction": "higher"},
            ],
            metrics={"forgetting": 0.8},
            project_results={
                "execution_result": {"status": "passed"},
                "paired_summary": [{
                    "metric": "forgetting",
                    "n": 3,
                    "baseline_mean": 0.7,
                    "candidate_mean": 0.8,
                    "delta_mean": 0.1,
                }],
            },
        )

        result = run_result_analysis(context)

        self.assertEqual(result.claims[0].verdict, "not_evaluated")
        self.assertTrue(any("unavailable" in item for item in result.claims[0].limitations))

    def test_secondary_improvement_does_not_imply_partial_hypothesis_support(self) -> None:
        context = AnalysisContext(
            hypotheses=[{
                "id": "h1", "statement": "Reduce forgetting without harming accuracy.",
                "metric_refs": ["forgetting", "accuracy", "average_incremental_accuracy"],
            }],
            expected_metrics=[
                {"name": "forgetting", "direction": "lower"},
                {"name": "accuracy", "direction": "higher"},
                {"name": "average_incremental_accuracy", "direction": "higher"},
            ],
            metrics={"forgetting": 0.71, "accuracy": 0.12, "average_incremental_accuracy": 0.28},
            project_results={"paired_summary": [
                {"metric": "forgetting", "n": 3, "delta_mean": 0.025},
                {"metric": "accuracy", "n": 3, "delta_mean": -0.011},
                {"metric": "average_incremental_accuracy", "n": 3, "delta_mean": 0.008},
            ]},
        )
        result = run_result_analysis(context)
        claim = result.claims[0]
        self.assertEqual(claim.verdict, "not_evaluated")
        self.assertEqual(len(claim.evidence), 3)
        self.assertTrue(any("do not determine" in item for item in claim.limitations))

    def test_llm_supported_claim_without_evidence_is_downgraded(self) -> None:
        import json

        context = AnalysisContext(
            task_id="T2",
            expected_metrics=[{"name": "rmse", "direction": "minimize"}],
            metrics={"rmse": 0.42},
        )
        client = FakeAnalysisClient(
            {
                "summary": {"method": "Measured", "results": "The experiment is successful.",
                            "limitations": "Only one run was provided."},
                "claims": [
                    {
                        "claim_id": "c1",
                        "claim": "The experiment is successful.",
                        "verdict": "supported",
                        "confidence": "high",
                    }
                ],
                "analysis_audit": {},
            }
        )

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = run_result_analysis(context, client=client, use_llm=True, output_dir=out)
            self.assertEqual(result.claims[0].verdict, "not_evaluated")
            self.assertIn("c1", result.audit.downgraded_claims)
            saved = json.loads((out / "claims.json").read_text(encoding="utf-8"))
            for claims in (result.claims_payload, saved):
                self.assertEqual(claims["claims"][0]["verdict"], "not_evaluated")
                self.assertEqual(claims["hypothesis_verdicts"][0]["verdict"], "not_evaluated")
            report = (out / "analysis_report.md").read_text(encoding="utf-8")
            self.assertEqual(report, result.readme_markdown)
            self.assertIn("**not_evaluated** `c1`", report)
            self.assertNotIn("**supported** `c1`", report)
            self.assertNotIn("\nThe experiment is successful.\n", report)
            self.assertIn("Only one run was provided.", report)
            self.assertIn("results narrative was not adopted", report)
            self.assertEqual(json.loads((out / "analysis_response.json").read_text(encoding="utf-8")), client.payload)

        # The deterministic path uses the same audited claims, without changing
        # the supplied original result or attributing a natural-language verdict.
        supplied = {"claims": [{"claim_id": "c2", "claim": "Another claim", "verdict": "supported"}]}
        result = run_result_analysis(context.model_copy(update={"project_results": supplied}))
        self.assertEqual(result.claims[0].verdict, "not_evaluated")
        self.assertEqual(result.claims_payload["claims"][0]["verdict"], "not_evaluated")
        self.assertIn("**not_evaluated** `c2`", result.readme_markdown)
        self.assertEqual(supplied["claims"][0]["verdict"], "supported")
        response = {"claims": [], "summary": {"results": "Another claim is supported."}}
        fallback = run_result_analysis(context.model_copy(update={"project_results": supplied}),
            client=FakeAnalysisClient(response), use_llm=True)
        self.assertIn("c2", fallback.audit.downgraded_claims)
        self.assertNotIn("\nAnother claim is supported.\n", fallback.readme_markdown)

        grounded = {"claims": [{"claim_id": "c3", "claim": "Observed rmse", "verdict": "supported",
                                "metric_refs": ["rmse"]}],
                    "summary": {"results": "The recorded rmse is 0.42."}}
        result = run_result_analysis(context, client=FakeAnalysisClient(grounded), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "supported")
        self.assertIn("The recorded rmse is 0.42.", result.readme_markdown)
        mixed = {"claims": [{"claim_id": "c3", "claim": "Observed rmse", "verdict": "supported",
                             "metric_refs": ["rmse", "invented:row"]}]}
        result = run_result_analysis(context, client=FakeAnalysisClient(mixed), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "supported")
        self.assertIn("invented:row", " ".join(result.claims[0].limitations))
        for verdict in ("supported", "unsupported"):
            ungrounded = {"claims": [{"claim_id": "c4", "claim": "A comparison", "verdict": verdict,
                                      "evidence": ["An invented source says so."]}]}
            result = run_result_analysis(context, client=FakeAnalysisClient(ungrounded), use_llm=True)
            self.assertEqual(result.claims[0].verdict, "not_evaluated")
            self.assertIn("c4", result.audit.downgraded_claims)
        cited = {"claims": [{"claim_id": "c5", "claim": "A recorded statement", "verdict": "supported",
                              "evidence": [{"quote": "The supplied text."}]}]}
        result = run_result_analysis(context.model_copy(update={"existing_writeup": "The supplied text."}),
            client=FakeAnalysisClient(cited), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "supported")
        cited["claims"][0]["evidence"] = ["The supplied text."]
        result = run_result_analysis(context.model_copy(update={"existing_writeup": "The supplied text."}),
            client=FakeAnalysisClient(cited), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "supported")
        invalid = {"claims": [{"claim_id": "c6", "claim": "Observed value", "verdict": "supported",
                                "metric_refs": ["score"]}]}
        result = run_result_analysis(AnalysisContext(metrics={"score": float("nan")}),
            client=FakeAnalysisClient(invalid), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "not_evaluated")
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "observed.txt").write_text("input", encoding="utf-8")
            sources = context.model_copy(update={"run_dir": tmp,
                "artifacts": {"observed": "observed.txt", "missing": "missing.txt"}})
            # A file locator, even if it exists, is not an observed source body.
            for source in ("observed", "missing"):
                response = {"claims": [{"claim_id": "c7", "claim": "Recorded input", "verdict": "supported",
                                        "evidence": [{"source": source}]}]}
                result = run_result_analysis(sources, client=FakeAnalysisClient(response), use_llm=True)
                self.assertEqual(result.claims[0].verdict, "not_evaluated")
        rows = [{"dataset": f"d{i}", "condition": "model", "rmse_mean": float(i)} for i in range(250)]
        long_context = context.model_copy(update={"project_results": {"aggregates": rows}})
        response = {"claims": [{"claim_id": "c8", "claim": "Recorded row", "verdict": "supported",
                                "metric_refs": ["rmse:d249:model"]}]}
        result = run_result_analysis(long_context, client=FakeAnalysisClient(response), use_llm=True)
        self.assertEqual(result.claims[0].verdict, "supported")

    def test_writes_analysis_artifacts(self) -> None:
        context = AnalysisContext(
            task_id="T3",
            metrics={"score": 1.0},
            criteria=[{"id": "c1", "task_category": "Code Execution", "weight": 1.0}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            run_result_analysis(context, output_dir=out)

            self.assertTrue((out / "analysis_context.json").is_file())
            self.assertTrue((out / "metric_summary.json").is_file())
            self.assertTrue((out / "rubric_coverage.json").is_file())
            self.assertTrue((out / "claims.json").is_file())
            self.assertTrue((out / "analysis_report.md").is_file())
            self.assertTrue((out / "analysis_audit.json").is_file())

    def test_invalid_llm_json_writes_raw_response_for_diagnosis(self) -> None:
        context = AnalysisContext(task_id="T4", metrics={"score": 1.0})
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            with self.assertRaises(ValueError):
                run_result_analysis(
                    context,
                    output_dir=out,
                    client=FakeAnalysisClient("# Markdown response\nnot json"),
                    use_llm=True,
                )

            self.assertTrue((out / "analysis_prompt.txt").is_file())
            self.assertTrue((out / "analysis_raw_response.txt").is_file())
            self.assertIn("Markdown response", (out / "analysis_raw_response.txt").read_text(encoding="utf-8"))

    def test_refuted_claim_and_dict_metric_refs_are_normalized(self) -> None:
        context = AnalysisContext(
            task_id="T5",
            expected_metrics=[{"name": "rmse", "direction": "minimize"}],
            metrics={"rmse": 2.0},
            criteria=[{"id": "r1", "task_category": "Result Analysis", "weight": 1.0}],
        )
        client = FakeAnalysisClient(
            {
                "summary": {
                    "method": "Compared two conditions.",
                    "results": "The hypothesis was refuted by RMSE.",
                    "limitations": "Small run.",
                    "reproduction_notes": "Run the provided command.",
                },
                "rubric_coverage": [
                    {
                        "category": "Result Analysis",
                        "leaf_count": 1,
                        "verdict": "supported",
                        "evidence": "Hypothesis is discussed.",
                    }
                ],
                "claims": [
                    {
                        "claim_id": "H1",
                        "claim": "Bagging beats boosting.",
                        "verdict": "refuted",
                        "metric_refs": [
                            {
                                "dataset_name": "d1",
                                "condition_name": "bagging",
                                "metric": "rmse",
                                "mean": 2.0,
                            }
                        ],
                        "evidence": ["rmse:d1:bagging was worse."],
                    }
                ],
                "analysis_audit": {},
            }
        )

        result = run_result_analysis(context, client=client, use_llm=True)

        # The model's dataset/condition reference does not exist in the input;
        # an invented reference cannot establish even a negative verdict.
        self.assertEqual(result.claims[0].verdict, "not_evaluated")
        self.assertEqual(result.claims[0].metric_refs, ["rmse:d1:bagging"])
        self.assertIn("Rubric Coverage", result.readme_markdown)
        self.assertIn("Result Analysis", result.readme_markdown)

    def test_llm_rubric_coverage_keeps_deterministic_leaf_counts(self) -> None:
        context = AnalysisContext(
            task_id="T6",
            metrics={"score": 1.0},
            criteria=[
                {"id": "c1", "task_category": "Code Development", "weight": 2.0},
                {"id": "c2", "task_category": "Code Development", "weight": 3.0},
                {"id": "c3", "task_category": "Result Analysis", "weight": 5.0},
            ],
        )
        client = FakeAnalysisClient(
            {
                "summary": {
                    "method": "Implemented and analyzed.",
                    "results": "Metrics are present.",
                    "limitations": "Limited task.",
                    "reproduction_notes": "Run command.",
                },
                "rubric_coverage": [
                    {
                        "category": "Code Development",
                        "verdict": "partially_supported",
                        "evidence": "Implementation artifacts are present.",
                    }
                ],
                "claims": [],
                "analysis_audit": {},
            }
        )

        result = run_result_analysis(context, client=client, use_llm=True)

        coverage = {row["category"]: row for row in result.rubric_coverage}
        self.assertEqual(coverage["Code Development"]["leaf_count"], 2)
        self.assertEqual(coverage["Result Analysis"]["leaf_count"], 1)
        self.assertIn("| Code Development | 2 |", result.readme_markdown)

    def test_extracts_aggregate_rows_and_dict_hypothesis_verdicts(self) -> None:
        context = AnalysisContext(
            task_id="T7",
            hypotheses=[
                {"id": "H1", "statement": "Bagging should beat boosting on high-noise data."},
            ],
            expected_metrics=[{"name": "rmse", "direction": "minimize"}],
            metrics={"rmse": 2.0},
            project_results={
                "aggregates": [
                    {
                        "dataset": "friedman_high_noise",
                        "condition": "bagging",
                        "rmse_mean": 2.0,
                        "rmse_std": 0.1,
                        "n_seeds": 5,
                    },
                    {
                        "dataset": "friedman_high_noise",
                        "condition": "boosting",
                        "rmse_mean": 2.5,
                        "rmse_std": 0.2,
                        "n_seeds": 5,
                    },
                ],
                "hypothesis_verdicts": {
                    "H1": {
                        "supported": True,
                        "evidence": "Bagging has lower RMSE than boosting.",
                        "metric_refs": ["rmse:friedman_high_noise:bagging", "rmse:friedman_high_noise:boosting"],
                    }
                },
            },
        )

        result = run_result_analysis(context)

        rows = result.metric_summary["result_tables"]["primary_metric_rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["evidence_id"], "rmse:friedman_high_noise:bagging")
        self.assertEqual(result.claims[0].claim_id, "H1")
        self.assertEqual(result.claims[0].verdict, "supported")
        self.assertIn("Bagging should beat boosting", result.claims[0].claim)

    def test_extracts_summary_mean_std_rows(self) -> None:
        context = AnalysisContext(
            task_id="T8",
            expected_metrics=[{"name": "balanced_accuracy", "direction": "maximize"}],
            metrics={"balanced_accuracy": 0.8},
            project_results={
                "summaries": [
                    {
                        "dataset": "d1",
                        "condition": "smote",
                        "mean": {"balanced_accuracy": 0.8, "f1": 0.7},
                        "std": {"balanced_accuracy": 0.05, "f1": 0.04},
                        "n_seeds": 5,
                    }
                ]
            },
        )

        result = run_result_analysis(context)

        rows = result.metric_summary["result_tables"]["primary_metric_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["dataset"], "d1")
        self.assertEqual(rows[0]["condition"], "smote")
        self.assertEqual(rows[0]["mean"], 0.8)
        self.assertEqual(rows[0]["std"], 0.05)
        self.assertEqual(rows[0]["count"], 5)

    def test_extracts_generated_project_aggregate_rows(self) -> None:
        context = AnalysisContext(
            task_id="T9",
            expected_metrics=[{"name": "test_accuracy", "direction": "maximize"}],
            metrics={"test_accuracy": 0.9},
            project_results={
                "aggregate_rows": [
                    {
                        "dataset": "wine",
                        "condition": "standard_scaler_knn",
                        "n_splits": 5,
                        "test_accuracy_mean": 0.94,
                        "accuracy_std": 0.02,
                        "macro_f1_mean": 0.93,
                        "macro_f1_std": 0.03,
                    },
                    {
                        "dataset": "wine",
                        "condition": "minmax_scaler_knn",
                        "n_splits": 5,
                        "test_accuracy_mean": 0.96,
                        "accuracy_std": 0.01,
                        "macro_f1_mean": 0.95,
                        "macro_f1_std": 0.02,
                    },
                ],
            },
        )

        result = run_result_analysis(context)

        rows = result.metric_summary["result_tables"]["primary_metric_rows"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["evidence_id"], "test_accuracy:wine:standard_scaler_knn")
        self.assertEqual(rows[0]["count"], 5)
        self.assertIn("| wine | standard_scaler_knn | test_accuracy | 0.94", result.readme_markdown)

    def test_discovers_nested_aggregate_records_by_shape(self) -> None:
        context = AnalysisContext(
            task_id="T10",
            expected_metrics=[{"name": "score", "direction": "maximize"}],
            metrics={"score": 0.77},
            project_results={
                "experiment_output": {
                    "tables": {
                        "by_condition": [
                            {
                                "dataset": "d1",
                                "condition": "method_a",
                                "metric": "score",
                                "mean": 0.77,
                                "std": 0.03,
                                "n": 4,
                            }
                        ]
                    }
                }
            },
        )

        result = run_result_analysis(context)

        rows = result.metric_summary["result_tables"]["primary_metric_rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_id"], "score:d1:method_a")
        self.assertEqual(rows[0]["mean"], 0.77)
        self.assertEqual(rows[0]["std"], 0.03)

    def test_normalizes_structurally_distinct_result_records(self) -> None:
        context = AnalysisContext(
            task_id="T10-variants",
            expected_metrics=[
                {"name": "balanced_accuracy", "direction": "maximize"},
                {"name": "roc_auc", "direction": "maximize"},
                {"name": "r2", "direction": "maximize"},
                {"name": "one_step_rmse", "direction": "minimize"},
            ],
            project_results={
                "root_list_records": [
                    {
                        "dataset": "wine",
                        "condition": "baseline",
                        "seed": 0,
                        "metrics": {"balanced_accuracy": 0.8},
                    }
                ],
                "per_run_records": [
                    {
                        "dataset": "fraud",
                        "method_name": "isolation_forest",
                        "seed": 0,
                        "roc_auc": 0.74,
                    }
                ],
                "aggregate_metrics": [
                    {
                        "dataset_id": "friedman",
                        "kernel_id": "rbf",
                        "mean_r2": 0.77,
                        "n_seeds": 5,
                    }
                ],
                "conditions": [
                    {
                        "key": {"model_type": "gp", "seed": 0, "train_size": 100},
                        "one_step_rmse": 0.31,
                    }
                ],
            },
        )

        result = run_result_analysis(context)
        rows = result.metric_summary["result_tables"]["all_metric_rows"]
        identities = {(row["dataset"], row["condition"], row["metric"]) for row in rows}

        self.assertIn(("wine", "baseline", "balanced_accuracy"), identities)
        self.assertIn(("fraud", "isolation_forest", "roc_auc"), identities)
        self.assertIn(("friedman", "rbf", "r2"), identities)
        self.assertIn(("train_size=100", "gp", "one_step_rmse"), identities)

    def test_task_contract_supplies_required_metrics_and_claim_specs(self) -> None:
        context = AnalysisContext(
            task_id="T11",
            metrics={"accuracy": 0.91},
            task_contract={
                "schema_version": "code_task_contract.v3",
                "contract_id": "contract-test",
                "version_hash": "abc123",
                "metric_contract": {
                    "primary_metric": "accuracy",
                    "required_metrics": ["accuracy", "macro_f1"],
                },
                "claim_specs": [
                    {
                        "claim_id": "H1",
                        "statement": "The classifier should improve macro-F1 without hurting accuracy.",
                        "required_metrics": ["accuracy", "macro_f1"],
                    }
                ],
            },
        )

        result = run_result_analysis(context)

        self.assertIn("macro_f1", result.audit.missing_required_metrics)
        self.assertEqual(result.claims[0].claim_id, "H1")
        self.assertEqual(result.claims[0].verdict, "not_evaluated")
        self.assertEqual(result.claims_payload["task_contract"]["contract_id"], "contract-test")


if __name__ == "__main__":
    unittest.main()
