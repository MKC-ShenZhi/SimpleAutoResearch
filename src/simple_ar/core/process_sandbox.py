"""Filesystem and network boundary for task-authorized Linux processes."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessSandbox:
    workspace: Path
    writable: Path
    python: Path
    extra_read: tuple[Path, ...] = ()
    extra_write: tuple[Path, ...] = ()
    allow_gpu: bool = False


def probe_bwrap_sandbox() -> None:
    """Verify bubblewrap is installed and permitted to create Linux user namespaces."""
    executable = shutil.which('bwrap')
    if executable is None:
        raise ValueError('Workspace execution requires bubblewrap on a Linux execution host')
    probe_bin = shutil.which('true') or '/bin/true'
    try:
        result = subprocess.run(
            [executable, '--unshare-all', '--new-session', '--die-with-parent',
             '--ro-bind', '/usr', '/usr', '--ro-bind-try', '/bin', '/bin',
             '--ro-bind-try', '/lib', '/lib', '--ro-bind-try', '/lib64', '/lib64',
             '--', probe_bin],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f'Workspace execution bubblewrap probe failed: {exc}') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or '').strip().splitlines()
        hint = detail[0] if detail else f'exit code {result.returncode}'
        raise ValueError(
            'Workspace execution bubblewrap cannot create a Linux sandbox in this environment '
            f'({hint}). On dedicated containers where user namespaces are disabled, use '
            '--allow-execution (optionally with --auto-approve-execution) instead.'
        )


def _gpu_device_paths() -> list[str]:
    dev = Path('/dev')
    if not dev.is_dir():
        return []
    paths: list[str] = []
    for entry in sorted(dev.iterdir(), key=lambda p: p.name):
        if entry.name.startswith('nvidia') or entry.name == 'dri':
            paths.append(str(entry))
    return paths


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
    if sandbox.allow_gpu:
        for device in _gpu_device_paths():
            command += ['--dev-bind-try', device, device]
    for path in ('/etc/alternatives', '/etc/fonts', '/etc/texmf', '/var/lib/texmf', '/var/cache/fontconfig'):
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
    for path in sandbox.extra_write:
        command += ['--bind', str(path), str(path)]
    return [*command, '--chdir', str(cwd), '--', *argv]
