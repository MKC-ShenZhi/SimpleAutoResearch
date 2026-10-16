"""Factual provenance is shared by writing/review, without running code."""
import json
import unittest

from simple_ar.report.agent import (
    _writer_prompt, _writer_recovery_prompt, _reviewer_prompt,
    WRITER_SYSTEM, REVIEWER_SYSTEM,
)
from simple_ar.report.execution_evidence import execution_record, report_execution_evidence
from simple_ar.report.schema import (
    ReportContext, ReportMemory, ReportRuntimeConfig, ReportSectionDraft, ReportSectionPlan,
)
from simple_ar.report.templates import load_report_template_bundle


class ReportExecutionEvidenceTests(unittest.TestCase):
    def test_experiment_reuses_literature_identity_without_survey_limitations(self):
        from simple_ar.core import ArtifactRef
        from simple_ar.literature.models import Paper
        from simple_ar.report.projection import build_literature_report_inputs, build_research_report_inputs
        from simple_ar.research.contracts import ResearchExperimentContract
        from simple_ar.research.documents.ingest import DocumentBundle
        from simple_ar.research.sources import SearchResult
        from simple_ar.research.synthesis import SynthesisResult
        from simple_ar.result_analysis.schema import AnalysisResult

        papers = tuple(Paper(id=key, title=key, authors=[], abstract="Source assertion.", url="")
            for key in ("z", "a", "z"))
        search = SearchResult(status="completed", responses=(), papers=papers, selected_papers=papers)
        brief = SynthesisResult(status="ready", gap_summary="", ideas=(), novelty_checks=(),
            synthesis_markdown="Retained synthesis.", comparisons=({"relation": "single_source"},),
            experiment_contract=ResearchExperimentContract("contract", "Measured hypothesis.", risks=["Protocol risk."]))
        shared = dict(topic="Evidence", brief=brief, search=search,
            documents=DocumentBundle([], {}, {}, [], []), brief_ref=ArtifactRef("brief.json"))
        survey, _ = build_literature_report_inputs(**shared)
        for design_ref in (None, ArtifactRef("design.json")):
            with self.subTest(design=design_ref):
                context, memory = build_research_report_inputs(**shared, execution=self.context.results,
                    analysis=AnalysisResult(readme_markdown="", status="passed", audit={"limitations": ["Analysis limit."]}),
                    execution_ref=ArtifactRef("result.json"), analysis_ref=ArtifactRef("analysis.json"), design_ref=design_ref)
                self.assertEqual(context.citation_key_map, {"P1": "z", "P2": "a", "P3": "z"})
                for field in ("papers", "citation_key_map", "synthesis_markdown", "source_comparisons"):
                    self.assertEqual(getattr(context, field), getattr(survey, field))
                self.assertEqual(context.source_handles[-3:], survey.source_handles[1:])
                self.assertEqual([row.handle for row in context.source_handles[:-3]],
                    ["artifact:synthesis", *(["artifact:research_design"] if design_ref else []),
                     "artifact:experiment_execution", "artifact:result_analysis"])
                self.assertEqual(memory.source_handles, context.source_handles)
                self.assertEqual((context.report_mode, memory.template, memory.objective),
                    ("experiment", "experiment", "Measured hypothesis."))
                self.assertEqual(memory.limitations, ["Analysis limit.", "Protocol risk."])
                self.assertEqual(report_execution_evidence(context)["execution_records"],
                    report_execution_evidence(self.context)["execution_records"])

    def setUp(self):
        self.context = ReportContext(
            topic="A fixed reproduction", report_mode="experiment",
            execution_context="Requested CPU.\n## Prepared execution specification\n120 second limit.",
            experiment_plan={"dataset": "Declared synthetic data", "comparison_conditions": {"seed": 7}},
            results={"status": "passed", "command": ["not", "an", "observation"],
                     "execution": {"process": {
                         "invocation_id": "run-1", "status": "finished",
                         "argv": ["python", "run.py", "--seed", "7"], "cwd": "/work",
                         "timeout_sec": 120, "duration_sec": 0.8, "returncode": 0,
                         "streams": {"stdout": {"log_path": "/work/stdout.log", "bytes_seen": 20,
                                                   "log_bytes_discarded": 0}},
                     }}},
        )

    def test_only_executor_record_is_an_observation(self):
        evidence = report_execution_evidence(self.context)
        self.assertEqual(evidence["declared_protocol"]["comparison_conditions"]["seed"], 7)
        self.assertIn("Prepared execution specification", evidence["declared_execution_context"]["text"])
        observed = evidence["execution_records"][0]
        self.assertEqual(observed["argv"], ["python", "run.py", "--seed", "7"])
        self.assertEqual(observed["duration_sec"], 0.8)
        self.assertEqual(observed["timeout_sec"], 120)
        self.assertNotIn("hardware", observed)
        self.assertEqual(evidence["implementation_verification"]["status"], "not_checked")
        self.assertEqual(execution_record({"command": ["python"], "status": "passed"}),
                         {"observation_status": "not_recorded"})

    def test_history_preserves_failed_and_passed_observations_without_duplicate(self):
        record = execution_record(self.context.results)
        self.context.results["measurement_history"] = [
            {"action": "candidate", "execution_record": record},
            {"action": "repair", "execution_record": {**record, "invocation_id": "run-2", "returncode": 1}},
        ]
        evidence = report_execution_evidence(self.context)
        self.assertEqual(evidence["execution_records_total"], 2)
        self.assertEqual(evidence["execution_records"][1]["returncode"], 1)

    def test_long_argv_and_declarations_mark_truncation(self):
        self.context.execution_context = "x" * 6000
        process = self.context.results["execution"]["process"]
        process["argv"] = ["x" * 1100] * 70
        evidence = report_execution_evidence(self.context)
        self.assertTrue(evidence["declared_execution_context"]["truncated"])
        self.assertTrue(evidence["execution_records"][0]["argv_truncated"])
        self.assertEqual(len(evidence["execution_records"][0]["argv"]), 64)

    def test_writer_recovery_and_reviewer_use_the_same_roles(self):
        section = ReportSectionPlan(section_id="setup", heading="Local Setup", goal="Explain conditions")
        memory = ReportMemory(section_plan=[section])
        config = ReportRuntimeConfig(template="reproduction")
        template = load_report_template_bundle(report_mode="experiment", config=config)
        draft = ReportSectionDraft(section_id="setup", heading="Local Setup", draft_markdown="Declared setup.")
        writer = json.loads(_writer_prompt(context=self.context, template=template, memory=memory,
            section=section, config=config, extra_context=[], previous_draft=None, review=None,
            source_batch_index=1, source_batch_count=1, include_previous_draft=True, draft_mode="section").split("\n\n", 1)[1])
        recovery = json.loads(_writer_recovery_prompt(context=self.context, memory=memory,
            section=section, config=config, previous_draft=draft, review=None, draft_mode="section_revision").split("\n\n", 1)[1])
        reviewer = json.loads(_reviewer_prompt(context=self.context, template=template, memory=memory,
            section=section, draft=draft).split("\n\n", 1)[1])
        expected = report_execution_evidence(self.context)
        self.assertEqual(writer["global_research_context"]["execution_evidence"], expected)
        self.assertEqual(recovery["execution_evidence"], expected)
        self.assertEqual(reviewer["execution_evidence"], expected)
        self.assertIn("not an observation", " ".join(writer["style_rules"]))
        self.assertIn("not observed execution", " ".join(recovery["style_rules"]))
        self.assertNotIn("executed project", " ".join(recovery["style_rules"]))
        self.assertIn("declarations", WRITER_SYSTEM)
        self.assertIn("declaration/executor/producer", REVIEWER_SYSTEM)

    def test_reproduction_template_and_writer_agree_on_body_and_attachment_responsibility(self):
        config = ReportRuntimeConfig(template='reproduction')
        template = load_report_template_bundle(report_mode='experiment', config=config)
        self.assertIn('attachments', template.template_markdown)
        self.assertIn('do not require their duplication in the body', template.criteria_markdown)
        self.assertIn('truncated output preview does not establish', template.criteria_markdown)
        section = ReportSectionPlan(section_id='setup', heading='Local Setup', goal='Explain')
        prompt = _writer_prompt(context=self.context, template=template,
            memory=ReportMemory(section_plan=[section]), section=section, config=config,
            extra_context=[], previous_draft=None, review=None, source_batch_index=1,
            source_batch_count=1, include_previous_draft=True, draft_mode='section')
        self.assertIn('duplicate these in the body only when the user requests it', prompt)

if __name__ == "__main__":
    unittest.main()
