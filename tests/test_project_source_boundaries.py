"""Static project consumers must not inspect installed dependency trees."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.code_task.analysis.entrypoints import analyze_entrypoint_debuggability
from simple_ar.code_task.analysis.interfaces import (
    find_local_api_mismatches, find_return_contract_mismatches, project_api_contract,
)
from simple_ar.code_task.analysis.resource_static import analyze_resource_risks
from simple_ar.code_task.execution.failure import _source_signal_files as failure_sources
from simple_ar.code_task.execution.failure_graph import _source_signal_files as graph_sources
from simple_ar.code_task.analysis.index import build_codebase_index, project_python_files
from simple_ar.code_task.generation.generated_project_repair import _compile_project
from simple_ar.code_task.generation.review import review_generated_project
from simple_ar.code_task.review_pipeline import build_review_index
from simple_ar.code_task.tools.gateway import LocalCodeTaskToolGateway


class ProjectSourceBoundaryTests(unittest.TestCase):
    def test_search_and_generated_checks_retain_tolerant_utf8_decoding(self):
        for encoding, prose in (('gbk', '中文'), ('latin-1', 'café')):
            for suffix in ('.md', '.json', '.toml'):
                with self.subTest(encoding=encoding, suffix=suffix), tempfile.TemporaryDirectory() as folder:
                    run = Path(folder)
                    root = run / 'code_task/workspace'
                    root.mkdir(parents=True)
                    (root / 'main.py').write_text('print("measured")\n')
                    documents = root / '.project'
                    documents.mkdir()
                    payload = (prose + ' score\nmetrics["score"] = 0.0\n').encode(encoding)
                    (documents / ('results' + suffix)).write_bytes(payload)
                    result = LocalCodeTaskToolGateway(run).call('search_code_task_code', {'query': 'score'})
                    self.assertEqual(result.status, 'ok')
                    self.assertEqual([row['preview'] for row in result.data['matches']],
                        payload.decode('utf-8', errors='replace').splitlines())
                    report = review_generated_project(project_dir=root, code_artifacts={}, resource_plan={},
                        result_schema={'required_metrics': ['score']}, use_llm=False)
                    categories = {row['category'] for row in report['findings']}
                    self.assertNotIn('metric_not_visible', categories)
                    self.assertIn('required_metric_default_fill', categories)
                    # Ignoring invalid bytes can join an identifier; replacing
                    # them cannot. Preserve each consumer's original choice.
                    (documents / ('results' + suffix)).write_bytes(b'sc\xffore\n')
                    report = review_generated_project(project_dir=root, code_artifacts={}, resource_plan={},
                        result_schema={'required_metrics': ['score']}, use_llm=False)
                    self.assertFalse(any(row['category'] == 'metric_not_visible' for row in report['findings']))
                    result = LocalCodeTaskToolGateway(run).call('search_code_task_code', {'query': 'score'})
                    self.assertEqual(result.data['matches'], [])

    def test_review_visibility_distinguishes_directory_rules_from_file_names(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'code_task/workspace'
            root.mkdir(parents=True)
            visible = {'.config/settings.toml', '.project/nested/module.py',
                'files/env', 'files/dist', 'files/build', 'files/package.dist-info', 'files/package.egg-info'}
            blocked = {'env', 'dist', 'build', 'package.dist-info', 'package.egg-info',
                '.venv', '.ipynb_checkpoints', '.env.private', 'private', 'denied'}
            for relative in visible | {name + '/hidden.py' for name in blocked}:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('VALUE = 1\n')
            import simple_ar.code_task.review_pipeline as pipeline
            with patch.object(pipeline, 'read_code_task_text', wraps=pipeline.read_code_task_text) as read:
                index = build_review_index(root, excluded_names=frozenset({'private'}),
                    path_filter=lambda path: path.name != 'denied')
            self.assertEqual({row['path'] for row in index['files']}, visible)
            self.assertEqual({call.args[0].relative_to(root).as_posix() for call in read.call_args_list}, visible)
            # The frozen copy/index defaults must not inherit review visibility.
            self.assertFalse(any(row['path'].startswith(('.config/', '.project/'))
                for row in build_codebase_index(root)['files']))
            result = LocalCodeTaskToolGateway(Path(folder)).call('search_code_task_code', {'query': 'VALUE'})
            matches = {row['path'] for row in result.data['matches']}
            self.assertTrue({'.config/settings.toml', '.project/nested/module.py'}.issubset(matches))
            self.assertFalse(any(path.startswith(('.env', '.venv/')) for path in matches))
            (root / '.env.service.example').write_text('EXAMPLE_PORT=1234\n')
            template = LocalCodeTaskToolGateway(Path(folder)).call('search_code_task_code',
                {'query': 'EXAMPLE_PORT', 'extensions': ['.example']})
            self.assertEqual([row['path'] for row in template.data['matches']], ['.env.service.example'])

    def test_hidden_directory_visibility_never_authorizes_linked_targets(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            root = run / 'code_task/workspace'
            hidden = root / '.config'
            hidden.mkdir(parents=True)
            outside = run / 'outside'
            outside.mkdir()
            (outside / 'secret.py').write_text('SECRET_VALUE = 1\n')
            try:
                (hidden / 'linked.py').symlink_to(outside / 'secret.py')
                (root / '.linked').symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            self.assertEqual(build_review_index(root)['files'], [])
            result = LocalCodeTaskToolGateway(run).call('search_code_task_code', {'query': 'SECRET_VALUE'})
            self.assertEqual(result.status, 'ok')
            self.assertEqual(result.data['matches'], [])

    def test_review_and_reopened_search_use_project_sources_not_installed_metrics(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            root = run / 'code_task/workspace'
            root.mkdir(parents=True)
            (root / 'main.py').write_text('print("measured")\n')
            source = root / 'environment'
            source.mkdir()
            (source / 'metrics.py').write_text('metrics["score"] = 0.0\n')
            dependency = root / 'previous-attempt/runtime'
            dependency.mkdir(parents=True)
            (dependency / 'pyvenv.cfg').write_text('home = /usr/bin\n')
            (dependency / 'metrics.py').write_text('metrics["dependency_score"] = 0.0\n')
            (root / '.env.private').write_text('dependency_score=secret\n')
            result = LocalCodeTaskToolGateway(run).call('search_code_task_code', {'query': 'metrics'})
            self.assertEqual(result.status, 'ok')
            self.assertEqual([row['path'] for row in result.data['matches']], ['environment/metrics.py'])
            hidden = LocalCodeTaskToolGateway(run).call('search_code_task_code', {'query': 'dependency_score'})
            self.assertEqual(hidden.data['matches'], [])
            index = build_review_index(root)
            self.assertEqual({row['path'] for row in index['files']}, {'main.py', 'environment/metrics.py'})
            report = review_generated_project(project_dir=root, code_artifacts={},
                result_schema={'required_metrics': ['dependency_score']}, resource_plan={}, use_llm=False)
            fills = [row for row in report['findings'] if row['category'] == 'required_metric_default_fill']
            self.assertEqual(len(fills), 1)
            self.assertIn('environment/metrics.py', fills[0]['summary'])
            self.assertTrue(any(row['category'] == 'metric_not_visible' for row in report['findings']))

    def project(self, root):
        def write(relative, text):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        write('environment/domain.py', 'def transform(value):\n    return value\n')
        write('environment/consumer.py', 'from . import domain\ndomain.missing_attribute()\n')
        write('main.py', 'def main():\n    return "missing_attribute"\n')
        write('search.py', 'def run(model, values):\n    for value in values:\n        model.fit(value)\n')
        # An arbitrary name identifies the environment by its standard marker.
        write('previous-attempt/generated-runtime/pyvenv.cfg', 'home = /usr/bin\n')
        write('previous-attempt/generated-runtime/domain.py', 'def transform(value):\n    return value\n')
        write('previous-attempt/generated-runtime/consumer.py',
              'from . import domain\ndomain.missing_attribute()\n')
        write('previous-attempt/generated-runtime/main.py',
              'try:\n    run()\nexcept Exception:\n    print("suppressed")\n')
        write('previous-attempt/generated-runtime/search.py',
              'def run(model, values):\n    for value in values:\n        for other in values:\n            model.fit(other)\n')
        write('previous-attempt/generated-runtime/broken.py', 'def invalid(\n')
        return root

    def test_interfaces_ignore_dependencies_but_keep_real_source_named_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            contracts = project_api_contract(root)
            self.assertIn('environment/domain.py', contracts)
            self.assertFalse(any('generated-runtime/' in path for path in contracts))
            findings = find_local_api_mismatches(root)
            self.assertEqual({row['caller'] for row in findings}, {'environment/consumer.py'})
            selected = project_api_contract(root, relevant_paths=['environment/domain.py'])
            self.assertEqual(list(selected), ['environment/domain.py'])

    def test_entrypoint_and_resource_checks_do_not_blame_dependencies_or_spend_their_cap(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            self.assertEqual(analyze_entrypoint_debuggability(root)['findings'], [])
            result = analyze_resource_risks(root)
            self.assertEqual(result['total_fit_call_count'], 1)
            self.assertEqual(result['max_fit_loop_depth'], 1)
            self.assertEqual([row['path'] for row in result['files']], ['search.py'])
            self.assertEqual(analyze_resource_risks(root, max_files=4)['total_fit_call_count'], 1)

    def test_failure_ranking_and_generated_repair_keep_the_same_source_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = self.project(Path(folder))
            for search in (failure_sources, graph_sources):
                paths = search(root, "AttributeError: object has no attribute 'missing_attribute'")
                self.assertIn('environment/consumer.py', paths)
                self.assertFalse(any('generated-runtime/' in path for path in paths))
            paths = [path.relative_to(root).as_posix() for path in project_python_files(root)]
            self.assertIn('environment/domain.py', paths)
            self.assertFalse(any('generated-runtime/' in path for path in paths))
            self.assertEqual(_compile_project(root), [])

    def test_shared_ast_scan_retains_return_contract_and_open_export_behavior(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'producer.py').write_text('def produce() -> dict:\n    return {}\n')
            (root / 'consumer.py').write_text(
                'from producer import produce\ndef consume(rows: list):\n    return rows\n'
                'def run():\n    result = produce()\n    consume(result)\n')
            (root / 'open.py').write_text('from external import *\n')
            (root / 'uses_open.py').write_text('from open import unseen_export\n')
            self.assertEqual(find_local_api_mismatches(root), [])
            findings = find_return_contract_mismatches(root)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]['caller'], 'consumer.py')
            self.assertEqual(findings[0]['producer'], 'producer.produce')


if __name__ == '__main__':
    unittest.main()
