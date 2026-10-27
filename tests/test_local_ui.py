"""Integrated local HTTP/owner boundary checks; no provider calls.

Run on the verification server: python -m unittest discover -s tests -p test_local_ui.py
"""
import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import quote
from unittest.mock import Mock, patch

from simple_ar.app.tool_application import ToolApplication
from tests.native_history_fixture import load_native_history
from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient
from simple_ar.research.documents.ingest import DocumentBundle
from simple_ar.tools.sources import SourceToolGateway
from simple_ar.ui import Workspace, bounded, native_arguments, server


def call(name, arguments, identity='test-call'):
    return {'tool_name': name, 'arguments': arguments}


class LocalWorkspaceTests(unittest.TestCase):
    def test_delivery_validity_is_independent_of_http_file_visibility(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            tasks = base / 'tasks'
            package = base / 'extension'
            shutil.copytree(Path(__file__).resolve().parents[1] / 'examples/local_extension', package)
            unused = base / 'unused.txt'
            unused.write_text('Old raw source', encoding='utf-8')
            app = ToolApplication.create(tasks / 'retained', 'Deliver saved local files', model=None,
                sources={'source0': unused}, extensions=[package])
            app.run(actions=[
                call('write_file', {'path': 'workspace/report.txt', 'content': 'Visible output'}),
                call('write_file', {'path': 'workspace/execution.log', 'content': 'Retained process evidence'}),
                call('deliver_result', {'summary': 'Saved outputs', 'artifacts': [
                    'workspace/report.txt', 'workspace/execution.log'], 'unresolved': []}),
            ], progress=lambda _: None)
            # Historical dependencies are outside UI grants and can disappear.
            unused.unlink()
            descriptor = package / 'extension.toml'
            descriptor.write_bytes(descriptor.read_bytes() + b'\n# changed\n')
            workspace = Workspace(tasks, [], tasks)
            root = tasks / 'retained'
            records = {p: p.read_bytes() for p in root.rglob('*.json')}
            visible = workspace.delivery('retained')
            self.assertTrue(visible['current'])
            descriptor.unlink()
            with patch.object(ToolApplication, 'load', side_effect=AssertionError('No executable assembly')):
                self.assertTrue(workspace.delivery('retained')['current'])
            self.assertEqual(visible['unavailable_file_count'], 1)
            self.assertEqual({row['path'] for row in visible['files']}, {'report.txt'})
            with self.assertRaisesRegex(ValueError, 'cannot be served'):
                workspace.file('retained', 'workspace', 'execution.log')
            self.assertEqual({p: p.read_bytes() for p in records}, records)
            # Hidden from HTTP does not mean exempt from native version checks.
            log = root / 'workspace/execution.log'
            original = log.read_bytes()
            log.write_bytes(b'Untracked replacement')
            self.assertFalse(workspace.delivery('retained')['current'])
            log.write_bytes(original)
            self.assertTrue(workspace.delivery('retained')['current'])
            # Exact artifact admission precedes hashing in from_session reuse.
            from simple_ar.cli.start import reuse_session_materials
            from simple_ar.tools.workspace import RetainedDelivery
            args = native_arguments()
            args.from_session, args.reuse = root, ['artifacts']
            denied = root / 'workspace/report.txt'
            admitted = []

            def admit(path):
                admitted.append(path)
                if path == denied:
                    raise ValueError('Exact selected material denied')
                return workspace.read(path)

            args._material_authorize = admit
            args._material_preflight = workspace.material_grants
            with patch.object(RetainedDelivery, 'hashes', side_effect=AssertionError('Grant before hash')):
                with self.assertRaisesRegex(ValueError, 'Exact selected material denied'):
                    reuse_session_materials(args, native=True)
            self.assertIn(denied, admitted)
            args._material_authorize = workspace.read
            with patch.object(ToolApplication, 'load', side_effect=AssertionError('No executable assembly')):
                reuse_session_materials(args, native=True)
            self.assertEqual(set(args.material), {denied, log})
            # The actual UI configuration entry wires the same admission into
            # migration, despite missing historical sources and extensions.
            config = tasks / 'reuse.toml'
            config.write_text('[task]\nrequest="Use portable outputs"\nmodel="profile:fixture"\n'
                + 'from_session=' + json.dumps(str(root)) + '\nreuse=["artifacts"]\n', encoding='utf-8')
            catalog = Mock()
            catalog.select.return_value = ('fixture', Mock())
            with patch('simple_ar.ui.load_model_catalog', return_value=catalog), \
                    patch.object(workspace, 'profiles', return_value=[{'selector': 'profile:fixture'}]), \
                    patch.object(workspace, 'connection', return_value=({}, 'fixture-binding')), \
                    patch.object(workspace, 'validate_sources', side_effect=AssertionError('No old dependencies')), \
                    patch.object(ToolApplication, 'load', side_effect=AssertionError('No executable assembly')):
                migrated, _ = workspace.creation_inputs({'task_config': str(config)})
            self.assertEqual(set(migrated.source), {denied, log})
            self.assertIsNone(migrated.from_session)

    def test_browser_drafts_are_task_scoped_and_dispatch_is_never_replayed(self):
        # Execute the shipped state/action helpers without a browser or model.
        # Full DOM, polling and page rendering remain browser acceptance work.
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node is required for browser state regression')
        from simple_ar import ui
        source = (Path(ui.__file__).parent / 'ui_assets/app.js').read_text(encoding='utf-8')
        helpers = source[source.index('let selected ='):source.index('const pretty =')]
        dispatch = source[source.index('async function action('):source.index('function confirmationControls()')]
        creation = source[source.index("$('createForm').onsubmit ="):source.index("$('resume').onclick =")]
        mode = source[source.index('function creationMode()'):source.index('async function refresh()')]
        script = r"""
const assert = require('node:assert/strict');
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {value: '', checked: false, hidden: false,
    type: ['network', 'execution'].includes(id) ? 'checkbox' : 'text',
    replaceChildren() { this.value = ''; }});
  return elements.get(id);
};
const storage = new Map();
const sessionStorage = {setItem: (key, value) => storage.set(key, value)};
let messages = [], requests = [], finish, finishRefresh;
let reviews = [];
function reviewAction(title, op, extra, facts) { reviews.push({title, op, extra, facts}); }
function notice(message) { messages.push(message); }
function confirmationControls() {}
async function refresh() {
  await new Promise(resolve => { finishRefresh = resolve; });
  throw Error('read unavailable');
}
function json(path, data) {
  requests.push(data);
  return new Promise(resolve => { finish = resolve; });
}
""" + helpers + dispatch + creation + mode + r"""
(async () => {
  viewKey = 'workspace-one';
  $('goal').value = 'Keep my research question';
  $('materials').value = '/approved/materials';
  $('taskConfig').value = '/approved/task.toml';
  $('network').checked = true;
  selectTask('alpha');
  $('feedback').value = 'Revise the figure and caption';
  $('switchReason').value = 'Existing authorization';
  $('mount').value = 'source0'; folder = 'measurements';
  previewTarget = {mount: 'workspace', path: 'figures/plot.png', name: 'plot.png'};
  selectTask('beta');
  assert.equal($('feedback').value, '');
  assert.equal(folder, '');
  $('feedback').value = 'Other project feedback';
  selectTask('alpha');
  assert.equal($('feedback').value, 'Revise the figure and caption');
  assert.equal(folder, 'measurements');
  assert.equal($('mount').value, ''); // Rebound only after owner snapshot.
  assert.equal(view.tasks.alpha.mount, 'source0');
  assert.equal(previewTarget.path, 'figures/plot.png');
  assert.equal(view.tasks.beta.feedback, 'Other project feedback');
  assert.equal(view.create.goal, 'Keep my research question');
  assert.equal(view.create.network, true);
  assert.equal(view.create.taskConfig, '/approved/task.toml');
  assert.equal(requests.length, 0); // Selecting/recovering a view never runs work.
  const saved = JSON.parse(storage.get(viewKey));
  view = saved;
  selectTask(view.selected, false);
  assert.equal($('feedback').value, 'Revise the figure and caption');
  const first = action('feedback', {feedback: $('feedback').value});
  await action('feedback', {feedback: 'double click'});
  assert.equal(requests.length, 1);
  assert.equal(requests[0].task, 'alpha');
  selectTask('beta');
  finish({task: 'alpha', message: 'accepted'});
  await new Promise(resolve => setImmediate(resolve));
  await action('resume');
  assert.equal(requests.length, 1); // Still locked while awaiting owner state.
  finishRefresh();
  assert.equal((await first).task, 'alpha');
  assert.equal(selected, 'beta'); // A late reply cannot redirect the user.
  assert.equal(requests.length, 1); // Failed refresh never repeats dispatch.
  assert.ok(messages.some(text => text.includes('服务器已接受操作')));
  assert.equal(actionSending, false);
  assert.equal(view.tasks.alpha.feedback, 'Revise the figure and caption');
  selected = '';
  $('taskConfig').value = '/approved/task.toml';
  creationMode();
  assert.equal($('plainCreation').disabled, true);
  const previewing = $('createForm').onsubmit({preventDefault() {}});
  assert.equal(requests.length, 2);
  assert.deepEqual(requests[1], {op: 'preview_create', task_config: '/approved/task.toml'});
  await $('createForm').onsubmit({preventDefault() {}});
  assert.equal(requests.length, 2);
  $('taskConfig').value = '/edited/task.toml';
  finish({preview_id: 'owner-seal', goal: 'Effective goal', extensions: [], access: {}, limits: {}});
  await previewing;
  assert.equal(reviews.length, 1);
  assert.deepEqual(reviews[0].extra, {task_config: '/approved/task.toml', preview_id: 'owner-seal', confirmed: true});
  assert.match(Object.values(reviews[0].facts).join(' '), /宿主机 OS 权限/);
  assert.equal(requests.length, 2); // Preview itself never dispatches creation.
  $('taskConfig').value = '';
  creationMode();
  assert.equal($('plainCreation').disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run([node, '-e', script], capture_output=True, text=True,
            encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_text_only_recipient_rendering_keeps_task_loading_available(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            catalog = root / 'models.toml'
            catalog.write_text('version = 1\n[routes]\ntext = "only"\n[profiles.only]\n'
                'api = "openai_chat"\nmodel = "fake-text"\nbase_url = "https://text.invalid/v1"\n'
                'api_key_env = "SAR_UI_TEST_KEY"\ncapabilities = ["text"]\n', encoding='utf-8')
            workspace = Workspace(root, [], root / 'tasks')
            with patch.dict(os.environ, {'SIMPLE_AR_MODELS_CONFIG': str(catalog),
                    'SAR_UI_TEST_KEY': 'fake-ui-credential'}), \
                    patch.object(LLMClient, '_complete_request', side_effect=AssertionError('No provider traffic')):
                profile, = workspace.profiles()
            self.assertTrue(profile['default'])
            self.assertIsNone(profile['recipients']['code'])
            self.assertEqual(profile['recipients']['writing']['model'], 'fake-text')
            node = shutil.which('node')
            if node is None:
                self.skipTest('Node is required for the recipient JavaScript regression')
            from simple_ar import ui
            source = (Path(ui.__file__).parent / 'ui_assets/app.js').read_text(encoding='utf-8')
            # Execute the real renderer with its minimal DOM contract. A null
            # recipient must not throw before the following refresh is reached.
            renderer = source[source.index('function recipient()'):source.index('function effectiveAccess()')]
            script = """
const assert = require('node:assert/strict');
const metadata = {profiles: [JSON.parse(process.argv[1])]};
const elements = {profile: {value: metadata.profiles[0].selector}, recipient: {textContent: ''}};
const $ = id => elements[id];
""" + renderer + """
let refreshed = false;
recipient(); refreshed = true;
assert.equal(refreshed, true);
assert.match(elements.recipient.textContent, /code → 不可用/);
assert.match(elements.recipient.textContent, /task → fake-text/);
assert.match(elements.recipient.textContent, /writing → fake-text/);
"""
            result = subprocess.run([node, '-e', script, json.dumps(profile)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_prepared_approval_shows_owner_bound_command_and_rejects_changed_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            project = root / 'project'
            project.mkdir()
            runner = project / 'runner.py'
            runner.write_text('print("original")\n', encoding='utf-8')
            workspace = Workspace(project, [], root / 'tasks')
            app = load_native_history(workspace.task('prepared'), 'Inspect a saved prepared command',
                config={'model':None, 'sources':{'source0':project}, 'allow_execution':True})
            client = Mock()
            client._budget_ledger = None
            client.ask_json.return_value = {'summary': 'Candidate only', 'unresolved': [],
                'files': [{'path': 'worker.py', 'content': 'print("candidate")\n'}],
                # New plans use OS argv in the candidate cwd. External inputs
                # use their observed absolute location, not task-mount syntax.
                'execution_plan': {'argv': ['python', str(runner)],
                    'outputs': ['result.txt'], 'timeout_sec': 7}}
            app.implementation.client_factory = lambda _: client
            with patch.object(LLMClient, '_complete_request', side_effect=AssertionError('No provider traffic')), \
                    patch('simple_ar.tools.workspace.run_process', side_effect=AssertionError('No process execution')):
                app.run(actions=[call('prepare_implementation', {'goal': 'Prepare a command'})], progress=lambda _: None)
                result = app.session.store.read_json(app.state['turns'][-1]['tools'][0]['result'])
                self.assertEqual(result['status'], 'ok', result.get('error'))
                app.run(actions=[call('execute_implementation', {
                    'prepared_result': result['data']['prepared_result']})], progress=lambda _: None)
                pending = app.state['pending']
                token = app.approval_token(pending['call'])
                before = {p.relative_to(app.session.store.root): p.read_bytes()
                    for p in app.session.store.root.rglob('*') if p.is_file()}
                with patch.object(ToolApplication, 'load', side_effect=AssertionError('GET must not load an application')), \
                        patch.object(Path, 'mkdir', side_effect=AssertionError('GET must not create directories')):
                    view = workspace.snapshot('prepared')
                self.assertEqual(view['approval']['token'], token)
                self.assertEqual(view['approval']['execution'], {
                    'argv': [str(app.workspace.python), str(runner)],
                    'cwd': result['data']['project_dir'], 'timeout_sec': 7})
                self.assertIsNone(view['approval_unavailable'])
                self.assertEqual(before, {p.relative_to(app.session.store.root): p.read_bytes()
                    for p in app.session.store.root.rglob('*') if p.is_file()})
                runner.write_text('print("replacement")\n', encoding='utf-8')
                changed = workspace.snapshot('prepared')
                self.assertIsNone(changed['approval'])
                self.assertTrue(changed['approval_unavailable'])
                runner.write_text('print("original")\n', encoding='utf-8')
                self.assertEqual(workspace.snapshot('prepared')['approval']['token'], token)
                candidate = Path(result['data']['project_dir']) / 'worker.py'
                candidate.write_text('print("unregistered change")\n', encoding='utf-8')
                self.assertIsNone(workspace.snapshot('prepared')['approval'])
                candidate.write_text('print("candidate")\n', encoding='utf-8')
                (app.session.store.root / 'evidence').rmdir()
                with patch.object(ToolApplication, 'load', side_effect=AssertionError('GET must not repair a task')), \
                        patch.object(Path, 'mkdir', side_effect=AssertionError('GET must not repair directories')):
                    self.assertIsNone(workspace.snapshot('prepared')['approval'])
                self.assertFalse((app.session.store.root / 'evidence').exists())

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
            diagnostic = '<img src=x onerror=alert(1)> Candidate evidence needs review\nOriginal diagnostic'
            app = ToolApplication.create(root, 'Inspect retained evidence', model=None,
                sources={'source0': project, 'source1': source_file, 'source2': project / 'models.toml'},
                allow_execution=True, max_attempts=80,
                process_limits={'process_invocations': 1, 'process_wall_seconds': 30})
            app.run(actions=[call('write_file', {'path': 'workspace/report.txt', 'content': 'first report',
                'delivery': {'summary': 'Initial result', 'artifacts': ['workspace/report.txt'],
                             'unresolved': [diagnostic]} })],
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
                status, page = request('/')
                self.assertEqual(status, 200)
                self.assertIn(b'id="confirmation"', page)
                self.assertIn(b'id="confirmationCancel"', page)
                self.assertIn(b'id="confirmationSubmit"', page)
                status, script = request('/app.js')
                self.assertEqual(status, 200)
                self.assertNotRegex(script.decode('utf-8'), r'\bconfirm\s*\(')
                # Static sink/wiring constraints only: these do not execute JS
                # or establish browser rendering/XSS behavior. Verify DOM on server.
                source = script.decode('utf-8')
                self.assertNotIn('innerHTML', source)
                self.assertIn("paragraph($('delivery'), diagnostic, 'receipt')", source)
                self.assertIn("delivery.current ? '当前请求交付'", source)
                self.assertIn("row.observed.returncode !== 0", source)
                self.assertIn('执行失败（原工具回执状态：', source)
                self.assertEqual(request('/api/tasks', authenticated=False)[0], 403)
                self.assertEqual(request('/api/tasks', host='evil.invalid')[0], 403)
                self.assertEqual(json.loads(request('/api/tasks')[1]),
                    [dict(task=name, topic='Inspect retained evidence', status='partial')])
                self.assertEqual(workspace.task('.'), tasks)
                for invalid in ('', '..', str(root), name + '/workspace'):
                    with self.assertRaises(ValueError):
                        workspace.task(invalid)
                self.assertEqual(request('/api/action', {'op': 'resume', 'task': name},
                                         origin='http://evil.invalid')[0], 403)
                status, raw = request(f'/api/task?task={name}')
                self.assertEqual(status, 200)
                view = json.loads(raw)
                self.assertEqual(view['manifest']['status'], 'partial')
                self.assertEqual(view['mode'], 'saved results browser')
                self.assertFalse(view['worker_live'])
                self.assertIsNone(view['plan'])
                self.assertEqual(view['activity_total'], 1)
                receipt = view['activity'][0]
                self.assertEqual(receipt['tool'], 'write_file')
                self.assertEqual(receipt['status'], 'ok')
                self.assertEqual(receipt['request_count'], 1)
                self.assertTrue(receipt['attempt'].startswith('native-tool-'))
                self.assertEqual(json.loads((root / receipt['record']).read_text())['status'], receipt['status'])
                delivery = json.loads(request(f'/api/delivery?task={name}')[1])
                self.assertTrue(delivery['current'])
                self.assertEqual(delivery['unresolved'], [diagnostic])
                self.assertEqual(delivery['summary'], 'Initial result')
                self.assertTrue(any(f['path'] == 'report.txt' for f in delivery['files']))
                (root / 'workspace/report.txt').write_text('untracked replacement', encoding='utf-8')
                historical = json.loads(request(f'/api/delivery?task={name}')[1])
                self.assertFalse(historical['current'])
                self.assertEqual(historical['unresolved'], [diagnostic])
                self.assertEqual(historical['summary'], delivery['summary'])
                self.assertEqual(historical['files'], [])
                (root / 'workspace/report.txt').write_bytes(initial)
                self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=source.txt')[1], b'Observed input')
                rows = json.loads(request(f'/api/files?task={name}&mount=source0')[1])
                self.assertEqual({row['name'] for row in rows}, {'source.txt', '观察.txt', '分组'})
                rows = json.loads(request(f'/api/files?task={name}&mount=source0&path=分组')[1])
                self.assertEqual(rows, [dict(name='输入.txt', path='分组/输入.txt', directory=False)])
                bundle = DocumentBundle.from_rows(documents=[dict(document_id='paper', title='<b>Human title</b>',
                    source='supporting_material', url='https://example.invalid/paper', extraction_status='parsed')],
                    chunks=[dict(chunk_id='chunk', document_id='paper', text='retained source ' * 200,
                        page=3, metadata={'extraction_status': 'parsed'})]).to_handoff_dict()
                bundle_path = project / 'declared.json'
                bundle_path.write_text(json.dumps(bundle), encoding='utf-8')
                (project / 'ordinary.json').write_text('{"title":"ordinary data"}', encoding='utf-8')
                # Polls/listings do not parse material JSON; explicit inspection
                # calls the same source owner after the HTTP file guards.
                with patch.object(SourceToolGateway, 'inventory_source', wraps=SourceToolGateway.inventory_source) as inspect:
                    request(f'/api/task?task={name}')
                    request(f'/api/files?task={name}&mount=source0')
                    inspect.assert_not_called()
                    status, raw = request(f'/api/source-preview?task={name}&mount=source0&path=declared.json')
                    self.assertEqual(status, 200)
                    source = json.loads(raw)
                    self.assertEqual(source['documents'][0]['title'], '<b>Human title</b>')
                    self.assertEqual(source['documents'][0]['url'], 'https://example.invalid/paper')
                    self.assertEqual(source['documents'][0]['status'], 'parsed')
                    self.assertEqual(source['retained_chunks'], 1)
                    self.assertTrue(source['retained_preview'][0]['truncated'])
                    self.assertEqual(source['retained_preview'][0]['page'], 3)
                    self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=declared.json')[1], bundle_path.read_bytes())
                    self.assertIsNone(json.loads(request(f'/api/source-preview?task={name}&mount=source0&path=ordinary.json')[1]))
                    self.assertEqual(request(f'/api/file?task={name}&mount=source0&path=ordinary.json')[1], b'{"title":"ordinary data"}')
                    (root / 'workspace/declared.json').write_text(json.dumps(bundle), encoding='utf-8')
                    inspect.reset_mock()
                    self.assertIsNone(json.loads(request(f'/api/source-preview?task={name}&mount=workspace&path=declared.json')[1]))
                    inspect.assert_not_called()
                self.assertEqual(request(f'/api/source-preview?task={name}&mount=source0&path=declared.json', authenticated=False)[0], 403)
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
                    self.assertEqual(request(f'/api/source-preview?task={name}&mount=source0&path=' + path)[0], 400)
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
                self.assertEqual(view['activity'][0]['request_count'], 1)
                # Keep old delivery on disk: the HTTP view still says paused.
                self.assertTrue((root / 'delivery.json').exists())
                historical = json.loads(request(f'/api/delivery?task={name}')[1])
                self.assertFalse(historical['current'])
                self.assertEqual(historical['unresolved'], [diagnostic])
                app = ToolApplication.load(root)
                # Bounded recent history includes a real refusal, not model
                # prose or a synthetic UI status. No plan call is required.
                app.run(actions=[call('read_file', {'path': 'source0/source.txt'}) for _ in range(25)] +
                    [call('read_file', {'path': 'source0/../outside.txt'})], progress=lambda _: None)
                original_state = (root / 'actions.json').read_bytes()
                view = json.loads(request(f'/api/task?task={name}')[1])
                self.assertEqual(len(view['activity']), 24)
                self.assertEqual(view['activity_total'], 27)
                refusal = view['activity'][0]
                self.assertEqual(refusal['status'], 'blocked')
                original = json.loads((root / refusal['record']).read_text())
                self.assertEqual(refusal['error'], original['error'])
                self.assertTrue(refusal['error'])
                self.assertEqual(refusal['request_count'], 2)
                self.assertEqual((root / 'actions.json').read_bytes(), original_state)
                app.run(actions=[call('run_process', {'argv': ['python', '-c', 'print("approved"); raise SystemExit(7)'], 'timeout_sec': 5})],
                        progress=lambda _: None)
                view = json.loads(request(f'/api/task?task={name}')[1])
                self.assertIsNotNone(view['approval'])
                self.assertEqual(view['pending']['attempt'], view['approval']['attempt'])
                self.assertEqual(view['pending']['call'], view['approval']['call'])
                self.assertEqual(request('/api/action', {'op': 'approve', 'task': name, 'token': 'wrong'})[0], 400)
                action('approve', token=view['approval']['token'])
                process = json.loads(request(f'/api/task?task={name}')[1])['activity'][0]
                self.assertEqual(process['tool'], 'run_process')
                self.assertEqual(process['status'], 'error')
                self.assertEqual(process['observed']['returncode'], 7)
                original = json.loads((root / process['record']).read_text())
                self.assertEqual(original['status'], process['status'])
                self.assertEqual(original['data']['returncode'], process['observed']['returncode'])
                self.assertIn('Process failed or stopped', original['error'])
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
                    bundle_path.write_text(json.dumps(bundle).replace('Human title', 'credential-do-not-output'), encoding='utf-8')
                    self.assertNotIn(b'credential-do-not-output', request(f'/api/source-preview?task={name}&mount=source0&path=declared.json')[1])
                outside = base / 'outside'
                outside.mkdir()
                try:
                    (project / 'linked').symlink_to(outside, target_is_directory=True)
                except OSError:
                    pass  # Windows may prohibit symlink creation for this account.
                else:
                    self.assertEqual(request(f'/api/files?task={name}&mount=source0&path=linked')[0], 400)
                    self.assertEqual(request(f'/api/source-preview?task={name}&mount=source0&path=linked/declared.json')[0], 400)
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
            catalog_text = ('version = 1\n[routes]\ndefault = "a"\ncode = "code"\nwriting = "writing"\n'
                            'vision = "vision"\nimage = "image"\n')
            for name in ('a', 'b', 'code', 'writing'):
                catalog_text += (f'\n[profiles.{name}]\napi = "openai_chat"\n'
                    f'base_url = "https://{name}.invalid/v1"\nmodel = "fake-{name}"\n'
                    'api_key_env = "SAR_UI_TEST_KEY"\ncapabilities = ["text", "code"]\n')
            catalog_text += ('\n[profiles.vision]\napi="openai_chat"\nbase_url="https://vision.invalid/v1"\n'
                'model="fake-vision"\napi_key_env="SAR_UI_TEST_KEY"\ncapabilities=["vision"]\n'
                '\n[profiles.image]\napi="openai_images"\nbase_url="https://image.invalid/v1"\n'
                'model="fake-image"\napi_key_env="SAR_UI_TEST_KEY"\ncapabilities=["image_generate"]\n')
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
                    tool, arguments = 'update_work_plan', {'steps': [
                        {'step': 'Inspect the authorized evidence for the requested outcome', 'status': 'in_progress'}]}
                else:
                    tool, arguments = 'ask_user', {'question': 'Inspect this retained result'}
                self.assertIn(tool, {row['function']['name'] for row in kwargs['tools']},
                    'UI transport fixture must obey the actual current function contract')
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
                    status, metadata = request('/api/meta')
                    self.assertEqual(status, 200, metadata)
                    profiles = metadata['profiles']
                    defaults = metadata['creation_defaults']
                    args = native_arguments()
                    self.assertEqual(defaults['max_turns'], args.max_turns)
                    self.assertEqual(defaults['max_tokens'], args.max_tokens)
                    self.assertEqual(defaults['allow_execution'], args.allow_execution)
                    _, _, owner_limits, _ = ToolApplication._configuration(workspace.tasks,
                        'Configuration preview', model=None, sources={},
                        max_turns=args.max_turns, max_tokens=args.max_tokens)
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
                    self.assertEqual(request('/api/action', create)[0], 400)  # No legacy confirmation bypass.
                    status, preview = request('/api/action', create | {'op': 'preview_create'})
                    self.assertEqual(status, 200, preview)
                    self.assertEqual(list(workspace.tasks.iterdir()), [])
                    self.assertEqual(preview['limits']['process_invocations'], 0)
                    self.assertEqual(preview['access']['allow_execution'], False)
                    self.assertEqual(preview['goal'], create['goal'])
                    self.assertEqual(preview['creation_declarations'], {})
                    create['preview_id'] = preview['preview_id']
                    self.assertEqual(request('/api/action', create | {'network': True})[0], 400)
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
                    # The TOML consumer shares CLI interpretation, including
                    # relative material roles and trusted extension identities.
                    from simple_ar.cli.start import migrate_new_start, native_creation_options
                    from simple_ar.cli.task_options import apply_task_config
                    from simple_ar.cli.parser import build_parser
                    package = project / 'extension'
                    package.mkdir()
                    descriptor = package / 'extension.toml'
                    descriptor_text = ('[extension]\nid="ui_notes"\nversion="1"\n'
                        'description="Trusted notes"\nscope="Research"\ninstructions="notes.txt"\n'
                        '[[tools]]\nname="inspect"\ndescription="Inspect notes"\n'
                        'implementation="tool.py"\nfunction="inspect"\npermission_level="read_only"\n'
                        '[tools.input_schema]\ntype="object"\n')
                    descriptor.write_text(descriptor_text, encoding='utf-8')
                    (package / 'notes.txt').write_text('Preserve source provenance.', encoding='utf-8')
                    (package / 'tool.py').write_text('raise AssertionError("must not import at creation")\n', encoding='utf-8')
                    (project / 'notes.txt').write_text('Unverified notes', encoding='utf-8')
                    config_path = project / 'task.toml'
                    config_text = ('[task]\nrequest="Reuse these notes"\nmodel="route:default"\n'
                        'kind="writing"\nmaterial=["notes.txt"]\nproject="."\nextensions=["extension"]\n'
                        'allow_network=true\nallow_execution=false\nallow_vision=true\nmax_images=1\nmax_turns=4\nmax_tokens=2000\n'
                        'max_processes=2\nmax_process_seconds=12.5\n')
                    config_path.write_text(config_text, encoding='utf-8')
                    config_input = dict(op='preview_create', task_config=str(config_path))
                    status, config_preview = request('/api/action', config_input)
                    self.assertEqual(status, 200, config_preview)
                    self.assertEqual(config_preview['model'], 'profile:a')
                    self.assertTrue(config_preview['access']['allow_vision'])
                    self.assertEqual(config_preview['limits']['image_requests'], 1)
                    self.assertEqual(config_preview['recipients']['vision']['base_url'], 'https://vision.invalid/v1')
                    self.assertEqual(config_preview['recipients']['image']['base_url'], 'https://image.invalid/v1')
                    self.assertEqual(config_preview['sources'], {'source0': str(project / 'notes.txt'), 'source1': str(project)})
                    self.assertEqual(config_preview['goal'], 'Reuse these notes')
                    self.assertEqual(config_preview['creation_declarations'], {
                        'outcome_preference': 'writing',
                        'material_roles': {'material': ['source0'], 'project': ['source1']}})
                    self.assertEqual(config_preview['extensions'][0]['tools'][0]['permission_level'], 'read_only')
                    self.assertEqual(set(config_preview['extensions'][0]['files']), {'tool.py', 'notes.txt'})
                    cli = build_parser().parse_args(['start', '--task-config', str(config_path), '--yes', '--prepare-only'])
                    cli._explicit_start_destinations = {'task_config', 'yes', 'prepare_only'}
                    apply_task_config(cli)
                    migrate_new_start(cli)
                    cli.model = 'profile:a'  # Same named route selection; no provider invocation.
                    options = native_creation_options(cli)
                    projected = {k: v for k, v in options.items()
                        if k not in {'professional_inputs', 'context_protocol'}}
                    expected, _, expected_limits, _ = ToolApplication._configuration(workspace.tasks, cli.request, **projected)
                    self.assertEqual(config_preview['goal'], cli.request)
                    self.assertEqual(config_preview['creation_declarations'], expected['creation_declarations'])
                    self.assertEqual(config_preview['limits'], expected_limits)
                    config_create = config_input | dict(op='create', confirmed=True, preview_id=config_preview['preview_id'])
                    config_path.write_text(config_text.replace('allow_execution=false', 'allow_execution=true'), encoding='utf-8')
                    self.assertEqual(request('/api/action', config_create)[0], 400)
                    config_path.write_text(config_text, encoding='utf-8')
                    (package / 'tool.py').write_text('raise AssertionError("changed code")\n', encoding='utf-8')
                    self.assertEqual(request('/api/action', config_create)[0], 400)
                    (package / 'tool.py').write_text('raise AssertionError("must not import at creation")\n', encoding='utf-8')
                    catalog.write_text(catalog_text.replace('https://writing.invalid', 'https://new-writing.invalid'), encoding='utf-8')
                    self.assertEqual(request('/api/action', config_create)[0], 400)
                    catalog.write_text(catalog_text.replace('https://image.invalid', 'https://new-image.invalid'), encoding='utf-8')
                    self.assertEqual(request('/api/action', config_create)[0], 400)
                    catalog.write_text(catalog_text, encoding='utf-8')
                    original_projection = ToolApplication._configuration

                    def changed_projection(*args, **kwargs):
                        result = original_projection(*args, **kwargs)
                        result[0]['allow_network'] = False
                        return result

                    # Even a change at create()'s final owner projection is
                    # rejected before its session/ledger can be persisted.
                    with patch.object(ToolApplication, '_configuration', side_effect=changed_projection):
                        self.assertEqual(request('/api/action', config_create)[0], 400)

                    def changed_declarations(*args, **kwargs):
                        result = original_projection(*args, **kwargs)
                        result[0]['creation_declarations'] = {**result[0]['creation_declarations'],
                            'with_report': True}
                        return result

                    with patch.object(ToolApplication, '_configuration', side_effect=changed_declarations):
                        self.assertEqual(request('/api/action', config_create)[0], 400)
                    self.assertEqual(len(list(workspace.tasks.iterdir())), 1)
                    status, configured = request('/api/action', config_create)
                    self.assertEqual(status, 200, configured)
                    configured_root = workspace.task(configured['task'])
                    saved_config = json.loads((configured_root / 'actions.json').read_text())['config']
                    self.assertEqual({key: saved_config[key] for key in expected}, expected)
                    self.assertEqual(saved_config['control_protocol'], 'operations.v1')
                    self.assertNotIn('professional_inputs', saved_config)
                    self.assertNotIn('professional_work', saved_config)
                    self.assertEqual(saved_config['input_contract'], 'declared_inputs.v1')
                    self.assertEqual(saved_config['creation_declarations'], config_preview['creation_declarations'])
                    self.assertEqual(json.loads((configured_root / 'actions.json').read_text())['requests'],
                                     ['Reuse these notes'])
                    self.assertEqual(saved_config['context_protocol'], 'working.v4')
                    self.assertEqual(json.loads((configured_root / 'budget_ledger.json').read_text())['limits'], expected_limits)
                    self.assertEqual(calls, [])
                    # Neither a config path nor a descriptor reference grants
                    # additional host reads. Narrow file grants stay narrow.
                    narrow = Workspace(project, [], base / 'narrow-tasks')
                    outside = base / 'outside.toml'
                    outside.write_text(config_text, encoding='utf-8')
                    with self.assertRaises(ValueError):
                        narrow.preview_creation({'task_config': str(outside)})
                    limited = Workspace(project / 'extension', [config_path, project / 'notes.txt'], base / 'limited-tasks')
                    # The project alias in TOML is outside these narrow grants.
                    with self.assertRaises(ValueError):
                        limited.preview_creation({'task_config': str(config_path)})
                    analysis = project / 'analysis.json'
                    analysis.write_text(json.dumps({'schema_version': 'table_analysis.v1',
                        'source': {'path': 'notes.txt'}}), encoding='utf-8')
                    config_path.write_text('[task]\nrequest="Inspect analysis"\nmaterial=["analysis.json"]\n', encoding='utf-8')
                    attachment_limited = Workspace(package, [config_path, analysis], base / 'attachment-tasks')
                    with patch('simple_ar.result_analysis.table.load_analysis_package',
                               side_effect=AssertionError('Must reject the attachment before the package owner reads it')):
                        with self.assertRaises(ValueError):
                            attachment_limited.preview_creation({'task_config': str(config_path)})
                    config_path.write_text(config_text + 'output_root="../ungranted-output"\n', encoding='utf-8')
                    self.assertEqual(request('/api/action', config_input)[0], 400)
                    config_path.write_text(config_text, encoding='utf-8')
                    outside_session = base / 'ungranted-session'
                    outside_session.mkdir()
                    config_path.write_text('[task]\nrequest="Reuse a project"\nfrom_session='
                        + json.dumps(str(outside_session)) + '\nreuse=["code_project"]\n', encoding='utf-8')
                    with patch('simple_ar.ui.migrate_new_start',
                            side_effect=AssertionError('Reject ungranted sessions before their owner reads them')):
                        with self.assertRaises(ValueError):
                            workspace.preview_creation({'task_config': str(config_path)})
                    config_path.write_text(config_text, encoding='utf-8')
                    descriptor.write_text(descriptor_text.replace('notes.txt', '../../outside.toml'), encoding='utf-8')
                    self.assertEqual(request('/api/action', config_input)[0], 400)
                    descriptor.write_text(descriptor_text, encoding='utf-8')
                    switch = dict(op='switch', task=name, model='profile:b', binding_id=b['binding_id'],
                                  confirmed=True, switch_id='ui-switch', reason='Explicit recipient change')
                    self.assertEqual(request('/api/action', switch | {'confirmed': False})[0], 400)
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
                    # The current recipient is retained task data, not a fresh
                    # catalog read. A subsequent worker can use the saved hint
                    # without making users restore a process-local variable.
                    os.environ.pop('SIMPLE_AR_MODELS_CONFIG')
                    retained = request('/api/task?task=' + name)[1]
                    self.assertEqual(retained['saved_connections']['task']['base_url'], 'https://b.invalid/v1')
                    self.assertEqual(retained['saved_connections']['code']['base_url'], 'https://code.invalid/v1')
                    self.assertEqual(retained['catalog_hints']['task'], str(catalog))
                    self.assertEqual(request('/api/action', dict(op='resume', task=name))[0], 200)
                    self.assertTrue(entered.wait(10))
                    live = request('/api/task?task=' + name)[1]
                    self.assertTrue(live['worker_live'])
                    self.assertEqual(live['local_workers'], [name])
                    self.assertEqual(live['pending']['kind'], 'model')
                    self.assertEqual(live['activity'], [])
                    self.assertEqual(request('/api/action', dict(op='stop', task=name))[0], 200)
                    self.assertTrue(request('/api/task?task=' + name)[1]['stop_requested'])
                    release.set()
                    workspace.workers[name][0].join(10)
                    self.assertFalse(workspace.workers[name][0].is_alive())
                    paused = request('/api/task?task=' + name)[1]
                    self.assertEqual(paused['manifest']['status_reason'], 'Caller requested boundary stop')
                    self.assertFalse(paused['worker_live'])
                    self.assertEqual(paused['local_workers'], [])
                    self.assertEqual(paused['queued_actions'], 1)
                    self.assertEqual(paused['activity'], [])
                    self.assertEqual(json.loads((root / 'actions.json').read_text())['turns'][0]['tools'], [])
                    self.assertEqual(request('/api/action', dict(op='resume', task=name))[0], 200)
                    workspace.workers[name][0].join(10)
                    self.assertFalse(workspace.workers[name][0].is_alive())
                    adopted = ToolApplication.load(root)._work_plan()
                    self.assertEqual(adopted['steps'], [
                        {'step': 'Inspect the authorized evidence for the requested outcome', 'status': 'in_progress'}])
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
