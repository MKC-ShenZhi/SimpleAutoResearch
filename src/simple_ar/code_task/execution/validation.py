from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import write_json
from simple_ar.core.budget import BudgetExceededError
from simple_ar.experiment.execution.backend import ExecutionError
from simple_ar.code_task.editing.attempts import (
    load_latest_code_task_batch,
    update_code_task_batch_state,
)
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    utcnow_iso,
)


RISKY_IMPORTS = {
    "ctypes",
    "ftplib",
    "http",
    "requests",
    "shutil",
    "signal",
    "smtplib",
    "socket",
    "subprocess",
    "urllib",
}

RISKY_CALLS = {
    "__import__",
    "compile",
    "eval",
    "exec",
    "os.popen",
    "os.system",
    "shutil.rmtree",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.run",
}


@dataclass(frozen=True)
class CodeTaskValidationResult:
    """Result returned after validating a code-task workspace.

    Args:
        run_dir: Code-task run directory.
        report_path: Path to ``validation_report.json``.
        status: ``passed`` when no errors were found, otherwise ``failed``.
        error_count: Number of validation errors.
        warning_count: Number of validation warnings.
        issue_count: Total number of issues.
    """

    run_dir: Path
    report_path: Path
    status: str
    error_count: int
    warning_count: int
    issue_count: int


def validate_code_task(
    run_dir: Path,
    *,
    strict: bool = False,
    max_file_bytes: int = 500_000,
    process_scope: Any | None = None,
) -> CodeTaskValidationResult:
    """Validate Python files in a code-task workspace.

    The validator is intentionally lightweight. Syntax errors are errors except
    byte-identical files proven present in the frozen Git-worktree or initial
    syntax-error baseline, which remain visible warnings in non-strict mode. Risky imports/calls become errors
    in strict mode. This keeps ordinary benchmark projects usable while still
    making security-sensitive behavior visible.

    Args:
        run_dir: Code-task run directory.
        strict: Treat risky imports/calls as errors.
        max_file_bytes: Per-file scan budget. Larger files are warned and
            skipped for static analysis.

    Returns:
        Validation summary and report location.

    Raises:
        FileNotFoundError: If the run or workspace is missing.
        RuntimeError: If ``run_dir`` is not a code-task run.
    """
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    if not paths.workspace_dir.is_dir():
        raise FileNotFoundError(f"Missing code-task workspace: {paths.workspace_dir}")

    issues: list[dict[str, Any]] = []
    policy = manifest_section(manifest, "environment").get("policy", {})
    external_python = policy.get("python_executable") if policy.get("mode") == "external" else None
    import_availability = None
    if external_python:
        names: set[str] = set()
        for path in _iter_workspace_files(paths.workspace_dir):
            if path.suffix == ".py" and (max_file_bytes <= 0 or path.stat().st_size <= max_file_bytes):
                try:
                    names.update(name for name, _ in _imports(ast.parse(path.read_bytes(), filename=str(path))))
                except SyntaxError:
                    pass  # Reported by the ordinary syntax pass below.
        import_availability = _external_import_availability(str(external_python), names,
            **({'process_scope': process_scope, 'cwd': paths.workspace_dir} if process_scope is not None else {}))
        if import_availability is None:
            issues.append(_issue(severity="warning", code="dependency_check_unavailable", path="",
                message="Could not inspect the configured external interpreter; dependency availability is unknown."))
            import_availability = {}  # Do not substitute the host environment as evidence.
    scanned_files = 0
    python_files = 0
    for path in _iter_workspace_files(paths.workspace_dir):
        scanned_files += 1
        rel_path = path.relative_to(paths.workspace_dir).as_posix()
        size = path.stat().st_size
        if max_file_bytes > 0 and size > max_file_bytes:
            issues.append(
                _issue(
                    severity="warning",
                    code="file_too_large",
                    path=rel_path,
                    message=f"Skipped static scan because file is larger than {max_file_bytes} bytes.",
                )
            )
            continue
        if path.suffix.lower() != ".py":
            continue
        python_files += 1
        _validate_python_file(
            path,
            rel_path=rel_path,
            workspace_dir=paths.workspace_dir,
            strict=strict,
            issues=issues,
            import_availability=import_availability,
        )
        if not strict and issues and issues[-1]["code"] == "syntax_error" and issues[-1]["path"] == rel_path:
            baseline = _unchanged_git_baseline(path, rel_path, paths.workspace_dir, manifest, max_file_bytes)
            if baseline:
                issues[-1].update(severity="warning", baseline_status="byte_identical_frozen_git_source",
                                  baseline_commit=baseline)
            elif (manifest_section(manifest, "workspace").get("initial_syntax_errors", {}).get(rel_path)
                  == hashlib.sha256(path.read_bytes()).hexdigest()):
                issues[-1].update(severity="warning", baseline_status="byte_identical_initial_syntax_error")

    error_count = sum(1 for item in issues if item["severity"] == "error")
    warning_count = sum(1 for item in issues if item["severity"] == "warning")
    status = "failed" if error_count else "passed"
    report = {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "status": status,
        "strict": strict,
        "max_file_bytes": max_file_bytes,
        "workspace": str(paths.workspace_dir),
        "dependency_interpreter": str(external_python or sys.executable),
        "validation_scope": "static syntax and import discovery; proven unchanged initial syntax defects are warnings unless strict; not runtime or scientific validation",
        "file_count": scanned_files,
        "python_file_count": python_files,
        "issue_count": len(issues),
        "error_count": error_count,
        "warning_count": warning_count,
        "issues": issues,
    }
    report_path = paths.meta_dir / "validation_report.json"
    write_json(report_path, report)
    _update_manifest_after_validation(
        run_dir,
        manifest,
        status=status,
        strict=strict,
        error_count=error_count,
        warning_count=warning_count,
    )
    if _patch_applied(manifest):
        _update_latest_batch_after_validation(run_dir, report_path, status)
    return CodeTaskValidationResult(
        run_dir=paths.run_dir,
        report_path=report_path,
        status=status,
        error_count=error_count,
        warning_count=warning_count,
        issue_count=len(issues),
    )


def _unchanged_git_baseline(path: Path, relative: str, workspace: Path,
                            manifest: dict[str, Any], max_file_bytes: int) -> str:
    """Use existing immutable provenance, never a live source or previous failure.

    Only inspect files that failed parsing. Unknown provenance remains an error;
    neither test directory names nor an equal error message excuse a mutation.
    Git reads are bounded and do not execute repository code or hooks.
    """
    record = manifest_section(manifest, "workspace")
    provenance = record.get("git")
    commit = provenance.get("origin_commit") if isinstance(provenance, dict) else None
    if record.get("selected_mode", record.get("mode")) != "git_worktree" or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return ""
    prefix = str(record.get("project_relative_path") or "")
    parts = [part for part in (prefix + "/" + relative).split("/") if part not in {"", "."}]
    if ".." in parts or any("\\" in part for part in parts):
        return ""
    if not path.resolve().is_relative_to(workspace.resolve()):
        return ""
    object_name = commit + ":" + "/".join(parts)
    limit = max_file_bytes if max_file_bytes > 0 else 500_000
    try:
        size = subprocess.run(["git", "-C", str(workspace), "cat-file", "-s", object_name],
                              capture_output=True, timeout=5, check=False)
        if size.returncode != 0 or int(size.stdout) > limit:
            return ""
        original = subprocess.run(["git", "-C", str(workspace), "show", object_name],
                                  capture_output=True, timeout=5, check=False)
        return commit if original.returncode == 0 and original.stdout == path.read_bytes() else ""
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def _validate_python_file(
    path: Path,
    *,
    rel_path: str,
    workspace_dir: Path,
    strict: bool,
    issues: list[dict[str, Any]],
    import_availability: dict[str, bool] | None = None,
) -> None:
    try:
        # Parse source bytes like Python: honor UTF-8 BOM/PEP 263 cookies and
        # reject invalid encodings rather than replacing characters silently.
        tree = ast.parse(path.read_bytes(), filename=rel_path)
        # AST construction omits compiler checks (future-import placement,
        # break/return/nonlocal scope). Compile without executing or writing pyc.
        compile(tree, rel_path, "exec", dont_inherit=True)
    except SyntaxError as exc:
        issues.append(
            _issue(
                severity="error",
                code="syntax_error",
                path=rel_path,
                line=exc.lineno,
                column=exc.offset,
                message=exc.msg,
            )
        )
        return

    imported_names = _imports(tree)
    for name, line in imported_names:
        if name in RISKY_IMPORTS:
            issues.append(
                _issue(
                    severity="error" if strict else "warning",
                    code="risky_import",
                    path=rel_path,
                    line=line,
                    message=f"Import `{name}` can perform external, destructive, or network operations.",
                )
            )
        if not _import_available(name, workspace_dir, current_file=path, import_availability=import_availability):
            issues.append(
                _issue(
                    severity="warning",
                    code="missing_import",
                    path=rel_path,
                    line=line,
                    message=f"Import `{name}` was not found in the workspace or validation interpreter (see dependency_interpreter).",
                )
            )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        if call_name in RISKY_CALLS:
            issues.append(
                _issue(
                    severity="error" if strict else "warning",
                    code="risky_call",
                    path=rel_path,
                    line=getattr(node, "lineno", None),
                    message=f"Call `{call_name}` may mutate the system or execute dynamic code.",
                )
            )


def _imports(tree: ast.AST) -> list[tuple[str, int | None]]:
    names: list[tuple[str, int | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append((alias.name.split(".", 1)[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            if node.module:
                names.append((node.module.split(".", 1)[0], node.lineno))
    return names


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _import_available(name: str, workspace_dir: Path, *, current_file: Path | None = None,
                      import_availability: dict[str, bool] | None = None) -> bool:
    if not name:
        return True
    if import_availability is None and name in sys.builtin_module_names:
        return True
    stdlib_names = getattr(sys, "stdlib_module_names", set())
    if import_availability is None and name in stdlib_names:
        return True
    roots = [workspace_dir, workspace_dir / "src", workspace_dir / "generated_project"]
    if current_file is not None:
        roots.insert(0, current_file.parent)
        roots.insert(1, current_file.parent.parent)
    seen_roots: set[Path] = set()
    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)
        if (root / f"{name}.py").is_file():
            return True
        if (root / name / "__init__.py").is_file():
            return True
    if import_availability is not None:
        return import_availability.get(name, True)  # Unknown is reported once, not as missing.
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _external_import_availability(executable: str, names: set[str], *,
                                  process_scope: Any | None = None, cwd: Path | None = None) -> dict[str, bool] | None:
    """One bounded stdlib-only probe; never import project or training modules."""
    script = (
        "import importlib.util,json,sys; "
        "names=json.load(sys.stdin); "
        "print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}))"
    )
    try:
        if process_scope is not None:
            scoped_script = script.replace('json.load(sys.stdin)', 'json.loads(sys.argv[1])')
            result = process_scope.probe([executable, '-I', '-c', scoped_script, json.dumps(sorted(names))],
                cwd=cwd, timeout_sec=10)
        else:
            result = subprocess.run([executable, "-I", "-c", script],
                input=json.dumps(sorted(names)), capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) and all(type(v) is bool for v in data.values()) else None
    except (OSError, subprocess.TimeoutExpired, ValueError, BudgetExceededError, ExecutionError):
        return None


def _iter_workspace_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
        ]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            if path.is_file():
                files.append(path)
    files.sort(key=lambda item: item.relative_to(root).as_posix())
    return files


def _issue(
    *,
    severity: str,
    code: str,
    path: str,
    message: str,
    line: int | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "path": path,
        "line": line,
        "column": column,
        "message": message,
    }


def _update_manifest_after_validation(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    status: str,
    strict: bool,
    error_count: int,
    warning_count: int,
) -> None:
    layout = manifest_section(manifest, "layout")
    layout["validation_report"] = "code_task/meta/validation_report.json"
    validation = manifest_section(manifest, "validation")
    validation.update(
        {
            "status": status,
            "generated_at": utcnow_iso(),
            "strict": strict,
            "report": "code_task/meta/validation_report.json",
            "error_count": error_count,
            "warning_count": warning_count,
        }
    )
    manifest["layout"] = layout
    manifest["validation"] = validation
    manifest["status"] = "validated" if status == "passed" else "validation_failed"
    save_code_task_manifest(run_dir, manifest)


def _update_latest_batch_after_validation(run_dir: Path, report_path: Path, status: str) -> None:
    batch = load_latest_code_task_batch(run_dir)
    if batch is None:
        return
    update_code_task_batch_state(
        run_dir,
        batch.batch_state_path,
        state="failed" if status == "failed" else "validating",
        artifacts={"validation_report": _relative_to_run(run_dir, report_path)},
        detail=f"Static validation {status}.",
        extra={"validation_status": status},
    )


def _relative_to_run(run_dir: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(run_dir).resolve()).as_posix()
    except ValueError:
        return str(path)


def _patch_applied(manifest: dict[str, Any]) -> bool:
    patch = manifest.get("patch")
    return isinstance(patch, dict) and patch.get("status") == "applied"
