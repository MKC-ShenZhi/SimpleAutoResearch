"""Integrated local HTTP/owner boundary checks; no provider calls.

Run on the verification server: python -m unittest discover -s tests -p test_local_ui.py
"""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote
from unittest.mock import patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient
from simple_ar.ui import Workspace, bounded, native_arguments, native_creation_options, server


def call(name, arguments, identity='test-call'):
    return {'tool_name': name, 'arguments': arguments}


class LocalWorkspaceTests(unittest.TestCase):
    def test_http_owner_feedback_approval_records_and_guards(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            project = base / '.research' / 'secret-study' / '材料'
            project.mkdir(parents=True)
            (project / 'source.txt').write_text('Observed input', encoding='utf-8')
            (project / '.env').write_text('DO_NOT_EXPOSE', encoding='utf-8')
            source_file = project / '观察.txt'
            source_file.write_text('实测输入 café', encoding='utf-8')
            (project / '分组').mkdir()
            (project / '分组' / '输入.txt').write_text('分组输入', encoding='utf-8')
            for private in ('.hidden', '.ssh/id_rsa', 'credentials/token.txt', 'logs/run.log', 'models.toml'):
                target = project / private
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('DO_NOT_EXPOSE', encoding='utf-8')
            tasks = base / '.research' / 'logs' / 'tasks'
            workspace = Workspace(project, [source_file], tasks)
            name = '保留任务 café'
            root = tasks / name
            app = ToolApplication.create(root, 'Inspect retained evidence', model=None,
                sources={'source0': project, 'source1': source_file, 'source2': project / 'models.toml'},
                allow_execution=True, max_attempts=30,
                process_limits={'process_invocations': 1, 'process_wall_seconds': 30})
            app.run(actions=[call('write_file', {'path': 'workspace/report.txt', 'content': 'first report',
                'delivery': {'summary': 'Initial result', 'artifacts': ['workspace/report.txt']} })],
                progress=lambda _: None)
            initial = (root / 'workspace/report.txt').read_bytes()
            httpd = server(workspace)
            serving = threading.Thread(target=httpd.serve_forever, daemon=True)
            serving.start()

            def request(path, data=None, *, authenticated=True, origin=None, host=None):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=10)
                headers = {'Host': host or httpd.authority}
                if authenticated:
                    headers['Authorization'] = 'Bearer ' + httpd.session_token
                if data is not None:
                    headers.update({'Content-Type': 'application/json', 'Origin': origin or httpd.origin})
                connection.request('POST' if data is not None else 'GET', quote(path, safe='/?=&%'),
                    body=json.dumps(data) if data is not None else None, headers=headers)
                response = connection.getresponse()
                result = response.status, response.read()
                connection.close()
                return result

            def action(op, **kwargs):
                status, raw = request('/api/action', {'op': op, 'task': name, **kwargs})
                self.assertEqual(status, 200, raw)
                if op in {'feedback', 'resume', 'approve'}:
                    workspace.workers[name][0].join(10)
                    self.assertFalse(workspace.workers[name][0].is_alive())
                return json.loads(raw)

            try:
                self.assertEqual(request('/')[0], 200)
                self.assertEqual(request('/api/tasks', authenticated=False)[0], 403)
                self.assertEqual(request('/api/tasks', host='evil.invalid')[0], 403)
                self.assertEqual(json.loads(request('/api/tasks')[1]),
                    [dict(task=name, topic='Inspect retained evidence', status='completed')])
                self.assertEqual(workspace.task('.'), tasks)
                for invalid in ('', '..', str(root), name + '/workspace'):
                    with self.assertRaises(ValueError):
                        workspace.task(invalid)
                self.assertEqual(request('/api/action', {'op': 'resume', 'task': name},
                                         origin='http://evil.invalid')[0], 403)
                status, raw = request(f'/api/task?task={name}')
                self.assertEqual(status, 200)
                view = json.loads(raw)
                self.assertEqual(view['manifest']['status'], 'completed')
                self.assertEqual(view['mode'], 'saved results browser')
                delivery = json.loads(request(f'/api/delivery?task={name}')[1])
                self.assertTrue(delivery['current'])
                self.assertTrue(any(f['path'] == 'report.txt' for f in delivery['files']))
                (root / 'workspace/report.txt').write_text('untracked replacement', encoding='utf-8')
                self.assertFalse(json.loads(request(f'/api/delivery?task={name}')[1])['current'])
                (root / 'workspace/report.txt').write_bytes(initial)
                self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=source.txt')[1], b'Observed input')
                rows = json.loads(request(f'/api/files?task={name}&mount=source0')[1])
                self.assertEqual({row['name'] for row in rows}, {'source.txt', '观察.txt', '分组'})
                rows = json.loads(request(f'/api/files?task={name}&mount=source0&path=分组')[1])
                self.assertEqual(rows, [dict(name='输入.txt', path='分组/输入.txt', directory=False)])
                for mount, relative in (('source0', '观察.txt'), ('source1', '')):
                    status, raw = request(f'/api/file?task={name}&mount={mount}&path={relative}')
                    self.assertEqual(status, 200)
                    self.assertEqual(raw.decode('utf-8'), '实测输入 café')
                self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=分组/输入.txt')[1].decode('utf-8'),
                                 '分组输入')
                self.assertEqual(request(f'/api/file?task={name}&mount=workspace&path=report.txt')[1], initial)
                self.assertEqual(request(f'/api/file?task={name}&mount=source1&path=child')[0], 400)
                for path in ('.env', '.hidden', '../source.txt', '.ssh/id_rsa', 'keys/private.pem',
                             'credentials/token.txt', 'logs/run.log', 'models.toml'):
                    self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=' + path)[0], 400)
                # Explicit file mounts still protect their basename; native
                # source admission continues to reject env/credential roots.
                for private in ('.env', '.ssh/id_rsa'):
                    with self.assertRaises(ValueError):
                        workspace.read(project / private)
                self.assertEqual(request(f'/api/file?task={name}&mount=source2')[0], 400)
                self.assertEqual(request('/api/task?task=../materials')[0], 400)
                self.assertEqual(request('/api/action', {'op': 'run_command', 'task': name,
                                                        'argv': ['arbitrary']})[0], 400)
                # This is the actual owner, not a UI-generated completion result.
                action('feedback', feedback='Explain limitations of the same evidence')
                state = json.loads((root / 'actions.json').read_text(encoding='utf-8'))
                self.assertEqual(len(state['requests']), 2)
                self.assertEqual(state['requests'][-1], 'Explain limitations of the same evidence')
                self.assertEqual((root / 'workspace/report.txt').read_bytes(), initial)
                view = json.loads(request(f'/api/task?task={name}')[1])
                self.assertEqual(view['manifest']['status'], 'paused')
                self.assertIsNone(view['plan'])
                # Keep old delivery on disk: the HTTP view still says paused.
                self.assertTrue((root / 'delivery.json').exists())
                self.assertFalse(json.loads(request(f'/api/delivery?task={name}')[1])['current'])
                app = ToolApplication.load(root)
                app.run(actions=[call('run_process', {'argv': ['python', '-c', 'print("approved")'], 'timeout_sec': 5})],
                        progress=lambda _: None)
                view = json.loads(request(f'/api/task?task={name}')[1])
                self.assertIsNotNone(view['approval'])
                self.assertEqual(request('/api/action', {'op': 'approve', 'task': name, 'token': 'wrong'})[0], 400)
                action('approve', token=view['approval']['token'])
                ledger = BudgetLedger.load(root / 'budget_ledger.json')
                self.assertEqual(ledger.remaining('process_invocations'), 0)
                self.assertEqual(request('/api/action', {'op': 'stop', 'task': name})[0], 400)
                # Original ledger remains the source for both unknown and reserved.
                ledger.reserve('ui-unknown', {'custom_dimension': 3})
                ledger.mark_unknown('ui-unknown', reason='Receipt unavailable')
                ledger.reserve('ui-reserved', {'another_dimension': 2})
                before = (root / 'budget_ledger.json').read_bytes()
                view = json.loads(request(f'/api/task?task={name}')[1])
                self.assertIn('unknown', {e['status'] for e in view['resources']['entries']})
                self.assertIn('reserved', {e['status'] for e in view['resources']['entries']})
                self.assertTrue(all({'actual_source', 'reason'} <= set(e) for e in view['resources']['entries']))
                self.assertEqual((root / 'budget_ledger.json').read_bytes(), before)
                with patch.dict(os.environ, {'UI_TEST_API_KEY': 'credential-do-not-output'}):
                    (project / 'source.txt').write_text('credential-do-not-output', encoding='utf-8')
                    self.assertNotIn(b'credential-do-not-output', request(f'/api/file?task={name}&mount=source0&path=source.txt')[1])
                outside = base / 'outside'
                outside.mkdir()
                try:
                    (project / 'linked').symlink_to(outside, target_is_directory=True)
                except OSError:
                    pass  # Windows may prohibit symlink creation for this account.
                else:
                    self.assertEqual(request(f'/api/files?task={name}&mount=source0&path=linked')[0], 400)
                    with self.assertRaises(ValueError):
                        bounded(project / 'linked' / 'child', project)
                # Discovery must fail closed even when excess entries are invalid.
                for index in range(256):
                    (tasks / f'invalid-{index}').write_text('not a task', encoding='utf-8')
                self.assertEqual(request('/api/tasks')[0], 400)
            finally:
                httpd.shutdown()
                httpd.server_close()
                serving.join(10)

    def test_cooperative_boundary_stop_and_resume_preserves_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'boundary'
            app = ToolApplication.create(root, 'Bounded caller work', model=None, sources={}, max_attempts=20)
            actions = [call('write_file', {'path': 'workspace/result.txt', 'content': 'retained'}, 'write'),
                       call('ask_user', {'question': 'Please inspect retained result'}, 'question')]
            stopped = app.run(actions=actions, should_stop=lambda: True, progress=lambda _: None)
            self.assertEqual(stopped['status'], 'paused')
            self.assertFalse((root / 'workspace/result.txt').exists())
            self.assertEqual(len(app.state['turns'][-1]['remaining']), 2)
            resumed = ToolApplication.load(root).run(progress=lambda _: None)
            self.assertEqual(resumed['status'], 'paused')
            self.assertEqual((root / 'workspace/result.txt').read_text(), 'retained')
            self.assertEqual(resumed['session_id'], stopped['session_id'])

    def test_http_create_switch_bound_recipients_and_live_boundary_stop_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            catalog = base / 'models.toml'
            catalog_text = 'version = 1\n[routes]\ndefault = "a"\ncode = "code"\nwriting = "writing"\n'
            for name in ('a', 'b', 'code', 'writing'):
                catalog_text += (f'\n[profiles.{name}]\napi = "openai_chat"\n'
                    f'base_url = "https://{name}.invalid/v1"\nmodel = "fake-{name}"\n'
                    'api_key_env = "SAR_UI_TEST_KEY"\ncapabilities = ["text", "code"]\n')
            catalog.write_text(catalog_text, encoding='utf-8')
            project = base / 'project'
            project.mkdir()
            workspace = Workspace(project, [], base / 'tasks')
            entered, release = threading.Event(), threading.Event()
            calls = []

            def fake_turn(client, messages, **kwargs):
                calls.append(kwargs['budget_call_id'])
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError('Test did not release model boundary')
                    tool, arguments = 'write_file', {'path': 'workspace/result.txt', 'content': 'same task'}
                else:
                    tool, arguments = 'ask_user', {'question': 'Inspect this retained result'}
                return {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'fake-' + str(len(calls)), 'type': 'function',
                     'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}

            with patch.dict(os.environ, {'SIMPLE_AR_MODELS_CONFIG': str(catalog), 'SAR_UI_TEST_KEY': 'fake-ui-credential'}), \
                    patch.object(LLMClient, 'chat_turn', new=fake_turn), \
                    patch.object(LLMClient, '_complete_request', side_effect=AssertionError('No provider traffic')):
                httpd = server(workspace)
                serving = threading.Thread(target=httpd.serve_forever, daemon=True)
                serving.start()

                def request(path, data=None):
                    connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=10)
                    headers = {'Authorization': 'Bearer ' + httpd.session_token, 'Origin': httpd.origin,
                               'Content-Type': 'application/json'}
                    connection.request('POST' if data else 'GET', path,
                                       json.dumps(data) if data else None, headers)
                    response = connection.getresponse()
                    status, raw = response.status, response.read()
                    connection.close()
                    self.assertNotIn(b'fake-ui-credential', raw)
                    return status, json.loads(raw)

                try:
                    metadata = request('/api/meta')[1]
                    profiles = metadata['profiles']
                    defaults = metadata['creation_defaults']
                    args = native_arguments()
                    self.assertEqual(defaults['max_turns'], args.max_turns)
                    self.assertEqual(defaults['max_tokens'], args.max_tokens)
                    self.assertEqual(defaults['allow_execution'], args.allow_execution)
                    options = native_creation_options(args)
                    options.update(model=None, sources={})
                    _, _, owner_limits, _ = ToolApplication._configuration(workspace.tasks,
                        'Configuration preview', **options)
                    for field in ('process_invocations', 'process_wall_seconds'):
                        self.assertEqual(defaults[field], owner_limits[field])
                        self.assertGreater(defaults[field], 0)
                    self.assertEqual(set(defaults), {'max_turns', 'max_tokens', 'max_images',
                        'allow_execution', 'allow_network', 'allow_vision',
                        'process_invocations', 'process_wall_seconds'})
                    # Parser changes flow into metadata without a second UI default.
                    args.max_turns += 1
                    args.max_tokens += 1
                    with patch('simple_ar.ui.native_arguments', return_value=args):
                        changed = request('/api/meta')[1]['creation_defaults']
                        self.assertEqual(changed['max_turns'], args.max_turns)
                        self.assertEqual(changed['max_tokens'], args.max_tokens)
                    a = next(p for p in profiles if p['selector'] == 'profile:a')
                    b = next(p for p in profiles if p['selector'] == 'profile:b')
                    self.assertTrue(a['default'])
                    self.assertFalse(b['default'])
                    self.assertEqual(b['recipients']['code']['base_url'], 'https://code.invalid/v1')
                    self.assertEqual(b['recipients']['writing']['base_url'], 'https://writing.invalid/v1')
                    create = dict(op='create', confirmed=True, model='profile:a', binding_id=a['binding_id'],
                        goal='Keep original evidence and limits', materials=[str(project)], max_turns=defaults['max_turns'],
                        max_tokens=defaults['max_tokens'], process_invocations=0, process_wall_seconds=0,
                        network=False, execution=False)
                    self.assertEqual(request('/api/action', create | {'confirmed': False})[0], 400)
                    self.assertEqual(request('/api/action', create | {'max_tokens': -1})[0], 400)
                    status, result = request('/api/action', create)
                    self.assertEqual(status, 200, result)
                    name = result['task']
                    root = workspace.task(name)
                    self.assertEqual(request('/api/tasks')[1], [dict(task=name,
                        topic=create['goal'], status='created')])
                    ledger_before = (root / 'budget_ledger.json').read_bytes()
                    actual_limits = json.loads(ledger_before)['limits']
                    self.assertEqual(actual_limits['llm_requests'], create['max_turns'])
                    self.assertEqual(actual_limits['total_tokens'], create['max_tokens'])
                    self.assertEqual(actual_limits['process_invocations'], 0)
                    self.assertEqual(actual_limits['process_wall_seconds'], 0)
                    switch = dict(op='switch', task=name, model='profile:b', binding_id=b['binding_id'],
                                  confirmed=True, switch_id='ui-switch', reason='Explicit recipient change')
                    # A changed nested writing recipient invalidates confirmation.
                    catalog.write_text(catalog_text.replace('https://writing.invalid', 'https://new-writing.invalid'), encoding='utf-8')
                    self.assertEqual(request('/api/action', switch)[0], 400)
                    catalog.write_text(catalog_text, encoding='utf-8')
                    self.assertEqual(request('/api/action', switch)[0], 200)
                    saved = json.loads((root / 'actions.json').read_text(encoding='utf-8'))
                    self.assertEqual(saved['config']['model'], 'profile:b')
                    self.assertEqual(saved['config']['model_rebindings'][-1]['to_connection']['settings']['base_url'],
                                     'https://b.invalid/v1')
                    self.assertEqual((root / 'budget_ledger.json').read_bytes(), ledger_before)
                    self.assertEqual(calls, [])
                    self.assertEqual(request('/api/action', dict(op='resume', task=name))[0], 200)
                    self.assertTrue(entered.wait(10))
                    self.assertEqual(request('/api/action', dict(op='stop', task=name))[0], 200)
                    self.assertTrue(request('/api/task?task=' + name)[1]['stop_requested'])
                    release.set()
                    workspace.workers[name][0].join(10)
                    self.assertFalse(workspace.workers[name][0].is_alive())
                    paused = request('/api/task?task=' + name)[1]
                    self.assertEqual(paused['manifest']['status_reason'], 'Caller requested boundary stop')
                    self.assertFalse((root / 'workspace/result.txt').exists())
                    self.assertEqual(request('/api/action', dict(op='resume', task=name))[0], 200)
                    workspace.workers[name][0].join(10)
                    self.assertFalse(workspace.workers[name][0].is_alive())
                    self.assertEqual((root / 'workspace/result.txt').read_text(), 'same task')
                    self.assertEqual(len(json.loads((root / 'actions.json').read_text())['requests']), 1)
                    self.assertEqual(request('/api/task?task=' + name)[1]['manifest']['session_id'], name)
                finally:
                    release.set()
                    for thread, stop in workspace.workers.values():
                        stop.set()
                        thread.join(10)
                    httpd.shutdown()
                    httpd.server_close()
                    serving.join(10)


if __name__ == '__main__':
    unittest.main()
