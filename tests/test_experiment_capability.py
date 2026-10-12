from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from simple_ar.core import CapabilityRegistry, SessionController
from simple_ar.research.analysis import analyze_experiment_capability
from simple_ar.research.analysis import compare_experiment_results
from simple_ar.experiment.execution.backend import RunRequest
from simple_ar.research.experiment import (
    ExperimentRequest,
    experiment_request_from_synthesis,
    run_experiment_capability,
    run_and_analyze,
    run_experiment,
)
from simple_ar.result_analysis.schema import AnalysisContext
from simple_ar.research.contracts import ResearchExperimentContract
from simple_ar.research.synthesis import SynthesisResult


class ExperimentCapabilityTests(unittest.TestCase):
    def test_comparison_separates_candidate_identity_from_measured_conditions(self):
        import copy
        from types import SimpleNamespace
        from simple_ar.experiment.execution.measurement import measurement_record, comparison_compatibility

        contract = {
            "contract_id": "idea-a", "hypothesis": "Original hypothesis",
            "dataset_refs": [{"asset_id": "data"}], "split_spec": {"split": "test"},
            "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
            "comparison_conditions": {"seed": 0, "epochs": 1}, "protocol_revision": 1,
        }
        schema = {"primary_metric": "accuracy", "direction": "higher"}
        def measured(identifier, conditions, result_schema=schema):
            return {"experiment_contract": conditions, "result_schema": result_schema,
                    "measurement": measurement_record(SimpleNamespace(
                        process_record={"invocation_id": identifier}, label=identifier), conditions, result_schema)}

        baseline = measured("baseline", contract)
        revised = {**contract, "contract_id": "idea-b", "hypothesis": "Different hypothesis",
                   "proposed_change": "Different candidate", "report_claim_plan": ["Still unproven"]}
        candidate = measured("candidate", revised)
        self.assertEqual(baseline["measurement"]["protocol_fingerprint"], candidate["measurement"]["protocol_fingerprint"])
        historical = copy.deepcopy(baseline)
        historical["measurement"]["protocol_fingerprint"] = "old-whole-contract-fingerprint"
        before = copy.deepcopy(historical)
        self.assertEqual(comparison_compatibility(historical, candidate)[0], "declared_match")
        self.assertEqual(historical, before)
        for key, value in {
            "dataset_refs": [{"asset_id": "other-data"}], "split_spec": {"split": "validation"},
            "metric_specs": [{"name": "accuracy", "unit": "percent"}],
            "comparison_conditions": {"seed": 1, "epochs": 1}, "protocol_revision": 2,
            "protected_assets": [{"asset_id": "evaluator", "path": "evaluate.py"}],
        }.items():
            with self.subTest(field=key):
                self.assertEqual(comparison_compatibility(historical, measured("candidate", {**revised, key: value}))[0], "mismatched")
        self.assertEqual(comparison_compatibility(historical, measured("candidate", revised, {**schema, "direction": "lower"}))[0], "mismatched")
        incomplete = measured("candidate", {"hypothesis": "No declared protocol"})
        self.assertEqual(comparison_compatibility(historical, incomplete)[0], "unknown")
        self.assertEqual(comparison_compatibility(baseline, baseline)[0], "unknown")
        candidate["measurement"]["source_kind"] = "unverified_backend"
        self.assertEqual(comparison_compatibility(historical, candidate)[0], "unknown")

    def test_process_output_directory_is_registered_as_an_experiment_artifact(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / "attempt")
            command = [sys.executable, "-c", "import os; from pathlib import Path; "
                "p=Path(os.environ['SIMPLE_AR_OUTPUT_DIR']); assert p.is_dir(); "
                "(p/'curve.json').write_text('[1, 2]'); print('accuracy: 0.5')"]
            result = run_experiment_capability(context=CapabilityContext(store=store,
                attempt=AttemptManifest(attempt_id="experiment-1")),
                request=ExperimentRequest(run=RunRequest(command, root, 5)))
            self.assertEqual(result.status, "completed", result.diagnostics)
            output = next(ref for ref in result.artifacts if ref.kind == "experiment_outputs")
            self.assertEqual((store.resolve(output) / "curve.json").read_text(), "[1, 2]")
            canonical = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_result"))
            self.assertEqual(canonical["artifacts"]["outputs"], output.path)

    def test_measurement_records_its_declared_implementation_before_persisting(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = ArtifactStore(root / "attempt")
            implementation = store.write_json(
                "inputs/implementation.json", {"status": "validated"},
                kind="implementation_result",
            )
            request = ExperimentRequest(run=RunRequest(
                [sys.executable, "-c", "print('accuracy: 0.5')"], root, 5,
            ))
            context = CapabilityContext(
                store=store,
                attempt=AttemptManifest(attempt_id="candidate-1"),
                inputs=(implementation,),
            )
            result = run_experiment_capability(context=context, request=request)
            canonical = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_result"))
            self.assertEqual(canonical["implementation_ref"], implementation.to_dict())

            other = store.write_json(
                "inputs/other-implementation.json", {"status": "validated"},
                kind="implementation_result",
            )
            with self.assertRaisesRegex(ValueError, "at most one producing implementation"):
                run_experiment_capability(
                    context=CapabilityContext(
                        store=store, attempt=AttemptManifest(attempt_id="candidate-2"),
                        inputs=(implementation, other),
                    ),
                    request=request,
                )

    def test_protected_file_changes_invalidate_measurement_without_losing_process_result(self):
        from simple_ar.experiment.execution.guards import evaluate_result_guard
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "split.json"
            data.write_text("[0, 1]", encoding="utf-8")
            contract = ResearchExperimentContract(
                contract_id="protected-evaluation", hypothesis="Compare on one fixed split.",
                dataset_refs=[{"asset_id": "fixture", "revision": "1"}], split_spec={"file": "split.json"},
                metric_specs=[{"name": "accuracy", "unit": "fraction"}], comparison_conditions={"seed": 0},
                protected_assets=[{"asset_id": "split", "path": "split.json"}],
            )
            def measure(code):
                return run_experiment(ExperimentRequest(
                    run=RunRequest([sys.executable, "-c", code], root, 5),
                    result_schema={"primary_metric": "accuracy", "direction": "higher"},
                    experiment_contract=contract,
                )).canonical
            baseline = measure("print('accuracy: 0.5')")
            self.assertEqual(baseline["validity_status"], "observed_assets_unchanged")
            data.write_text("[2, 3]", encoding="utf-8")
            candidate = measure("print('accuracy: 0.9')")
            self.assertEqual(compare_experiment_results(baseline, candidate)["comparability"], "mismatched")
            tampered = measure("from pathlib import Path; Path('split.json').unlink(); print('accuracy: 1.0')")
            self.assertEqual(tampered["returncode"], 0)
            self.assertEqual(tampered["execution_status"], "passed")
            self.assertEqual(tampered["validity_status"], "invalid")
            self.assertEqual(tampered["status"], "failed")
            self.assertEqual(tampered["metrics"]["accuracy"], 1.0)
            self.assertEqual(tampered["measurement"]["asset_integrity"]["changed_assets"], ["split"])
            self.assertEqual(evaluate_result_guard(tampered)["status"], "failed")
            with self.assertRaises(FileNotFoundError):
                measure("from pathlib import Path; Path('must-not-run').touch()")
            self.assertFalse((root / "must-not-run").exists())

    def test_measured_comparison_tracks_protocol_and_rejects_changed_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            contract = ResearchExperimentContract(
                contract_id="paired-validation", hypothesis="Compare two small classifiers.",
                dataset_refs=[{"asset_id": "fixture", "revision": "v1"}],
                split_spec={"validation_indices": [0, 1]},
                metric_specs=[{"name": "accuracy", "direction": "higher", "unit": "fraction"}],
                comparison_conditions={"seed": 0},
            )
            self.assertEqual(ResearchExperimentContract.from_row(contract.to_row()), contract)
            schema = {"primary_metric": "accuracy", "direction": "higher"}
            def measure(label, correct, protocol=contract, result_schema=schema, exitcode=0):
                return run_experiment(ExperimentRequest(
                    run=RunRequest([sys.executable, "-c", f"print('accuracy:', {correct} / 2); raise SystemExit({exitcode})"],
                                   Path(tmp), 5, label=label),
                    result_schema=result_schema, experiment_contract=protocol,
                )).canonical
            baseline = measure("baseline", 1)
            candidate = measure("candidate", 2)
            self.assertNotEqual(baseline["measurement"]["measurement_id"], candidate["measurement"]["measurement_id"])
            self.assertEqual(candidate["measurement"]["condition_id"], "candidate")
            matching = compare_experiment_results(baseline, candidate)
            self.assertEqual(matching["comparability"], "declared_match")
            self.assertEqual(matching["verdict"], "improved")
            self.assertEqual(compare_experiment_results(candidate, candidate)["comparability"], "unknown")
            failed = measure("failed", 1, exitcode=3)
            self.assertEqual(compare_experiment_results(failed, candidate)["verdict"], "inconclusive")
            for changed, changed_schema in (
                (replace(contract, split_spec={"validation_indices": [2, 3]}), schema),
                (contract, {"primary_metric": "accuracy", "direction": "lower"}),
            ):
                other = measure("other", 2, changed, changed_schema)
                comparison = compare_experiment_results(baseline, other)
                self.assertEqual(comparison["comparability"], "mismatched")
                self.assertEqual(comparison["verdict"], "inconclusive")
                self.assertEqual(comparison["metrics"][0]["interpretation"], "not_comparable")
                self.assertEqual(comparison["deltas"]["accuracy"], 0.5)
                self.assertFalse(any("improved" in reason for reason in comparison["reasons"]))
            incomplete = measure("incomplete", 2, replace(contract, split_spec={}))
            self.assertEqual(compare_experiment_results(baseline, incomplete)["comparability"], "unknown")

    def test_execution_boundary_placeholders_do_not_claim_comparability(self):
        with tempfile.TemporaryDirectory() as tmp:
            contract = ResearchExperimentContract(
                contract_id="prepared-placeholder",
                hypothesis="Compare a prepared command.",
                dataset_refs=[{"asset_id": "prepared_execution", "source": "execution_boundary"}],
                split_spec={"source": "execution_boundary", "status": "not_declared_by_framework"},
                metric_specs=[{"name": "accuracy", "direction": "higher"}],
                comparison_conditions={"source": "execution_boundary", "mode": "same_declared_evaluator"},
            )
            schema = {"primary_metric": "accuracy", "direction": "higher"}

            def measure(label, score):
                return run_experiment(ExperimentRequest(
                    run=RunRequest([sys.executable, "-c", f"print('accuracy:', {score})"], Path(tmp), 5, label=label),
                    result_schema=schema, experiment_contract=contract,
                )).canonical

            baseline, candidate = measure("baseline", 0.5), measure("candidate", 0.9)
            comparison = compare_experiment_results(baseline, candidate)
            self.assertEqual(comparison["comparability"], "unknown")
            self.assertEqual(comparison["verdict"], "inconclusive")
            self.assertIn("placeholder", comparison["reasons"][0])

    def test_synthesis_handoff_builds_explicit_experiment_request(self) -> None:
        contract = ResearchExperimentContract(
            contract_id="synthesis-contract-001",
            hypothesis="The proposed change improves accuracy.",
            metrics=["accuracy"],
        )
        synthesis = SynthesisResult(
            status="ready",
            gap_summary="# Gap Summary\n\nA fixture gap.",
            ideas=(),
            novelty_checks=(),
            experiment_contract=contract,
        )

        request = experiment_request_from_synthesis(
            synthesis.to_handoff_dict(),
            run=RunRequest(
                command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                cwd=Path.cwd(),
                timeout_sec=5,
            ),
            result_schema={"primary_metric": "accuracy", "direction": "higher"},
            artifacts={"results": "results.json"},
        )

        self.assertIsInstance(request, ExperimentRequest)
        self.assertEqual(
            request.experiment_contract.contract_id
            if isinstance(request.experiment_contract, ResearchExperimentContract)
            else None,
            "synthesis-contract-001",
        )
        self.assertEqual(request.result_schema["primary_metric"], "accuracy")
        self.assertEqual(request.artifacts["results"], "results.json")

    def test_synthesis_handoff_without_contract_is_not_executable(self) -> None:
        synthesis = SynthesisResult(
            status="needs_review",
            gap_summary="",
            ideas=(),
            novelty_checks=(),
        )

        with self.assertRaises(ValueError):
            experiment_request_from_synthesis(
                synthesis,
                run=RunRequest(
                    command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                    cwd=Path.cwd(),
                    timeout_sec=5,
                ),
            )

    def test_execution_returns_canonical_results_without_file_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_experiment(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                        label="fixture",
                    ),
                    result_schema={
                        "primary_metric": "accuracy",
                        "direction": "higher",
                    },
                    artifacts={"stdout": "run/stdout.txt"},
                )
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(result.run.metrics["accuracy"], 0.75)
        self.assertEqual(result.canonical["primary_metric"], "accuracy")
        self.assertEqual(result.canonical["execution"]["label"], "fixture")
        self.assertEqual(result.canonical["artifacts"]["stdout"], "run/stdout.txt")

    def test_comparison_can_be_carried_by_canonical_execution(self) -> None:
        comparison = compare_experiment_results(
            {"status": "passed", "metrics": {"accuracy": 0.70}},
            {"status": "passed", "metrics": {"accuracy": 0.75}},
            primary_metric="accuracy",
            metric_directions={"accuracy": "higher"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = run_experiment(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    comparisons=(comparison,),
                )
            )

        self.assertEqual(result.canonical["comparisons"][0]["verdict"], "improved")

    def test_research_contract_can_cross_into_execution_without_type_aliasing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = ResearchExperimentContract(
                contract_id="research-contract-001",
                hypothesis="A bounded change improves accuracy.",
                motivation_refs=["paper-1#claim-1"],
                baseline="baseline",
                dataset="fixture-data",
                metrics=["accuracy"],
                proposed_change="Change one feature.",
            )
            restored = ResearchExperimentContract.from_row(contract.to_row())
            result = run_experiment(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    result_schema={"primary_metric": "accuracy", "direction": "higher"},
                    experiment_contract=restored,
                )
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(
            result.canonical["experiment_contract"]["contract_id"],
            "research-contract-001",
        )

    def test_typed_research_contract_supplies_missing_result_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            contract = ResearchExperimentContract(
                contract_id="research-contract-002",
                hypothesis="A bounded change improves accuracy.",
                metrics=["accuracy", "macro_f1"],
            )
            result = run_experiment(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    experiment_contract=contract,
                )
            )

        self.assertEqual(result.status, "passed")
        self.assertEqual(
            result.canonical["result_schema"],
            {
                "primary_metric": "accuracy",
                "required_metrics": ["accuracy", "macro_f1"],
                "direction": "unknown",
            },
        )

    def test_explicit_result_schema_overrides_typed_contract_metrics(self) -> None:
        contract = ResearchExperimentContract(
            contract_id="research-contract-003",
            hypothesis="A bounded change improves accuracy.",
            metrics=["accuracy", "macro_f1"],
        )

        request = ExperimentRequest(
            run=RunRequest(
                command=[sys.executable, "-c", "print('score: 0.75')"],
                cwd=Path.cwd(),
                timeout_sec=5,
            ),
            result_schema={"primary_metric": "score", "direction": "higher"},
            experiment_contract=contract,
        )

        self.assertEqual(
            request.normalized_result_schema(),
            {"primary_metric": "score", "direction": "higher"},
        )

    def test_timeout_remains_a_normalized_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_experiment(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "import time; time.sleep(2)"],
                        cwd=Path(tmp),
                        timeout_sec=1,
                    )
                )
            )

        self.assertEqual(result.status, "timed_out")
        self.assertTrue(result.canonical["timed_out"])

    def test_run_and_analyze_passes_observed_metrics_to_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_and_analyze(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    result_schema={"primary_metric": "accuracy", "direction": "higher"},
                ),
                AnalysisContext(
                    task_id="fixture",
                ),
            )

            self.assertEqual(result.status, "passed")
            self.assertEqual(result.analysis.metric_summary["primary_metric"], "accuracy")
            self.assertEqual(result.analysis.metric_summary["metrics"][0]["value"], 0.75)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_run_and_analyze_keeps_failed_execution_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_and_analyze(
                ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "import sys; sys.exit(2)"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    )
                ),
                {"task_id": "failed-fixture", "expected_metrics": [{"name": "score"}]},
            )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.execution.canonical["status"], "failed")
            self.assertIn("score", result.analysis.audit.missing_required_metrics)

    def test_session_adapter_persists_canonical_result_without_fake_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("run-experiment", run_experiment_capability)
            controller = SessionController.create(
                tmp,
                session_id="experiment-session",
                topic="experiment adapter",
                registry=registry,
            )

            result = controller.execute_attempt(
                "run-experiment",
                attempt_id="attempt-001",
                request=ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    result_schema={"primary_metric": "accuracy", "direction": "higher"},
                ),
            )

            self.assertEqual(result.status, "completed")
            refs = controller.attempt_output_refs("attempt-001")
            payload = controller.store.read_json(refs[0])
            self.assertEqual(payload["schema_version"], "2.5")
            self.assertEqual(payload["metrics"]["accuracy"], 0.75)
            self.assertEqual(payload["guard"]["status"], "passed")
            self.assertEqual(payload["diagnosis"]["status"], "passed")
            self.assertEqual(payload["artifacts"]["guard"], "guard_report.json")
            self.assertEqual(payload["artifacts"]["diagnosis"], "diagnosis.json")
            self.assertTrue(
                controller.store.exists("attempts/attempt-001/guard_report.json")
            )
            self.assertTrue(
                controller.store.exists("attempts/attempt-001/diagnosis.json")
            )

    def test_session_adapter_keeps_timeout_as_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("run-experiment", run_experiment_capability)
            controller = SessionController.create(
                tmp,
                session_id="experiment-timeout",
                topic="experiment timeout",
                registry=registry,
            )

            result = controller.execute_attempt(
                "run-experiment",
                attempt_id="attempt-001",
                request=ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "import time; time.sleep(2)"],
                        cwd=Path(tmp),
                        timeout_sec=1,
                    ),
                ),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("Experiment execution timed out.", result.diagnostics)
            self.assertEqual(
                controller.store.read_json(controller.attempt_output_refs("attempt-001")[0])["status"],
                "timed_out",
            )
            self.assertEqual(
                controller.store.read_json(
                    "attempts/attempt-001/guard_report.json"
                )["status"],
                "failed",
            )

    def test_session_adapter_rejects_missing_required_metric(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("run-experiment", run_experiment_capability)
            controller = SessionController.create(
                tmp,
                session_id="experiment-missing-metric",
                topic="experiment guard",
                registry=registry,
            )

            result = controller.execute_attempt(
                "run-experiment",
                attempt_id="attempt-001",
                request=ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('score: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    result_schema={
                        "primary_metric": "accuracy",
                        "required_metrics": ["accuracy"],
                    },
                ),
            )

            self.assertEqual(result.status, "failed")
            self.assertIn("Experiment result guard failed.", result.diagnostics)
            payload = controller.store.read_json(
                "attempts/attempt-001/results.json"
            )
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["guard"]["status"], "failed")

    def test_session_adapter_persists_execution_streams_as_declared_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = CapabilityRegistry()
            registry.register("run-experiment", run_experiment_capability)
            controller = SessionController.create(
                root,
                session_id="experiment-streams",
                topic="execution evidence",
                registry=registry,
            )

            result = controller.execute_attempt(
                "run-experiment",
                attempt_id="attempt-001",
                request=ExperimentRequest(
                    run=RunRequest(
                        command=[
                            sys.executable,
                            "-c",
                            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(2)",
                        ],
                        cwd=root,
                        timeout_sec=5,
                    ),
                ),
            )

            self.assertEqual(result.status, "failed")
            result_ref = controller.attempt_output_refs("attempt-001")[0]
            payload = controller.store.read_json(result_ref)
            self.assertEqual(payload["artifacts"]["stdout"], "execution/stdout.txt")
            self.assertEqual(payload["artifacts"]["stderr"], "execution/stderr.txt")
            process_ref = next(ref for ref in result.artifacts if ref.kind == "process_invocation")
            process_record = controller.store.read_json(controller.attempt_output_ref(
                "attempt-001", kind="process_invocation", schema="process_invocation.v1",
            ))
            self.assertEqual(process_record["returncode"], 2)
            self.assertIn("invocation.json", process_ref.path)
            self.assertEqual(
                controller.store.read_text("attempts/attempt-001/execution/stdout.txt"),
                "out\n",
            )
            self.assertEqual(
                controller.store.read_text("attempts/attempt-001/execution/stderr.txt"),
                "err\n",
            )

    def test_execution_and_analysis_adapters_use_explicit_result_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("experiment", run_experiment_capability)
            registry.register("analysis", analyze_experiment_capability)
            controller = SessionController.create(
                tmp,
                session_id="run-analysis-session",
                topic="execution to analysis",
                profile="experiment",
                registry=registry,
            )

            controller.execute_attempt(
                "experiment",
                attempt_id="attempt-001",
                request=ExperimentRequest(
                    run=RunRequest(
                        command=[sys.executable, "-c", "print('accuracy: 0.75')"],
                        cwd=Path(tmp),
                        timeout_sec=5,
                    ),
                    result_schema={"primary_metric": "accuracy", "direction": "higher"},
                ),
            )
            result_ref = controller.attempt_output_refs("attempt-001")[0]
            result = controller.execute_attempt(
                "analysis",
                attempt_id="attempt-002",
                inputs=(result_ref,),
                result_ref=result_ref,
                analysis_context={"task_id": "fixture"},
            )

            self.assertEqual(result.status, "completed")
            analysis_ref = controller.attempt_output_refs("attempt-002")[0]
            payload = controller.store.read_json(analysis_ref)
            self.assertEqual(payload["schema_version"], "analysis_handoff.v1")
            self.assertEqual(payload["execution_ref"]["path"], "attempts/attempt-001/results.json")
            self.assertEqual(
                payload["analysis"]["metric_summary"]["metrics"][0]["value"],
                0.75,
            )
            self.assertEqual(payload["analysis"]["status"], "passed")


if __name__ == "__main__":
    unittest.main()
