"""Shared axes are explicit comparisons, not implicit rescaling or statistics."""
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
from tests import captured_data_figures

from simple_ar.cli.main import main
from simple_ar.result_analysis.table import TableSpec, describe_table, load_analysis_package, rebuild
from simple_ar.result_analysis.figures import render_table_figures


class SharedCoordinateTests(unittest.TestCase):
    rows = [{"step": 0, "A": 0, "B": 2}, {"step": 1, "A": 2, "B": 4}]

    def spec(self, **changes):
        return TableSpec(("A", "B"), "one supplied coordinate", mode="values", plot="line",
                         x_column="step", value_unit="points", **changes)

    def test_shared_panel_has_common_scale_legend_and_unchanged_values(self):
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            root = Path(directory)
            result = describe_table(self.rows, self.spec(series_layout="shared"))
            figures = render_table_figures(result, root)
            self.assertEqual(len(figures), 1)
            svg = ET.parse(root / figures[0]["path"]).getroot()
            self.assertEqual(len(drawn[0].axes), 1)
            ax = drawn[0].axes[0]
            points = ax.collections[0].get_offsets().tolist()
            self.assertCountEqual(points, [[0, 0], [1, 2], [0, 2], [1, 4]])
            self.assertEqual(ax.transData.transform(points[1])[1], ax.transData.transform(points[2])[1])
            self.assertEqual([node.get_text() for node in ax.get_legend().get_texts()], ["A", "B"])
            labels = [(node.text or "") + "".join(child.tail or "" for child in node) for node in svg.findall(".//{*}text")]
            self.assertIn("A", labels)
            self.assertIn("B", labels)
            self.assertEqual([row["value"] for row in result["records"]], [0, 2, 2, 4])
            self.assertIn("Series share axes", figures[0]["caption"])
            self.assertNotIn("sample_std", result["records"][0])

    def test_default_and_legacy_spec_keep_separate_axes(self):
        self.assertEqual(TableSpec.from_config({"value_columns": ["A", "B"],
            "observation_unit": "point", "mode": "values", "plot": "line", "x_column": "step"}).series_layout, "separate")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(len(render_table_figures(describe_table(self.rows, self.spec()), Path(directory))), 2)

    def test_missing_points_break_only_their_own_line(self):
        rows = [self.rows[0], {"step": .5, "A": None, "B": 3}, self.rows[1]]
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            root = Path(directory)
            figure = render_table_figures(describe_table(rows, self.spec(series_layout="shared", missing="omit")), root)[0]
            ET.parse(root / figure["path"])
            curves = drawn[0].axes[0].lines
            self.assertEqual(len(curves), 2)
            self.assertEqual([line.get_xdata().tolist() for line in curves], [[0, .5, 1], [0, .5, 1]])
            self.assertEqual([math.isnan(v) for v in curves[0].get_ydata()], [False, True, False])
            self.assertEqual(curves[0].get_ydata()[[0, 2]].tolist(), [0, 2])
            self.assertEqual(curves[1].get_ydata().tolist(), [2, 3, 4])
            self.assertEqual(len(drawn[0].axes[0].collections[0].get_offsets()), 5)

    def test_column_labels_wrap_without_losing_common_unit(self):
        spec = TableSpec(("A", "B"), "point", mode="values", plot="scatter", x_column="step",
                         width="column", series_layout="shared", value_unit="author-defined conditional coverage error")
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            root = Path(directory)
            figure = render_table_figures(describe_table(self.rows, spec), root)[0]
            svg = ET.parse(root / figure["path"]).getroot()
            labels = [(node.text or "") + "".join(child.tail or "" for child in node) for node in svg.findall(".//{*}text")]
            ax = drawn[0].axes[0]
            self.assertEqual(" ".join(ax.get_ylabel().split()), "Values (author-defined conditional coverage error)")
            self.assertIn("\n", ax.get_ylabel())
            self.assertIn(" ".join(ax.get_ylabel().split()), " ".join(" ".join(labels).split()))
            self.assertAlmostEqual(drawn[0].get_figwidth(), 3.5)
            self.assertAlmostEqual(float(svg.get("viewBox").split()[-1]), drawn[0].get_figheight() * 72)
            label_box = ax.yaxis.label.get_window_extent(drawn[0].canvas.get_renderer())
            self.assertGreaterEqual(label_box.x0, 0)
            self.assertGreaterEqual(label_box.y0, 0)
            self.assertLessEqual(label_box.x1, drawn[0].bbox.width)
            self.assertLessEqual(label_box.y1, drawn[0].bbox.height)
            self.assertCountEqual(ax.collections[0].get_offsets().tolist(), [[0, 0], [1, 2], [0, 2], [1, 4]])

    def test_physical_point_limit_applies_to_the_entire_shared_panel(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "max_points"):
                render_table_figures(describe_table(self.rows, self.spec(series_layout="shared", max_points=3)), Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_or_bar_layout_is_not_silently_ignored(self):
        for layout in ("guess", "shared"):
            with self.subTest(layout=layout), self.assertRaises(ValueError):
                TableSpec(("A",), "row", series_layout=layout)

    def test_public_config_package_rebuild_and_completed_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.csv").write_text("step,A,B\n0,0,2\n1,2,4\n")
            config = root / "task.toml"
            config.write_text('[task]\nkind="data_analysis"\ngoal="Compare supplied curves"\noutput_root="out"\n'
                              '[analysis]\nfile="data.csv"\nvalue_columns=["A","B"]\nobservation_unit="supplied point"\n'
                              'mode="values"\nplot="line"\nx_column="step"\nvalue_unit="points"\nseries_layout="shared"\n')
            with contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--config", str(config)])
            path = next((root / "out").glob("*/attempts/data_analysis-*/analysis.json"))
            result, _, _ = load_analysis_package(path)
            self.assertEqual(result["spec"]["series_layout"], "shared")
            self.assertEqual(len(json.loads(path.read_text())["figures"]), 1)
            rebuild(path)
            session = path.parents[2]
            attempts = sorted(p.name for p in (session / "attempts").iterdir())
            ledger = json.loads((session / "budget_ledger.json").read_text())
            with contextlib.redirect_stdout(io.StringIO()):
                main(["research-session", "--config", str(config), "--session-root", str(session)])
            self.assertEqual(sorted(p.name for p in (session / "attempts").iterdir()), attempts)
            self.assertEqual(json.loads((session / "budget_ledger.json").read_text()), ledger)
