from __future__ import annotations

import tempfile
import unittest

from tests.legacy_session_fixture import historical_session, session_files
from pathlib import Path

from simple_ar.app.research_report import build_research_session_report_inputs
from simple_ar.report.projection import metric_sources_from_execution
from simple_ar.report.schema import (
    ReportContext,
    ReportMemory,
)


class ResearchReportApplicationTests(unittest.TestCase):
    def test_execution_projection_keeps_all_measurements_without_an_implementation(self):
        from simple_ar.core import ArtifactStore
        from simple_ar.report.projection import build_implementation_report_inputs

        with tempfile.TemporaryDirectory() as folder:
            store = ArtifactStore(Path(folder))
            refs = [store.write_json(f'attempts/run-{i}/results.json', {
                'status': status, 'metrics': {'accuracy': value},
                'experiment_contract': {'comparison_conditions': {'seed': i}},
                'measurement': {'condition_id': f'candidate:{i}', 'source_kind': 'measured'},
            }) for i, (status, value) in enumerate([('passed', 0.5), ('passed', 0.7), ('failed', 0.99)])]
            context, memory = build_implementation_report_inputs(topic='Observed data', store=store,
                implementation_ref=None, measurement_refs=refs, analysis_artifacts={})
            self.assertNotIn('implementation', context.results)
            self.assertNotIn('metrics', context.results)
            self.assertEqual([row.value for row in context.metric_sources], [0.5, 0.7])
            self.assertEqual([row.artifact for row in context.metric_sources], [ref.path for ref in refs[:2]])
            self.assertEqual(len({row.metric_id for row in context.metric_sources}), 2)
            self.assertEqual([row['metrics'] for row in context.results['measurement_history']],
                [{'accuracy': 0.5}, {'accuracy': 0.7}, {}])
            self.assertEqual(len(context.experiment_plan['measurement_protocols']), 3)
            self.assertEqual(memory.metric_sources, context.metric_sources)

    def test_execution_projection_uses_normalized_paired_and_skip_artifacts(self):
        from simple_ar.core import ArtifactStore
        from simple_ar.report.projection import build_implementation_report_inputs, ReportProjectionError

        for skip in (False, True):
            with self.subTest(skip=skip), tempfile.TemporaryDirectory() as folder:
                store = ArtifactStore(Path(folder))
                implementation = store.write_json('attempts/code/implementation.json', {'status': 'completed'})
                refs, pairs = [], []
                for seed in (3, 7):
                    pair = {'seed': seed, 'baseline': None, 'candidate': None}
                    for role in (('candidate',) if skip else ('baseline', 'candidate')):
                        ref = store.write_json(f'attempts/{role}-{seed}/results.json', {
                            'status': 'passed', 'metrics': {'accuracy': 0.7 if role == 'candidate' else 0.5},
                            **({'implementation_ref': implementation.to_dict()} if role == 'candidate' else {
                                'preparation': {'source_ref': {'path': 'attempts/prepared/execution.json'}}}),
                            'measurement': {'seed': seed, 'condition_id': f'{role}:{seed}', 'source_kind': 'measured'},
                            'experiment_contract': {'comparison_conditions': {'seed': seed}},
                        })
                        refs.append(ref)
                        pair[role] = ref.to_dict()
                    pairs.append(pair)
                collection = store.write_json('attempts/analyze/explicit-set.json', {'pairs': pairs})
                summary = {'group_id': 0, 'metric': 'accuracy', 'n': 2, 'seeds': [3, 7],
                    'candidate_mean': 0.7, 'candidate_sample_std': 0.0, 'baseline_mean': 0.5,
                    'delta_mean': 0.2, 'delta_sample_std': 0.0}
                payload = {'collection_ref': collection.to_dict(), 'status': 'passed',
                    'comparisons': [] if skip else [{'seed': pair['seed'], 'metrics': [
                        {'name': 'accuracy', 'baseline': 0.5, 'candidate': 0.7, 'delta': 0.2}]} for pair in pairs],
                    'paired_summary': [] if skip else [summary],
                    **({'baseline_policy': 'skip', 'condition_summary': [summary]} if skip else {}),
                    'limitations': ['Descriptive observations only.']}
                paired = store.write_json('attempts/analyze/explicit-paired.json', payload)
                analysis = store.write_json('attempts/analyze/explicit-analysis.json', {
                    'execution_ref': collection.to_dict(), 'analysis': {'status': 'passed',
                        'readme_markdown': 'Observed only.', 'audit': {'limitations': ['No significance test.']}}})
                artifacts = {'analysis_result': analysis.path, 'experiment_set': collection.path,
                    'experiment_set_analysis': paired.path}
                context, memory = build_implementation_report_inputs(topic='Conditions', store=store,
                    implementation_ref=implementation, measurement_refs=refs, analysis_artifacts=artifacts)
                self.assertEqual(len(context.results['measurement_history']), len(refs))
                self.assertEqual(len(context.experiment_plan['paired_protocols']), len(refs))
                derived = [row for row in context.metric_sources if row.source_kind.startswith('derived_')]
                self.assertTrue(derived)
                self.assertEqual({row.artifact for row in derived}, {paired.path})
                self.assertEqual(context.results['accepted_analyses'][0]['artifact'], analysis.path)
                self.assertIn('No significance test.', memory.limitations)
                if skip:
                    self.assertEqual(context.results['baseline_policy'], 'skip')
                    self.assertFalse(any('delta' in row.name for row in derived))
                else:
                    self.assertTrue(any(row.name == 'accuracy.delta_mean' for row in derived))
                    self.assertTrue(any(row.metadata.get('preparation', {}).get('source_ref', {}).get('path')
                        == 'attempts/prepared/execution.json' for row in context.source_handles))
                with self.assertRaises(ReportProjectionError):
                    build_implementation_report_inputs(topic='Subset', store=store,
                        implementation_ref=implementation, measurement_refs=refs[:-1], analysis_artifacts=artifacts)

    def test_execution_projection_joins_single_analysis_and_comparison_to_measurements(self):
        from simple_ar.core import ArtifactStore
        from simple_ar.report.projection import build_implementation_report_inputs, ReportProjectionError

        with tempfile.TemporaryDirectory() as folder:
            store = ArtifactStore(Path(folder))
            refs = [store.write_json(f'attempts/run-{i}/results.json', {'status': 'passed',
                'metrics': {'value': i}}) for i in (1, 2)]
            analysis = store.write_json('attempts/analyze/analysis.json', {
                'execution_ref': refs[0].to_dict(), 'analysis': {'readme_markdown': 'Observed.', 'status': 'passed'}})
            artifacts = {'analysis_result': analysis.path}
            context, _ = build_implementation_report_inputs(topic='Single', store=store,
                measurement_refs=refs[:1], analysis_artifacts=artifacts)
            self.assertEqual(len(context.results['accepted_analyses']), 1)
            # An old accepted subset cannot stand in for a new full selection.
            with self.assertRaises(ReportProjectionError):
                build_implementation_report_inputs(topic='Changed selection', store=store,
                    measurement_refs=refs, analysis_artifacts=artifacts)
            with self.assertRaises(ReportProjectionError):
                build_implementation_report_inputs(topic='Empty selection', store=store,
                    measurement_refs=[], analysis_artifacts=artifacts)
            legacy, _ = build_implementation_report_inputs(topic='No guessed analysis', store=store,
                measurement_refs=refs[:1])
            self.assertNotIn('accepted_analyses', legacy.results)
            comparison = store.write_json('attempts/analyze/explicit-comparison.json', {
                'baseline_ref': refs[1].to_dict(), 'candidate_ref': refs[0].to_dict(),
                'comparability': 'declared_match', 'metrics': [
                    {'name': 'value', 'baseline': 2, 'candidate': 1, 'delta': -1}]})
            paired, _ = build_implementation_report_inputs(topic='Explicit comparison', store=store,
                measurement_refs=refs, analysis_artifacts={**artifacts, 'experiment_comparison': comparison.path})
            self.assertEqual([(row.value, row.artifact) for row in paired.metric_sources
                if row.source_kind == 'derived_comparison'], [(-1, comparison.path)])

    def test_execution_projection_retains_implementation_lineage_checks(self):
        from simple_ar.core import ArtifactStore
        from simple_ar.report.projection import build_implementation_report_inputs, ReportProjectionError

        with tempfile.TemporaryDirectory() as folder:
            store = ArtifactStore(Path(folder))
            implementation = store.write_json('attempts/code/implementation.json', {'status': 'completed'})
            measurement = store.write_json('attempts/run/results.json', {'status': 'passed',
                'metrics': {'value': 1}, 'implementation_ref': {'path': 'attempts/other/implementation.json'}})
            with self.assertRaises(ReportProjectionError):
                build_implementation_report_inputs(topic='Wrong legacy revision', store=store,
                    implementation_ref=implementation, measurement_refs=[measurement])
            valid = store.write_json('attempts/valid/results.json', {'status': 'passed',
                'metrics': {'value': 1}, 'implementation_ref': implementation.to_dict()})
            legacy, _ = build_implementation_report_inputs(topic='Historical composition', store=store,
                implementation_ref=implementation, measurement_refs=[valid])
            self.assertEqual(legacy.report_mode, 'supplied_materials')
            self.assertNotIn('accepted_analyses', legacy.results)
            self.assertEqual(legacy.metric_sources[0].metric_id, 'metric:candidate:value')

    def test_delivery_uses_goal_not_process_success_and_preserves_explicit_template(self):
        from simple_ar.report.templates import resolve_experiment_delivery, load_report_template_bundle
        from simple_ar.report.schema import ReportRuntimeConfig
        config = ReportRuntimeConfig()
        for task_type, status, execution_status, disposition, expected in (
            ("improvement", "not_met", "passed", "deliver_observed_result", "analysis_report"),
            ("improvement", "inconclusive", "passed", "deliver_observed_result", "analysis_report"),
            ("improvement", "met", "passed", "deliver_observed_result", "experiment"),
            ("improvement", "met", "failed", "deliver_observed_result", "analysis_report"),
            ("improvement", "met", "passed", "deliver_with_limits", "analysis_report"),
            ("reproduction", "not_met", "metric_below_target", "deliver_with_limits", "reproduction"),
        ):
            with self.subTest(task_type=task_type, status=status, execution=execution_status):
                analysis = {"status": execution_status, "goal_assessment": {
                    "task_type": task_type, "status": status, "evidence_refs": ["accuracy"]}}
                selected, delivery = resolve_experiment_delivery(config, analysis, {"disposition": disposition})
                self.assertEqual(selected.template, expected)
                self.assertEqual(delivery["goal_assessment"], analysis["goal_assessment"])
                self.assertEqual(load_report_template_bundle(report_mode="experiment", config=selected).name, expected)
        self.assertEqual(config.template, "auto")
        selected, _ = resolve_experiment_delivery(ReportRuntimeConfig(template="experiment"), {}, {})
        self.assertEqual(selected.template, "experiment")
        selected, _ = resolve_experiment_delivery(config, {"status": "passed"}, {})
        self.assertEqual(selected.template, "analysis_report")
        selected, _ = resolve_experiment_delivery(config, {"goal_assessment": {
            "status": "not_met", "requested_delivery": "paper"}}, {})
        self.assertEqual(selected.template, "experiment")

    def test_paired_singleton_summary_and_failed_measurement_keep_scientific_limits(self):
        from simple_ar.report.projection import attach_paired_report_measurements
        from simple_ar.core.capabilities import ArtifactRef

        context, _ = attach_paired_report_measurements(
            ReportContext(topic="Classification", report_mode="experiment"), ReportMemory(), [],
            comparisons=[], comparison_ref=ArtifactRef("paired_analysis.json"), summaries=[{
                "group_id": 0, "metric": "accuracy", "unit": "fraction", "n": 1, "seeds": [7],
                "baseline_mean": 0.5, "candidate_mean": 0.6, "delta_mean": 0.1, "delta_sample_std": None}])
        self.assertEqual(len(context.metric_sources), 4)
        self.assertFalse(any(m.name.endswith("std") for m in context.metric_sources))
        self.assertEqual({m.unit for m in context.metric_sources}, {"fraction", "count"})
        self.assertTrue(all(m.source_kind == "derived_summary" and m.artifact == "paired_analysis.json"
                            for m in context.metric_sources))

        baseline = {"status": "passed", "metrics": {"accuracy": 0.75}}
        failed = {"status": "failed", "metrics": {"accuracy": 0.99}}
        context, memory = attach_paired_report_measurements(ReportContext(topic="Classification", report_mode="experiment"), ReportMemory(), [
            (0, "baseline", ArtifactRef("baseline.json"), baseline),
            (0, "candidate", ArtifactRef("failed.json"), failed)],
            comparisons=[{"seed": 0, "metrics": [{"name": "accuracy", "delta": 0.24}]}],
            comparison_ref=ArtifactRef("paired_analysis.json"))
        self.assertEqual([m.value for m in context.metric_sources], [0.75])
        self.assertEqual(context.metric_sources[0].artifact, "baseline.json")
        self.assertTrue(any(h.artifact == "failed.json" for h in context.source_handles))
        self.assertTrue(any("did not pass" in message for message in memory.limitations))

    def test_report_tools_bind_connector_and_local_notes_to_document_identity(self):
        from simple_ar.report.projection import attach_report_read_evidence, build_material_report_inputs
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.research.contracts import DocumentRecord, TextChunk
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.evidence.reader import ReadResult
        from simple_ar.report.schema import SourceHandle, ReportToolCall
        from simple_ar.report.tool_gateway import ReportToolGateway

        # The same report consumer accepts connector identities and plain local
        # documents; matching titles and unrelated chunks must never join them.
        for local in (False, True):
            with self.subTest(local=local):
                records = [DocumentRecord(document_id=f"doc-{key}",
                    source="local_files" if local else "fixture", title="Same title",
                    abstract=f"Abstract for {key}", extraction_status="parsed" if local else "metadata_only",
                    metadata={} if local else {"paper_id": key}) for key in ("p1", "p2")]
                ids = [row.document_id if local else key for key, row in zip(("p1", "p2"), records)]
                context = ReportContext(topic="Calibration", report_mode="research_only", source_handles=[
                    SourceHandle(handle=f"paper:{key}", kind="paper", paper_id=key, title="Same title", citation_key=f"P{index}")
                    for index, key in enumerate(ids, 1)],
                    citation_key_map={f"P{index}": key for index, key in enumerate(ids, 1)})
                documents = DocumentBundle(records=records + [DocumentRecord(document_id="doc-p1#linked",
                    source="supporting_material", title="Author code page", url="https://example.test/code",
                    metadata={"kind": "supporting_material", "parent_document_id": "doc-p1"})],
                    fulltext_manifest={}, fulltext_extraction={}, sections=[],
                    chunks=[TextChunk(f"{row.document_id}-span", row.document_id, f"Reported evidence for {row.document_id}")
                            for row in records] + [TextChunk("unrelated", "different", "Do not join this source")])
                notes = tuple({"paper_id": row.document_id, "method": f"Method for {row.document_id}",
                    "datasets": ["Public observations"], "metrics": ["Prediction error"],
                    "key_claims": [f"Reported evidence for {row.document_id}"], "limitations": ["One setting"], "confidence": "medium",
                    "evidence_refs": [f"{row.document_id}-span"], "reading_coverage": {
                        "available_chunks": 30, "shown_chunk_ids": [f"{row.document_id}-span"],
                        "semantic_verification": "not_performed"}} for row in reversed(records))
                projected, memory = attach_report_read_evidence(context, ReportMemory(), documents=documents,
                    read=ReadResult(status="completed", bundle=documents, paper_notes=notes,
                        question_assessments=({"question_id": "q-intervals", "status": "missing",
                            "reason": "Only point-prediction candidates"},)),
                    read_ref=ArtifactRef("read/result.json"), documents_ref=ArtifactRef("read/documents.json"))
                material = next(h for h in projected.source_handles if h.handle == "material:doc-p1#linked")
                self.assertEqual((material.kind, material.artifact), ("material", "read/documents.json"))
                self.assertEqual(material.metadata["parent_document_id"], "doc-p1")
                self.assertEqual(material.metadata["evidence_role"], "linked_material_not_paper_or_measured_result")
                gateway = ReportToolGateway(projected)
                result = gateway.call(ReportToolCall(tool_name="get_paper_brief",
                    arguments={"citation_key": material.citation_key}))
                self.assertEqual(result.source_handles, [material.handle])
                for index, (key, row) in enumerate(zip(ids, records), 1):
                    result = gateway.call(ReportToolCall(tool_name="get_paper_brief",
                        arguments={"citation_key": f"P{index}"} if local else {"paper_id": key}))
                    evidence = result.content["handles"][0]
                    metadata = evidence["metadata"]
                    self.assertEqual(evidence["summary"], row.abstract)
                    note = next(n for n in notes if n["paper_id"] == row.document_id)
                    self.assertEqual({key: metadata["reading_notes"][key] for key in note if key != "paper_id"},
                                     {key: value for key, value in note.items() if key != "paper_id"})
                    self.assertEqual(metadata["reading_notes_kind"], "model_interpretation_not_source_text")
                    self.assertEqual(metadata["reading_artifact"], "read/result.json")
                    self.assertEqual(metadata["extraction_status"], row.extraction_status)
                    self.assertEqual([span["text"] for span in metadata["evidence_passages"]],
                                     [f"Reported evidence for {row.document_id}"])
                self.assertEqual(memory.source_handles, projected.source_handles)
                self.assertEqual(context.source_handles[0].summary, "")
                self.assertIn("1 metadata/abstract-only" if local else "3 metadata/abstract-only", projected.evidence_summary)
                self.assertIn("Only point-prediction candidates", projected.evidence_summary)
                self.assertIn("not verified answers", projected.evidence_summary)
                self.assertTrue(any("Do not describe metadata/abstract-only sources" in line for line in memory.limitations))
                if local:
                    continue
                # Compose the same identities without recreating intake assets;
                # drafts and derived analysis never become bibliographic papers.
                retained = DocumentBundle(records=records + [
                    DocumentRecord(document_id="previous", source="fixture", title="Same title",
                        metadata={"kind": "prior_draft", "paper_id": "previous-paper"}),
                    DocumentRecord(document_id="derived", source="local_analysis", title="Same title",
                        metadata={"paper_id": "not-a-measured-paper"})],
                    fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=documents.chunks[:-1])
                kwargs = dict(topic="Use original identities", documents=retained,
                    documents_ref=ArtifactRef("sources/selected.json"), assets=[], selected_evidence=True)
                native, native_memory = build_material_report_inputs(**kwargs)
                article, article_memory = build_material_report_inputs(**kwargs, include_memory=False)
                self.assertEqual([row["id"] for row in native.papers], ids)
                self.assertEqual(set(native.citation_key_map.values()), set(ids))
                self.assertEqual({row.metadata["document_id"] for row in native.source_handles if row.kind == "paper"},
                                 {"doc-p1", "doc-p2"})
                self.assertEqual(native_memory.source_handles, native.source_handles)
                self.assertTrue(any("not independently verified" in row for row in native_memory.limitations))
                self.assertIsNone(article_memory)
                self.assertEqual(article.model_dump(mode="json"), native.model_dump(mode="json"))

    def test_projection_and_synthesis_tool_keep_evidence_prose_not_only_idea_scaffold(self):
        from simple_ar.report.projection import _synthesis_markdown
        from simple_ar.research.synthesis import SynthesisResult
        from simple_ar.report.schema import ReportToolCall
        from simple_ar.report.tool_gateway import ReportToolGateway

        text = "## Reported evidence\nThe source reports a controlled measurement, not a new experiment."
        synthesis = SynthesisResult(status="ready", gap_summary="Sparse local heuristic", ideas=(),
                                    novelty_checks=(), synthesis_markdown=text)
        self.assertEqual(_synthesis_markdown(synthesis), text)
        context = ReportContext(topic="Review", report_mode="research_only", synthesis_markdown=text,
                                evidence_summary="One document ingested.")
        result = ReportToolGateway(context).call(ReportToolCall(tool_name="get_synthesis_brief"))
        self.assertEqual(result.content["text"], text)
        legacy = SynthesisResult(status="ready", gap_summary="Historical summary", ideas=(), novelty_checks=())
        self.assertIn("Historical summary", _synthesis_markdown(legacy))

    def test_metric_projection_keeps_each_conditions_own_units_and_direction(self):
        candidate = {"metrics": {"accuracy": 0.8},
                     "result_schema": {"primary_metric": "accuracy", "direction": "higher"},
                     "measurement": {"measurement_id": "candidate-2", "condition_id": "seed-2",
                                     "protocol_id": "protocol", "protocol_revision": 2,
                                     "protocol_fingerprint": "candidate-fingerprint", "source_kind": "measured"},
                     "experiment_contract": {"metric_specs": [{"name": "accuracy", "unit": "fraction"}]},
                     "baseline": {"metrics": {"accuracy": 80},
                                  "result_schema": {"metric_directions": {"accuracy": "lower"}},
                                  "measurement": {"measurement_id": "baseline-1", "condition_id": "seed-1", "source_kind": "measured"},
                                  "experiment_contract": {"metric_specs": [{"name": "accuracy", "unit": "percent"}]}}}
        rows = metric_sources_from_execution(candidate, artifact="result.json")
        by_label = {row.label: row for row in rows}
        self.assertEqual((by_label["candidate"].unit, by_label["candidate"].direction), ("fraction", "higher"))
        self.assertEqual((by_label["baseline"].unit, by_label["baseline"].direction), ("percent", "lower"))
        self.assertEqual(by_label["candidate"].measurement_id, "candidate-2")
        self.assertEqual(by_label["baseline"].measurement_id, "baseline-1")
        candidate["comparisons"] = [{"metrics": [{"name": "accuracy", "delta": -79.2}]}]
        separate = metric_sources_from_execution(candidate, artifact="result.json",
            metric_artifacts={"baseline": "baseline.json", "comparison_delta": "comparison.json"})
        self.assertEqual({row.label: row.artifact for row in separate},
            {"candidate": "result.json", "baseline": "baseline.json", "comparison_delta": "comparison.json"})
        self.assertEqual([row.value for row in separate], [0.8, 80, -79.2])
        embedded = metric_sources_from_execution(candidate, artifact="embedded.json")
        self.assertTrue(all(row.artifact == "embedded.json" for row in embedded))
        legacy = metric_sources_from_execution({"metrics": {"accuracy": 0.5}}, artifact="old.json")[0]
        self.assertEqual(legacy.source_kind, "legacy_unverified")
        self.assertIsNone(legacy.measurement_id)
        self.assertEqual(legacy.unit, "")


    def test_report_projection_prefers_the_measured_execution_contract(self) -> None:
        from dataclasses import replace

        for shape, aggregate, measured in (("historical-separate", False, False),
                                           ("historical-aggregate", True, False),
                                           ("measured-priority", False, True)):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                session = historical_session(root, aggregate_brief=aggregate)
                execution_contract = {
                    "contract_id": "measured-protocol",
                    "dataset_refs": [{"asset_id": "dataset-v3", "revision": "3"}],
                    "split_spec": {"evaluation": "held-out-test"},
                    "comparison_conditions": {"seed": 19, "epochs": 2},
                }
                if measured:
                    session = replace(session,
                        execution={**session.execution, "experiment_contract": execution_contract})
                before = session_files(root)
                context, memory = build_research_session_report_inputs(session)
                self.assertEqual(context.topic, "reliable agents")
                self.assertEqual(context.results["status"], "passed")
                self.assertTrue(any(item.name == "accuracy" for item in context.metric_sources))
                self.assertTrue(any(item.kind == "analysis" for item in memory.source_handles))
                self.assertEqual(memory.source_handles, context.source_handles)
                self.assertEqual(session_files(root), before)
                if measured:
                    self.assertEqual(context.experiment_plan["contract_id"], "measured-protocol")
                    self.assertEqual(context.experiment_plan["dataset_refs"], execution_contract["dataset_refs"])
                    self.assertEqual(context.experiment_plan["comparison_conditions"], {"seed": 19, "epochs": 2})
                    self.assertEqual(context.experiment_plan["metrics"], [])
                    self.assertEqual(context.experiment_plan["dataset"], "unknown")
                    self.assertEqual(context.hypothesis_markdown, "Validation improves reliability.")

if __name__ == "__main__":
    unittest.main()
