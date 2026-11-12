"""Filesystem and network boundary for task-authorized Linux processes."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessSandbox:
    workspace: Path
    writable: Path
    python: Path
    extra_read: tuple[Path, ...] = ()


def sandbox_command(sandbox: ProcessSandbox, cwd: Path, argv: list[str]) -> list[str]:
    """Wrap an operation without changing its recorded command or task ledger.

    Source mounts are deliberately not made visible to arbitrary processes:
    their ordinary tool access excludes private files, while a bind mount of a
    directory would expose those files. Copy the needed material to workspace.
    """
    executable = shutil.which('bwrap')
    if executable is None:
        raise ValueError('Workspace execution requires bubblewrap on the execution host')
    workspace, writable = sandbox.workspace.resolve(), sandbox.writable.resolve()
    working = cwd.resolve()
    allowed_cwd = working.is_relative_to(writable) or any(
        path.is_dir() and working.is_relative_to(path.resolve()) for path in sandbox.extra_read)
    if not writable.is_relative_to(workspace) or not allowed_cwd:
        raise ValueError('Process working directory is outside its writable task scope')
    command = [executable, '--unshare-all', '--new-session', '--die-with-parent',
        '--ro-bind', '/usr', '/usr']
    for name in ('bin', 'sbin', 'lib', 'lib64'):
        path = Path('/') / name
        if path.is_symlink():
            command += ['--symlink', str(path.readlink()), str(path)]
        elif path.exists():
            command += ['--ro-bind', str(path), str(path)]
    command += ['--dir', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--dir', '/etc']
    for path in ('/etc/fonts', '/etc/texmf', '/var/lib/texmf', '/var/cache/fontconfig'):
        command += ['--ro-bind-try', path, path]
    # Virtualenv and Conda interpreters need their environment, not the entire
    # operator home or task root. System interpreters already live under /usr.
    python = sandbox.python.absolute()
    if not python.is_relative_to('/usr'):
        environment = python.parent.parent
        if environment in (Path('/'), Path('/root'), Path('/home'), Path('/tmp'), Path('/opt')):
            raise ValueError('Task Python must identify a self-contained environment for workspace execution')
        command += ['--ro-bind', str(environment), str(environment)]
    if writable != workspace:
        command += ['--ro-bind', str(workspace), str(workspace)]
    command += ['--bind', str(writable), str(writable)]
    for path in sandbox.extra_read:
        command += ['--ro-bind', str(path), str(path)]
    return [*command, '--chdir', str(cwd), '--', *argv]
