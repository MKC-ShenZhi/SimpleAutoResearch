from __future__ import annotations

import shutil
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from simple_ar.agent_backends.policy import AgentPermissionPolicy
from simple_ar.code_task.memory import code_task_memory_paths, task_memory_context
from simple_ar.core.artifacts import write_json, write_text
from simple_ar.tools.openai_schema import export_openai_tool_schemas
from simple_ar.tools.registry import ToolRegistry, default_tool_registry


@dataclass(frozen=True)
class AgentHandoffPackage:
    """Paths for a generated agent handoff package."""

    handoff_dir: Path
    instructions_path: Path
    tool_schema_path: Path
    permission_policy_path: Path
    artifact_handles_path: Path
    expected_outputs_path: Path
    workspace_manifest_path: Path

    def to_json(self) -> dict[str, str]:
        return {
            "schema_version": "agent_handoff_package.v1",
            "handoff_dir": str(self.handoff_dir),
            "instructions": str(self.instructions_path),
            "tool_schema": str(self.tool_schema_path),
            "permission_policy": str(self.permission_policy_path),
            "artifact_handles": str(self.artifact_handles_path),
            "expected_outputs": str(self.expected_outputs_path),
            "workspace_manifest": str(self.workspace_manifest_path),
        }


def create_agent_handoff(
    *,
    run_dir: Path,
    instructions: str,
    name: str = "default",
    registry: ToolRegistry | None = None,
    permission_policy: AgentPermissionPolicy | None = None,
    expected_outputs: dict[str, Any] | None = None,
    artifact_refs: Iterable[str] = (),
    profile_name: str = "codex",
    profile_text: str | None = None,
    context_files: dict[str, str] | None = None,
) -> AgentHandoffPackage:
    """Write a workspace-scoped handoff package for an external agent backend."""
    run_dir = Path(run_dir)
    handoff_dir = run_dir / "agent_handoff" / _safe_name(name)
    _relocate_legacy_handoff_archives(run_dir)
    _archive_existing_handoff(handoff_dir, run_dir)
    context_dir = handoff_dir / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    registry = registry or default_tool_registry(include_report=False)
    permission_policy = permission_policy or AgentPermissionPolicy.read_only()
    expected_outputs = expected_outputs or _default_expected_outputs()
    profile = profile_text if profile_text is not None else load_profile(profile_name)

    package = AgentHandoffPackage(
        handoff_dir=handoff_dir,
        instructions_path=handoff_dir / "instructions.md",
        tool_schema_path=handoff_dir / "tool_schema.json",
        permission_policy_path=handoff_dir / "permission_policy.json",
        artifact_handles_path=handoff_dir / "artifact_handles.json",
        expected_outputs_path=handoff_dir / "expected_outputs.json",
        workspace_manifest_path=handoff_dir / "workspace_manifest.json",
    )
    write_text(
        package.instructions_path,
        _render_instructions(
            instructions=instructions,
            profile_name=profile_name,
            profile_text=profile,
            expected_outputs=expected_outputs,
            permission_policy=permission_policy,
        ),
    )
    write_json(package.tool_schema_path, {"schema_version": "agent_tool_schema.v1", "tools": export_openai_tool_schemas(registry)})
    write_json(package.permission_policy_path, permission_policy.model_dump(mode="json"))
    write_json(package.artifact_handles_path, _artifact_handles(run_dir, artifact_refs))
    write_json(package.expected_outputs_path, {"schema_version": "agent_expected_outputs.v1", **expected_outputs})
    write_json(package.workspace_manifest_path, _workspace_manifest(run_dir))
    for rel, text in (context_files or {}).items():
        safe = _safe_relative(rel)
        if safe:
            write_text(context_dir / safe, text)
    write_json(handoff_dir / "handoff_package.json", package.to_json())
    return package


def _code_task_context(run_dir: Path, task_text: str) -> tuple[str, str, list[str]]:
    """Supply the same retained task and memory to both code-agent entry points."""
    task_path = run_dir / "code_task" / "task.md"
    if not task_text and task_path.is_file():
        task_text = task_path.read_text(encoding="utf-8", errors="replace")
    memory_context = task_memory_context(run_dir)
    paths = code_task_memory_paths(run_dir)
    refs = [_artifact_ref_inside_run(run_dir, path) for path in (
        paths.task_memory_md, paths.compressed_memory_md,
        paths.review_findings_jsonl, paths.repair_memory_jsonl)]
    return task_text, memory_context, [ref for ref in refs if ref]


def build_code_task_handoff(
    run_dir: Path,
    *,
    name: str = "code-task",
    task_text: str = "",
    permission_policy: AgentPermissionPolicy | None = None,
) -> AgentHandoffPackage:
    """Create a handoff package from the current code-task artifacts."""
    run_dir = Path(run_dir)
    task_text, memory_context, memory_refs = _code_task_context(run_dir, task_text)
    refs = [
        "code_task/task.md",
        "code_task/work_plan.md",
        "code_task/patch_plan.md",
        "code_task/summary.md",
        "code_task/meta/environment_report.json",
        "code_task/meta/proposed_edits.json",
    ]
    refs.extend(memory_refs)
    context_files = {"task.md": task_text} if task_text else {}
    if memory_context.strip():
        context_files["task_memory.md"] = memory_context
    return create_agent_handoff(
        run_dir=run_dir,
        name=name,
        instructions=(
            "# Code Task Handoff\n\n"
            "Use the task, task memory, and listed code-task artifacts to propose or review implementation work. "
            "Treat task memory as the compact continuity record for prior plans, edits, validation, and repair attempts. "
            "Do not apply edits directly unless the permission policy allows it.\n\n"
            "## User Task\n\n"
            f"{task_text.strip() or '(No task.md was found.)'}\n"
        ),
        permission_policy=permission_policy,
        expected_outputs={
            "mode": "code_task",
            "allowed_outputs": ["review.md", "patch.diff", "proposed_edits.json", "agent_result.json"],
            "canonical_result": "agent_result.json",
        },
        artifact_refs=refs,
        context_files=context_files,
        registry=default_tool_registry(include_report=False, include_experiment=False, include_code_task=True),
    )


def build_code_task_greenfield_handoff(
    run_dir: Path,
    *,
    name: str = "code-task-greenfield",
    task_text: str = "",
    permission_policy: AgentPermissionPolicy | None = None,
    extra_instructions: str = "",
    context_files: dict[str, str] | None = None,
) -> AgentHandoffPackage:
    """Create a generated-files handoff package for greenfield code-task runs."""
    run_dir = Path(run_dir)
    task_text, memory_context, memory_refs = _code_task_context(run_dir, task_text)
    refs = [
        "code_task/task.md",
        "code_task/summary.md",
        "code_task/meta/environment_report.json",
        "code_task/meta/resource_probe.json",
        "code_task/meta/resource_decision.json",
        "code_task/meta/implementation_plan.json",
        "code_task/meta/architecture_plan.json",
        "code_task/meta/file_plan.json",
        "code_task/meta/repo_map_summary.md",
    ]
    refs.extend(memory_refs)
    merged_context = dict(context_files or {})
    if task_text.strip():
        merged_context.setdefault("task.md", task_text)
    if memory_context.strip():
        merged_context.setdefault("task_memory.md", memory_context)
    return create_agent_handoff(
        run_dir=run_dir,
        name=name,
        instructions=(
            "# Code Task Greenfield Handoff\n\n"
            "Generate the project requested by the code-task contract. "
            "Write source files only under `generated_files/`; do not write into "
            "`code_task/workspace/` directly. SimpleAutoResearch will ingest, copy, "
            "review, validate, and run the generated project.\n\n"
            "Use task memory as the compact continuity record for task constraints, "
            "prior generation attempts, review findings, and repair context.\n\n"
            "## User Task\n\n"
            f"{task_text.strip() or '(No task.md was found.)'}\n"
            + (f"\n## Additional Attempt Context\n\n{extra_instructions.strip()}\n" if extra_instructions.strip() else "")
        ),
        permission_policy=permission_policy,
        expected_outputs={
            "mode": "code_task_greenfield",
            "allowed_outputs": ["generated_files/", "review.md", "agent_result.json"],
            "canonical_result": "agent_result.json",
        },
        artifact_refs=refs,
        context_files=merged_context,
        registry=default_tool_registry(include_report=False, include_experiment=False, include_code_task=True),
    )


def build_greenfield_handoff(
    run_dir: Path,
    *,
    name: str = "greenfield",
    permission_policy: AgentPermissionPolicy | None = None,
    extra_instructions: str = "",
    context_files: dict[str, str] | None = None,
) -> AgentHandoffPackage:
    """Create a handoff package from greenfield experiment artifacts."""
    refs = [
        "01-plan/contract.json",
        "01-plan/report.md",
        "04-synthesize/synthesis_brief.json",
        "04-synthesize/report.md",
        "05-design/experiment_contract.json",
        "05-design/result_schema.json",
        "05-design/resource_plan.json",
        "05-design/domain_profile.json",
        "06-code/architecture_plan.json",
    ]
    return create_agent_handoff(
        run_dir=run_dir,
        name=name,
        instructions=(
            "# Greenfield Experiment Handoff\n\n"
            "Use the experiment contract, resource plan, and result schema to implement or review a generated project. "
            "Write canonical outputs only under the handoff output paths and let SimpleAutoResearch validate them.\n"
            + (f"\n## Additional Attempt Context\n\n{extra_instructions.strip()}\n" if extra_instructions.strip() else "")
        ),
        permission_policy=permission_policy,
        expected_outputs={
            "mode": "greenfield_experiment",
            "allowed_outputs": ["generated_files/", "review.md", "results.json", "agent_result.json"],
            "canonical_result": "agent_result.json",
        },
        artifact_refs=refs,
        context_files=context_files,
        registry=default_tool_registry(include_report=False, include_experiment=True, include_code_task=False),
    )


def ingest_agent_outputs(
    *,
    run_dir: Path,
    handoff_dir: Path,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Collect untrusted backend outputs into a reviewable run artifact folder.

    This deliberately does not apply patches or treat generated files as validated.
    """
    run_dir = Path(run_dir).resolve()
    handoff_dir = Path(handoff_dir).resolve()
    try:
        handoff_dir.relative_to(run_dir)
    except ValueError as exc:
        raise ValueError("handoff_dir must be inside the run directory for safe ingestion.") from exc
    output_dir = output_dir or (run_dir / "agent_outputs" / handoff_dir.name)
    output_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, Any]] = []
    for rel in ("agent_result.json", "review.md", "patch.diff", "proposed_edits.json", "results.json"):
        src = handoff_dir / rel
        if src.is_file():
            copied.append(_copy_file(src, output_dir / rel, run_dir))
    src_generated = handoff_dir / "generated_files"
    if src_generated.is_dir():
        dst_generated = output_dir / "generated_files"
        if dst_generated.exists():
            shutil.rmtree(dst_generated)
        shutil.copytree(src_generated, dst_generated)
        copied.append(
            {
                "path": _rel(run_dir, dst_generated),
                "kind": "dir",
                "file_count": sum(1 for path in dst_generated.rglob("*") if path.is_file()),
            }
        )
    output_snapshot = _agent_output_snapshot(output_dir)
    normalized = _normalize_agent_outputs(output_dir, output_snapshot)
    write_json(output_dir / "output_snapshot.json", output_snapshot)
    write_json(output_dir / "normalized_outputs.json", normalized)
    summary = {
        "schema_version": "agent_output_ingestion.v1",
        "ingested_at": _utcnow_iso(),
        "handoff_dir": _rel(run_dir, handoff_dir),
        "output_dir": _rel(run_dir, output_dir),
        "artifacts": copied,
        "status": "ok" if copied else "empty",
        "validation_required": True,
        "snapshot": _rel(run_dir, output_dir / "output_snapshot.json"),
        "normalized_outputs": _rel(run_dir, output_dir / "normalized_outputs.json"),
        "changed_files": normalized.get("changed_files", []),
    }
    write_json(output_dir / "ingestion.json", summary)
    return summary


def load_profile(name: str) -> str:
    safe = _safe_name(name)
    path = Path(__file__).parent / "profiles" / f"{safe}.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return (
        f"# {safe} External Backend Profile\n\n"
        "Follow the handoff instructions, permission policy, and expected output schema. "
        "All outputs remain untrusted until SimpleAutoResearch validates them.\n"
    )


def _render_instructions(
    *,
    instructions: str,
    profile_name: str,
    profile_text: str,
    expected_outputs: dict[str, Any],
    permission_policy: AgentPermissionPolicy,
) -> str:
    outputs = "\n".join(f"- `{item}`" for item in expected_outputs.get("allowed_outputs", []))
    policy_notes = "\n".join(f"- {note}" for note in permission_policy.notes)
    write_patterns = "\n".join(f"- `{item}`" for item in permission_policy.allowed_write_patterns)
    protected_patterns = "\n".join(f"- `{item}`" for item in permission_policy.protected_patterns)
    return (
        "# SimpleAutoResearch Agent Handoff\n\n"
        f"Generated: `{_utcnow_iso()}`\n\n"
        "## Backend Profile\n\n"
        f"Profile: `{profile_name}`\n\n"
        f"{profile_text.strip()}\n\n"
        "## Task Instructions\n\n"
        f"{instructions.strip()}\n\n"
        "## Expected Outputs\n\n"
        f"Canonical result: `{expected_outputs.get('canonical_result', 'agent_result.json')}`\n\n"
        f"{outputs or '- No write outputs are allowed by default.'}\n\n"
        "## Permission Summary\n\n"
        f"- File write: `{permission_policy.allow_file_write}`\n"
        f"- Shell commands: `{permission_policy.allow_shell_commands}`\n"
        f"- Network: `{permission_policy.allow_network}`\n"
        f"- Secret access: `{permission_policy.allow_secret_access}`\n"
        f"\nAllowed write patterns:\n{write_patterns or '- None'}\n\n"
        f"Protected patterns:\n{protected_patterns or '- None'}\n\n"
        f"{policy_notes}\n\n"
        "## Non-Negotiable Boundary\n\n"
        "Do not bypass SimpleAutoResearch validation. Patches, generated files, and results are proposals until the main workflow validates them.\n"
    )


def _default_expected_outputs() -> dict[str, Any]:
    return {
        "mode": "review_only",
        "allowed_outputs": ["review.md", "agent_result.json"],
        "canonical_result": "agent_result.json",
    }


def _artifact_handles(run_dir: Path, refs: Iterable[str]) -> dict[str, Any]:
    handles = []
    for ref in refs:
        safe = _safe_relative(ref)
        if not safe:
            continue
        path = run_dir / safe
        item: dict[str, Any] = {"path": safe, "exists": path.exists()}
        if path.is_file():
            item["bytes"] = path.stat().st_size
        handles.append(item)
    return {"schema_version": "artifact_handles.v1", "artifacts": handles}


def _workspace_manifest(run_dir: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for path in sorted(run_dir.iterdir()) if run_dir.is_dir() else []:
        if path.name in {"agent_handoff", "agent_outputs"}:
            continue
        item: dict[str, Any] = {"path": path.name, "kind": "dir" if path.is_dir() else "file"}
        if path.is_file():
            item["bytes"] = path.stat().st_size
        entries.append(item)
    return {"schema_version": "workspace_manifest.v1", "run_dir": str(run_dir), "entries": entries}


def _copy_file(src: Path, dst: Path, run_dir: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"path": _rel(run_dir, dst), "kind": "file", "bytes": dst.stat().st_size}


def _agent_output_snapshot(output_dir: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(output_dir).as_posix()
        if rel in {"output_snapshot.json", "normalized_outputs.json"}:
            continue
        rows.append(
            {
                "path": rel,
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
                "line_count": _line_count(path),
            }
        )
    return {
        "schema_version": "agent_output_snapshot.v1",
        "created_at": _utcnow_iso(),
        "root": str(output_dir),
        "files": rows,
    }


def _normalize_agent_outputs(output_dir: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    patch_path = output_dir / "patch.diff"
    proposed_path = output_dir / "proposed_edits.json"
    generated_dir = output_dir / "generated_files"
    result_path = output_dir / "agent_result.json"
    review_path = output_dir / "review.md"
    results_path = output_dir / "results.json"
    changed = _parse_patch_changed_files(patch_path)
    changed.extend(_parse_proposed_edit_files(proposed_path))
    if generated_dir.is_dir():
        changed.extend(
            f"generated_files/{path.relative_to(generated_dir).as_posix()}"
            for path in sorted(generated_dir.rglob("*"))
            if path.is_file()
        )
    changed = _dedupe(changed)
    return {
        "schema_version": "agent_normalized_outputs.v1",
        "created_at": _utcnow_iso(),
        "output_dir": str(output_dir),
        "changed_files": changed,
        "outputs": {
            "patch": _output_file_row(patch_path),
            "proposed_edits": _output_file_row(proposed_path),
            "generated_files": {
                "path": "generated_files",
                "exists": generated_dir.is_dir(),
                "file_count": sum(1 for path in generated_dir.rglob("*") if path.is_file()) if generated_dir.is_dir() else 0,
            },
            "review": _output_file_row(review_path),
            "results": _output_file_row(results_path),
            "agent_result": _output_file_row(result_path),
        },
        "snapshot_file_count": len(snapshot.get("files", [])) if isinstance(snapshot.get("files"), list) else 0,
    }


def _output_file_row(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"path": path.name, "exists": False}
    return {
        "path": path.name,
        "exists": True,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _parse_patch_changed_files(path: Path) -> list[str]:
    if not path.is_file():
        return []
    rows: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("+++ b/"):
            rows.append(line.removeprefix("+++ b/").strip())
        elif line.startswith("--- a/"):
            rows.append(line.removeprefix("--- a/").strip())
    return [row for row in rows if row and row != "/dev/null"]


def _parse_proposed_edit_files(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    edits = data.get("edits") if isinstance(data, dict) else None
    if not isinstance(edits, list):
        return []
    return [str(row.get("path", "")).strip() for row in edits if isinstance(row, dict) and str(row.get("path", "")).strip()]


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _line_count(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def _dedupe(values: Iterable[str]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).replace("\\", "/").strip()
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _archive_existing_handoff(handoff_dir: Path, run_dir: Path) -> None:
    if not handoff_dir.exists():
        return
    try:
        has_contents = any(handoff_dir.iterdir())
    except OSError:
        has_contents = True
    if not has_contents:
        shutil.rmtree(handoff_dir)
        return
    # Keep stale handoff transcripts out of the next external-agent workspace.
    # Codex/Claude-style agents may inspect nearby sibling directories even when
    # asked not to, so old stdout/stderr belongs in the ignored local cache.
    archive_root = _handoff_archive_root(run_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    base_name = f"{handoff_dir.name}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    target = archive_root / base_name
    suffix = 1
    while target.exists():
        suffix += 1
        target = archive_root / f"{base_name}-{suffix}"
    shutil.move(str(handoff_dir), str(target))


def _relocate_legacy_handoff_archives(run_dir: Path) -> None:
    legacy = run_dir / "agent_handoff" / "archives"
    if not legacy.exists():
        return
    archive_root = _handoff_archive_root(run_dir)
    archive_root.mkdir(parents=True, exist_ok=True)
    target = archive_root / f"legacy-archives-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    suffix = 1
    while target.exists():
        suffix += 1
        target = archive_root / f"{target.name}-{suffix}"
    shutil.move(str(legacy), str(target))


def _handoff_archive_root(run_dir: Path) -> Path:
    run_key = _safe_name(f"{run_dir.parent.name}-{run_dir.name}")
    return Path(".simple_ar_cache") / "agent_handoff_archives" / run_key


def _safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip().lower())
    return cleaned.strip("-") or "default"


def _safe_relative(value: str) -> str:
    value = value.replace("\\", "/").strip().lstrip("/")
    if not value or any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    return value


def _rel(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _artifact_ref_inside_run(base: Path, path: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return ""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
