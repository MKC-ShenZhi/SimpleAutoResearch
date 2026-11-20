"""Paired differences use common rows and retain their data-derived uncertainty."""
from dataclasses import asdict
import json
import math
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import data_settings, research_defaults
from simple_ar.result_analysis.figures import render_table_figures
from simple_ar.result_analysis.table import TableSpec, describe_table, load_analysis_package, table_values_markdown
from tests import captured_data_figures


class PairedTableTests(unittest.TestCase):
    def setUp(self):
        self.spec = TableSpec(("baseline", "candidate"), "one independently sampled matched run",
            value_unit="seconds", missing="omit", paired_baseline="baseline")
        self.rows = [{"baseline": 10, "candidate": 13}, {"baseline": 100, "candidate": None},
                     {"baseline": None, "candidate": 5}, {"baseline": 5, "candidate": 6}]

    def test_missing_is_jointly_omitted_not_subtracted_from_separate_means(self):
        result = describe_table(self.rows, self.spec)
        pair = result["paired_comparisons"][0]
        self.assertEqual(pair["differences"], [3, 1])
        self.assertEqual((pair["count"], pair["missing_pairs"], pair["mean_difference"]), (2, 2, 2))
        self.assertAlmostEqual(pair["sample_std_difference"], math.sqrt(2))
        self.assertAlmostEqual(pair["standard_error"], 1)
        self.assertNotEqual(pair["mean_difference"], result["records"][1]["mean"] - result["records"][0]["mean"])
        self.assertIn("candidate minus baseline", table_values_markdown(result))

    def test_single_and_zero_pairs_do_not_invent_uncertainty(self):
        for rows, count in ((self.rows[:1], 1), (self.rows[1:3], 0)):
            pair = describe_table(rows, self.spec)["paired_comparisons"][0]
            self.assertEqual(pair["count"], count)
            self.assertIsNone(pair["sample_std_difference"])
            self.assertIsNone(pair["standard_error"])
            if count == 0:
                self.assertIsNone(pair["mean_difference"])

    def test_group_pairing_is_preserved(self):
        spec = TableSpec(("baseline", "candidate"), "one matched run", group_column="dataset", paired_baseline="baseline")
        result = describe_table([{"dataset": "A", "baseline": 1, "candidate": 2},
                                {"dataset": "B", "baseline": 9, "candidate": 7}], spec)
        self.assertEqual([row["mean_difference"] for row in result["paired_comparisons"]], [1, -2])

    def test_aggregated_or_unselected_baseline_is_rejected(self):
        with self.assertRaises(ValueError):
            TableSpec(("candidate",), "one run", paired_baseline="baseline")
        with self.assertRaises(ValueError):
            TableSpec(("baseline", "candidate"), "one supplied mean", mode="values", group_column="dataset", paired_baseline="baseline")

    def test_derived_figure_contains_actual_se_without_confidence_claim(self):
        result = describe_table(self.rows, self.spec)
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figures = render_table_figures(result, Path(directory))
            self.assertEqual(len(figures), 3)
            svg = (Path(directory) / figures[-1]["path"]).read_text()
            segment = drawn[-1].axes[0].collections[0].get_segments()[0]
            self.assertEqual(segment[:, 0].tolist(), [1, 3])
            self.assertIn("whiskers = ±1 SE", svg)
            self.assertIn("n=2; missing=2", " ".join(drawn[-1].axes[0].get_yticklabels()[0].get_text().split()))
            self.assertIn("not a confidence interval", figures[-1]["caption"])
            self.assertIn("Positive does not imply improvement", figures[-1]["caption"])

    def test_package_rechecks_paired_statistics_against_copied_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.json"
            source.write_text(json.dumps(self.rows))
            result = describe_table(self.rows, self.spec)
            result["source"] = {"path": "input.json", "kind": "user_data", "schema": None}
            result["source_name"] = "input.json"
            path = root / "analysis.json"
            path.write_text(json.dumps(result))
            restored, _, _ = load_analysis_package(path)
            self.assertEqual(restored["paired_comparisons"], result["paired_comparisons"])
            result["paired_comparisons"][0]["mean_difference"] = 900
            path.write_text(json.dumps(result))
            with self.assertRaises(ValueError):
                load_analysis_package(path)

    def test_paired_baseline_and_candidates_share_scale_without_changing_difference_data(self):
        spec = TableSpec(('baseline', 'candidate'), 'one paired seed', paired_baseline='baseline')
        result = describe_table([{'baseline': 1, 'candidate': 2}, {'baseline': 1, 'candidate': 4}], spec)
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figures = render_table_figures(result, Path(directory))
            widths = []
            for figure in figures[:2]:
                ax = drawn[figures.index(figure)].axes[0]
                widths.append(ax.patches[0].get_width())
                self.assertIn('share the same numeric scale', figure['caption'])
            self.assertEqual(drawn[0].axes[0].get_xlim(), drawn[1].axes[0].get_xlim())
            self.assertAlmostEqual(widths[1] / widths[0], 3)
            self.assertEqual(result['paired_comparisons'][0]['differences'], [1, 3])
            self.assertIn('not a confidence interval', figures[-1]['caption'])

    def test_cli_and_config_use_the_same_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data.json"
            source.write_text(json.dumps(self.rows))
            args = build_parser().parse_args(["research-session", "--topic", "Compare matched runs", "--task-kind", "data_analysis",
                "--data-file", str(source), "--value-column", "baseline", "--value-column", "candidate",
                "--observation-unit", "one matched run", "--data-missing", "omit", "--paired-baseline", "baseline"])
            settings = data_settings(args)
            self.assertEqual(settings["paired_baseline"], "baseline")
            config = root / "research.toml"
            config.write_text('[task]\nkind="data_analysis"\ngoal="Compare matched runs"\n[analysis]\nfile="data.json"\nvalue_columns=["baseline","candidate"]\nobservation_unit="one matched run"\npaired_baseline="baseline"\nmissing="omit"\n')
            defaults = research_defaults(["research-session", "--config", str(config)])
            args = build_parser(research_defaults=defaults).parse_args(["research-session", "--config", str(config)])
            self.assertEqual(asdict(TableSpec.from_config(data_settings(args)))["paired_baseline"], "baseline")

    def test_interactive_columns_show_names_and_values_before_selection(self):
        from simple_ar.cli.start import prepare_start
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'data.csv'
            source.write_text('repeat,baseline,candidate\n1,3,5\n2,4,5\n')
            args = build_parser().parse_args(['start', '--kind', 'data_analysis', '--goal', 'Compare pairs',
                '--data-file', str(source), '--paired-baseline', 'baseline', '--output-root', str(root / 'runs'), '--prepare-only'])
            with patch('sys.stdin.isatty', return_value=True), patch('builtins.input', side_effect=['2,3', 'one independent pair']), patch('simple_ar.cli.start.print_line') as printed:
                config = prepare_start(args)
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertEqual(defaults['value_column'], ['baseline', 'candidate'])
            self.assertTrue(any('baseline' in call.args[0] and "'3'" in call.args[0] for call in printed.call_args_list))

if __name__ == "__main__":
    unittest.main()
