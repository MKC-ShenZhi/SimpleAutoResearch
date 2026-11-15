"""Python source validation follows the compiler's encoding contract."""
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from simple_ar.code_task import initialize_code_task, validate_code_task
from simple_ar.code_task.execution.validation import _validate_python_file
from simple_ar.core.artifacts import read_json, write_json

class SourceEncodingTests(unittest.TestCase):
    def inspect(self, raw):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.py'
            source.write_bytes(raw)
            issues = []
            _validate_python_file(source, rel_path='source.py', workspace_dir=root,
                                  strict=False, issues=issues)
            self.assertEqual(source.read_bytes(), raw)
            return issues

    def test_valid_bom_and_encoding_cookie_are_not_false_syntax_errors(self):
        for raw in (b'\xef\xbb\xbfimport json\nVALUE = 1\n',
                    b'# coding: latin-1\nVALUE = "caf\xe9"\n',
                    b'\xef\xbb\xbf# coding: utf-8\nVALUE = "\xe6\xb5\x8b"\n',
                    b'from __future__ import annotations\nraise RuntimeError("must not execute")\n'):
            with self.subTest(raw=raw):
                self.assertFalse([row for row in self.inspect(raw) if row['severity'] == 'error'])

    def test_invalid_encoding_and_true_syntax_errors_still_fail(self):
        for raw in (b'\xef\xbb\xbf# coding: latin-1\nVALUE = 1\n',
                    b'VALUE = "\xff"\n', b'if True print(1)\n',
                    b'# coding: imaginary_encoding\nVALUE = 1\n',
                    b'VALUE = 1\nfrom __future__ import annotations\n',
                    b'return 1\n', b'break\n', b'nonlocal missing\n'):
            with self.subTest(raw=raw):
                errors = [row for row in self.inspect(raw) if row['severity'] == 'error']
                self.assertEqual(errors[0]['code'], 'syntax_error')

    def test_external_dependency_scan_uses_the_same_encoded_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            raw = b'\xef\xbb\xbfimport json\nVALUE = 7\n'
            (project / 'main.py').write_bytes(raw)
            task = root / 'task.md'
            task.write_text('Inspect without executing project code.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=task)
            manifest = read_json(run / 'manifest.json')
            manifest['environment']['policy'] = {'mode': 'external', 'python_executable': sys.executable}
            write_json(run / 'manifest.json', manifest)
            with patch('simple_ar.code_task.execution.validation._external_import_availability', return_value={'json': True}) as probe:
                result = validate_code_task(run)
            self.assertEqual(result.status, 'passed')
            self.assertIn('json', probe.call_args.args[1])
            self.assertEqual((project / 'main.py').read_bytes(), raw)

    def test_only_byte_identical_frozen_git_errors_are_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            # Not a tests-directory exemption: a broken production file is also
            # reported, and any mutation (even the same parse error) is fatal.
            (project / 'bad.py').write_bytes(b'# coding: unknown_codec\nVALUE = 1\n')
            (project / 'good.py').write_text('VALUE = 1\n')
            def git(*args):
                return subprocess.run(['git', '-C', str(project), *args], capture_output=True, check=True)
            git('init')
            git('add', '.')
            git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'baseline')
            task = root / 'task.md'
            task.write_text('Repair only the requested source; preserve unrelated fixtures.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=task, workspace_mode='git_worktree')
            from simple_ar.code_task.runtime.state import code_task_paths
            workspace = code_task_paths(run).workspace_dir
            with patch('simple_ar.code_task.execution.validation._unchanged_git_baseline',
                       side_effect=AssertionError('An initial record already owns this observation')):
                result = validate_code_task(run)
            self.assertEqual(result.status, 'passed')
            issue = next(row for row in read_json(result.report_path)['issues'] if row['code'] == 'syntax_error')
            self.assertEqual(issue['baseline_status'], 'byte_identical_initial_syntax_error')
            # Older worktrees without the initial record can still use their frozen commit.
            manifest = read_json(run / 'manifest.json')
            manifest['workspace'].pop('initial_syntax_errors')
            write_json(run / 'manifest.json', manifest)
            historical = validate_code_task(run)
            issue = next(row for row in read_json(historical.report_path)['issues'] if row['code'] == 'syntax_error')
            self.assertEqual(issue['baseline_status'], 'byte_identical_frozen_git_source')
            self.assertEqual(validate_code_task(run, strict=True).status, 'failed')
            # Moving the live source no longer changes the frozen comparison.
            (project / 'bad.py').write_text('VALUE = 2\n')
            self.assertEqual(validate_code_task(run).status, 'passed')
            (workspace / 'bad.py').write_bytes(b'# coding: unknown_codec\nVALUE = 2\n')
            self.assertEqual(validate_code_task(run).status, 'failed')
            (workspace / 'bad.py').write_text('VALUE = 1\n')
            (workspace / 'new.py').write_text('if True print(1)\n')
            self.assertEqual(validate_code_task(run).status, 'failed')

    def test_legacy_copy_without_initial_evidence_keeps_syntax_errors_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            (project / 'bad.py').write_text('if True print(1)\n')
            task = root / 'task.md'
            task.write_text('Inspect copied source.')
            run = root / 'run'
            initialize_code_task(run_dir=run, code_root=project, task_file=task, workspace_mode='copy')
            manifest = read_json(run / 'manifest.json')
            manifest['workspace'].pop('initial_syntax_errors', None)
            write_json(run / 'manifest.json', manifest)
            self.assertEqual(validate_code_task(run).status, 'failed')

    def test_initial_copy_errors_are_observed_not_new_defects_or_runtime_success(self):
        from simple_ar.code_task.analysis.repo_map import build_code_task_repo_map
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / 'project'
            project.mkdir()
            original = b'# coding: imaginary_codec\nVALUE = 1\n'
            (project / 'bad.py').write_bytes(original)
            (project / 'good.py').write_text('VALUE = 42\n')
            task = root / 'task.md'
            task.write_text('Repair a scoped defect without changing unrelated inputs.')
            for mode in ('copy', 'sparse_copy'):
                with self.subTest(mode=mode):
                    run = root / mode
                    initialized = initialize_code_task(run_dir=run, code_root=project, task_file=task, workspace_mode=mode)
                    workspace = initialized.workspace_dir
                    saved = read_json(run / 'manifest.json')['workspace']['initial_syntax_errors']
                    self.assertEqual(set(saved), {'bad.py'})
                    result = validate_code_task(run)
                    self.assertEqual(result.status, 'passed')
                    report = read_json(result.report_path)
                    issue = next(row for row in report['issues'] if row['code'] == 'syntax_error')
                    self.assertEqual(issue['baseline_status'], 'byte_identical_initial_syntax_error')
                    self.assertEqual(validate_code_task(run, strict=True).status, 'failed')
                    # A mutable live source is not the saved comparison.
                    (project / 'bad.py').write_bytes(b'# coding: imaginary_codec\nVALUE = 9\n')
                    self.assertEqual(validate_code_task(run).status, 'passed')
                    # Same error message after editing is not proof of unchanged bytes.
                    (workspace / 'bad.py').write_bytes(b'# coding: imaginary_codec\nVALUE = 2\n')
                    build_code_task_repo_map(run, refresh_index=True)
                    self.assertEqual(read_json(run / 'manifest.json')['workspace']['initial_syntax_errors'], saved)
                    self.assertEqual(validate_code_task(run).status, 'failed')
                    (workspace / 'bad.py').write_bytes(original)
                    (workspace / 'new.py').write_text('if True print(1)\n')
                    self.assertEqual(validate_code_task(run).status, 'failed')
                    (project / 'bad.py').write_bytes(original)

    def test_source_index_respects_encoding_bytes_like_validation(self):
        from simple_ar.code_task.analysis.index import build_codebase_index
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bom.py').write_bytes(b'\xef\xbb\xbfVALUE = 1\n')
            (root / 'latin.py').write_bytes(b'# coding: latin-1\ndef caf\xe9():\n    return 1\n')
            (root / 'unknown.py').write_bytes(b'# coding: imaginary_codec\nVALUE = 1\n')
            (root / 'compiler_error.py').write_bytes(b'VALUE = 1\nfrom __future__ import annotations\n')
            rows = {row['path']: row['python'] for row in build_codebase_index(root)['files']}
            self.assertTrue(rows['bom.py']['syntax_ok'])
            self.assertTrue(rows['latin.py']['syntax_ok'])
            self.assertEqual(rows['latin.py']['functions'][0]['name'], 'café')
            self.assertFalse(rows['unknown.py']['syntax_ok'])
            self.assertFalse(rows['compiler_error.py']['syntax_ok'])

if __name__ == '__main__':
    unittest.main()
