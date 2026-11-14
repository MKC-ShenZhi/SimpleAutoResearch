"""Bounded inspection of installed Python interfaces requested by CodeTask."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from simple_ar.core.budget import BudgetExceededError
from simple_ar.experiment.execution.backend import ExecutionError


_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*$")
_MODULE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_PROBE = """
import contextlib, importlib, inspect, io, json, sys
rows = []
for item in json.loads(sys.argv[1]):
    row = {'module': item['module'], 'status': 'unavailable', 'signatures': {}}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            module = importlib.import_module(item['module'])
        for name in item['symbols']:
            value = vars(module).get(name)
            if value is None:
                continue
            try:
                row['signatures'][name] = str(inspect.signature(value))[:500]
            except (TypeError, ValueError):
                continue
        row['status'] = 'observed'
    except Exception:
        pass
    rows.append(row)
print(json.dumps(rows))
"""


def inspect_dependency_api(
    request: object,
    index: dict[str, Any],
    selected_paths: list[str],
    *,
    python_executable: str,
    workspace_dir: Path,
    timeout_sec: int = 12,
    process_scope: Any | None = None,
) -> dict[str, Any]:
    """Resolve an explicit, limited API request against the configured interpreter.

    Only packages imported by selected project source may be queried. This is
    observational evidence, not permission to change an implementation or its
    environment. Importing a third-party package executes its initialization;
    callers must use the already-authorized project interpreter.
    """

    selected = set(selected_paths)
    imported = {
        name
        for file_row in index.get("files", [])
        if isinstance(file_row, dict) and file_row.get("path") in selected
        for name in (file_row.get("python") or {}).get("imports", [])
        if isinstance(name, str)
    }
    accepted: list[dict[str, Any]] = []
    if isinstance(request, list):
        for item in request[:2]:
            if not isinstance(item, dict):
                continue
            module = item.get("module")
            symbols = item.get("symbols")
            if (not isinstance(module, str) or not _MODULE.fullmatch(module)
                    or module.split(".", 1)[0] not in imported or not isinstance(symbols, list)):
                continue
            names = [
                value for value in symbols[:6]
                if isinstance(value, str) and _IDENTIFIER.fullmatch(value)
                and not value.startswith("_")
            ]
            if names:
                accepted.append({"module": module, "symbols": list(dict.fromkeys(names))})
    if not accepted:
        return {"status": "invalid_request", "interfaces": []}
    try:
        argv = [python_executable, "-I", "-c", _PROBE, json.dumps(accepted)]
        completed = (process_scope.probe(argv, cwd=workspace_dir, timeout_sec=timeout_sec)
            if process_scope is not None else subprocess.run(argv, cwd=workspace_dir,
                capture_output=True, text=True, timeout=timeout_sec, check=False))
        interfaces = json.loads(completed.stdout) if completed.returncode == 0 else None
        if not isinstance(interfaces, list):
            raise ValueError("Dependency probe did not return a JSON list")
    except (OSError, subprocess.TimeoutExpired, ValueError, BudgetExceededError, ExecutionError):
        return {"status": "unavailable", "request": accepted, "interfaces": []}
    return {
        "status": "observed" if any(
            isinstance(row, dict) and row.get("signatures") for row in interfaces
        ) else "unavailable",
        "request": accepted,
        "interfaces": [row for row in interfaces if isinstance(row, dict)][:2],
    }
