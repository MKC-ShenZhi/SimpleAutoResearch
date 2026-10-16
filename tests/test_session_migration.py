from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import tempfile
import unittest

from simple_ar.app.research_application import ResearchApplicationServices
from simple_ar.app.session_migration import import_legacy_session
from simple_ar.app.tool_application import ToolApplication
from simple_ar.core import ArtifactStore
from simple_ar.research.workflow_contracts import ResearchBrief


class SessionMigrationTests(unittest.TestCase):
    def _write_legacy_session(self, root: Path) -> None:
        store = ArtifactStore(root)
        search_ref = store.write_text(
            "state/search.md",
            "# Calibration evidence\nA paper reports calibration error of 0.12.\n",
            kind="search",
            schema="old_search.v1",
            producer="legacy",
        )
        store.write_text("state/unselected.md", "This material was not selected for import.\n")
        brief_ref = store.write_json(
            "state/brief.json",
            ResearchBrief(
                request_text="Study calibration",
                objective="Study calibration under a small budget",
                requested_outputs=("report",),
                source_path=str(root / "request.md"),
                asset_requests=({"locator": str(root / "state/unselected.md"), "role": "material"},),
            ).to_dict(),
            kind="brief",
            schema="research_brief_input.v1",
            producer="legacy",
        )
        store.write_json(
            "session_manifest.json",
            {
                "schema_version": "session_manifest.v1",
                "session_id": "legacy-session",
                "topic": "Study calibration",
                "status": "created",
                "state_refs": {
                    "search": search_ref.to_dict(),
                    "brief": brief_ref.to_dict(),
                },
                "budget": {"max_attempts": 3, "max_no_progress": 2,
                           "attempts": 3, "no_progress": 2},
                "decisions": [],
            },
            kind="session",
            schema="session_manifest.v1",
            producer="legacy",
        )

    def test_import_creates_successor_and_preserves_legacy_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            successor = base / "successor"
            self._write_legacy_session(legacy)
            before = {
                path.relative_to(legacy): path.read_bytes()
                for path in legacy.rglob("*")
                if path.is_file()
            }

            result = import_legacy_session(
                legacy,
                successor,
                artifact_names=("search", "not_present"),
                services=ResearchApplicationServices(max_results=1),
            )

            self.assertEqual(result.parent_session, "legacy-session")
            self.assertEqual(result.brief_source, "legacy_brief")
            self.assertEqual(result.budget_status, "unknown_not_imported")
            self.assertEqual([item.name for item in result.imported], ["search"])
            self.assertEqual([item.name for item in result.skipped], ["not_present"])
            self.assertTrue((successor / result.imported[0].destination_ref.path).is_file())

            restored = ToolApplication.load(successor)
            self.assertEqual(restored.session.manifest.transition_recipe, ToolApplication.RECIPE)
            self.assertEqual(restored.session.manifest.parent_session, "legacy-session")
            self.assertEqual(restored.session.manifest.session_id, result.destination_session)
            self.assertIn("legacy_import", restored.session.manifest.state_refs)
            provenance = restored.session.store.read_json(result.migration_ref)
            self.assertEqual(provenance["budget_status"], "unknown_not_imported")
            self.assertEqual(provenance["parent_session"], "legacy-session")
            brief = ResearchBrief.from_dict(restored.session.store.read_json(
                restored.session.manifest.state_refs["brief"]))
            self.assertEqual(brief.objective, "Study calibration under a small budget")
            self.assertEqual(brief.requested_outputs, ("report",))
            self.assertIsNone(brief.source_path)
            self.assertTrue(restored.state["config"]["report_requirement"])
            self.assertEqual(restored.session.list_attempts(), ())
            self.assertEqual(restored.ledger.entries, ())
            self.assertEqual(restored.session.manifest.budget.attempts, 0)
            self.assertEqual(restored.session.manifest.budget.no_progress, 0)
            copied = successor / result.imported[0].destination_ref.path
            self.assertEqual(copied.read_bytes(), before[Path("state/search.md")])
            self.assertEqual(result.imported[0].sha256, hashlib.sha256(copied.read_bytes()).hexdigest())
            self.assertEqual(result.imported[0].size_bytes, len(copied.read_bytes()))
            mounts = restored.state["config"]["sources"]
            self.assertEqual(len(mounts), 1)
            for mount in mounts.values():
                self.assertTrue(Path(mount).resolve().is_relative_to(successor.resolve()))
            self.assertEqual(restored.state["config"]["research_policy"]["materials"], ["source0"])

            after = {
                path.relative_to(legacy): path.read_bytes()
                for path in legacy.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

            # Resume real research against retained bytes, with the original gone.
            shutil.rmtree(legacy)
            resumed = ToolApplication.load(successor)
            snapshot = resumed.run(actions=[{"tool_name": "research", "arguments": {
                    "operation": "ingest", "question": "Read the calibration evidence",
                    "use_llm": False}}], progress=lambda _: None)
            self.assertEqual(snapshot["session_id"], result.destination_session)
            turn = resumed.state["turns"][-1]
            observed = resumed.session.store.read_json(turn["tools"][-1]["result"])
            self.assertEqual(observed["status"], "ok", observed)
            document = resumed.workspace.resolve(observed["data"]["document_record"])
            bundle = json.loads(document.read_text(encoding="utf-8"))
            self.assertEqual(bundle["schema_version"], "document_bundle.v1")
            self.assertTrue(bundle["documents"])
            self.assertIn("calibration error of 0.12", json.dumps(bundle["chunks"]))
            self.assertTrue(resumed.session.list_attempts())
            self.assertTrue(all(item.capability == "native_tool"
                                for item in resumed.session.list_attempts()))

    def test_topic_only_import_preserves_outputs_and_bounds_selected_copies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = base / "legacy"
            successor = base / "successor"
            self._write_legacy_session(legacy)
            store = ArtifactStore(legacy)
            old = store.read_json("session_manifest.json")
            del old["state_refs"]["brief"]
            store.write_json("session_manifest.json", old)
            before = {p.relative_to(legacy): p.read_bytes() for p in legacy.rglob("*") if p.is_file()}
            result = import_legacy_session(legacy, successor, artifact_names=("search",),
                max_artifact_bytes=1, requested_outputs=("assessment",))
            self.assertEqual(result.brief_source, "manifest_topic")
            self.assertEqual(result.imported, ())
            self.assertEqual([row.name for row in result.skipped], ["search"])
            restored = ToolApplication.load(successor)
            brief = ResearchBrief.from_dict(restored.session.store.read_json(
                restored.session.manifest.state_refs["brief"]))
            self.assertEqual(brief.request_text, "Study calibration")
            self.assertEqual(brief.objective, "Study calibration")
            self.assertEqual(brief.requested_outputs, ("assessment",))
            self.assertEqual(restored.state["config"]["sources"], {})
            self.assertEqual(restored.state["config"]["research_policy"]["materials"], [])
            self.assertEqual(before, {p.relative_to(legacy): p.read_bytes()
                                     for p in legacy.rglob("*") if p.is_file()})
            manifest = ArtifactStore(successor).read_json("session_manifest.json")
            refs = manifest["state_refs"]
            for ref in refs.values():
                self.assertFalse(Path(ref["path"]).is_absolute())
            self.assertEqual(manifest["parent_session"], "legacy-session")


if __name__ == "__main__":
    unittest.main()
