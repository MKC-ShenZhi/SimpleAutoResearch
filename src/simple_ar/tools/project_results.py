"""Read-only discovery of the existing task manifests in a project directory."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import json
import os
from pathlib import Path
import stat
from typing import Callable

from simple_ar.core.session import SessionManifest


MAX_ENTRIES = 256
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProjectTask:
    path: Path
    goal: str
    status: str


def _linked(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def discover_project_tasks(directory: Path, *, authorize: Callable[[Path], Path] | None = None
                           ) -> tuple[list[ProjectTask], int]:
    """List the directory and immediate children without loading or running tasks."""
    directory = directory.expanduser().absolute()
    for ancestor in reversed((directory, *directory.parents)):
        if _linked(ancestor):
            raise ValueError('Task directory and its ancestors must not be links/junctions')
    directory = directory.resolve(strict=True)
    with os.scandir(directory) as entries:
        children = list(islice(entries, MAX_ENTRIES + 1))
    if len(children) > MAX_ENTRIES:
        raise ValueError(f'Task directory exceeds {MAX_ENTRIES} entries; choose a narrower directory')
    tasks, skipped = [], 0
    candidates = [directory] + sorted((Path(entry.path) for entry in children), key=lambda p: p.name)
    for candidate in candidates:
        try:
            if _linked(candidate):
                skipped += 1
                continue
            if not candidate.is_dir():
                continue
            manifest_path = candidate / 'session_manifest.json'
            if not manifest_path.exists() and not manifest_path.is_symlink():
                continue
            if (_linked(manifest_path) or not stat.S_ISREG(manifest_path.lstat().st_mode)
                    or manifest_path.resolve().parent != candidate):
                raise ValueError('Linked or nonregular manifest')
            if authorize is not None:
                authorize(manifest_path)
            with manifest_path.open('rb') as stream:
                content = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ValueError('Manifest size limit exceeded')
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError('Manifest must be an object')
            if (payload.get('schema_version') != 'session_manifest.v2'
                    or payload.get('transition_recipe') != 'native-tools-v1'):
                skipped += 1
                continue
            manifest = SessionManifest.from_dict(payload)
            tasks.append(ProjectTask(candidate, manifest.topic, manifest.status))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            skipped += 1
    return tasks, skipped
