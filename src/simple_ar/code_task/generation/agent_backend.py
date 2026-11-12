from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from simple_ar.agent_backends import (
    AgentExecutionMode,
    AgentPermissionPolicy,
    AgentRunRequest,
    build_code_task_greenfield_handoff,
    create_agent_backend,
    ingest_agent_outputs,
    is_external_agent_provider,
    normalize_agent_mode,
    validate_agent_mode_for_provider,
)
from simple_ar.core.artifacts import read_json, write_json
from simple_ar.core.budget import BudgetLedger
from simple_ar.agent_backends.external_cli import ExternalCliBackend
from simple_ar.integrations.llm import LLMClient
from simple_ar.code_task.generation.common import safe_relative_path as _safe_project_path
from simple_ar.code_task.generation.common import generation_candidate
from simple_ar.code_task.generation.file_specs import is_deliverable_project_file as _is_deliverable_project_file


def should_use_agent_backend(provider: str) -> bool:
    normalized = _normalize_provider(provider)
    return normalized != "local" and is_external_agent_provider(normalized)


def check_agent_budget(run_dir: Path, provider: str, *, enabled: bool,
                       binary: str, mode: str, ledger: BudgetLedger | None) -> None:
    """Detect an incompatible external budget before planning invokes a model."""
    if (enabled and should_use_agent_backend(provider)
            and normalize_agent_mode(mode, provider=_normalize_provider(provider)) != AgentExecutionMode.DELEGATED_WORKSPACE):
        backend = create_agent_backend(provider, enabled=True, binary=binary or None,
                                       budget_ledger=ledger)
        if isinstance(backend, ExternalCliBackend):
            backend.accounting_ledger(run_dir)


def retained_greenfield_plan(*, run_dir: Path, provider: str, client: LLMClient | None,
                             enabled: bool, binary: str, model: str, timeout_sec: int,
                             args: tuple[str, ...], ledger: BudgetLedger | None,
                             session_id: str, attempt_id: str, contract: Mapping[str, Any],
                             result_schema: Mapping[str, Any]) -> tuple[dict, str, dict | None] | None:
    """Find a completed external handoff before buying a replacement plan."""
    normalized = _normalize_provider(provider)
    handoff = run_dir / 'agent_handoff' / f'code-task-greenfield-{normalized}'
    if not handoff.is_dir():
        return None
    backend = create_agent_backend(normalized, enabled=enabled, client=client, model=model or None,
        timeout_sec=timeout_sec, binary=binary or None, extra_args=args, budget_ledger=ledger,
        session_id=session_id, attempt_id=attempt_id)
    if not isinstance(backend, ExternalCliBackend):
        return None
    request = AgentRunRequest(provider=normalized, run_dir=run_dir, handoff_dir=handoff,
        workspace_dir=run_dir / 'code_task' / 'workspace')
    if backend.retained_result(request) is None:
        return None
    context = handoff / 'context'
    if (read_json(context / 'contract.json') != dict(contract)
            or read_json(context / 'result_schema.json') != dict(result_schema)):
        raise ValueError('Retained agent handoff has different work inputs; preserve it and start a new attempt')
    plan = read_json(run_dir / 'code_task' / 'meta' / 'implementation_plan.json')
    return (read_json(context / 'architecture_plan.json'), plan['architecture_mode'],
            (plan.get('ablation') or {}).get('planning_snapshot'))


def write_greenfield_project_from_agent_backend(
    *,
    run_dir: Path,
    project_dir: Path,
    provider: str,
    agent_mode: str,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
    architecture: Mapping[str, Any],
    memory: dict[str, Any],
    client: LLMClient | None,
    timeout_sec: int,
    external_enabled: bool,
    agent_model: str = "",
    agent_binary: str = "",
    agent_args: tuple[str, ...] = (),
    budget_ledger: BudgetLedger | None = None,
    session_id: str = '',
    attempt_id: str = '',
    validate_candidate: Callable[[Path], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run an external backend for greenfield code-task generation.

    The handoff asks for candidates under ``agent_handoff/<name>/generated_files``.
    Those outputs are ingested into the code-task workspace for normal review;
    filesystem and network isolation depend on the selected CLI, not the handoff.
    """

    root = Path(run_dir)
    client_ledger = client.budget_ledger if isinstance(client, LLMClient) else None
    if budget_ledger is None:
        budget_ledger = client_ledger
    elif client_ledger is not None and client_ledger is not budget_ledger:
        raise ValueError('Code task and model client must use the same budget ledger')
    normalized_provider = _normalize_provider(provider)
    resolved_agent_mode = normalize_agent_mode(agent_mode, provider=normalized_provider)
    if resolved_agent_mode == AgentExecutionMode.DELEGATED_WORKSPACE:
        _write_delegated_workspace_dry_run(
            run_dir=root,
            provider=normalized_provider,
            contract=contract,
            result_schema=result_schema,
        )
        raise NotImplementedError(
            "`agent_mode = \"delegated_workspace\"` is recognized, but the "
            "workspace snapshot/diff/rollback runner is not executable yet. "
            "Use `agent_mode = \"handoff\"` for now."
        )
    validate_agent_mode_for_provider(resolved_agent_mode, provider=normalized_provider)
    if not should_use_agent_backend(normalized_provider):
        raise ValueError(f"Provider `{provider}` is not an external greenfield backend.")
    if not external_enabled and normalized_provider not in {"fake", "dry_run", "dryrun", "local_llm", "llm"}:
        raise RuntimeError(
            "External greenfield implementation provider requested but "
            "`implementation.allow_external_agent` is not enabled."
        )

    permission_policy = AgentPermissionPolicy(
        allow_file_write=True,
        allow_shell_commands=True,
        allow_network=False,
        allowed_write_patterns=["generated_files/**", "review.md", "agent_result.json"],
        notes=[
            "Write generated source files only under generated_files/.",
            "Do not write directly into code_task/workspace/.",
            "Do not install dependencies, access secrets, or read/write outside the handoff directory.",
            "SimpleAutoResearch will ingest, validate, review, and run the generated project separately.",
        ],
    )
    handoff_name = f"code-task-greenfield-{normalized_provider}"
    handoff_dir = root / 'agent_handoff' / handoff_name
    request = AgentRunRequest(
        provider=normalized_provider,
        run_dir=root,
        handoff_dir=handoff_dir,
        workspace_dir=root / "code_task" / "workspace",
        timeout_sec=timeout_sec,
        metadata={
            "mode": "greenfield",
            "runtime": "code_task",
            "agent_mode": resolved_agent_mode.value,
            "result_schema": dict(result_schema),
            "architecture_file_count": len(architecture.get("files", [])) if isinstance(architecture.get("files"), list) else 0,
        },
    )
    backend = create_agent_backend(
        normalized_provider,
        enabled=external_enabled,
        client=client,
        model=agent_model or None,
        timeout_sec=timeout_sec,
        binary=agent_binary or None,
        extra_args=agent_args,
        budget_ledger=budget_ledger,
        session_id=session_id,
        attempt_id=attempt_id,
    )
    result = backend.retained_result(request) if isinstance(backend, ExternalCliBackend) else None
    if result is not None:
        expected = {'architecture_plan.json': architecture, 'result_schema.json': result_schema, 'contract.json': contract}
        if any(read_json(handoff_dir / 'context' / name) != dict(value) for name, value in expected.items()):
            raise ValueError('Retained agent handoff has different work inputs; preserve it and start a new attempt')
    else:
        package = build_code_task_greenfield_handoff(root, name=handoff_name,
            permission_policy=permission_policy, context_files={
                "architecture_plan.json": _json_context(architecture),
                "result_schema.json": _json_context(result_schema),
                "contract.json": _json_context(contract)})
        handoff_dir = package.handoff_dir
        request = request.model_copy(update={'handoff_dir': handoff_dir})
        result = backend.run(request)
    ingestion = ingest_agent_outputs(run_dir=root, handoff_dir=handoff_dir)
    generated_dir = handoff_dir / "generated_files"
    generated_files = _generated_file_paths(generated_dir)
    if (not result.ok or not generated_files) and result.metadata.get('usage_status') != 'unknown':
        retry_package = build_code_task_greenfield_handoff(
            root,
            name=f"code-task-greenfield-{normalized_provider}-retry",
            permission_policy=permission_policy,
            extra_instructions=_retry_instructions(
                result_status=result.status,
                result_message=result.message,
                generated_file_count=len(generated_files),
                ingestion=ingestion,
            ),
            context_files={
                "previous_attempt_failure.md": _retry_context_markdown(
                    result_status=result.status,
                    result_message=result.message,
                    generated_file_count=len(generated_files),
                    ingestion=ingestion,
                ),
                "architecture_plan.json": _json_context(architecture),
                "result_schema.json": _json_context(result_schema),
                "contract.json": _json_context(contract),
            },
        )
        retry_request = request.model_copy(update={"handoff_dir": retry_package.handoff_dir})
        result = backend.run(retry_request)
        ingestion = ingest_agent_outputs(run_dir=root, handoff_dir=retry_package.handoff_dir)
        handoff_dir = retry_package.handoff_dir
        generated_dir = handoff_dir / "generated_files"
        generated_files = _generated_file_paths(generated_dir)
    if not result.ok or not generated_files:
        raise RuntimeError(
            "Agent greenfield backend did not produce validated generated_files/. "
            f"Status={result.status}; generated_file_count={len(generated_files)}; "
            f"message={result.message}; result={result.result_path or handoff_dir}."
        )
    with generation_candidate(project_dir, validate=validate_candidate) as candidate:
        shutil.copytree(generated_dir, candidate, ignore=_copy_ignore, dirs_exist_ok=True)
    code_artifacts = _code_artifacts_from_project(project_dir, source=f"agent:{normalized_provider}")
    agent_result = {
        "provider": normalized_provider,
        "status": result.status,
        "agent_mode": resolved_agent_mode.value,
        "handoff_dir": handoff_dir.relative_to(root).as_posix(),
        "ingestion": ingestion,
        "result_path": result.result_path,
        "review_path": result.review_path,
        **({'usage_status': result.metadata['usage_status'],
            'usage_reservation': result.metadata['usage_reservation']}
           if 'usage_status' in result.metadata else {}),
        **({'process_record': result.metadata['process_record']}
           if 'process_record' in result.metadata else {}),
    }
    memory.setdefault("agent_backends", []).append(agent_result)
    return code_artifacts, agent_result


def _write_delegated_workspace_dry_run(
    *,
    run_dir: Path,
    provider: str,
    contract: Mapping[str, Any],
    result_schema: Mapping[str, Any],
) -> None:
    write_json(
        run_dir / "code_task" / "meta" / "delegated_workspace_dry_run.json",
        {
            "schema_version": "code_task_delegated_workspace_dry_run.v1",
            "status": "not_executable",
            "provider": provider,
            "reason": "delegated workspace requires snapshot, diff, rollback, permission, and benchmark guard enforcement before real execution.",
            "required_boundaries": [
                "isolated workspace only",
                "pre/post file snapshots",
                "allowed/protected path enforcement",
                "rollback before accepting generated workspace state",
                "validation and benchmark guard before accepting outputs",
            ],
            "contract_id": contract.get("contract_id") if isinstance(contract, Mapping) else "",
            "primary_metric": result_schema.get("primary_metric") if isinstance(result_schema, Mapping) else "",
        },
    )


def _retry_instructions(
    *,
    result_status: str,
    result_message: str,
    generated_file_count: int,
    ingestion: dict[str, Any],
) -> str:
    return (
        "The previous external-agent attempt did not produce a valid non-empty "
        "`generated_files/` directory. Retry once with the same task, focusing "
        "only on producing the required files under `generated_files/`.\n\n"
        f"- Previous status: `{result_status}`\n"
        f"- Previous message: {result_message or '(empty)'}\n"
        f"- Previous generated file count: `{generated_file_count}`\n"
        f"- Previous normalized changed files: {ingestion.get('changed_files', [])}\n\n"
        "Do not write outside this new handoff directory. Do not rely on the previous handoff directory."
    )


def _retry_context_markdown(
    *,
    result_status: str,
    result_message: str,
    generated_file_count: int,
    ingestion: dict[str, Any],
) -> str:
    return (
        "# Previous Attempt Failure\n\n"
        f"- Status: `{result_status}`\n"
        f"- Message: {result_message or '(empty)'}\n"
        f"- Generated file count: `{generated_file_count}`\n"
        f"- Ingestion status: `{ingestion.get('status', 'unknown')}`\n"
        f"- Normalized outputs: `{ingestion.get('normalized_outputs', '')}`\n"
        f"- Snapshot: `{ingestion.get('snapshot', '')}`\n"
    )


def _code_artifacts_from_project(project_dir: Path, *, source: str) -> dict[str, Any]:
    generated: list[dict[str, Any]] = []
    total_lines = 0
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = _safe_project_path(path.relative_to(project_dir).as_posix())
        if not rel or not _is_deliverable_project_file(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        line_count = max(1, len(text.splitlines()))
        total_lines += line_count
        generated.append(
            {
                "path": rel,
                "mode": source,
                "line_count": line_count,
                "summary": "Generated by external agent backend.",
            }
        )
    return {
        "schema_version": "greenfield_code_artifacts.v1",
        "project_dir": str(project_dir),
        "generated_files": generated,
        "total_lines": total_lines,
        "entrypoint": "main.py",
    }


def _generated_file_paths(generated_dir: Path) -> list[Path]:
    if not generated_dir.is_dir():
        return []
    files: list[Path] = []
    for path in generated_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = _safe_project_path(path.relative_to(generated_dir).as_posix())
        if rel and _is_deliverable_project_file(rel):
            files.append(path)
    return files


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        rel = _safe_project_path((Path(directory).name + "/" + name).replace("\\", "/"))
        if name in {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
            ignored.add(name)
            continue
        if name.endswith((".pyc", ".pyo")):
            ignored.add(name)
            continue
        if name in {"agent_result.json", "ingestion.json"}:
            ignored.add(name)
            continue
        if name == "review.md":
            ignored.add(name)
            continue
        if rel and not _is_deliverable_project_file(name):
            ignored.add(name)
    return ignored



def _normalize_provider(value: str) -> str:
    return (value or "local").strip().lower().replace("-", "_") or "local"


def _json_context(value: Mapping[str, Any]) -> str:
    import json

    return json.dumps(dict(value), indent=2, ensure_ascii=False)
