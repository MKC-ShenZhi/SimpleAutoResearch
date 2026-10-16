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

    def test_offline_scaffold_does_not_guess_modules_or_fill_available_budget(self) -> None:
        expected = {"main.py", "config.json", "generated_experiment/__init__.py", "generated_experiment/runner.py"}
        goals = ("Build a classifier experiment.", "检查生态观测数据并生成报告。",
                 "Input preprocessing analysis reporting self-check resource README workbench")
        for goal in goals:
            for cap in (4, 8, 16):
                with self.subTest(goal=goal, cap=cap):
                    plan = fallback_architecture_plan(contract={"objective": goal},
                        result_schema={"primary_metric": "observed_value", "required_metrics": ["observed_value"]},
                        resource_plan={"max_files": cap}, domain_profile={"task_excerpt": goal})
                    self.assertEqual(plan["objective"], goal)
                    self.assertEqual({row["path"] for row in plan["files"]}, expected)
                    self.assertTrue(all(set(row["dependencies"]) <= expected for row in plan["files"]))

    def test_offline_scaffold_respects_the_same_file_cap_as_model_plans(self) -> None:
        from simple_ar.code_task.generation.architecture import greenfield_architecture_prompt
        from simple_ar.integrations.llm import LLMResponseError
        prompt = greenfield_architecture_prompt(contract={}, result_schema={},
            resource_plan={"max_files": 1, "max_generated_lines": 8000}, domain_profile={})
        self.assertIn("upper limits, not target sizes", prompt)
        self.assertNotIn("use 10-18", prompt)
        self.assertNotIn("use 6-10", prompt)
        for cap in (1, 3):
            with self.subTest(cap=cap), self.assertRaisesRegex(LLMResponseError, "max_files"):
                fallback_architecture_plan(contract={"objective": "Prepare development scaffolding"},
                    result_schema={}, resource_plan={"max_files": cap}, domain_profile={})

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


class ScientificToolTests(unittest.TestCase):
    """Exercise the native domain with physical processes, without model calls."""

    def setUp(self):
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.scientific import ScientificToolGateway
        from simple_ar.tools.workspace import WorkspaceToolGateway

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.ledger = BudgetLedger({'process_invocations': 8, 'process_wall_seconds': 80},
            storage_path=self.root / 'budget.json')
        self.workspace = WorkspaceToolGateway(self.root, {'source0': self.source}, self.ledger)
        self.workspace.mounts['records'] = self.root / 'attempts'
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task')

    def context(self, identity):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext

        return CapabilityContext(ArtifactStore(self.root / 'attempts' / identity),
            AttemptManifest(identity, capability='tool'))

    def arguments(self, directory='project-one', metric='score', value=3, *, file=False, exitcode=0):
        project = self.workspace.workspace / directory
        project.mkdir(parents=True, exist_ok=True)
        script = project / 'evaluate.py'
        script.write_text((
            "import json, os, sys\nfrom pathlib import Path\n"
            "value = float(sys.argv[1])\n"
            "out = Path(os.environ['SIMPLE_AR_OUTPUT_DIR'])\n"
            "(out / 'observations.json').write_text(json.dumps({'batch': [{'observed': value}]}))\n" +
            ("print('file measurement written')\n" if file else f"print('{metric}: ' + str(value))\n") +
            "print('retained diagnostic', file=sys.stderr)\n"
            f"sys.exit({exitcode})\n"
        ), encoding='utf-8')
        schema = {'primary_metric': metric, 'required_metrics': [metric], 'direction': 'higher'}
        if file:
            schema.update(output_files={'observations': 'observations.json'},
                metric_sources={metric: {'output': 'observations', 'path': ['batch', 0, 'observed']}})
        return {'command': ['python', 'evaluate.py', str(value)], 'cwd': f'workspace/{directory}',
            'timeout_sec': 5, 'result_schema': schema, 'label': directory,
            'protocol': {'contract_id': 'explicit-evaluation', 'dataset_refs': [{'id': 'fixture-input'}],
                'split_spec': {'name': 'heldout'}, 'metric_specs': [{'name': metric, 'direction': 'higher'}],
                'comparison_conditions': {'seed': 17}}}

    def measure(self, identity, arguments):
        approval = {'token': 'approved-' + identity,
            'binding': self.gateway.approval_binding(arguments, identity=identity)}
        return self.gateway.execute_tool('measure_experiment', self.context(identity), arguments, approval=approval)

    def test_native_measurements_real_stdout_and_nested_file_metrics_share_budget(self):
        for identity, directory, metric, file in (
                ('stdout', 'project-one', 'score', False),
                ('file', 'different/nested-project', 'throughput', True)):
            with self.subTest(identity=identity):
                result = self.measure(identity, self.arguments(directory, metric, 6, file=file))
                self.assertEqual(result.status, 'ok', result.error)
                self.assertEqual(result.data['metrics'][metric], 6)
                self.assertEqual((result.data['returncode'], result.data['timed_out'],
                    result.data['execution_status']), (0, False, 'passed'))
                self.assertTrue(all(path.startswith('records/') for path in result.artifacts))
                canonical = read_json(self.root / 'attempts' / identity / 'results.json')
                self.assertEqual(canonical['measurement']['source_kind'], 'measured')
                if file:
                    self.assertEqual(canonical['metric_sources'][metric]['path'], ['batch', 0, 'observed'])
                self.assertIn('retained diagnostic', (self.root / 'attempts' / identity /
                    'execution' / 'stderr.txt').read_text())
                self.assertTrue(result.metadata['capability_artifacts'])
        self.assertEqual(len(self.ledger.entries), 2)
        self.assertEqual({entry.attempt_id for entry in self.ledger.entries}, {'stdout', 'file'})
        self.assertTrue(all(entry.status == 'settled' and entry.session_id == 'native-task'
            for entry in self.ledger.entries))

    def test_actual_failure_retains_canonical_and_logs_and_consumes_shared_budget(self):
        args = self.arguments(exitcode=9)
        failed = self.measure('failed', args)
        self.assertEqual(failed.status, 'ok', failed.error)
        self.assertEqual(failed.data['returncode'], 9)
        self.assertEqual(failed.data['execution_status'], 'failed')
        self.assertEqual(failed.data['capability_status'], 'failed')
        self.assertTrue(self.gateway.can_reconcile('measure_experiment', 'failed'))
        self.ledger.limits['process_invocations'] = 1
        exhausted = self.measure('exhausted', args)
        self.assertEqual(exhausted.status, 'blocked')
        self.assertEqual(len(self.ledger.entries), 1)
        self.assertFalse((self.root / 'attempts/exhausted/scientific_request.json').exists())
        self.assertFalse(self.gateway.has_started('exhausted'))
        self.assertFalse(self.gateway.can_reconcile('measure_experiment', 'exhausted'))
        observed = {'tool_name': 'measure_experiment', 'arguments': args,
            'record': 'attempts/failed/scientific_tool_result.json', 'data': {'execution_status': 'passed'}}
        self.assertTrue(self.gateway.delivery_issues([observed]))
        analysis_args = {'result_refs': [failed.data['result_ref']]}
        analyzed = self.gateway.execute_tool('analyze_measurements', self.context('failed-analysis'), analysis_args)
        self.assertEqual(analyzed.status, 'ok', analyzed.error)
        self.assertTrue(self.gateway.delivery_issues([observed, {'tool_name': 'analyze_measurements',
            'arguments': analysis_args, 'record': 'records/failed-analysis/scientific_tool_result.json'}]))
        self.assertFalse(self.gateway.delivery_issues([]))  # A new request supplies its own accepted observations.
        self.ledger.limits['process_invocations'] = 8
        repaired_args = self.arguments()
        repaired = self.measure('repaired', repaired_args)
        self.assertEqual(repaired.status, 'ok', repaired.error)
        self.assertFalse(self.gateway.delivery_issues([observed, {**observed, 'arguments': repaired_args,
            'record': 'records/repaired/scientific_tool_result.json'}]))

    def test_completed_recovery_never_relaunches_and_rejects_changed_request_or_artifacts(self):
        from copy import deepcopy
        import hashlib
        from simple_ar.core.artifacts import write_json
        from simple_ar.tools.scientific import ScientificToolGateway

        args = self.arguments()
        original = self.measure('completed', args)
        self.assertEqual(original.status, 'ok', original.error)
        gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task')
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            recovered = gateway.execute_tool('measure_experiment', self.context('completed'), args)
            self.assertEqual(recovered, original)
            changed = gateway.execute_tool('measure_experiment', self.context('completed'),
                {**args, 'label': 'changed'})
            self.assertEqual(changed.status, 'blocked')
            receipt_path = self.root / 'attempts/completed/scientific_result.json'
            receipt = read_json(receipt_path)
            for field, value in (('data', {'metrics': {'score': 999}}), ('status', 'blocked'), ('artifacts', [])):
                altered = deepcopy(receipt)
                altered['result'][field] = value
                write_json(receipt_path, altered)
                self.assertFalse(gateway.can_reconcile('measure_experiment', 'completed'))
            write_json(receipt_path, receipt)
            canonical_path = self.root / 'attempts/completed/results.json'
            canonical = read_json(canonical_path)
            for field, value in (('command', ['forged']), ('result_schema', {}),
                    ('experiment_contract', {}), ('execution', {**canonical['execution'], 'cwd': str(self.source)})):
                write_json(canonical_path, {**canonical, field: value})
                altered = deepcopy(receipt)
                altered['hashes']['records/completed/results.json'] = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
                write_json(receipt_path, altered)  # Even refreshing this digest cannot bypass cross-checks.
                self.assertFalse(gateway.can_reconcile('measure_experiment', 'completed'))
            write_json(canonical_path, canonical)
            write_json(receipt_path, receipt)
            invocation = canonical['measurement']['measurement_id']
            disk = self.root / f'attempts/completed/execution/process/{invocation}/invocation.json'
            physical = read_json(disk)
            write_json(disk, {**physical, 'argv': ['forged']})
            altered = deepcopy(receipt)
            altered['hashes'][self.workspace.locator(str(disk))] = hashlib.sha256(disk.read_bytes()).hexdigest()
            write_json(receipt_path, altered)
            self.assertFalse(gateway.can_reconcile('measure_experiment', 'completed'))
            write_json(disk, physical)
            write_json(receipt_path, receipt)
            entry = self.ledger.entries[0]
            for field, value in (('actual', {'process_invocations': 0, 'process_wall_seconds': physical['duration_sec']}),
                    ('actual', {'process_invocations': 1, 'process_wall_seconds': physical['duration_sec'] + 1}),
                    ('actual_source', 'model'), ('logical_call_id', 'other'), ('reservation_id', 'other'),
                    ('reserved', {'process_invocations': 1, 'process_wall_seconds': 6})):
                prior = getattr(entry, field)
                setattr(entry, field, value)
                self.assertFalse(gateway.can_reconcile('measure_experiment', 'completed'))
                setattr(entry, field, prior)
            self.assertTrue(gateway.can_reconcile('measure_experiment', 'completed'))
            (self.root / 'attempts/completed/execution/stdout.txt').write_text('tampered')
            self.assertFalse(gateway.can_reconcile('measure_experiment', 'completed'))
            tampered = gateway.execute_tool('measure_experiment', self.context('completed'), args)
            self.assertEqual(tampered.status, 'blocked')
            process.assert_not_called()
        self.assertEqual(len(self.ledger.entries), 1)

    def test_interruption_after_launch_keeps_blocked_even_with_finished_process(self):
        import simple_ar.tools.scientific as scientific

        args = self.arguments()
        actual = scientific.run_experiment_capability

        def interrupted(**kwargs):
            actual(**kwargs)
            raise RuntimeError('interruption before native receipt')

        with patch.object(scientific, 'run_experiment_capability', side_effect=interrupted):
            result = self.measure('interrupted', args)
        self.assertEqual(result.status, 'blocked')
        self.assertTrue(self.gateway.has_started('interrupted'))
        self.assertTrue((self.root / 'attempts/interrupted/results.json').is_file())
        self.assertFalse(self.gateway.can_reconcile('measure_experiment', 'interrupted'))
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            self.assertEqual(self.gateway.execute_tool('measure_experiment',
                self.context('interrupted'), args).status, 'blocked')
            process.assert_not_called()
        self.assertEqual(len(self.ledger.entries), 1)
        import simple_ar.research.project_environment as environments
        prepare = environments.prepare_project_environment
        def interrupted_preparation(**kwargs):
            prepare(**kwargs)
            raise RuntimeError('interruption after setup, before formal measurement')
        prepared_args = {**args, 'environment': {'mode': 'current', 'timeout_sec': 5,
            'check_command': [sys.executable, '-c', 'pass']}}
        with patch.object(environments, 'prepare_project_environment', side_effect=interrupted_preparation):
            result = self.measure('interrupted-preparation', prepared_args)
        self.assertEqual(result.status, 'blocked')
        self.assertTrue(self.gateway.has_started('interrupted-preparation'))
        self.assertFalse(self.context('interrupted-preparation').store.exists('results.json'))
        self.assertFalse(self.gateway.can_reconcile('measure_experiment', 'interrupted-preparation'))
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            self.assertEqual(self.gateway.execute_tool('measure_experiment',
                self.context('interrupted-preparation'), prepared_args).status, 'blocked')
            process.assert_not_called()
        self.assertEqual(len(self.ledger.entries), 2)

    def test_analysis_compares_task_receipts_without_process_or_model_and_keeps_conditions(self):
        import simple_ar.research.analysis as analysis_owner
        from simple_ar.tools.scientific import ScientificToolGateway, measurement_contract

        args = self.arguments()
        candidate_args = self.arguments('another candidate', value=5)
        for request in (args, candidate_args):
            (self.workspace.resolve(request['cwd']) / 'checker.txt').write_text('identical protected evaluator')
            request['protocol']['protected_assets'] = [{'asset_id': 'checker', 'path': 'checker.txt'}]
            request['cwd'] = str(self.workspace.resolve(request['cwd']))
            request['command'] = [sys.executable, *request['command'][1:]]
        different = {**candidate_args, 'protocol': {**args['protocol'], 'comparison_conditions': {'seed': 18}}}
        base18_args = {**args, 'protocol': different['protocol']}
        rows = [(17, 'baseline', args), (17, 'candidate', candidate_args),
                (18, 'baseline', base18_args), (18, 'candidate', different)]
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task',
            execution_contract=measurement_contract({'conditions': [
                {'id': f'{role}:{seed}', 'role': role, 'seed': seed, 'execution': request} for seed, role, request in rows]}))
        for seed, role, request in rows:
            request['condition_id'] = f'{role}:{seed}'
        with self.assertRaises(ValueError):
            self.gateway.approval_binding({**args, 'condition_id': 'candidate:17'}, identity='wrong-condition')
        baseline = self.measure('baseline', args)
        candidate = self.measure('candidate', candidate_args)
        self.assertEqual(baseline.status, 'ok', baseline.error)
        self.assertEqual(candidate.status, 'ok', candidate.error)
        analysis_args = {'result_refs': [candidate.data['result_ref']],
            'baseline_ref': baseline.data['result_ref'], 'question': 'Did score improve?'}
        with patch('simple_ar.experiment.execution.backend.run_process') as process, \
                patch('simple_ar.research.analysis.analyze_results',
                    wraps=analysis_owner.analyze_results) as analyze:
            result = self.gateway.execute_tool('analyze_measurements', self.context('analysis'), analysis_args)
            self.assertEqual(result.status, 'ok', result.error)
            self.assertFalse(analyze.call_args.args[0].use_llm)
            comparison = read_json(self.root / 'attempts/analysis/comparison.json')
            self.assertEqual(comparison['comparability'], 'declared_match')
            self.assertEqual(comparison['deltas']['score'], 2)
            self.assertIn('records/analysis/analysis.md', result.artifacts)
            self.assertEqual(self.gateway.execute_tool('analyze_measurements',
                self.context('analysis'), analysis_args), result)
            process.assert_not_called()
        unmatched = self.measure('different-seed', different)
        analysis_args['result_refs'] = [unmatched.data['result_ref']]
        result = self.gateway.execute_tool('analyze_measurements', self.context('unmatched'), analysis_args)
        self.assertEqual(result.status, 'ok', result.error)
        comparison = read_json(self.root / 'attempts/unmatched/comparison.json')
        self.assertEqual((comparison['comparability'], comparison['verdict']), ('mismatched', 'inconclusive'))
        self.assertEqual(len(self.ledger.entries), 3)
        base18 = self.measure('baseline-18', base18_args)
        self.assertEqual(base18.status, 'ok', base18.error)
        pairs = {'pairs': [
            {'seed': 17, 'baseline_ref': baseline.data['result_ref'], 'candidate_ref': candidate.data['result_ref']},
            {'seed': 18, 'baseline_ref': base18.data['result_ref'], 'candidate_ref': unmatched.data['result_ref']}]}
        with patch('simple_ar.experiment.execution.backend.run_process') as process, \
                patch('simple_ar.research.analysis.analyze_results', wraps=analysis_owner.analyze_results) as analyze:
            locators = [result.data['result_ref'] for result in (baseline, candidate, unmatched, base18)]
            self.assertEqual(self.gateway.analysis_arguments(locators), pairs)
            self.assertIn('Missing declared measurement: baseline:18',
                self.gateway.evidence_requirement(locators[:-1], []))
            self.assertTrue(self.gateway.evidence_requirement(locators,
                ['records/analysis/scientific_tool_result.json']))  # A valid subset is not full coverage.
            collection = self.gateway.execute_tool('analyze_measurements', self.context('collection'), pairs)
            self.assertEqual(collection.status, 'ok', collection.error)
            self.assertEqual(self.gateway.evidence_requirement(locators,
                ['records/collection/scientific_tool_result.json']), [])
            self.assertFalse(analyze.call_args.args[0].use_llm)
            paired = read_json(self.root / 'attempts/collection/paired_analysis.json')
            self.assertEqual(paired['paired_summary'][0]['n'], 2)
            self.assertEqual(paired['paired_summary'][0]['delta_mean'], 2)
            self.assertEqual(paired['paired_summary'][0]['seeds'], [17, 18])
            self.assertIn('records/collection/experiment_set.json', collection.artifacts)
            self.assertIn('experiment_set', [ref['kind'] for ref in collection.metadata['capability_artifacts']])
            self.assertEqual(self.gateway.execute_tool('analyze_measurements', self.context('collection'), pairs), collection)
            for invalid in ({'pairs': [pairs['pairs'][0]], 'result_refs': [candidate.data['result_ref']]},
                    {'pairs': [{'seed': 18, 'candidate_ref': candidate.data['result_ref']}]},
                    {'pairs': [{'seed': 17, 'baseline_ref': baseline.data['result_ref'],
                                'candidate_ref': baseline.data['result_ref']}]},
                    {'pairs': [{'candidate_ref': candidate.data['result_ref']}]},
                    {'pairs': [{'seed': True}]}, {'pairs': [pairs['pairs'][0]] * 2}):
                self.assertEqual(self.gateway.execute_tool('analyze_measurements', self.context('invalid-pairs'), invalid).status, 'blocked')
                self.assertFalse(self.gateway.has_started('invalid-pairs'))
            gaps = self.gateway.execute_tool('analyze_measurements', self.context('gaps'),
                {'pairs': [pairs['pairs'][0], {'seed': 18, 'candidate_ref': unmatched.data['result_ref']}, {'seed': 19}]})
            self.assertEqual(gaps.status, 'ok', gaps.error)
            self.assertNotEqual(gaps.data['analysis_status'], 'passed')
            self.assertEqual(len(gaps.data['missing_measurements']), 3)
            self.assertTrue(self.gateway.delivery_issues([{'tool_name': 'analyze_measurements',
                'record': 'records/gaps/scientific_tool_result.json', 'arguments': {
                    'pairs': [pairs['pairs'][0], {'seed': 18, 'candidate_ref': unmatched.data['result_ref']}, {'seed': 19}]}}]))
            process.assert_not_called()
        self.assertEqual(len(self.ledger.entries), 4)

        # An unseeded declaration is not a waiver of the existing comparison
        # owner's observed condition mismatch. Reuse physical evidence only.
        original = self.gateway.execution_contract
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task',
            execution_contract=measurement_contract({'conditions': [
            {**row, 'seed': None} for row in original['conditions']
            if row['id'] in {'baseline:17', 'candidate:18'}]}))
        issues = self.gateway.evidence_requirement([baseline.data['result_ref'], unmatched.data['result_ref']],
            ['records/unmatched/scientific_tool_result.json'])
        self.assertTrue(any('Comparison conditions mismatched' in issue for issue in issues))
        legacy = dict(read_json(self.root / 'attempts/unmatched/scientific_result.json')['result']['data'])
        legacy.pop('comparison_gaps')
        self.assertTrue(any('Comparison conditions mismatched' in issue for issue in
            self.gateway._analysis_issues(legacy, self.context('unmatched').store)))
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task')
        self.assertEqual(self.gateway._analysis_issues(legacy, self.context('unmatched').store), [])
        # Missing newly introduced fields in an unfrozen historical contract
        # do not retroactively invalidate its already accepted terminal result.

    def test_native_environment_preparation_binds_real_steps_and_never_projects_inherited_secrets(self):
        import os
        import json
        import zipfile
        from simple_ar.tools.scientific import ScientificToolGateway, measurement_contract
        from simple_ar.research.project_environment import project_environment_plan
        secret = 'private-environment-sentinel'
        for mode in ('current', 'venv'):
            with self.subTest(mode=mode):
                args = self.arguments('prepared-' + mode)
                project = self.workspace.resolve(args['cwd'])
                args['cwd'] = str(project)
                args['command'][0] = sys.executable if mode == 'current' else 'python'
                profile = {'mode': mode, 'timeout_sec': 15 if mode == 'venv' else 5,
                    'check_command': [sys.executable if mode == 'current' else 'python', '-c', "print('score: 999')"]}
                if mode == 'venv':
                    profile.update(python_executable=sys.executable, requirements=['requirements.txt'],
                        check_command=['python', '-m', 'pip', 'check'])
                    wheel = project / 'fixture_env-0.1-py3-none-any.whl'
                    with zipfile.ZipFile(wheel, 'w') as archive:
                        for name, text in {'fixture_env.py': 'VALUE = 3\n',
                                'fixture_env-0.1.dist-info/METADATA': 'Metadata-Version: 2.1\nName: fixture-env\nVersion: 0.1\n',
                                'fixture_env-0.1.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
                                'fixture_env-0.1.dist-info/RECORD': ''}.items():
                            archive.writestr(name, text)
                    (project / 'requirements.txt').write_text('./' + wheel.name + '\n')
                    script = project / 'evaluate.py'
                    script.write_text(script.read_text().replace('value = float(sys.argv[1])',
                        'from fixture_env import VALUE\nvalue = VALUE'))
                args['environment'] = profile
                self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task',
                    execution_contract=measurement_contract(args))
                env = {'PRIVATE_ENV_KEY': secret, **({'PYTHONHOME': '/absent-host-python',
                    'PYTHONPATH': '/absent-host-site'} if mode == 'venv' else {})}
                with patch.dict(os.environ, env):
                    binding = self.gateway.approval_binding(args, identity='environment-' + mode)
                    plan = project_environment_plan(profile, project,
                        self.context('environment-' + mode).store.root / 'environment', args['command'])
                    self.assertEqual(binding['environment_preparation']['steps'], plan['steps'])
                    result = self.measure('environment-' + mode, args)
                    self.assertEqual(result.status, 'ok', result.error)
                    self.assertEqual(result.data['metrics']['score'], 3)
                    store = self.context('environment-' + mode).store
                    canonical = store.read_json('results.json')
                    self.assertEqual(canonical['preparation']['source_ref'], binding['environment_preparation']['prepared_ref'])
                    self.assertEqual(len(store.read_json('environment_setup.json')['steps']), len(plan['steps']))
                    self.assertEqual(result.data['effective_execution']['argv'], plan['command'])
                    for name in ('scientific_request.json', 'scientific_result.json', 'results.json', 'environment_observation.json'):
                        self.assertNotIn(secret, json.dumps(store.read_json(name)))
                    ledger = self.ledger.to_dict()
                    with patch('simple_ar.experiment.execution.backend.run_process') as process:
                        recovered = self.gateway.execute_tool('measure_experiment', self.context('environment-' + mode), args)
                        self.assertEqual(recovered, result)
                        self.assertEqual(self.ledger.to_dict(), ledger)
                        process.assert_not_called()
                    if mode == 'venv':
                        setup = store.read_json('environment_setup.json')
                        setup['steps'][-1] = setup['steps'][-2]
                        original_read = store.read_json
                        with patch.object(store, 'read_json', side_effect=lambda path: setup
                                if path == 'environment_setup.json' else original_read(path)), \
                                self.assertRaisesRegex(ValueError, 'own physical invocation'):
                            self.gateway._environment_evidence(store, binding)
        self.assertEqual(len(self.ledger.entries), 7)  # check+measure, then venv/install/check/probe+measure.

    def test_preparation_budget_and_post_setup_source_checks_prevent_unapproved_measurement(self):
        from simple_ar.tools.scientific import ScientificToolGateway, measurement_contract
        args = self.arguments('protected-preparation')
        project = self.workspace.resolve(args['cwd'])
        args.update(cwd=str(project), command=[sys.executable, *args['command'][1:]], environment={
            'mode': 'current', 'timeout_sec': 5, 'check_command': [sys.executable, '-c',
                "from pathlib import Path; Path('evaluate.py').write_text(\"print('score: 999')\\n\")"]})
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task',
            execution_contract=measurement_contract(args))
        with patch.dict(self.ledger.limits, {'process_invocations': 1}), \
                patch('simple_ar.experiment.execution.backend.run_process') as process:
            result = self.measure('no-capacity', args)
            self.assertEqual(result.status, 'blocked')
            self.assertFalse(self.gateway.has_started('no-capacity'))
            process.assert_not_called()
        result = self.measure('mutating-check', args)
        self.assertEqual(result.status, 'blocked')
        self.assertIn('Preparation changed', result.error)
        self.assertTrue(self.gateway.has_started('mutating-check'))
        self.assertFalse(self.context('mutating-check').store.exists('results.json'))
        self.assertEqual(len(self.ledger.entries), 1)
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            self.assertEqual(self.gateway.execute_tool('measure_experiment', self.context('mutating-check'), args).status, 'blocked')
            process.assert_not_called()
        import shutil
        checker = project / 'external-checker'
        shutil.copyfile(sys.executable, checker)
        args['environment'] = {'mode': 'venv', 'python_executable': sys.executable, 'timeout_sec': 5,
            'check_command': [str(checker), '-c', 'pass']}
        args['command'][0] = 'python'
        self.gateway = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task',
            execution_contract=measurement_contract(args))
        approval = {'token': 'explicit-checker', 'binding': self.gateway.approval_binding(args, identity='changed-checker')}
        checker.write_bytes(checker.read_bytes() + b'changed after approval')
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            changed = self.gateway.execute_tool('measure_experiment', self.context('changed-checker'), args, approval=approval)
            self.assertEqual(changed.status, 'blocked')
            self.assertFalse(self.gateway.has_started('changed-checker'))
            process.assert_not_called()

    def test_authorization_strict_input_and_approval_binding_reject_changes_before_launch(self):
        args = self.arguments()
        binding = self.gateway.approval_binding(args, identity='stale')
        script = self.workspace.workspace / 'project-one/evaluate.py'
        script.write_text(script.read_text() + '\n# source changed\n')
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            result = self.gateway.execute_tool('measure_experiment', self.context('stale'), args,
                approval={'token': 'approved', 'binding': binding})
            self.assertEqual(result.status, 'blocked')
            self.assertFalse(self.gateway.has_started('stale'))
            for changes in ({'cwd': 'source0'}, {'cwd': 'workspace/../source'},
                    {'timeout_sec': True}, {'timeout_sec': 0}, {'command': 'python evaluate.py'},
                    {'implementation_ref': 'source0/fake.json'},
                    {'protocol': {'protected_assets': [{'asset_id': 'escape', 'path': str(self.root / 'budget.json')}]}}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.gateway.execute_tool('measure_experiment', self.context('invalid'),
                        {**args, **changes}, approval={'token': 'x', 'binding': {}}).status, 'blocked')
            self.assertEqual(self.gateway.call('measure_experiment', args, identity='no-context').status, 'blocked')
            wrong = self.context('another-task')
            from dataclasses import replace
            from simple_ar.core.capabilities import ArtifactStore
            wrong = replace(wrong, store=ArtifactStore(self.root / 'foreign/attempts/another-task'))
            self.assertEqual(self.gateway.execute_tool('measure_experiment', wrong, args).status, 'blocked')
            process.assert_not_called()
        self.assertFalse(self.ledger.entries)
        from contextlib import chdir
        import hashlib
        from simple_ar.tools.scientific import ScientificToolGateway
        project = self.workspace.resolve(args['cwd'])
        (self.root / 'runner').write_text('host executable is not the approved program')
        runner = project / 'runner'
        runner.write_text('candidate executable bytes')
        exact = {'command': ['./runner'], 'cwd': str(project), 'timeout_sec': 5,
            'result_schema': args['result_schema']}
        fixed = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task', execution_contract=exact)
        with chdir(self.root):
            approved = fixed.approval_binding(exact, identity='relative-executable')
        self.assertEqual(approved['argv'], ['./runner'])
        self.assertEqual(approved['executable_sha256'], hashlib.sha256(runner.read_bytes()).hexdigest())
        import os
        for directory, content in ((self.root / 'bin', 'host PATH program'), (project / 'bin', 'measurement PATH program')):
            directory.mkdir()
            (directory / 'runner').write_text(content)
            (directory / 'runner').chmod(0o755)
        path_contract = {**exact, 'command': ['runner']}
        path_owner = ScientificToolGateway(self.root, self.workspace, self.ledger, 'native-task', execution_contract=path_contract)
        with chdir(self.root), patch.dict(os.environ, {'PATH': './bin' + os.pathsep + os.environ.get('PATH', '')}):
            approved = path_owner.approval_binding(path_contract, identity='relative-path-entry')
        self.assertEqual(approved['executable_sha256'], hashlib.sha256((project / 'bin/runner').read_bytes()).hexdigest())

    def test_analysis_rejects_forged_workspace_results_multiple_candidates_and_tampering(self):
        args = self.arguments()
        measured = self.measure('real', args)
        self.assertEqual(measured.status, 'ok', measured.error)
        canonical = self.root / 'attempts/real/results.json'
        forged = self.workspace.workspace / 'results.json'
        forged.write_bytes(canonical.read_bytes())
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            for refs in (['workspace/results.json'], ['records/real/results.json'] * 2):
                self.assertEqual(self.gateway.execute_tool('analyze_measurements', self.context('forged'),
                    {'result_refs': refs}).status, 'blocked')
            canonical.write_text('{}')
            self.assertEqual(self.gateway.execute_tool('analyze_measurements', self.context('tampered'),
                {'result_refs': ['records/real/results.json']}).status, 'blocked')
            process.assert_not_called()

    def test_protected_assets_observed_and_unbounded_budget_is_not_authorization(self):
        args = self.arguments()
        asset = self.source / 'data.csv'
        asset.write_text('observed,input\n1,2\n')
        args['protocol']['protected_assets'] = [{'asset_id': 'dataset', 'path': 'source0/data.csv'}]
        binding = self.gateway.approval_binding(args, identity='asset')
        self.assertEqual(binding['protected_assets']['dataset']['path'], str(asset))
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                self.gateway.approval_binding(args, identity='linked-asset')
        asset.write_text('changed,input\n3,4\n')
        self.assertEqual(self.gateway.execute_tool('measure_experiment', self.context('asset'), args,
            approval={'token': 'approved', 'binding': binding}).status, 'blocked')
        observed = self.measure('observed', args)
        self.assertEqual(observed.status, 'ok', observed.error)
        canonical = read_json(self.root / 'attempts/observed/results.json')
        self.assertEqual(canonical['measurement']['asset_integrity']['status'], 'observed_unchanged')
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            for key, value in (('process_wall_seconds', None), ('process_wall_seconds', 0),
                    ('process_wall_seconds', 4), ('process_invocations', 0)):
                with self.subTest(key=key, value=value):
                    self.ledger.limits.update(process_invocations=8, process_wall_seconds=80)
                    self.ledger.limits[key] = value
                    self.assertEqual(self.measure('preflight', args).status, 'blocked')
                    self.assertFalse(self.gateway.has_started('preflight'))
                    self.assertFalse((self.root / 'attempts/preflight/scientific_request.json').exists())
            process.assert_not_called()

    def test_mutated_protected_asset_and_unknown_attempt_remain_honest_observations(self):
        args = self.arguments()
        asset = self.workspace.workspace / 'project-one/input.txt'
        asset.write_text('before')
        args['protocol']['protected_assets'] = [{'asset_id': 'input', 'path': 'input.txt'}]
        script = self.workspace.workspace / 'project-one/evaluate.py'
        script.write_text(script.read_text().replace("sys.exit(0)", "Path('input.txt').write_text('after')"))
        measured = self.measure('asset-change', args)
        self.assertEqual(measured.status, 'ok', measured.error)
        self.assertEqual(measured.data['capability_status'], 'failed')
        self.assertEqual(measured.data['canonical_status'], 'failed')
        self.assertEqual(measured.data['execution_status'], 'passed')
        canonical = read_json(self.root / 'attempts/asset-change/results.json')
        self.assertEqual(canonical['measurement']['asset_integrity']['status'], 'changed')
        self.assertTrue(self.gateway.can_reconcile('measure_experiment', 'asset-change'))
        self.ledger.reserve('process-unknown', {'process_invocations': 1, 'process_wall_seconds': 5},
            attempt_id='unknown', session_id='native-task', logical_call_id='unknown', purpose='local_process')
        self.ledger.mark_unknown('process-unknown', reason='Process supervision interrupted')
        with patch('simple_ar.experiment.execution.backend.run_process') as process:
            self.assertTrue(self.gateway.has_started('unknown'))
            self.assertFalse(self.gateway.can_reconcile('measure_experiment', 'unknown'))
            self.assertEqual(self.measure('unknown', args).status, 'blocked')
            process.assert_not_called()


if __name__ == "__main__":
    unittest.main()
