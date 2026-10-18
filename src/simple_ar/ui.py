"""Local thin client. Start with python -m simple_ar.ui --help.

Only the worker liveness/stop signal is volatile. All research state belongs to
ToolApplication and its original session, supply, approval and ledger owners.
"""
from __future__ import annotations

import argparse
import hashlib
from itertools import islice
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from simple_ar.app.tool_application import ToolApplication
from simple_ar.cli.project_tasks import discover_project_tasks
from simple_ar.cli.start import add_start_parser, native_creation_options
from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient
from simple_ar.integrations.model_profiles import load_model_catalog
from simple_ar.tools.workspace import resolve_mounted_path, resolve_source_mount


def native_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_start_parser(parser.add_subparsers(dest='command'))
    return parser.parse_args(['start'])


def bounded(path: Path, root: Path) -> Path:
    """Reject links (including junctions and linked ancestors), not just escapes."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if not path.is_relative_to(root):
        raise ValueError('Outside explicitly granted root')
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part, 'is_junction', lambda: False)():
            raise ValueError('Linked paths are not available')
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Outside explicitly granted root')
    return path


def public_text(text: str) -> str:
    # Never serialize runtime configuration/exception objects. Also mask known
    # environment credentials in user-authored records and source previews.
    for name, value in os.environ.items():
        if len(value) >= 6 and re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL', name, re.I):
            text = text.replace(value, '[redacted]')
    return re.sub(r'(?im)(["\w-]*(?:api[_-]?key|password|secret|access_token)["\w-]*\s*[:=]\s*)[^\n,}]+',
                  r'\1"[redacted]"', text)


def public_data(value):
    if isinstance(value, dict):
        return {key: ('[redacted]' if re.search(r'(?i)api[_-]?key|password|secret|access_token', key)
                      else public_data(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [public_data(item) for item in value]
    return public_text(value) if isinstance(value, str) else value


class Workspace:
    def __init__(self, project: Path, reads: list[Path], tasks: Path):
        self.project = bounded(project, project)
        self.reads = [bounded(p, p) for p in [project, *reads]]
        self.tasks = bounded(tasks, tasks)
        if not self.project.is_dir() or any(not p.exists() for p in self.reads):
            raise ValueError('Explicit project/read roots must exist')
        self.tasks.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.workers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        self.errors: dict[str, str] = {}

    def task(self, name: str) -> Path:
        if name != '.' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name):
            raise ValueError('Use a direct task directory name')
        return bounded(self.tasks / name, self.tasks)

    def read(self, path: Path) -> Path:
        for root in self.reads:
            try:
                return resolve_source_mount(bounded(path, root))
            except ValueError:
                pass
        raise ValueError('Material is outside launch grants')

    def record(self, root: Path, name: str) -> dict:
        path = bounded(root / name, root)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Record exceeds display limit')
        return json.loads(path.read_text(encoding='utf-8'))

    def profiles(self) -> list[dict]:
        catalog = load_model_catalog()
        rows = []
        try:
            default_name = catalog.select(purpose='text')[0] if catalog else None
        except ValueError:
            default_name = None
        for name, profile in (catalog.profiles.items() if catalog else []):
            if 'text' not in profile.capabilities or profile.api != 'openai_chat':
                continue
            try:
                binding, identity = self.connection('profile:' + name)
                settings = binding['settings']
                recipients = {'task': {k: settings.get(k) for k in ('base_url', 'model', 'api_mode')}}
                for purpose in ('code', 'writing'):
                    route = binding.get(purpose + '_route') or {}
                    connection = route.get('connection') or settings
                    recipients[purpose] = {k: connection.get(k) for k in ('base_url', 'model')}
                rows.append(dict(selector='profile:' + name, model=profile.model, default=name == default_name, recipients=recipients,
                    binding_id=identity, input_price=profile.input_price_per_million,
                    output_price=profile.output_price_per_million))
            except Exception:
                continue
        return rows

    def creation_defaults(self) -> dict:
        args = native_arguments()
        options = native_creation_options(args)
        # The native configuration owner resolves automatic process limits.
        # No model or material is authorized for this read-only projection.
        options.update(model=None, sources={})
        _, _, limits, _ = ToolApplication._configuration(self.tasks, 'Configuration preview', **options)
        return {key: options[key] for key in ('max_turns', 'max_tokens', 'max_images',
                    'allow_execution', 'allow_network', 'allow_vision')} | {
            key: limits[key] for key in ('process_invocations', 'process_wall_seconds')}

    def connection(self, selector: str) -> tuple[dict, str]:
        if not isinstance(selector, str) or not selector.startswith('profile:'):
            raise ValueError('Select an explicit named profile')
        binding = LLMClient.from_env(selector).connection_binding()
        if not binding or binding.get('settings', {}).get('api_mode') != 'chat':
            raise ValueError('A named Chat binding is required')
        return binding, hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    def listing(self) -> list[dict]:
        tasks, _ = discover_project_tasks(self.tasks)
        return [dict(task='.' if task.path == self.tasks else task.path.name,
                     topic=task.goal, status=task.status) for task in tasks]

    def validate_sources(self, root: Path) -> None:
        config = self.record(root, 'actions.json')['config']
        for source in config.get('sources', {}).values():
            path = Path(source)
            if path.is_relative_to(root / 'evidence'):
                bounded(path, root)
            else:
                self.read(path)

    def delivery(self, name: str) -> dict:
        """Original reuse owner checks bytes; old delivery presence is only history."""
        root = self.task(name)
        with self.lock:
            manifest = self.record(root, 'session_manifest.json')
            state = self.record(root, 'actions.json')
            saved = self.record(root, 'delivery.json') if (root / 'delivery.json').exists() else {}
            result = dict(label='历史交付 / 未验证为当前请求成果', current=False,
                          summary=saved.get('summary'), unresolved=saved.get('unresolved', []), files=[])
            worker = self.workers.get(name)
            if (worker and worker[0].is_alive() or state.get('pending')
                    or manifest['status'] not in {'completed', 'partial'} or not state['turns']
                    or state['turns'][-1].get('request_count', 1) != len(state['requests'])
                    or state['turns'][-1]['remaining']):
                return result
            try:
                self.validate_sources(root)
                for directory in ('workspace', 'evidence'):
                    if not bounded(root / directory, root).is_dir():
                        return result
                groups = ToolApplication.load(root).reusable_materials()
                files = []
                for group, paths in groups.items():
                    for path in paths:
                        for mount in ('workspace', 'evidence'):
                            if path.is_relative_to(root / mount):
                                relative = path.relative_to(root / mount).as_posix()
                                self.file(name, mount, relative)
                                files.append(dict(group=group, mount=mount, path=relative, name=path.name))
                                break
                if (self.record(root, 'session_manifest.json') != manifest
                        or self.record(root, 'actions.json') != state):
                    return result
                result.update(current=True, label='当前可复用交付（原 owner 核查；不认证科研质量）', files=files)
            except Exception:
                pass
            return result

    def snapshot(self, name: str) -> dict:
        root = self.task(name)
        manifest = self.record(root, 'session_manifest.json')
        state = self.record(root, 'actions.json')
        # Read original records without loading an application on the HTTP read
        # path: loading configures tools and can create directories.
        ledger_path = bounded(root / 'budget_ledger.json', root)
        ledger = BudgetLedger.load(ledger_path)
        pending = state.get('pending')
        reason = manifest.get('status_reason', '')
        token = re.search(r'Process requires explicit approval; token=([a-f0-9]{64})', reason)
        approval = None
        if token and pending and pending.get('kind') == 'tool':
            approval = {'token': token[1], 'call': pending['call'], 'attempt': pending['attempt']}
        plan = None
        for turn in reversed(state['turns']):
            for tool in reversed(turn['tools']):
                result = self.record(root, tool['result'])
                if (result.get('tool_name') == 'update_work_plan' and result.get('status') == 'ok'
                        and result.get('data', {}).get('request_count') == len(state['requests'])):
                    plan = result.get('data')
                    break
            if plan is not None:
                break
        config = state['config']
        with self.lock:
            worker = self.workers.get(name)
            live = bool(worker and worker[0].is_alive())
            stopping = bool(live and worker[1].is_set())
            error = self.errors.get(name)
        return dict(task=name, mode='live execution' if live else 'saved results browser',
            worker_live=live, stop_requested=stopping, worker_error=error,
            manifest={k: manifest.get(k) for k in ('session_id', 'topic', 'status', 'status_reason',
                'current_attempt', 'revision', 'budget', 'updated_at')},
            requests=state['requests'], plan=plan, pending=pending, approval=approval,
            model=config.get('model'), access={k: config.get(k) for k in
                ('allow_network', 'allow_execution', 'allow_vision')},
            sources=list(config.get('sources', {})),
            resources=ledger.snapshot() | {'entries': [
                {k: e.to_dict()[k] for k in ('reservation_id', 'status', 'reserved', 'actual',
                    'purpose', 'attempt_id')} for e in ledger.entries]})

    def file(self, name: str, mount: str, relative: str) -> Path:
        root = self.task(name)
        if mount in {'workspace', 'evidence'}:
            base = bounded(root / mount, root)
        else:
            source = self.record(root, 'actions.json')['config']['sources'][mount]
            base = self.read(Path(source))
        path = bounded(base / relative, base) if base.is_dir() else base
        if base.is_dir():
            resolve_mounted_path({'view': base}, 'view/' + (relative or '.'))
        if not base.is_dir() and relative:
            raise ValueError('File mount has no children')
        if any(p.startswith('.') or re.search(r'(?i)(credential|secret|^keys?$|^logs?$|\.log$|\.pem$|\.key$|^models\.toml$)', p)
               for p in path.parts):
            raise ValueError('Private/configuration files cannot be served')
        return path

    def mutate(self, data: dict) -> dict:
        op = data['op']
        name = data.get('task', '')
        if op == 'stop':
            with self.lock:
                worker = self.workers.get(name)
                if not worker or not worker[0].is_alive():
                    raise ValueError('No local worker; external processes are not controlled here')
                worker[1].set()
            return {'message': 'Stop requested at next action boundary; provider/process not cancelled'}
        with self.lock:
            if any(t.is_alive() for t, _ in self.workers.values()):
                raise ValueError('Wait for the current local worker before mutating tasks')
            if op == 'create':
                if data.get('confirmed') is not True:
                    raise ValueError('Confirm displayed access and hard caps')
                selector = data['model']
                if selector not in {p['selector'] for p in self.profiles()}:
                    raise ValueError('Select an available named Chat profile')
                _, identity = self.connection(selector)
                if data.get('binding_id') != identity:
                    raise ValueError('Connection changed after confirmation')
                args = native_arguments()
                args.model = selector
                args.source = [self.read(Path(p)) for p in data['materials']]
                args.allow_execution = data.get('execution') is True
                args.allow_network = data.get('network') is True
                root = self.task('task-' + secrets.token_hex(8))
                for field, destination in (('max_turns', 'max_turns'), ('max_tokens', 'max_tokens'),
                        ('process_invocations', 'max_processes'), ('process_wall_seconds', 'max_process_seconds')):
                    value = data[field]
                    allowed = {int, float} if field == 'process_wall_seconds' else {int}
                    if type(value) not in allowed:
                        raise ValueError('Use numeric hard caps; task owner validates their bounds')
                    setattr(args, destination, value)
                ToolApplication.create(root, data['goal'], **native_creation_options(args))
                return {'task': root.name}
            root = self.task(name)
            # Launch grants also constrain execution of retained tasks. An old
            # manifest cannot silently add arbitrary host materials to a run.
            self.validate_sources(root)
            if op == 'switch':
                if data.get('confirmed') is not True:
                    raise ValueError('Confirm the selected recipient connection')
                if data['model'] not in {p['selector'] for p in self.profiles()}:
                    raise ValueError('Unknown named profile')
                binding, identity = self.connection(data['model'])
                if data.get('binding_id') != identity:
                    raise ValueError('Connection changed after confirmation')
                ToolApplication.load(root).switch_model(data['model'], switch_id=data['switch_id'],
                    reason=data['reason'], expected_connection=binding)
                return {'task': name}
            if op not in {'resume', 'feedback', 'approve'}:
                raise ValueError('Unsupported operation')
            kwargs = {}
            if op == 'feedback':
                if not isinstance(data['feedback'], str) or not data['feedback'].strip():
                    raise ValueError('Feedback is required')
                kwargs['feedback'] = data['feedback']
            if op == 'approve':
                approval = self.snapshot_unlocked_approval(root)
                if not approval or data['token'] != approval:
                    raise ValueError('Approval differs from current pending proposal')
                kwargs['approved_execution'] = data['token']
            stop = threading.Event()

            def run():
                try:
                    ToolApplication.load(root).run(**kwargs, progress=lambda _: None, should_stop=stop.is_set)
                except Exception:
                    with self.lock:
                        self.errors[name] = 'Worker could not continue; inspect original task records with CLI'

            thread = threading.Thread(target=run, daemon=True)
            self.errors.pop(name, None)
            self.workers[name] = thread, stop
            thread.start()
            return {'task': name, 'message': 'Worker dispatched; task outcome remains in original manifest'}

    def snapshot_unlocked_approval(self, root: Path) -> str | None:
        reason = self.record(root, 'session_manifest.json').get('status_reason', '')
        found = re.search(r'Process requires explicit approval; token=([a-f0-9]{64})', reason)
        return found[1] if found else None


def server(workspace: Workspace, port: int = 0) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # URLs and task content never enter access logs.

        def send(self, status, content, kind='application/json'):
            raw = content if isinstance(content, bytes) else json.dumps(public_data(content), ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; script-src 'self'; style-src 'self'; object-src 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def guard(self, mutation=False):
            if self.headers.get('Host') != self.server.authority:
                raise PermissionError
            if mutation and self.headers.get('Origin') != self.server.origin:
                raise PermissionError
            if self.headers.get('Authorization') != 'Bearer ' + token:
                raise PermissionError

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                if url.path in {'/', '/app.js', '/style.css'}:
                    if self.headers.get('Host') != self.server.authority:
                        raise PermissionError
                    filename = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[url.path]
                    self.send(200, (Path(__file__).parent / 'ui_assets' / filename).read_bytes(),
                              mimetypes.guess_type(filename)[0] or 'text/plain')
                    return
                self.guard()
                q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
                if url.path == '/api/meta':
                    self.send(200, dict(project=str(workspace.project), reads=list(map(str, workspace.reads)),
                                        tasks=str(workspace.tasks), profiles=workspace.profiles(),
                                        creation_defaults=workspace.creation_defaults()))
                elif url.path == '/api/tasks':
                    self.send(200, workspace.listing())
                elif url.path == '/api/task':
                    self.send(200, workspace.snapshot(q['task']))
                elif url.path == '/api/delivery':
                    self.send(200, workspace.delivery(q['task']))
                elif url.path == '/api/files':
                    p = workspace.file(q['task'], q['mount'], q.get('path', ''))
                    if p.is_dir():
                        rows = []
                        children = list(islice(p.iterdir(), 257))
                        if len(children) > 256:
                            raise ValueError('Choose a narrower directory (256 entries maximum)')
                        for child in sorted(children):
                            try:
                                rel = (Path(q.get('path', '')) / child.name).as_posix()
                                checked = workspace.file(q['task'], q['mount'], rel)
                                rows.append(dict(name=child.name, path=rel, directory=checked.is_dir()))
                            except ValueError:
                                continue
                        self.send(200, rows)
                    else:
                        self.send(200, [dict(name=p.name, path='', directory=False)])
                elif url.path == '/api/file':
                    p = workspace.file(q['task'], q['mount'], q.get('path', ''))
                    if not p.is_file() or p.stat().st_size > 16 * 1024 * 1024:
                        raise ValueError('Preview/download limit is 16 MiB')
                    raw = p.read_bytes()
                    kind = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
                    if p.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.pdf', '.zip'}:
                        raw = public_text(raw.decode('utf-8')).encode()
                        kind = 'text/plain; charset=utf-8'
                    self.send(200, raw, kind)
                else:
                    self.send(404, {'error': 'Unknown route'})
            except PermissionError:
                self.send(403, {'error': 'Local host/session protection rejected request'})
            except Exception:
                self.send(400, {'error': 'Unavailable record, path or configuration; inspect launch grants and original task'})

        def do_POST(self):
            try:
                self.guard(mutation=True)
                if self.path != '/api/action' or self.headers.get_content_type() != 'application/json':
                    raise ValueError
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 65536:
                    raise ValueError
                data = json.loads(self.rfile.read(size))
                self.send(200, workspace.mutate(data))
            except PermissionError:
                self.send(403, {'error': 'Local host/origin/session protection rejected request'})
            except Exception:
                self.send(400, {'error': 'Action rejected; check confirmation, caps, current proposal and original task records'})

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    httpd.daemon_threads = True
    httpd.authority = f'127.0.0.1:{httpd.server_port}'
    httpd.origin = 'http://' + httpd.authority
    httpd.session_token = token
    return httpd


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--read-root', type=Path, action='append', default=[])
    parser.add_argument('--task-root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    workspace = Workspace(args.project_root.absolute(), [p.absolute() for p in args.read_root],
                          args.task_root.absolute())
    httpd = server(workspace, args.port)
    print(f'Open {httpd.origin}/#{httpd.session_token}', flush=True)
    print('Keep this process alive. Stop requests wait for action boundaries; no provider cancellation.', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        with workspace.lock:
            workers = list(workspace.workers.values())
            for _, stop in workers:
                stop.set()
        if any(thread.is_alive() for thread, _ in workers):
            print('Waiting for current actions to return and save boundary stops; no provider cancellation.', flush=True)
        for thread, _ in workers:
            thread.join()


if __name__ == '__main__':
    main()
