from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from simple_ar.core.artifacts import append_jsonl, read_json, read_text, write_json, write_text
from simple_ar.code_task.editing.scope import (
    allowed_patterns_from_manifest,
    is_edit_allowed_path,
    protected_patterns_from_manifest,
    prompt_file_inventory,
)
from simple_ar.code_task.runtime.state import (
    code_task_paths, read_required_json as _read_required_json, utcnow_iso as _utcnow_iso,
)
from simple_ar.code_task.analysis.context import (
    LoadedCodeTaskContextPack,
    ensure_code_task_context_pack,
    clip_source_snippet,
    load_latest_code_task_context_pack,
    read_source_snippets,
)
from simple_ar.code_task.memory import task_memory_context
from simple_ar.code_task.analysis.interfaces import render_source_snippets, snippet_api_contract
from simple_ar.integrations.llm import LLMClient, LLMError, LLMResponseError
from simple_ar.code_task.generation.agent_step import (
    response_accounting_known, response_accounting_snapshot, retry_suffix, run_completed_response_corrections,
)
from simple_ar.integrations.usage import record_usage


CODE_TASK_PLAN_SYSTEM = (
    "You are a careful senior engineer planning a small, reviewable code change. "
    "You must analyze the supplied task, codebase index, and source snippets, "
    "then produce a conservative patch plan. Do not write code. Do not invent "
    "files or behavior that is not supported by the supplied context."
)

MessageCallback = Callable[[str], None]


@dataclass(frozen=True)
class PatchPlanResult:
    """Result returned after generating a code-task patch plan.

    Args:
        run_dir: Code-task run directory.
        patch_plan_path: Markdown plan written for human review.
        manifest_path: Root manifest updated with plan state.
        mode: ``llm`` when model output was used, otherwise ``offline``.
        selected_files: Workspace-relative files included in prompt context.
        pending_approval: Whether the plan still requires human approval.
    """

    run_dir: Path
    patch_plan_path: Path
    manifest_path: Path
    mode: str
    selected_files: tuple[str, ...]
    pending_approval: bool


def generate_patch_plan(
    run_dir: Path,
    *,
    model: str | None = None,
    use_llm: bool = True,
    allow_llm_fallback: bool = False,
    llm_retry_attempts: int = 1,
    force: bool = False,
    max_files: int = 8,
    max_source_chars_per_file: int = 2500,
    message_callback: MessageCallback | None = None,
    llm_client: LLMClient | None = None,
) -> PatchPlanResult:
    """Generate a reviewable patch plan for an initialized code-task run.

    Args:
        run_dir: Code-task run directory created by ``code-task init``.
        model: Optional OpenAI-compatible model override.
        use_llm: Whether to call the configured LLM provider.
        allow_llm_fallback: Permit a deterministic plan after completed format
            failures with reconciled accounting. Provider, budget, stream and
            deadline failures propagate without fallback.
        llm_retry_attempts: Total attempts for completed JSON format correction.
            Provider retries and accounting belong to LLMClient.
        force: Overwrite an existing ``patch_plan.md`` when true.
        max_files: Maximum number of relevant source files to include in the
            planning context.
        max_source_chars_per_file: Per-file source snippet character budget.
        message_callback: Optional callback for progress messages.

    Returns:
        Paths, mode, and selected context files for the generated plan.

    Raises:
        FileNotFoundError: If required code-task artifacts are missing.
        RuntimeError: If ``run_dir`` is not a code-task run.
        FileExistsError: If ``patch_plan.md`` already exists and ``force`` is
            false.
    """
    if llm_retry_attempts < 1:
        raise ValueError("llm_retry_attempts must be at least 1")
    root = Path(run_dir)
    paths = code_task_paths(root)
    task_dir = paths.task_dir
    meta_dir = paths.meta_dir
    patch_plan_path = task_dir / "patch_plan.md"
    manifest_path = paths.manifest_path
    if patch_plan_path.exists() and not force:
        raise FileExistsError(f"Patch plan already exists: {patch_plan_path}")

    manifest, inputs, selected, context_pack_ref = _prepare_planning_context(
        root, use_llm=use_llm, max_files=max_files,
        max_source_chars_per_file=max_source_chars_per_file,
        message_callback=message_callback,
    )
    inputs["benchmark_command"] = _benchmark_command(manifest)
    plan_data, mode = _generate_plan_data(
        meta_dir, inputs=inputs, selected_files=selected,
        ask=_ask_llm_for_plan, offline=_offline_plan,
        plan_name="patch", usage_stage="code_task.plan",
        model=model, use_llm=use_llm, llm_client=llm_client,
        llm_retry_attempts=llm_retry_attempts,
        allow_llm_fallback=allow_llm_fallback, message_callback=message_callback,
    )

    markdown = _render_patch_plan(
        plan_data,
        task_text=inputs["task_text"],
        selected_files=selected,
        run_context=inputs["run_context"],
        context_pack=context_pack_ref,
        mode=mode,
    )
    write_text(patch_plan_path, markdown)
    _update_manifest_after_plan(
        manifest_path,
        manifest,
        selected_files=selected,
        run_context=inputs["run_context"],
        context_pack=context_pack_ref,
        mode=mode,
    )
    return PatchPlanResult(
        run_dir=root,
        patch_plan_path=patch_plan_path,
        manifest_path=manifest_path,
        mode=mode,
        selected_files=tuple(selected),
        pending_approval=True,
    )


def _prepare_planning_context(
    run_dir: Path,
    *,
    use_llm: bool,
    max_files: int,
    max_source_chars_per_file: int,
    message_callback: MessageCallback | None,
) -> tuple[dict[str, Any], dict[str, Any], list[str], dict[str, Any] | None]:
    """Prepare the shared inputs for both planning entry points.

    Offline planning only reads saved context. The pack owner may update the
    manifest online, so return its latest snapshot for the eventual plan write.
    """
    paths = code_task_paths(run_dir)
    manifest = _load_code_task_manifest(paths.manifest_path)
    task_text = _read_required_text(paths.task_dir / "task.md")
    index = _read_required_json(paths.meta_dir / "codebase_index.json")
    allowed_patterns = allowed_patterns_from_manifest(manifest)
    protected_patterns = protected_patterns_from_manifest(manifest)
    run_context = _collect_run_context(run_dir, manifest)
    memory_context = task_memory_context(run_dir)
    if use_llm:
        loaded = ensure_code_task_context_pack(
            run_dir, query=task_text, max_files=max_files,
            max_source_chars_per_file=max_source_chars_per_file,
        )
        manifest = _load_code_task_manifest(paths.manifest_path)
    else:
        loaded = load_latest_code_task_context_pack(run_dir)

    selected: list[str] = []
    snippets: list[dict[str, Any]] = []
    context_pack_ref = None
    if loaded is not None and loaded.selected_files:
        selected = loaded.selected_paths(max_files=max_files)
        snippets = _context_pack_snippets(
            loaded, max_files=max_files,
            max_chars_per_file=max_source_chars_per_file,
        )
        context_pack_ref = loaded.manifest_reference(run_dir)
        _emit(message_callback, f"Using code-task context pack: {context_pack_ref['path']}")
        if not snippets:
            _emit(message_callback, "Context pack has no readable snippets; falling back to index selection.")

    if not selected or not snippets:
        selected = select_relevant_files(index, task_text, max_files=max_files)
        snippets = read_source_snippets(
            paths.workspace_dir, selected,
            max_chars_per_file=max_source_chars_per_file,
            editable_files=[path for path in selected if is_edit_allowed_path(
                path, allowed_patterns=allowed_patterns, protected_patterns=protected_patterns)],
        )
        # The legacy source reader has a 200-character floor. Planning must
        # still honor smaller caller budgets, including patch-plan's 180.
        snippets = [clip_source_snippet(row, max_chars=max(1, max_source_chars_per_file))
                    for row in snippets]
        context_pack_ref = None

    return manifest, {
        "task_text": task_text,
        "index": index,
        "snippets": snippets,
        "run_context": run_context,
        "memory_context": memory_context,
        "allowed_patterns": allowed_patterns,
        "protected_patterns": protected_patterns,
    }, selected, context_pack_ref


def _generate_plan_data(
    meta_dir: Path,
    *,
    inputs: dict[str, Any],
    selected_files: list[str],
    ask: Callable[..., dict[str, Any]],
    offline: Callable[..., dict[str, Any]],
    plan_name: str,
    usage_stage: str,
    model: str | None,
    use_llm: bool,
    llm_client: LLMClient | None,
    llm_retry_attempts: int,
    allow_llm_fallback: bool,
    message_callback: MessageCallback | None,
) -> tuple[dict[str, Any], str]:
    """Adapt planning to the existing completed-response correction owner."""
    if use_llm:
        client = LLMClient.for_task(
            client=llm_client, model=model,
            usage_callback=lambda usage: record_usage(
                meta_dir, usage, stage=usage_stage, message_callback=message_callback),
        )

        # Keep fallback conservative across every correction in this plan unit.
        accounting_before = response_accounting_snapshot(client)

        def operation(attempt: int, last_error: LLMError | None) -> dict[str, Any]:
            suffix = f" (attempt {attempt}/{llm_retry_attempts})" if llm_retry_attempts > 1 else ""
            _emit(message_callback, f"Calling LLM for code-task {plan_name} planning{suffix}.")
            return ask(client, **inputs, retry_feedback=retry_suffix(last_error, attempt))

        def on_failure(exc: LLMError, attempt: int, correct: bool) -> None:
            if isinstance(exc, LLMResponseError):
                write_json(meta_dir / "planning_steps" / f"{plan_name}-plan-response-{attempt:03d}.json",
                    {"error": str(exc), "raw_response": exc.raw_response})
                if response_accounting_known(client, accounting_before):
                    _emit(message_callback, f"LLM {plan_name} planning attempt {attempt} failed: {exc}")

        try:
            return run_completed_response_corrections(
                client=client, operation=operation, retry_attempts=llm_retry_attempts,
                on_failure=on_failure,
            ), "llm"
        except LLMResponseError as exc:
            if not allow_llm_fallback or not response_accounting_known(client, accounting_before):
                raise
            _emit(message_callback, f"Using offline fallback after LLM {plan_name} planning failed. {exc}")

    return offline(
        task_text=inputs["task_text"], index=inputs["index"],
        selected_files=selected_files, benchmark_command=inputs["benchmark_command"],
        run_context=inputs["run_context"], allowed_patterns=inputs["allowed_patterns"],
        protected_patterns=inputs["protected_patterns"],
    ), "offline"


def record_plan_decision(
    run_dir: Path,
    *,
    decision: str,
    note: str = "",
    reviewer: str = "user",
) -> dict[str, Any]:
    """Record a human decision for the current ``patch_plan.md``.

    Args:
        run_dir: Code-task run directory.
        decision: One of ``approve``, ``reject``, or ``revise``.
        note: Optional human-readable reason or follow-up instruction.
        reviewer: Name recorded in the HITL decision log.

    Returns:
        JSON row appended to ``hitl_decisions.jsonl``.

    Raises:
        FileNotFoundError: If no patch plan exists.
        ValueError: If ``decision`` is unsupported.
    """
    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject", "revise"}:
        raise ValueError("decision must be one of: approve, reject, revise")

    root = Path(run_dir)
    task_dir = root / "code_task"
    meta_dir = task_dir / "meta"
    patch_plan_path = task_dir / "patch_plan.md"
    if not patch_plan_path.exists():
        raise FileNotFoundError(f"Missing patch plan: {patch_plan_path}")

    manifest_path = root / "manifest.json"
    manifest = _load_code_task_manifest(manifest_path)
    row = {
        "schema_version": 1,
        "created_at": _utcnow_iso(),
        "kind": "patch_plan_decision",
        "decision": normalized,
        "reviewer": reviewer,
        "note": note,
        "patch_plan": "code_task/patch_plan.md",
    }
    append_jsonl(meta_dir / "hitl_decisions.jsonl", row)

    status_by_decision = {
        "approve": "approved",
        "reject": "rejected",
        "revise": "revision_requested",
    }
    plan = _object_dict(manifest.get("plan"))
    plan["status"] = status_by_decision[normalized]
    plan["last_decision"] = row
    manifest["plan"] = plan
    work_plan = manifest.get("work_plan")
    if isinstance(work_plan, dict):
        approval = work_plan.get("approval")
        if not isinstance(approval, dict):
            approval = {}
        approval["status"] = status_by_decision[normalized]
        approval["last_decision"] = row
        work_plan["approval"] = approval
        if normalized == "approve" and work_plan.get("status") == "pending_approval":
            work_plan["status"] = "ready"
        elif normalized == "reject":
            work_plan["status"] = "rejected"
        elif normalized == "revise":
            work_plan["status"] = "revision_requested"
        manifest["work_plan"] = work_plan
    manifest["status"] = "plan_" + status_by_decision[normalized]
    write_json(manifest_path, manifest)
    return row


def select_relevant_files(
    index: dict[str, Any],
    task_text: str,
    *,
    max_files: int = 8,
) -> list[str]:
    """Select a compact source context for patch planning.

    The selector is deterministic and intentionally simple. It favors files
    whose path, summary, functions, classes, imports, or role tags overlap with
    the task, while also keeping likely entrypoints and tests visible.
    """
    terms = _terms(task_text)
    files = _index_files(index)
    scored: list[tuple[int, str]] = []
    for item in files:
        path = str(item.get("path", ""))
        if not path:
            continue
        haystack = _file_haystack(item)
        score = sum(3 for term in terms if term in haystack)
        role_tags = [str(tag) for tag in item.get("role_tags", []) if isinstance(tag, str)]
        if "entrypoint" in role_tags:
            score += 4
        if "test" in role_tags:
            score += 3
        if "source" in role_tags:
            score += 2
        if "config" in role_tags:
            score += 1
        if score > 0:
            scored.append((score, path))

    if not scored:
        for item in files:
            path = str(item.get("path", ""))
            if path and str(item.get("kind", "")) in {"python", "markdown", "text"}:
                scored.append((1, path))

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    selected: list[str] = []
    for _, path in scored:
        if path not in selected:
            selected.append(path)
        if len(selected) >= max(1, max_files):
            break
    return selected


def _ask_llm_for_plan(
    client: LLMClient,
    *,
    task_text: str,
    index: dict[str, Any],
    snippets: list[dict[str, Any]],
    benchmark_command: str,
    run_context: dict[str, Any],
    memory_context: str,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
    retry_feedback: str = "",
) -> dict[str, Any]:
    prompt = _plan_user_prompt(
        task_text=task_text,
        index=index,
        snippets=snippets,
        benchmark_command=benchmark_command,
        run_context=run_context,
        memory_context=memory_context,
        protected_patterns=protected_patterns,
        allowed_patterns=allowed_patterns,
    )
    response = client.ask_json(CODE_TASK_PLAN_SYSTEM, prompt + retry_feedback, label="code-task-plan")
    return _normalize_plan_data(
        response,
        index,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
    )


def _plan_user_prompt(
    *,
    task_text: str,
    index: dict[str, Any],
    snippets: list[dict[str, Any]],
    benchmark_command: str,
    run_context: dict[str, Any],
    memory_context: str,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
) -> str:
    compact_index = prompt_file_inventory(
        index,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
        selected_paths=(str(item.get("path", "")) for item in snippets),
    )
    snippet_text = render_source_snippets(snippets)
    return (
        "Create a code modification plan for this existing workspace. "
        "Return JSON with these fields exactly: "
        "`summary` string, `goals` list of strings, `files_to_modify` list of "
        "objects with `path`, `reason`, and `change_type`, `new_files` list of "
        "objects with `path` and `reason`, `proposed_steps` list of strings, "
        "`validation` list of strings, `risks` list of strings, `rollback` list "
        "of strings, `open_questions` list of strings, and "
        "`requires_approval_before_patch` boolean.\n\n"
        "Code-planning rules:\n"
        "- Analyze first; do not write or apply code.\n"
        "- Prefer modifying existing code over generating unrelated new modules.\n"
        "- Mention only workspace-relative paths from the index in "
        "`files_to_modify`. Put truly new files in `new_files`.\n"
        "- Treat inventory `read_only_files` as evidence only. Do "
        "not include them in `files_to_modify` or propose changing them.\n"
        "- Keep the plan small enough for one reviewable patch.\n"
        "- Include the benchmark or validation command when available.\n"
        "- Use the supplied run context. Do not ask open questions that are "
        "already answered by baseline metrics, environment policy, validation "
        "status, or recorded artifacts.\n"
        "- Name risks, rollback steps, and any missing information.\n"
        "- Require human approval before patch application.\n\n"
        f"Task:\n{task_text}\n\n"
        f"Benchmark command recorded for later validation:\n{benchmark_command or 'None'}\n\n"
        f"Run context JSON:\n{json.dumps(run_context, separators=(',', ':'), ensure_ascii=False)}\n\n"
        f"Task memory:\n{memory_context}\n\n"
        f"Codebase index summary JSON:\n{json.dumps(compact_index, separators=(',', ':'), ensure_ascii=False)}\n\n"
        "Selected Python API contract (derived from the exact snippets below):\n"
        f"{json.dumps(snippet_api_contract(snippets), separators=(',', ':'), ensure_ascii=False)}\n\n"
        f"Selected source snippets:\n{snippet_text or 'No source snippets selected.'}"
    )


def _offline_plan(
    *,
    task_text: str,
    index: dict[str, Any],
    selected_files: list[str],
    benchmark_command: str,
    run_context: dict[str, Any],
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
) -> dict[str, Any]:
    files_to_modify = [
        {
            "path": path,
            "reason": "Selected as likely relevant to the task based on path, role tags, or code summary.",
            "change_type": "inspect_then_edit",
        }
        for path in selected_files[:5]
        if is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
    ]
    validation = [benchmark_command] if benchmark_command else [
        "Run the project's existing tests or benchmark command after applying a patch."
    ]
    return {
        "summary": "Offline fallback plan generated from the task text and codebase index.",
        "goals": [
            _first_sentence(task_text) or "Clarify the requested code improvement.",
            "Keep the change small, reviewable, and limited to the copied workspace.",
        ],
        "files_to_modify": files_to_modify,
        "new_files": [],
        "proposed_steps": [
            _offline_context_step(run_context),
            "Inspect the selected files and confirm which functions/classes implement the target behavior.",
            "Prepare a minimal patch that changes only the behavior required by the task.",
            "Update or add focused tests when the current test coverage does not exercise the requested behavior.",
            "Run validation and preserve stdout/stderr in the code-task run artifacts.",
        ],
        "validation": validation,
        "risks": [
            "The offline plan is based on summaries and selected snippets, so it may miss hidden coupling in unselected files.",
            "Changing benchmark logic can invalidate comparisons if the validation command is not representative.",
        ],
        "rollback": [
            "Discard changes inside code_task/workspace and rerun code-task init from the original code root.",
            "Use the file hashes in codebase_index.json to compare the initial workspace state with later edits.",
        ],
        "open_questions": [
            "Should the patch optimize implementation quality, benchmark score, test coverage, or all of them?",
            "Are there external constraints not visible in the copied workspace?",
        ],
        "requires_approval_before_patch": True,
    }


def _normalize_plan_data(
    data: dict[str, Any],
    index: dict[str, Any],
    *,
    protected_patterns: tuple[str, ...],
    allowed_patterns: tuple[str, ...],
) -> dict[str, Any]:
    known_paths = {str(item.get("path", "")) for item in _index_files(index)}
    normalized = {
        "summary": _string(data.get("summary")) or "Patch plan generated by LLM.",
        "goals": _string_list(data.get("goals")),
        "files_to_modify": _file_plan_list(
            data.get("files_to_modify"),
            known_paths,
            protected_patterns=protected_patterns,
            allowed_patterns=allowed_patterns,
        ),
        "new_files": _new_file_plan_list(data.get("new_files")),
        "proposed_steps": _string_list(data.get("proposed_steps")),
        "validation": _string_list(data.get("validation")),
        "risks": _string_list(data.get("risks")),
        "rollback": _string_list(data.get("rollback")),
        "open_questions": _string_list(data.get("open_questions")),
        "requires_approval_before_patch": data.get("requires_approval_before_patch") is not False,
    }
    if not normalized["goals"]:
        normalized["goals"] = ["Implement the requested change in a small, reviewable patch."]
    if not normalized["proposed_steps"]:
        normalized["proposed_steps"] = ["Inspect relevant files before preparing a patch."]
    if not normalized["validation"]:
        normalized["validation"] = ["Run the recorded benchmark or project test command."]
    if not normalized["rollback"]:
        normalized["rollback"] = ["Discard workspace edits and rerun code-task init."]
    return normalized


def patch_plan_context(patch_plan: str, task_text: str) -> str:
    """Do not resend the renderer's exact Task copy beside the full task.

    Keep stored/approved Markdown intact. Custom plans and nonidentical task
    sections remain verbatim; this is deduplication, not summarization.
    """
    task = task_text.strip()
    repeated = f"## Task\n\n{task}\n\n## Run Context"
    return patch_plan.replace(repeated, "## Task\n\nComplete task supplied separately.\n\n## Run Context", 1) if task else patch_plan


def _render_patch_plan(
    plan: dict[str, Any],
    *,
    task_text: str,
    selected_files: list[str],
    run_context: dict[str, Any],
    context_pack: dict[str, Any] | None,
    mode: str,
) -> str:
    sections = [
        "# Patch Plan",
        "",
        f"Generated mode: `{mode}`",
        "",
        "## Task",
        "",
        task_text.strip() or "No task text was provided.",
        "",
        "## Run Context",
        "",
        _run_context_markdown(run_context),
        "",
        "## Summary",
        "",
        _string(plan.get("summary")) or "No summary was generated.",
        "",
        "## Goals",
        "",
        _bullet_list(_string_list(plan.get("goals"))),
        "",
        "## Context Used",
        "",
        _context_pack_markdown(context_pack),
        "",
        _bullet_list([f"`{path}`" for path in selected_files]) or "- No files selected.",
        "",
        "## Files To Modify",
        "",
        _file_plan_markdown(plan.get("files_to_modify")),
        "",
        "## New Files",
        "",
        _new_file_plan_markdown(plan.get("new_files")),
        "",
        "## Proposed Steps",
        "",
        _bullet_list(_string_list(plan.get("proposed_steps"))),
        "",
        "## Validation",
        "",
        _bullet_list(_string_list(plan.get("validation"))),
        "",
        "## Risks",
        "",
        _bullet_list(_string_list(plan.get("risks"))),
        "",
        "## Rollback",
        "",
        _bullet_list(_string_list(plan.get("rollback"))),
        "",
        "## Open Questions",
        "",
        _bullet_list(_string_list(plan.get("open_questions"))) or "- None recorded.",
        "",
        "## Human Approval",
        "",
        "Patch application is blocked until this plan is reviewed. Record a decision with:",
        "",
        "```bash",
        "uv run simple-ar code-task decide-plan runs/<run-id> --decision approve --note \"reviewed\"",
        "```",
        "",
    ]
    return "\n".join(sections)


def _file_plan_markdown(value: object) -> str:
    rows = value if isinstance(value, list) else []
    lines: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        path = _string(item.get("path"))
        if not path:
            continue
        reason = _string(item.get("reason")) or "No reason provided."
        change_type = _string(item.get("change_type")) or "edit"
        lines.append(f"- `{path}` ({change_type}): {reason}")
    return "\n".join(lines) if lines else "- No existing files selected."


def _new_file_plan_markdown(value: object) -> str:
    rows = value if isinstance(value, list) else []
    lines: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        path = _string(item.get("path"))
        reason = _string(item.get("reason")) or "No reason provided."
        if path:
            lines.append(f"- `{path}`: {reason}")
    return "\n".join(lines) if lines else "- No new files proposed."


def _collect_run_context(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    task_dir = run_dir / "code_task"
    meta_dir = task_dir / "meta"
    run_artifact_dir = task_dir / "run"
    environment = _read_optional_json(meta_dir / "environment_report.json")
    validation = _read_optional_json(meta_dir / "validation_report.json")
    baseline_execution = _read_optional_json(
        run_artifact_dir / "baseline" / "execution_report.json"
    )
    baseline_metrics = _read_optional_json(run_artifact_dir / "baseline" / "metrics.json")
    patched_execution = _read_optional_json(
        run_artifact_dir / "patched" / "execution_report.json"
    )
    patched_metrics = _read_optional_json(run_artifact_dir / "patched" / "metrics.json")
    context = {
        "environment": _environment_context(environment, manifest),
        "validation": _validation_context(validation),
        "baseline": _execution_context(baseline_execution, baseline_metrics),
        "patched": _execution_context(patched_execution, patched_metrics),
    }
    context["available_artifacts"] = [
        name
        for name, value in (
            ("environment_report", environment),
            ("validation_report", validation),
            ("baseline_execution", baseline_execution),
            ("baseline_metrics", baseline_metrics),
            ("patched_execution", patched_execution),
            ("patched_metrics", patched_metrics),
        )
        if value
    ]
    return context


def _environment_context(environment: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    policy = _object_dict(environment.get("execution_policy"))
    if not policy:
        manifest_environment = _object_dict(manifest.get("environment"))
        policy = _object_dict(manifest_environment.get("policy"))
    platform = _object_dict(environment.get("platform"))
    gpu = _object_dict(environment.get("gpu"))
    project = _object_dict(environment.get("project"))
    return {
        "status": environment.get("status"),
        "mode": policy.get("mode"),
        "python_executable": policy.get("python_executable"),
        "dependency_install": policy.get("dependency_install"),
        "platform": {
            "system": platform.get("system"),
            "release": platform.get("release"),
            "machine": platform.get("machine"),
        },
        "gpu": {
            "available": gpu.get("available"),
            "count": gpu.get("count"),
        },
        "dependency_files": project.get("dependency_files", []),
        "test_dirs": project.get("test_dirs", []),
        "warnings": environment.get("warnings", []),
    }


def _validation_context(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": validation.get("status"),
        "error_count": validation.get("error_count"),
        "warning_count": validation.get("warning_count"),
        "strict": validation.get("strict"),
    }


def _execution_context(
    execution: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": execution.get("status"),
        "returncode": execution.get("returncode"),
        "timed_out": execution.get("timed_out"),
        "duration_sec": execution.get("duration_sec"),
        "command_text": execution.get("command_text"),
        "metrics": {
            str(key): value
            for key, value in metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    }


def _run_context_markdown(run_context: dict[str, Any]) -> str:
    lines: list[str] = []
    artifacts = run_context.get("available_artifacts", [])
    if isinstance(artifacts, list) and artifacts:
        lines.append("- Available artifacts: " + ", ".join(f"`{item}`" for item in artifacts))
    else:
        lines.append("- Available artifacts: none beyond task and codebase index.")
    environment = _object_dict(run_context.get("environment"))
    if environment and environment.get("status"):
        lines.append(
            "- Environment: "
            f"`{environment.get('status')}` mode=`{environment.get('mode', 'unknown')}` "
            f"dependency_install=`{environment.get('dependency_install', 'unknown')}`"
        )
        gpu = _object_dict(environment.get("gpu"))
        if gpu:
            lines.append(f"- GPU devices visible: `{gpu.get('count', 0)}`")
    baseline = _object_dict(run_context.get("baseline"))
    if baseline and baseline.get("status"):
        lines.append(
            "- Baseline: "
            f"`{baseline.get('status')}` returncode=`{baseline.get('returncode')}` "
            f"duration=`{baseline.get('duration_sec')}`"
        )
        metrics = _object_dict(baseline.get("metrics"))
        if metrics:
            lines.append("- Baseline metrics: " + _metric_inline(metrics))
    patched = _object_dict(run_context.get("patched"))
    if patched and patched.get("status"):
        lines.append(
            "- Existing patched run: "
            f"`{patched.get('status')}` returncode=`{patched.get('returncode')}`"
        )
        metrics = _object_dict(patched.get("metrics"))
        if metrics:
            lines.append("- Existing patched metrics: " + _metric_inline(metrics))
    validation = _object_dict(run_context.get("validation"))
    if validation and validation.get("status"):
        lines.append(
            "- Latest validation: "
            f"`{validation.get('status')}` errors=`{validation.get('error_count')}` "
            f"warnings=`{validation.get('warning_count')}`"
        )
    return "\n".join(lines)


def _context_pack_markdown(context_pack: dict[str, Any] | None) -> str:
    if not context_pack:
        return "- Context pack: none; selected files came from the fallback index selector."
    lines = [
        f"- Context pack: `{context_pack.get('path', '')}`",
        f"- Prompt context: `{context_pack.get('prompt_context', '')}`",
    ]
    budget = context_pack.get("budget")
    if isinstance(budget, dict):
        lines.append(
            "- Context budget: "
            f"files=`{len(context_pack.get('selected_files', []))}` "
            f"chars=`{budget.get('used_chars', 0)}`/`{budget.get('max_total_chars', '')}`"
        )
    return "\n".join(lines)


def _metric_inline(metrics: dict[str, Any]) -> str:
    return ", ".join(f"`{key}`={value}" for key, value in sorted(metrics.items()))


def _offline_context_step(run_context: dict[str, Any]) -> str:
    baseline = _object_dict(run_context.get("baseline"))
    metrics = _object_dict(baseline.get("metrics"))
    if metrics:
        return (
            "Use the recorded baseline metrics when judging the patch: "
            + _metric_inline(metrics)
            + "."
        )
    return "Run the recorded benchmark before judging whether the patch improves behavior."


def _manifest_plan_context(run_context: dict[str, Any]) -> dict[str, Any]:
    context = _manifest_run_context(run_context)
    context["validation_status"] = _object_dict(run_context.get("validation")).get("status")
    return context


def _manifest_run_context(run_context: dict[str, Any]) -> dict[str, Any]:
    environment = _object_dict(run_context.get("environment"))
    baseline = _object_dict(run_context.get("baseline"))
    return {
        "available_artifacts": run_context.get("available_artifacts", []),
        "environment_status": environment.get("status"),
        "environment_mode": environment.get("mode"),
        "baseline_status": baseline.get("status"),
        "baseline_metrics": baseline.get("metrics", {}),
    }


def _update_manifest_after_plan(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    selected_files: list[str],
    run_context: dict[str, Any],
    context_pack: dict[str, Any] | None,
    mode: str,
) -> None:
    plan = _object_dict(manifest.get("plan"))
    plan.update(
        {
            "status": "pending_approval",
            "mode": mode,
            "generated_at": _utcnow_iso(),
            "patch_plan": "code_task/patch_plan.md",
            "selected_files": selected_files,
            "context_pack": context_pack,
            "context": _manifest_plan_context(run_context),
        }
    )
    layout = _object_dict(manifest.get("layout"))
    layout["patch_plan"] = "code_task/patch_plan.md"
    manifest["layout"] = layout
    manifest["plan"] = plan
    manifest["status"] = "planned"
    write_json(manifest_path, manifest)




def _context_pack_snippets(
    loaded: LoadedCodeTaskContextPack,
    *,
    max_files: int,
    max_chars_per_file: int,
    include_read_only: bool = True,
) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    for row in loaded.snippets:
        role = str(row.get("access_role", "editable"))
        if not include_read_only and role != "editable":
            continue
        path = _string(row.get("path"))
        text = row.get("text")
        if not path or not isinstance(text, str):
            continue
        snippets.append(clip_source_snippet(row, max_chars=max(1, max_chars_per_file)))
        if len(snippets) >= max(1, max_files):
            break
    return snippets


def _load_code_task_manifest(path: Path) -> dict[str, Any]:
    data = _read_required_json(path)
    if data.get("workflow") != "code_task":
        raise RuntimeError(f"Run is not a code-task workflow: {path.parent}")
    return data


def _read_required_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing required artifact: {path}")
    return read_text(path)


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def _index_files(index: dict[str, Any]) -> list[dict[str, Any]]:
    return _object_list(index.get("files"))


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _file_haystack(item: dict[str, Any]) -> str:
    pieces = [
        str(item.get("path", "")),
        str(item.get("summary", "")),
        " ".join(str(tag) for tag in item.get("role_tags", []) if isinstance(tag, str)),
    ]
    python = item.get("python")
    if isinstance(python, dict):
        pieces.extend(str(name) for name in python.get("imports", []) if isinstance(name, str))
        for function in python.get("functions", []):
            if isinstance(function, dict):
                pieces.append(str(function.get("name", "")))
        for klass in python.get("classes", []):
            if isinstance(klass, dict):
                pieces.append(str(klass.get("name", "")))
                for method in klass.get("methods", []):
                    if isinstance(method, dict):
                        pieces.append(str(method.get("name", "")))
    return " ".join(pieces).lower()


def _file_plan_list(
    value: object,
    known_paths: set[str],
    *,
    protected_patterns: tuple[str, ...],
    allowed_patterns: tuple[str, ...],
) -> list[dict[str, str]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        path = _string(item.get("path"))
        if not path or path not in known_paths:
            continue
        if not is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        ):
            continue
        result.append(
            {
                "path": path,
                "reason": _string(item.get("reason")),
                "change_type": _string(item.get("change_type")) or "edit",
            }
        )
    return result


def _new_file_plan_list(value: object) -> list[dict[str, str]]:
    rows = value if isinstance(value, list) else []
    result: list[dict[str, str]] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        path = _string(item.get("path"))
        if not path or path.startswith("/") or ".." in Path(path).parts:
            continue
        result.append({"path": path, "reason": _string(item.get("reason"))})
    return result


def _benchmark_command(manifest: dict[str, Any]) -> str:
    benchmark = manifest.get("benchmark", {})
    if isinstance(benchmark, dict):
        command = benchmark.get("command")
        return str(command) if command else ""
    return ""


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_string(item) for item in value if _string(item)]


def _bullet_list(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _terms(text: str) -> set[str]:
    return {
        term.lower()
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
        if term.lower() not in {"the", "and", "for", "with", "this", "that"}
    }


def _first_sentence(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return ""
    return re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0]


def _emit(callback: MessageCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)
