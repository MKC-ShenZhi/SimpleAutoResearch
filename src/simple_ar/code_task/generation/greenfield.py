from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from simple_ar.integrations.usage import record_usage
from simple_ar.code_task.analysis.index import build_codebase_index
from simple_ar.code_task.analysis.repo_map import build_repo_map
from simple_ar.code_task.editing.scope import (
    allowed_patterns_from_manifest,
    protected_patterns_from_manifest,
)
from simple_ar.code_task.execution.summary import write_code_task_summary
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.core.artifacts import read_json, read_text, write_json, write_text
from simple_ar.code_task.generation.architecture import (
    build_architecture_plan,
    file_plan_from_architecture,
    render_architecture_markdown,
)
from simple_ar.code_task.generation.task_contract import (
    build_greenfield_task_contract,
    contract_coverage,
    finalize_task_contract,
    load_task_contract,
    save_task_contract,
)
from simple_ar.code_task.generation.writer import write_generated_project
from simple_ar.code_task.generation.implementation_memory import initial_implementation_memory
from simple_ar.code_task.generation.review import review_generated_project
from simple_ar.integrations.llm import LLMClient, LLMError

from .agent_backend import (
    should_use_agent_backend,
    write_greenfield_project_from_agent_backend,
)
from .dependencies import (
    build_dependency_advice,
    dependency_advice_messages,
    render_dependency_advice_markdown,
)


MessageCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class GreenfieldCodeTaskResult:
    run_dir: Path
    project_dir: Path
    implementation_plan_path: Path
    architecture_plan_path: Path
    file_plan_path: Path
    code_artifacts_path: Path
    review_report_path: Path
    review_status: str
    generated_files: tuple[str, ...]


def _load_planning_snapshot(
    *,
    source_run_dir: Path,
    target_contract: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """Load immutable planning artifacts without importing mutable implementation state."""

    source_root = source_run_dir.expanduser().resolve()
    source_meta = source_root / "code_task" / "meta"
    raw_source_contract = _read_raw_task_contract(source_meta)
    source_contract = load_task_contract(source_meta)
    target_hash = str(target_contract.get("version_hash") or "")
    source_hash = str(source_contract.get("version_hash") or "")
    contract_match_mode = "exact"
    if not source_contract or not target_hash:
        raise ValueError(
            "Planning snapshot contract is missing a canonical task-contract hash."
        )
    if source_hash != target_hash:
        if not _is_legacy_contract_compatible(raw_source_contract, target_contract):
            raise ValueError(
                "Planning snapshot contract mismatch: source and target must have the same "
                "canonical task-contract hash."
            )
        contract_match_mode = "legacy_v3_compatible"
    try:
        architecture = read_json(source_meta / "architecture_plan.json")
        implementation = read_json(source_meta / "implementation_plan.json")
        file_plan = read_json(source_meta / "file_plan.json")
    except Exception as exc:
        raise ValueError(f"Planning snapshot is incomplete under {source_meta}") from exc
    if not isinstance(architecture, dict) or not isinstance(implementation, dict) or not isinstance(file_plan, dict):
        raise ValueError("Planning snapshot artifacts must be JSON objects")
    architecture_mode = str(implementation.get("architecture_mode") or "").strip()
    if not architecture_mode:
        raise ValueError("Planning snapshot implementation_plan.json has no architecture_mode")
    return (
        architecture,
        architecture_mode,
        {
            "schema_version": "greenfield_planning_snapshot.v1",
            "source_run_dir": str(source_root),
            "source_contract_path": "code_task/meta/task_contract.json",
            "source_architecture_path": "code_task/meta/architecture_plan.json",
            "source_file_plan_path": "code_task/meta/file_plan.json",
            "source_contract_hash": source_hash,
            "target_contract_hash": target_hash,
            "contract_match_mode": contract_match_mode,
            "architecture_hash": _json_hash(architecture),
            "file_plan_hash": _json_hash(file_plan),
            "source_planning_mode": str(implementation.get("planning_mode") or ""),
            "source_planning_review_rounds": implementation.get("planning_review_rounds"),
            "imported_artifacts": [
                "architecture_plan.json",
                "file_plan.json",
                "implementation_plan.json",
            ],
            "excluded_state": [
                "workspace/generated_project",
                "memory/implementation_memory.json",
                "memory/repair_memory.jsonl",
                "meta/review_report.json",
                "meta/review_findings.jsonl",
                "meta/repair_snapshots",
            ],
        },
    )


def _read_raw_task_contract(meta_dir: Path) -> dict[str, Any]:
    """Read a persisted contract without applying the current schema normalizer."""

    try:
        value = read_json(meta_dir / "task_contract.json")
    except Exception:
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _is_legacy_contract_compatible(
    source_contract: dict[str, Any],
    target_contract: dict[str, Any],
) -> bool:
    """Allow v3 snapshots only when their pre-v4 task semantics are unchanged.

    v4 added ``implementation_contract`` to the persisted schema. Historical v3
    plans cannot have that field, so their full canonical hashes necessarily differ
    from a newly initialized run. This compatibility path is deliberately narrow:
    every field other than schema/version metadata and the v4-only contract must
    still match exactly.
    """

    if source_contract.get("schema_version") != "code_task_contract.v3":
        return False
    if "implementation_contract" in source_contract:
        return False
    return _legacy_contract_identity_hash(source_contract) == _legacy_contract_identity_hash(target_contract)


def _legacy_contract_identity_hash(contract: dict[str, Any]) -> str:
    excluded = {
        "schema_version",
        "legacy_schema_version",
        "version_hash",
        "implementation_contract",
        "coverage",
        "derived_views",
    }
    value = {str(key): item for key, item in contract.items() if key not in excluded}
    return _json_hash(value)


def _json_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def generate_greenfield_code_task(
    run_dir: Path,
    *,
    llm_client: LLMClient | None = None,
    model: str | None = None,
    planner_model: str | None = None,
    writer_model: str | None = None,
    reviewer_model: str | None = None,
    use_llm: bool = True,
    max_files: int = 8,
    max_generated_lines: int = 1600,
    max_source_chars_per_file: int = 4000,
    implementation_provider: str = "local",
    implementation_agent_mode: str = "",
    implementation_allow_external_agent: bool = False,
    implementation_agent_model: str = "",
    implementation_agent_binary: str = "",
    implementation_agent_args: tuple[str, ...] = (),
    implementation_agent_timeout_sec: int = 600,
    allow_planning_fallback: bool = False,
    llm_retry_attempts: int = 1,
    planning_mode: str = "compact",
    planning_review_rounds: int = 0,
    planning_snapshot_from: str | Path | None = None,
    message_callback: MessageCallback | None = None,
) -> GreenfieldCodeTaskResult:
    """Generate a greenfield project inside a code-task workspace.

    This is intentionally a code-task strategy, not an 8-stage experiment
    special case. It writes the same durable code-task artifacts as existing
    project runs: implementation metadata under ``code_task/meta``, generated
    files under ``code_task/workspace``, memory under ``code_task/memory``, and
    benchmark settings in the shared manifest.
    """

    root = Path(run_dir)
    paths = code_task_paths(root)
    manifest = load_code_task_manifest(root)
    if _code_task_kind(manifest) != "greenfield":
        raise RuntimeError("generate_greenfield_code_task requires [code_task].kind = greenfield")
    if manifest_section(manifest, "implementation").get("status") == "generated":
        # Public direct callers have the same no-regeneration contract as execute.
        meta = paths.meta_dir
        artifacts = read_json(meta / "code_artifacts.json")
        review = read_json(meta / "review_report.json")
        return GreenfieldCodeTaskResult(
            run_dir=root, project_dir=paths.workspace_dir / "generated_project",
            implementation_plan_path=meta / "implementation_plan.json",
            architecture_plan_path=meta / "architecture_plan.json",
            file_plan_path=meta / "file_plan.json", code_artifacts_path=meta / "code_artifacts.json",
            review_report_path=meta / "review_report.json", review_status=str(review.get("status", "unknown")),
            generated_files=tuple(str(row["path"]) for row in artifacts.get("generated_files", [])
                                  if isinstance(row, dict) and row.get("path")),
        )
    paths.meta_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = paths.task_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    task_text = _read_task(paths.task_dir / "task.md", limit=max_source_chars_per_file * 2)
    resource_decision = _optional_json(paths.meta_dir / "resource_decision.json")
    result_schema = result_schema_from_manifest(manifest)
    benchmark_command = _benchmark_command(manifest)
    existing_contract = load_task_contract(paths.meta_dir)
    if existing_contract:
        contract = dict(existing_contract)
        if not contract.get("benchmark_command"):
            contract["benchmark_command"] = benchmark_command
    else:
        contract = build_greenfield_task_contract(
            task_text,
            benchmark_command=benchmark_command,
            max_files=max_files,
            max_generated_lines=max_generated_lines,
            result_schema=result_schema,
            source={
                "kind": "code_task",
                "task_file": "code_task/task.md",
                "origin": "greenfield_generation",
            },
        )
    contract = finalize_task_contract(contract)
    save_task_contract(paths.meta_dir, contract)
    source_snapshot = Path(planning_snapshot_from) if planning_snapshot_from else None
    prompt_contract = contract
    result_schema = _result_schema_with_contract_metrics(result_schema, contract)
    resource_plan = _resource_plan(
        resource_decision,
        task_text=task_text,
        max_files=max_files,
        max_generated_lines=max_generated_lines,
    )
    dependency_advice = build_dependency_advice(task_text)
    dependency_plan = _dependency_plan(task_text, dependency_advice=dependency_advice)
    domain_profile = _domain_profile(task_text, dependency_advice=dependency_advice, benchmark_command=benchmark_command)
    dependency_advice_path = paths.meta_dir / "dependency_advice.json"
    write_json(dependency_advice_path, dependency_advice)
    write_text(paths.meta_dir / "dependency_advice.md", render_dependency_advice_markdown(dependency_advice))
    for message in dependency_advice_messages(dependency_advice):
        _emit(message_callback, message)

    writer_client = _llm_client(
        paths.meta_dir,
        llm_client=llm_client,
        model=writer_model or model,
        use_llm=use_llm,
        allow_fallback=allow_planning_fallback,
        message_callback=message_callback,
    )
    reviewer_client = _llm_client(
        paths.meta_dir,
        llm_client=llm_client,
        model=reviewer_model or model,
        use_llm=use_llm,
        allow_fallback=allow_planning_fallback,
        message_callback=message_callback,
    )
    planning_dir = paths.meta_dir / "planning"
    planning_snapshot: dict[str, Any] | None = None
    if source_snapshot is not None:
        _emit(message_callback, "Importing immutable accepted greenfield plan.")
        architecture, architecture_mode, planning_snapshot = _load_planning_snapshot(
            source_run_dir=source_snapshot,
            target_contract=contract,
        )
        planning_dir.mkdir(parents=True, exist_ok=True)
        write_json(paths.meta_dir / "planning_snapshot.json", planning_snapshot)
    else:
        planner_client = _llm_client(
            paths.meta_dir,
            llm_client=llm_client,
            model=planner_model or model,
            use_llm=use_llm,
            allow_fallback=allow_planning_fallback,
            message_callback=message_callback,
        )
        _emit(message_callback, "Planning greenfield project architecture.")
        architecture, architecture_mode = build_architecture_plan(
            contract=prompt_contract,
            result_schema=result_schema,
            resource_plan=resource_plan,
            domain_profile=domain_profile,
            client=planner_client,
            allow_fallback=allow_planning_fallback or not use_llm,
            retry_attempts=llm_retry_attempts,
            planning_mode=planning_mode,
            planning_review_rounds=planning_review_rounds,
            planner_selector=planner_model or model or "",
            planning_dir=planning_dir,
            message_callback=message_callback,
        )
    implementation_plan_path = paths.meta_dir / "implementation_plan.json"
    architecture_plan_path = paths.meta_dir / "architecture_plan.json"
    file_plan_path = paths.meta_dir / "file_plan.json"
    effective_planning_mode = str((planning_snapshot or {}).get("source_planning_mode") or "compact")
    effective_planning_review_rounds = (planning_snapshot or {}).get(
        "source_planning_review_rounds",
        0,
    )
    write_json(
        implementation_plan_path,
        {
            "schema_version": "code_task_implementation_plan.v1",
            "mode": "greenfield",
            "architecture_mode": architecture_mode,
            "provider": _normalize_provider(implementation_provider),
            "agent_mode": implementation_agent_mode or "",
            "project_dir": "code_task/workspace/generated_project",
            "entrypoint": benchmark_command,
            "planning_mode": effective_planning_mode,
            "requested_planning_mode": planning_mode,
            "planning_review_rounds": effective_planning_review_rounds,
            "ablation": {
                "planning_snapshot": planning_snapshot,
            },
            "planning_dir": "code_task/meta/planning",
            "task_contract": "code_task/meta/task_contract.json",
            "models": {
                "default": model or "",
                "planner": planner_model or "",
                "writer": writer_model or "",
                "reviewer": reviewer_model or "",
            },
            "resource_plan": resource_plan,
            "dependency_plan": dependency_plan,
            "dependency_advice": "code_task/meta/dependency_advice.json",
            "task_contract_hash": contract.get("version_hash", ""),
        },
    )
    write_json(architecture_plan_path, architecture)
    write_json(paths.meta_dir / "task_contract_coverage.json", contract_coverage(contract, architecture))
    write_text(paths.meta_dir / "architecture_plan.md", render_architecture_markdown(architecture))
    write_json(file_plan_path, file_plan_from_architecture(architecture))

    memory = initial_implementation_memory(
        contract=prompt_contract,
        architecture_plan=architecture,
        mode=architecture_mode,
    )
    project_dir = paths.workspace_dir / "generated_project"
    provider = _normalize_provider(implementation_provider)
    agent_result: dict[str, Any] | None = None
    if should_use_agent_backend(provider):
        _emit(message_callback, f"Generating project through `{provider}` agent handoff.")
        code_artifacts, agent_result = write_greenfield_project_from_agent_backend(
            run_dir=root,
            project_dir=project_dir,
            provider=provider,
            agent_mode=implementation_agent_mode,
            contract=prompt_contract,
            result_schema=result_schema,
            architecture=architecture,
            memory=memory,
            client=writer_client,
            timeout_sec=implementation_agent_timeout_sec,
            external_enabled=implementation_allow_external_agent,
            agent_model=implementation_agent_model,
            agent_binary=implementation_agent_binary,
            agent_args=implementation_agent_args,
        )
    else:
        _emit(message_callback, "Writing related project files through the shared implementation generator.")
        code_artifacts = write_generated_project(
            project_dir=project_dir,
            architecture_plan=architecture,
            result_schema=result_schema,
            contract=prompt_contract,
            memory=memory,
            dependency_advice=dependency_advice,
            client=writer_client,
            max_generated_lines=max_generated_lines,
            files_per_batch=4,
            retry_attempts=llm_retry_attempts,
            allow_fallback=allow_planning_fallback or not use_llm,
            agent_step_dir=paths.meta_dir / "generation_steps",
            message_callback=message_callback,
        )
    generated_files = tuple(
        f"generated_project/{row.get('path')}"
        for row in code_artifacts.get("generated_files", [])
        if isinstance(row, dict) and row.get("path")
    )
    code_artifacts_path = paths.meta_dir / "code_artifacts.json"
    write_json(code_artifacts_path, code_artifacts)
    write_json(memory_dir / "implementation_memory.json", memory)
    write_json(
        paths.meta_dir / "code_backend.json",
        {
            "schema_version": "code_backend.v1",
            "backend": "greenfield_agent" if agent_result else "greenfield_local",
            "provider": provider,
            "agent_mode": implementation_agent_mode or ("handoff" if agent_result else "model"),
            "project_dir": "code_task/workspace/generated_project",
            "entrypoint": benchmark_command,
            "agent_result": agent_result,
        },
    )

    _refresh_codebase_artifacts(root, generated_files=generated_files)
    _emit(message_callback, "Reviewing generated greenfield project.")
    review = review_generated_project(
        project_dir=project_dir,
        code_artifacts=code_artifacts,
        result_schema=result_schema,
        resource_plan=resource_plan,
        contract=prompt_contract,
        dependency_advice=dependency_advice,
        implementation_memory=memory,
        architecture_plan=architecture,
        client=reviewer_client,
        meta_dir=paths.meta_dir,
    )
    review_report_path = paths.meta_dir / "review_report.json"
    write_json(review_report_path, review)
    _update_manifest_after_generation(
        root,
        manifest=load_code_task_manifest(root),
        generated_files=generated_files,
        project_dir=project_dir,
        review=review,
        provider=provider,
        agent_mode=implementation_agent_mode or ("handoff" if agent_result else "model"),
        contract=contract,
    )
    write_code_task_summary(root)
    return GreenfieldCodeTaskResult(
        run_dir=root,
        project_dir=project_dir,
        implementation_plan_path=implementation_plan_path,
        architecture_plan_path=architecture_plan_path,
        file_plan_path=file_plan_path,
        code_artifacts_path=code_artifacts_path,
        review_report_path=review_report_path,
        review_status=str(review.get("status", "unknown")),
        generated_files=generated_files,
    )


def _llm_client(
    meta_dir: Path,
    *,
    llm_client: LLMClient | None = None,
    model: str | None,
    use_llm: bool,
    allow_fallback: bool,
    message_callback: MessageCallback | None,
) -> LLMClient | None:
    if not use_llm:
        return None
    try:
        return LLMClient.for_task(
            client=llm_client,
            model=model,
            usage_callback=lambda usage: record_usage(
                meta_dir,
                usage,
                stage="code_task.greenfield",
                message_callback=message_callback,
            ),
        )
    except LLMError as exc:
        if allow_fallback and not (model or "").startswith(("profile:", "route:")):
            _emit(message_callback, f"LLM unavailable; using bounded fallback generation. {exc}")
            return None
        raise LLMError(
            "Greenfield code generation requires an available LLM client when fallback is disabled. "
            "Set [execute].allow_planning_fallback = true only for demos/offline smoke tests."
        ) from exc




def result_schema_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    benchmark = manifest.get("benchmark", {}) if isinstance(manifest.get("benchmark"), dict) else {}
    primary = str(benchmark.get("primary_metric") or "score").strip() or "score"
    directions = benchmark.get("metric_directions")
    required = [primary]
    if isinstance(directions, dict):
        required.extend(str(name) for name in directions if str(name).strip() and str(name) != primary)
    return {
        "schema_version": "code_task_greenfield_result_schema.v1",
        "primary_metric": primary,
        "required_metrics": list(dict.fromkeys(required)),
        "metric_directions": directions if isinstance(directions, dict) else {},
    }


def _result_schema_with_contract_metrics(
    result_schema: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    metric_contract = contract.get("metric_contract")
    required = list(result_schema.get("required_metrics", [])) if isinstance(result_schema.get("required_metrics"), list) else []
    if isinstance(metric_contract, dict) and isinstance(metric_contract.get("required_metrics"), list):
        required.extend(str(item) for item in metric_contract["required_metrics"] if str(item).strip())
    merged = dict(result_schema)
    merged["required_metrics"] = list(dict.fromkeys(required))
    directions = merged.get("metric_directions")
    if not isinstance(directions, dict):
        directions = {}
    merged["metric_directions"] = dict(directions)
    return merged




def _resource_plan(
    resource_decision: dict[str, Any],
    *,
    task_text: str,
    max_files: int,
    max_generated_lines: int,
) -> dict[str, Any]:
    profile = str(resource_decision.get("profile") or "local_cpu")
    task_cpu_only = _task_disallows_gpu(task_text)
    allow_gpu = bool(resource_decision.get("allow_gpu")) and not task_cpu_only
    if task_cpu_only:
        profile = "local_cpu"
    execution_budget = _execution_budget(task_text=task_text, profile=profile, max_files=max_files)
    return {
        "schema_version": "code_task_greenfield_resource_plan.v1",
        "profile": profile,
        "allow_gpu": allow_gpu,
        "max_files": max_files,
        "max_generated_lines": max_generated_lines,
        "execution_budget": execution_budget,
        "decision": resource_decision,
        "constraint_overrides": [
            "Task text explicitly requires CPU-only execution; GPU preference from environment probe was disabled."
        ]
        if task_cpu_only
        else [],
    }


def _execution_budget(*, task_text: str, profile: str, max_files: int) -> dict[str, Any]:
    text = " ".join(str(task_text or "").lower().replace("-", " ").split())
    is_ml = any(
        token in text
        for token in (
            "classifier",
            "cross validation",
            "dataset",
            "experiment",
            "model",
            "regression",
            "scikit",
            "sklearn",
            "training",
        )
    )
    scale = "medium" if max_files >= 12 else "compact"
    if profile in {"gpu", "large"}:
        max_runtime = 900 if scale == "medium" else 600
    elif profile == "local_cpu":
        max_runtime = 300 if scale == "medium" else 180
    else:
        max_runtime = 450 if scale == "medium" else 240
    return {
        "schema_version": "code_task_execution_budget.v1",
        "profile": profile,
        "scale": scale,
        "max_runtime_sec_hint": max_runtime,
        "max_model_fit_operations_hint": 600 if is_ml and scale == "medium" else 180,
        "guidance": [
            "Keep benchmark paths bounded and deterministic; prefer smoke/standard presets over exhaustive sweeps.",
            "When using model fitting, cap dataset, seed, fold, candidate, query, and hyperparameter loops explicitly.",
            "If an exact method is expensive, implement a documented bounded approximation instead of an unbounded nested loop.",
            "Runtime artifacts should preserve measured evidence without printing unbounded logs.",
        ],
    }


def _task_disallows_gpu(task_text: str) -> bool:
    text = " ".join(str(task_text or "").lower().replace("-", " ").split())
    return any(
        phrase in text
        for phrase in (
            "cpu only",
            "cpu-only",
            "no gpu",
            "without gpu",
            "do not use gpu",
            "gpu disabled",
        )
    )


def _dependency_plan(task_text: str, *, dependency_advice: dict[str, Any]) -> dict[str, Any]:
    mentioned = _mentioned_packages(task_text)
    available = [
        str(row.get("package"))
        for row in dependency_advice.get("packages", [])
        if isinstance(row, dict) and row.get("status") == "installed"
    ]
    return {
        "schema_version": "code_task_greenfield_dependency_plan.v1",
        "install_allowed": False,
        "allowed_dependency_policy": "standard_library_preferred_or_explicitly_available",
        "mentioned_packages": mentioned,
        "available_task_relevant_packages": available,
        "notes": [
            "Generated code should prefer the Python standard library for local smoke paths.",
            "Generated code may use task-relevant installed packages listed in dependency_advice, but must not install new dependencies.",
            "If an optional package is missing, fail clearly or provide a bounded fallback when the task allows it.",
        ],
    }


def _mentioned_packages(task_text: str) -> list[str]:
    text = task_text.lower()
    candidates = [
        "numpy",
        "pandas",
        "scikit-learn",
        "sklearn",
        "torch",
        "pytorch",
        "transformers",
        "datasets",
        "rich",
        "pydantic",
        "tree-sitter",
        "lancedb",
    ]
    return [name for name in candidates if name in text]


def _domain_profile(task_text: str, *, dependency_advice: dict[str, Any], benchmark_command: str) -> dict[str, Any]:
    relevant_packages = []
    for row in dependency_advice.get("packages", []):
        if not isinstance(row, dict) or row.get("status") != "installed":
            continue
        relevant_packages.append(
            {
                "package": row.get("package"),
                "version": row.get("version"),
                "import_names": row.get("import_names") or [row.get("import_name")],
                "role": row.get("role"),
                "priority": row.get("priority"),
            }
        )
    return {
        "schema_version": "code_task_greenfield_domain_profile.v1",
        "task_excerpt": task_text[:2000],
        "expected_entrypoints": [benchmark_command or "python generated_project/main.py"],
        "generated_project_root": "generated_project",
        "file_plan_path_rule": (
            "Architecture/file-plan paths are relative to the generated project root. "
            "`generated_project/main.py` in a run command corresponds to `main.py` in the file plan."
        ),
        "environment_package_count": dependency_advice.get("environment_package_count", 0),
        "available_task_relevant_packages": relevant_packages[:60],
    }


def _refresh_codebase_artifacts(run_dir: Path, *, generated_files: tuple[str, ...]) -> None:
    paths = code_task_paths(run_dir)
    index_path = paths.meta_dir / "codebase_index.json"
    codebase_index = build_codebase_index(paths.workspace_dir, output_path=index_path)
    allowed = allowed_patterns_from_manifest(load_code_task_manifest(run_dir))
    protected = protected_patterns_from_manifest(load_code_task_manifest(run_dir))
    repo_map_path = paths.meta_dir / "repo_map.json"
    repo_map_summary_path = paths.meta_dir / "repo_map_summary.md"
    repo_map = build_repo_map(
        codebase_index,
        output_path=repo_map_path,
        summary_path=repo_map_summary_path,
        allowed_patterns=tuple(allowed),
        protected_patterns=tuple(protected),
    )
    manifest = load_code_task_manifest(run_dir)
    codebase = manifest_section(manifest, "codebase")
    project = codebase_index.get("project", {}) if isinstance(codebase_index.get("project"), dict) else {}
    repo_project = repo_map.get("project", {}) if isinstance(repo_map.get("project"), dict) else {}
    codebase.update(
        {
            "file_count": project.get("file_count", 0),
            "python_file_count": project.get("python_file_count", 0),
            "test_file_count": project.get("test_file_count", 0),
            "entrypoint_candidates": project.get("entrypoint_candidates", []),
            "generated_files": list(generated_files),
            "repo_map": {
                "schema_version": repo_map.get("schema_version"),
                "path": "code_task/meta/repo_map.json",
                "summary": "code_task/meta/repo_map_summary.md",
                "directory_count": repo_project.get("directory_count", 0),
                "symbol_count": repo_project.get("symbol_count", 0),
                "benchmark_file_count": repo_project.get("benchmark_file_count", 0),
                "config_file_count": repo_project.get("config_file_count", 0),
            },
        }
    )
    manifest["codebase"] = codebase
    save_code_task_manifest(run_dir, manifest)


def _update_manifest_after_generation(
    run_dir: Path,
    *,
    manifest: dict[str, Any],
    generated_files: tuple[str, ...],
    project_dir: Path,
    review: dict[str, Any],
    provider: str,
    agent_mode: str,
    contract: dict[str, Any],
) -> None:
    layout = manifest_section(manifest, "layout")
    layout.update(
        {
            "generated_project": "code_task/workspace/generated_project",
            "implementation_plan": "code_task/meta/implementation_plan.json",
            "architecture_plan": "code_task/meta/architecture_plan.json",
            "file_plan": "code_task/meta/file_plan.json",
            "code_artifacts": "code_task/meta/code_artifacts.json",
            "code_backend": "code_task/meta/code_backend.json",
            "review_report": "code_task/meta/review_report.json",
            "task_contract": "code_task/meta/task_contract.json",
            "task_contract_coverage": "code_task/meta/task_contract_coverage.json",
        }
    )
    manifest["layout"] = layout
    implementation = manifest_section(manifest, "implementation")
    implementation.update(
        {
            "status": "generated" if review.get("status") != "failed" else "review_failed",
            "mode": "greenfield",
            "generated_at": utcnow_iso(),
            "project_dir": "code_task/workspace/generated_project",
            "generated_files": list(generated_files),
            "review_status": review.get("status", "unknown"),
            "provider": _normalize_provider(provider),
            "agent_mode": agent_mode,
            "task_contract": "code_task/meta/task_contract.json",
            "task_contract_hash": contract.get("version_hash", ""),
        }
    )
    manifest["implementation"] = implementation
    patch = manifest_section(manifest, "patch")
    patch.update(
        {
            "status": "applied",
            "mode": "greenfield_generated",
            "changed_files": list(generated_files),
            "applied_at": utcnow_iso(),
        }
    )
    manifest["patch"] = patch
    benchmark = manifest_section(manifest, "benchmark")
    if not benchmark.get("command"):
        benchmark["command"] = "python generated_project/main.py"
    manifest["benchmark"] = benchmark
    manifest["status"] = "generated" if review.get("status") != "failed" else "review_failed"
    save_code_task_manifest(run_dir, manifest)


def _benchmark_command(manifest: dict[str, Any]) -> str:
    benchmark = manifest.get("benchmark")
    if isinstance(benchmark, dict) and benchmark.get("command"):
        return str(benchmark["command"])
    return "python generated_project/main.py"


def _read_task(path: Path, *, limit: int) -> str:
    text = read_text(path) if path.is_file() else ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _code_task_kind(manifest: dict[str, Any]) -> str:
    section = manifest.get("code_task")
    if isinstance(section, dict):
        return str(section.get("kind") or "existing_project").strip().lower()
    return "existing_project"


def _normalize_provider(value: object) -> str:
    return str(value or "local").strip().lower().replace("-", "_") or "local"


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
