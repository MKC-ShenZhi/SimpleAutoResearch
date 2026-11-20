"""Terminal navigation for the shared read-only project task listing."""
from __future__ import annotations

import argparse

from simple_ar.cli.task_options import resume_command
from simple_ar.core.console import print_line
from simple_ar.tools.project_results import discover_project_tasks


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
        print_line(resume_command(task.path))
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
