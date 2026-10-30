from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_ar.core.capabilities import (
    ArtifactRef,
    ArtifactStore,
    AttemptManifest,
    CapabilityContext,
)
from simple_ar.research.analysis import (
    AnalysisRequest,
    AnalysisHandoff,
    analyze_results,
    analyze_experiment_capability,
    compare_experiment_results,
)
from simple_ar.result_analysis.schema import AnalysisContext
from simple_ar.result_analysis.schema import AnalysisResult


class AnalysisCapabilityTests(unittest.TestCase):
    def test_exact_table_owner_preserves_literal_grant_and_explicit_limits(self):
        from simple_ar.tools.analysis import AnalysisToolGateway, validate_table_analysis_contract
        from simple_ar.tools.workspace import WorkspaceToolGateway
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = WorkspaceToolGateway(root, {}, None)
            workspace.call('write_file', {'path': 'workspace/data.csv',
                'content': 'value\n1\n2\n'}, identity='data')
            grant = {'path': 'workspace/data.csv', 'spec': {
                'value_columns': ['value'], 'observation_unit': 'supplied row',
                'max_mb': 37, 'max_figures': 101, 'max_points': 10001}}
            frozen = validate_table_analysis_contract(workspace, grant)
            owner = AnalysisToolGateway(root, workspace, contract=frozen)
            grant['spec']['max_mb'] = 1  # Caller mutation cannot alter the grant.
            with patch('simple_ar.tools.analysis.preview_table_source', return_value={'row_count': 2}) as preview:
                self.assertEqual(owner.call('inspect_table', {'path': frozen['path']}, identity='inspect').status, 'ok')
                self.assertEqual(preview.call_args.kwargs['max_mb'], 37)
                denied = owner.call('inspect_table', {'path': str(workspace.resolve(frozen['path']))}, identity='alias')
                self.assertEqual(denied.status, 'error')
                self.assertIn('original authorized path', denied.error)
            changed = {**frozen, 'spec': {**frozen['spec'], 'observation_unit': 'invented replicate'}}
            with patch('simple_ar.tools.analysis.snapshot_table_capability') as snapshot:
                self.assertIn('frozen TableSpec', owner.call('analyze_table', changed, identity='changed').error)
                snapshot.assert_not_called()
            ordinary = AnalysisToolGateway(root, workspace)
            self.assertIn('bounded to 20 MB', ordinary.call('analyze_table', frozen, identity='ungranted').error)
            # Reach the retained snapshot owner with the exact original limits,
            # without computing figures or calling any model in this check.
            from simple_ar.core.capabilities import CapabilityResult
            with patch('simple_ar.tools.analysis.snapshot_table_capability',
                       return_value=CapabilityResult(status='blocked')) as snapshot:
                self.assertIn('snapshot did not complete', owner.call('analyze_table', frozen, identity='exact').error)
                self.assertEqual(snapshot.call_args.kwargs['request']['max_mb'], 37)
                self.assertEqual(snapshot.call_args.kwargs['request']['max_points'], 10001)
            self.assertFalse((root / 'tools/analysis/changed').exists())
            with self.assertRaises(ValueError):
                AnalysisToolGateway(root, workspace, contract={**frozen, 'unexpected': True})
            with self.assertRaises(ValueError):
                validate_table_analysis_contract(workspace, {**frozen, 'path': 'workspace/missing.csv'})

    def test_single_execution_analysis_receives_implementation_lineage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            implementation_ref = store.write_json(
                "implementation.json",
                {
                    "status": "validated",
                    "method_validation": {
                        "status": "未检查",
                        "reason": "No candidate-specific behavior check was recorded.",
                    },
                    "steps": [{"name": "patch", "status": "passed"}],
                },
                kind="implementation_result",
            )
            stale_ref = store.write_json(
                "stale-implementation.json", {"status": "incomplete"}, kind="implementation_result",
            )
            result_ref = store.write_json(
                "result.json",
                {
                    "status": "passed",
                    "metrics": {"accuracy": 0.8},
                    "implementation_ref": implementation_ref.to_dict(),
                },
                kind="experiment_result",
            )
            captured = {}
            analysis = AnalysisResult(readme_markdown="ok", status="passed")

            def capture(request, *, client=None):
                captured["context"] = request.context
                return analysis

            with patch("simple_ar.research.analysis.analyze_results", side_effect=capture):
                result = analyze_experiment_capability(
                    context=CapabilityContext(
                        store=store,
                        attempt=AttemptManifest(attempt_id="analysis-implementation"),
                        inputs=(result_ref, implementation_ref),
                    ),
                    result_ref=result_ref,
                    analysis_context={
                        "project_results": {"implementation_ref": stale_ref.to_dict()},
                    },
                )

            self.assertEqual(captured["context"].project_results["implementation"]["status"], "validated")
            self.assertEqual(captured["context"].project_results["implementation"]["artifact_ref"], implementation_ref.to_dict())
            self.assertNotIn("implementation_ref", captured["context"].project_results)
            self.assertNotIn("steps", captured["context"].project_results["implementation"])
            self.assertEqual(captured["context"].metadata["method_validation"]["status"], "未检查")
            analysis_ref = next(ref for ref in result.artifacts if ref.kind == "analysis_result")
            analysis_payload = store.read_json(analysis_ref)
            self.assertTrue(any(
                "Candidate method validation is 未检查" in item
                for item in analysis_payload["analysis"]["audit"]["limitations"]
            ))

    def test_analysis_does_not_substitute_an_unregistered_implementation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            implementation_ref = store.write_json(
                "implementation.json", {"status": "validated"}, kind="implementation_result",
            )
            result_ref = store.write_json(
                "result.json",
                {"status": "passed", "metrics": {"accuracy": 0.8},
                 "implementation_ref": implementation_ref.to_dict()},
                kind="experiment_result",
            )
            with self.assertRaisesRegex(ValueError, "not a declared analysis input"):
                analyze_experiment_capability(
                    context=CapabilityContext(
                        store=store, attempt=AttemptManifest(attempt_id="analysis-missing-input"),
                        inputs=(result_ref,),
                    ),
                    result_ref=result_ref,
                    analysis_context={},
                )

    def test_paired_summary_groups_conditions_and_preserves_singleton_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            implementation_ref = store.write_json(
                "implementation.json", {"status": "validated"}, kind="implementation_result",
            )
            pairs, inputs = [], []
            for seed, delta, epochs in ((0, 0.1, 1), (1, 0.3, 1), (2, 0.4, 2)):
                pair = {"seed": seed}
                for role, value in (("baseline", 0.5), ("candidate", 0.5 + delta)):
                    ref = store.write_json(f"{role}-{seed}.json", {
                        "status": "passed", "metrics": {"accuracy": value},
                        **({"implementation_ref": implementation_ref.to_dict()} if role == "candidate" else {}),
                        "experiment_contract": {
                            "dataset_refs": [{"asset_id": "fixture"}],
                            "split_spec": {"split": "held-out"},
                            "metric_specs": [{"name": "accuracy", "unit": "fraction"}],
                            "comparison_conditions": {"seed": seed, "epochs": epochs}},
                        "measurement": {"measurement_id": f"{role}-{seed}", "source_kind": "measured",
                                        "protocol_fingerprint": f"fixture-{seed}"}}, kind="experiment_result")
                    pair[role] = ref.to_dict()
                    inputs.append(ref)
                pairs.append(pair)
            collection = store.write_json("set.json", {
                "pairs": pairs, "implementation_ref": implementation_ref.to_dict(),
            }, kind="experiment_set")
            result = analyze_experiment_capability(context=CapabilityContext(store=store,
                attempt=AttemptManifest(attempt_id="analysis-1"), inputs=(collection, *inputs, implementation_ref)),
                result_ref=collection, analysis_context={"research_question": "Describe paired outcomes."})
            evidence = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_set_analysis"))
            summaries = evidence["paired_summary"]
            self.assertEqual(len(summaries), 2)
            self.assertEqual(summaries[0]["seeds"], [0, 1])
            self.assertAlmostEqual(summaries[0]["delta_mean"], 0.2)
            self.assertAlmostEqual(summaries[0]["delta_sample_std"], 0.02 ** 0.5)
            self.assertEqual(len(summaries[0]["sources"]), 2)
            self.assertEqual(summaries[1]["n"], 1)
            self.assertIsNone(summaries[1]["delta_sample_std"])
            self.assertTrue(any("no significance" in text for text in evidence["limitations"]))
            self.assertEqual(evidence["implementation_ref"], implementation_ref.to_dict())
            one_group = store.write_json('one-group.json', {
                'pairs': pairs[:2], 'implementation_ref': implementation_ref.to_dict()}, kind='experiment_set')
            analyze_experiment_capability(context=CapabilityContext(store, AttemptManifest('hypothesis'),
                inputs=(one_group, *inputs, implementation_ref)), result_ref=one_group,
                analysis_context={'hypotheses': [{'id': 'h', 'statement': 'Accuracy decreases.',
                    'metric_refs': ['accuracy']}], 'expected_metrics': [{'name': 'accuracy', 'direction': 'higher'}]})
            claim = store.read_json('analysis.json')['analysis']['claims'][0]
            self.assertEqual(claim['verdict'], 'not_evaluated')
            self.assertAlmostEqual(claim['evidence'][0]['delta'], 0.2)
            candidate_only = store.write_json('candidate-only.json', {'pairs': [
                {**pair, 'baseline': None} for pair in pairs], 'baseline_policy': 'skip'}, kind='experiment_set')
            analyze_experiment_capability(context=CapabilityContext(store, AttemptManifest('candidate-only'),
                inputs=(candidate_only, *inputs, implementation_ref)), result_ref=candidate_only, analysis_context={})
            descriptive = store.read_json('paired_analysis.json')
            self.assertEqual(descriptive['status'], 'passed')
            self.assertEqual(descriptive['missing_measurements'], [])
            self.assertEqual(descriptive['paired_summary'], [])
            self.assertEqual(descriptive['comparisons'], [])
            self.assertEqual([row['n'] for row in descriptive['condition_summary']], [2, 1])
            self.assertIsNone(descriptive['condition_summary'][1]['candidate_sample_std'])
            hidden = store.write_json('hidden-baseline.json', {'pairs': pairs, 'baseline_policy': 'skip'}, kind='experiment_set')
            with self.assertRaisesRegex(ValueError, 'cannot hide'):
                analyze_experiment_capability(context=CapabilityContext(store, AttemptManifest('hidden-baseline'),
                    inputs=(hidden, *inputs, implementation_ref)), result_ref=hidden, analysis_context={})
            for role in ('baseline', 'candidate'):
                altered = store.read_json(f'{role}-1.json')
                altered['result_schema'] = {'output_files': {'measurements': 'metrics.json'},
                    'metric_sources': {'accuracy': {'output': 'measurements', 'path': ['nested', 'value']}}}
                store.write_json(f'{role}-1.json', altered)
            # Each seed pair still matches internally; different selectors must
            # not silently join the same cross-seed mean.
            analyze_experiment_capability(context=CapabilityContext(store,
                AttemptManifest('different-schema'), inputs=(collection, *inputs, implementation_ref)),
                result_ref=collection, analysis_context={})
            separated = store.read_json('paired_analysis.json')['paired_summary']
            self.assertEqual(len(separated), 3)
            self.assertTrue(all(row['n'] == 1 for row in separated))
            other_ref = store.write_json("other-implementation.json", {}, kind="implementation_result")
            mismatched = store.write_json("mismatched-set.json", {
                "pairs": pairs, "implementation_ref": other_ref.to_dict(),
            }, kind="experiment_set")
            with self.assertRaisesRegex(ValueError, "disagrees with its candidate measurement lineage"):
                analyze_experiment_capability(context=CapabilityContext(
                    store=store, attempt=AttemptManifest(attempt_id="analysis-mismatched"),
                    inputs=(mismatched, *inputs, implementation_ref)),
                    result_ref=mismatched, analysis_context={},
                )
            # Exit-zero/canonical passed does not overrule independent guard
            # rejection. The shared owner, not a native-only adapter, excludes it.
            rejected = store.read_json('candidate-0.json')
            rejected['guard'] = {'status': 'failed'}
            store.write_json('candidate-0.json', rejected)
            guarded = analyze_experiment_capability(context=CapabilityContext(store,
                AttemptManifest('guard-analysis'), inputs=(collection, *inputs, implementation_ref)),
                result_ref=collection, analysis_context={})
            guarded_set = store.read_json('paired_analysis.json')
            self.assertEqual(guarded_set['status'], 'incomplete')
            self.assertEqual(guarded_set['failed_measurements'][0]['artifact'], 'candidate-0.json')
            self.assertNotIn(0, [seed for row in guarded_set['paired_summary'] for seed in row['seeds']])
            self.assertIn(guarded.status, {'partial', 'failed'})
            self.assertEqual(store.read_json('candidate-0.json'), rejected)

    def test_collection_analysis_preserves_failed_and_missing_pairs_without_zero_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            baseline = store.write_json("baseline.json", {"status": "passed", "metrics": {"accuracy": 0.75}},
                                        kind="experiment_result")
            failed = store.write_json("failed.json", {"status": "failed", "metrics": {"accuracy": 0.99}},
                                      kind="experiment_result")
            collection = store.write_json("set.json", {"schema_version": "experiment_set.v1", "pairs": [
                {"seed": 0, "baseline": baseline.to_dict(), "candidate": failed.to_dict()},
                {"seed": 1, "baseline": None, "candidate": None},
            ]}, kind="experiment_set")
            result = analyze_experiment_capability(context=CapabilityContext(
                store=store, attempt=AttemptManifest(attempt_id="analysis-1"),
                inputs=(collection, baseline, failed)), result_ref=collection,
                analysis_context={"research_question": "Compare classifiers."})
            evidence = store.read_json(next(ref for ref in result.artifacts if ref.kind == "experiment_set_analysis"))
            self.assertEqual(evidence["status"], "incomplete")
            self.assertEqual(evidence["planned_pairs"], 2)
            self.assertEqual(evidence["failed_measurements"], [
                {"seed": 0, "condition": "candidate", "status": "failed", "artifact": failed.path}])
            self.assertEqual(evidence["missing_measurements"], [
                {"seed": 1, "condition": "baseline"}, {"seed": 1, "condition": "candidate"}])
            self.assertEqual(len(evidence["seed_evidence"]), 1)
            self.assertEqual(evidence["seed_evidence"][0]["metrics"], {"accuracy": 0.75})
            self.assertEqual(evidence["comparisons"][0]["candidate_ref"], failed.to_dict())
            self.assertEqual(evidence["metrics"], {})
            self.assertTrue(evidence['coverage_gaps'])

    def test_analysis_handoff_round_trips_without_execution(self) -> None:
        handoff = AnalysisHandoff(
            execution_ref=ArtifactRef(
                "attempts/run-001/results.json",
                kind="experiment_result",
                schema="canonical_results.2.5",
            ),
            execution_status="passed",
            analysis=analyze_results(
                AnalysisRequest(
                    context={
                        "task_id": "fixture",
                        "project_results": {
                            "execution_result": {
                                "status": "passed",
                                "metrics": {"score": 0.8},
                            }
                        },
                        "metrics": {"score": 0.8},
                    }
                )
            ),
        )

        restored = AnalysisHandoff.from_handoff_dict(handoff.to_handoff_dict())

        self.assertEqual(restored.execution_ref.path, "attempts/run-001/results.json")
        self.assertEqual(restored.execution_status, "passed")
        self.assertEqual(restored.analysis.status, "passed")

    def test_analysis_handoff_rejects_missing_execution_status(self) -> None:
        handoff = AnalysisHandoff(
            execution_ref=ArtifactRef(
                "attempts/run-001/results.json",
                kind="experiment_result",
                schema="canonical_results.2.5",
            ),
            execution_status="passed",
            analysis=analyze_results(
                AnalysisRequest(context={"task_id": "fixture"})
            ),
        ).to_handoff_dict()
        handoff.pop("execution_status")

        with self.assertRaisesRegex(ValueError, "execution_status"):
            AnalysisHandoff.from_handoff_dict(handoff)

    def test_analysis_is_deterministic_and_does_not_write_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = analyze_results(
                AnalysisRequest(
                    context=AnalysisContext(
                        task_id="fixture",
                        title="Offline fixture",
                        research_question="Does the candidate improve accuracy?",
                        expected_metrics=[{"name": "accuracy", "direction": "higher"}],
                        metrics={"accuracy": 0.75},
                    )
                )
            )

            self.assertEqual(result.metric_summary["primary_metric"], "accuracy")
            self.assertFalse(result.audit.llm_used)
            self.assertEqual(result.status, "incomplete")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_analysis_persistence_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "analysis"
            analyze_results(
                AnalysisRequest(
                    context={"task_id": "fixture", "metrics": {"score": 1.0}},
                    output_dir=output_dir,
                )
            )

            self.assertTrue((output_dir / "metric_summary.json").is_file())
            self.assertTrue((output_dir / "analysis_audit.json").is_file())
            self.assertTrue((output_dir / "analysis_status.json").is_file())

    def test_analysis_status_requires_an_explicit_execution_record(self) -> None:
        result = analyze_results(
            AnalysisRequest(
                context=AnalysisContext(
                    task_id="no-execution",
                    metrics={"score": 0.8},
                )
            )
        )

        self.assertEqual(result.status, "incomplete")
        self.assertIn("No canonical execution", result.status_reasons[0])

    def test_analysis_capability_preserves_incomplete_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            result_ref = store.write_json(
                "input/results.json",
                {"status": "passed", "metrics": {"score": 0.8}},
            )
            capability = analyze_experiment_capability(
                context=CapabilityContext(
                    store=store,
                    attempt=AttemptManifest(attempt_id="analysis-001"),
                    inputs=(result_ref,),
                ),
                result_ref=result_ref,
                analysis_context={
                    "task_id": "missing-required-evidence",
                    "expected_metrics": [
                        {"name": "score"},
                        {"name": "f1"},
                    ],
                },
            )

            self.assertEqual(capability.status, "partial")
            self.assertTrue(any("Required metrics are missing" in item for item in capability.diagnostics))

    def test_analysis_status_preserves_execution_and_evidence_state(self) -> None:
        cases = (
            ("passed", "passed"),
            ("failed", "failed"),
            ("timed_out", "failed"),
            ("blocked_by_validation", "blocked"),
        )
        for execution_status, expected in cases:
            with self.subTest(execution_status=execution_status):
                result = analyze_results(
                    AnalysisRequest(
                        context=AnalysisContext(
                            task_id=execution_status,
                            expected_metrics=[{"name": "score", "direction": "higher"}],
                            metrics={"score": 0.8},
                            project_results={
                                "execution_result": {
                                    "status": execution_status,
                                    "metrics": {"score": 0.8},
                                }
                            },
                        )
                    )
                )
                self.assertEqual(result.status, expected)

    def test_analysis_status_marks_guard_or_required_metric_deficiency_incomplete(self) -> None:
        for execution_result in (
            {
                "status": "passed",
                "metrics": {"score": 0.8},
                "guard": {"status": "failed"},
            },
            {
                "status": "passed",
                "metrics": {"score": 0.8},
            },
        ):
            with self.subTest(execution_result=execution_result):
                result = analyze_results(
                    AnalysisRequest(
                        context=AnalysisContext(
                            task_id="incomplete",
                            expected_metrics=[
                                {"name": "score", "direction": "higher"},
                                {"name": "f1", "direction": "higher"},
                            ],
                            metrics={"score": 0.8},
                            project_results={"execution_result": execution_result},
                        )
                    )
                )
                self.assertEqual(result.status, "incomplete")

    def test_analysis_status_uses_only_explicit_regression_for_metric_below_target(self) -> None:
        result = analyze_results(
            AnalysisRequest(
                context=AnalysisContext(
                    task_id="regressed",
                    expected_metrics=[{"name": "score", "direction": "higher"}],
                    metrics={"score": 0.8},
                    project_results={
                        "execution_result": {
                            "status": "passed",
                            "metrics": {"score": 0.8},
                            "comparisons": [{"verdict": "regressed"}],
                        }
                    },
                )
            )
        )

        self.assertEqual(result.status, "metric_below_target")
        self.assertIn("explicit experiment comparison", result.status_reasons[0])

    def test_empty_label_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            AnalysisRequest(context={}, label=" ")

    def test_experiment_comparison_uses_primary_metric_direction(self) -> None:
        comparison = compare_experiment_results(
            {
                "status": "passed",
                "metrics": {"accuracy": 0.70, "runtime_sec": 2.0},
                "primary_metric": "accuracy",
            },
            {
                "status": "passed",
                "metrics": {"accuracy": 0.75, "runtime_sec": 1.5},
                "primary_metric": "accuracy",
            },
            metric_directions={
                "accuracy": "higher_is_better",
                "runtime_sec": "resource",
            },
        )

        self.assertEqual(comparison["schema_version"], "experiment_comparison.v1")
        self.assertEqual(comparison["status"], "ready")
        self.assertEqual(comparison["verdict"], "improved")
        self.assertAlmostEqual(comparison["deltas"]["accuracy"], 0.05)
        accuracy = next(row for row in comparison["metrics"] if row["name"] == "accuracy")
        self.assertTrue(accuracy["is_primary"])
        self.assertEqual(accuracy["interpretation"], "improved")

    def test_experiment_comparison_reads_embedded_schema_and_handles_failure(self) -> None:
        baseline = {
            "status": "failed",
            "metrics": {},
            "result_schema": {
                "primary_metric": "score",
                "direction": "higher",
            },
        }
        candidate = {
            "status": "passed",
            "metrics": {"score": 0.4},
            "result_schema": {
                "primary_metric": "score",
                "direction": "higher",
            },
        }

        comparison = compare_experiment_results(baseline, candidate)

        self.assertEqual(comparison["verdict"], "improved")
        self.assertEqual(comparison["metric_config"]["primary_metric"], "score")
        self.assertEqual(comparison["metric_config"]["metric_directions"]["score"], "higher")

    def test_experiment_comparison_is_inconclusive_without_directional_evidence(self) -> None:
        comparison = compare_experiment_results(
            {"status": "passed", "metrics": {"runtime_sec": 2.0}},
            {"status": "passed", "metrics": {"runtime_sec": 1.0}},
            primary_metric="runtime_sec",
            metric_directions={"runtime_sec": "resource"},
        )

        self.assertEqual(comparison["verdict"], "inconclusive")
        self.assertIn("no directional", comparison["reasons"][0])

    def test_experiment_comparison_reports_missing_primary_metric(self) -> None:
        comparison = compare_experiment_results(
            {"status": "passed", "metrics": {"accuracy": 0.7}},
            {"status": "passed", "metrics": {"accuracy": 0.8}},
            primary_metric="f1",
            metric_directions={"f1": "higher"},
        )

        self.assertEqual(comparison["verdict"], "inconclusive")
        self.assertIn("was not shared", comparison["reasons"][0])


if __name__ == "__main__":
    unittest.main()
