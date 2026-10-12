from __future__ import annotations

import tempfile
import unittest

from tests.legacy_session_fixture import historical_session
from pathlib import Path

from simple_ar.app.research_report import build_research_session_report_inputs
from simple_ar.report.projection import metric_sources_from_execution
from simple_ar.report.schema import (
    ReportContext,
    ReportMemory,
)


class ResearchReportApplicationTests(unittest.TestCase):
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

    def test_paired_metrics_preserve_measurement_and_comparison_origins(self):
        from simple_ar.report.projection import attach_paired_report_measurements
        from simple_ar.core.capabilities import ArtifactRef

        context, memory = attach_paired_report_measurements(
            ReportContext(topic="Classification", report_mode="experiment"), ReportMemory(), [
                (7, "baseline", ArtifactRef("baseline.json"),
                 {"status": "passed", "metrics": {"accuracy": 0.7}}),
                (7, "candidate", ArtifactRef("candidate.json"),
                 {"status": "passed", "metrics": {"accuracy": 0.8}}),
            ], comparisons=[{"seed": 7, "metrics": [{"name": "accuracy", "delta": 0.1}]}],
            comparison_ref=ArtifactRef("comparison.json"),
        )
        self.assertEqual(
            [(m.label, m.value, m.artifact) for m in context.metric_sources],
            [("baseline:seed=7", 0.7, "baseline.json"),
             ("candidate:seed=7", 0.8, "candidate.json"),
             ("comparison_delta:seed=7", 0.1, "comparison.json")],
        )
        self.assertEqual(memory.metric_sources, context.metric_sources)
        self.assertEqual({h.artifact for h in memory.source_handles},
                         {"baseline.json", "candidate.json"})

    def test_summary_sources_keep_units_and_do_not_invent_singleton_std(self):
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

    def test_paired_report_excludes_failed_measurement_and_its_delta(self):
        from simple_ar.report.projection import attach_paired_report_measurements
        from simple_ar.core.capabilities import ArtifactRef
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

    def test_report_tools_receive_notes_by_document_identity_not_title(self):
        from simple_ar.report.projection import attach_report_read_evidence
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.research.contracts import DocumentRecord
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.evidence.reader import ReadResult
        from simple_ar.report.schema import SourceHandle, ReportToolCall
        from simple_ar.report.tool_gateway import ReportToolGateway

        handles = [SourceHandle(handle=f"paper:{key}", kind="paper", paper_id=key, title="Same title")
                   for key in ("p1", "p2")]
        context = ReportContext(topic="Calibration", report_mode="research_only", source_handles=handles)
        documents = DocumentBundle(records=[DocumentRecord(document_id=f"doc-{key}", source="fixture",
            title="Same title", abstract=f"Abstract for {key}", metadata={"paper_id": key}) for key in ("p1", "p2")],
            fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=[])
        read = ReadResult(status="completed", bundle=documents, paper_notes=tuple(
            {"paper_id": f"doc-{key}", "method": f"Method for {key}", "limitations": ["Abstract only"],
             "evidence_refs": [f"doc-{key}#abstract"]} for key in ("p2", "p1")),
            question_assessments=({"question_id": "q-intervals", "status": "missing",
                                   "reason": "Only point-prediction candidates"},))
        documents.records.append(DocumentRecord(document_id="doc-p1#linked", source="supporting_material",
            title="Author code page", url="https://example.test/code", metadata={
                "kind": "supporting_material", "parent_document_id": "doc-p1"}))
        projected, memory = attach_report_read_evidence(context, ReportMemory(), documents=documents, read=read,
            read_ref=ArtifactRef("read/result.json"), documents_ref=ArtifactRef("read/documents.json"))
        material = next(h for h in projected.source_handles if h.handle == "material:doc-p1#linked")
        self.assertEqual(material.kind, "material")
        self.assertEqual(material.artifact, "read/documents.json")
        self.assertEqual(material.metadata["parent_document_id"], "doc-p1")
        self.assertEqual(material.metadata["evidence_role"], "linked_material_not_paper_or_measured_result")
        citation_id = projected.citation_key_map[material.citation_key]
        entry = next(row for row in projected.papers if row["id"] == citation_id)
        self.assertEqual(entry["url"], "https://example.test/code")
        self.assertEqual(entry["authors"], [])
        self.assertEqual(entry["source"], "supporting_material")
        self.assertNotIn("#", citation_id)
        from simple_ar.report.citations import references_markdown
        from simple_ar.literature.models import Paper
        self.assertIn("https://example.test/code", references_markdown([Paper.from_row(entry)]))
        repeated, _ = attach_report_read_evidence(projected, memory, documents=documents, read=read,
            read_ref=ArtifactRef("read/result.json"), documents_ref=ArtifactRef("read/documents.json"))
        self.assertEqual(repeated.citation_key_map, projected.citation_key_map)
        self.assertEqual(sum(row["id"] == citation_id for row in repeated.papers), 1)
        result = ReportToolGateway(projected).call(ReportToolCall(tool_name="get_paper_brief",
            arguments={"citation_key": material.citation_key}))
        self.assertEqual(result.source_handles, [material.handle])
        from simple_ar.report.projection import build_material_report_inputs
        from simple_ar.research.contracts import TextChunk
        supplied = DocumentBundle(records=[documents.records[-1]], fulltext_manifest={}, fulltext_extraction={},
            sections=[], chunks=[TextChunk("linked-text", "doc-p1#linked", "Recorded implementation conditions.")])
        reused, _ = build_material_report_inputs(topic="Implementation conditions", documents=supplied,
            documents_ref=ArtifactRef("sources/bundle.json"), assets=[])
        self.assertEqual(reused.papers[0]["url"], "https://example.test/code")
        self.assertEqual(reused.source_handles[0].citation_key, "P1")
        # Connector identities survive composition without recreating intake
        # assets or guessing publication identities from matching titles.
        originals = documents.records[:2]
        prior = DocumentRecord(document_id='previous', source='fixture', title='Same title',
            metadata={'kind': 'prior_draft', 'paper_id': 'previous-paper'})
        analysis = DocumentRecord(document_id='derived', source='local_analysis', title='Same title',
            metadata={'paper_id': 'not-a-measured-paper'})
        retained = DocumentBundle(records=[*originals, prior, analysis], fulltext_manifest={},
            fulltext_extraction={}, sections=[], chunks=[TextChunk(f'{row.document_id}-text',
                row.document_id, 'Retained original or explicitly derived text.')
                for row in [*originals, prior, analysis]])
        native, native_memory = build_material_report_inputs(topic='Use original identities',
            documents=retained, documents_ref=ArtifactRef('sources/selected.json'), assets=[],
            selected_evidence=True)
        self.assertEqual([row['id'] for row in native.papers], ['p1', 'p2'])
        self.assertEqual(set(native.citation_key_map.values()), {'p1', 'p2'})
        self.assertEqual(native_memory.source_handles, native.source_handles)
        self.assertEqual({row.metadata['document_id'] for row in native.source_handles
            if row.kind == 'paper'}, {'doc-p1', 'doc-p2'})
        for key in ("p1", "p2"):
            result = ReportToolGateway(projected).call(ReportToolCall(tool_name="get_paper_brief", arguments={"paper_id": key}))
            evidence = result.content["handles"][0]
            self.assertEqual(evidence["summary"], f"Abstract for {key}")
            self.assertEqual(evidence["metadata"]["reading_notes"]["method"], f"Method for {key}")
            self.assertEqual(evidence["metadata"]["extraction_status"], "metadata_only")
            self.assertEqual(evidence["metadata"]["reading_artifact"], "read/result.json")
        self.assertEqual(memory.source_handles, projected.source_handles)
        self.assertEqual(context.source_handles[0].summary, "")
        self.assertIn("3 metadata/abstract-only", projected.evidence_summary)
        self.assertIn("Only point-prediction candidates", projected.evidence_summary)
        self.assertIn("not verified answers", projected.evidence_summary)
        self.assertTrue(any("Do not describe metadata/abstract-only sources" in line
                            for line in memory.limitations))

    def test_local_document_reading_notes_reach_report_tools_without_connector_metadata(self):
        from simple_ar.report.projection import attach_report_read_evidence
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.research.contracts import DocumentRecord
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.evidence.reader import ReadResult
        from simple_ar.report.schema import SourceHandle, ReportToolCall
        from simple_ar.report.tool_gateway import ReportToolGateway

        record = DocumentRecord(document_id="local-paper", source="local_files", title="Local paper",
                                extraction_status="parsed", abstract="Background only")
        from simple_ar.research.contracts import TextChunk
        documents = DocumentBundle(records=[record], fulltext_manifest={}, fulltext_extraction={}, sections=[],
            chunks=[TextChunk(chunk_id="result-span", document_id="local-paper", text="Reported error was 0.12"),
                    TextChunk(chunk_id="other-paper", document_id="different", text="Do not join this source")])
        note = {"paper_id": "local-paper", "datasets": ["Public observations"],
                "metrics": ["Prediction error"], "key_claims": ["Reported error was 0.12"],
                "limitations": ["One setting"], "confidence": "medium", "evidence_refs": ["result-span"],
                "reading_coverage": {"available_chunks": 30, "shown_chunk_ids": ["result-span"],
                                     "semantic_verification": "not_performed"}}
        context = ReportContext(topic="Evaluation", report_mode="research_only", source_handles=[
            SourceHandle(handle="paper:local-paper", kind="paper", paper_id="local-paper", citation_key="P1")])
        context, memory = attach_report_read_evidence(context, ReportMemory(), documents=documents,
            read=ReadResult(status="completed", bundle=documents, paper_notes=(note,)), read_ref=ArtifactRef("read.json"))
        result = ReportToolGateway(context).call(ReportToolCall(tool_name="get_paper_brief", arguments={"citation_key": "P1"}))
        metadata = result.content["handles"][0]["metadata"]
        self.assertEqual(metadata["reading_notes"]["key_claims"], note["key_claims"])
        self.assertEqual(metadata["reading_notes"]["datasets"], note["datasets"])
        self.assertEqual(metadata["reading_notes"]["metrics"], note["metrics"])
        self.assertEqual(metadata["reading_notes"]["reading_coverage"], note["reading_coverage"])
        self.assertEqual(metadata["reading_notes_kind"], "model_interpretation_not_source_text")
        self.assertEqual(metadata["evidence_passages"][0]["text"], "Reported error was 0.12")
        self.assertEqual(memory.source_handles, context.source_handles)

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


    def test_research_session_report_inputs_keep_execution_and_analysis_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = historical_session(root / "session")

            context, memory = build_research_session_report_inputs(session)

            self.assertEqual(context.topic, "reliable agents")
            self.assertEqual(context.results["status"], "passed")
            self.assertTrue(any(item.name == "accuracy" for item in context.metric_sources))
            self.assertTrue(any(item.kind == "analysis" for item in memory.source_handles))

    def test_report_projection_prefers_the_measured_execution_contract(self) -> None:
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as tmp:
            session = historical_session(Path(tmp) / "session")
            execution_contract = {
                "contract_id": "measured-protocol",
                "dataset_refs": [{"asset_id": "dataset-v3", "revision": "3"}],
                "split_spec": {"evaluation": "held-out-test"},
                "comparison_conditions": {"seed": 19, "epochs": 2},
            }
            session = replace(
                session,
                execution={**session.execution, "experiment_contract": execution_contract},
            )

            context, _ = build_research_session_report_inputs(session)

        self.assertEqual(context.experiment_plan["contract_id"], "measured-protocol")
        self.assertEqual(context.experiment_plan["dataset_refs"], execution_contract["dataset_refs"])
        self.assertEqual(context.experiment_plan["comparison_conditions"], {"seed": 19, "epochs": 2})
        self.assertEqual(context.experiment_plan["metrics"], [])
        self.assertEqual(context.experiment_plan["dataset"], "unknown")
        self.assertEqual(context.hypothesis_markdown, "Validation improves reliability.")

    def test_report_projection_assigns_model_citation_keys(self) -> None:
        from simple_ar.core.capabilities import ArtifactRef
        from simple_ar.literature.models import Paper
        from simple_ar.report.projection import build_literature_report_inputs
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.sources.capability import SearchResult
        from simple_ar.research.synthesis import SynthesisResult

        paper = Paper(
            id="openalex-W1", title="A retrieved paper", authors=[], abstract="Evidence.", url="https://example.test/paper"
        )
        search = SearchResult(status="completed", responses=(), papers=(paper,), selected_papers=(paper,))
        brief = SynthesisResult(status="ready", gap_summary="", ideas=(), novelty_checks=())
        documents = DocumentBundle(records=[], fulltext_manifest={}, fulltext_extraction={}, sections=[], chunks=[])

        context, memory = build_literature_report_inputs(
            topic="A topic", brief=brief, search=search, documents=documents,
            brief_ref=ArtifactRef("synthesis.json"),
        )

        self.assertEqual(context.citation_key_map, {"P1": "openalex-W1"})
        self.assertEqual(context.source_handles[1].citation_key, "P1")
        self.assertEqual(memory.source_handles[1].citation_key, "P1")
        direct, direct_memory = build_literature_report_inputs(
            topic="A topic", brief=None, search=search, documents=documents, brief_ref=None)
        self.assertEqual(direct.citation_key_map, context.citation_key_map)
        self.assertEqual(direct.report_mode, "research_only")
        self.assertEqual(direct.source_handles[0].citation_key, "P1")
        self.assertEqual(direct_memory.source_handles, direct.source_handles)
        self.assertEqual(direct.synthesis_markdown, "")
        self.assertIn("No experiment", direct_memory.limitations[0])


if __name__ == "__main__":
    unittest.main()
