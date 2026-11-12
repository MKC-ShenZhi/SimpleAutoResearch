"""Exercise task-authorized execution on a real Linux workspace boundary."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_ar.app.tool_application import ToolApplication


def action(name, **arguments):
    return {'tool_name': name, 'arguments': arguments}


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bwrap'), 'Linux bubblewrap required')
class WorkspaceExecutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.source = self.base / 'original'
        self.source.mkdir()
        (self.source / 'data.txt').write_text('source')
        (self.source / '.env').write_text('private')
        self.app = ToolApplication.create(self.base / 'task', 'Analyze the material', model=None,
            sources={'source': self.source}, allow_execution=True, sandbox_execution=True)

    def test_common_work_runs_without_approval_and_keeps_physical_receipt(self):
        program = ('import json,os,socket; '
            'open("result.txt","w").write("done"); '
            's=socket.socket(); s.settimeout(1); '
            f'print(json.dumps({{"source_visible":os.path.exists({str(self.source)!r}), '
            '"network_result":s.connect_ex(("1.1.1.1",53))}))')
        result = self.app.run(actions=[action('run_process', argv=['python', '-c', program])],
            progress=lambda _: None, approve=lambda *_: self.fail('A scoped command should not pause'))
        self.assertNotIn('approval', result.get('reason', '').lower())
        self.assertEqual((self.app.workspace.workspace / 'result.txt').read_text(), 'done')
        tool = self.app.state['turns'][-1]['tools'][-1]
        observed = self.app.session.store.read_json(tool['result'])
        self.assertEqual(observed['status'], 'ok', observed)
        self.assertEqual(json.loads(observed['data']['stdout'])['source_visible'], False)
        self.assertNotEqual(json.loads(observed['data']['stdout'])['network_result'], 0)
        receipt = self.app.session.store.read_json(observed['data']['receipt'])
        self.assertEqual(receipt['execution_scope'], 'workspace')
        self.assertEqual(receipt['argv'][1:3], ['-c', program])
        self.assertEqual(self.app.ledger.entries[-1].status, 'settled')
        loaded = ToolApplication.load(self.app.session.store.root)
        self.assertTrue(loaded.state['config']['sandbox_execution'])
        self.assertEqual(loaded.ledger.to_dict(), self.app.ledger.to_dict())

    def test_independent_work_writes_only_its_scratch_area(self):
        workspace = self.app.workspace.workspace
        (workspace / 'candidate.txt').write_text('original')
        scratch = workspace / 'inspection-work' / 'review-1'
        scratch.mkdir(parents=True)
        program = ('from pathlib import Path\n'
            'Path("local.txt").write_text("evidence")\n'
            'try:\n Path("../../candidate.txt").write_text("changed")\n'
            'except OSError:\n print("candidate read-only")\n')
        args = {'argv': ['python', '-c', program], 'cwd': 'workspace/inspection-work/review-1'}
        pending = {'attempt': 'inspection-test', 'call': {'function': {
            'name': 'run_process', 'arguments': json.dumps(args)}}}
        self.assertTrue(self.app._approve_action(pending, '', lambda *_: self.fail('Unexpected approval'),
            lambda _: None, scoped=True))
        observed = self.app.workspace.call('run_process', args, identity='inspection-test')
        self.assertEqual(observed.status, 'ok', observed)
        self.assertEqual((scratch / 'local.txt').read_text(), 'evidence')
        self.assertEqual((workspace / 'candidate.txt').read_text(), 'original')
        self.assertIn('candidate read-only', observed.data['stdout'])

    def test_main_and_independent_tool_contracts_describe_the_actual_scope(self):
        main = self.app.gateway.registry.get('run_process').description
        self.assertIn('without per-command approval', main)
        tools = self.app.operations.inspection._shared_tools('review-1')
        review = next(row['function'] for row in tools if row['function']['name'] == 'run_process')
        self.assertIn('without per-command approval', review['description'])
        cwd = review['parameters']['properties']['cwd']['description']
        self.assertIn('Writes stay in this directory', cwd)
        self.assertNotIn('no sandbox', cwd)

    def test_host_resources_require_explicit_approval_before_execution(self):
        outside = self.base / 'outside.txt'
        program = f'from pathlib import Path; Path({str(outside)!r}).write_text("approved")'
        result = self.app.run(actions=[action('run_process', argv=['python', '-c', program], host_access=True)],
            progress=lambda _: None)
        self.assertEqual(result['status'], 'paused')
        self.assertFalse(outside.exists())
        self.assertFalse(self.app.ledger.entries)
        token = self.app.approval_token(self.app.state['pending']['call'])
        self.app.run(approved_execution=token, progress=lambda _: None)
        self.assertEqual(outside.read_text(), 'approved')
        self.assertEqual(self.app.ledger.entries[-1].status, 'settled')

    def test_saved_task_can_receive_the_explicit_scope_without_resetting_history(self):
        old = ToolApplication.create(self.base / 'old', 'Old task', model=None, sources={}, allow_execution=True)
        before = old.ledger.to_dict()
        old.supply(supply_id='workspace-access', reason='Authorize workspace execution',
            changes={'sandbox_execution': True})
        restored = ToolApplication.load(old.session.store.root)
        self.assertTrue(restored.state['config']['sandbox_execution'])
        self.assertEqual(restored.ledger.to_dict(), before)
        self.assertEqual(restored.state['requests'][0], 'Old task')
        self.assertIn('Authorize workspace execution', restored.state['requests'][-1])

    @unittest.skipUnless(shutil.which('pandoc') and shutil.which('xelatex'), 'Pandoc and XeLaTeX required')
    def test_publication_uses_the_same_scope_and_records_each_process(self):
        (self.app.workspace.workspace / 'draft.md').write_text('# Report\n\nA result.\n', encoding='utf-8')
        self.app.run(actions=[action('export_report', markdown='draft.md', layout='article', compile_pdf=True)],
            progress=lambda _: None, approve=lambda *_: self.fail('Scoped publication should not pause'))
        observed = self.app.session.store.read_json(self.app.state['turns'][-1]['tools'][-1]['result'])
        self.assertEqual(observed['status'], 'ok', observed)
        self.assertTrue(list((self.app.workspace.workspace / 'publications').rglob('*.pdf')))
        self.assertTrue(self.app.ledger.entries)
        self.assertTrue(all(entry.status == 'settled' for entry in self.app.ledger.entries))


class WorkspaceExecutionAdmissionTests(unittest.TestCase):
    def test_missing_sandbox_never_silently_grants_execution(self):
        with tempfile.TemporaryDirectory() as tmp, patch('shutil.which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'bubblewrap'):
                ToolApplication.preview_creation(Path(tmp) / 'task', 'Work', model=None, sources={},
                    allow_execution=True, sandbox_execution=True)

    def test_ordinary_saved_authorization_still_requires_command_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = ToolApplication.create(Path(tmp) / 'task', 'Work', model=None, sources={}, allow_execution=True)
            result = app.run(actions=[action('run_process', argv=['python', '-c', 'print(1)'])],
                progress=lambda _: None)
            self.assertEqual(result['status'], 'paused')
            self.assertIn('explicit approval', app.session.manifest.status_reason)
            self.assertFalse(app.ledger.entries)


if __name__ == '__main__':
    unittest.main()
