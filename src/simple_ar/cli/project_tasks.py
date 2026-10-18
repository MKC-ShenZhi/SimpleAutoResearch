"""Bounded, read-only discovery; the native application still owns resume/state."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from itertools import islice
import json
import os
from pathlib import Path
import stat

from simple_ar.core.console import print_line


MAX_ENTRIES = 256
MAX_MANIFEST_BYTES = 256 * 1024


@dataclass(frozen=True)
class ProjectTask:
    path: Path
    goal: str
    status: str

    @property
    def resume_command(self) -> str:
        from simple_ar.cli.task_options import resume_command
        return resume_command(self.path)


def _linked(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def discover_project_tasks(directory: Path) -> tuple[list[ProjectTask], int]:
    """Inspect only directory and direct children; return tasks and skipped count.

    No application/controller loading, attempt/artifact scanning or state writes.
    Oversized inventories fail closed instead of selecting from a partial listing.
    """
    from simple_ar.app.tool_application import ToolApplication
    from simple_ar.core.session import SessionManifest

    directory = directory.expanduser().absolute()
    for ancestor in reversed((directory, *directory.parents)):
        if _linked(ancestor):
            raise ValueError('--tasks-in and its ancestors must not be links/junctions')
    directory = directory.resolve(strict=True)
    with os.scandir(directory) as entries:
        children = list(islice(entries, MAX_ENTRIES + 1))
    if len(children) > MAX_ENTRIES:
        raise ValueError(f'--tasks-in exceeds {MAX_ENTRIES} entries; choose a narrower directory')
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
            with manifest_path.open('rb') as stream:
                content = stream.read(MAX_MANIFEST_BYTES + 1)
            if len(content) > MAX_MANIFEST_BYTES:
                raise ValueError('Manifest size limit exceeded')
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError('Manifest must be an object')
            if (payload.get('schema_version') != 'session_manifest.v2'
                    or payload.get('transition_recipe') != ToolApplication.RECIPE):
                skipped += 1
                continue
            manifest = SessionManifest.from_dict(payload)
            tasks.append(ProjectTask(candidate, manifest.topic, manifest.status))
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            skipped += 1
    return tasks, skipped


def handle_project_tasks(args: argparse.Namespace, *, interactive: bool) -> bool:
    """Call before config preparation. True exits; False continues with args.

    With --tasks-in, --resume-task accepts only an exact listed directory name,
    or '.' for the supplied directory itself. Without it, direct resume is left
    to its existing owner. Selection never loads or executes a task.
    """
    directory = getattr(args, 'tasks_in', None)
    select = getattr(args, 'select_task', False)
    if directory is None:
        if select:
            raise ValueError('--select-task requires --tasks-in PATH')
        return False
    if getattr(args, 'task_config', None) or getattr(args, 'write_task_config', None):
        raise ValueError('Use task discovery separately from task configuration read/export')
    if select and args.resume_task is not None:
        raise ValueError('Choose --select-task or --resume-task NAME, not both')
    if select and not interactive:
        raise ValueError('--select-task requires a terminal; use --resume-task NAME with --tasks-in')
    if not select and args.resume_task is None:
        supplied = set(getattr(args, '_explicit_start_destinations', ())) - {'tasks_in'}
        if args.request or args.goal or supplied:
            raise ValueError('Listing is read-only; use --resume-task NAME or --select-task with task changes')
    tasks, skipped = discover_project_tasks(directory)
    root = directory.expanduser().resolve()
    choices = {'.' if task.path == root else task.path.name: task for task in tasks}
    for index, (name, task) in enumerate(choices.items(), 1):
        goal = ' '.join(task.goal.split())
        print_line(f'{index}. {name} | status={task.status} | goal={goal[:240]}' + ('…' if len(goal) > 240 else ''))
        print_line(task.resume_command)
    print_line(f'{len(tasks)} native tasks; {skipped} skipped linked, unsupported or unreadable entries. '
               'Only this directory and immediate children inspected. Recorded status is not quality verification; '
               'partial retains unresolved work.')
    selector = str(args.resume_task) if args.resume_task is not None else None
    if select and choices:
        try:
            selector = input('Task number or exact name (blank cancels): ').strip()
        except EOFError:
            return True
        if selector.isdecimal() and 1 <= int(selector) <= len(choices):
            selector = list(choices)[int(selector) - 1]
    if not selector:
        return True
    if selector not in choices:
        raise ValueError('Task selector must be an exact listed directory name (or .); no paths or recursive search')
    args.resume_task = choices[selector].path
    # Discovery options have been consumed; only the native resume path proceeds.
    args.tasks_in, args.select_task = None, False
    if hasattr(args, '_explicit_start_destinations'):
        args._explicit_start_destinations.difference_update({'tasks_in', 'select_task'})
        args._explicit_start_destinations.add('resume_task')
    return False
