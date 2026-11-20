"""Retained delivery reader and project inventory helpers for WorkspaceToolGateway."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Literal

from simple_ar.core.artifacts import is_private_path_name as _private_name, read_json
from simple_ar.tools.specs import tool_completion
from simple_ar.tools.workspace_models import (
    DeliveryRequest,
    PartialProjectDelivery,
    _ContextEdit,
    _CopyProject,
    _FULL_DIGEST_MAX_BYTES,
    _List,
    _TOOLS,
    resolve_mounted_path,
)

def current_delivery_turn(manifest: dict, state: dict) -> dict | None:
    """Find the closed turn that can contain delivery for the current request."""
    if (manifest.get('status') not in {'completed', 'partial'} or state.get('pending')
            or not state.get('turns')):
        return None
    turn = state['turns'][-1]
    if (turn.get('remaining') or not turn.get('tools')
            or turn.get('request_count', 1) != len(state.get('requests', []))):
        return None
    return turn


def read_terminal_delivery(reader: Any, turn: dict, *, terminal: dict | None = None
                           ) -> tuple[DeliveryRequest, dict]:
    """Read the committed claim through the caller's record admission."""
    item = turn['tools'][-1]
    calls = reader.record(turn['reply']).get('tool_calls') or []
    if not calls or calls[-1]['id'] != item['id']:
        raise ValueError('Current delivery has no matching terminal call')
    if terminal is None:
        terminal = reader.record(item['result'])
    name = calls[-1]['function']['name']
    completion = tool_completion(name, reader.state['config'].get('tool_completions'))
    if (completion is None or completion.kind != 'delivery'
            or terminal.get('status') != 'ok' or terminal.get('tool_name') != name):
        raise ValueError('Current delivery has no matching terminal observation')
    data = terminal.get('data') or {}
    bound = data.get(completion.field) if completion.field else data
    delivery = DeliveryRequest.model_validate(reader.record('delivery.json'))
    if DeliveryRequest.model_validate(bound) != delivery:
        raise ValueError('Saved delivery differs from its terminal observation')
    return delivery, terminal


class RetainedDelivery:
    """Read the existing delivery and provenance without assembling executable owners.

    The optional admission callback runs before every record/artifact read. Saved
    source mounts describe paths only; unused sources and extensions are never
    opened, imported, or required to exist. No state is created or repaired.
    """

    def __init__(self, root: Path, *, authorize: Any = None) -> None:
        self.root = root.absolute()
        self.authorize = authorize
        self.manifest = self.record('session_manifest.json')
        if self.manifest['transition_recipe'] != 'native-tools-v1':
            raise ValueError('Retained delivery reader requires a native-tool session')
        self.state = self.record('actions.json')
        self.mounts = {name: Path(path).absolute()
            for name, path in self.state['config']['sources'].items()}
        if 'workspace' in self.mounts:
            raise ValueError('Sources cannot replace the writable workspace mount')
        self.mounts.update(workspace=self.root / 'workspace', evidence=self.root / 'evidence',
                           records=self.root / 'attempts')

    def admit(self, path: Path) -> Path:
        if self.authorize is not None:
            self.authorize(path)
        if '..' in path.parts:
            raise ValueError('Use an authorized path without traversal')
        # A granted task root can itself live below .codex (managed worktrees).
        # Its ancestors are not task contents. External sources retain the
        # original full-path private-name policy and admission callback.
        parts = path.relative_to(self.root).parts if path.is_relative_to(self.root) else path.parts
        if any(_private_name(part) for part in parts):
            raise ValueError('Private/runtime paths are not exposed to retained material reads')
        return path

    def record(self, locator: str) -> dict:
        path = resolve_mounted_path({'workspace': self.root}, locator, discovery=True,
                                    authorize=self.admit)
        return read_json(path)

    def resolve(self, locator: str) -> Path:
        # Admission precedes even discovery/stat of an external material.
        return resolve_mounted_path(self.mounts, locator, discovery=True, authorize=self.admit)

    def hashes(self, locators: Iterable[str]) -> dict[str, str]:
        return self._resolved_hashes({value: self.resolve(value) for value in locators})

    @staticmethod
    def _resolved_hashes(paths: dict[str, Path]) -> dict[str, str]:
        hashes = {}
        for value, path in paths.items():
            if not path.is_file():
                raise ValueError(f'Retained artifact is missing: {value}')
            with path.open('rb') as handle:
                hashes[value] = hashlib.file_digest(handle, 'sha256').hexdigest()
        return hashes

    def _delivery_files(self, turn: dict) -> tuple[dict[str, Path], dict]:
        delivery, terminal = read_terminal_delivery(self, turn)
        delivered = {value: self.resolve(value) for value in delivery.artifacts}
        paths = set(delivered.values())
        # External deliverables still require original controller registration.
        external = {p for p in paths if not any(p.is_relative_to(self.mounts[name])
            for name in ('workspace', 'evidence'))}
        if external:
            registered = []
            for path in sorted((self.root / 'attempts').iterdir()):
                if not (path / 'attempt_manifest.json').is_file():
                    continue
                attempt = self.record(f'attempts/{path.name}/attempt_manifest.json')
                if attempt['status'] == 'completed':
                    for ref in attempt['outputs']:
                        if ref['status'] == 'available' and ref['kind'] not in {
                                'native_reply', 'native_result', 'native_observation', 'caller_actions'}:
                            registered.append(resolve_mounted_path({'workspace': path}, ref['path'],
                                discovery=True, authorize=self.admit))
            if any(not any(p == output or p.is_relative_to(output) and output.is_dir()
                           for output in registered) for p in external):
                raise ValueError('Delivery references an unregistered external artifact')
        pinned = terminal.get('metadata', {}).get('delivery_hashes')
        actual = self._resolved_hashes(delivered)
        if pinned is not None and pinned != actual:
            raise ValueError('Current delivered files changed; retained results cannot certify their replacements')
        return delivered, terminal

    def reusable_materials(self) -> dict[str, list[Path]]:
        state = self.state
        turn = current_delivery_turn(self.manifest, state)
        if turn is None:
            return {}
        delivered, terminal = self._delivery_files(turn)
        terminal_record = turn['tools'][-1]['result']
        paths = set(delivered.values())
        available = {'artifacts': list(dict.fromkeys(delivered.values()))} if paths else {}
        for turn in state['turns']:
            for item in turn['tools']:
                row = terminal if item['result'] == terminal_record else self.record(item['result'])
                if row.get('status') != 'ok':
                    continue
                groups = row.get('metadata', {}).get('reusable_materials', {})
                if not groups:
                    continue
                # Historical groups unrelated to the delivery do not require old inputs.
                try:
                    owned = {self.resolve(value) for value in row.get('artifacts', [])}
                    candidates = {name: locators for name, locators in groups.items() if locators
                        and {self.resolve(value) for value in locators} <= paths & owned}
                except (OSError, ValueError):
                    # An unrelated historical group may no longer be readable.
                    # It cannot supply a named role, but current pinned files
                    # retain their independent artifact proof above.
                    continue
                if not candidates:
                    continue
                hashes = row['data']['artifact_sha256']
                try:
                    unchanged = self.hashes(hashes) == hashes
                except (OSError, ValueError):
                    continue
                if not unchanged:
                    continue
                for name, locators in candidates.items():
                    available[name] = [self.resolve(value) for value in locators]
        if self.record('session_manifest.json') != self.manifest or self.record('actions.json') != state:
            raise ValueError('Task changed during retained delivery inspection')
        return available

    def reusable_code_project(self, *, check_producers: Any) -> Path | None:
        """Offer one delivered complete copy, without inventing a project root.

        The terminal pins current bytes; a completed copy operation supplies the
        explicit root and complete member list. Neither proves code correctness,
        a checker, an environment or permission to execute in the next task.
        """
        turn = current_delivery_turn(self.manifest, self.state)
        if turn is None:
            return None
        paths, terminal = self._delivery_files(turn)
        if not paths:
            return None
        terminal_item = turn['tools'][-1]
        pinned = terminal.get('metadata', {}).get('delivery_hashes')
        if pinned is None:
            raise ValueError('Project reuse requires a hash-bound terminal delivery')
        delivered = {path: pinned[name] for name, path in paths.items()}
        candidates, records, history = {}, {}, []

        position = 0
        for turn in self.state['turns']:
            reply = self.record(turn['reply'])
            records[turn['reply']] = reply
            calls = {call['id']: call for call in reply.get('tool_calls', [])}
            for item in turn['tools']:
                position += 1
                call = calls.get(item['id'])
                if call is None:
                    continue
                observed = terminal if item['result'] == terminal_item['result'] else self.record(item['result'])
                name = call['function']['name']
                data = observed.get('data') or {}
                process_ref = (f'tools/processes/{Path(item["result"]).parent.name}/invocation.json'
                               if name == 'run_process' else data.get('receipt'))
                if isinstance(process_ref, str) and process_ref.startswith('tools/processes/'):
                    records[item['result']] = observed
                    history.append({'position': position, 'process_ref': process_ref})
                if observed.get('status') != 'ok' or observed.get('tool_name') != name:
                    continue
                arguments = json.loads(call['function']['arguments'])
                # Normalize version edges once. The graph below reasons about
                # versions and record boundaries, not a growing tool-name switch.
                event = None
                if (name in {'write_file', 'edit_file', 'copy_file'}
                        and isinstance(data.get('sha256'), str) and data.get('absolute_path')):
                    # Use the primitive's admitted contract, including omitted
                    # defaults. A similar-looking payload is not a write owner.
                    model = _ContextEdit if name == 'edit_file' else _TOOLS[name][0]
                    arguments = model.model_validate(arguments).model_dump()
                    path, digest = self.resolve(arguments['path']), data['sha256']
                    before = data.get('previous_sha256', arguments['expected_sha256'])
                    inputs = {path: before} if before else {}
                    links = {path: [(path, digest)]} if before == digest else {}
                    if arguments.get('source_sha256') == digest and 'source' in arguments:
                        source = self.resolve(arguments['source'])
                        inputs[source] = digest
                        links.setdefault(path, []).append((source, digest))
                    event = {'outputs': {path: digest}, 'inputs': inputs, 'links': links,
                             'consistent': data['absolute_path'] == str(path)}
                if name != 'copy_project':
                    if event is not None:
                        records[item['result']] = observed
                        attempt_path = (Path(item['result']).parent / 'attempt_manifest.json').as_posix()
                        attempt = self.record(attempt_path)
                        records[attempt_path] = attempt
                        history.append({**event, 'position': position,
                            'start': attempt['created_at'], 'end': attempt['updated_at']})
                    continue
                request = _CopyProject.model_validate(json.loads(call['function']['arguments']))
                project = self.resolve(request.path)
                data = observed['data']
                record_path = (Path(item['result']).parent / 'project-copy.json').as_posix()
                members = {self.resolve(str(project / name)): digest for name, digest in data['files'].items()}
                source = self.resolve(request.source)
                links = {path: [(self.resolve(str(source / path.relative_to(project))), digest)]
                         for path, digest in members.items()
                         if path.is_relative_to(project) and str(path.relative_to(project)) != request.archive_name}
                attempt_path = (Path(item['result']).parent / 'attempt_manifest.json').as_posix()
                attempt = self.record(attempt_path)
                records[attempt_path] = attempt
                records[item['result']] = observed
                event = {'position': position, 'start': attempt['created_at'], 'end': attempt['updated_at'],
                    'outputs': members, 'inputs': dict(link for group in links.values() for link in group), 'links': links,
                    'record': record_path, 'expected_record': {'request': request.model_dump(), 'data': data},
                    'consistent': (data['absolute_path'] == str(project) and data['source'] == str(source)
                        and project.is_relative_to(self.mounts['workspace']) and project != self.mounts['workspace']
                        and all(path.is_relative_to(project) for path in members))}
                history.append(event)
                # Ignore unrelated copies, including old snapshots not delivered.
                if not any(path.is_relative_to(project) for path in delivered):
                    continue
                receipt = self.record(record_path)
                if not event['consistent'] or receipt != event['expected_record']:
                    raise ValueError('Delivered project copy has inconsistent ownership')
                records[record_path] = receipt
                expected = members
                if data['copy']['skipped_count']:
                    raise PartialProjectDelivery('Delivered project copy has omissions; no complete project proof')
                files = data['files']
                if not files:
                    raise ValueError('Delivered project copy has no member inventory')
                if not expected.keys() <= delivered.keys():
                    raise PartialProjectDelivery('Deliver every copied project member before offering code_project')
                if any(delivered[path] != digest for path, digest in expected.items()):
                    raise PartialProjectDelivery('Delivered project changed since copying; create a final snapshot')
                inventory = bind_project_inventory(project, resolve=lambda value: self.resolve(value))
                if {Path(name): digest for name, digest in inventory['file_hashes'].items()} != expected:
                    raise PartialProjectDelivery('Delivered project inventory changed since copying')
                if project in candidates and candidates[project] != expected:
                    raise ValueError('Ambiguous copy inventories for the delivered project')
                candidates[project] = expected
        if not candidates:
            return None  # Portable files alone do not declare a complete project.
        if len(candidates) != 1:
            raise ValueError('Ambiguous delivery contains multiple code projects')
        ledger_record = self.record('budget_ledger.json')
        if any(entry['status'] in {'reserved', 'unknown'} and
               {'process_invocations', 'process_wall_seconds'} & entry['reserved'].keys()
               for entry in ledger_record['entries']):
            raise ValueError('Project has an unresolved process execution; no live project reuse')
        project, files = next(iter(candidates.items()))
        # The checker follows these existing receipts at their recorded cutoffs;
        # later versions of an origin cannot rewrite a copied version's owner.
        records.update(check_producers([str(path) for path in files],
            retained_versions=files, retained_history=history))
        # Freshness and terminal checks are read-only, and cannot grant authority
        # or settle/replay a process. Detect concurrent changes before returning.
        if (self.record('session_manifest.json') != self.manifest
                or self.record('actions.json') != self.state
                or self.record('budget_ledger.json') != ledger_record
                or self.record(terminal_item['result']) != terminal
                or any(self.record(name) != receipt for name, receipt in records.items())
                or self.hashes(pinned) != pinned
                or bind_project_inventory(project, resolve=lambda value: self.resolve(value))['file_hashes']
                    != {str(path): digest for path, digest in files.items()}):
            raise ValueError('Task changed during project reuse inspection')
        return project


def bind_project_inventory(project: Path, *, resolve: Any, list_page: Any = None,
                            material_candidates: bool | Literal['implementation'] = False) -> dict:
    if material_candidates not in (False, True, 'implementation'):
        raise ValueError('Unknown candidate inventory consumer')
    # Mounted pagination owns discovery/private exclusions. Code and legacy
    # receipts bind every visible file; writing filters before hash/read.
    rows, omitted, offset, total = [], [], 0, 0
    while True:
        inventory = (list_page(offset) if list_page else list_project_files(project,
            _List(path=str(project), recursive=True, limit=200, offset=offset),
            resolve=resolve, listed_path=lambda path, requested: str(path)))
        for row in inventory['entries']:
            if material_candidates:
                suffix = Path(row['path']).suffix.lower()
                # Known binary containers have no consumer in composition.
                # Other names remain possible text windows (including files
                # without extensions); eligibility is not a scientific role.
                figures = {'.png', '.jpg', '.jpeg', '.svg', '.pdf'}
                binary = {'.bin', '.pt', '.pth', '.ckpt', '.safetensors', '.npy', '.npz',
                    '.parquet', '.arrow', '.feather', '.h5', '.hdf5', '.pkl', '.pickle',
                    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.exe', '.dll',
                    '.so', '.dylib', '.o', '.obj', '.a', '.lib', '.mp4', '.mp3', '.wav',
                    '.gif', '.webp', '.tif', '.tiff', '.bmp', '.woff', '.woff2'}
                reason = ('unsupported_format' if material_candidates is True and suffix in binary else
                    'consumer_size_limit' if row['size_bytes'] > (
                        16_000_000 if material_candidates is True and suffix in figures else _FULL_DIGEST_MAX_BYTES) else '')
                if reason:
                    omitted.append({**row, 'reason': reason})
                    continue
            rows.append(row)
            total += row['size_bytes']
        if inventory['inventory_truncated'] or len(rows) + len(omitted) > 4096:
            raise ValueError('Project exceeds 4096 traversal entries; choose a focused project_path')
        if not material_candidates and total > 128 * 1024 * 1024:
            raise ValueError('Project exceeds the 128 MiB inventory/hash budget; choose a focused project_path')
        if not inventory['has_more']:
            break
        if inventory['next_offset'] <= offset:
            raise ValueError('Project inventory did not advance')
        offset = inventory['next_offset']
    if material_candidates:
        if material_candidates == 'implementation':
            return {'project_path': str(project), 'inventory_policy': 'implementation_candidates.v1',
                'candidate_files': {row['path']: row['size_bytes'] for row in rows}, 'omitted_files': omitted,
                'inventory_scope': 'Authorized file metadata, including binary dependencies, not observed '
                    'text or verified results. Files above 64 MiB, private/runtime paths, symlinks and '
                    'environments are omitted. Reading and byte-preserving copies have separate '
                    'admission and digest checks; listing grants no new access or authority.'}
        return {'project_path': str(project), 'inventory_policy': 'writing_candidates.v1',
            'candidate_files': {row['path']: row['size_bytes'] for row in rows}, 'omitted_files': omitted,
            'inventory_scope': 'Bounded authorized candidate paths/sizes, not a complete content inventory. '
                'Known unsupported binary formats and files above read_file full-digest (64 MiB) '
                'or figure (16 MB) limits are listed as omissions without reading/hashing. '
                'Unselected contents are not hash-bound; actual selected inputs bind full digests before Writer. '
                'Unlisted does not mean absent. Private/runtime paths, symlinks and Python environments '
                'are excluded before inspection. Text windows report unread ranges, not full reading '
                'or verified measurements.'}
    hashes = {}
    for row in rows:
        source = resolve(row['path'])
        if not source.is_file():
            raise ValueError('Retained inventory file is missing')
        with source.open('rb') as handle:
            hashes[row['path']] = hashlib.file_digest(handle, 'sha256').hexdigest()
    if len(hashes) != len(rows) or any(resolve(row['path']).stat().st_size
            != row['size_bytes'] for row in rows):
        raise ValueError('Project inventory changed while hashing; use a new candidate identity')
    return {'project_path': str(project), 'file_hashes': hashes,
        'inventory_scope': 'Complete mounted visible file inventory within bounds; private/runtime paths, symlinks and Python environments are excluded.'}

def list_project_files(target: Path, request: _List, *, resolve: Any, listed_path: Any,
                       path_allowed: Any = None) -> dict[str, Any]:
    if not target.is_dir():
        raise ValueError("Recursive listing requires a mounted directory")
    entries, matched, visited = [], 0, 0
    for directory, dirs, names in os.walk(target, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not _private_name(d)
                         and not (Path(directory) / d).is_symlink()
                         and not (Path(directory) / d / "pyvenv.cfg").is_file()
                         and (path_allowed is None or path_allowed(Path(directory) / d)))
        visited += len(dirs)
        for name in sorted(names):
            visited += 1
            if visited > 4096:
                break
            path = Path(directory) / name
            if _private_name(name) or path.is_symlink() or not fnmatch.fnmatchcase(name, request.file_glob):
                continue
            if path_allowed is not None and not path_allowed(path):
                continue
            relative = listed_path(path, request.path)
            path = resolve(relative)
            if not path.is_file():
                continue
            matched += 1
            if matched <= request.offset:
                continue
            if len(entries) == request.limit:
                return {"entries": entries, "next_offset": request.offset + len(entries),
                        "has_more": True, "inventory_truncated": False}
            entries.append({"path": relative, "size_bytes": path.stat().st_size})
        if visited > 4096:
            break
    return {"entries": entries, "next_offset": request.offset + len(entries),
            "has_more": False, "inventory_truncated": visited > 4096}
