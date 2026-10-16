"""Declared data sources travel with copied results, never become verified papers."""
import contextlib
import html
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.cli.main import main
from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import research_defaults
from simple_ar.cli.start import session_materials
from simple_ar.report.citations import append_references_section
from simple_ar.report.data_delivery import analysis_delivery_block
from simple_ar.report.schema import ReportRuntimeConfig
from simple_ar.result_analysis.table import TableSpec, copy_analysis_package, data_attribution_markdown, load_analysis_package


class DataAttributionTests(unittest.TestCase):
    def test_optional_default_retains_old_spec_and_invalid_type_fails(self):
        spec = TableSpec(("value",), "one measurement")
        self.assertNotIn("attribution", spec.to_config())
        self.assertEqual(TableSpec.from_config(spec.to_config()), spec)
        with self.assertRaisesRegex(ValueError, "attribution"):
            TableSpec(("value",), "one measurement", attribution={"verified": True})

    def test_cli_to_copy_to_writer_preserves_declaration_and_recomputes_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.csv"
            source.write_text("value\n1\n3\n", encoding="utf-8")
            declaration = "Example laboratory dataset, release 2025; https://example.org/data-v2"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["start", "--kind", "data_analysis", "--goal", "Describe values",
                    "--data-file", str(source), "--value-column", "value", "--observation-unit", "one measurement",
                    "--data-attribution", declaration, "--yes", "--output-root", str(root / "tasks")])
            self.assertIsNone(code)
            config = next((root / "tasks").glob("*/research.toml"))
            self.assertEqual(research_defaults(["research-session", "--config", str(config)])["data_attribution"], declaration)
            session = next((config.parent / "sessions").iterdir())
            packages = session_materials(session)['data_analysis']
            self.assertEqual(len(packages), 1)
            package = packages[0]
            source.unlink()  # Only the frozen package is needed thereafter.
            result, raw, _ = load_analysis_package(package)
            self.assertEqual(result["spec"]["attribution"], declaration)
            self.assertEqual(result["records"][0]["mean"], 2)
            moved = copy_analysis_package(package, root / "elsewhere")
            self.assertEqual(moved["spec"]["attribution"], declaration)
            self.assertEqual((root / "elsewhere/input.csv").read_bytes(), raw)
            block = analysis_delivery_block(moved, index=1, handle="", config=ReportRuntimeConfig(), plan=None, section_ids=[])
            self.assertIn(declaration, html.unescape(block["markdown"]))
            self.assertIn("not independently verified", block["markdown"])
            self.assertIn(declaration, html.unescape((root / "elsewhere/analysis.md").read_text()))
            payload = json.loads(package.read_text())
            payload["spec"].pop("attribution")
            package.write_text(json.dumps(payload))
            old, _, _ = load_analysis_package(package)
            self.assertNotIn("attribution", old["spec"])
            self.assertNotIn("Data attribution", data_attribution_markdown(old))

    def test_metadata_is_literal_text_not_remote_image_heading_or_citation(self):
        declaration = "![source](https://example.org/a.svg)\n## verified <script> @fake `quoted`"
        rendered = data_attribution_markdown({"spec": {"attribution": declaration}})
        self.assertNotIn("![", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("@fake", rendered)
        self.assertNotIn("\n", rendered)
        self.assertIn(declaration.replace("\n", " "), html.unescape(rendered))

    def test_empty_paper_list_does_not_claim_missing_data_sources(self):
        text = "# Dataset report\n\nData attribution: a supplied dataset."
        self.assertEqual(append_references_section(text, []), text + "\n")
        self.assertNotIn("## References", append_references_section(text, []))

    def test_data_option_rejected_for_other_functions(self):
        args = build_parser().parse_args(["start", "--kind", "survey", "--sources", "materials", "--goal", "Review", "--data-attribution", "A source"])
        from simple_ar.cli.start import prepare_start
        with patch("sys.stdin.isatty", return_value=False), self.assertRaisesRegex(ValueError, "Data options"):
            prepare_start(args)
