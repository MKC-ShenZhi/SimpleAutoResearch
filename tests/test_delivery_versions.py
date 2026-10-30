"""Server-only validation of new delivery retention and unchanged old history."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.tools.delivery_versions import DeliveryVersions
from simple_ar.tools.workspace import RetainedDelivery
from tests.native_history_fixture import load_native_history


def action(name, arguments):
    return {'tool_name': name, 'arguments': arguments}


class DeliveryVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / 'task'
        self.app = ToolApplication.create(self.root, 'Deliver and revise files', model=None,
            sources={}, allow_execution=True, max_attempts=100,
            process_limits={'process_invocations': 4, 'process_wall_seconds': 60})
        self.script = self.app.workspace.workspace / 'program.py'
        self.figure = self.app.workspace.workspace / 'figure.bin'
        self.script.write_bytes(b'value = 1\r\n')
        self.figure.write_bytes(b'\x00first\xff')
        self.locators = ['workspace/program.py', 'workspace/figure.bin']

    def deliver(self, summary='First version', feedback=''):
        return self.app.run(feedback=feedback, actions=[action('deliver_result', {
            'summary': summary, 'artifacts': self.locators})], progress=lambda _: None)

    def pending_capture(self):
        with patch.object(DeliveryVersions, 'retain', side_effect=PermissionError('capture interrupted')):
            result = self.deliver()
        self.assertEqual(result['status'], 'paused')
        record = self.app.state['turns'][-1]['tools'][-1]['result']
        parent = (self.root / record).parent
        identity = {'observation': record,
            'observation_sha256': hashlib.sha256((self.root / record).read_bytes()).hexdigest()}
        return parent, identity, self.app.ledger.to_dict(), len(self.app.session.list_attempts())

    def assert_capture_resumes(self, identity, ledger, attempts):
        self.assertTrue(self.app._commit_terminal(lambda _: None))
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(len(self.app.session.list_attempts()), attempts)
        version = DeliveryVersions(self.root).versions()[0]
        self.assertEqual(version['observation'], identity['observation'])
        self.assertEqual(version['status'], 'retained')
        for locator, row in version['files'].items():
            with (self.root / row['blob']).open('rb') as stream:
                self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), row['sha256'])
            with self.app.workspace.resolve(locator).open('rb') as stream:
                self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), row['sha256'])

    def test_codex_ancestor_allows_delivery_but_task_private_and_external_rules_remain(self):
        root = self.base / '.codex/worktrees/checkout/runs/task'
        app = ToolApplication.create(root, 'Deliver in a managed worktree', model=None, sources={})
        result = app.run(actions=[
            action('write_file', {'path': 'workspace/result.txt', 'content': 'managed bytes'}),
            action('deliver_result', {'summary': 'Result', 'artifacts': ['workspace/result.txt']})],
            progress=lambda _: None)
        self.assertEqual(result['status'], 'completed')
        source = result['delivery_versions'][0]['files']['workspace/result.txt']['source']
        read = app.workspace.call('read_file', {'path': source}, identity='managed-version')
        self.assertTrue(read.ok, read)
        self.assertEqual(read.data['text'], 'managed bytes')
        private = root / 'workspace/.codex/secret.txt'
        private.parent.mkdir()
        private.write_bytes(b'task-private')
        admitted = []
        external = self.base / 'ordinary.txt'
        external.write_bytes(b'external')
        def authorize(path):
            admitted.append(path)
            if path == external:
                raise ValueError('External grant denied')
        reader = RetainedDelivery(root, authorize=authorize)
        with self.assertRaisesRegex(ValueError, 'Private/runtime'):
            reader.admit(private)
        with self.assertRaisesRegex(ValueError, 'Private/runtime'):
            reader.resolve('workspace/.codex/secret.txt')
        with self.assertRaisesRegex(ValueError, 'Private/runtime'):
            reader.admit(self.base / '.codex/external.txt')
        with self.assertRaisesRegex(ValueError, 'External grant denied'):
            reader.admit(external)
        self.assertIn(external, admitted)
        with self.assertRaisesRegex(ValueError, 'traversal'):
            reader.record('../outside.json')
        with self.assertRaisesRegex(ValueError, 'traversal'):
            reader.admit(root / '../outside.json')

    def test_retained_reader_still_rejects_links_inside_codex_ancestor_layout(self):
        root = self.base / '.codex/worktrees/linked/runs/task'
        app = ToolApplication.create(root, 'Check retained path admission', model=None, sources={})
        outside = self.base / 'outside.txt'
        outside.write_bytes(b'outside bytes')
        link = app.workspace.workspace / 'linked.txt'
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f'Server cannot create symlinks: {exc}')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            RetainedDelivery(root).resolve('workspace/linked.txt')
        self.assertEqual(outside.read_bytes(), b'outside bytes')

    def test_empty_legacy_stage_recovers_and_new_stage_is_promoted_with_marker(self):
        parent, identity, ledger, attempts = self.pending_capture()
        stage = parent / '.delivery-version-pending'
        stage.mkdir()
        keep = parent / 'user-notes.txt'
        keep.write_bytes(b'leave other attempt contents alone')
        orphan = parent / '.delivery-version-bootstrap-old'
        orphan.mkdir()
        (orphan / 'user.txt').write_bytes(b'unknown bootstrap contents')
        promoted = []
        rename = os.rename
        def checked_rename(source, destination):
            if Path(destination) == stage:
                self.assertFalse(stage.exists())
                self.assertTrue(Path(source).name.startswith('.delivery-version-bootstrap-'))
                marker = read_json(Path(source) / 'receipt.json')
                self.assertEqual(marker, {'schema': 'delivery_version.staging.v1', **identity})
                self.assertFalse((Path(source) / 'bytes').exists())
                promoted.append(Path(source))
            return rename(source, destination)
        with patch('simple_ar.tools.delivery_versions.os.rename', side_effect=checked_rename):
            self.assert_capture_resumes(identity, ledger, attempts)
        self.assertEqual(len(promoted), 1)
        self.assertEqual(keep.read_bytes(), b'leave other attempt contents alone')
        self.assertEqual((orphan / 'user.txt').read_bytes(), b'unknown bootstrap contents')

    def test_owned_stage_accepts_receipt_write_temporaries_but_rejects_unknown_contents(self):
        parent, identity, ledger, attempts = self.pending_capture()
        stage = parent / '.delivery-version-pending'
        (stage / 'bytes').mkdir(parents=True)
        write_json(stage / 'receipt.json', {'schema': 'delivery_version.staging.v1', **identity})
        (stage / 'bytes/pending').write_bytes(b'partial file bytes')
        with tempfile.NamedTemporaryFile(prefix='.receipt.json.', suffix='.tmp', dir=stage, delete=False) as stream:
            stream.write(b'{"partial":')
        foreign = stage / '.receipt.json.notes.txt'
        foreign.write_bytes(b'user notes, not an atomic-write temporary')
        before = {p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'Unknown delivery staging contents'):
            self.app._commit_terminal(lambda _: None)
        self.assertEqual({p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(len(self.app.session.list_attempts()), attempts)
        foreign.unlink()  # Only the fixture removes the deliberate user conflict.
        # A matching temporary name must still be a regular file.
        directory = stage / '.receipt.json.directory.tmp'
        directory.mkdir()
        (directory / 'keep.txt').write_bytes(b'not a temporary file')
        before = {p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'Unknown delivery staging contents'):
            self.app._commit_terminal(lambda _: None)
        self.assertEqual({p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}, before)
        (directory / 'keep.txt').unlink()
        directory.rmdir()
        write_json(stage / 'receipt.json', {'schema': 'delivery_version.staging.v1',
            **identity, 'observation_sha256': '0' * 64})
        before = {p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}
        with self.assertRaisesRegex(ValueError, 'Unknown delivery staging contents'):
            self.app._commit_terminal(lambda _: None)
        self.assertEqual({p: p.read_bytes() for p in stage.rglob('*') if p.is_file()}, before)
        write_json(stage / 'receipt.json', {'schema': 'delivery_version.staging.v1', **identity})
        self.assert_capture_resumes(identity, ledger, attempts)

    def test_receipt_temporary_without_owner_is_not_permission_to_delete_files(self):
        parent, identity, ledger, attempts = self.pending_capture()
        stage = parent / '.delivery-version-pending'
        stage.mkdir()
        unknown = stage / '.receipt.json.unowned.tmp'
        unknown.write_bytes(b'unowned bytes')
        with self.assertRaisesRegex(ValueError, 'Unknown delivery staging contents'):
            self.app._commit_terminal(lambda _: None)
        self.assertEqual(unknown.read_bytes(), b'unowned bytes')
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        unknown.unlink()
        self.assert_capture_resumes(identity, ledger, attempts)

    def test_real_process_exit_during_bootstrap_or_final_marker_recovers_same_attempt(self):
        parent, identity, ledger, attempts = self.pending_capture()
        script = '''
import os, sys, tempfile
from pathlib import Path
from simple_ar.app.tool_application import ToolApplication
import simple_ar.tools.delivery_versions as versions
original = versions.write_json
def interrupted(path, data):
    path = Path(path)
    bootstrap = path.parent.name.startswith('.delivery-version-bootstrap-')
    final = path.parent.name == '.delivery-version-pending' and data.get('schema') == 'delivery_version.v1'
    if (sys.argv[2] == 'bootstrap' and bootstrap) or (sys.argv[2] == 'final' and final):
        with tempfile.NamedTemporaryFile(prefix='.receipt.json.', suffix='.tmp', dir=path.parent, delete=False) as stream:
            stream.write(b'{"interrupted":')
        os._exit(79)
    return original(path, data)
versions.write_json = interrupted
ToolApplication.load(Path(sys.argv[1]))._commit_terminal(lambda _: None)
'''
        stage = parent / '.delivery-version-pending'
        # Both exits bypass every Python finally block, unlike an exception mock.
        for point in ('bootstrap', 'final'):
            with self.subTest(crash=point):
                result = subprocess.run([sys.executable, '-c', script, str(self.root), point],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
                    env={**os.environ, 'PYTHONPATH': os.pathsep.join(map(str, sys.path))})
                self.assertEqual(result.returncode, 79, result.stderr)
                if point == 'bootstrap':
                    self.assertFalse(stage.exists())
                    orphans = list(parent.glob('.delivery-version-bootstrap-*'))
                    self.assertTrue(orphans)
                    orphan_files = {p: p.read_bytes() for orphan in orphans for p in orphan.rglob('*') if p.is_file()}
                else:
                    self.assertTrue((stage / 'receipt.json').is_file())
                    self.assertTrue(list(stage.glob('.receipt.json.*.tmp')))
                self.app = ToolApplication.load(self.root)
                self.assertEqual(self.app.ledger.to_dict(), ledger)
                self.assertEqual(len(self.app.session.list_attempts()), attempts)
        keep = parent / 'keep.txt'
        keep.write_bytes(b'untouched by recovery')
        self.assert_capture_resumes(identity, ledger, attempts)
        self.assertEqual({p: p.read_bytes() for p in orphan_files}, orphan_files)
        self.assertEqual(keep.read_bytes(), b'untouched by recovery')

    def test_new_deliveries_keep_distinct_bytes_readable_and_copyable_after_undeclared_process(self):
        initial = self.deliver()
        self.assertEqual(initial['status'], 'completed')
        first = initial['delivery_versions'][0]
        result = self.app.run(feedback='Revise both files; keep the first delivery', actions=[
            action('edit_file', {'path': self.locators[0],
                'expected_sha256': hashlib.sha256(self.script.read_bytes()).hexdigest(),
                'replacements': [{'old_text': '1', 'new_text': '2'}]}),
            action('run_process', {'argv': ['python', '-c',
                "from pathlib import Path; Path('figure.bin').write_bytes(bytes([0,2,255]))"],
                'timeout_sec': 10}),
            action('deliver_result', {'summary': 'Second version', 'artifacts': self.locators})],
            approve=lambda *_: True, progress=lambda _: None)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([v['status'] for v in result['delivery_versions']], ['retained', 'retained'])
        source = first['files'][self.locators[0]]
        read = self.app.workspace.call('read_file', {'path': source['source']}, identity='read-first')
        self.assertTrue(read.ok, read)
        self.assertEqual(read.data['text'], 'value = 1\r\n')
        self.assertEqual(read.data['sha256'], source['source_sha256'])
        for version, expected, name in ((first, b'\x00first\xff', 'first.bin'),
                (result['delivery_versions'][1], bytes([0, 2, 255]), 'second.bin')):
            source = version['files'][self.locators[1]]
            restored = self.app.workspace.call('copy_file', {'source': source['source'],
                'source_sha256': source['source_sha256'], 'path': 'workspace/' + name},
                identity=name.replace('.', '-'))
            self.assertTrue(restored.ok, restored)
            self.assertEqual((self.figure.parent / name).read_bytes(), expected)
        self.assertEqual(self.script.read_bytes(), b'value = 2\r\n')
        self.assertEqual(self.figure.read_bytes(), bytes([0, 2, 255]))
        self.assertTrue(list((self.root / 'tools/edits').glob('*/previous')))

    def test_storage_failure_keeps_old_delivery_and_retries_same_attempt_without_new_usage(self):
        self.deliver()
        old = (self.root / 'delivery.json').read_bytes()
        self.figure.write_bytes(b'new bytes')
        with patch('simple_ar.tools.delivery_versions.os.rename', side_effect=PermissionError('disk denied')):
            result = self.deliver('Revision', feedback='Use the revised output')
        self.assertEqual(result['status'], 'paused')
        self.assertIn('disk denied', result['status_reason'])
        self.assertEqual((self.root / 'delivery.json').read_bytes(), old)
        self.assertFalse(list(self.root.rglob('.delivery-version-pending')))
        before = self.app.ledger.to_dict()
        attempts = len(self.app.session.list_attempts())
        record = self.app.state['turns'][-1]['tools'][-1]['result']
        observation = (self.root / record).read_bytes()
        pinned = self.figure.read_bytes()
        acceptance = self.app.operations.delivery_issues

        def changed_after_acceptance(*args, **kwargs):
            issues = acceptance(*args, **kwargs)
            self.assertEqual(issues, [])
            self.figure.write_bytes(b'changed after semantic acceptance')
            return issues

        # Removing duplicate completeness policy must not remove the byte check
        # between operation acceptance and retention/publication.
        with patch.object(self.app.operations, 'delivery_issues', side_effect=changed_after_acceptance):
            with self.assertRaisesRegex(ValueError, 'Delivery version is unbound or changed'):
                self.app._commit_terminal(lambda _: None)
        self.assertEqual((self.root / 'delivery.json').read_bytes(), old)
        self.assertEqual((self.root / record).read_bytes(), observation)
        self.assertEqual(self.app.ledger.to_dict(), before)
        self.assertEqual(len(self.app.session.list_attempts()), attempts)
        self.figure.write_bytes(pinned)
        self.assertTrue(self.app._commit_terminal(lambda _: None))
        self.assertEqual(self.app.ledger.to_dict(), before)
        self.assertEqual(len(self.app.session.list_attempts()), attempts)
        self.assertEqual(len([v for v in DeliveryVersions(self.root).versions() if v['status'] == 'retained']), 2)

    def test_legacy_missing_bytes_continue_without_upgrade_or_reconstruction(self):
        for bound in (True, False):
            with self.subTest(bound=bound):
                root = self.base / ('legacy-bound' if bound else 'legacy-unbound')
                app = load_native_history(root, 'Historical delivery', config={'tool_contracts': 'full'})
                output = app.workspace.workspace / 'old.txt'
                output.write_bytes(b'old bytes')
                # Historical writer fixture: no retained byte versions.
                with patch.object(DeliveryVersions, 'retain', return_value={}):
                    app.run(actions=[action('deliver_result', {'summary': 'Old result',
                        'artifacts': ['workspace/old.txt']})], progress=lambda _: None)
                record = app.state['turns'][-1]['tools'][-1]['result']
                if not bound:
                    receipt = read_json(root / record)
                    receipt['metadata'].pop('delivery_hashes', None)
                    write_json(root / record, receipt)
                old_record = (root / record).read_bytes()
                output.unlink()
                app.supply(supply_id='execution-grant', reason='Authorize later explicit commands',
                    changes={'allow_execution': True})
                result = app.run(feedback='Continue with a new file', actions=[
                    action('write_file', {'path': 'workspace/new.txt', 'content': 'new bytes'}),
                    action('deliver_result', {'summary': 'New result', 'artifacts': ['workspace/new.txt']})],
                    progress=lambda _: None)
                self.assertEqual(result['status'], 'completed')
                self.assertEqual((root / record).read_bytes(), old_record)
                self.assertFalse(output.exists())
                self.assertFalse((root / record).parent.joinpath('delivery-version').exists())
                self.assertEqual(result['delivery_versions'][0]['status'], 'unavailable')
                self.assertEqual(result['delivery_versions'][0]['files'], {})
                self.assertEqual(result['delivery_versions'][1]['status'], 'retained')

    def test_damaged_previous_archive_does_not_block_new_delivery_or_candidate_backup(self):
        first = self.deliver()['delivery_versions'][0]
        file = first['files'][self.locators[1]]
        self.app.workspace.resolve(file['source']).unlink()
        changed = self.app.workspace.call('edit_file', {'path': self.locators[0],
            'expected_sha256': hashlib.sha256(self.script.read_bytes()).hexdigest(),
            'replacements': [{'old_text': '1', 'new_text': '3'}]}, identity='candidate')
        self.assertTrue(changed.ok, changed)
        self.assertEqual((self.root / 'tools/edits/candidate/previous').read_bytes(), b'value = 1\r\n')
        result = self.deliver('New delivery', feedback='Deliver the candidate')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([v['status'] for v in result['delivery_versions']], ['unavailable', 'retained'])

    def test_no_new_retention_limit_above_existing_copy_tool_limit(self):
        size = 64 * 1024 * 1024 + 1
        with self.figure.open('wb') as stream:
            stream.truncate(size)
        result = self.deliver()
        self.assertEqual(result['status'], 'completed')
        source = result['delivery_versions'][0]['files'][self.locators[1]]
        path = self.app.workspace.resolve(source['source'])
        self.assertEqual(path.stat().st_size, size)
        read = self.app.workspace.call('read_file', {'path': source['source'], 'max_bytes': 8}, identity='large-window')
        self.assertTrue(read.ok, read)
        self.assertTrue(read.data['has_more'])
        with path.open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), source['source_sha256'])

    def test_existing_cli_discovery_is_read_only_and_labels_missing_versions(self):
        from simple_ar.cli.main import _print_research_session_status, _render_native_result
        from rich.console import Console
        result = self.deliver()
        version = result['delivery_versions'][0]
        self.app.workspace.resolve(version['files'][self.locators[1]]['source']).unlink()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for render in (lambda: _print_research_session_status(self.root),
                lambda: _render_native_result(self.app, self.app.session.status_snapshot())):
            output = io.StringIO()
            with patch('simple_ar.core.console.make_console', return_value=Console(file=output, width=500)), \
                    patch('simple_ar.cli.main.print_line', side_effect=lambda text: output.write(str(text) + '\n')):
                render()
            self.assertIn('Retained byte versions', output.getvalue())
            self.assertIn('unavailable', output.getvalue())
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}, before)


if __name__ == '__main__':
    unittest.main()
