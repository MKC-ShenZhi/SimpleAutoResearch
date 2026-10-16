from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from simple_ar.core.artifacts import read_json, read_text, write_json
from simple_ar.code_task.editing.scope import (
    allowed_patterns_from_manifest,
    editable_context_files as _editable_repair_context_files,
    edit_scope_rejection_reason,
    is_edit_allowed_path,
    protected_patterns_from_manifest,
)
from simple_ar.code_task.analysis.interfaces import render_source_snippets
from simple_ar.code_task.analysis.source_context import diff_source_anchors, source_context_for_files
from simple_ar.code_task.execution.failure import FILE_LINE_RE, analyze_code_task_failure
from simple_ar.code_task.editing.planning import select_relevant_files, patch_plan_context
from simple_ar.code_task.editing.work_plan import _creation_paths
from simple_ar.code_task.editing.actions import looks_like_diff_fragment as _looks_like_diff_fragment
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    manifest_section,
    read_required_json as _read_required_json,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.execution.summary import write_code_task_summary
from simple_ar.code_task.memory import task_memory_context
from simple_ar.integrations.llm import LLMClient
from simple_ar.integrations.usage import record_usage


CODE_TASK_REPAIR_SYSTEM = (
    "You are a careful senior engineer proposing a minimal repair patch for "
    "an isolated code-task workspace. Use the failure analysis, execution "
    "report, patch plan, and supplied source snippets. Return only JSON. Do "
    "not broaden the change unless the observed failure requires it. The "
    "previous plan is a hypothesis, not proof: correct its diagnosis when "
    "source and runtime evidence contradict it."
)

MessageCallback = Callable[[str], None]


@dataclass(frozen=True)
class RepairEvidence:
    """Observed failure supplied by the owner of an external measurement.

    This is input evidence, not a replacement CodeTask benchmark record.
    Scientific regressions with a successful process are not repair failures.
    """

    source: str
    execution_report: dict[str, Any]
    failure_analysis: str

    def __post_init__(self) -> None:
        if not self.source.strip() or not self.failure_analysis.strip():
            raise ValueError("Repair evidence requires a source and failure details.")
        preflight = self.execution_report.get('preflight', {})
        creation_blocked = (self.execution_report["execution_status"] == 'blocked_by_validation'
            and preflight.get('reason') == 'required_new_files' and bool(preflight.get('missing_files'))
            and self.execution_report.get('returncode') is None)
        if self.execution_report["execution_status"] not in {"failed", "timed_out"} and not creation_blocked:
            raise ValueError("Repair requires a failed execution, not a valid low-scoring result.")


@dataclass(frozen=True)
class RepairProposalResult:
    """Result returned after proposing a bounded repair.

    Args:
        run_dir: Code-task run directory.
        repair_dir: Directory for this repair attempt.
        proposal_path: JSON edit proposal path.
        mode: ``llm`` when model output was used, otherwise ``offline``.
        edit_count: Number of normalized repair edits.
        selected_files: Workspace-relative source files included as context.
    """

    run_dir: Path
    repair_dir: Path
    proposal_path: Path
    mode: str
    edit_count: int
    selected_files: tuple[str, ...]


def propose_repair_edits(
    run_dir: Path,
    *,
    model: str | None = None,
    llm_client: LLMClient | None = None,
    use_llm: bool = True,
    max_files: int = 8,
    max_source_chars_per_file: int = 4000,
    message_callback: MessageCallback | None = None,
    failure_evidence: RepairEvidence | None = None,
) -> RepairProposalResult:
    """Propose a bounded repair edit set from the latest failed run.

    The function writes a repair proposal but does not apply it. Keeping
    repair application explicit preserves the same human-reviewable patch gate
    as the initial edit flow.

    Args:
        run_dir: Code-task run directory.
        model: Optional OpenAI-compatible model override.
        use_llm: Whether to call the configured LLM provider.
        max_files: Maximum source files included as repair context.
        max_source_chars_per_file: Per-file source snippet budget.
        message_callback: Optional progress callback.
        failure_evidence: Explicit external failure; skips legacy run discovery.

    Returns:
        Repair proposal metadata.
    """
    manifest = load_code_task_manifest(run_dir)
    paths = code_task_paths(run_dir)
    if failure_evidence is None:
        artifacts = _latest_run_artifacts(paths, manifest)
        analysis_path = artifacts["failure_analysis"]
        if not analysis_path.exists():
            analysis = analyze_code_task_failure(run_dir)
            if analysis.status == "no_failure":
                raise RuntimeError("Latest benchmark passed; repair proposal is not needed.")
            manifest = load_code_task_manifest(run_dir)
            artifacts = _latest_run_artifacts(paths, manifest)
            analysis_path = artifacts["failure_analysis"]
        failure_analysis = read_text(analysis_path)
        execution_report = _read_optional_json(artifacts.get("execution_report"))
        validation_report = _read_optional_json(artifacts.get("validation_report"))
        if execution_report.get("status") == "passed" and not _validation_failed(validation_report):
            raise RuntimeError("Latest benchmark passed; repair proposal is not needed.")
        source_analysis = analysis_path.relative_to(paths.run_dir).as_posix()
    else:
        failure_analysis = failure_evidence.failure_analysis
        execution_report = failure_evidence.execution_report
        validation_report = {}  # No second validation outcome is invented.

    task_text = _read_optional_text(paths.task_dir / "task.md")
    patch_plan = _read_optional_text(paths.task_dir / "patch_plan.md")
    patch_diff = _read_optional_text(paths.task_dir / "patch.diff")
    memory_context = task_memory_context(run_dir)
    index = _read_required_json(paths.meta_dir / "codebase_index.json")
    allowed_patterns = allowed_patterns_from_manifest(manifest)
    protected_patterns = protected_patterns_from_manifest(manifest)
    new_files = _missing_creation_paths(paths.workspace_dir,
        _creation_paths(manifest, run_dir=run_dir),
        allowed_patterns=allowed_patterns, protected_patterns=protected_patterns)[:max(1, max_files)]
    selected_context = _repair_context_files(
        manifest,
        index,
        task_text=task_text,
        failure_analysis=failure_analysis,
        max_files=max_files,
    )
    selected = _editable_repair_context_files(
        index,
        selected_context,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
        max_files=max_files - len(new_files),
    ) if max_files > len(new_files) else []
    read_only_context = [
        path
        for path in selected_context
        if not is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
    ]
    snippets = source_context_for_files(
        paths.workspace_dir,
        selected + read_only_context[:max(0, max_files - len(selected) - len(new_files))],
        max_chars_per_file=max_source_chars_per_file,
        anchors=_repair_source_anchors(failure_analysis, patch_diff, validation_report),
        editable_files=selected,
    )

    repair_dir = _next_repair_dir(paths.repairs_dir)
    repair_dir.mkdir(parents=True, exist_ok=False)
    proposal_path = repair_dir / "proposed_edits.json"
    if failure_evidence is not None:
        evidence_path = repair_dir / "failure_evidence.json"
        write_json(evidence_path, {
            "source": failure_evidence.source, "execution_report": execution_report,
            "failure_analysis": failure_analysis,
        })
        source_analysis = evidence_path.relative_to(paths.run_dir).as_posix()

    mode = "offline"
    if use_llm:
        _emit(message_callback, "Calling LLM for bounded repair proposal.")
        client = LLMClient.for_task(
            client=llm_client,
            model=model,
            usage_callback=lambda usage: record_usage(
                paths.meta_dir,
                usage,
                stage="code_task.repair",
                message_callback=message_callback,
            ),
        )
        proposal = client.ask_json(
            CODE_TASK_REPAIR_SYSTEM,
            _repair_prompt(
                task_text=task_text,
                patch_plan=patch_plan,
                patch_diff=patch_diff,
                memory_context=memory_context,
                failure_analysis=failure_analysis,
                execution_report=execution_report,
                validation_report=validation_report,
                snippets=snippets,
                read_only_context=read_only_context,
                new_files=new_files,
            ),
            label="code-task-repair",
        )
        mode = "llm"
    else:
        proposal = _offline_repair(selected)

    normalized = _normalize_repair_proposal(
        proposal,
        index=index,
        mode=mode,
        selected_files=selected,
        read_only_context=read_only_context,
        source_analysis=source_analysis,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
        new_files=new_files,
        workspace_dir=paths.workspace_dir,
    )
    write_json(proposal_path, normalized)
    _update_manifest_after_repair(
        run_dir,
        manifest,
        repair_dir=repair_dir,
        edit_count=len(normalized["edits"]),
        selected_files=selected,
    )
    write_code_task_summary(run_dir)
    return RepairProposalResult(
        run_dir=paths.run_dir,
        repair_dir=repair_dir,
        proposal_path=proposal_path,
        mode=mode,
        edit_count=len(normalized["edits"]),
        selected_files=tuple(selected),
    )


def _repair_prompt(
    *,
    task_text: str,
    patch_plan: str,
    patch_diff: str,
    memory_context: str,
    failure_analysis: str,
    execution_report: dict[str, Any],
    validation_report: dict[str, Any],
    snippets: list[dict[str, Any]],
    read_only_context: list[str],
    new_files: list[str] | None = None,
) -> str:
    snippet_text = render_source_snippets(snippets)
    return (
        "Return JSON with fields: `summary` string, `edits` list, "
        "`validation` list of strings, and `risks` list of strings.\n\n"
        "Each edit must contain `path`, `old`, `new`, and `reason` string fields.\n\n"
        "Rules:\n"
        "- Use exact old/new text replacements only.\n"
        "- Do not include diff markers such as `+`, `-`, `@@`, `---`, or "
        "`+++` inside `old` or `new`; they must contain only file text.\n"
        "- Edit only workspace-relative paths marked editable in the supplied snippets, or the "
        "declared missing creation targets below. Create an absent target with old=\"\" and nonempty new text.\n"
        "- Prefer repairing implicated or recently changed files.\n"
        "- Treat validation errors as first-class evidence even when the benchmark was not launched.\n"
        "- Do not change read-only files such as tests, benchmarks, or validation targets.\n"
        "- Keep the repair minimal and runnable.\n"
        "- Distinguish measured symptoms from suspected causes; do not preserve a disproven plan.\n"
        "- Partial source is not a full implementation; do not invent unseen code.\n"
        "- Do not return markdown or a unified diff.\n\n"
        f"Task:\n{task_text or 'No task text found.'}\n\n"
        f"Patch plan:\n{patch_plan_context(patch_plan, task_text) or 'No patch plan found.'}\n\n"
        f"Current patch diff:\n```diff\n{patch_diff or 'No patch diff found.'}\n```\n\n"
        f"Task memory:\n{memory_context}\n\n"
        f"Execution report JSON:\n{json.dumps(execution_report or {'status': 'not_available'}, indent=2, ensure_ascii=False)}\n\n"
        f"Validation report JSON:\n{json.dumps(validation_report or {'status': 'not_available'}, indent=2, ensure_ascii=False)}\n\n"
        f"Failure analysis:\n{failure_analysis}\n\n"
        "Declared missing creation targets (no source exists; these are not snippets):\n"
        f"{json.dumps(new_files or [], indent=2, ensure_ascii=False)}\n\n"
        "Read-only context paths (never editable; source shown within the same file budget):\n"
        f"{json.dumps(read_only_context, indent=2, ensure_ascii=False)}\n\n"
        f"Selected source snippets:\n{snippet_text or 'No source snippets selected.'}"
    )


def _repair_context_files(
    manifest: dict[str, Any],
    index: dict[str, Any],
    *,
    task_text: str,
    failure_analysis: str,
    max_files: int,
) -> list[str]:
    known_paths = {str(item.get("path", "")) for item in _index_files(index)}
    selected: list[str] = []
    patch = manifest.get("patch", {})
    if isinstance(patch, dict):
        for path in patch.get("changed_files", []):
            if isinstance(path, str) and path in known_paths and path not in selected:
                selected.append(path)
    failure = manifest.get("failure_analysis", {})
    if isinstance(failure, dict):
        for path in failure.get("implicated_files", []):
            if isinstance(path, str) and path in known_paths and path not in selected:
                selected.append(path)
    for path in select_relevant_files(index, task_text + "\n" + failure_analysis, max_files=max_files):
        if path not in selected:
            selected.append(path)
    return selected[: max(1, max_files)]


def _repair_source_anchors(
    failure_analysis: str, patch_diff: str, validation_report: dict[str, Any],
) -> list[tuple[str, int]]:
    """Prioritize observed traceback/static lines, then current unified-diff hunks."""
    anchors = [(path.replace("\\", "/"), int(line))
               for path, line, _ in FILE_LINE_RE.findall(failure_analysis)]
    anchors.extend((str(issue.get("path", "")).replace("\\", "/"), issue["line"])
        for issue in validation_report.get("issues", []) if isinstance(issue, dict)
        and type(issue.get("line")) is int and issue["line"] > 0)
    return anchors + diff_source_anchors(patch_diff)


def _offline_repair(selected_files: list[str]) -> dict[str, Any]:
    return {
        "summary": "Offline mode does not invent repair edits. Review the failure analysis and provide an edits JSON file.",
        "edits": [],
        "validation": [
            "No repair edits were generated because --no-llm was used.",
        ],
        "risks": [
            "Manual repair edits should still be applied through code-task apply-edits and re-run.",
        ],
        "selected_files": selected_files,
    }


def _normalize_repair_proposal(
    proposal: dict[str, Any],
    *,
    index: dict[str, Any],
    mode: str,
    selected_files: list[str],
    read_only_context: list[str],
    source_analysis: str,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
    new_files: list[str] | None = None,
    workspace_dir: Path | None = None,
) -> dict[str, Any]:
    known_paths = {str(item.get("path", "")) for item in _index_files(index)}
    permitted_paths = set(selected_files) if selected_files or new_files else known_paths
    creating_paths = set(_missing_creation_paths(workspace_dir, new_files or [],
        allowed_patterns=allowed_patterns, protected_patterns=protected_patterns)) if workspace_dir is not None else set()
    warnings: list[str] = []
    edits: list[dict[str, str]] = []
    for item in proposal.get("edits", []):
        if not isinstance(item, dict):
            warnings.append("Dropped non-object edit.")
            continue
        path = _string(item.get("path"))
        old = item.get("old")
        new = item.get("new")
        creating = path in creating_paths and old == "" and isinstance(new, str) and bool(new.strip())
        if path not in known_paths and not creating:
            warnings.append(f"Dropped edit for unknown path: {path or '<empty>'}")
            continue
        rejection = edit_scope_rejection_reason(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
        if rejection:
            warnings.append(f"Dropped edit for disallowed path `{path}`: {rejection}")
            continue
        if path not in permitted_paths and not creating:
            warnings.append(f"Dropped edit outside repair context: {path}")
            continue
        if not isinstance(old, str) or not isinstance(new, str):
            warnings.append(f"Dropped edit for {path}: old/new must be strings.")
            continue
        if _looks_like_diff_fragment(old) or _looks_like_diff_fragment(new):
            warnings.append(
                f"Dropped edit for {path}: old/new must be exact text, not a diff fragment."
            )
            continue
        if old == new:
            warnings.append(f"Dropped edit for {path}: old and new are identical.")
            continue
        edits.append(
            {
                "path": path,
                "old": old,
                "new": new,
                "reason": _string(item.get("reason")) or "No reason provided.",
            }
        )
    return {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "kind": "repair_proposal",
        "mode": mode,
        "source_analysis": source_analysis,
        "selected_files": selected_files,
        "read_only_context": read_only_context,
        "constraints": [
            "Only selected context files or declared absent creation targets may be edited by this repair proposal.",
            "Protected read-only files such as tests and benchmarks cannot be edited.",
            "Apply this proposal explicitly with code-task apply-edits after review.",
        ],
        "summary": _string(proposal.get("summary")) or "No summary provided.",
        "edits": edits,
        "validation": _string_list(proposal.get("validation")),
        "risks": _string_list(proposal.get("risks")),
        "warnings": warnings,
    }


def _missing_creation_paths(workspace: Path, paths: list[str], *,
                            allowed_patterns: tuple[str, ...], protected_patterns: tuple[str, ...]) -> list[str]:
    result = []
    for name in paths:
        if not is_edit_allowed_path(name, allowed_patterns=allowed_patterns, protected_patterns=protected_patterns):
            continue
        target = workspace / name
        try:
            if (target.exists() or not target.resolve().is_relative_to(workspace.resolve())
                    or any(path.is_symlink() or path.is_junction() or path.is_file()
                           for path in (target, *target.parents) if path != workspace and workspace in path.parents)):
                continue
        except (OSError, RuntimeError):
            continue
        result.append(name)
    return result


def _next_repair_dir(repairs_dir: Path) -> Path:
    repairs_dir.mkdir(parents=True, exist_ok=True)
    existing = [
        path
        for path in repairs_dir.iterdir()
        if path.is_dir() and path.name.startswith("repair-")
    ]
    numbers: list[int] = []
    for path in existing:
        try:
            numbers.append(int(path.name.removeprefix("repair-")))
        except ValueError:
            continue
    return repairs_dir / f"repair-{(max(numbers) + 1 if numbers else 1):03d}"


def _update_manifest_after_repair(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    repair_dir: Path,
    edit_count: int,
    selected_files: list[str],
) -> None:
    root = code_task_paths(run_dir).run_dir
    rel_repair_dir = repair_dir.relative_to(root).as_posix()
    layout = manifest_section(manifest, "layout")
    layout["repairs"] = "code_task/repairs"
    repair = manifest_section(manifest, "repair")
    previous_count = int(repair.get("repair_count", 0) or 0)
    repair.update(
        {
            "status": "repair_proposed",
            "generated_at": utcnow_iso(),
            "repair_count": previous_count + 1,
            "latest_repair_dir": rel_repair_dir,
            "latest_proposed_edits": f"{rel_repair_dir}/proposed_edits.json",
            "latest_edit_count": edit_count,
            "selected_files": selected_files,
        }
    )
    manifest["layout"] = layout
    manifest["repair"] = repair
    manifest["status"] = "repair_proposed"
    save_code_task_manifest(run_dir, manifest)




def _read_optional_json(path: object) -> dict[str, Any]:
    if not isinstance(path, Path) or not path.exists():
        return {}
    value = read_json(path)
    if not isinstance(value, dict):
        return {}
    return value


def _read_optional_text(path: Path) -> str:
    return read_text(path) if path.exists() else ""


def _latest_run_artifacts(paths: Any, manifest: dict[str, Any]) -> dict[str, Any]:
    validation_report = paths.meta_dir / "validation_report.json"
    benchmark = manifest.get("benchmark", {})
    if isinstance(benchmark, dict):
        report_rel = benchmark.get("execution_report")
        if report_rel:
            report = paths.run_dir / str(report_rel)
            return {
                "execution_report": report,
                "validation_report": validation_report,
                "failure_analysis": report.parent / "failure_analysis.md",
            }
    return {
        "execution_report": None,
        "validation_report": validation_report,
        "failure_analysis": paths.meta_dir / "failure_analysis.md",
    }


def _index_files(index: dict[str, Any]) -> list[dict[str, Any]]:
    files = index.get("files", [])
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, dict)]


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_string(item) for item in value if _string(item)]


def _validation_failed(validation: dict[str, Any]) -> bool:
    return validation.get("status") == "failed" or _int_value(validation.get("error_count")) > 0


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
