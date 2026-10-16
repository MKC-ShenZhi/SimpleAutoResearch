from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, write_json
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.execution.summary import write_code_task_summary


DEPENDENCY_FILE_NAMES = {
    "environment.yaml",
    "environment.yml",
    "Pipfile",
    "poetry.lock",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
}
SUPPORTED_ENV_MODES = {"current", "external"}


def project_command(argv: Sequence[str], origin: Path, candidate: Path, *, python_executable: str | None = None) -> list[str]:
    """Relocate whole project paths, never shell text or arbitrary substrings.

    External inputs and relative argv retain their declared meaning. The
    actual command remains visible in the approval and physical receipt.
    """
    origin = Path(os.path.abspath(origin))
    result = []
    for index, arg in enumerate(argv):
        if index == 0 and python_executable and arg in {'python', 'python3', python_executable}:
            result.append(python_executable)
            continue
        key, separator, value = arg.partition('=') if arg.startswith('-') else ('', '', arg)
        path = Path(value)
        if path.is_absolute():
            path = Path(os.path.abspath(path))  # Normalize '..' without following/hiding links.
            if path.is_relative_to(origin):
                value = str(candidate / path.relative_to(origin))
        result.append(key + separator + value)
    return result


def native_validation_command(manifest: Mapping[str, Any], workspace: Path) -> list[str]:
    """Materialize only the relocation rule in a saved scientific approval."""
    bound = manifest['code_task']['native_binding']['binding']
    argv = list(bound['contract']['validation_command'])
    measurement = bound.get('measurement_contract')
    if measurement is not None and bound['contract'].get('project_path'):
        from simple_ar.experiment.execution.measurement import representative_measurement_execution
        measurement = representative_measurement_execution(measurement)
        argv = project_command(argv, Path(measurement['cwd']), workspace,
            python_executable=bound['python_executable'])
    return argv


@dataclass(frozen=True)
class CodeTaskEnvironmentResult:
    """Result returned after probing a copied code-task workspace.

    Args:
        run_dir: Code-task run directory.
        report_path: Path to ``code_task/meta/environment_report.json``.
        status: ``ok`` or ``warning`` depending on probe findings.
        warnings: Non-fatal limitations discovered during probing.
        tools: Tool availability mapping from the environment report.
        gpu: GPU availability summary from the environment report.
    """

    run_dir: Path
    report_path: Path
    status: str
    warnings: tuple[str, ...] = field(default_factory=tuple)
    tools: dict[str, Any] = field(default_factory=dict)
    gpu: dict[str, Any] = field(default_factory=dict)


def probe_code_task_environment(
    run_dir: Path,
    *,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
) -> CodeTaskEnvironmentResult:
    """Probe runtime and project environment for a code-task workspace.

    The probe is intentionally observational: it does not install dependencies,
    import project modules, run tests, or mutate the copied workspace. The report
    gives later planning and benchmark stages a stable view of the local
    constraints they should respect.

    Args:
        run_dir: Code-task run directory.
        env_mode: Optional execution environment mode to record before the
            probe. Supported now: ``current`` and ``external``.
        python_executable: External interpreter path or executable name when
            ``env_mode`` is ``external``.

    Returns:
        Paths and compact status for the generated environment report.

    Raises:
        FileNotFoundError: If the run is not a code-task run or the workspace is
            missing.
    """
    return _probe_code_task_environment(
        run_dir,
        env_mode=env_mode,
        python_executable=python_executable,
    )


def ensure_code_task_environment_policy(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Return a normalized environment policy and persist it if needed."""
    policy = _resolved_environment_policy(
        manifest,
        env_mode=env_mode,
        python_executable=python_executable,
    )
    environment = manifest_section(manifest, "environment")
    if environment.get("policy") != policy:
        environment.setdefault("status", "not_probed")
        environment["policy"] = policy
        manifest["environment"] = environment
        save_code_task_manifest(run_dir, manifest)
    return policy


def build_code_task_environment_policy(
    *,
    env_mode: str = "current",
    python_executable: str | Path | None = None,
) -> dict[str, Any]:
    """Build the initial execution environment policy for a code-task run."""
    mode = _normalize_env_mode(env_mode)
    executable = _resolve_policy_python(mode, python_executable)
    return {
        "schema_version": 1,
        "mode": mode,
        "python_executable": executable,
        "python_version": _python_version_for_policy(mode, executable),
        "allow_dependency_install": False,
        "dependency_install": "disabled",
        "managed": False,
        "created_at": utcnow_iso(),
        "notes": [
            "This policy selects the interpreter used for benchmark commands.",
            "It does not create environments or install dependencies.",
        ],
    }


def policy_python_executable(environment_policy: Mapping[str, Any]) -> str:
    """Return the interpreter selected by a persisted CodeTask policy."""
    executable = environment_policy.get("python_executable")
    if isinstance(executable, str) and executable:
        return executable
    return sys.executable


def apply_code_task_environment_policy(
    command: Sequence[str], environment_policy: Mapping[str, Any]
) -> list[str]:
    """Resolve only the supported bare Python command in an argv list."""
    args = list(command)
    if args and args[0] in {"python", "python3"}:
        args[0] = policy_python_executable(environment_policy)
    return args


def resolve_code_task_command(
    command: Sequence[str], *, env_mode: str = "current",
    python_executable: str | Path | None = None,
) -> list[str]:
    """Resolve a research command without probing or launching an interpreter.

    Full environment policies include a version probe and therefore belong to
    initialization/probe boundaries.  Request construction only needs the
    already-supported argv rewrite for a bare Python command.
    """
    args = list(command)
    if not args or args[0] not in {"python", "python3"}:
        return args
    mode = _normalize_env_mode(env_mode)
    executable = _resolve_policy_python(mode, python_executable)
    return apply_code_task_environment_policy(
        args, {"python_executable": executable}
    )


def _probe_code_task_environment(
    run_dir: Path,
    *,
    env_mode: str | None = None,
    python_executable: str | Path | None = None,
) -> CodeTaskEnvironmentResult:
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    if not paths.workspace_dir.is_dir():
        raise FileNotFoundError(f"Missing code-task workspace: {paths.workspace_dir}")
    paths.meta_dir.mkdir(parents=True, exist_ok=True)

    policy = ensure_code_task_environment_policy(
        paths.run_dir,
        manifest,
        env_mode=env_mode,
        python_executable=python_executable,
    )
    manifest = load_code_task_manifest(run_dir)
    codebase_index = _load_codebase_index(paths.meta_dir / "codebase_index.json")
    report = _build_environment_report(paths.workspace_dir, manifest, codebase_index, policy)
    report_path = paths.meta_dir / "environment_report.json"
    write_json(report_path, report)
    resource_probe, resource_decision = _resource_artifacts(report)
    write_json(paths.meta_dir / "resource_probe.json", resource_probe)
    write_json(paths.meta_dir / "resource_decision.json", resource_decision)
    _update_manifest_after_probe(run_dir, manifest, report)
    write_code_task_summary(run_dir)
    return CodeTaskEnvironmentResult(
        run_dir=paths.run_dir,
        report_path=report_path,
        status=str(report.get("status", "unknown")),
        warnings=tuple(str(item) for item in report.get("warnings", []) if item),
        tools=report.get("tools", {}) if isinstance(report.get("tools"), dict) else {},
        gpu=report.get("gpu", {}) if isinstance(report.get("gpu"), dict) else {},
    )


def _build_environment_report(
    workspace_dir: Path,
    manifest: dict[str, Any],
    codebase_index: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    warnings: list[str] = []
    if not codebase_index:
        warnings.append("codebase_index_missing")
    benchmark = manifest.get("benchmark", {})
    if not (isinstance(benchmark, dict) and benchmark.get("command")):
        warnings.append("benchmark_command_missing")

    gpu = _gpu_summary(warnings)
    project = _project_summary(workspace_dir, codebase_index)
    if not project["dependency_files"]:
        warnings.append("dependency_file_not_found")
    if not project["test_dirs"]:
        warnings.append("test_directory_not_found")

    return {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "status": "warning" if warnings else "ok",
        "platform": _platform_summary(),
        "python": _python_summary(),
        "execution_policy": policy,
        "tools": _tools_summary(),
        "python_modules": _python_modules_summary(),
        "execution_context": _execution_context_summary(),
        "gpu": gpu,
        "project": project,
        "warnings": warnings,
        "notes": [
            "This probe is observational. It does not install dependencies or run project code.",
            "Missing tools or files are recorded as planning constraints, not automatic failures.",
        ],
    }


def _resolved_environment_policy(
    manifest: dict[str, Any],
    *,
    env_mode: str | None,
    python_executable: str | Path | None,
) -> dict[str, Any]:
    environment = manifest_section(manifest, "environment")
    existing = environment.get("policy")
    existing_policy = existing if isinstance(existing, dict) else {}
    if env_mode is None and python_executable is None and _policy_is_supported(existing_policy):
        return dict(existing_policy)
    mode = _normalize_env_mode(env_mode or str(existing_policy.get("mode") or "current"))
    executable = python_executable
    if (
        executable is None
        and mode == "external"
        and existing_policy.get("mode") == "external"
    ):
        executable = existing_policy.get("python_executable")
    return build_code_task_environment_policy(
        env_mode=mode,
        python_executable=executable,
    )


def _policy_is_supported(policy: dict[str, Any]) -> bool:
    mode = policy.get("mode")
    executable = policy.get("python_executable")
    return isinstance(mode, str) and mode in SUPPORTED_ENV_MODES and bool(executable)


def _normalize_env_mode(value: str) -> str:
    mode = value.strip().lower().replace("_", "-")
    if mode not in SUPPORTED_ENV_MODES:
        raise ValueError(
            "env_mode must be one of: " + ", ".join(sorted(SUPPORTED_ENV_MODES))
        )
    return mode


def _resolve_policy_python(mode: str, python_executable: str | Path | None) -> str:
    if mode == "current":
        return sys.executable
    if mode == "external":
        if python_executable is None or str(python_executable).strip() == "":
            raise ValueError("--python is required when --env-mode external is used")
        return _resolve_external_python(str(python_executable).strip())
    raise ValueError(f"Unsupported env_mode: {mode}")


def _resolve_external_python(value: str) -> str:
    has_path_separator = any(sep and sep in value for sep in (os.sep, os.altsep))
    candidate = Path(value).expanduser()
    if candidate.is_absolute() or has_path_separator:
        # A virtualenv's bin/python is commonly a symlink to the base Python.
        # realpath/Path.resolve() would discard the virtualenv entrypoint and
        # silently run the base interpreter without its installed packages.
        path = Path(os.path.abspath(candidate))
        if not path.exists():
            raise FileNotFoundError(f"External Python executable does not exist: {path}")
        if path.is_dir():
            raise ValueError(f"External Python path is a directory: {path}")
        return str(path)
    resolved = shutil.which(value)
    if not resolved:
        raise FileNotFoundError(f"External Python executable not found on PATH: {value}")
    return os.path.abspath(resolved)


def _python_version_for_policy(mode: str, executable: str) -> str:
    if mode == "current" and executable == sys.executable:
        return platform.python_version()
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (completed.stdout or completed.stderr).strip()
    return text.replace("Python ", "", 1) if text else "unknown"


def _platform_summary() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }


def _python_summary() -> dict[str, Any]:
    return {
        "executable": sys.executable,
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "prefix": sys.prefix,
        "base_prefix": sys.base_prefix,
        "in_virtualenv": sys.prefix != sys.base_prefix,
        "conda_prefix": os.environ.get("CONDA_PREFIX"),
    }


def _tools_summary() -> dict[str, Any]:
    tools = {
        "python": {"available": True, "path": sys.executable},
    }
    for name in ("uv", "pip", "pytest", "git", "docker", "conda", "nvidia-smi"):
        tools[name] = _tool_summary(name)
    return tools


def _tool_summary(name: str) -> dict[str, Any]:
    path = shutil.which(name)
    return {
        "available": path is not None,
        "path": path,
    }


def _python_modules_summary() -> dict[str, bool]:
    return {
        name: importlib.util.find_spec(name) is not None
        for name in ("pip", "pytest", "numpy", "pandas", "sklearn")
    }


def _execution_context_summary() -> dict[str, Any]:
    release = platform.release().lower()
    return {
        "inside_docker": Path("/.dockerenv").exists() or os.environ.get("container") is not None,
        "inside_wsl": "microsoft" in release or bool(os.environ.get("WSL_DISTRO_NAME")),
        "cwd": str(Path.cwd()),
    }


def _gpu_summary(warnings: list[str]) -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {
            "available": False,
            "count": 0,
            "backend": None,
            "devices": [],
            "probe": "nvidia-smi_not_found",
        }
    try:
        completed = subprocess.run(
            [
                nvidia_smi,
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append("gpu_probe_failed")
        return {
            "available": False,
            "count": 0,
            "backend": "nvidia-smi",
            "devices": [],
            "probe": "failed",
            "error": str(exc),
        }
    if completed.returncode != 0:
        warnings.append("gpu_probe_failed")
        return {
            "available": False,
            "count": 0,
            "backend": "nvidia-smi",
            "devices": [],
            "probe": "failed",
            "stderr": completed.stderr.strip(),
        }
    devices = [_gpu_device(line) for line in completed.stdout.splitlines() if line.strip()]
    return {
        "available": bool(devices),
        "count": len(devices),
        "backend": "nvidia-smi",
        "devices": devices,
        "probe": "ok",
    }


def _gpu_device(line: str) -> dict[str, Any]:
    parts = [part.strip() for part in line.split(",", maxsplit=1)]
    memory_mib: int | None = None
    if len(parts) > 1:
        try:
            memory_mib = int(float(parts[1]))
        except ValueError:
            memory_mib = None
    return {
        "name": parts[0],
        "memory_total_mib": memory_mib,
    }


def _project_summary(workspace_dir: Path, codebase_index: dict[str, Any]) -> dict[str, Any]:
    project = codebase_index.get("project", {}) if isinstance(codebase_index, dict) else {}
    return {
        "workspace": str(workspace_dir),
        "dependency_files": _dependency_files(workspace_dir),
        "test_dirs": _test_dirs(workspace_dir, codebase_index),
        "entrypoint_candidates": _list_value(project.get("entrypoint_candidates")),
        "top_level_entries": _list_value(project.get("top_level_entries")),
        "file_count": int(project.get("file_count", 0)) if isinstance(project, dict) else 0,
        "python_file_count": (
            int(project.get("python_file_count", 0)) if isinstance(project, dict) else 0
        ),
        "test_file_count": int(project.get("test_file_count", 0)) if isinstance(project, dict) else 0,
        "total_bytes": int(project.get("total_bytes", 0)) if isinstance(project, dict) else 0,
    }


def _dependency_files(workspace_dir: Path) -> list[str]:
    found: list[str] = []
    for path in workspace_dir.iterdir():
        if path.is_file() and path.name in DEPENDENCY_FILE_NAMES:
            found.append(path.name)
    return sorted(found)


def _test_dirs(workspace_dir: Path, codebase_index: dict[str, Any]) -> list[str]:
    dirs = {
        path.name
        for path in workspace_dir.iterdir()
        if path.is_dir() and path.name.lower() in {"test", "tests"}
    }
    files = codebase_index.get("files", []) if isinstance(codebase_index, dict) else []
    if isinstance(files, list):
        for item in files:
            if not isinstance(item, dict):
                continue
            role_tags = item.get("role_tags", [])
            if not (isinstance(role_tags, list) and "test" in role_tags):
                continue
            path = Path(str(item.get("path", "")))
            if path.parts:
                dirs.add(path.parts[0])
    return sorted(dirs)


def _list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _load_codebase_index(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def _update_manifest_after_probe(
    run_dir: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
) -> None:
    layout = manifest_section(manifest, "layout")
    layout["environment_report"] = "code_task/meta/environment_report.json"
    layout["resource_probe"] = "code_task/meta/resource_probe.json"
    layout["resource_decision"] = "code_task/meta/resource_decision.json"
    manifest["layout"] = layout
    environment = manifest_section(manifest, "environment")
    environment.update(
        {
            "status": report.get("status", "unknown"),
            "probed_at": report.get("generated_at"),
            "report": "code_task/meta/environment_report.json",
            "policy": report.get("execution_policy", environment.get("policy", {})),
            "platform": report.get("platform", {}),
            "python": report.get("python", {}),
            "gpu": report.get("gpu", {}),
            "warnings": report.get("warnings", []),
        }
    )
    environment["resource_probe"] = "code_task/meta/resource_probe.json"
    environment["resource_decision"] = "code_task/meta/resource_decision.json"
    manifest["environment"] = environment
    if manifest.get("status") == "initialized":
        manifest["status"] = "environment_probed"
    save_code_task_manifest(run_dir, manifest)


def _resource_artifacts(report: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    gpu = report.get("gpu", {}) if isinstance(report.get("gpu"), dict) else {}
    platform_info = report.get("platform", {}) if isinstance(report.get("platform"), dict) else {}
    memory_total_mib = _system_memory_total_mib()
    probe = {
        "schema_version": "code_task_resource_probe.v1",
        "generated_at": report.get("generated_at"),
        "cpu": {
            "logical_count": os.cpu_count() or 0,
            "machine": platform_info.get("machine", ""),
            "processor": platform_info.get("processor", ""),
        },
        "memory": {
            "total_mib": memory_total_mib,
            "probe": "available" if memory_total_mib is not None else "unavailable",
        },
        "gpu": gpu,
        "tools": {
            "nvidia_smi": _tool_available(report, "nvidia-smi"),
            "uv": _tool_available(report, "uv"),
            "pytest": _tool_available(report, "pytest"),
        },
        "python_modules": report.get("python_modules", {}),
    }
    gpu_devices = gpu.get("devices", []) if isinstance(gpu.get("devices"), list) else []
    max_gpu_memory = 0
    for device in gpu_devices:
        if isinstance(device, dict) and isinstance(device.get("memory_total_mib"), int):
            max_gpu_memory = max(max_gpu_memory, int(device["memory_total_mib"]))
    allow_gpu = bool(gpu.get("available")) and max_gpu_memory > 0
    if allow_gpu and max_gpu_memory >= 24_000:
        profile = "gpu_large"
    elif allow_gpu:
        profile = "gpu_medium"
    else:
        profile = "local_cpu"
    decision = {
        "schema_version": "code_task_resource_decision.v1",
        "generated_at": report.get("generated_at"),
        "profile": profile,
        "allow_gpu": allow_gpu,
        "recommended": {
            "prefer_gpu": allow_gpu,
            "max_parallel_jobs": _recommended_parallel_jobs(profile),
            "timeout_multiplier": 2.0 if profile == "local_cpu" else 1.0,
            "notes": _resource_notes(profile, max_gpu_memory=max_gpu_memory),
        },
        "limits": {
            "observed_memory_total_mib": memory_total_mib,
            "observed_max_gpu_memory_mib": max_gpu_memory,
        },
    }
    return probe, decision


def _tool_available(report: dict[str, Any], name: str) -> bool:
    tools = report.get("tools", {})
    if not isinstance(tools, dict):
        return False
    row = tools.get(name)
    return bool(isinstance(row, dict) and row.get("available"))


def _recommended_parallel_jobs(profile: str) -> int:
    if profile == "gpu_large":
        return 4
    if profile == "gpu_medium":
        return 2
    return 1


def _resource_notes(profile: str, *, max_gpu_memory: int) -> list[str]:
    if profile == "gpu_large":
        return ["GPU is available with enough memory for moderate local ML experiments."]
    if profile == "gpu_medium":
        return [f"GPU is available with about {max_gpu_memory} MiB memory; keep batch sizes conservative."]
    return ["No usable NVIDIA GPU was detected; prefer CPU-friendly smoke tests and small local datasets."]


def _system_memory_total_mib() -> int | None:
    try:
        if platform.system().lower() == "windows":
            completed = subprocess.run(
                ["wmic", "computersystem", "get", "TotalPhysicalMemory", "/value"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            for line in completed.stdout.splitlines():
                if line.startswith("TotalPhysicalMemory="):
                    return int(int(line.split("=", 1)[1].strip()) / (1024 * 1024))
        if hasattr(os, "sysconf"):
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return int((pages * page_size) / (1024 * 1024))
    except Exception:
        return None
    return None
