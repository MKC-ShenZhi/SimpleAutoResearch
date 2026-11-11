"""Exact bytes for new terminal deliveries, attached to their original attempts.

Only delivery commit writes here. Missing history is reported, never rebuilt
from current files. No workspace snapshot, quota or history migration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from simple_ar.core.artifacts import write_json
from simple_ar.tools.specs import tool_completion


_OBSERVATION = re.compile(r'attempts/[A-Za-z0-9][A-Za-z0-9_-]{0,100}/native_result\.json')


class DeliveryVersions:
    def __init__(self, root: Path, *, authorize=None):
        from simple_ar.tools.workspace import RetainedDelivery
        self.reader = RetainedDelivery(root, authorize=authorize)
        self.root = self.reader.root

    def _path(self, relative: str) -> Path:
        from simple_ar.tools.workspace import resolve_mounted_path
        return resolve_mounted_path({'workspace': self.root}, relative, discovery=True,
            authorize=self.reader.admit)

    def _has_record(self, relative: str) -> bool:
        """Admit optional history before checking whether it actually exists.

        Scoped UI readers can require an existing path during admission. Only
        absence is optional; access denials and unsafe paths still propagate.
        """
        try:
            return self._path(relative).is_file()
        except FileNotFoundError:
            return False

    @staticmethod
    def _receipt(record: str) -> str:
        if not _OBSERVATION.fullmatch(record):
            raise ValueError('Delivery retention requires its original terminal attempt')
        return record.rsplit('/', 1)[0] + '/delivery-version/receipt.json'

    def _closed(self):
        for turn in self.reader.state['turns']:
            if not turn['remaining'] and turn['tools']:
                record = turn['tools'][-1]['result']
                if _OBSERVATION.fullmatch(record):
                    yield record, turn

    def _owner(self, record: str, turn: dict):
        calls = self.reader.record(turn['reply']).get('tool_calls') or []
        if not calls or calls[-1]['id'] != turn['tools'][-1]['id']:
            return None
        count = turn.get('request_count', 1)
        config = self.reader.state['config']
        for supply in reversed(self.reader.state.get('supplies', [])):
            if count < supply['request_count']:
                config = supply['previous_config']
        completion = tool_completion(calls[-1]['function']['name'], config.get('tool_completions'))
        if completion is None or completion.kind != 'delivery':
            return None
        observation = self.reader.record(record)
        if observation.get('status') != 'ok' or observation.get('error'):
            return None
        if observation.get('tool_name') != calls[-1]['function']['name']:
            raise ValueError('Terminal delivery observation differs from its original call')
        data = observation.get('data') or {}
        delivery = data.get(completion.field) if completion.field else data
        return (observation, delivery, count) if isinstance(delivery, dict) else None

    def _digest(self, relative: str) -> str:
        with self._path(relative).open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()

    def _load(self, record: str, observation: dict, delivery: dict, count: int):
        receipt = self._receipt(record)
        if not self._path(receipt).exists():
            return None
        saved = self.reader.record(receipt)
        if not isinstance(saved, dict):
            raise ValueError('Invalid retained delivery receipt')
        pinned = (observation.get('metadata') or {}).get('delivery_hashes')
        if (saved.get('schema') != 'delivery_version.v1'
                or saved.get('observation') != record
                or saved.get('observation_sha256') != self._digest(record)
                or saved.get('delivery') != delivery or saved.get('request_count') != count
                or not isinstance(saved.get('files'), dict) or not isinstance(pinned, dict)
                or set(saved['files']) != set(delivery.get('artifacts', []))
                or {key: row['sha256'] for key, row in saved['files'].items()} != pinned):
            raise ValueError('Retained delivery differs from its original terminal receipt')
        prefix = receipt.rsplit('/', 1)[0] + '/bytes/'
        for row in saved['files'].values():
            sha, size = row['sha256'], row['size_bytes']
            if (not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha)
                    or row['blob'] != prefix + sha or type(size) is not int or size < 0):
                raise ValueError('Invalid retained delivery byte reference')
            path = self._path(row['blob'])
            if not path.is_file() or path.stat().st_size != size:
                raise ValueError('Retained delivery bytes are missing or changed')
        return saved

    def versions(self) -> list[dict]:
        """Read-only metadata; broken history neither hides other rows nor blocks work.

        Never read live delivered files. Hashes are checked at capture and by
        ordinary copy preconditions, not by rehashing every version on listing.
        """
        rows = []
        for record, turn in self._closed():
            try:
                owner = self._owner(record, turn)
                if owner is None:
                    continue
                observation, delivery, count = owner
                saved = self._load(record, *owner)
                if saved is not None:
                    rows.append({**saved, 'status': 'retained'})
                    continue
                reason = 'No retained byte version; historical bytes cannot be reconstructed.'
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                delivery, count = {}, turn.get('request_count', 1)
                reason = f'Original version unavailable ({type(exc).__name__}); no bytes reconstructed.'
            rows.append({'observation': record, 'request_count': count, 'delivery': delivery,
                'files': {}, 'binding': 'unavailable', 'status': 'unavailable', 'reason': reason})
        return rows

    def select(self, version: int) -> dict:
        """Select an explicit retained delivery, independently of current work."""
        rows = self.versions()
        if type(version) is not int or not 1 <= version <= len(rows):
            raise ValueError('Select an existing retained delivery version number')
        selected = rows[version - 1]
        if selected['status'] != 'retained':
            raise ValueError('Selected delivery has no retained bytes; current files cannot reconstruct it')
        return selected

    def _workspace_relative(self, locator: str) -> PurePosixPath | None:
        """Identify a saved workspace path without opening the old live file."""
        workspace = self.reader.mounts['workspace']
        native = Path(locator)
        if native.is_absolute():
            if not native.is_relative_to(workspace):
                return None
            return PurePosixPath(native.relative_to(workspace).as_posix())
        path = PurePosixPath(locator)
        if '\\' in locator or '..' in path.parts:
            return None
        if path.parts and path.parts[0] == 'workspace':
            return PurePosixPath(*path.parts[1:])
        if path.parts and path.parts[0] not in self.reader.mounts:
            return path
        return None

    def _material_groups(self, version: int) -> tuple[dict[str, dict[str, dict]], PurePosixPath | None]:
        """Portable groups and, when declared, the delivered project layout."""
        from simple_ar.tools.workspace import _CopyProject
        selected = self.select(version)
        files = selected['files']
        groups = {'artifacts': dict(files)} if files else {}
        explicit = selected['delivery'].get('project_root')
        explicit_root = self._workspace_relative(explicit) if explicit else None
        roots = set()
        for turn in self.reader.state['turns']:
            for item in turn['tools']:
                try:
                    result = self.reader.record(item['result'])
                except (OSError, ValueError, KeyError, TypeError):
                    if item['result'] == selected['observation']:
                        raise
                    continue  # An unrelated missing opinion cannot erase captured input bytes.
                if result.get('status') == 'ok':
                    hashes = (result.get('data') or {}).get('artifact_sha256', {})
                    for name, locators in result.get('metadata', {}).get('reusable_materials', {}).items():
                        if name != 'code_project' and locators and all(
                                path in files and hashes.get(path) == files[path]['sha256'] for path in locators):
                            groups[name] = {path: files[path] for path in locators}
                    if result.get('tool_name') == 'copy_project' and explicit is None:
                        calls = self.reader.record(turn['reply']).get('tool_calls') or []
                        call = next((row for row in calls if row['id'] == item['id']), None)
                        if call is not None and call['function']['name'] == 'copy_project':
                            try:
                                request = _CopyProject.model_validate_json(call['function']['arguments'])
                                root = self._workspace_relative(request.path)
                                copy_data = result['data']
                                receipt = self.reader.record(str(PurePosixPath(item['result']).parent / 'project-copy.json'))
                            except (FileNotFoundError, KeyError, TypeError):
                                root = None
                            if (root is not None and root.parts
                                    and copy_data.get('absolute_path') == str(self.reader.mounts['workspace'] / root)
                                    and receipt == {'request': request.model_dump(), 'data': copy_data}):
                                roots.add(root)
                if item['result'] == selected['observation']:
                    candidates = {root for root in ([explicit_root] if explicit is not None else roots)
                        if root is not None and any((relative := self._workspace_relative(path)) is not None
                            and relative.is_relative_to(root) for path in files)}
                    project = next(iter(candidates)) if len(candidates) == 1 else None
                    if project is not None:
                        groups['code_project'] = {path: row for path, row in files.items()
                            if (relative := self._workspace_relative(path)) is not None
                            and relative.is_relative_to(project)}
                    return groups, project
        raise ValueError('Selected delivery no longer belongs to the original journal')

    def materials(self, version: int) -> dict[str, dict[str, dict]]:
        return self._material_groups(version)[0]

    def snapshot(self, version: int, names: list[str], *, max_bytes: int,
                 working_context: bool = False) -> tuple[dict, list[tuple[str, bytes]]]:
        """Prepare exact selected input bytes for a NEW task's evidence mount.

        The existing creation transaction writes these bytes. No cache, old-task
        write, execution permission or current completion is created here.
        """
        groups, project = self._material_groups(version)
        if not names or any(name not in groups for name in names):
            raise ValueError('Selected version does not contain every requested portable result group')
        files = {path: row for name in names for path, row in groups[name].items()}
        result, sources, layout, total = [], {}, {}, 0
        for locator, row in files.items():
            path = PurePosixPath(locator.replace('\\', '/'))
            if path.is_absolute():
                try:
                    path = path.relative_to(self.root.as_posix())
                except ValueError:
                    candidates = []
                    for alias, mount in self.reader.mounts.items():
                        original = PurePosixPath(mount.as_posix())
                        if path == original:
                            candidates.append(PurePosixPath(alias) / path.name)
                        elif original in path.parents:
                            candidates.append(PurePosixPath(alias) / path.relative_to(original))
                    if not candidates:
                        raise ValueError('Retained external files have no portable source layout') from None
                    path = min(candidates, key=lambda value: len(value.parts))
            elif path.parts and path.parts[0] not in self.reader.mounts:
                path = PurePosixPath('workspace') / path
            if not path.parts or any(part in {'', '.', '..'} for part in path.parts):
                raise ValueError('Retained delivery has no safe source-relative layout')
            relative = path.as_posix()
            layout[locator] = relative
            blob = self._path(row['blob'])
            total += row['size_bytes']
            if total > max_bytes:
                raise ValueError('Selected retained inputs exceed max_snapshot_bytes; narrow the selected groups')
            with blob.open('rb') as stream:
                content = stream.read(row['size_bytes'] + 1)
            if len(content) != row['size_bytes'] or hashlib.sha256(content).hexdigest() != row['sha256']:
                raise ValueError('Retained delivery bytes changed; no input snapshot created')
            if relative in sources:
                raise ValueError('Retained delivery has conflicting portable paths')
            sources[relative] = {'sha256': row['sha256'], 'size_bytes': len(content)}
            result.append((relative, content))
        handoff = self.working_context(version, files, layout, dict(result)) if working_context else None
        if handoff is not None and total + len(json.dumps(handoff, ensure_ascii=False).encode('utf-8')) > max_bytes:
            raise ValueError('Selected retained inputs and work context exceed max_snapshot_bytes; narrow the selected groups')
        if self.reader.record('actions.json') != self.reader.state:
            raise ValueError('Task changed during retained input selection')
        project_layout = (PurePosixPath('workspace') / project).as_posix() if 'code_project' in names else None
        return {'session': str(self.root), 'version': version, 'results': list(names), 'files': sources,
            'groups': {name: [layout[path] for path in groups[name]] for name in names},
            **({'project_root': project_layout} if project_layout is not None else {}),
            **({'working_context': handoff} if handoff is not None else {})}, result

    def working_context(self, version: int, files: dict, layout: dict, contents: dict) -> dict:
        """Original observations of explicitly selected bytes, not task authority.

        An equal hash at another path never establishes lineage. Only the
        original call's resolved subject and its measured retained byte range
        establish a transferable observation. Unrelated receipt fields, model
        reasoning, requests, execution state and opinions are not exported.
        """
        from simple_ar.tools.materials import journal_read_windows, lossless_text_window
        from simple_ar.tools.professional_outcomes import accepted_native_result
        selected = self.select(version)
        subjects = {str(self.reader.resolve(locator)): locator for locator in files}
        observations, notes, records, accepted = [], [], set(), {}
        for turn in self.reader.state['turns']:
            terminal = False
            try:
                reply = self.reader.record(turn['reply'])
            except FileNotFoundError:
                continue  # Missing unrelated work cannot erase retained bytes.
            calls = {call['id']: call for call in reply.get('tool_calls', [])}
            for item in turn['tools']:
                try:
                    receipt = self.reader.record(item['result'])
                except FileNotFoundError:
                    if item['result'] == selected['observation']:
                        raise
                    continue
                call = calls.get(item['id'])
                if call is None or receipt.get('tool_name') != call['function']['name']:
                    continue
                try:
                    arguments = json.loads(call['function']['arguments'])
                except (ValueError, TypeError):
                    continue
                for window in journal_read_windows(receipt, arguments):
                    data, request = window['data'], window['request']
                    try:
                        subject = str(self.reader.resolve(request['path']))
                    except (OSError, ValueError, KeyError):
                        continue
                    locator = subjects.get(subject)
                    if locator is None or data.get('sha256') != files[locator]['sha256']:
                        continue
                    # The recorded result must identify the called subject,
                    # not merely quote selected bytes in an unrelated receipt.
                    try:
                        observed_subject = str(self.reader.resolve(data.get('absolute_path') or data['path']))
                    except (OSError, ValueError, KeyError):
                        continue
                    start = data.get('offset_bytes', request.get('offset_bytes', 0))
                    end = data.get('next_offset_bytes')
                    raw = contents[layout[locator]]
                    if (observed_subject != subject or not lossless_text_window(data)
                            or type(start) is not int or type(end) is not int
                            or not 0 <= start <= end <= len(raw) or data.get('size_bytes') != len(raw)):
                        continue
                    try:
                        if raw[start:end].decode('utf-8') != data['text']:
                            continue
                    except UnicodeDecodeError:
                        continue
                    prefix = str(PurePosixPath(item['result']).parent)
                    # Access failures must escape optional proof validation;
                    # otherwise an unscoped later creation could import what
                    # this UI preview was forbidden to inspect.
                    if not all([self._has_record(prefix + '/' + name)
                            for name in ('attempt_manifest.json', 'capability_result.json')]):
                        continue  # Unregistered old reads remain ordinary retained bytes.
                    self._has_record(prefix + '/action.json')
                    try:
                        # Byte equality alone cannot turn an edited journal
                        # result into an original accepted observation.
                        if item['result'] not in accepted:
                            for name in ('attempt_manifest.json', 'capability_result.json'):
                                self.reader.record(prefix + '/' + name)
                            action = prefix + '/action.json'
                            seen = set()
                            while self._has_record(action):
                                binding = self.reader.record(action)
                                previous = binding.get('recovered_from')
                                if previous is None:
                                    break
                                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', previous) or previous in seen:
                                    raise ValueError('Invalid observation recovery chain')
                                seen.add(previous)
                                action = 'attempts/' + previous + '/action.json'
                            accepted_native_result(self.root, self.reader.manifest['session_id'],
                                receipt, Path(item['result']).parent.name, require_content_binding=True)
                            accepted[item['result']] = True
                        if not accepted[item['result']]:
                            continue
                    except (OSError, ValueError, KeyError, TypeError):
                        accepted[item['result']] = False
                        continue
                    record = 'records/' + item['result'].removeprefix('attempts/')
                    records.add(record)
                    observations.append({'path': layout[locator], 'sha256': data['sha256'],
                        'size_bytes': len(raw), 'offset_bytes': start, 'next_offset_bytes': end,
                        'text': data['text'], 'source': {'record': record,
                            'receipt_sha256': self._digest(item['result']), 'field': window['field'],
                            'request': request, 'subject': subject}})
                if item['result'] == selected['observation']:
                    terminal = True
                    break
            # Knowledge remains an original model claim. It is not adopted as
            # a checkpoint in the new task or allowed to retire its journal.
            snapshot = str(PurePosixPath(turn['reply']).parent / 'memory_snapshot.json')
            if self._has_record(snapshot):
                eligible = []
                prefix = str(PurePosixPath(snapshot).parent)
                proof_complete = all([self._has_record(prefix + '/' + name)
                    for name in ('attempt_manifest.json', 'capability_result.json',
                        'memory_input.json', 'memory_response.json')])
                proof_complete = self._has_record('budget_ledger.json') and proof_complete
                try:
                    if not proof_complete:
                        raise FileNotFoundError('Optional historical checkpoint proof is incomplete')
                    from simple_ar.app.work_memory import _bound_response, checkpoint_entries
                    from simple_ar.core.budget import BudgetLedger
                    manifest = self.reader.record(prefix + '/attempt_manifest.json')
                    capability = self.reader.record(prefix + '/capability_result.json')
                    required = {'memory_input.json', 'memory_response.json', 'memory_snapshot.json'}
                    registered = {ref['path'] for ref in manifest.get('outputs', [])
                        if ref in capability.get('artifacts', []) and ref.get('status') == 'available'}
                    if (manifest.get('status') != 'completed' or capability.get('status') != 'completed'
                            or not required <= registered):
                        raise ValueError('Historical understanding has no closed checkpoint registration')
                    ledger = BudgetLedger.load(self.reader.admit(self.root / 'budget_ledger.json'))
                    checkpoint_entries(ledger, Path(prefix).name, self.reader.manifest['session_id'])
                    view = self.reader.record(prefix + '/memory_input.json')
                    if self.reader.record(snapshot).get('input_sha256') != hashlib.sha256(
                            json.dumps(view, ensure_ascii=False).encode()).hexdigest():
                        raise ValueError('Historical understanding input changed')
                    # Re-derive text/citations from the original model response;
                    # no saved adoption/coverage or private version map transfers.
                    original_notes = _bound_response(self.reader.record(prefix + '/memory_response.json'),
                        view, lambda values: values)
                except (OSError, ValueError, KeyError, TypeError):
                    original_notes = []
                for note in original_notes:
                    citations = set(note.get('records', []))
                    if (citations and citations <= records
                            and isinstance(note.get('text'), str) and note['text'].strip()):
                        eligible.append({'text': note['text'], 'records': sorted(citations),
                            'knowledge_kind': note.get('knowledge_kind', 'judgment'),
                            **({'source_integrity': note['source_integrity']} if 'source_integrity' in note else {}),
                            'source': {'record': prefix + '/memory_response.json',
                                'sha256': self._digest(prefix + '/memory_response.json')}})
                notes = eligible  # Latest related snapshot, never accumulated private history.
            if terminal:
                return {'observations': observations, 'notes': notes,
                    'scope': 'Selected original byte windows and fallible historical working understanding. '
                        'Unselected, unbound and unread contents are excluded. No old request, '
                        'permission, completion, opinion approval, execution proof or ledger is inherited.'}
        raise ValueError('Selected delivery no longer belongs to the original journal')

    def material_paths(self, version: int) -> dict[str, list[Path]]:
        return {name: [self._path(row['blob']) for row in files.values()]
            for name, files in self.materials(version).items()}

    def _discard_stage(self, stage: Path, identity: dict) -> None:
        """Validate the entire owned stage before removing only known files.

        Empty stages are the old mkdir-before-marker crash window. rmdir never
        removes contents added since inspection. Atomic write_json temporaries
        belong to this stage only when its committed marker proves ownership.
        """
        stage = self._path(stage.relative_to(self.root).as_posix())
        if not stage.is_dir():
            raise ValueError('Unknown delivery staging path; no files removed')
        entries = list(stage.iterdir())
        if not entries:
            stage.rmdir()
            return
        marker = stage / 'receipt.json'
        saved = self.reader.record(marker.relative_to(self.root).as_posix()) if marker.is_file() else {}
        if (not isinstance(saved, dict)
                or saved.get('schema') not in {'delivery_version.staging.v1', 'delivery_version.v1'}
                or any(saved.get(key) != value for key, value in identity.items())):
            raise ValueError('Unknown delivery staging contents; no files removed')
        files, byte_dir = [], None
        for entry in entries:
            self._path(entry.relative_to(self.root).as_posix())
            if entry.name == 'bytes' and entry.is_dir():
                byte_dir = entry
                for blob in entry.iterdir():
                    self._path(blob.relative_to(self.root).as_posix())
                    if not blob.is_file() or not re.fullmatch(r'[a-f0-9]{64}|pending', blob.name):
                        raise ValueError('Unknown delivery staging bytes; no files removed')
                    files.append(blob)
            elif entry.name == 'receipt.json' and entry.is_file():
                continue
            elif entry.is_file() and re.fullmatch(r'\.receipt\.json\.[A-Za-z0-9_-]+\.tmp', entry.name):
                files.append(entry)
            else:
                raise ValueError('Unknown delivery staging contents; no files removed')
        # Do not recurse: new/unknown contents must survive even a cleanup race.
        for path in files:
            self._path(path.relative_to(self.root).as_posix()).unlink()
        if byte_dir is not None:
            byte_dir.rmdir()
        marker.unlink()
        stage.rmdir()

    def _prepare_stage(self, stage: Path, identity: dict) -> None:
        if stage.exists():
            self._discard_stage(stage, identity)
        # Only a complete, fsynced ownership marker is promoted into the fixed
        # staging name. A crash at any earlier instruction leaves an isolated
        # bootstrap orphan containing at most marker metadata, never file bytes.
        # Do not scan/delete such orphans: unknown user contents cannot block a
        # fresh bootstrap and remain untouched.
        bootstrap = Path(tempfile.mkdtemp(prefix='.delivery-version-bootstrap-', dir=stage.parent))
        marker = bootstrap / 'receipt.json'
        write_json(marker, {'schema': 'delivery_version.staging.v1', **identity})
        with marker.open('rb+') as stream:
            os.fsync(stream.fileno())
        if self._path(stage.relative_to(self.root).as_posix()).exists():
            raise ValueError('Delivery staging appeared during preparation; no files replaced')
        os.rename(bootstrap, stage)

    def retain(self, record: str, delivery: dict, hashes: dict, request_count: int) -> dict:
        """Stream only this new terminal attempt; never consult earlier versions.

        Storage failures propagate to the existing commit failure path. A
        retry verifies a published attempt without overwriting its bytes.
        """
        turns = self.reader.state['turns']
        turn = turns[-1] if turns else None
        if (turn is None or turn['remaining'] or not turn['tools']
                or turn['tools'][-1]['result'] != record):
            raise ValueError('Only the current closed terminal attempt can retain a new delivery')
        owner = self._owner(record, turn)
        if owner is None or owner[1] != delivery or owner[2] != request_count:
            raise ValueError('Delivery retention has no matching closed terminal observation')
        if not isinstance(hashes, dict) or hashes != (owner[0].get('metadata') or {}).get('delivery_hashes'):
            raise ValueError('Delivery retention cannot replace its original hash binding')
        existing = self._load(record, *owner)
        if existing is not None:
            for row in existing['files'].values():
                if self._digest(row['blob']) != row['sha256']:
                    raise ValueError('Retained delivery bytes changed; no replacement or replay')
            return existing
        destination = self._path(self._receipt(record)).parent
        if destination.exists():
            raise ValueError('Unknown delivery version directory; no files replaced')
        stage = self._path(destination.parent.relative_to(self.root).as_posix() + '/.delivery-version-pending')
        identity = {'observation': record, 'observation_sha256': self._digest(record)}
        self._prepare_stage(stage, identity)
        try:
            (stage / 'bytes').mkdir()
            files = {}
            for locator in delivery['artifacts']:
                source = self.reader.resolve(locator)
                if not source.is_file():
                    raise ValueError('Delivery retention source must be a regular file')
                temporary = stage / 'bytes' / 'pending'
                digest, size = hashlib.sha256(), 0
                with source.open('rb') as stream, temporary.open('wb') as output:
                    before = os.fstat(stream.fileno())
                    remaining = before.st_size
                    while remaining:
                        chunk = stream.read(min(65536, remaining))
                        if not chunk:
                            raise ValueError('Delivery retention source shrank; no version published')
                        remaining -= len(chunk)
                        size += len(chunk)
                        digest.update(chunk)
                        output.write(chunk)
                    if stream.read(1):
                        raise ValueError('Delivery retention source grew; no version published')
                    output.flush()
                    os.fsync(output.fileno())
                    after = os.fstat(stream.fileno())
                current = self.reader.resolve(locator).stat()
                def fingerprint(stat):
                    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
                sha = digest.hexdigest()
                if (fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(current)
                        or hashes.get(locator) != sha):
                    raise ValueError('Delivery retention source changed; no version published')
                blob = destination.relative_to(self.root).as_posix() + '/bytes/' + sha
                if (stage / 'bytes' / sha).exists():
                    temporary.unlink()
                else:
                    os.replace(temporary, stage / 'bytes' / sha)
                    if self._digest((stage / 'bytes' / sha).relative_to(self.root).as_posix()) != sha:
                        raise ValueError('Saved delivery bytes failed verification; no version published')
                files[locator] = {'blob': blob, 'sha256': sha, 'size_bytes': size}
            saved = {'schema': 'delivery_version.v1', **identity, 'request_count': request_count,
                'delivery': delivery, 'files': files, 'binding': 'terminal_hashes'}
            write_json(stage / 'receipt.json', saved)
            with (stage / 'receipt.json').open('rb+') as stream:
                os.fsync(stream.fileno())
            if self.reader.record('actions.json') != self.reader.state or self._digest(record) != identity['observation_sha256']:
                raise ValueError('Terminal owner changed during delivery retention')
            os.rename(stage, destination)
            return saved
        finally:
            if stage.exists():
                self._discard_stage(stage, identity)
