from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_ar.code_task.generation.architecture import fallback_architecture_plan
from simple_ar.code_task.generation.task_contract import build_greenfield_task_contract
from simple_ar.experiment.execution.backend import ExecutionError, LocalExecutionBackend, RunRequest
from simple_ar.experiment.execution.diagnosis import diagnose_experiment_run, render_diagnosis_markdown
from simple_ar.experiment.execution.guards import evaluate_result_guard
from simple_ar.experiment.execution.results import build_canonical_results
from simple_ar.experiment.tools.gateway import LocalExperimentToolGateway
from simple_ar.core.artifacts import read_json


TEST_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"


class ExperimentExecutionTests(unittest.TestCase):
    def test_output_dir_placeholders_write_only_current_invocation_and_record_actual_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "result.json").write_text('{"value": -1}', encoding="utf-8")
            (root / "metrics.csv").write_text("value\n-1\n", encoding="utf-8")
            script = (
                "import sys,json; from pathlib import Path; "
                "target=Path(sys.argv[1].removeprefix('--out=')); directory=Path(sys.argv[2]); "
                "assert target.parent == directory and directory.is_dir(); "
                "target.write_text(json.dumps({'value': 7}), encoding='utf-8'); "
                "(directory/'metrics.csv').write_text('value\\n7\\n', encoding='utf-8'); print(directory)"
            )
            command = [sys.executable, "-c", script, "--out={output_dir}/result.json", "{output_dir}"]
            request = RunRequest(command, root, 5, output_dir=root / "process logs")
            backend = LocalExecutionBackend()
            first, second = backend.run(request), backend.run(request)
            self.assertNotEqual(first.process_record["invocation_id"], second.process_record["invocation_id"])
            for result in (first, second):
                invocation = root / "process logs" / result.process_record["invocation_id"]
                outputs = invocation / "outputs"
                actual = command[:-2] + [f"--out={outputs}/result.json", str(outputs)]
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.command, actual)
                self.assertEqual(result.to_json()["command"], actual)
                self.assertEqual(result.process_record["argv"], actual)
                self.assertEqual(read_json(invocation / "invocation.json")["argv"], actual)
                self.assertEqual(read_json(outputs / "result.json"), {"value": 7})
                self.assertEqual((outputs / "metrics.csv").read_text(encoding="utf-8"), "value\n7\n")
                self.assertEqual(Path(result.stdout.strip()), outputs)
            self.assertEqual(request.command[-2:], ["--out={output_dir}/result.json", "{output_dir}"])
            self.assertEqual(read_json(root / "result.json"), {"value": -1})
            self.assertEqual((root / "metrics.csv").read_text(encoding="utf-8"), "value\n-1\n")

    def test_output_dir_placeholder_without_directory_is_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for arg in ("{output_dir}", "--out={output_dir}/result.json"):
                with self.subTest(arg=arg), patch("simple_ar.experiment.execution.backend.run_process") as run:
                    with self.assertRaisesRegex(ExecutionError, r"\{output_dir\}.*output_dir"):
                        LocalExecutionBackend().run(RunRequest(
                            [sys.executable, "-c", "raise AssertionError('must not launch')", arg], root, 5,
                            env={"SIMPLE_AR_OUTPUT_DIR": str(root)},
                        ))
                    run.assert_not_called()
            self.assertFalse(list(root.iterdir()))

    def test_local_invocations_get_distinct_output_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = [sys.executable, "-c", "import os; from pathlib import Path; "
                "p=Path(os.environ['SIMPLE_AR_OUTPUT_DIR']); assert p.is_dir(); "
                "(p/'metrics.json').write_text('measured'); print(p)"]
            request = RunRequest(command, root, 5, output_dir=root / "processes")
            first, second = LocalExecutionBackend().run(request), LocalExecutionBackend().run(request)
            self.assertEqual((first.returncode, second.returncode), (0, 0))
            paths = [root / "processes" / result.process_record["invocation_id"] / "outputs"
                     for result in (first, second)]
            self.assertNotEqual(paths[0], paths[1])
            for path, result in zip(paths, (first, second)):
                self.assertEqual(result.command, command)
                self.assertEqual(result.process_record["argv"], command)
                self.assertEqual((path / "metrics.json").read_text(), "measured")
                self.assertEqual(Path(result.stdout.strip()), path)

    def test_local_backend_and_canonical_results_keep_legacy_metric_fields(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            root = Path(tmp)
            script = root / "run.py"
            script.write_text("print('accuracy: 0.83')\n", encoding="utf-8")

            run = LocalExecutionBackend().run(
                RunRequest(
                    command=[sys.executable, "run.py"],
                    cwd=root,
                    timeout_sec=10,
                    label="unit",
                )
            )
            result = build_canonical_results(
                run,
                result_schema={
                    "primary_metric": "accuracy",
                    "required_metrics": ["accuracy"],
                },
                artifacts={"stdout": "07-run/stdout.txt"},
            )
            guard = evaluate_result_guard(result)

            self.assertEqual(result["schema_version"], "2.5")
            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["metrics"]["accuracy"], 0.83)
            self.assertEqual(result["execution"]["backend"], "local")
            self.assertEqual(guard["status"], "passed")

    def test_guard_flags_missing_primary_and_nonfinite_metrics(self) -> None:
        result = {
            "returncode": 0,
            "timed_out": False,
            "metrics": {"loss": math.inf},
            "result_schema": {
                "primary_metric": "accuracy",
                "required_metrics": ["accuracy", "loss"],
            },
        }

        guard = evaluate_result_guard(result)

        self.assertEqual(guard["status"], "failed")
        codes = {issue["code"] for issue in guard["issues"]}
        self.assertIn("missing_primary_metric", codes)
        self.assertIn("nonfinite_metric", codes)

    def test_guard_propagates_code_review_warning(self) -> None:
        result = {
            "returncode": 0,
            "timed_out": False,
            "metrics": {"score": 0.7},
            "result_schema": {
                "primary_metric": "score",
                "required_metrics": ["score"],
            },
            "experiment_contract": {
                "task_kind": "greenfield",
                "success_criteria": ["Emit parseable metrics."],
            },
            "verdicts": [
                {
                    "name": "primary_metric_observed",
                    "metric": "score",
                    "value": 0.7,
                    "verdict": "observed",
                }
            ],
            "code_review": {
                "status": "warning",
                "summary": {"warning_count": 1, "error_count": 0},
                "findings": [{"severity": "warning", "code": "review_risk"}],
            },
        }

        guard = evaluate_result_guard(result)

        self.assertEqual(guard["status"], "warning")
        codes = {issue["code"] for issue in guard["issues"]}
        self.assertIn("code_review_warning", codes)
        self.assertIn("success_criteria_requires_review", codes)

    def test_diagnosis_summarizes_missing_metrics_and_review_risk(self) -> None:
        result = {
            "returncode": 0,
            "timed_out": False,
            "metrics": {"loss": 0.4},
            "code_review": {
                "status": "warning",
                "findings": [
                    {
                        "code": "duplicate_pipeline",
                        "message": "Two duplicated experiment pipelines can diverge.",
                    }
                ],
            },
        }
        schema = {"primary_metric": "accuracy", "required_metrics": ["accuracy", "macro_f1"]}
        guard = evaluate_result_guard(result, result_schema=schema)

        diagnosis = diagnose_experiment_run(
            results=result,
            guard_report=guard,
            result_schema=schema,
            code_review=result["code_review"],
        )
        rendered = render_diagnosis_markdown(diagnosis)

        self.assertEqual(diagnosis["status"], "failed")
        self.assertEqual(diagnosis["completion"]["missing_metrics"], ["accuracy", "macro_f1"])
        codes = {item["code"] for item in diagnosis["deficiencies"]}
        self.assertIn("missing_primary_metric", codes)
        self.assertIn("duplicated_or_inconsistent_pipeline", codes)
        self.assertTrue(diagnosis["repair"]["local_repair_supported"])
        self.assertIn("Experiment Diagnosis", rendered)

    def test_guard_marks_greenfield_review_recovery_as_warning(self) -> None:
        result = {
            "returncode": 0,
            "timed_out": False,
            "metrics": {"score": 0.7},
            "result_schema": {
                "primary_metric": "score",
                "required_metrics": ["score"],
            },
            "review_failure_recovery": {
                "reason": "llm_project_failed_code_review",
                "recovery_mode": "deterministic_fallback_scaffold",
            },
        }

        guard = evaluate_result_guard(result)

        self.assertEqual(guard["status"], "warning")
        codes = {issue["code"] for issue in guard["issues"]}
        self.assertIn("code_generation_recovered", codes)

    def test_guard_marks_repaired_experiment_as_warning(self) -> None:
        result = {
            "returncode": 0,
            "timed_out": False,
            "metrics": {"accuracy": 0.86},
            "result_schema": {
                "primary_metric": "accuracy",
                "required_metrics": ["accuracy"],
            },
            "repair": {"status": "patched", "strategy": "schema_metric_fallback"},
        }

        guard = evaluate_result_guard(result)

        self.assertEqual(guard["status"], "warning")
        codes = {issue["code"] for issue in guard["issues"]}
        self.assertIn("experiment_repaired", codes)

    def test_greenfield_task_contract_extracts_evidence_plan(self) -> None:
        contract = build_greenfield_task_contract(
            (
                "# Task\n\n"
                "- Hypothesis H1: robust preprocessing improves accuracy on noisy datasets.\n"
                "- Compare standard scaling against no scaling and robust scaling.\n"
                "- Produce artifacts/results.json and artifacts/report.md with per-dataset rows.\n"
                "- Evaluate macro_f1 and accuracy across multiple conditions.\n"
            ),
            benchmark_command="python main.py",
            max_files=8,
            max_generated_lines=1200,
            result_schema={"primary_metric": "accuracy", "required_metrics": ["accuracy", "macro_f1"]},
        )

        evidence = contract["evidence_plan"]
        self.assertIn("accuracy", evidence["required_metrics"])
        self.assertTrue(evidence["hypotheses"])
        self.assertTrue(evidence["required_comparisons"])
        self.assertTrue(any("artifacts/results.json" in item for item in evidence["required_artifacts"]))
        self.assertTrue(any("Hypothesis evidence" in item for item in contract["success_criteria"]))

    def test_medium_greenfield_fallback_architecture_has_real_module_boundaries(self) -> None:
        plan = fallback_architecture_plan(
            contract={"objective": "Build a medium-light classifier experiment."},
            result_schema={
                "primary_metric": "accuracy",
                "required_metrics": ["accuracy", "macro_f1", "condition_count"],
            },
            resource_plan={"max_files": 10, "max_generated_lines": 1800},
            domain_profile={},
        )

        paths = [row["path"] for row in plan["files"]]

        self.assertIn("main.py", paths)
        self.assertIn("generated_experiment/config.py", paths)
        self.assertIn("generated_experiment/inputs.py", paths)
        self.assertIn("generated_experiment/core.py", paths)
        self.assertIn("generated_experiment/metrics.py", paths)
        self.assertIn("generated_experiment/analysis.py", paths)
        self.assertIn("generated_experiment/runner.py", paths)
        self.assertLessEqual(len(paths), 10)

    def test_large_capability_fallback_architecture_preserves_task_surface(self) -> None:
        plan = fallback_architecture_plan(
            contract={
                "objective": "Greenfield analysis workbench",
                "task": (
                    "Build an open-source style project with input loading, preprocessing, analysis, "
                    "metrics, resource detection, self-check, README, artifacts/results.json, "
                    "artifacts/report.md, and condition_results.jsonl."
                ),
            },
            result_schema={
                "primary_metric": "best_score",
                "required_metrics": ["best_score", "accuracy", "macro_f1", "condition_count"],
            },
            resource_plan={"max_files": 16, "max_generated_lines": 6000},
            domain_profile={"task_excerpt": "input processing analysis reporting self-check resource profile"},
        )

        paths = [row["path"] for row in plan["files"]]

        self.assertIn("README.md", paths)
        self.assertIn("generated_experiment/config.py", paths)
        self.assertIn("generated_experiment/inputs.py", paths)
        self.assertIn("generated_experiment/processing.py", paths)
        self.assertIn("generated_experiment/analysis.py", paths)
        self.assertIn("generated_experiment/reporting.py", paths)
        self.assertIn("generated_experiment/validation.py", paths)
        self.assertLessEqual(len(paths), 16)




    def test_local_experiment_tool_gateway_reads_contract_and_results(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            run_dir = Path(tmp) / "run"
            (run_dir / "05-design").mkdir(parents=True)
            (run_dir / "07-run").mkdir(parents=True)
            (run_dir / "05-design" / "experiment_contract.json").write_text(
                '{"contract_id": "exp-test", "task_kind": "greenfield"}',
                encoding="utf-8",
            )
            (run_dir / "05-design" / "result_schema.json").write_text(
                '{"primary_metric": "accuracy", "required_metrics": ["accuracy"]}',
                encoding="utf-8",
            )
            (run_dir / "07-run" / "results.json").write_text(
                '{"returncode": 0, "timed_out": false, "metrics": {"accuracy": 0.9}}',
                encoding="utf-8",
            )
            (run_dir / "07-run" / "diagnosis.json").write_text(
                '{"status": "passed", "summary": "ok"}',
                encoding="utf-8",
            )

            gateway = LocalExperimentToolGateway(run_dir)
            contract = gateway.call("read_experiment_contract")
            guard = gateway.call("validate_results_schema")
            diagnosis = gateway.call("read_experiment_diagnosis")
            failure_view = gateway.call("inspect_execution_failure")

            self.assertEqual(contract.status, "ok")
            self.assertEqual(contract.data["experiment_contract"]["contract_id"], "exp-test")
            self.assertEqual(guard.status, "ok")
            self.assertEqual(guard.data["guard"]["status"], "passed")
            self.assertEqual(diagnosis.status, "ok")
            self.assertEqual(diagnosis.data["diagnosis"]["status"], "passed")
            self.assertEqual(failure_view.data["diagnosis"]["summary"], "ok")

    def test_local_experiment_tool_gateway_searches_generated_code(self) -> None:
        TEST_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as tmp:
            run_dir = Path(tmp) / "run"
            project = run_dir / "06-code" / "generated_project"
            project.mkdir(parents=True)
            (project / "main.py").write_text(
                "def run_experiment():\n"
                "    metrics = {'accuracy': 0.9, 'macro_f1': 0.8}\n"
                "    return metrics\n",
                encoding="utf-8",
            )
            (project / "config.json").write_text('{"seed": 7}\n', encoding="utf-8")
            gateway = LocalExperimentToolGateway(run_dir)

            listing = gateway.call("list_generated_code_files", {"extensions": [".py"]})
            self.assertEqual(listing.status, "ok")
            self.assertEqual(listing.data["files"][0]["path"], "main.py")

            snippet = gateway.call(
                "read_generated_code_file",
                {"path": "main.py", "start_line": 2, "max_lines": 1},
            )
            self.assertEqual(snippet.status, "ok")
            self.assertIn("accuracy", snippet.data["text"])

            matches = gateway.call("search_generated_code", {"query": "macro_f1"})
            self.assertEqual(matches.status, "ok")
            self.assertEqual(matches.data["matches"][0]["line"], 2)

            blocked = gateway.call("read_generated_code_file", {"path": "../secret.txt"})
            self.assertEqual(blocked.status, "error")


if __name__ == "__main__":
    unittest.main()
