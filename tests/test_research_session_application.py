from __future__ import annotations

import json
import tempfile
import unittest

from tests.legacy_session_fixture import historical_session, session_files
from pathlib import Path

from simple_ar.app.research_session import (
    ResearchSessionError,
    load_research_session_result,
)


class ResearchSessionApplicationTests(unittest.TestCase):
    def test_historical_outputs_restore_without_executing_workflow(self) -> None:
        for aggregate, design, failed in ((False, True, False), (True, False, False),
                                           (False, False, True), (True, True, True)):
            with self.subTest(aggregate=aggregate, design=design, failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = historical_session(root, aggregate_brief=aggregate, with_design=design, failed=failed)
                before = session_files(root)
                restored = load_research_session_result(root)
                self.assertEqual(restored, result)
                self.assertEqual(session_files(root), before)
                self.assertEqual(result.status, "incomplete" if failed else "ready_for_report")
                self.assertEqual(result.report_ready, not failed)
                self.assertEqual(result.next_capability, "experiment" if failed else "report")
                self.assertEqual(result.execution["status"], "failed" if failed else "passed")
                self.assertEqual(result.analysis.status, "incomplete" if failed else "passed")
                self.assertEqual([a.attempt_id for a in result.attempts], sorted([
                    "analysis-001", "document-001", "experiment-001", "plan-001", "search-001",
                    *(["brief-001"] if aggregate else ["read-001", "synthesize-001"]),
                    *(["design-001"] if design else []),
                ]))
                self.assertEqual([d.action for d in result.decisions], ["accept"])
                self.assertEqual(result.design is not None, design)
                self.assertEqual(result.design_ref is not None, design)
                for ref in (result.brief_ref, result.execution_ref, result.analysis_ref):
                    self.assertTrue(ref.path.startswith("attempts/"))
                    self.assertIn(Path(ref.path), before)
                manifest = json.loads((root / "session_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual((manifest["profile"], manifest["status"]), ("full_research", "running"))

    def test_restore_rejects_a_missing_typed_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing-session"
            with self.assertRaises(ResearchSessionError):
                load_research_session_result(root)


if __name__ == "__main__":
    unittest.main()
