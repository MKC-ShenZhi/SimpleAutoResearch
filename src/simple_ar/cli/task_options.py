"""Editable native task inputs; execution and validation stay with the task owner."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shlex
import sys

from simple_ar.app.task_creation import (FIELDS, PATH_LISTS, PATHS, native_process_limits,
    apply_task_config as _apply_task_config)


def shell_path(path: Path) -> str:
    value = str(path)
    return "'" + value.replace("'", "''") + "'" if sys.platform == 'win32' else shlex.quote(value)


def resume_command(path: Path) -> str:
    return 'simple-ar start --resume-task ' + shell_path(path)


def _creation_only(args: argparse.Namespace) -> None:
    unsupported = set(args._explicit_start_destinations) - (FIELDS.keys() | {
        'task_config', 'write_task_config', 'goal', 'yes', 'prepare_only', 'interactive'})
    if unsupported:
        raise ValueError('Native creation configuration cannot preserve: ' + ', '.join(sorted(unsupported)))


def validate_interactive_options(args: argparse.Namespace) -> None:
    """CLI presentation mode, never a saved task setting or new authority."""
    if not getattr(args, 'interactive', False):
        return
    if not sys.stdin.isatty():
        raise ValueError('--interactive requires a terminal; omit it for the existing one-shot entry')
    separate = ('chat', 'resume_setup', 'prepare_only', 'write_task_config',
                'retry_model', 'switch_model', 'switch_id', 'switch_reason', 'supply_task',
                'authorize_remaining', 'authorization_id', 'authorization_reason',
                'additional_attempts', 'additional_no_progress',
                'decision_id', 'decision_response', 'decision_guidance', 'allow', 'validate')
    conflicts = [name for name in separate if getattr(args, name, None)]
    if getattr(args, 'resume_task', None):
        conflicts.extend(name for name in ('source', 'project', 'document', 'material', 'data_file',
            'data_path', 'allow_execution', 'allow_network', 'allow_vision') if getattr(args, name, None))
    if conflicts or hasattr(args, '_setup_state'):
        raise ValueError('--interactive is for ordinary native feedback; use structured setup, '
                         'retry, access, contract or resource changes separately: ' + ', '.join(conflicts))
    if (getattr(args, 'request', None) or getattr(args, 'goal', None) or '').lstrip().startswith('/'):
        raise ValueError('Use slash commands at the --interactive prompt, not as positional feedback')


def validate_resume_options(args: argparse.Namespace) -> None:
    """Reject creation settings instead of silently ignoring them on resume."""
    if not args.resume_task:
        return
    creation = set(getattr(args, '_explicit_start_destinations', ())) & {
        'max_turns', 'max_tokens', 'max_images', 'max_processes',
        'max_process_seconds', 'project_python', 'output_root', 'sandbox_execution',
        'extensions', 'from_session', 'reuse', 'reuse_version', 'independent_review_required',
    }
    creation.update(name for name in ('max_processes', 'max_process_seconds')
                    if getattr(args, name, None) is not None)
    creation.update(name for name in ('max_images', 'project_python') if getattr(args, name, None))
    if getattr(args, 'independent_review_required', None) is not None and (args.request or args.goal):
        creation.discard('independent_review_required')
    if creation:
        raise ValueError('Resume retains saved creation settings: ' + ', '.join(sorted(creation))
                         + '; use --authorize-remaining for resource changes, or --supply-task for contract changes')


def apply_task_config(args: argparse.Namespace) -> str | None:
    if args.task_config is None:
        return None
    _creation_only(args)
    return _apply_task_config(args)


def write_task_config(args: argparse.Namespace) -> bool:
    destination = getattr(args, 'write_task_config', None)
    if destination is None:
        return False
    _creation_only(args)
    path = destination.expanduser().absolute()
    from simple_ar.app.tool_application import ToolApplication

    limits = ToolApplication.creation_limits(args.max_turns, args.max_tokens, args.max_images,
        process_limits=native_process_limits(args))
    values = {name: getattr(args, name, None) for name in FIELDS}
    values['request'] = args.request or args.goal or 'Describe your goal and required deliverables here.'
    for name in PATH_LISTS:
        values[name] = [str(Path(item).expanduser().absolute()) for item in getattr(args, name, [])]
    for name in PATHS:
        if values[name] is not None:
            values[name] = str(Path(values[name]).expanduser().absolute())
    values['max_processes'] = limits['process_invocations']
    values['max_process_seconds'] = limits['process_wall_seconds']
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
        'reuse_version': 'Explicit retained delivery version from results SESSION; portable input bytes for a new task, independent of current completion.',
        'data_file': 'Existing data file. No column assumptions or experiment execution are implied.',
        'data_path': 'Additional read-only data files/directories.',
        'project_python': 'Python entry path, preserving a virtualenv symlink; relative paths use this file location. Saved task environments do not change on resume.',
        'allow_execution': 'Allows process proposals only. Each actual command still needs approval.',
        'sandbox_execution': 'With allow_execution, run ordinary work and rendering in the task workspace using Linux bubblewrap. Processes cannot access original sources or the network.',
        'allow_network': 'Allows public source acquisition; does not authorize installations.',
        'allow_vision': 'Allows sending rendered images to the configured vision connection.',
        'independent_review_required': 'Require an independent review of a complete delivery; partial results remain available.',
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
