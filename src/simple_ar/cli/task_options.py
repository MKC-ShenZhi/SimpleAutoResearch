"""Editable native task inputs; execution and validation stay with the task owner."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import shlex
import sys
import tomllib


FIELDS = {
    'request': str, 'model': str, 'source': list, 'output_root': str,
    'kind': str, 'project': str, 'document': list, 'material': list,
    'extensions': list, 'from_session': str, 'reuse': list,
    'data_file': str, 'data_path': list,
    'project_python': str, 'allow_execution': bool, 'allow_network': bool,
    'allow_vision': bool, 'max_images': int, 'max_turns': int, 'max_tokens': int,
    'max_processes': int, 'max_process_seconds': (int, float),
}
PATHS = {'output_root', 'project_python', 'project', 'data_file', 'from_session'}
PATH_LISTS = {'source', 'document', 'material', 'data_path', 'extensions'}


def shell_path(path: Path) -> str:
    value = str(path)
    return "'" + value.replace("'", "''") + "'" if sys.platform == 'win32' else shlex.quote(value)


def resume_command(path: Path) -> str:
    return 'simple-ar start --resume-task ' + shell_path(path)


def _creation_only(args: argparse.Namespace) -> None:
    unsupported = set(args._explicit_start_destinations) - (FIELDS.keys() | {
        'task_config', 'write_task_config', 'goal', 'yes', 'prepare_only'})
    if unsupported:
        raise ValueError('Native creation configuration cannot preserve: ' + ', '.join(sorted(unsupported)))


def native_process_limits(args: argparse.Namespace) -> dict | None:
    limits = dict(getattr(args, '_process_limits', None) or {})
    for name, dimension in (('max_processes', 'process_invocations'),
                            ('max_process_seconds', 'process_wall_seconds')):
        value = getattr(args, name, None)
        if value is not None:
            if value < 0 or not math.isfinite(value):
                raise ValueError(f'{name} must be finite and nonnegative')
            if dimension in limits and limits[dimension] != value:
                raise ValueError(f'Conflicting {dimension} in task and structured contract')
            limits[dimension] = value
    return limits or None


def validate_resume_options(args: argparse.Namespace) -> None:
    """Reject creation settings instead of silently ignoring them on resume."""
    if not args.resume_task:
        return
    creation = set(getattr(args, '_explicit_start_destinations', ())) & {
        'max_turns', 'max_tokens', 'max_images', 'max_processes',
        'max_process_seconds', 'project_python', 'output_root',
        'extensions', 'from_session', 'reuse',
    }
    creation.update(name for name in ('max_processes', 'max_process_seconds')
                    if getattr(args, name, None) is not None)
    creation.update(name for name in ('max_images', 'project_python') if getattr(args, name, None))
    if creation:
        raise ValueError('Resume retains saved creation settings: ' + ', '.join(sorted(creation))
                         + '; use --authorize-remaining for resource changes, or --supply-task for contract changes')


def apply_task_config(args: argparse.Namespace) -> None:
    path = getattr(args, 'task_config', None)
    if path is None:
        return
    _creation_only(args)
    path = path.expanduser().resolve()
    if path.stat().st_size > 65536:
        raise ValueError('Task configuration must be at most 64 KiB')
    with path.open('rb') as stream:
        document = tomllib.load(stream)
    if set(document) != {'task'} or not isinstance(document['task'], dict):
        raise ValueError('Task configuration requires one [task] table')
    unknown = set(document['task']) - FIELDS.keys()
    if unknown:
        raise ValueError('Unknown task settings: ' + ', '.join(sorted(unknown)))
    explicit = args._explicit_start_destinations
    for name, value in document['task'].items():
        types = FIELDS[name] if isinstance(FIELDS[name], tuple) else (FIELDS[name],)
        if type(value) not in types or isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f'Invalid task setting type: {name}')
        if name == 'kind':
            from simple_ar.cli.start import FUNCTION_LABELS
            if value not in FUNCTION_LABELS:
                raise ValueError('Unknown task outcome preference: ' + value)
        if name == 'reuse' and (any(type(item) is not str or not item.strip() for item in value)
                               or len(set(value)) != len(value)):
            raise ValueError('Task reuse must list distinct nonempty registered result names')
        if name in PATH_LISTS:
            if any(type(item) is not str or not item.strip() for item in value):
                raise ValueError(f'Task {name} must list nonempty file/directory paths')
            value = [(path.parent / Path(item).expanduser()).resolve() for item in value]
        elif name in PATHS:
            candidate = path.parent / Path(value).expanduser()
            # A venv executable's symlink target belongs to the base runtime;
            # resolving it silently selects a different Python environment.
            value = candidate.absolute() if name == 'project_python' else candidate.resolve()
        if name not in explicit and not (name == 'request' and (args.request is not None or args.goal)):
            setattr(args, name, value)
            explicit.add(name)
    args._task_config_source = str(path)
    native_process_limits(args)


def write_task_config(args: argparse.Namespace) -> bool:
    destination = getattr(args, 'write_task_config', None)
    if destination is None:
        return False
    _creation_only(args)
    path = destination.expanduser().absolute()
    limits = native_process_limits(args) or {}
    values = {name: getattr(args, name, None) for name in FIELDS}
    values['request'] = args.request or args.goal or 'Describe your goal and required deliverables here.'
    for name in PATH_LISTS:
        values[name] = [str(Path(item).expanduser().absolute()) for item in getattr(args, name, [])]
    for name in PATHS:
        if values[name] is not None:
            values[name] = str(Path(values[name]).expanduser().absolute())
    values['max_processes'] = limits.get('process_invocations', args.max_turns * 10)
    values['max_process_seconds'] = limits.get('process_wall_seconds', args.max_turns * 300)
    comments = {
        'request': 'Goal, language, length and intended figures belong here; the assistant chooses tools as needed.',
        'model': 'Named connection from your global models.toml; never put credentials in this file.',
        'extensions': 'Explicit trusted local extension.toml files or directories. Relative paths use this config location. Python has host OS privileges; v1 tools declare read_only/plan only.',
        'source': 'Read-only files/directories visible on the execution machine. Relative paths use this file location.',
        'kind': 'Optional outcome preference; tools combine according to the goal, without a fixed pipeline.',
        'project': 'Existing read-only project directory; omit to create new code from the goal.',
        'document': 'Local original-source files/directories; paths are relative to this configuration.',
        'material': 'Local notes, prior drafts or result packages; not independently verified evidence.',
        'from_session': 'Previous task with current byte-checked delivered materials; creates new work, not a resume or inherited budget.',
        'reuse': 'Named groups from results SESSION, e.g. artifacts or report. Names are not filesystem paths; pair with from_session.',
        'data_file': 'Existing data file. No column assumptions or experiment execution are implied.',
        'data_path': 'Additional read-only data files/directories.',
        'project_python': 'Python entry path, preserving a virtualenv symlink; relative paths use this file location. Saved task environments do not change on resume.',
        'allow_execution': 'Allows process proposals only. Each actual command still needs approval.',
        'allow_network': 'Allows public source acquisition; does not authorize installations.',
        'allow_vision': 'Allows sending rendered images to the configured vision connection.',
        'max_images': 'Paid image generation/edit submissions, separate from inspecting images.',
        'max_turns': 'Shared model request allowance, including specialist calls; feedback retains the same ledger.',
        'max_tokens': 'Shared model token allowance; unknown reservations remain recorded.',
        'max_processes': 'Cumulative process invocation cap, including search backend processes; zero disables them.',
        'max_process_seconds': 'Cumulative process wall-time allowance in seconds, not a single-command timeout.',
    }
    lines = ['# Review this configuration, then run:',
             '# simple-ar start --task-config ' + shell_path(path),
             '# Explicit command-line values override this file. Starting again creates a NEW task.',
             '# For feedback, use the saved task resume command instead.', '[task]']
    for name, value in values.items():
        if value is None:
            continue
        if name in comments:
            lines.append('# ' + comments[name])
        lines.append(name + ' = ' + json.dumps(value, ensure_ascii=False, allow_nan=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write('\n'.join(lines) + '\n')
    from simple_ar.core.console import print_line
    print_line(f'Task configuration saved: {path}\nReview/edit it, then run: simple-ar start --task-config {shell_path(path)}')
    return True
