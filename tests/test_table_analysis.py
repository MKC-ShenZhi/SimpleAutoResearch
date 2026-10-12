import contextlib
import io
import json
import shutil
import statistics
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from simple_ar.app.research_application import create_session, load_session, ResearchApplicationServices
from simple_ar.research.workflow_contracts import ResearchBrief
from simple_ar.result_analysis.table import (
    TableSpec, parse_table, describe_table, rebuild, copy_analysis_package,
    load_analysis_package, table_values_markdown,
)
from simple_ar.report.narrative import _compact_execution_results
from simple_ar.cli.main import main
from simple_ar.cli.parser import build_parser
from simple_ar.cli.research_config import research_defaults
from simple_ar.cli.start import prepare_start


class TableAnalysisTests(unittest.TestCase):
    def test_configuration_preserves_model_defaults_and_explicit_options(self):
        minimal = {'value_columns': ['value'], 'observation_unit': 'one run', 'file': 'values.csv'}
        self.assertEqual(TableSpec.from_config(minimal), TableSpec(('value',), 'one run'))
        selected = TableSpec(('y',), 'one row', 'group', 'accuracy', mode='values', missing='omit',
            width='column', max_mb=3, max_figures=4, plot='scatter', x_column='x', x_unit='seconds',
            max_points=20, series_layout='shared', attribution='supplied measurement', association='pearson')
        self.assertEqual(TableSpec.from_config(selected.to_config()), selected)
        for options in ({'value_columns': 'value'}, {'max_mb': 0}, {'missing': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                TableSpec.from_config({**minimal, **options})

    def test_descriptive_observations_have_explicit_counts_and_sample_std(self):
        rows = parse_table('group,value\nA,1\nA,3\nB,-2\n', '.csv')
        result = describe_table(rows, TableSpec(('value',), 'one run', 'group'))
        a, b = result['records']
        self.assertEqual(a['count'], 2)
        self.assertEqual(a['mean'], 2)
        self.assertAlmostEqual(a['sample_std'], 2 ** .5)
        self.assertIsNone(b['sample_std'])
        self.assertNotIn('execution_status', result)
        self.assertNotIn('significance', result)
        # Native and structured consumers share the computation/snapshot owner,
        # not a model's mental aggregate or a second table implementation.
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.tools.workspace import WorkspaceToolGateway
        from simple_ar.tools.analysis import AnalysisToolGateway, analysis_tool_specs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'supplied'
            source.mkdir()
            table = source / 'records.csv'
            original = 'group,value\nA,1\nA,3\nB,-2\n'
            table.write_text(original)
            workspace = WorkspaceToolGateway(root, {'source0': source}, BudgetLedger({}))
            gateway = AnalysisToolGateway(root, workspace)
            self.assertEqual({row.name for row in analysis_tool_specs()}, {'inspect_table', 'analyze_table'})
            preview = gateway.call('inspect_table', {'path': str(table)}, identity='inspect')
            self.assertTrue(preview.ok, preview.error)
            self.assertEqual(preview.data['row_count'], 3)
            self.assertEqual(preview.data['column_summary'][0]['distinct_nonmissing'], 2)
            arguments = {'path': str(table), 'spec': {'value_columns': ['value'],
                'observation_unit': 'one supplied row', 'group_column': 'group'}}
            computed = gateway.call('analyze_table', arguments, identity='computed')
            self.assertTrue(computed.ok, computed.error)
            package = workspace.resolve(computed.data['analysis'])
            saved = json.loads(package.read_text())
            self.assertEqual(saved['records'], result['records'])
            self.assertEqual(load_analysis_package(package)[0]['records'], result['records'])
            self.assertEqual(table.read_text(), original)
            table.write_text('group,value\nchanged,900\n')
            with patch('simple_ar.tools.analysis.analyze_table_capability') as recalculate:
                cached = gateway.call('analyze_table', arguments, identity='computed')
                self.assertEqual(cached.data, computed.data)
                recalculate.assert_not_called()
            self.assertEqual((package.parent / 'input.csv').read_text(), original)
            changed = gateway.call('analyze_table', {**arguments, 'spec': {**arguments['spec'], 'missing': 'omit'}}, identity='computed')
            self.assertFalse(changed.ok)
            self.assertIn('settings changed', changed.error)
            package.write_text('{}')
            modified = gateway.call('analyze_table', arguments, identity='computed')
            self.assertFalse(modified.ok)
            self.assertIn('was modified', modified.error)
            private = gateway.call('inspect_table', {'path': str(source / '.env.csv')}, identity='private')
            self.assertFalse(private.ok)

    def test_missing_is_not_zero_and_omissions_are_per_column(self):
        rows = parse_table('group,x,y\nA,1,\nA,,4\n', '.csv')
        with self.assertRaisesRegex(ValueError, 'Missing'):
            describe_table(rows, TableSpec(('x', 'y'), 'one observation', 'group'))
        result = describe_table(rows, TableSpec(('x', 'y'), 'one observation', 'group', missing='omit'))
        self.assertEqual([r['mean'] for r in result['records']], [1, 4])
        self.assertEqual([r['missing'] for r in result['records']], [1, 1])
        with self.assertRaisesRegex(ValueError, 'No numeric'):
            describe_table([{'x': None}], TableSpec(('x',), 'one row', missing='omit'))

    def test_summary_values_are_not_averaged_or_given_inferred_errors(self):
        spec = TableSpec(('mean',), 'one supplied summary', 'method', mode='values')
        result = describe_table(parse_table('[{"method":"A","mean":3.1},{"method":"B","mean":2.2}]', '.json'), spec)
        self.assertEqual(result['records'][0]['value'], 3.1)
        self.assertNotIn('sample_std', result['records'][0])
        with self.assertRaisesRegex(ValueError, 'unique labels'):
            describe_table([{'method': 'A', 'mean': 1}, {'method': 'A', 'mean': 3}], spec)

    def test_bad_input_fails_explicitly_across_formats(self):
        for text, suffix in [('x,x\n1,2', '.csv'), ('x\ty\n1', '.tsv'), ('x\n', '.csv'),
                             ('[{"x":1,"x":2}]', '.json'), ('{"x":[1]}', '.json'),
                             ('[{"x":1},{"y":2}]', '.json'), ('[{"x":NaN}]', '.json'),
                             ('[{"x":[1]}]', '.json')]:
            with self.subTest(text=text), self.assertRaises((ValueError, json.JSONDecodeError)):
                parse_table(text, suffix)
        for value in ('NaN', 'inf', True, 'word'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                describe_table([{'x': value}], TableSpec(('x',), 'one row'))
        with self.assertRaisesRegex(ValueError, 'not found'):
            describe_table([{'id': 1}], TableSpec(('value',), 'one row'))

    def test_data_actions_cannot_be_inserted_into_a_research_plan(self):
        from unittest.mock import MagicMock
        from simple_ar.research.task_plan import TaskPlanRequest, build_task_plan, default_task_steps
        request = TaskPlanRequest(task_kind='survey', goal='Review sources', request_text='Review sources',
                                  config={'research_materials_only': True, 'research_local_documents': ['paper.md']},
                                  requested_outputs=('summary',))
        client = MagicMock(model='fake')
        client.ask_json.return_value = {'steps': [*default_task_steps(request), {'action': 'data_analysis'}]}
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError, 'Data actions require'):
            build_task_plan(replace(request, use_llm=True, llm_client=client))

    def test_snapshot_survives_changed_deleted_input_and_moved_rebuild(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'observations.csv'
            source.write_text('group,value\nA,1\nA,3\nB,-2\n', encoding='utf-8')
            config = {'research_task_kind': 'data_analysis', 'data_analysis': {
                'file': str(source), 'value_columns': ['value'], 'group_column': 'group', 'observation_unit': 'one run'}}
            brief = ResearchBrief(request_text='Describe supplied values', requested_outputs=('data_analysis',),
                                  asset_requests=({'locator': str(source), 'kind': 'file', 'role': 'dataset'},))
            app = create_session(brief, root=root / 'session', services=ResearchApplicationServices(config=config))
            view = app.advance(max_actions=2)
            self.assertEqual(view.next_action, 'data_analysis', view.status_reason)
            source.write_text('group,value\nA,999\n', encoding='utf-8')
            source.unlink()
            app = load_session(root / 'session')
            view = app.advance()
            self.assertEqual(view.status, 'completed', view.status_reason)
            self.assertEqual([step['action'] for step in view.work_plan['steps']], ['data_ingest', 'data_analysis'])
            self.assertNotIn('experiment', view.state_refs)
            ref = view.state_refs['data_analysis']
            result = app.controller.store.read_json(ref)
            self.assertEqual(result['records'][0]['mean'], 2)
            output = (root / 'session' / ref.path).parent
            self.assertEqual((output / 'input.csv').read_bytes(), b'group,value\nA,1\nA,3\nB,-2\n')
            for figure in result['figures']:
                ET.parse(output / figure['path'])
            moved = root / 'delivery'
            shutil.copytree(output, moved)
            shutil.rmtree(root / 'session')
            rebuild(moved / 'analysis.json')
            (moved / 'input.csv').write_text('group,value\nA,999\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'no longer matches'):
                rebuild(moved / 'analysis.json')

    def test_plot_tasks_share_config_no_model_recovery_move_and_writing_import(self):
        cases = [
            ('bar', 'values', 'label\tscore\nA\t1\nB\t2\n', '.tsv', ['score'], 'label', []),
            ('box', 'observations', '[{"label":"A","score":1},{"label":"A","score":5}]', '.json', ['score'], 'label', []),
            ('heatmap', 'values', '[{"label":"A","score":1,"b":null},{"label":"B","score":2,"b":3}]',
             '.json', ['score', 'b'], 'label', []),
            ('line', 'values', 'x\tscore\n2\t1\n1\t3\n3\t\n', '.tsv', ['score'], '', ['--x-column', 'x', '--x-unit', 'iteration']),
            ('scatter', 'values', '[{"label":"A","x":1,"score":2},{"label":"B","x":1,"score":3}]',
             '.json', ['score'], 'label', ['--x-column', 'x']),
        ]
        for plot, mode, text, suffix, columns, group, extras in cases:
            with self.subTest(plot=plot), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / ('table with spaces' + suffix)
                source.write_text(text, encoding='utf-8')
                raw = source.read_bytes()
                argv = ['start', '--kind', 'data_analysis', '--goal', 'Describe supplied data',
                        '--data-file', str(source), '--observation-unit', 'one supplied row',
                        '--data-mode', mode, '--data-plot', plot, '--data-missing', 'omit',
                        '--output-root', str(root / 'runs'), '--prepare-only', *extras]
                for column in columns:
                    argv.extend(['--value-column', column])
                if group:
                    argv.extend(['--group-column', group])
                with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()):
                    config = prepare_start(build_parser().parse_args(argv))
                defaults = research_defaults(['research-session', '--config', str(config)])
                self.assertEqual((defaults['data_file'], defaults['data_mode'], defaults['data_plot']),
                                 (str(source), mode, plot))
                if extras:
                    self.assertEqual(defaults['x_column'], 'x')
                with patch('simple_ar.cli.main._optional_research_llm_client', side_effect=AssertionError('No API/model expected')), contextlib.redirect_stdout(io.StringIO()):
                    self.assertIsNone(main(['research-session', '--config', str(config)]))
                    package = next(config.parent.glob('sessions/*/attempts/data_analysis-*/analysis.json'))
                    frozen = package.read_bytes()
                    source.unlink()
                    self.assertIsNone(main(['research-session', '--session-root', str(package.parents[2]), '--model', 'env']))
                    self.assertEqual(package.read_bytes(), frozen)
                original = json.loads(frozen)
                moved = root / 'moved'
                shutil.copytree(package.parent, moved)
                # Explicit rebuilding may use a user-chosen result filename.
                path = (moved / 'analysis.json').rename(moved / 'renamed-result.json')
                rebuild(path)
                imported = copy_analysis_package(path, root / 'writing-input')
                rebuilt = json.loads(path.read_text())
                self.assertEqual(TableSpec.from_config(imported['spec']), TableSpec.from_config(original['spec']))
                self.assertEqual(rebuilt['spec'], original['spec'])
                for field in ('records', 'figures', 'observation_summaries', 'coordinate_summaries'):
                    self.assertEqual(rebuilt.get(field), original.get(field))
                    self.assertEqual(imported.get(field), original.get(field))
                self.assertEqual((moved / original['source']['path']).read_bytes(), raw)
                self.assertEqual((root / 'writing-input' / imported['source']['path']).read_bytes(), raw)
                for figure in original['figures']:
                    self.assertEqual((package.parent / figure['path']).read_bytes(), (moved / figure['path']).read_bytes())
                    self.assertEqual((package.parent / figure['path']).read_bytes(), (root / 'writing-input' / figure['path']).read_bytes())
                    for kind, asset in figure['exports'].items():
                        self.assertGreater((moved / asset).stat().st_size, 100)
                        self.assertTrue((root / 'writing-input' / asset).is_file())
                        self.assertIn(f'[{kind.upper()}]({asset})', (moved / 'analysis.md').read_text())
    def test_guided_analysis_rejects_unrelated_options_before_saving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.csv'
            source.write_text('x\n1\n', encoding='utf-8')
            base = ['start', '--kind', 'data_analysis', '--goal', 'Describe', '--data-file', str(source),
                    '--value-column', 'x', '--observation-unit', 'one row', '--output-root', str(root / 'runs'), '--prepare-only']
            for extra in (['--document', str(source)], ['--sources', 'search'], ['--validate', 'python test.py'],
                          ['--command', 'python', 'train.py'], ['--template', 'experiment'], ['--data-max-figures', '0']):
                with self.subTest(extra=extra), patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                    prepare_start(build_parser().parse_args([*base, *extra]))
            self.assertFalse((root / 'runs').exists())

    def test_byte_snapshot_keeps_bom_crlf_and_quoted_multiline_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = b'\xef\xbb\xbfgroup,value\r\n"line one\r\nline two",2\r\n'
            source = root / 'source.csv'
            source.write_bytes(raw)
            brief = ResearchBrief(request_text='Describe', requested_outputs=('data_analysis',))
            config = {'research_task_kind': 'data_analysis', 'data_analysis': {
                'file': str(source), 'value_columns': ['value'], 'group_column': 'group', 'observation_unit': 'one observation'}}
            app = create_session(brief, root=root / 'session', services=ResearchApplicationServices(config=config))
            view = app.advance(max_actions=10)
            self.assertEqual(view.status, 'completed', view.status_reason)
            result = app.controller.store.read_json(view.state_refs['data_analysis'])
            output = (root / 'session' / view.state_refs['data_analysis'].path).parent
            self.assertEqual((output / 'input.csv').read_bytes(), raw)
            self.assertEqual(result['records'][0]['group'], 'line one\r\nline two')

    def test_figures_keep_all_categories_negative_values_and_xml_escaping(self):
        from simple_ar.report.figures import render_table_figures
        from tests import captured_data_figures
        rows = [{'group': f'A<& very long category {i}' * 3, 'value': i - 10} for i in range(25)]
        for width in ('column', 'wide'):
            with self.subTest(width=width), tempfile.TemporaryDirectory() as directory, captured_data_figures() as drawn:
                result = describe_table(rows, TableSpec(('value',), 'one run', 'group', width=width))
                figures = render_table_figures(result, Path(directory))
                self.assertEqual(len(figures), 3)
                groups = []
                for figure in figures:
                    tree = ET.parse(Path(directory) / figure['path'])
                    self.assertIn('A<&', ''.join(tree.getroot().itertext()))
                    groups.extend(figure['encoding']['groups'])
                self.assertEqual(groups, [row['group'] for row in rows])
                self.assertEqual(sum(len(fig.axes[0].patches) for fig in drawn), 25)
                self.assertTrue(any(bar.get_width() < 0 for fig in drawn for bar in fig.axes[0].patches))
        with tempfile.TemporaryDirectory() as directory:
            result = describe_table(rows, TableSpec(('value',), 'one run', 'group', max_figures=1))
            with self.assertRaisesRegex(ValueError, 'physical max_figures'):
                render_table_figures(result, Path(directory))
            self.assertFalse((Path(directory) / 'figures').exists())


class ObservationDistributionTests(unittest.TestCase):
    def test_all_groups_and_missing_cells_are_used_not_the_preview_prefix(self):
        rows = [{"g": "A", "x": value, "y": None if value == 1 else 2 * value}
                for value in range(1, 14)] + [{"g": "B", "x": -10, "y": 100}]
        result = describe_table(rows, TableSpec(("x", "y"), "one specimen", "g", missing="omit"))
        a, ay, b, by = result["observation_summaries"]
        self.assertEqual(a["value"], {"min": 1, "q1": 4, "median": 7, "q3": 10, "max": 13})
        self.assertEqual(ay["missing"], 1)
        self.assertEqual(ay["value"]["median"], statistics.median(range(4, 27, 2)))
        self.assertEqual(b["value"], dict.fromkeys(("min", "q1", "median", "q3", "max"), -10))
        text = table_values_markdown(result)
        self.assertIn("| B | x | 1 | 0 | -10 | -10 | -10 | -10 | -10 |", text)
        self.assertIn("not confidence intervals", text)
        self.assertNotIn("significant", text)
        view = _compact_execution_results({"supplied_analyses": [{"document_id": "data", **result}]})["supplied_analyses"][0]
        self.assertEqual(view["observation_summaries"], result["observation_summaries"])
        self.assertIn("not representative", view["observation_summary_scope"])

    def test_summary_inputs_have_no_fabricated_distribution_and_extremes_are_finite(self):
        values = describe_table([{"g": "A", "x": 3}], TableSpec(("x",), "one mean", "g", mode="values"))
        self.assertNotIn("observation_summaries", values)
        result = describe_table([{"x": 1e308}, {"x": 1e308}], TableSpec(("x",), "one value"))
        self.assertEqual(result["observation_summaries"][0]["value"]["median"], 1e308)

    def test_legacy_import_recomputes_and_new_summary_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = '[{"x":1},{"x":3},{"x":8},{"x":10}]'
            (root / "input.json").write_text(raw)
            result = describe_table(json.loads(raw), TableSpec(("x",), "one observation"))
            result["source"] = {"path": "input.json", "kind": "user_data"}
            path = root / "analysis.json"
            path.write_text(json.dumps(result))
            copied = copy_analysis_package(path, root / "moved")
            self.assertEqual(copied["observation_summaries"], result["observation_summaries"])
            result["observation_summaries"][0]["value"]["median"] = 99
            path.write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "distributions"):
                load_analysis_package(path)
            result.pop("observation_summaries")
            path.write_text(json.dumps(result))
            before = path.read_bytes()
            restored, _, _ = load_analysis_package(path)
            self.assertEqual(restored["observation_summaries"][0]["value"]["median"], 5.5)
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
