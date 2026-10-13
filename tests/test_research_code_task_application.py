"""Historical segmented CodeTask readers; execution uses ResearchApplication."""

from pathlib import Path
import tempfile
import unittest

from tests.legacy_session_fixture import historical_session, session_files
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.app.research_code_task import (
    ResearchCodeTaskSessionError,
    load_research_code_task_session_result,
)
from simple_ar.app.research_code_task_report import build_code_task_report_inputs


class ResearchCodeTaskApplicationTests(unittest.TestCase):
    def test_history_and_report_projection_do_not_execute_or_rewrite(self):
        for failed, design in ((False, False), (False, True), (True, False), (True, True)):
            with self.subTest(failed=failed, design=design), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                original = historical_session(root, code_task=True, failed=failed, with_design=design)
                before = session_files(root)
                session = load_research_code_task_session_result(root)
                context, memory = build_code_task_report_inputs(session)
                self.assertEqual(session, original)
                self.assertEqual(session.status, "incomplete" if failed else "completed")
                self.assertEqual(session.design is not None, design)
                self.assertEqual(context.results["status"], "failed" if failed else "passed")
                self.assertEqual([(m.name, m.value) for m in context.metric_sources],
                                 [] if failed else [("accuracy", 0.75)])
                self.assertEqual(context.source_handles, memory.source_handles)
                self.assertEqual([h.artifact for h in context.source_handles],
                                 [session.execution_ref.path, session.analysis_ref.path])
                self.assertEqual(session_files(root), before)

    def test_reader_rejects_missing_declared_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            historical_session(root, code_task=True)
            (root / "inputs/code_task_input.json").unlink()
            with self.assertRaises(ResearchCodeTaskSessionError):
                load_research_code_task_session_result(root)

    def test_reader_rejects_analysis_bound_to_another_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            historical_session(root, code_task=True)
            path = root / "attempts/analysis-001/analysis.json"
            payload = read_json(path)
            payload["execution_ref"]["path"] = "attempts/experiment-002/results.json"
            write_json(path, payload)
            before = session_files(root)
            with self.assertRaisesRegex(ResearchCodeTaskSessionError,
                                        "does not reference experiment-001 output"):
                load_research_code_task_session_result(root)
            self.assertEqual(session_files(root), before)


if __name__ == "__main__":
    unittest.main()
