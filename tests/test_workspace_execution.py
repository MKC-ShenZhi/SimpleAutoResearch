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

    @unittest.skipUnless(Path('/etc/alternatives/awk').exists() and shutil.which('awk'),
                         'System awk alternatives link required')
    def test_system_alternatives_remain_available_in_workspace_scope(self):
        self.app.run(actions=[action('run_process', argv=['awk', 'BEGIN {print 42}'])],
            progress=lambda _: None, approve=lambda *_: self.fail('A scoped command should not pause'))
        tool = self.app.state['turns'][-1]['tools'][-1]
        observed = self.app.session.store.read_json(tool['result'])
        self.assertEqual(observed['status'], 'ok', observed)
        self.assertEqual(observed['data']['stdout'].strip(), '42')

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
        publication = self.app.gateway.registry.get('export_report').description
        self.assertIn('without per-command approval', publication)
        tools = self.app.operations.inspection._shared_tools('review-1')
        review = next(row['function'] for row in tools if row['function']['name'] == 'run_process')
        self.assertIn('without per-command approval', review['description'])
        cwd = review['parameters']['properties']['cwd']['description']
        self.assertIn('host_access', cwd)
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
        self.assertEqual(restored.state['config']['sandbox_operations'], 'workspace.v1')

    def test_scientific_measurement_uses_the_task_scope_and_owned_outputs(self):
        app = ToolApplication.create(self.base / 'science', 'Measure a workspace result', model=None,
            sources={'source': self.source}, allow_execution=True, sandbox_execution=True,
            process_limits={'process_invocations': 3, 'process_wall_seconds': 60})
        program = ('import json,os,socket; from pathlib import Path; '
            's=socket.socket(); s.settimeout(1); '
            f'facts={{"source_visible":os.path.exists({str(self.source)!r}), '
            '"secret_visible":bool(os.getenv("SIMPLE_AR_TEST_SECRET")), '
            '"network_result":s.connect_ex(("1.1.1.1",53))}; '
            'Path(os.environ["SIMPLE_AR_OUTPUT_DIR"],"observed.json").write_text(json.dumps(facts)); '
            'print("score: 1")')
        with patch.dict('os.environ', {'SIMPLE_AR_TEST_SECRET': 'not available to task'}):
            app.run(actions=[action('measure_experiment', command=['python', '-c', program],
                timeout_sec=10, result_schema={'required_metrics': ['score'],
                    'output_files': {'observation': 'observed.json'}})],
                progress=lambda _: None, approve=lambda *_: self.fail('Workspace measurement should not pause'))
        observed = app.session.store.read_json(app.state['turns'][-1]['tools'][-1]['result'])
        self.assertEqual(observed['status'], 'ok', observed)
        outputs = list((app.session.store.root / 'attempts').glob('*/execution/process/*/outputs/observed.json'))
        self.assertEqual(len(outputs), 1)
        facts = json.loads(outputs[0].read_text())
        self.assertFalse(facts['source_visible'])
        self.assertFalse(facts['secret_visible'])
        self.assertNotEqual(facts['network_result'], 0)
        receipt = json.loads((outputs[0].parent.parent / 'invocation.json').read_text())
        self.assertEqual(receipt['execution_scope'], 'workspace')
        self.assertEqual(app.ledger.entries[-1].status, 'settled')

    def test_specialized_scope_preserves_old_tasks_and_requires_approval_for_setup(self):
        def make(name):
            return ToolApplication.create(self.base / name, 'Measure', model=None, sources={},
                allow_execution=True, sandbox_execution=True,
                process_limits={'process_invocations': 3, 'process_wall_seconds': 60})
        legacy = make('legacy-science')
        legacy.state['config'].pop('sandbox_operations')
        legacy._save()
        legacy = ToolApplication.load(legacy.session.store.root)
        request = action('measure_experiment', command=['python', '-c', 'print("score: 1")'],
            timeout_sec=5, result_schema={'required_metrics': ['score']})
        result = legacy.run(actions=[request], progress=lambda _: None)
        self.assertEqual(result['status'], 'paused')
        self.assertIn('explicit approval', legacy.session.manifest.status_reason)
        self.assertFalse(legacy.ledger.entries)

        setup = make('environment-science')
        request['arguments']['environment'] = {'mode': 'current', 'timeout_sec': 5,
            'check_command': ['python', '-c', 'print("checked")']}
        result = setup.run(actions=[request], progress=lambda _: None)
        self.assertEqual(result['status'], 'paused')
        self.assertIn('explicit approval', setup.session.manifest.status_reason)
        self.assertFalse(setup.ledger.entries)

    def test_scoped_code_probes_and_validator_use_the_same_workspace_boundary(self):
        from types import SimpleNamespace
        from simple_ar.code_task.analysis.dependency_api import inspect_dependency_api
        from simple_ar.code_task.execution.runner import run_code_task_benchmark
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
        project = self.source
        (project / 'main.py').write_text('import json\nVALUE = 1\n')
        outside = self.base / 'outside.txt'
        other_result = self.base / 'code/workspace/other-result.txt'
        (project / 'check.py').write_text(
            'from pathlib import Path\n'
            f'paths = [{str(outside)!r}, {str(other_result)!r}]\n'
            'for name in paths:\n'
            ' try:\n  Path(name).write_text("changed")\n'
            ' except OSError:\n  print("outside blocked")\n')
        contract = {'project_path': 'source', 'goal': 'Change the model',
            'allowed_patterns': ['main.py'], 'validation_command': ['python', 'check.py'],
            'timeout_sec': 5, 'preparation': {'kind': 'existing_project', 'workspace_mode': 'copy'}}
        class UncalledClient:
            model, context_input_tokens = 'fixture', 128000
            def with_budget(self, *_args, **_kwargs):
                return self
            def ask_json(self, *_args, **_kwargs):
                raise AssertionError('This engineering check must not call a model')
        services = SimpleNamespace(llm_client=UncalledClient())
        app = ToolApplication.create(self.base / 'code', 'Change the model', model='fixture', services=services,
            sources={'source': project}, allow_execution=True, sandbox_execution=True,
            code_task=contract, process_limits={'process_invocations': 5, 'process_wall_seconds': 60})
        other_result.write_text('retained')
        call = {'function': {'name': 'implement_code_task', 'arguments': '{}'}}
        pending = {'attempt': 'code-scope', 'call': call}
        app.state['pending'] = pending
        self.assertTrue(app._approve_action(pending, '', lambda *_: self.fail('Scoped code should not pause'),
                                            lambda _: None, scoped=True))
        self.assertEqual(pending['execution_approval']['binding']['execution_scope'], 'workspace.v1')
        context = CapabilityContext(ArtifactStore(app.session.store.root / 'attempts/code-scope'),
                                    AttemptManifest('code-scope'))
        checked = []
        def exercise(*, context, request):
            scope = request.process_scope
            self.assertIs(scope.ledger, app.ledger)
            index = {'files': [{'path': 'main.py', 'python': {'imports': ['json']}}]}
            api = inspect_dependency_api([{'module': 'json', 'symbols': ['loads']}], index, ['main.py'],
                python_executable=str(app.workspace.python), workspace_dir=request.cwd, process_scope=scope)
            self.assertEqual(api['status'], 'observed')
            validation = run_code_task_benchmark(request.run_dir, command='python check.py',
                timeout_sec=5, skip_validation=False, budget_ledger=app.ledger,
                session_id=app.session.manifest.session_id, attempt_id='code-scope', process_scope=scope)
            self.assertEqual(validation.status, 'passed')
            self.assertIn('outside blocked', validation.stdout_path.read_text())
            self.assertFalse(outside.exists())
            self.assertEqual(other_result.read_text(), 'retained')
            checked.append(True)
            raise RuntimeError('Stop after exercising the actual isolated processes')
        with patch.object(app.implementation, '_client', return_value=object()), patch(
                'simple_ar.research.implementation.run_implementation_capability', side_effect=exercise):
            with self.assertRaisesRegex(RuntimeError, 'actual isolated processes'):
                app.implementation.execute(context, {}, approval=pending['execution_approval'])
        self.assertTrue(checked)
        self.assertTrue(any(row.purpose == 'code_probe' and row.status == 'settled' for row in app.ledger.entries))
        self.assertTrue(any(row.purpose == 'local_process' and row.status == 'settled' for row in app.ledger.entries))
        self.assertFalse(outside.exists())
        self.assertEqual(other_result.read_text(), 'retained')

        setup_contract = {**contract, 'environment': {'mode': 'current', 'timeout_sec': 5,
            'check_command': ['python', '-c', 'print("checked")']}}
        setup = ToolApplication.create(self.base / 'code-setup', 'Change the model', model='fixture', services=services,
            sources={'source': project}, allow_execution=True, sandbox_execution=True,
            code_task=setup_contract, process_limits={'process_invocations': 5, 'process_wall_seconds': 60})
        pending_setup = {'attempt': 'code-setup', 'call': call}
        setup.state['pending'] = pending_setup
        self.assertFalse(setup._approve_action(pending_setup, '', None, lambda _: None, scoped=True))
        self.assertIn('explicit approval', setup.session.manifest.status_reason)
        self.assertFalse(setup.ledger.entries)

    def test_scoped_code_task_baseline_uses_the_same_boundary_and_accounting(self):
        from simple_ar.code_task import execute_code_task, initialize_code_task
        from simple_ar.code_task.execution.runner import run_code_task_benchmark
        from simple_ar.code_task.execution.scope import CodeTaskProcessScope
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.core.process_sandbox import ProcessSandbox

        source = self.base / 'baseline-project'
        source.mkdir()
        outside = self.base / 'outside-baseline.txt'
        (source / 'check.py').write_text(
            'from pathlib import Path\n'
            f'target = Path({str(outside)!r})\n'
            'try:\n target.write_text("outside")\n'
            'except OSError:\n print("outside blocked")\n'
            'print("score: 1")\n')
        task = self.base / 'baseline-task.txt'
        task.write_text('Check the existing project')
        run = initialize_code_task(run_dir=self.base / 'baseline-run', code_root=source,
            task_file=task, benchmark_command='python check.py',
            env_mode='external', python_executable=sys.executable)
        workspace = run.workspace_dir.resolve()
        ledger = BudgetLedger({'process_invocations': 4, 'process_wall_seconds': 60})
        sandbox = ProcessSandbox(workspace, workspace, Path(sys.executable))
        scope = CodeTaskProcessScope(sandbox, ledger, 'unit', 'baseline',
            self.base / 'baseline-probes', {'PATH': '/usr/bin:/bin', 'HOME': str(workspace)}, 5)
        result = execute_code_task(run.run_dir, to_step='baseline', use_llm=False,
            baseline_policy='run', timeout_sec=5, process_scope=scope)
        self.assertEqual(result.stop_reason, 'stop_point')
        report = json.loads((run.run_dir / 'code_task/run/baseline/execution_report.json').read_text())
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['process']['execution_scope'], 'workspace')
        self.assertFalse(outside.exists())
        self.assertTrue(any(entry.purpose == 'code_probe' and entry.status == 'settled'
                            for entry in ledger.entries))
        self.assertTrue(any(entry.purpose == 'local_process' and entry.status == 'settled'
                            for entry in ledger.entries))

        replacement = self.base / 'replacement-python'
        replacement.write_text(f'#!/bin/sh\nprintf unsafe > {str(outside)}\n')
        replacement.chmod(0o755)
        before = ledger.to_dict()
        with self.assertRaisesRegex(ValueError, 'saved Python environment'):
            run_code_task_benchmark(run.run_dir, env_mode='external',
                python_executable=str(replacement), process_scope=scope)
        self.assertFalse(outside.exists())
        self.assertEqual(ledger.to_dict(), before)

        greenfield = initialize_code_task(run_dir=self.base / 'greenfield-run', code_root=None,
            task_file=task, kind='greenfield', workspace_mode='empty', benchmark_command='python main.py')
        before = ledger.to_dict()
        with self.assertRaisesRegex(ValueError, 'Scoped greenfield orchestration is not supported'):
            execute_code_task(greenfield.run_dir, to_step='baseline', use_llm=False, process_scope=scope)
        self.assertEqual(ledger.to_dict(), before)

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
