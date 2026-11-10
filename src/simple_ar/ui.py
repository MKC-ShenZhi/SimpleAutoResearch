"""Local thin client. Start with python -m simple_ar.ui --help.

Only the worker liveness/stop signal is volatile. All research state belongs to
ToolApplication and its original session, supply, approval and ledger owners.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import io
from itertools import islice
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from simple_ar.app.tool_application import ToolApplication
from simple_ar.app.reuse import ReuseAccess
from simple_ar.app.task_view import read_task_view, saved_model_recipients
from simple_ar.cli.project_tasks import discover_project_tasks
from simple_ar.cli.start import add_start_parser, migrate_new_start, native_creation_options
from simple_ar.cli.task_options import apply_task_config
from simple_ar.core.budget import BudgetLedger
from simple_ar.integrations.llm import LLMClient
from simple_ar.integrations.model_profiles import load_model_catalog
from simple_ar.tools.sources import SourceToolGateway
from simple_ar.tools.workspace import RetainedDelivery, resolve_mounted_path, resolve_source_mount


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
        self.creation_key = secrets.token_bytes(32)

    def task(self, name: str) -> Path:
        # Discovery exposes native direct-child names, not an ASCII identifier.
        # Keep '.' for a task rooted at the launch directory and for creation
        # allow a not-yet-existing child; the native task owner validates state.
        child = Path(name)
        if name != '.' and (not name or name == '..' or child.anchor or child.name != name):
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
                rows.append(dict(selector='profile:' + name, model=profile.model, default=name == default_name, recipients=saved_model_recipients(binding),
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
        _, _, limits, _ = ToolApplication.preview_creation(self.tasks, 'Configuration preview', **options)
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

    def extension_grants(self, entries: list[Path]) -> dict[str, str]:
        """Access preflight only; the extension owner validates/freezes contracts.

        Inspect descriptor path references before freeze_extensions can read them.
        Do not import Python or treat read_only/plan as an OS sandbox.
        """
        descriptors = {}
        for entry in entries:
            path = self.read(entry)
            path = self.read(path / 'extension.toml') if path.is_dir() else path
            if path.stat().st_size > 65536:
                raise ValueError('Extension descriptor exceeds display limit')
            raw = path.read_bytes()
            doc = tomllib.loads(raw.decode('utf-8'))
            meta = doc.get('extension', {})
            references = [*meta.get('resources', [])]
            if meta.get('instructions'):
                references.append(meta['instructions'])
            references.extend(tool['implementation'] for tool in doc.get('tools', []))
            for reference in references:
                self.read(path.parent / reference)
            descriptors[str(path)] = hashlib.sha256(raw).hexdigest()
        return descriptors

    def creation_inputs(self, data: dict) -> tuple[argparse.Namespace, str | None]:
        args = native_arguments()
        args._explicit_start_destinations = set()
        config_digest = None
        if data.get('task_config'):
            args.task_config = self.read(Path(data['task_config']).expanduser().absolute())
            if args.task_config.stat().st_size > 65536:
                raise ValueError('Task configuration must be at most 64 KiB')
            config_digest = hashlib.sha256(args.task_config.read_bytes()).hexdigest()
            apply_task_config(args)
            if hashlib.sha256(args.task_config.read_bytes()).hexdigest() != config_digest:
                raise ValueError('Task configuration changed during inspection')
            if ('output_root' in args._explicit_start_destinations
                    and args.output_root != self.tasks.resolve()):
                raise ValueError('Task output_root must match the UI launch task root')
        else:
            args.request = data['goal']
            args.model = data['model']
            args.source = [Path(p) for p in data['materials']]
            if data.get('from_session'):
                args.from_session = Path(data['from_session'])
                args.reuse = data.get('reuse', ['artifacts'])
                args.reuse_version = data.get('reuse_version')
            args.allow_execution = data.get('execution') is True
            args.allow_network = data.get('network') is True
            for field, destination in (('max_turns', 'max_turns'), ('max_tokens', 'max_tokens'),
                    ('process_invocations', 'max_processes'), ('process_wall_seconds', 'max_process_seconds')):
                value = data[field]
                allowed = {int, float} if field == 'process_wall_seconds' else {int}
                if type(value) not in allowed:
                    raise ValueError('Use numeric hard caps; task owner validates their bounds')
                setattr(args, destination, value)
        # Check every material before common migration inspects package contents.
        for field in ('source', 'document', 'material', 'data_path'):
            setattr(args, field, [self.read(Path(p).expanduser().absolute()) for p in getattr(args, field)])
        for field in ('project', 'data_file'):
            if getattr(args, field) is not None:
                setattr(args, field, self.read(getattr(args, field)))
        args.extensions = [self.read(p) for p in args.extensions]
        if args.from_session is not None:
            args.from_session = self.read(args.from_session)
        # Common CLI migration rechecks declared analysis packages. Grant their
        # explicitly referenced attachments BEFORE that owner opens any bytes.
        # This is path admission, not a second package validator or importer.
        self.material_grants(args.material)
        if not (args.request or args.goal or '').strip():
            raise ValueError('Supply a nonempty task goal')
        # UI main detaches terminal input. These flags suppress access/selection
        # questions: the browser previews and confirms the complete projection.
        args.yes = args.prepare_only = True
        migrate_new_start(args, reuse_access=ReuseAccess(authorize=self.read, preflight=self.material_grants))
        args.source = [self.read(p) for p in args.source]
        args.request = args.request or args.goal
        catalog = load_model_catalog()
        if catalog is None or (args.model and args.model != 'env' and not args.model.startswith(('profile:', 'route:'))):
            raise ValueError('Select a named Chat connection')
        name, _ = catalog.select(None if args.model == 'env' else args.model, purpose='text')
        args.model = 'profile:' + name
        if args.model not in {p['selector'] for p in self.profiles()}:
            raise ValueError('Select an available named Chat profile')
        _, identity = self.connection(args.model)
        if not data.get('task_config') and data.get('binding_id') != identity:
            raise ValueError('Connection changed after selection')
        return args, config_digest

    def material_grants(self, paths: list[Path]) -> None:
        """Admit selected package references before the common importer reads them."""
        for path in paths:
            path = self.read(path)
            if path.is_file() and path.suffix.lower() == '.json':
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise ValueError('Material exceeds UI inspection limit')
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
                schema = str(payload.get('schema_version', '')) if isinstance(payload, dict) else ''
                if schema.startswith('table_analysis.'):
                    self.read(path.parent / payload['source']['path'])
                elif schema.startswith('code_analysis.'):
                    for relative in [*payload['files'], 'outputs/results.json']:
                        self.read(path.parent / relative)

    def creation_configuration(self, root: Path, goal: str, options: dict) -> tuple:
        descriptors = self.extension_grants(options['extensions'])
        result = ToolApplication.preview_creation(root, goal, **options)
        for extension in result[0].get('extensions', []):
            if descriptors.get(extension['path']) != extension['descriptor_sha256']:
                raise ValueError('Extension descriptor changed during inspection')
        return result

    def creation_identity(self, args: argparse.Namespace, config_digest: str | None,
                          options: dict, result: tuple) -> tuple[str, dict | None]:
        image = None
        if args.max_images:
            name, profile = load_model_catalog().select(purpose='image')
            image = {'profile': name, **profile.model_dump(mode='json')}
        frozen = dict(goal=args.request, task_config=str(args.task_config) if args.task_config else None,
                      config_digest=config_digest, options=options,
                      configuration=ToolApplication.creation_binding(result), image=image)
        seal = hmac.new(self.creation_key, json.dumps(frozen, sort_keys=True, default=str,
            allow_nan=False).encode(), hashlib.sha256).hexdigest()
        return seal, image

    def preview_creation(self, data: dict) -> dict:
        args, digest = self.creation_inputs(data)
        options = native_creation_options(args)
        config, _, limits, attempts = result = self.creation_configuration(self.tasks, args.request, options)
        seal, image = self.creation_identity(args, digest, options, result)
        binding = config['model_connection']
        recipients = saved_model_recipients(binding)
        if config.get('vision_connection'):
            recipients['vision'] = saved_model_recipients(config['vision_connection'])['task']
        if image:
            recipients['image'] = {k: image[k] for k in ('profile', 'api', 'base_url', 'model')}
        return dict(preview_id=seal, goal=args.request, task_config=str(args.task_config) if args.task_config else None,
            creation_declarations=config['creation_declarations'],
            task_root=str(self.tasks), model=config['model'],
            binding_id=hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
            recipients=recipients, sources=config['sources'], project_python=config['project_python'],
            access={k: config[k] for k in ('allow_execution', 'allow_network', 'allow_vision')},
            limits=limits, attempts=attempts, extensions=config.get('extensions', []))

    def listing(self) -> list[dict]:
        tasks, _ = discover_project_tasks(self.tasks)
        return [dict(task='.' if task.path == self.tasks else task.path.name,
                     topic=task.goal, status=task.status) for task in tasks]

    def validate_sources(self, root: Path) -> None:
        config = self.record(root, 'actions.json')['config']
        for extension in config.get('extensions', []):
            descriptor = self.read(Path(extension['path']))
            if self.extension_grants([descriptor]).get(str(descriptor)) != extension['descriptor_sha256']:
                raise ValueError('Retained extension descriptor changed')
            for relative in extension['files']:
                self.read(descriptor.parent / relative)
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
                          summary=saved.get('summary'), unresolved=saved.get('unresolved', []),
                          limitations=saved.get('limitations', []), files=[])
            from simple_ar.tools.delivery_versions import DeliveryVersions
            try:
                result['versions'] = [dict(number=number, status=row['status'],
                    summary=row['delivery'].get('summary'), file_count=len(row['files']),
                    origin=dict(from_session=str(root), reuse_version=number, reuse=['artifacts']))
                    for number, row in enumerate(DeliveryVersions(root,
                        authorize=lambda path: bounded(path, root)).versions(), 1)]
            except (OSError, ValueError, KeyError, TypeError):
                result['versions'] = []
            worker = self.workers.get(name)
            if (worker and worker[0].is_alive() or state.get('pending')
                    or manifest['status'] not in {'completed', 'partial'} or not state['turns']
                    or state['turns'][-1].get('request_count', 1) != len(state['requests'])
                    or state['turns'][-1]['remaining']):
                return result
            try:
                for directory in ('workspace', 'evidence'):
                    if not bounded(root / directory, root).is_dir():
                        return result
                def admit(path):
                    if path.is_relative_to(root):
                        return bounded(path, root)
                    return self.read(path)
                reader = RetainedDelivery(root, authorize=admit)
                groups = reader.reusable_materials()
                files, unavailable = [], set()
                for group, paths in groups.items():
                    for path in paths:
                        for mount in ('workspace', 'evidence'):
                            if path.is_relative_to(root / mount):
                                relative = path.relative_to(root / mount).as_posix()
                                try:
                                    self.file(name, mount, relative)
                                except ValueError:
                                    # The native owner already checked every
                                    # delivered byte. HTTP preview policy is a
                                    # separate, narrower display capability.
                                    unavailable.add(path)
                                    break
                                files.append(dict(group=group, mount=mount, path=relative, name=path.name))
                                break
                if (self.record(root, 'session_manifest.json') != manifest
                        or self.record(root, 'actions.json') != state):
                    return result
                result.update(current=True, label='当前可复用交付（原 owner 核查；不认证科研质量）',
                              files=files, unavailable_file_count=len(unavailable))
                from simple_ar.tools.delivery_review import recorded_delivery_review
                try:
                    result['review'] = recorded_delivery_review(reader)
                except (OSError, ValueError, KeyError, TypeError):
                    result['review_notice'] = '检查意见记录不可核对；不推断检查结果。'
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result['diagnostic'] = public_text(str(exc))
            return result

    def task_view(self, root: Path) -> dict:
        """Supply UI read grants; the app layer owns saved-task interpretation."""
        def admit_execution():
            self.validate_sources(root)
            for directory in ('workspace', 'evidence'):
                if not bounded(root / directory, root).is_dir():
                    raise ValueError('Prepared task directory unavailable')

        return read_task_view(root, read_record=lambda name: self.record(root, name),
            ledger=BudgetLedger.load(bounded(root / 'budget_ledger.json', root)),
            authorize_execution=admit_execution, native_recipe=ToolApplication.RECIPE)

    def snapshot(self, name: str) -> dict:
        view = self.task_view(self.task(name))
        source_access = {}
        for mount in view.get('sources', []):
            try:
                self.file(name, mount, '')
                source_access[mount] = True
            except (OSError, ValueError, KeyError):
                source_access[mount] = False
        with self.lock:
            worker = self.workers.get(name)
            live = bool(worker and worker[0].is_alive())
            stopping = bool(live and worker[1].is_set())
            error = self.errors.get(name)
            local_workers = [task for task, (thread, _) in self.workers.items() if thread.is_alive()]
        return dict(view, task=name, source_access=source_access,
            mode='live execution' if live else 'saved results browser',
            worker_live=live, stop_requested=stopping, worker_error=error,
            local_workers=local_workers)

    def file(self, name: str, mount: str, relative: str) -> Path:
        root = self.task(name)
        if mount in {'workspace', 'evidence'}:
            base = bounded(root / mount, root)
        else:
            source = self.record(root, 'actions.json')['config']['sources'][mount]
            base = self.read(Path(source))
        if base.is_dir():
            bounded(base / relative, base)
            path = resolve_mounted_path({'view': base}, 'view/' + (relative or '.'))
            parts = path.relative_to(base).parts
        else:
            if relative:
                raise ValueError('File mount has no children')
            path, parts = base, (base.name,)
        # Shared resolvers own native private/runtime exclusions. These extra
        # HTTP preview exclusions concern mounted contents (or a file root's
        # basename), not names of explicitly granted directory ancestors.
        if any(p.startswith('.') or re.search(r'(?i)(credential|secret|^keys?$|^logs?$|\.log$|\.pem$|\.key$|^models\.toml$)', p)
               for p in parts):
            raise ValueError('Private/configuration files cannot be served')
        return path

    def source_preview(self, name: str, mount: str, relative: str) -> dict | None:
        path = self.file(name, mount, relative)
        if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Preview/download limit is 16 MiB')
        root = self.task(name)
        if mount == 'workspace' or any(path.is_relative_to(root / d) for d in ('workspace', 'attempts')):
            return None
        # Only an explicitly clicked, currently authorized file is inspected.
        # The source owner recognizes the declared schema and owns coverage.
        try:
            return SourceToolGateway.inventory_source(path)
        except (ValueError, TypeError, KeyError):
            return None  # Malformed declarations remain downloadable text.

    def mutate(self, data: dict) -> dict:
        op = data['op']
        name = data.get('task', '')
        if op == 'feedback':
            root = self.task(name)
            for path in ('session_manifest.json', 'actions.json',
                         'tools/submissions/feedback.json', 'tools/submissions/.feedback.lock'):
                bounded(root / path, root)
            # Reception has its own original owner/lock. A running worker must
            # not prevent acceptance or write feedback into a loaded snapshot.
            receipt = ToolApplication.submit_feedback(root, submission_id=data['submission_id'],
                feedback=data['feedback'])
            result = {'task': name, 'submission': receipt,
                'message': 'Feedback received; adoption remains pending at a safe action boundary'}
            with self.lock:
                if receipt['status'] == 'applied':
                    result['message'] = 'Feedback was already adopted; no duplicate execution dispatched'
                elif any(thread.is_alive() for thread, _ in self.workers.values()):
                    result['message'] += '; use resume if this task has no active worker'
                else:
                    try:
                        self.validate_sources(root)
                    except (OSError, ValueError, KeyError, TypeError):
                        result['message'] += '; restore launch read grants before resuming execution'
                    else:
                        self.dispatch(name, root, {'submission_id': data['submission_id']})
            return result
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
            if op == 'preview_create':
                return self.preview_creation(data)
            if op == 'create':
                if data.get('confirmed') is not True:
                    raise ValueError('Confirm displayed access and hard caps')
                args, _ = self.creation_inputs(data)
                root = self.task('task-' + secrets.token_hex(8))
                options = native_creation_options(args)
                descriptors = self.extension_grants(options['extensions'])

                def verify(result):
                    # Check the owner's actual projection and fresh grants
                    # before it creates the session or its ledger.
                    current, digest = self.creation_inputs(data)
                    current_options = native_creation_options(current)
                    if current.request != args.request or current_options != options:
                        raise ValueError('Task configuration changed during creation')
                    current_descriptors = self.extension_grants(current_options['extensions'])
                    for extension in result[0].get('extensions', []):
                        if (descriptors.get(extension['path']) != extension['descriptor_sha256']
                                or current_descriptors.get(extension['path']) != extension['descriptor_sha256']):
                            raise ValueError('Extension descriptor changed during inspection')
                    identity, _ = self.creation_identity(current, digest, current_options, result)
                    if not hmac.compare_digest(str(data.get('preview_id', '')), identity):
                        raise ValueError('Effective configuration changed; preview and confirm again')

                ToolApplication.create(root, args.request, verify_creation=verify, **options)
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
            if op not in {'resume', 'approve'}:
                raise ValueError('Unsupported operation')
            kwargs = {}
            if op == 'approve':
                approval = self.snapshot_unlocked_approval(root)
                if not approval or data['token'] != approval:
                    raise ValueError('Approval differs from current pending proposal')
                remember = data.get('remember_execution', False)
                if type(remember) is not bool:
                    raise ValueError('Remembering execution must be a boolean choice')
                kwargs['approved_execution'] = data['token']
                kwargs['remember_execution'] = remember
            self.dispatch(name, root, kwargs)
            return {'task': name, 'message': 'Worker dispatched; task outcome remains in original manifest'}

    def dispatch(self, name: str, root: Path, kwargs: dict) -> None:
        """Launch a local worker under self.lock; no input or task state is owned here."""
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

    def snapshot_unlocked_approval(self, root: Path) -> str | None:
        approval = self.task_view(root)['approval']
        return approval['token'] if approval else None


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
                elif url.path == '/api/source-preview':
                    self.send(200, workspace.source_preview(q['task'], q['mount'], q.get('path', '')))
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
    # A thin HTTP consumer never asks the launch terminal CLI intake questions.
    sys.stdin = io.StringIO()
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
