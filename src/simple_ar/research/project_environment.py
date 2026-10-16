"""Opt-in environment preparation and checks through the existing process backend.

The preparation attempt owns the environment and receipts; this is not another
executor, installer service or environment database. Installation is executable
project input, not evidence that an experiment is scientifically valid.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import sys
from typing import Any

from simple_ar.core.capabilities import CapabilityContext, CapabilityResult
from simple_ar.experiment.execution.backend import ExecutionBackend, LocalExecutionBackend


_PYTHON_ALIASES = {"python", "python3", "python.exe", "python3.exe"}


def environment_profile(value: Any, *, project: Path | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"mode", "requirements", "install_project", "python_executable", "timeout_sec", "check_command"}:
        raise ValueError("execution.environment accepts mode, requirements, install_project, python_executable, timeout_sec and check_command only.")
    mode = value.get("mode")
    check = value.get("check_command", [])
    if not isinstance(check, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in check):
        raise ValueError("environment.check_command must be an argv list of nonempty strings, without shell interpretation.")
    if mode not in ("current", "venv") or (mode == "current" and not check):
        raise ValueError('Use mode="venv", or mode="current" with an explicit check_command; otherwise omit the table.')
    if mode == "current" and (value.get("requirements") or value.get("install_project") or "python_executable" in value):
        raise ValueError("Current-environment checks do not install dependencies or override the interpreter; select it in the command itself.")
    requirements = value.get("requirements", [])
    if not isinstance(requirements, (list, tuple)) or any(not isinstance(item, str) or not item.strip() for item in requirements):
        raise ValueError("environment.requirements must list project-relative requirements files.")
    for item in requirements:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts or "\\" in item or item.startswith("-"):
            raise ValueError("Requirements paths must stay inside the named execution project.")
        if project is not None:
            resolved = (project / path).resolve()
            if not resolved.is_relative_to(project.resolve()) or not resolved.is_file():
                raise ValueError(f"Requirements file not found inside the project: {item}")
    install_project = value.get("install_project", False)
    if type(install_project) is not bool:
        raise ValueError("environment.install_project must be a boolean; omission does not install the project.")
    if install_project and project is not None:
        if not any((project / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
            raise ValueError("Project installation requires pyproject.toml, setup.py or setup.cfg in execution.cwd; a declaration does not guarantee a build succeeds.")
    python = value.get("python_executable", sys.executable)
    if not isinstance(python, str) or not python.strip():
        raise ValueError("environment.python_executable must be an executable name/path.")
    timeout = value.get("timeout_sec", 300)
    if type(timeout) is not int or timeout < 1:
        raise ValueError("environment.timeout_sec must be positive.")
    return {"mode": mode, "requirements": list(dict.fromkeys(requirements)), "install_project": install_project,
            **({"python_executable": python} if mode == "venv" else {}), "timeout_sec": timeout,
            **({"check_command": list(check)} if check else {})}


def project_environment_plan(profile: Any, project: Path, directory: Path,
                             command: list[str], *, input_project: Path | None = None) -> dict[str, Any]:
    """Project literal preparation steps and the future command without writes."""
    profile = environment_profile(profile, project=input_project or project)
    isolated = profile["mode"] == "venv"
    formal = list(command)
    # Explicit binaries and non-Python launchers remain the caller's protocol.
    if isolated and (not formal or formal[0] not in _PYTHON_ALIASES):
        raise ValueError("Task venv requires the command's first argument to be python/python3; explicit interpreters and non-Python launchers are not silently replaced.")
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    steps = [[profile["python_executable"], "-m", "venv", str(directory)]] if isolated else []
    if isolated and (profile["requirements"] or profile["install_project"]):
        steps.append([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                      *[arg for path in profile["requirements"] for arg in ("-r", str(project / path))],
                      *([str(project)] if profile["install_project"] else [])])
    if isolated:
        steps.append([str(python), "-m", "pip", "check"])
        formal[0] = str(python)
    if profile.get("check_command"):
        check = list(profile["check_command"])
        if isolated and check[0] in _PYTHON_ALIASES:
            check[0] = str(python)
        if check == formal:
            raise ValueError('Preparation check must differ from the formal command; record scientific measurement separately')
        steps.append(check)
    return {"profile": profile, "steps": steps, "command": formal,
            "python_executable": str(python) if isolated else None}


def project_environment_process_env(profile: Mapping[str, Any],
                                    environment: Mapping[str, str] | None) -> Mapping[str, str] | None:
    """Keep current env identity; isolate venv search paths without losing indexes."""
    if profile["mode"] == "current":
        return environment
    environment = dict(os.environ if environment is None else environment)
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


def environment_executable(argv: list[str], project: Path, environment: Mapping[str, str] | None) -> Path:
    """Resolve an approved process executable using its actual cwd and PATH."""
    value = argv[0]
    path = Path(value)
    if path.is_absolute() or '/' in value or '\\' in value:
        path = path if path.is_absolute() else project / path
    else:
        search = (os.environ if environment is None else environment).get('PATH', '')
        search = os.pathsep.join(str(Path(part) if Path(part).is_absolute() else project / part)
            for part in search.split(os.pathsep))
        found = shutil.which(value, path=search)
        if found is None:
            raise ValueError('Executable is unavailable in the approved process PATH')
        path = Path(found)
    if not path.is_file():
        raise ValueError('Approved executable is unavailable')
    return path.absolute()


def environment_preparation_identity(plan: Mapping[str, Any], project: Path,
                                     environment: Mapping[str, str] | None) -> dict[str, Any]:
    """Bind existing executables; the task-local venv binary does not exist yet."""
    def digest(path: Path) -> str:
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()
    base = plan['profile'].get('python_executable')
    executable = environment_executable([base] if base else plan['steps'][0], project, environment)
    executables = {}
    for argv in plan['steps']:
        if argv[0] != plan['python_executable']:
            path = environment_executable(argv, project, environment)
            executables[str(path)] = digest(path)
    return {**plan, 'base_executable': str(executable), 'base_executable_sha256': digest(executable),
        'executables': executables}


def verify_environment_preparation(store: Any, binding: Mapping[str, Any], *,
                                    ledger: Any, session_id: str, attempt_id: str) -> tuple[set[str], list[Path]]:
    """Reconcile one preparation's actual steps, never a second execution state."""
    from simple_ar.core.process import verify_process_accounting
    plan = binding['environment_preparation']
    setup, prepared = store.read_json('environment_setup.json'), store.read_json('execution.json')
    observed = store.read_json('environment_observation.json')
    if (setup['profile'] != plan['profile'] or len(setup['steps']) != len(plan['steps'])
            or prepared['schema_version'] != 'prepared_execution.v1' or prepared['workspace'] != binding['cwd']
            or any(prepared['execution'].get(key) != binding[key] for key in ('cwd', 'result_schema', 'protocol'))
            or prepared['execution']['command'] != binding['argv']
            or observed['argv'] != binding['argv'] or not observed['executable_sha256']):
        raise ValueError('Prepared environment disagrees with its approved plan and actual formal execution')
    required = [store.root / name for name in ('execution.json', 'environment_setup.json', 'environment_observation.json')]
    invocations = set()
    for argv, step in zip(plan['steps'], setup['steps'], strict=True):
        process = step['process_record']
        if process['invocation_id'] in invocations:
            raise ValueError('Each preparation step requires its own physical invocation')
        invocations.add(process['invocation_id'])
        path = store.root / 'environment_processes' / process['invocation_id'] / 'invocation.json'
        required.append(path)
        expected = [arg.replace('{output_dir}', str(path.parent / 'outputs')) for arg in argv]
        if (store.read_json(path) != process or process['argv'] != expected or process['cwd'] != binding['cwd']
                or process.get('returncode') != 0 or process.get('stop_reason') or step['status'] != 'passed'
                or step['command'] != expected or step['cwd'] != binding['cwd']
                or step['duration_sec'] != process['duration_sec']):
            raise ValueError('Preparation step lacks its approved successful physical invocation')
        verify_process_accounting(process, ledger, session_id=session_id,
            attempt_id=attempt_id, timeout_sec=plan['profile']['timeout_sec'])
    return invocations, required


def prepare_project_environment(*, context: CapabilityContext, request: Any,
                                backend: ExecutionBackend | None = None,
                                prepared_payload: Mapping[str, Any] | None = None,
                                record_runtime: bool = False) -> CapabilityResult:
    config = dict(request.execution)
    if request.run is None or "dataset" in config or config.get("pairs"):
        raise ValueError("Environment preparation requires one declared project command.")
    project = request.run.cwd.resolve()
    if not project.is_dir():
        raise ValueError(f"Execution project not found: {project}")
    formal = list(config["command"]) if "code_task" in config else list(request.run.command)
    before = {}
    if "code_task" in config:
        from simple_ar.app.research_execution import code_task_validation
        from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
        from simple_ar.experiment.execution.measurement import snapshot_protocol_assets
        task = dict(config["code_task"])
        run_dir = Path(task["run_dir"])
        if prepared_payload is None or code_task_paths(run_dir).workspace_dir.resolve() != project:
            raise ValueError("Environment installation requires the initialized isolated CodeTask workspace.")
        # This owner prepares a process environment, not a research protocol.
        # Structured reproduction validates its scientific contract before it
        # gets here; ordinary code tasks need the same explicit checker only.
        validator, _ = code_task_validation(config)
        if validator is None:
            raise ValueError('Candidate environment preparation requires an explicit CodeTask checker')
        manifest = load_code_task_manifest(run_dir)
        # Reuse the protocol's named assets. Only the actual Python checker/
        # formal entry and accepted source config are added; never hash a tree.
        locators = [argv[1] for argv in (formal, task["validation_command"])
                    if len(argv) > 1 and argv[1].endswith('.py')]
        conditions = (config.get("protocol") or {}).get("comparison_conditions", {})
        source_config = conditions.get("source_config") if isinstance(conditions, Mapping) else None
        if isinstance(source_config, str) and source_config.strip():
            locators.append(source_config)
        assets = [{"asset_id": f"entry:{locator}", "path": locator}
                  for locator in dict.fromkeys(locators) if (project / locator).is_file()]
        before = snapshot_protocol_assets(config.get("protocol"), project)
        before.update(snapshot_protocol_assets({"protected_assets": assets}, project))
    directory = context.store.root / "environment"
    plan = project_environment_plan(config["environment"], project=project, directory=directory, command=formal)
    profile = plan["profile"]
    isolated = profile["mode"] == "venv"
    if directory.exists() or (context.store.root / "environment_setup.json").exists():
        raise ValueError("Preparation environment already exists; use its recorded result or an explicit new attempt, not an in-place replay.")
    python = Path(plan["python_executable"]) if isolated else None
    steps = plan["steps"]
    observed = []
    refs = []
    environment = project_environment_process_env(profile, request.run.env)
    selected_backend = backend or LocalExecutionBackend()
    limitations = [("Task venv; not an OS sandbox. Approved installation may run build code, access package indexes and write build metadata in the execution workspace." if isolated else "Current environment; no dependency installation or interpreter replacement. Checks execute project code, not in an OS sandbox."),
                   "Successful installation and pip check do not prove project imports, binary/GPU compatibility, data splits or scientific validity.",
                   "A check proves only its observed exit status; its outputs are preparation records, not scientific measurements. No automatic shortening or retry of the scientific command.",
                   "No automatic data download, framework-directed project-code edits or changes to the scientific command's arguments.",
                   *(["Only declared protocol assets and execution/checker entries are content-checked; CodeTask data/edit protections are not an OS sandbox for installation build code."] if "code_task" in config else [])]
    for number, argv in enumerate(steps, 1):
        # Allocate before launch. An interrupted attempt retains this unknown
        # result; normal completed recovery uses the existing prepared_execution.
        observed.append({"command": argv, "status": "allocated_result_unknown"})
        context.store.write_json("environment_setup.json", {"profile": profile, "steps": observed,
            "limitations": limitations}, kind="environment_setup", schema="environment_setup.v1")
        result = selected_backend.run(replace(request.run, command=argv, timeout_sec=profile["timeout_sec"],
            env=environment,
            label=f"environment:{number}", output_dir=context.store.root / "environment_processes"))
        observed[-1] = result.to_json()
        refs.extend((context.store.write_text(f"environment_logs/{number}.stdout.txt", result.stdout, kind="execution_log"),
                     context.store.write_text(f"environment_logs/{number}.stderr.txt", result.stderr, kind="execution_log")))
        ref = context.store.write_json("environment_setup.json", {"profile": profile, "steps": observed,
            "limitations": limitations}, kind="environment_setup", schema="environment_setup.v1")
        if result.status != "passed":
            return CapabilityResult(status="failed", artifacts=(*refs, ref),
                diagnostics=(f"Environment preparation step {number} {result.status}; the scientific command was not run.",))
    if before:
        from simple_ar.experiment.execution.measurement import reconcile_protocol_assets
        integrity = reconcile_protocol_assets(before)
        guard = context.store.write_json("environment_integrity.json", integrity, kind="asset_integrity")
        refs.append(guard)
        if integrity["status"] == "changed":
            return CapabilityResult(status="failed", artifacts=(*refs, ref),
                diagnostics=("Environment setup changed a declared asset or execution/checker entry; no implementation or measurement is authorized.",))
    config.pop("environment")
    config["command"] = plan["command"]
    if isolated:
        baseline = config.get("baseline")
        if isinstance(baseline, Mapping) and isinstance(baseline.get("command"), (list, tuple)):
            argv = list(baseline["command"])
            if argv and argv[0] in _PYTHON_ALIASES:
                config["baseline"] = {**baseline, "command": [str(python), *argv[1:]]}
        if "code_task" in config:
            from simple_ar.code_task.execution.environment import ensure_code_task_environment_policy
            ensure_code_task_environment_policy(run_dir, manifest,
                env_mode="external", python_executable=python)
            config["code_task"] = {**task, "env_mode": "external", "python_executable": str(python)}
    payload = {**(prepared_payload or {}), "schema_version": "prepared_execution.v1",
        "execution": config, "source_project": (prepared_payload or {}).get("source_project", str(project)), "workspace": str(project),
        "environment": {"mode": profile["mode"], **({"python_executable": str(python)} if isolated else {}), "setup_ref": ref.to_dict(), "requirements": profile["requirements"],
                        "install_project": profile["install_project"]},
        "limitations": [*limitations, *[item for item in (prepared_payload or {}).get("limitations", [])
                         if not item.startswith("No dependency installation")]]}
    prepared = context.store.write_json("execution.json", payload,
        kind="prepared_execution", schema="prepared_execution.v1", producer="research.preparation")
    if record_runtime:
        executable = environment_executable(plan['command'], project, environment)
        with executable.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        refs.append(context.store.write_json('environment_observation.json', {
            'argv': plan['command'], 'executable_sha256': digest},
            kind='environment_runtime', schema='environment_runtime.v1'))
    return CapabilityResult(status="completed", artifacts=(*refs, ref, prepared))
