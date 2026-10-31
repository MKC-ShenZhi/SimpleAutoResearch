"""Static project consumers must not inspect installed dependency trees."""
from pathlib import Path
import json
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
from simple_ar.code_task.review import review_code_task_changes
from simple_ar.code_task.runtime.state import code_task_workspace_boundary
from simple_ar.research.preparation import inspect_execution_entry, inspect_project_preparation


class ProjectSourceBoundaryTests(unittest.TestCase):
    def test_workspace_keeps_dotenv_private_while_code_task_keeps_examples(self):
        from simple_ar.tools.workspace import WorkspaceToolGateway, resolve_source_mount
        from simple_ar.core.budget import BudgetLedger
        with tempfile.TemporaryDirectory() as folder:
            task = Path(folder) / 'task'
            project = task / 'workspace/project'
            project.mkdir(parents=True)
            (project / 'README.md').write_text('Public instructions')
            code_root = task / 'code_task/workspace'
            code_root.mkdir(parents=True)
            gateway = WorkspaceToolGateway(task, {}, BudgetLedger())
            for name in ('.env', '.env.example', '.env.service.example'):
                for root in (project, code_root, Path(folder)):
                    (root / name).write_text('EXAMPLE_PORT=1234')
                with self.subTest(name=name):
                    result = gateway.call('read_file', {'path': f'workspace/project/{name}'}, identity='read')
                    self.assertEqual(result.status, 'blocked')
                    with self.assertRaisesRegex(ValueError, 'Private'):
                        resolve_source_mount(Path(folder) / name)
                    with self.assertRaisesRegex(ValueError, 'Private'):
                        WorkspaceToolGateway(task, {'example': project / name}, BudgetLedger())
                    result = LocalCodeTaskToolGateway(task).call('read_code_task_file_range', {'path': name})
                    self.assertEqual(result.status, 'blocked' if name == '.env' else 'ok')
            for recursive in (False, True):
                result = gateway.call('list_directory', {'path': 'workspace/project', 'recursive': recursive},
                                      identity='listing')
                self.assertEqual(result.status, 'ok')
                self.assertNotIn('.env', json.dumps(result.data))
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                self.assertFalse(path.is_relative_to(project) and path.name.startswith('.env'))
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', guarded_open):
                result = gateway.call('inspect_project', {'path': 'workspace/project'}, identity='inspection')
            self.assertEqual(result.status, 'ok')
            facts = json.loads(Path(result.data['inspection_record']).read_text())
            self.assertEqual(facts['source_file_paths'], ['README.md'])
            self.assertEqual(facts['excerpts'][0]['text'], 'Public instructions')
            (code_root / '.env.directory.example').mkdir()
            (code_root / '.env.directory.example/secret.py').write_text('PRIVATE_SENTINEL')
            self.assertEqual({row['path'] for row in build_review_index(code_root, access_boundary=code_root)['files']},
                             {'.env.example', '.env.service.example'})
            private = Path(folder) / '.ssh/project'
            private.mkdir(parents=True)
            for index in (None, {'files': [{'path': 'README.md'}]}):
                with self.assertRaisesRegex(ValueError, 'Private'):
                    inspect_project_preparation(private, index=index)
            with self.assertRaisesRegex(ValueError, 'Private'):
                inspect_execution_entry({'code_task': {'code_root': str(private)}})

    def test_historical_review_restores_original_grants_without_rewriting_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder) / 'host/task'
            project = run / 'code_task/workspace/packages/project'
            project.mkdir(parents=True)
            (project / 'main.py').write_text('print("public")\n')
            standard_project = project
            legacy_project = run / 'legacy-workspace/package'
            legacy_project.mkdir(parents=True)
            (legacy_project / 'main.py').write_text('print("public")\n')
            manifest = run / 'manifest.json'
            # Missing, relative, absolute and legacy layout grants all restore.
            for grant in ({'workspace': {'project_root': 'code_task/workspace/packages/project'}},
                          {'workspace': {'workspace_dir': 'code_task/workspace',
                                         'project_root': 'code_task/workspace/packages/project'}},
                          {'workspace': {'workspace_dir': str(run / 'code_task/workspace'),
                                         'project_root': str(project)}},
                          {'layout': {'workspace': 'legacy-workspace',
                                      'project_root': 'legacy-workspace/package'}}):
                with self.subTest(grant=grant):
                    project = legacy_project if 'layout' in grant else standard_project
                    manifest.write_text(json.dumps({'workflow': 'code_task', **grant}))
                    before = manifest.read_bytes()
                    result = LocalCodeTaskToolGateway(run).call('read_code_task_file_range', {'path': 'main.py'})
                    self.assertEqual(result.status, 'ok')
                    report = review_generated_project(project_dir=project,
                        access_boundary=code_task_workspace_boundary(run), code_artifacts={},
                        result_schema={}, resource_plan={}, use_llm=False)
                    self.assertEqual(report['metadata']['project_dir'], str(project))
                    self.assertEqual(manifest.read_bytes(), before)
            # Output directory spelling cannot provide or select a source grant.
            meta = run / 'ordinary/code_task/meta'
            meta.mkdir(parents=True)
            unrelated = meta.parent.parent / 'manifest.json'
            unrelated.write_text('not a CodeTask manifest')
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                self.assertFalse(path.is_relative_to(project), 'No source read without a grant')
                return original_open(path, *args, **kwargs)
            with patch.object(Path, 'open', guarded_open):
                with self.assertRaisesRegex(ValueError, 'boundary'):
                    build_review_index(project)
                with self.assertRaisesRegex(ValueError, 'boundary'):
                    review_generated_project(project_dir=project, meta_dir=meta,
                        code_artifacts={}, result_schema={}, resource_plan={}, use_llm=False)
            review_generated_project(project_dir=project, meta_dir=meta,
                access_boundary=code_task_workspace_boundary(run), code_artifacts={},
                result_schema={}, resource_plan={}, use_llm=False)
            self.assertEqual(unrelated.read_text(), 'not a CodeTask manifest')

    def test_restored_private_or_external_projects_are_blocked_before_reads_and_writes(self):
        from simple_ar.code_task.orchestration.execute import _rerun_greenfield_review
        from simple_ar.code_task.runtime.state import code_task_paths
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            workspace, meta = run / 'code_task/workspace', run / 'code_task/meta'
            project = workspace / '.ssh/project'
            project.mkdir(parents=True)
            meta.mkdir(parents=True)
            (project / 'main.py').write_text('PRIVATE_SENTINEL')
            (meta / 'review_report.json').write_text('{"historical_receipt": true}')
            grants = [{'project_root': 'code_task/workspace/.ssh/project'},
                      {'workspace_dir': 'code_task/workspace', 'project_root': str(project)},
                      {'workspace_dir': str(workspace), 'project_root': str(project)},
                      {'project_root': str(project)}]
            external = run / 'external-project'
            external.mkdir()
            (external / 'main.py').write_text('PRIVATE_SENTINEL')
            cases = [('workspace', grant) for grant in [*grants, {'project_root': str(external)}]]
            cases.append(('layout', {'workspace': 'code_task/workspace', 'project_root': str(project)}))
            denied_roots = [project, external]
            # The saved boundary's basename is innocuous in every case.
            for ancestor, absolute in (('.ssh', False), ('.AWS', True), ('.env.example', False)):
                boundary = run / ancestor / 'project'
                boundary.mkdir(parents=True)
                (boundary / 'main.py').write_text('PRIVATE_SENTINEL')
                denied_roots.append(boundary)
                value = str(boundary) if absolute else boundary.relative_to(run).as_posix()
                cases.append(('workspace', {'workspace_dir': value, 'project_root': value}))
            # An alias above boundary can hide either the private lexical
            # ancestor or the private resolved ancestor; neither is a grant.
            public_target = run / 'public-target/project'
            public_target.mkdir(parents=True)
            (public_target / 'main.py').write_text('PRIVATE_SENTINEL')
            denied_roots.append(public_target)
            for alias, target in ((run / 'public-alias', run / '.ssh'),
                                  (run / '.agents', public_target.parent)):
                try:
                    alias.symlink_to(target, target_is_directory=True)
                except (OSError, NotImplementedError):
                    continue  # Keep non-link restore cases on hosts without symlinks.
                boundary = alias / 'project'
                denied_roots.append(boundary)
                cases.append(('workspace', {'workspace_dir': str(boundary), 'project_root': str(boundary)}))
            for section, grant in cases:
                with self.subTest(section=section, grant=grant):
                    (run / 'manifest.json').write_text(json.dumps({'workflow': 'code_task', section: grant}))
                    before = {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}
                    original_open = Path.open
                    def guarded_open(path, *args, **kwargs):
                        self.assertFalse(any(path.is_relative_to(root) for root in denied_roots))
                        return original_open(path, *args, **kwargs)
                    with patch.object(Path, 'open', guarded_open):
                        gateway = LocalCodeTaskToolGateway(run)
                        self.assertEqual(gateway.call('read_code_task_file_range', {'path': 'main.py'}).status, 'blocked')
                        search = gateway.call('search_code_task_code', {'query': 'PRIVATE_SENTINEL'})
                        self.assertEqual(search.status, 'blocked' if grant['project_root'] == str(external) else 'ok')
                        self.assertEqual(search.data.get('matches', []), [])
                        for review in (lambda: review_code_task_changes(run, use_llm=False),
                                       lambda: build_codebase_index(code_task_paths(run).workspace_dir,
                                           access_boundary=code_task_workspace_boundary(run)),
                                       lambda: review_generated_project(project_dir=code_task_paths(run).workspace_dir,
                                           access_boundary=code_task_workspace_boundary(run), code_artifacts={},
                                           result_schema={}, resource_plan={}, use_llm=False),
                                       lambda: _rerun_greenfield_review(run, code_task_paths(run),
                                           max_files=12, max_generated_lines=1000)):
                            with self.assertRaisesRegex(ValueError, 'private|boundary'):
                                review()
                    self.assertEqual(before, {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()})

    def test_indexes_cannot_authorize_private_or_escaped_paths_or_expand_frozen_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            root, meta = run / 'code_task/workspace', run / 'code_task/meta'
            (root / '.ssh').mkdir(parents=True)
            (root / 'ordinary').mkdir()
            meta.mkdir(parents=True)
            (root / 'main.py').write_text('print("public")\n')
            (root / 'README.md').write_text('Public instructions')
            unsafe = ['.ssh/README.md', 'ordinary/.SSH', '../README.md', str(run / 'README.md')]
            for name in unsafe:
                (root / name).write_text('PRIVATE_SENTINEL')
            original_open = Path.open
            def guarded_open(path, *args, **kwargs):
                self.assertNotIn(path, [root / name for name in unsafe])
                return original_open(path, *args, **kwargs)
            index = {'files': [{'path': name, 'summary': 'PRIVATE_SENTINEL'} for name in unsafe]
                     + [{'path': 'README.md'}, {'path': 'main.py'}],
                     'entrypoints': ['main.py', *unsafe], 'notebooks': [{'path': name} for name in unsafe]}
            saved = meta / 'codebase_index.json'
            saved.write_text(json.dumps(index))
            (meta / 'repo_map.json').write_text(json.dumps({'symbols': [
                {'path': name, 'name': 'PRIVATE_SENTINEL'} for name in unsafe]}))
            before = saved.read_bytes()
            with patch.object(Path, 'open', guarded_open):
                self.assertEqual({row['path'] for row in build_codebase_index(root)['files']}, {'main.py', 'README.md'})
                (root / 'unlisted.py').write_text('print("not in frozen inventory")')
                facts = inspect_execution_entry({'code_task': {'code_root': str(root)}},
                    access_boundary=root, project_index=index)
                self.assertEqual(facts['preparation']['source_file_paths'], ['README.md', 'main.py'])
                self.assertEqual(facts['entrypoint_candidates'], ['main.py'])
                self.assertEqual(facts['preparation']['notebook_candidates'], [])
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(facts))
                gateway = LocalCodeTaskToolGateway(run)
                for tool, arguments in [('list_code_task_files', {}),
                        ('search_code_task_code', {'query': 'PRIVATE_SENTINEL'}),
                        ('find_code_task_symbol', {'query': 'PRIVATE_SENTINEL'}),
                        ('find_code_task_related_files', {'query': 'PRIVATE_SENTINEL'})]:
                    with self.subTest(tool=tool):
                        result = gateway.call(tool, arguments)
                        self.assertEqual(result.status, 'ok')
                        self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result.data))
                for name in unsafe:
                    with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'indexed'):
                        inspect_project_preparation(root, access_boundary=root, index=index, read_paths=(name,))
            self.assertEqual(saved.read_bytes(), before)
            self.assertEqual(json.loads(before), index)

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
                    report = review_generated_project(project_dir=root, access_boundary=root, code_artifacts={}, resource_plan={},
                        result_schema={'required_metrics': ['score']}, use_llm=False)
                    categories = {row['category'] for row in report['findings']}
                    self.assertNotIn('metric_not_visible', categories)
                    self.assertIn('required_metric_default_fill', categories)
                    # Ignoring invalid bytes can join an identifier; replacing
                    # them cannot. Preserve each consumer's original choice.
                    (documents / ('results' + suffix)).write_bytes(b'sc\xffore\n')
                    report = review_generated_project(project_dir=root, access_boundary=root, code_artifacts={}, resource_plan={},
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
                    access_boundary=root,
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
            (root / 'public.md').write_text('PUBLIC_VALUE')
            (root / '.env.local').write_text('SECRET_VALUE')
            try:
                (hidden / 'linked.py').symlink_to(outside / 'secret.py')
                (root / '.linked').symlink_to(outside, target_is_directory=True)
                (root / 'public-alias.md').symlink_to(root / 'public.md')
                (root / 'private-alias.md').symlink_to(root / '.env.local')
                (root / 'directory-alias').symlink_to(hidden, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks unavailable')
            self.assertEqual([row['path'] for row in build_review_index(root, access_boundary=root)['files']], ['public.md'])
            gateway = LocalCodeTaskToolGateway(run)
            result = gateway.call('search_code_task_code', {'query': 'SECRET_VALUE'})
            self.assertEqual(result.status, 'ok')
            self.assertEqual(result.data['matches'], [])
            aliases = ['.config/linked.py', '.linked/secret.py', 'public-alias.md',
                       'private-alias.md', 'directory-alias/linked.py', 'directory-alias/../public.md']
            for name in aliases:
                with self.subTest(path=name):
                    self.assertEqual(gateway.call('read_code_task_file_range', {'path': name}).status, 'blocked')
            facts = inspect_project_preparation(root, access_boundary=root,
                index={'files': [{'path': name} for name in aliases], 'entrypoints': aliases})
            self.assertEqual(facts['source_file_paths'], [])
            self.assertEqual(facts['entrypoint_candidates'], [])

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
            index = build_review_index(root, access_boundary=root)
            self.assertEqual({row['path'] for row in index['files']}, {'main.py', 'environment/metrics.py'})
            report = review_generated_project(project_dir=root, access_boundary=root, code_artifacts={},
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
