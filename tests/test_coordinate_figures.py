"""Coordinate figures retain supplied data, share the existing task and package."""
import contextlib
import io
import json
import math
import shutil
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from tests import captured_data_figures

from simple_ar.cli.main import main
from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import research_defaults
from simple_ar.cli.start import prepare_start
from simple_ar.report.narrative import _compact_execution_results
from simple_ar.result_analysis.figures import render_table_figures
from simple_ar.result_analysis.table import (
    TableSpec, describe_table, load_analysis_package, rebuild, table_input_handling, table_markdown, table_values_markdown,
)


class CoordinateFigureTests(unittest.TestCase):
    def spec(self, **overrides):
        return TableSpec.from_config(dict(value_columns=['loss'], observation_unit='one supplied checkpoint',
                                         mode='values', plot='line', x_column='step', **overrides))

    def test_no_coordinate_semantics_or_aggregation_is_guessed(self):
        settings = dict(value_columns=['loss'], observation_unit='one row', mode='values', plot='line', x_column='step')
        for override in ({'mode': 'observations'}, {'group_column': 'step'}, {'x_column': ''},
                         {'x_column': 'loss'}, {'plot': 'unknown'}, {'max_points': 0}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                TableSpec.from_config({**settings, **override})
        for value in ('inf', 'word', True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                describe_table([{'step': value, 'loss': 2}], self.spec(missing='omit'))
        with self.assertRaisesRegex(ValueError, 'unique numeric x'):
            describe_table([{'step': 1, 'loss': 2}, {'step': 1, 'loss': 3}], self.spec())
        scatter = TableSpec.from_config({**settings, 'plot': 'scatter'})
        self.assertEqual(len(describe_table([{'step': 1, 'loss': 2}, {'step': 1, 'loss': 3}], scatter)['records']), 2)

    def test_line_sorted_for_display_only_and_missing_y_breaks_connections(self):
        rows = [{'step': 4, 'loss': 0}, {'step': 1, 'loss': 3}, {'step': 3, 'loss': None}, {'step': 2, 'loss': 2}]
        result = describe_table(rows, self.spec(missing='omit', x_unit='iteration <&>', value_unit='arbitrary units'))
        self.assertEqual([row['x'] for row in result['records']], [4, 1, 3, 2])
        self.assertIsNone(result['records'][2]['value'])
        self.assertEqual(result['records'][2]['missing'], 1)
        self.assertIn('missing', table_markdown({**result, 'source_name': 'data.csv', 'figures': []}))
        self.assertIn('| x | value |', table_values_markdown(result))
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figures = render_table_figures(result, Path(directory))
            svg = ET.parse(Path(directory) / figures[0]['path'])
            ax = drawn[0].axes[0]
            self.assertEqual(len(ax.collections[0].get_offsets()), 3)
            self.assertEqual([math.isnan(v) for v in ax.lines[0].get_ydata()], [False, False, True, False])
            self.assertIn('1 missing y', figures[0]['caption'])
            self.assertIn('iteration <&>', ''.join(svg.getroot().itertext()))
        self.assertEqual([row['x'] for row in result['records']], [4, 1, 3, 2])

    def test_each_metric_has_own_axis_and_limits_fail_without_sampling(self):
        spec = TableSpec(('a', 'b'), 'one coordinate', mode='values', plot='scatter', x_column='x')
        result = describe_table([{'x': -1e308, 'a': 1e308, 'b': 0},
                                 {'x': 1e308, 'a': -1e308, 'b': 0}], spec)
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figures = render_table_figures(result, Path(directory))
            self.assertEqual(len(figures), 2)
            for figure in figures:
                path = Path(directory) / figure['path']
                ET.parse(path)
                ax = drawn[figures.index(figure)].axes[0]
                self.assertEqual(len(ax.collections[0].get_offsets()), 2)
                self.assertNotRegex(path.read_text().lower(), r'(?<![a-z])(nan|[+-]?inf)(?![a-z])')
        with self.assertRaisesRegex(ValueError, 'max_points'):
            describe_table([{'step': 1, 'loss': 2}, {'step': 2, 'loss': 3}], self.spec(max_points=1))
        with tempfile.TemporaryDirectory() as directory:
            limited = {**result, 'spec': {**result['spec'], 'max_figures': 1}}
            with self.assertRaisesRegex(ValueError, 'max_figures'):
                render_table_figures(limited, Path(directory))
            self.assertFalse((Path(directory) / 'figures').exists())

    def test_legacy_coordinate_summaries_are_upgraded_but_tampering_is_rejected(self):
        from simple_ar.core.capabilities import ArtifactStore
        with tempfile.TemporaryDirectory() as directory:
            moved = Path(directory)
            rows = [{'step': 2, 'loss': 1}, {'step': 1, 'loss': 3}, {'step': 3, 'loss': None}]
            payload = describe_table(rows, self.spec(missing='omit'))
            (moved / 'input.json').write_text(json.dumps(rows))
            payload['source'] = ArtifactStore(moved).ref('input.json', kind='user_data').to_dict()
            (moved / 'analysis.json').write_text(json.dumps(payload))
            summaries = payload.pop('coordinate_summaries')
            (moved / 'analysis.json').write_text(json.dumps(payload))
            legacy, _, _ = load_analysis_package(moved / 'analysis.json')
            self.assertEqual(legacy['coordinate_summaries'], summaries)
            for summary in summaries:
                for key in ('missing_x_rows', 'missing_value_rows', 'missing_both_rows'):
                    summary.pop(key)
            payload['coordinate_summaries'] = summaries
            (moved / 'analysis.json').write_text(json.dumps(payload))
            upgraded, _, _ = load_analysis_package(moved / 'analysis.json')
            self.assertIn('missing_x_rows', upgraded['coordinate_summaries'][0])
            payload['coordinate_summaries'][0]['x']['median'] = 999
            (moved / 'analysis.json').write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'axis summaries'):
                load_analysis_package(moved / 'analysis.json')
            payload.pop('coordinate_summaries')
            payload['records'][0]['x'] = 999
            (moved / 'analysis.json').write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'no longer matches'):
                rebuild(moved / 'analysis.json')

    def test_coordinate_flags_do_not_silently_apply_to_other_tasks(self):
        base = ['start', '--kind', 'survey', '--sources', 'materials', '--goal', 'Survey', '--prepare-only']
        for flag in (['--data-plot', 'scatter'], ['--x-column', 'time'], ['--x-unit', 'seconds'], ['--data-max-points', '5']):
            with self.subTest(flag=flag), patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'Data options require'):
                prepare_start(build_parser().parse_args([*base, *flag]))

    def test_input_use_is_observed_not_inferred_from_reject_policy(self):
        result = describe_table([{'step': i, 'loss': i * 2} for i in range(35)], self.spec())
        view = table_input_handling(result)
        self.assertEqual(view['observed_use'], [{'column': 'loss', 'used': 35, 'missing': 0}])
        self.assertIn('does not delete rows', view['policy_behavior'])
        self.assertIn('35 used, 0 missing', table_values_markdown(result))
        from simple_ar.report.narrative import _compact_execution_results
        result.update(document_id='data', evidence_role='recomputed_from_user_supplied_data')
        projected = _compact_execution_results({'supplied_analyses': [result]})['supplied_analyses'][0]
        self.assertEqual(len(projected['records']), 12)
        self.assertEqual(projected['input_handling'], {**view, 'observed_use_omitted': 0})
        partial = {**result, 'records': [{'column': 'loss', 'value': 2}]}
        self.assertEqual(table_input_handling(partial)['observed_use'], 'unavailable_in_this_snapshot')

    def test_omit_counts_coordinate_pairs_without_deleting_source_rows(self):
        spec = TableSpec(('a', 'b'), 'one specimen', mode='values', plot='scatter',
                         x_column='x', missing='omit')
        result = describe_table([{'x': 1, 'a': 2, 'b': None}, {'x': None, 'a': 3, 'b': 4},
                                 {'x': 3, 'a': None, 'b': 5}], spec)
        self.assertEqual(result['row_count'], 3)
        self.assertEqual(len(result['records']), 6)
        self.assertEqual(table_input_handling(result)['observed_use'], [
            {'column': 'a', 'used': 1, 'missing': 2}, {'column': 'b', 'used': 1, 'missing': 2}])

    def test_dense_scatter_retains_points_order_and_updates_rebuilt_delivery(self):
        spec = TableSpec(('mass',), 'one specimen', mode='values', plot='scatter',
                         x_column='length', group_column='type', width='column')
        rows = [{'length': i % 80, 'mass': i % 100, 'type': 'B' if i % 2 else 'A'} for i in range(1800)]
        result = describe_table(rows, spec)
        before = json.dumps(result, sort_keys=True)
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            root = Path(directory)
            figures = render_table_figures(result, root)
            self.assertEqual(json.dumps(result, sort_keys=True), before)
            ET.parse(root / figures[0]['path'])
            points = drawn[0].axes[0].collections[0]
            self.assertEqual(len(points.get_offsets()), 1800)
            self.assertLess(points.get_sizes()[0], 3.6 ** 2)
            self.assertEqual(points.get_alpha(), .5)
            self.assertEqual(points.get_offsets()[:4].tolist(), [[i, i] for i in range(4)])
            self.assertEqual(drawn[0].axes[0].yaxis.label.get_rotation(), 90)
            source = root / 'input.json'
            raw = json.dumps(rows).encode()
            source.write_bytes(raw)
            from simple_ar.core.capabilities import ArtifactStore
            store = ArtifactStore(root)
            payload = {**result, 'source_name': source.name, 'source': store.ref(source.name, kind='user_data').to_dict(),
                       'figures': [{**figures[0], 'caption': 'Old caption', 'encoding': {'plot': 'scatter'}}]}
            store.write_json('analysis.json', payload, kind='table_analysis', schema='table_analysis.v1')
            rebuild(root / 'analysis.json')
            saved = json.loads((root / 'analysis.json').read_text())
            self.assertEqual(saved['figures'], figures)
            self.assertEqual(saved['records'], result['records'])
            self.assertEqual(source.read_bytes(), raw)
            self.assertIn(figures[0]['caption'], (root / 'analysis.md').read_text())
            self.assertNotIn('Old caption', (root / 'analysis.md').read_text())


class GroupedCoordinatesTests(unittest.TestCase):
    def spec(self, **changes):
        return TableSpec.from_config({'value_columns': ['y'], 'observation_unit': 'one supplied specimen',
            'mode': 'values', 'plot': 'scatter', 'x_column': 'x', 'group_column': 'species',
            'missing': 'omit', 'value_unit': 'mm', 'x_unit': 'mm', **changes})

    def test_scatter_keeps_original_rows_and_missing_coordinates_without_imputation(self):
        rows = [{'species': 'A<&>', 'x': 1, 'y': 10}, {'species': 'B', 'x': 1, 'y': 20},
                {'species': 'A<&>', 'x': None, 'y': 9}, {'species': 'B', 'x': 2, 'y': None}]
        result = describe_table(rows, self.spec())
        self.assertEqual(len(result['records']), 4)
        self.assertEqual(result['records'][2]['value'], 9)
        self.assertIsNone(result['records'][2]['x'])
        self.assertEqual([row['count'] for row in result['records']], [1, 1, 0, 0])
        self.assertIn('| Series |', table_values_markdown(result))
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figure = render_table_figures(result, Path(directory))[0]
            svg = ET.parse(Path(directory) / figure['path'])
            points = drawn[0].axes[0].collections[0]
            self.assertEqual(len(points.get_offsets()), 2)
            self.assertEqual(len({tuple(color) for color in points.get_facecolors()}), 2)
            self.assertIn('2 missing coordinates', figure['caption'])
            self.assertIn('A<&>', ''.join(svg.getroot().itertext()))
            self.assertIn('x=missing', table_markdown({**result, 'source_name': 'sample.json', 'figures': [figure]}))

    def test_line_x_uniqueness_and_connections_are_per_group(self):
        rows = [{'species': 'A', 'x': 1, 'y': 2}, {'species': 'B', 'x': 1, 'y': 3},
                {'species': 'A', 'x': 2, 'y': None}, {'species': 'A', 'x': 3, 'y': 5},
                {'species': 'B', 'x': 2, 'y': 4}]
        result = describe_table(rows, self.spec(plot='line'))
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figure = render_table_figures(result, Path(directory))[0]
            ET.parse(Path(directory) / figure['path'])
            curves = drawn[0].axes[0].lines
            self.assertEqual(len(curves), 2)
            self.assertEqual([math.isnan(v) for v in curves[0].get_ydata()], [False, True, False])
            self.assertEqual(len(curves[1].get_ydata()), 2)
            self.assertTrue(all(math.isfinite(v) for v in curves[1].get_ydata()))
        with self.assertRaisesRegex(ValueError, 'within each group'):
            describe_table(rows + [rows[0]], self.spec(plot='line'))
        with self.assertRaisesRegex(ValueError, 'Missing x'):
            describe_table([{'species': 'A', 'x': None, 'y': 1}], self.spec(plot='line'))

    def test_category_pages_share_axes_and_do_not_drop_points(self):
        rows = [{'species': f'group-{i}', 'x': i, 'y': i * 10} for i in range(8)]
        result = describe_table(rows, self.spec())
        with tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
            figures = render_table_figures(result, Path(directory))
            self.assertEqual(len(figures), 2)
            count = 0
            for figure in figures:
                ET.parse(Path(directory) / figure['path'])
                count += len(drawn[figures.index(figure)].axes[0].collections[0].get_offsets())
                self.assertIn('same axes', figure['caption'])
            self.assertEqual(count, 8)
            self.assertEqual(drawn[0].axes[0].get_xlim(), drawn[1].axes[0].get_xlim())
            self.assertEqual(drawn[0].axes[0].get_ylim(), drawn[1].axes[0].get_ylim())
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'max_figures'):
            render_table_figures({**result, 'spec': {**result['spec'], 'max_figures': 1}}, Path(directory))

    def test_missing_group_and_all_missing_coordinates_fail_without_invention(self):
        for rows in ([{'species': None, 'x': 1, 'y': 2}], [{'species': 'A', 'x': None, 'y': 2}],
                     [{'species': True, 'x': 1, 'y': 2}]):
            with self.assertRaises(ValueError):
                describe_table(rows, self.spec())

    def test_axis_distributions_cover_all_groups_not_only_the_first_rows(self):
        rows = [{'species': 'first', 'x': i, 'y': 10 + i} for i in range(20)]
        rows += [{'species': 'second', 'x': 100 + i, 'y': 200 + i} for i in range(4)]
        rows += [{'species': 'second', 'x': None, 'y': 999},
                 {'species': 'unplotted', 'x': None, 'y': 1}]
        result = describe_table(rows, self.spec())
        summaries = result['coordinate_summaries']
        self.assertEqual([row['series_group'] for row in summaries], ['first', 'second', 'unplotted'])
        self.assertEqual(summaries[0]['x'], {'min': 0, 'q1': 4.75, 'median': 9.5, 'q3': 14.25, 'max': 19})
        self.assertEqual(summaries[1]['value'], {'min': 200, 'q1': 200.75, 'median': 201.5, 'q3': 202.25, 'max': 203})
        self.assertEqual(summaries[1]['missing_coordinate_rows'], 1)
        self.assertEqual(summaries[1]['missing_x_rows'], 1)
        self.assertEqual(summaries[1]['missing_value_rows'], 0)
        self.assertEqual(summaries[1]['missing_both_rows'], 0)
        self.assertIsNone(summaries[2]['x'])
        analysis = {'document_id': 'data', 'evidence_role': 'recomputed_from_user_supplied_data', **result}
        projected = _compact_execution_results({'supplied_analyses': [analysis]})['supplied_analyses'][0]
        self.assertEqual(projected['coordinate_summaries'], summaries)
        self.assertEqual(projected['coordinate_summaries_omitted'], 0)
        self.assertIn('not representative', projected['coordinate_summary_scope'])
        self.assertEqual(len(projected['records']), 12)
        # Degenerate/extreme values have finite exact medians, not overflow.
        huge = describe_table([{'species': 'a', 'x': 1e308, 'y': -1e308}] * 2, self.spec())
        self.assertEqual(huge['coordinate_summaries'][0]['x']['median'], 1e308)
        self.assertEqual(huge['coordinate_summaries'][0]['value']['median'], -1e308)

    def test_large_group_summary_projection_reports_omissions(self):
        result = describe_table([{'species': str(i), 'x': i, 'y': i} for i in range(30)], self.spec())
        projected = _compact_execution_results({'supplied_analyses': [
            {'document_id': 'data', 'evidence_role': 'recomputed_from_user_supplied_data', **result}]})['supplied_analyses'][0]
        self.assertEqual(len(projected['coordinate_summaries']), 24)
        self.assertEqual(projected['coordinate_summaries_omitted'], 6)

class CoordinateAssociationTests(unittest.TestCase):
    def spec(self, **changes):
        return TableSpec.from_config({'value_columns': ['y'], 'observation_unit': 'one supplied coordinate pair',
            'plot': 'scatter', 'mode': 'values', 'x_column': 'x', 'association': 'pearson', **changes})

    def test_explicit_method_only_and_nondefault_combination_validation(self):
        rows = [{'x': 1, 'y': 2}, {'x': 2, 'y': 5}, {'x': 4, 'y': 3}]
        ordinary = describe_table(rows, self.spec(association='none'))
        self.assertNotIn('association', ordinary['spec'])
        self.assertNotIn('association', ordinary['coordinate_summaries'][0])
        associated = describe_table(rows, self.spec())
        self.assertEqual(associated['records'], ordinary['records'])
        self.assertAlmostEqual(associated['coordinate_summaries'][0]['association']['coefficient'], 1 / 7)
        for invalid in ('spearman', [], None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.spec(association=invalid)
        with self.assertRaisesRegex(ValueError, 'coordinates'):
            TableSpec(('y',), 'one observation', association='pearson')

    def test_groups_are_not_pooled_and_missing_coordinates_are_jointly_omitted(self):
        rows = [{'g': group, 'x': x, 'y': y} for group, values in
            (('A', [(1, 3), (2, 2), (3, 1)]), ('B', [(10, 12), (11, 11), (12, 10)])) for x, y in values]
        rows += [{'g': 'A', 'x': None, 'y': 100}, {'g': 'A', 'x': 99, 'y': None}]
        result = describe_table(rows, self.spec(group_column='g', missing='omit'))
        a, b = result['coordinate_summaries']
        self.assertEqual((a['plotted_rows'], a['missing_coordinate_rows'], a['missing_x_rows'], a['missing_value_rows']), (3, 2, 1, 1))
        self.assertAlmostEqual(a['association']['coefficient'], -1)
        self.assertAlmostEqual(b['association']['coefficient'], -1)
        self.assertEqual(len(result['coordinate_summaries']), 2)
        self.assertEqual(len(result['records']), 8)
        text = table_values_markdown(result)
        self.assertIn('groups are not pooled', text)
        self.assertIn('not a paired mean difference', text)
        self.assertIn('| A | x | y | 3 | 2 | -1 | computed |', text)

    def test_small_constant_empty_and_extreme_pairs_are_not_fabricated(self):
        cases = [([{'x': 1, 'y': 2}], 'insufficient_complete_pairs', None),
            ([{'x': 1, 'y': 2}, {'x': 1, 'y': 3}], 'constant_coordinate', None),
            ([{'x': 1, 'y': 2}, {'x': 2, 'y': 2}], 'constant_coordinate', None),
            ([{'x': -1e308, 'y': 1e308}, {'x': 0, 'y': 0}, {'x': 1e308, 'y': -1e308}], 'computed', -1),
            ([{'x': 1e16 + offset, 'y': y} for offset, y in ((0, 1), (2, 2), (6, 3))], 'computed', math.sqrt(27 / 28)),
            ([{'x': 1e-300, 'y': -1e-300}, {'x': 2e-300, 'y': -2e-300}], 'computed', -1)]
        for rows, status, expected in cases:
            with self.subTest(rows=rows):
                value = describe_table(rows, self.spec())['coordinate_summaries'][0]['association']
                self.assertEqual(value['status'], status)
                if expected is None:
                    self.assertIsNone(value['coefficient'])
                else:
                    self.assertTrue(math.isfinite(value['coefficient']))
                    self.assertAlmostEqual(value['coefficient'], expected)
        result = describe_table([{'g': 'A', 'x': None, 'y': 3}, {'g': 'B', 'x': 1, 'y': 2}],
                                self.spec(group_column='g', missing='omit'))
        self.assertIsNone(result['coordinate_summaries'][0]['association']['coefficient'])
        self.assertEqual(result['coordinate_summaries'][0]['plotted_rows'], 0)

    def test_writing_projection_carries_results_not_an_inferred_relation(self):
        result = describe_table([{'x': x, 'y': 2 * x} for x in range(1, 16)], self.spec())
        view = _compact_execution_results({'supplied_analyses': [{'document_id': 'data', **result}]})['supplied_analyses'][0]
        self.assertTrue(view['records_truncated'])
        self.assertEqual(view['coordinate_summaries'][0]['plotted_rows'], 15)
        self.assertAlmostEqual(view['coordinate_summaries'][0]['association']['coefficient'], 1)
        self.assertIn('descriptive complete-pair Pearson', view['coordinate_summary_scope'])

    def test_cli_config_execution_recovery_moving_and_tampering_use_same_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'points.csv'
            source.write_text('x,y\n1,2\n2,5\n4,3\n')
            argv = ['start', '--kind', 'data_analysis', '--goal', 'Describe linear association',
                '--data-file', str(source), '--value-column', 'y', '--x-column', 'x',
                '--data-mode', 'values', '--data-plot', 'scatter', '--data-association', 'pearson',
                '--observation-unit', 'one supplied pair', '--output-root', str(root / 'tasks'), '--prepare-only']
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                config = prepare_start(build_parser().parse_args(argv))
            self.assertEqual(research_defaults(['research-session', '--config', str(config)])['data_association'], 'pearson')
            with patch('simple_ar.cli.main._optional_research_llm_client', side_effect=AssertionError('No API')), contextlib.redirect_stdout(io.StringIO()):
                main(['research-session', '--config', str(config)])
                package = next(config.parent.glob('sessions/*/attempts/data_analysis-*/analysis.json'))
                before = package.read_bytes()
                source.unlink()
                main(['research-session', '--session-root', str(package.parents[2]), '--model', 'env'])
                self.assertEqual(package.read_bytes(), before)
            moved = root / 'moved'
            shutil.copytree(package.parent, moved)
            restored, _, _ = load_analysis_package(moved / 'analysis.json')
            self.assertAlmostEqual(restored['coordinate_summaries'][0]['association']['coefficient'], 1 / 7)
            rebuild(moved / 'analysis.json')
            payload = json.loads((moved / 'analysis.json').read_text())
            payload['coordinate_summaries'][0]['association']['coefficient'] = .8
            (moved / 'analysis.json').write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, 'axis summaries'):
                load_analysis_package(moved / 'analysis.json')


if __name__ == '__main__':
    unittest.main()
