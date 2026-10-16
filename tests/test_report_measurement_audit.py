"""Final report tables must preserve measurement conditions, not just numbers."""

import unittest

from simple_ar.report.projection import (
    _append_verified_experiment_evidence,
    _metric_ledger,
    _verified_experiment_evidence,
    attach_implementation_evidence,
    build_research_report_inputs,
    ReportProjectionError,
)
from simple_ar.core import ArtifactRef, ArtifactStore
from pathlib import Path
import tempfile
from simple_ar.report.audit import build_report_audit
from simple_ar.report.schema import MetricSource, ReportContext, ReportMemory, ReviewerFinding


class ReportMeasurementAuditTests(unittest.TestCase):
    def test_saved_measurement_projection_rejects_unrelated_or_missing_owners(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            current = ArtifactRef(path="attempts/current/results.json", kind="experiment_result")
            foreign = ArtifactRef(path="attempts/other/results.json", kind="experiment_result")
            analysis_ref = ArtifactRef(path="attempts/analysis/analysis.json")
            comparison = store.write_json("comparison.json", {
                "candidate_ref": foreign.to_dict(), "baseline_ref": current.to_dict()})
            inputs = dict(topic="Recorded results", brief=None, search=None, documents=None,
                execution={"status": "passed", "metrics": {}}, analysis=None,
                brief_ref=ArtifactRef(path="brief.json"), execution_ref=current,
                analysis_ref=analysis_ref, evidence_store=store)
            with self.assertRaisesRegex(ReportProjectionError, "accepted candidate"):
                build_research_report_inputs(**inputs, related_refs={"comparison": comparison})
            with self.assertRaises(FileNotFoundError):
                build_research_report_inputs(**inputs, related_refs={
                    "baseline": ArtifactRef(path="missing-baseline.json")})
            collection = ArtifactRef(path="collection.json", kind="experiment_set")
            store.write_json("attempts/analysis/paired_analysis.json", {
                "schema_version": "experiment_set_analysis.v1", "collection_ref": foreign.to_dict()})
            with self.assertRaisesRegex(ReportProjectionError, "accepted measurement collection"):
                build_research_report_inputs(**{**inputs, "execution_ref": collection})

    def test_unlocated_display_requires_verification_not_assumed_rewrite(self):
        from simple_ar.report.schema import finding_requires_resolution
        for name, body in (
            ("group_A_mean_error", "| Group | Mean error |\n|---|---:|\n| A | 0.123 |"),
            ("peak_signal_voltage", "The peak signal was 2.75 V."),
            ("median_queue_time_ms", "No timing result was included."),
        ):
            with self.subTest(name=name):
                value = 2.75 if "voltage" in name else 0.1234
                context = ReportContext(topic="Displayed records", report_mode="experiment",
                    metric_sources=[MetricSource(metric_id="observed", name=name, value=value, artifact="values.json")])
                audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
                self.assertEqual(audit.metric_audit.unmatched_metrics, ["observed"])
                self.assertEqual(audit.status, "warning")
                finding = next(row for row in audit.reviewer_findings if row.type == "mechanical_audit")
                self.assertEqual(finding.required_action, "verify")
                self.assertTrue(finding_requires_resolution(finding))
                self.assertIn("does not establish omission", finding.message)
                self.assertIn("condition, unit and displayed precision", finding.suggested_action)

    def test_confirmed_table_error_is_not_downgraded_to_display_verification(self):
        context = ReportContext(topic="Altered record", report_mode="experiment",
            metric_sources=[MetricSource(metric_id="observed", name="accuracy", value=0.8,
                artifact="values.json", label="candidate")])
        body = _metric_ledger(context.metric_sources).replace("0.8", "0.9")
        audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
        self.assertEqual(audit.status, "failed")
        self.assertEqual(audit.metric_audit.status, "failed")
        confirmed = [row for row in audit.reviewer_findings if "Measurement table row" in row.message]
        self.assertTrue(confirmed)
        self.assertTrue(all(row.required_action is None for row in confirmed))

    def test_multi_condition_appendix_uses_measured_protocol_pairs(self):
        history = []
        sources = []
        for seed, baseline, candidate in ((0, 0.4443464279, 0.4283993840),
                                          (1, 0.4443523586, 0.4287708998)):
            for role, value in (("baseline", baseline), ("candidate", candidate)):
                history.append({
                    "action": f"run-{role}-{seed}", "status": "passed",
                    "artifact": f"attempts/{role}-{seed}/results.json",
                    "metrics": {"rmse": value},
                    "measurement": {
                        "condition_id": f"{role}:seed={seed}",
                        "protocol_fingerprint": f"protocol-{seed}",
                        "source_kind": "measured", "seed": seed,
                    },
                })
                sources.append(MetricSource(
                    metric_id=f"{role}-{seed}", name="rmse", value=value,
                    artifact=history[-1]["artifact"], label=role,
                ))
        context = ReportContext(
            topic="Paired conditions", report_mode="experiment",
            results={
                "measurement_history": history,
                "result_schema": {"primary_metric": "rmse", "required_metrics": ["rmse"]},
                "comparisons": [{"metrics": [{"name": "rmse", "baseline": 0.4443523586,
                                               "candidate": 0.4287708998,
                                               "delta": 0.4287708998 - 0.4443523586}]}],
            },
            metric_sources=sources,
        )
        body = _verified_experiment_evidence(context)
        self.assertIn("### Recorded Paired Conditions", body)
        self.assertIn("| seed=0 | `rmse` | 0.444346 | 0.428399 |", body)
        self.assertIn("| seed=1 | `rmse` | 0.444352 | 0.428771 |", body)
        audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
        self.assertEqual(audit.metric_audit.unmatched_metrics, [])

    def test_multi_condition_appendix_does_not_pair_mismatched_or_duplicate_runs(self):
        rows = [
            {"status": "passed", "metrics": {"rmse": 0.4}, "measurement": {
                "condition_id": "baseline:seed=0", "protocol_fingerprint": "a", "source_kind": "measured"}},
            {"status": "passed", "metrics": {"rmse": 0.3}, "measurement": {
                "condition_id": "candidate:seed=0", "protocol_fingerprint": "b", "source_kind": "measured"}},
        ]
        context = ReportContext(
            topic="Unpaired", report_mode="experiment",
            results={"measurement_history": rows},
            metric_sources=[MetricSource(metric_id="baseline", name="rmse", value=0.4,
                                         artifact="baseline.json", label="baseline")],
        )
        self.assertNotIn("Recorded Paired Conditions", _verified_experiment_evidence(context))
        rows[1]["measurement"]["protocol_fingerprint"] = "a"
        rows.extend([dict(rows[0]), dict(rows[1])])
        context.results["measurement_history"] = rows
        self.assertNotIn("Recorded Paired Conditions", _verified_experiment_evidence(context))

    def test_missing_paired_row_does_not_hide_individual_measurements(self):
        context = ReportContext(
            topic="Failed candidate", report_mode="experiment",
            results={
                "result_schema": {"primary_metric": "rmse", "required_metrics": ["rmse"]},
                "comparisons": [{"seed": 0, "metrics": []}],
            },
            metric_sources=[
                MetricSource(
                    metric_id="baseline-rmse", name="rmse", value=0.44,
                    artifact="attempts/baseline/results.json", label="baseline",
                ),
                MetricSource(
                    metric_id="candidate-rmse", name="rmse", value=0.44,
                    artifact="attempts/candidate/results.json", label="candidate",
                ),
            ],
        )
        body = _verified_experiment_evidence(context)
        self.assertIn("No valid paired comparison row", body)
        self.assertIn("available individual measurements are listed below", body)
        self.assertIn("| baseline | `rmse` | 0.44", body)
        self.assertIn("| candidate | `rmse` | 0.44", body)

    def test_figure_filename_is_not_metric_evidence(self):
        context = ReportContext(
            topic="Measured comparison", report_mode="experiment",
            experiment_plan={"metrics": ["accuracy"]},
            metric_sources=[MetricSource(
                metric_id="accuracy-source", name="accuracy", value=1,
                artifact="attempts/experiment-001/results.json", label="candidate",
            )],
        )
        body = "# Result\n\n![accuracy](figures/paired-1.svg)\n"
        audit = build_report_audit(
            report=body, report_body=body, context=context, memory=ReportMemory(),
        )
        self.assertIn("accuracy-source", audit.metric_audit.unmatched_metrics)
        captioned = body + "\nThe measured accuracy was 1.\n"
        with_caption = build_report_audit(
            report=captioned, report_body=captioned, context=context,
            memory=ReportMemory(),
        )
        self.assertIn("accuracy-source", with_caption.metric_audit.matched_metrics)

    def test_unresolved_minor_factual_review_is_not_a_passed_report(self):
        for mode in ("survey", "experiment"):
            with self.subTest(mode=mode):
                context = ReportContext(topic="Evidence review", report_mode=mode)
                body = "# Evidence review\n\nThe observed comparison is described here.\n"
                memory = ReportMemory(reviewer_findings=[ReviewerFinding(
                    finding_id="known-discrepancy", type="metric_mismatch", severity="minor",
                    message="A prose comparison conflicts with the recorded evidence.",
                )])
                audit = build_report_audit(
                    report=body, report_body=body, context=context, memory=memory,
                )
                self.assertEqual(audit.status, "warning")
                self.assertEqual(audit.semantic_review_status, "semantic_unchecked")
                memory.reviewer_findings[0].severity = "info"
                informational = build_report_audit(
                    report=body, report_body=body, context=context, memory=memory,
                )
                self.assertEqual(informational.status, "passed")

    def test_unresolved_major_factual_review_fails_but_style_remains_warning(self):
        for topic in ("enzyme screening", "database query planning"):
            context = ReportContext(topic=topic, report_mode="research_only")
            body = f"# {topic}\n\nA finding needs review.\n"
            factual = ReportMemory(reviewer_findings=[ReviewerFinding(
                finding_id="unsupported", type="unsupported_claim", severity="major",
                message="The claim is not supported by the inspected source.",
            )])
            style = ReportMemory(reviewer_findings=[ReviewerFinding(
                finding_id="style", type="style", severity="major", message="Improve organization.",
            )])
            self.assertEqual(build_report_audit(report=body, report_body=body,
                context=context, memory=factual).status, "failed")
            self.assertEqual(build_report_audit(report=body, report_body=body,
                context=context, memory=style).status, "warning")

    def test_explicit_citation_scope_is_checked_for_unrelated_survey_topics(self):
        for topic in ("enzyme screening", "database query planning"):
            with self.subTest(topic=topic):
                context = ReportContext(
                    topic=topic, report_mode="research_only",
                    papers=[{"id": f"P{i}"} for i in range(1, 5)],
                    survey_contract={"max_cited_sources": 2},
                )
                excessive = "# Review\n\nFirst [@P1], second [@P2], third [@P3].\n"
                audit = build_report_audit(report=excessive, report_body=excessive,
                                           context=context, memory=ReportMemory())
                self.assertEqual(audit.status, "failed")
                self.assertIn("source-scope-exceeded", [row.finding_id for row in audit.reviewer_findings])
                bounded = "# Review\n\nFirst [@P1], second [@P2].\n"
                accepted = build_report_audit(report=bounded, report_body=bounded,
                                              context=context, memory=ReportMemory())
                self.assertNotIn("source-scope-exceeded", [row.finding_id for row in accepted.reviewer_findings])

    def test_verified_metrics_keep_declared_rows_and_link_full_execution_evidence(self):
        metric_names = ["accuracy", "macro_f1", "forgetting", "backward_transfer"] + [
            f"accuracy_after_task_{index}_on_task_{task}"
            for index in range(11)
            for task in range(5)
        ]
        comparison_metrics = []
        sources = []
        for index, name in enumerate(metric_names):
            baseline = 0.5 + index / 1000
            candidate = baseline + 0.01
            comparison_metrics.append({
                "name": name, "baseline": baseline, "candidate": candidate,
                "delta": candidate - baseline, "interpretation": "improved",
            })
            for label, value, source_kind in (
                ("baseline", baseline, "measured"),
                ("candidate", candidate, "measured"),
                ("comparison_delta", candidate - baseline, "derived_comparison"),
            ):
                sources.append(MetricSource(
                    metric_id=f"metric:{label}:{name}", name=name, value=value,
                    artifact="attempts/experiment-001/results.json", label=label,
                    source_kind=source_kind,
                ))
        context = ReportContext(
            topic="Continual learning", report_mode="experiment",
            experiment_plan={"metrics": ["accuracy", "macro_f1"]},
            results={
                "result_schema": {"primary_metric": "accuracy", "required_metrics": ["accuracy", "macro_f1"]},
                "comparisons": [{"seed": 4, "metrics": comparison_metrics}],
            },
            metric_sources=sources,
        )

        body = _verified_experiment_evidence(context)
        appended = _append_verified_experiment_evidence((), context)[0]

        self.assertIn("`accuracy`", body)
        self.assertIn("`macro_f1`", body)
        self.assertNotIn("accuracy_after_task_", body)
        self.assertNotIn("Metric Provenance", body)
        self.assertIn("Derived delta (candidate − baseline)", body)
        self.assertIn("../experiment-001/results.json", body)
        self.assertEqual(len(context.metric_sources), 59 * 3)
        self.assertEqual(appended.metric_ids, [metric.metric_id for metric in sources])

        audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
        self.assertEqual(audit.metric_audit.status, "passed")
        self.assertEqual(audit.metric_audit.unmatched_metrics, [])

        missing_required = body.replace("`macro_f1`", "`withheld_metric`")
        audit = build_report_audit(
            report=missing_required, report_body=missing_required,
            context=context, memory=ReportMemory(),
        )
        self.assertEqual(audit.metric_audit.status, "failed")
        self.assertIn("metric:baseline:macro_f1", audit.metric_audit.unmatched_metrics)

        explicitly_required = "accuracy_after_task_0_on_task_0"
        required_context = ReportContext(
            topic="Continual learning", report_mode="experiment",
            experiment_plan={"metrics": [explicitly_required]},
            results={"paired_summary": [{
                "metric": explicitly_required, "n": 2, "baseline_mean": 0.5,
                "candidate_mean": 0.6, "delta_mean": 0.1, "delta_sample_std": 0.0,
            }]},
            metric_sources=[MetricSource(
                metric_id="required-task-metric", name=f"{explicitly_required}.candidate_mean",
                value=0.6, artifact="attempts/experiment-001/results.json", label="paired_summary:0",
            )],
        )
        self.assertIn(explicitly_required, _verified_experiment_evidence(required_context))

    def test_metric_name_and_value_on_different_lines_do_not_count_as_evidence(self):
        context = ReportContext(
            topic="Classifier", report_mode="experiment",
            metric_sources=[MetricSource(
                metric_id="candidate-accuracy", name="accuracy", value=0.81,
                artifact="candidate.json", label="candidate",
            )],
        )
        body = "# Results\n\nAccuracy was measured.\n\nAn unrelated threshold was 0.81.\n"
        audit = build_report_audit(report=body, report_body=body,
                                   context=context, memory=ReportMemory())
        self.assertIn("candidate-accuracy", audit.metric_audit.unmatched_metrics)

    def test_numeric_metric_name_is_not_itself_a_measured_value(self):
        context = ReportContext(
            topic="Code evaluation", report_mode="experiment",
            metric_sources=[MetricSource(
                metric_id="pass-at-one", name="pass@1", value=1,
                artifact="results.json", label="candidate",
            )],
        )
        body = "# Results\n\nWe recorded pass@1.\n"
        audit = build_report_audit(report=body, report_body=body,
                                   context=context, memory=ReportMemory())
        self.assertIn("pass-at-one", audit.metric_audit.unmatched_metrics)
        measured = body + "The measured pass@1 was 1.\n"
        checked = build_report_audit(report=measured, report_body=measured,
                                     context=context, memory=ReportMemory())
        self.assertIn("pass-at-one", checked.metric_audit.matched_metrics)

    def test_swapped_baseline_and_candidate_table_values_fail_audit(self):
        context = ReportContext(
            topic="Classifier", report_mode="experiment",
            results={"comparisons": [{"metrics": [{
                "name": "accuracy", "baseline": 0.61, "candidate": 0.82,
                "delta": 0.21, "interpretation": "improved",
            }]}], "result_schema": {"primary_metric": "accuracy"}},
            metric_sources=[
                MetricSource(metric_id="baseline", name="accuracy", value=0.61,
                             artifact="baseline.json", label="baseline"),
                MetricSource(metric_id="candidate", name="accuracy", value=0.82,
                             artifact="candidate.json", label="candidate"),
            ],
        )
        body = _verified_experiment_evidence(context)
        good = build_report_audit(report=body, report_body=body,
                                  context=context, memory=ReportMemory())
        self.assertEqual(good.metric_audit.status, "passed")
        altered = body.replace("| `accuracy` | 0.61 | 0.82 |", "| `accuracy` | 0.82 | 0.61 |")
        bad = build_report_audit(report=altered, report_body=altered,
                                 context=context, memory=ReportMemory())
        self.assertEqual(bad.metric_audit.status, "failed")
        self.assertTrue(any("persisted evidence" in message for message in bad.metric_audit.warnings))

        missing_table = "## Verified Experiment Metrics\n\nAccuracy was 0.61 and 0.82.\n"
        missing = build_report_audit(report=missing_table, report_body=missing_table,
                                     context=context, memory=ReportMemory())
        self.assertEqual(missing.metric_audit.status, "failed")
        self.assertTrue(any("table is missing" in message for message in missing.metric_audit.warnings))

    def test_paired_report_keeps_detailed_measurements_out_of_paper_body(self):
        summary = {
            "metric": "accuracy",
            "n": 3,
            "baseline_mean": 0.60,
            "candidate_mean": 0.65,
            "delta_mean": 0.05,
            "delta_sample_std": 0.01,
        }
        metrics = [
            MetricSource(
                metric_id=f"metric:summary:{field}",
                name=f"accuracy.{field}",
                value=value,
                artifact="outputs/experiment_set.json",
                label="paired_summary:0",
            )
            for field, value in (
                ("n", 3),
                ("baseline_mean", 0.60),
                ("candidate_mean", 0.65),
                ("delta_mean", 0.05),
                ("delta_sample_std", 0.01),
            )
        ]
        context = ReportContext(
            topic="Continual learning",
            report_mode="experiment",
            metric_sources=metrics,
            results={
                "paired_summary": [summary],
                "collection_ref": {"path": "outputs/experiment_set.json"},
            },
        )
        body = _verified_experiment_evidence(context)
        self.assertIn("Aggregate Paired Metrics", body)
        self.assertIn("0.65", body)
        self.assertNotIn("accuracy_after_task_", body)
        self.assertLess(len(body.splitlines()), 20)
        audit = build_report_audit(
            report=body,
            report_body=body,
            context=context,
            memory=ReportMemory(),
        )
        self.assertEqual(audit.metric_audit.status, "passed")
        self.assertEqual(audit.metric_audit.unmatched_metrics, [])

    def test_numeric_prose_is_not_a_measurement_or_a_proof_of_support(self):
        from simple_ar.report.schema import ReportAudit
        metrics = [MetricSource(metric_id="accuracy", name="accuracy", value=0.8, artifact="candidate.json",
                                label="candidate", condition_id="eval", unit="fraction", source_kind="measured")]
        context = ReportContext(topic="Classifier", report_mode="experiment", metric_sources=metrics)
        for prose in ("Execution was limited to 20 seconds.", "A survey discusses 37 papers.",
                      "An unsupported sentence claims a sample size of 999."):
            body = _metric_ledger(metrics) + "\n" + prose
            audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
            self.assertEqual(audit.metric_audit.status, "passed")
            self.assertEqual(audit.semantic_review_status, "semantic_unchecked")
        # Historical warnings remain readable; they are not rewritten as passing.
        old = ReportAudit.model_validate({"status": "warning", "metric_audit": {
            "status": "warning", "unmatched_numbers": ["20"],
        }})
        self.assertEqual(old.metric_audit.unmatched_numbers, ["20"])
        self.assertEqual(old.status, "warning")

    def test_experiment_sections_keep_design_sources_beyond_search_prefix(self):
        from simple_ar.report.memory import initialize_report_memory
        from simple_ar.report.schema import SourceHandle, ReportRuntimeConfig
        from simple_ar.report.templates import load_report_template_bundle

        papers = [SourceHandle(handle=f"paper:p{i}", kind="paper", paper_id=f"p{i}",
                               title="General research survey") for i in range(12)]
        papers[-1].title = "Original replay method"
        context = ReportContext(
            topic="Replay", report_mode="experiment", max_section_sources=3,
            experiment_plan={"motivation_refs": ["p11#claim-001"]},
            source_handles=[SourceHandle(handle="execution", kind="experiment"), *papers],
        )
        template = load_report_template_bundle(report_mode="experiment", config=ReportRuntimeConfig())
        memory = initialize_report_memory(context=context, template=template)
        for section in memory.section_plan:
            self.assertIn("paper:p11", section.evidence_handles)
            self.assertIn("execution", section.evidence_handles)
            self.assertLessEqual(len(section.evidence_handles), 3)

    def test_writer_tool_reads_frozen_patch_with_truncation_and_provenance(self):
        from simple_ar.report.tool_gateway import ReportToolGateway
        from simple_ar.report.schema import ReportToolCall

        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            patch = "--- method.py\n+++ method.py\n+use_phrase_features = True\n" + "x" * 12000
            store.write_text("attempts/implement-1/code_task/patch.diff", patch)
            store.write_text("attempts/implement-1/code_task/source.txt", "Read the source entry and its caller; unrelated files were not read.")
            store.write_text("attempts/implement-1/code_task/validation.json", '{"status":"passed","scope":"static syntax check"}')
            store.write_text("attempts/implement-1/code_task/execution_report.json", '{"status":"passed","returncode":0,"scope":"runtime smoke check"}')
            ref = store.write_json("attempts/implement-1/implementation.json", {
                "status": "validated", "asset_integrity": {"status": "observed_unchanged"},
                "method_validation": {
                    "status": "未检查",
                    "reason": "No candidate-specific behavior check was recorded.",
                    "planned_checks": ["Observe the changed behavior on the supplied fixture."],
                },
                "artifact_refs": {"patch": {"path": "code_task/patch.diff"},
                    "source_context": {"path": "code_task/source.txt"},
                    "validation": {"path": "code_task/validation.json"},
                    "validation_report": {"path": "code_task/execution_report.json"}},
            })
            context = ReportContext(
                topic="Classifier",
                report_mode="experiment",
                experiment_plan={"contract_id": "fixture-v1", "metric": "accuracy"},
                results={"metrics": {"accuracy": 0.8}},
            )
            attach_implementation_evidence(context, store, ref)
            result = ReportToolGateway(context).call(ReportToolCall(tool_name="get_code_task_result", arguments={}))
            implementation = result.content["results"]["implementation"]
            self.assertEqual(result.content["results"]["metrics"], {"accuracy": 0.8})
            evidence = implementation["evidence"]["patch"]
            self.assertEqual(evidence["text"], patch[:12000])
            self.assertTrue(evidence["truncated"])
            self.assertEqual(evidence["artifact"], "attempts/implement-1/code_task/patch.diff")
            self.assertNotIn("review", implementation["evidence"])
            self.assertEqual(implementation["method_validation"]["status"], "未检查")
            # Tool accessibility alone does not prove the agents received it.
            import json
            from simple_ar.report.agent import run_report_agent
            from simple_ar.report.schema import ReportRuntimeConfig, ReportSectionPlan
            from simple_ar.report.templates import load_report_template_bundle
            received = {}

            class Client:
                fail_first_draft = False

                def ask_json(self, system, user, *, label="", **kwargs):
                    payload = json.loads(user[user.index("{"):])
                    if "reviewer" in label:
                        received["reviewer"] = payload["verified_execution_results"]["implementation"]
                        received["reviewer_plan"] = payload["experiment_plan"]
                        return {"section_id": "method", "verdict": "pass", "findings": []}
                    if self.fail_first_draft and not label.endswith("-retry"):
                        from simple_ar.integrations.llm import LLMResponseError
                        raise LLMResponseError("invalid section JSON")
                    evidence_context = payload if label.endswith("-retry") else payload["global_research_context"]
                    received["writer"] = evidence_context["verified_execution_results"]["implementation"]
                    received["writer_plan"] = evidence_context["experiment_plan"]
                    return {"section_id": "method", "heading": "Method", "draft_markdown": "The recorded patch enables phrase features; the remainder is truncated."}

            config = ReportRuntimeConfig(max_review_iterations=0)
            memory = ReportMemory(section_plan=[ReportSectionPlan(section_id="method", heading="Method", goal="Describe actual changes")])
            run_report_agent(client=Client(), context=context, memory=memory, config=config,
                             template=load_report_template_bundle(report_mode="experiment", config=config),
                             gateway=ReportToolGateway(context))
            self.assertEqual(
                set(received),
                {"writer", "reviewer", "writer_plan", "reviewer_plan"},
            )
            self.assertEqual(received["writer_plan"], context.experiment_plan)
            self.assertEqual(received["reviewer_plan"], context.experiment_plan)
            for role in ("writer", "reviewer"):
                view = received[role]
                self.assertEqual(view["evidence"]["patch"], evidence)
                for name in ("source_context", "validation", "validation_report"):
                    self.assertEqual(view["evidence"][name], implementation["evidence"][name])
                self.assertEqual(view["asset_integrity"], implementation["asset_integrity"])
                self.assertEqual(view["method_validation"]["status"], "未检查")
            received.clear()
            retry_client = Client()
            retry_client.fail_first_draft = True
            run_report_agent(client=retry_client, context=context, memory=memory, config=config,
                             template=load_report_template_bundle(report_mode="experiment", config=config),
                             gateway=ReportToolGateway(context))
            self.assertEqual(
                set(received),
                {"writer", "reviewer", "writer_plan", "reviewer_plan"},
            )
            self.assertEqual(received["writer_plan"], context.experiment_plan)
            self.assertEqual(received["reviewer_plan"], context.experiment_plan)
            for role in ("writer", "reviewer"):
                self.assertEqual(received[role]["evidence"]["patch"], evidence)

    def test_implementation_report_keeps_initial_patch_and_repair_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            initial_patch = "--- utils/buffer.py\n+++ utils/buffer.py\n+gradient admission\n"
            repair_patch = "--- models/er.py\n+++ models/er.py\n+enable grad\n"
            store.write_text("attempts/implement-1/code_task/patch.diff", initial_patch)
            initial_ref = store.write_json("attempts/implement-1/implementation.json", {
                "status": "validated",
                "asset_integrity": {"status": "observed_unchanged"},
                "artifact_refs": {"patch": {"path": "code_task/patch.diff"}},
            })
            store.write_text("attempts/implement-2/code_task/patch.diff", repair_patch)
            repair_ref = store.write_json("attempts/implement-2/implementation.json", {
                "status": "validated",
                "failure_ref": {"path": "attempts/experiment-1/results.json"},
                "asset_integrity": {"status": "observed_unchanged"},
                "artifact_refs": {"patch": {"path": "code_task/patch.diff"}},
            })
            context = ReportContext(topic="Replay", report_mode="experiment")

            attach_implementation_evidence(
                context, store, repair_ref, lineage_refs=(initial_ref, repair_ref,
                    ArtifactRef(path="attempts/future-unmeasured/implementation.json"))
            )

            implementation = context.results["implementation"]
            self.assertEqual(implementation["method_validation"]["status"], "未检查")
            self.assertEqual(
                [item["artifact"] for item in implementation["lineage"]],
                ["attempts/implement-1/implementation.json", "attempts/implement-2/implementation.json"],
            )
            patches = implementation["evidence"]["patches"]
            self.assertEqual(len(patches), 2)
            self.assertEqual(patches[0]["text"], initial_patch)
            self.assertEqual(patches[1]["text"], repair_patch)

    def test_swapped_values_or_conditions_fail_even_when_all_numbers_are_present(self):
        metrics = [MetricSource(metric_id=f"metric:{label}:accuracy", name="accuracy", value=value,
                               artifact=f"{label}.json", label=label, condition_id=label, unit="fraction", source_kind="measured")
                   for label, value in (("baseline", 0.6), ("candidate", 0.8))]
        context = ReportContext(topic="Calibration", report_mode="experiment", metric_sources=metrics)
        valid = _metric_ledger(metrics)
        variants = {
            "valid": valid,
            "swapped_values": valid.replace("0.6", "TEMP").replace("0.8", "0.6").replace("TEMP", "0.8"),
            "wrong_unit": valid.replace("fraction", "percent"),
            "wrong_condition": valid.replace("fraction | baseline", "fraction | candidate"),
            "unknown_metric": valid.replace("`accuracy`", "`f1`"),
        }
        for name, body in variants.items():
            with self.subTest(name=name):
                audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
                self.assertEqual(audit.metric_audit.status, "passed" if name == "valid" else "failed")
                self.assertEqual(audit.semantic_review_status, "semantic_unchecked")
        # Merely mentioning both numbers in prose proves neither attribution nor improvement.
        body = "Baseline accuracy 0.8 exceeds candidate accuracy 0.6."
        audit = build_report_audit(report=body, report_body=body, context=context, memory=ReportMemory())
        self.assertEqual(audit.semantic_review_status, "semantic_unchecked")

    def test_literature_report_cannot_invent_a_measurement_ledger(self):
        body = "| Source | Metric | Value | Unit | Condition | Origin |\n| --- | --- | --- | --- | --- | --- |\n| candidate | `accuracy` | 0.8 | fraction | candidate | measured |"
        audit = build_report_audit(report=body, report_body=body,
                                  context=ReportContext(topic="Review", report_mode="research_only"), memory=ReportMemory())
        self.assertEqual(audit.metric_audit.status, "failed")
