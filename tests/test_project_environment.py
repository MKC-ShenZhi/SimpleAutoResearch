import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext
from simple_ar.experiment.execution.backend import RunRequest, RunResult
from simple_ar.research.preparation import PreparationRequest, run_preparation_capability
from simple_ar.research.project_environment import (
    environment_profile, project_environment_plan, project_environment_process_env,
)
from simple_ar.cli.parser import build_parser
from simple_ar.cli.start import _inline_value, prepare_start
from simple_ar.cli.research_config import research_defaults
from simple_ar.research.task_plan import TaskPlanRequest, default_task_steps, build_task_plan


class ProjectEnvironmentTests(unittest.TestCase):
    def test_session_reuses_completed_check_and_measures_only_the_formal_command(self):
        # Historical sessions retain their separate preparation/resume contract.
        from simple_ar.app.research_application import create_session, load_session, ResearchApplicationServices
        from simple_ar.research.workflow_contracts import ResearchBrief
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data.txt').write_text('0.5')
            paper = root / 'paper.md'
            paper.write_text('# Fixture\nMeasure the supplied value, not the preparation score.')
            check = "from pathlib import Path; assert Path('data.txt').is_file(); Path('checks').write_text('once'); print('score: 999')"
            formal = "from pathlib import Path; Path('runs').write_text('once'); print('score: ' + Path('data.txt').read_text())"
            app = create_session(ResearchBrief(request_text='Measure supplied data', requested_outputs=('experiments',),
                asset_requests=({'locator': str(paper), 'role': 'paper'},)), root=root / 'session',
                services=ResearchApplicationServices(config={'research_task_kind': 'measurement', 'execution': {
                    'command': [sys.executable, '-c', formal], 'cwd': str(root), 'timeout_sec': 5,
                    'result_schema': {'primary_metric': 'score', 'required_metrics': ['score']},
                    'environment': {'mode': 'current', 'check_command': [sys.executable, '-c', check], 'timeout_sec': 5}}},
                    budget_limits={'process_invocations': 2, 'process_wall_seconds': 10}))
            for _ in range(10):
                if app.view().next_action == 'experiment':
                    break
                app.advance()
            self.assertEqual(app.view().next_action, 'experiment', app.view().status_reason)
            self.assertTrue((root / 'checks').exists())
            self.assertFalse((root / 'runs').exists())
            app = load_session(root / 'session')
            view = app.advance(max_actions=10)
            self.assertEqual(view.status, 'completed', view.status_reason)
            measured = app.controller.store.read_json(view.state_refs['experiment'])
            self.assertEqual(measured['metrics']['score'], 0.5)
            self.assertEqual(app.budget_ledger.remaining('process_invocations'), 0)
            snapshots = {name: (root / name).stat().st_mtime_ns for name in ('checks', 'runs')}
            self.assertEqual(load_session(root / 'session').advance(max_actions=10).status, 'completed')
            self.assertEqual(snapshots, {name: (root / name).stat().st_mtime_ns for name in snapshots})

    def test_current_check_executes_without_installation_and_never_publishes_check_metrics(self):
        from simple_ar.experiment.execution.backend import LocalExecutionBackend
        for program, timeout, expected in (
            ('print("score=999")', 5, 'completed'),
            ('raise SystemExit(2)', 5, 'failed'),
            ('import time; time.sleep(3)', 1, 'failed'),
        ):
            with self.subTest(program=program), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                context = self.context(root)
                request = self.request(root, environment={'mode': 'current',
                    'check_command': [sys.executable, '-c', program], 'timeout_sec': timeout})
                paths = list(root.rglob('*'))
                plan = project_environment_plan(request.execution['environment'], root,
                    context.store.root / 'environment', request.run.command)
                self.assertEqual(list(root.rglob('*')), paths)
                self.assertEqual(plan, {'profile': environment_profile(request.execution['environment'], project=root),
                    'steps': [[sys.executable, '-c', program]], 'command': request.run.command,
                    'python_executable': None})
                explicit = {'PYTHONPATH': '/selected/packages', 'PYTHONHOME': '/selected/python'}
                self.assertIs(project_environment_process_env(plan['profile'], explicit), explicit)
                self.assertIsNone(project_environment_process_env(plan['profile'], None))
                result = run_preparation_capability(context=context, request=request, backend=LocalExecutionBackend())
                self.assertEqual(result.status, expected)
                self.assertFalse((context.store.root / 'environment').exists())
                receipt = context.store.read_json('environment_setup.json')
                self.assertEqual(len(receipt['steps']), 1)
                self.assertFalse(any(ref.kind == 'experiment' for ref in result.artifacts))
                if expected == 'completed':
                    prepared = context.store.read_json('execution.json')
                    self.assertEqual(prepared['execution']['command'], request.execution['command'])
                    self.assertEqual(prepared['execution']['timeout_sec'], 19)
                    self.assertNotIn('metrics', prepared['environment'])
                else:
                    self.assertFalse((context.store.root / 'execution.json').exists())
                with self.assertRaisesRegex(ValueError, 'already exists'):
                    run_preparation_capability(context=context, request=request, backend=LocalExecutionBackend())

    def test_current_check_guided_config_and_budget_share_the_preparation_owner(self):
        from simple_ar.app.tool_application import ToolApplication
        from simple_ar.cli.main import main
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / 'paper.md').write_text('Reference')
            (root / 'data.txt').write_text('0.5')
            (root / 'check.py').write_text("from pathlib import Path\nassert Path('data.txt').is_file()\n"
                "with Path('checks').open('a') as f: f.write('checked\\n')\nprint('score: 999')\n")
            (root / 'run.py').write_text("from pathlib import Path\n"
                "with Path('measurements.log').open('a') as f: f.write('measured\\n')\n"
                "print('score: ' + Path('data.txt').read_text())\n")
            args = build_parser().parse_args(['start', '--kind', 'reproduction', '--goal', 'Check conclusion',
                '--document', str(root / 'paper.md'), '--project', str(root),
                '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare score',
                '--metric', 'score', '--timeout-sec', '7', '--prepare-only', '--output-root', str(root / 'runs'),
                '--check-argv', json.dumps([sys.executable, 'check.py']),
                '--command', sys.executable, 'run.py', '--fixed', '42'])
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(args)
            defaults = research_defaults(['research-session', '--config', str(config)])
            profile = defaults['execution_details']['environment']
            self.assertEqual(profile['check_command'], [sys.executable, 'check.py'])
            self.assertEqual(defaults['process_invocations'], 2)
            self.assertEqual(defaults['process_wall_seconds'], 14)
            plan = project_environment_plan(profile, root, root / 'future-attempt' / 'environment', defaults['command_argv'])
            self.assertEqual(defaults['process_invocations'], len(plan['steps']) + 1)
            self.assertFalse((root / 'future-attempt').exists())
            # The reproduction bundle keeps its research controls. Its literal
            # execution/budget can also be explicitly requested as measurement.
            execution = {**defaults['execution_details'], 'command': defaults['command_argv'],
                'cwd': defaults['cwd'], 'timeout_sec': defaults['timeout_sec'], 'primary_metric': defaults['primary_metric'],
                'metrics': defaults['metric']}
            measurement = root / 'measurement.toml'
            measurement.write_text('[task]\nkind="measurement"\ngoal="Observe declared data"\noutputs=["experiments"]\n'
                '[model]\nname=""\n[execution]\n'
                + ''.join(f'{key}={_inline_value(value)}\n' for key, value in execution.items())
                + '[budget]\n' + ''.join(f'{key}={defaults[key]}\n' for key in
                    ('process_invocations', 'process_wall_seconds')), encoding='utf-8')
            with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stdout(io.StringIO()) as output, \
                    patch.dict('os.environ', {'SAR_ENV_DISPLAY_SECRET': 'not-for-approval-display'}), \
                    patch('simple_ar.app.research_application.create_session', side_effect=AssertionError('No legacy new session')), \
                    patch('simple_ar.app.tool_application.LLMClient.from_env', side_effect=AssertionError('No model')):
                main(['research-session', '--config', str(measurement), '--output-root', str(root / 'native')])
                display = ' '.join(output.getvalue().split())
                self.assertIn('Environment preparation steps (literal argv):', display)
                self.assertIn(''.join(json.dumps([sys.executable, 'check.py'], ensure_ascii=False).split()),
                    ''.join(output.getvalue().split()))
                self.assertIn('2 invocations; <= 14s declared wall reservation', display)
                self.assertIn('Shared process limits:', display)
                self.assertNotIn('SAR_ENV_DISPLAY_SECRET', output.getvalue())
                self.assertNotIn('not-for-approval-display', output.getvalue())
                app = ToolApplication.load(next((root / 'native').rglob('session_manifest.json')).parent)
                self.assertEqual(app.session.manifest.status, 'completed', app.session.manifest.status_reason)
                self.assertIsNone(app.state['config']['model'])
                self.assertEqual(app.state['config']['execution_contract']['environment'], profile)
                self.assertEqual(app.ledger.limits['process_invocations'], defaults['process_invocations'])
                self.assertEqual(app.ledger.limits['process_wall_seconds'], defaults['process_wall_seconds'])
                self.assertEqual(app.ledger.remaining('process_invocations'), 0)
                self.assertEqual(len(app.ledger.entries), 2)
                observations = [app.session.store.read_json(item['result'])
                    for turn in app.state['turns'] for item in turn['tools']]
                self.assertEqual([row['tool_name'] for row in observations],
                    ['update_work_plan', 'measure_experiment'])
                self.assertTrue(all(row['status'] == 'ok' for row in observations), observations)
                self.assertEqual(observations[0]['data']['report_intent']['kind'], 'none')
                attempts = app.session.list_attempts()
                self.assertEqual([row.capability for row in attempts], ['native_tool', 'native_tool'])
                observed = observations[1]
                canonical = app.workspace.resolve(observed['data']['result_ref'])
                self.assertEqual(canonical.parent.name, attempts[1].attempt_id)
                self.assertEqual({entry.attempt_id for entry in app.ledger.entries}, {canonical.parent.name})
                measured = json.loads(canonical.read_text(encoding='utf-8'))
                self.assertEqual(measured['metrics'], {'score': 0.5})
                self.assertEqual(measured['command'], defaults['command_argv'])
                self.assertEqual(app.session.store.resolve(measured['preparation']['source_ref']['path']),
                    canonical.with_name('execution.json'))
                self.assertIn('score: 999', (canonical.parent / 'environment_logs/1.stdout.txt').read_text())
                self.assertTrue(any(path.endswith('/analysis.md') for path in
                    app.session.store.read_json('delivery.json')['artifacts']))
                with patch('simple_ar.experiment.execution.backend.run_process') as process:
                    main(['start', '--resume-task', str(app.session.store.root), '--yes'])
                    process.assert_not_called()
                self.assertEqual(ToolApplication.load(app.session.store.root).session.manifest.status, 'completed')
                self.assertEqual((root / 'checks').read_text().splitlines(), ['checked'])
                self.assertEqual((root / 'measurements.log').read_text().splitlines(), ['measured'])

    def test_application_preparation_requires_declared_scope_not_fake_research_design(self):
        from simple_ar.app.research_application import create_session, ResearchApplicationServices
        from simple_ar.research.workflow_contracts import ResearchBrief
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = {'execution': {'command': ['python', 'run.py'], 'cwd': str(root), 'timeout_sec': 7,
                                   'environment': {'mode': 'venv'}}, 'research_task_kind': 'measurement'}
            app = create_session(ResearchBrief(request_text='Prepare declared environment', requested_outputs=('experiments',)),
                                 root=root / 'session', services=ResearchApplicationServices(config=config))
            with patch.object(app, '_execute', return_value=True) as execute:
                self.assertTrue(app._run_prepare_execution_action())
            self.assertNotIn('design', [ref.kind for ref in execute.call_args.args[3]])
            self.assertEqual(execute.call_args.args[2].execution['environment']['mode'], 'venv')

    def context(self, root):
        return CapabilityContext(ArtifactStore(root / 'attempt'), AttemptManifest('prepare-1'))

    def request(self, root, **changes):
        config = {'command': ['python', 'run.py', '--fixed', '42'], 'cwd': str(root), 'timeout_sec': 19,
                  'environment': {'mode': 'venv', 'requirements': ['requirements.txt'], 'timeout_sec': 7}}
        config.update(changes)
        return PreparationRequest(config, 'Preserve the fixed method', RunRequest(config['command'], root, 19, session_id='session', attempt_id='prepare-1'))

    def test_profile_requires_explicit_bounded_scope_without_install_commands(self):
        for value in (None, {}, {'mode': 'current'}, {'mode': 'venv', 'commands': ['pip install x']},
                      {'mode': 'venv', 'requirements': 'requirements.txt'}, {'mode': 'venv', 'timeout_sec': True},
                      {'mode': 'venv', 'requirements': ['../outside.txt']}, {'mode': 'venv', 'requirements': ['-r outside']},
                      {'mode': 'venv', 'requirements': ['a\\b.txt']}, {'mode': 'venv', 'python_executable': ''}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                environment_profile(value)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(ValueError, 'not found inside'):
                environment_profile({'mode': 'venv', 'requirements': ['absent.txt']}, project=root)
            for mode in ('current', 'venv'):
                with self.subTest(same_command=mode), self.assertRaisesRegex(ValueError, 'must differ'):
                    project_environment_plan({'mode': mode, 'check_command': ['python', 'formal.py']},
                        root, root / 'environment', ['python', 'formal.py'])
        self.assertEqual(environment_profile({'mode': 'venv'})['requirements'], [])

    def test_each_step_uses_same_backend_and_final_command_preserves_scientific_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'requirements.txt').write_text('numpy==2.0.0\n')
            context = self.context(root)
            calls = []
            class Backend:
                def run(self, request):
                    calls.append(request)
                    if len(calls) == 1:
                        import os
                        python = Path(request.command[-1]) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
                        python.parent.mkdir(parents=True)
                        python.touch()
                    return RunResult(0, False, 'observed stdout', '', command=request.command, cwd=str(request.cwd))
            request = self.request(root,
                baseline={'command': ['python3', 'control.py', '--seed', '42'], 'label': 'control'},
                environment={'mode': 'venv', 'requirements': ['requirements.txt'], 'timeout_sec': 7,
                             'check_command': ['python.exe', 'check.py']})
            paths = list(root.rglob('*'))
            plan = project_environment_plan(request.execution['environment'], root,
                context.store.root / 'environment', request.run.command)
            self.assertEqual(list(root.rglob('*')), paths)
            self.assertEqual(request.run.command, ['python', 'run.py', '--fixed', '42'])
            self.assertEqual(request.execution['environment']['check_command'], ['python.exe', 'check.py'])
            explicit = {'PYTHONPATH': '/selected/packages', 'PYTHONHOME': '/selected/python', 'PIP_INDEX_URL': 'selected-index'}
            self.assertEqual(project_environment_process_env(plan['profile'], explicit), {'PIP_INDEX_URL': 'selected-index'})
            self.assertEqual(set(explicit), {'PYTHONPATH', 'PYTHONHOME', 'PIP_INDEX_URL'})
            with patch.dict('os.environ', {'PYTHONPATH': '/controller/packages', 'PYTHONHOME': '/controller/python',
                                         'PIP_INDEX_URL': 'https://pypi.org/simple'}):
                inherited = project_environment_process_env(plan['profile'], None)
                self.assertNotIn('PYTHONPATH', inherited)
                self.assertNotIn('PYTHONHOME', inherited)
                self.assertEqual(inherited['PIP_INDEX_URL'], 'https://pypi.org/simple')
                result = run_preparation_capability(context=context, request=request, backend=Backend())
            self.assertEqual(result.status, 'completed')
            self.assertEqual(len(calls), 4)
            self.assertEqual([row.command for row in calls], plan['steps'])
            self.assertEqual([row.label for row in calls], [f'environment:{number}' for number in range(1, 5)])
            self.assertTrue(all('PYTHONPATH' not in row.env and 'PYTHONHOME' not in row.env for row in calls))
            self.assertTrue(all(row.env['PIP_INDEX_URL'] == 'https://pypi.org/simple' for row in calls))
            self.assertTrue(all(row.timeout_sec == 7 and row.session_id == 'session' and row.attempt_id == 'prepare-1' for row in calls))
            self.assertEqual(calls[0].command[:3], [sys.executable, '-m', 'venv'])
            self.assertEqual(calls[1].command[-2:], ['-r', str(root / 'requirements.txt')])
            self.assertEqual(calls[2].command[-3:], ['-m', 'pip', 'check'])
            self.assertEqual(calls[3].command, [calls[2].command[0], 'check.py'])
            payload = context.store.read_json(next(ref for ref in result.artifacts if ref.kind == 'prepared_execution'))
            self.assertNotIn('environment', payload['execution'])
            self.assertEqual(payload['execution']['command'], plan['command'])
            self.assertEqual(payload['environment']['python_executable'], plan['python_executable'])
            self.assertEqual(payload['execution']['command'][1:], ['run.py', '--fixed', '42'])
            self.assertTrue(payload['execution']['command'][0].startswith(str(context.store.root)))
            self.assertEqual(payload['execution']['timeout_sec'], 19)
            from simple_ar.app.research_execution import execution_request
            control = execution_request(payload['execution'], condition='baseline').run
            candidate = execution_request(payload['execution']).run
            self.assertEqual(control.command[0], candidate.command[0])
            self.assertEqual(control.command[1:], ['control.py', '--seed', '42'])
            self.assertEqual(control.label, 'control')
            self.assertIn('scientific validity', ' '.join(payload['limitations']))

    def test_project_install_is_opt_in_and_requires_a_declared_package_before_launch(self):
        self.assertFalse(environment_profile({'mode': 'venv'})['install_project'])
        for value in ('true', 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'boolean'):
                environment_profile({'mode': 'venv', 'install_project': value})
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            context = self.context(root)
            with self.assertRaisesRegex(ValueError, 'packaging|pyproject'), patch(
                    'simple_ar.research.project_environment.LocalExecutionBackend.run', side_effect=AssertionError('no launch')):
                run_preparation_capability(context=context, request=self.request(root,
                    environment={'mode': 'venv', 'install_project': True}))
            self.assertFalse((context.store.root / 'environment').exists())
            for name in ('pyproject.toml', 'setup.py', 'setup.cfg'):
                (root / name).touch()
                self.assertTrue(environment_profile({'mode': 'venv', 'install_project': True}, project=root)['install_project'])
                (root / name).unlink()

    def test_project_and_requirements_share_one_resolver_invocation(self):
        for requirements in ([], ['requirements.txt']):
            with self.subTest(requirements=requirements), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'pyproject.toml').write_text('[project]\nname="fixture"\nversion="0.1"\n')
                (root / 'requirements.txt').write_text('numpy==2.0.0\n')
                calls = []
                class Backend:
                    def run(self, request):
                        calls.append(request)
                        if len(calls) == 1:
                            import os
                            python = Path(request.command[-1]) / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
                            python.parent.mkdir(parents=True)
                            python.touch()
                        return RunResult(0, False, 'observed', '', command=request.command)
                context = self.context(root)
                request = self.request(root,
                    baseline={'command': ['explicit-baseline-python', 'control.py']},
                    environment={'mode': 'venv', 'requirements': requirements, 'install_project': True})
                plan = project_environment_plan(request.execution['environment'], root,
                    context.store.root / 'environment', request.run.command)
                result = run_preparation_capability(context=context, request=request, backend=Backend())
                self.assertEqual(result.status, 'completed')
                self.assertEqual(len(calls), 3)
                self.assertEqual([row.command for row in calls], plan['steps'])
                self.assertEqual(calls[1].command[1:5], ['-m', 'pip', 'install', '--disable-pip-version-check'])
                self.assertEqual(calls[1].command[5:],
                                 (['-r', str(root / 'requirements.txt')] if requirements else []) + [str(root)])
                payload = context.store.read_json('execution.json')
                self.assertTrue(payload['environment']['install_project'])
                self.assertEqual(payload['execution']['command'][1:], ['run.py', '--fixed', '42'])
                self.assertEqual(payload['execution']['baseline']['command'], ['explicit-baseline-python', 'control.py'])

    def test_project_build_failure_cannot_publish_a_prepared_scientific_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'setup.py').write_text('raise RuntimeError("build error")')
            calls = []
            class Backend:
                def run(self, request):
                    calls.append(request)
                    return RunResult(0, False, '', '') if len(calls) == 1 else RunResult(1, False, '', 'build error')
            context = self.context(root)
            result = run_preparation_capability(context=context, request=self.request(root,
                environment={'mode': 'venv', 'install_project': True}), backend=Backend())
            self.assertEqual(result.status, 'failed')
            self.assertEqual(len(calls), 2)
            self.assertFalse((context.store.root / 'execution.json').exists())
            self.assertTrue(context.store.read_json('environment_setup.json')['profile']['install_project'])

    def test_failed_or_timed_out_install_stops_before_science_and_preserves_observations(self):
        for failure in (RunResult(1, False, '', 'install failed'), RunResult(None, True, '', 'timeout')):
            with self.subTest(failure=failure.status), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / 'requirements.txt').write_text('some-dependency\n')
                context = self.context(root)
                calls = []
                class Backend:
                    def run(self, request):
                        calls.append(request)
                        return RunResult(0, False, '', '') if len(calls) == 1 else failure
                result = run_preparation_capability(context=context, request=self.request(root), backend=Backend())
                self.assertEqual(result.status, 'failed')
                self.assertEqual(len(calls), 2)
                self.assertFalse(any(ref.kind == 'prepared_execution' for ref in result.artifacts))
                self.assertEqual(context.store.read_json('environment_setup.json')['steps'][-1]['status'], failure.status)

    def test_interruption_records_unknown_step_and_no_in_place_environment_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'requirements.txt').write_text('')
            context = self.context(root)
            class Interrupted:
                def run(self, request):
                    (context.store.root / 'environment').mkdir()
                    raise RuntimeError('worker stopped after allocation')
            with self.assertRaisesRegex(RuntimeError, 'worker stopped'):
                run_preparation_capability(context=context, request=self.request(root), backend=Interrupted())
            self.assertEqual(context.store.read_json('environment_setup.json')['steps'][0]['status'], 'allocated_result_unknown')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                run_preparation_capability(context=context, request=self.request(root), backend=Interrupted())

    def test_explicit_interpreter_and_combined_workflows_are_not_silently_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'requirements.txt').write_text('')
            for changes in ({'command': [sys.executable, 'run.py']}, {'code_task': {}}, {'dataset': 'x.csv'}, {'pairs': [{}]}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    run_preparation_capability(context=self.context(root), request=self.request(root, **changes))

    def test_guided_option_serializes_same_profile_and_counts_preparation_invocations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'paper.md').write_text('A supplied reference')
            (root / 'requirements.txt').write_text('numpy==2.0.0\n')
            args = build_parser().parse_args(['start', '--kind', 'reproduction', '--goal', 'Check source conclusion',
                '--document', str(root / 'paper.md'), '--project', str(root), '--environment', 'venv',
                '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare observed score',
                '--metric', 'score', '--timeout-sec', '11', '--prepare-only', '--output-root', str(root / 'runs'),
                '--command', 'python', 'run.py'])
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(args)
            defaults = research_defaults(['research-session', '--config', str(config)])
            profile = defaults['execution_details']['environment']
            self.assertEqual(profile['requirements'], ['requirements.txt'])
            self.assertEqual(profile['timeout_sec'], 11)
            self.assertEqual(defaults['process_invocations'], 4)
            self.assertEqual(defaults['process_wall_seconds'], 44)
            self.assertEqual(defaults['command_argv'], ['python', 'run.py'])
            plan = project_environment_plan(profile, root, root / 'budget-preview' / 'environment', defaults['command_argv'])
            self.assertEqual(defaults['process_invocations'], len(plan['steps']) + 1)
            self.assertFalse((root / 'budget-preview').exists())
            self.assertNotIn('__attempt__', config.read_text(encoding='utf-8'))

    def test_measurement_plan_has_only_confirmed_preparation_then_measurement_analysis(self):
        request = TaskPlanRequest(goal='Observe fixed runtime', request_text='Observe fixed runtime', task_kind='measurement', requested_outputs=('experiments',),
            execution={'command': ['python', 'run.py'], 'environment': {'mode': 'venv'}}, execution_protocol_accepted=True)
        self.assertEqual([row['action'] for row in default_task_steps(request)], ['prepare_execution', 'experiment', 'analysis'])
        plan = build_task_plan(request)
        self.assertEqual([row.action for row in plan.steps], ['prepare_execution', 'experiment', 'analysis'])

    def test_explicit_cli_project_install_serializes_and_counts_the_shared_install_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'paper.md').write_text('Supplied reference')
            (root / 'setup.cfg').write_text('[metadata]\nname=fixture\n')
            args = build_parser().parse_args(['start', '--kind', 'reproduction', '--goal', 'Check fixed conclusion',
                '--document', str(root / 'paper.md'), '--project', str(root), '--environment', 'venv',
                '--install-project', '--hypothesis', 'Claim', '--dataset', 'Fixed data', '--expected-outcome', 'Compare score',
                '--metric', 'score', '--timeout-sec', '11', '--prepare-only', '--output-root', str(root / 'runs'),
                '--command', 'python', 'run.py'])
            with patch('sys.stdin.isatty', return_value=False), patch('simple_ar.cli.start.print_line'):
                config = prepare_start(args)
                args.environment = 'current'
                with self.assertRaisesRegex(ValueError, 'requires --kind reproduction --environment venv'):
                    prepare_start(args)
            defaults = research_defaults(['research-session', '--config', str(config)])
            self.assertTrue(defaults['execution_details']['environment']['install_project'])
            self.assertEqual(defaults['execution_details']['environment']['requirements'], [])
            self.assertEqual(defaults['process_invocations'], 4)
            self.assertEqual(defaults['process_wall_seconds'], 44)
