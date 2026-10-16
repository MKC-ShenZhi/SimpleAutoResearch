from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from simple_ar.app.research_intake import (
    ResearchInputError,
    normalize_assets,
    parse_research_input,
    validate_brief,
    write_intake_artifacts,
)
from simple_ar.research.workflow_contracts import ResearchBrief




class ResearchIntakeTests(unittest.TestCase):
    def test_plain_text_is_preserved_without_inferred_intent(self) -> None:
        brief = parse_research_input("  Compare two small classifiers.\n")

        self.assertEqual(brief.request_text, "  Compare two small classifiers.\n")
        self.assertEqual(brief.intents, ())
        self.assertEqual(brief.requested_outputs, ())
        self.assertEqual(brief.asset_requests, ())
        diagnostics = validate_brief(brief, ())
        self.assertEqual([item.code for item in diagnostics], ["outputs_unspecified"])
        self.assertEqual(diagnostics[0].severity, "info")

    def test_markdown_request_and_relative_asset_get_light_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.md"
            paper_path = root / "notes" / "paper.md"
            paper_path.parent.mkdir()
            request_path.write_text("# Reproduce\nCompare the baseline and candidate.", encoding="utf-8")
            paper_path.write_text("A small evidence note.", encoding="utf-8")

            brief = parse_research_input(request_path, asset_paths=(Path("notes/paper.md"),))
            assets = normalize_assets(brief, input_base_dir=request_path.parent)

        self.assertEqual(brief.source_path, str(request_path.resolve()))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].kind, "document")
        self.assertEqual(assets[0].availability, "available")
        self.assertEqual(assets[0].identity["fingerprint_strength"], "content")
        self.assertTrue(assets[0].identity["sha256"])
        self.assertEqual(assets[0].diagnostics, ())

    def test_toml_explicit_fields_and_same_asset_roles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "digits.csv"
            dataset.write_text("x,y\n1,0\n", encoding="utf-8")
            config = root / "research.toml"
            config.write_text(
                """
request_text = "Evaluate a lightweight feature change."
objective = "Compare the candidate against the baseline."
intents = ["evaluate", "implement"]
requested_outputs = ["experiment", "report"]

[[assets]]
path = "digits.csv"
role = "dataset"

[[assets]]
path = "digits.csv"
role = "baseline"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            brief = parse_research_input("", config_path=config)
            assets = normalize_assets(brief, input_base_dir=config.parent)

            output = root / "intake"
            paths = write_intake_artifacts(brief, assets, output)
            restored = ResearchBrief.from_dict(json.loads(paths["brief"].read_text()))
            markdown = paths["markdown"].read_text(encoding="utf-8")

        self.assertEqual(brief.objective, "Compare the candidate against the baseline.")
        self.assertEqual(brief.intents, ("evaluate", "implement"))
        self.assertEqual(brief.requested_outputs, ("experiment", "report"))
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0].asset_id, assets[1].asset_id)
        self.assertEqual({asset.role for asset in assets}, {"dataset", "baseline"})
        self.assertEqual(validate_brief(brief, assets), ())
        self.assertEqual(restored.request_text, brief.request_text)
        self.assertIn("## Assets", markdown)

    def test_missing_asset_is_explicit_warning_not_research_failure(self) -> None:
        brief = parse_research_input(
            "Summarize the literature.",
            asset_paths=("missing-paper.pdf",),
        )
        assets = normalize_assets(brief, input_base_dir=Path.cwd())
        diagnostics = validate_brief(brief, assets)

        self.assertEqual(assets[0].availability, "missing")
        self.assertIn("asset_missing", [item.code for item in diagnostics])
        self.assertNotIn("error", [item.severity for item in diagnostics])

    def test_invalid_input_reports_diagnostic(self) -> None:
        with self.assertRaises(ResearchInputError) as raised:
            parse_research_input("")

        self.assertEqual(raised.exception.diagnostics[0].code, "request_text_required")

if __name__ == "__main__":
    unittest.main()
