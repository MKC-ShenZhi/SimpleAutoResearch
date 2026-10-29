"""Exact bytes for new terminal deliveries, attached to their original attempts.

Only delivery commit writes here. Missing history is reported, never rebuilt
from current files. No workspace snapshot, quota or history migration.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile

from simple_ar.core.artifacts import write_json
from simple_ar.tools.specs import tool_completion


_OBSERVATION = re.compile(r'attempts/[A-Za-z0-9][A-Za-z0-9_-]{0,100}/native_result\.json')


class DeliveryVersions:
    def __init__(self, root: Path):
        from simple_ar.tools.workspace import RetainedDelivery
        self.reader = RetainedDelivery(root)
        self.root = self.reader.root

    def _path(self, relative: str) -> Path:
        from simple_ar.tools.workspace import resolve_mounted_path
        return resolve_mounted_path({'workspace': self.root}, relative, discovery=True,
            authorize=self.reader.admit)

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
