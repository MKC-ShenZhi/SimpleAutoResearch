from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, read_text, write_json, write_text
from simple_ar.code_task.editing.budget import budget_profiles_json
from simple_ar.code_task.editing.scope import (
    is_edit_allowed_path,
    prompt_file_inventory,
)
from simple_ar.code_task.generation.common import safe_relative_path
from simple_ar.code_task.editing.planning import (
    MessageCallback,
    _bullet_list,
    _first_sentence,
    _generate_plan_data,
    _index_files,
    _manifest_run_context,
    _object_dict,
    _object_list,
    _prepare_planning_context,
    _string,
    _string_list,
)
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    save_code_task_manifest,
    utcnow_iso,
)
from simple_ar.code_task.analysis.interfaces import render_source_snippets, snippet_api_contract
from simple_ar.integrations.llm import LLMClient


CODE_TASK_WORK_PLAN_SYSTEM = (
    "You are a careful senior engineer decomposing a code task into small, "
    "reviewable implementation batches. You must plan execution only. Do not "
    "write patches, full code, diffs, or old/new edit payloads."
)

VALID_BUDGET_PROFILES = {"normal", "large", "absolute"}


@dataclass(frozen=True)
class CodeTaskWorkPlanResult:
    """Result returned after generating a code-task work plan.

    Args:
        run_dir: Code-task run directory.
        work_plan_path: Machine-readable work plan JSON.
        work_plan_markdown_path: Human-readable work plan Markdown.
        manifest_path: Root manifest updated with work-plan state.
        mode: ``llm`` when model output was used, otherwise ``offline``.
        selected_files: Workspace-relative files used as planning context.
        item_count: Number of normalized work items.
        pending_approval: Whether the plan should be reviewed before editing.
    """

    run_dir: Path
    work_plan_path: Path
    work_plan_markdown_path: Path
    manifest_path: Path
    mode: str
    selected_files: tuple[str, ...]
    item_count: int
    pending_approval: bool


def generate_code_task_work_plan(
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
) -> CodeTaskWorkPlanResult:
    """Generate a staged implementation plan for an initialized code-task run.

    The work plan is intentionally one level above ``patch_plan.md``. It turns
    ``task.md`` and the selected code context into reviewable work items, each
    of which can later receive its own context pack, edit proposal, validation,
    and retry state.

    Args:
        run_dir: Code-task run directory created by ``code-task init``.
        model: Optional OpenAI-compatible model override.
        use_llm: Whether to call the configured LLM provider.
        allow_llm_fallback: Permit a deterministic plan after completed format
            failures with reconciled accounting. Provider, budget, stream and
            deadline failures propagate without fallback.
        llm_retry_attempts: Total attempts for completed JSON format correction.
            Provider retries and accounting belong to LLMClient.
        force: Overwrite existing ``work_plan.json`` and ``work_plan.md``.
        max_files: Maximum number of source/evidence files included in the
            planning prompt.
        max_source_chars_per_file: Per-file source snippet character budget.
        message_callback: Optional callback for progress messages.

    Returns:
        Paths and summary metadata for the generated work plan.

    Raises:
        FileExistsError: If work-plan artifacts already exist and ``force`` is
            false.
        FileNotFoundError: If required code-task artifacts are missing.
        RuntimeError: If ``run_dir`` is not a code-task workflow.
    """

    if max_files < 1:
        raise ValueError("max_files must be at least 1")
    if max_source_chars_per_file < 200:
        raise ValueError("max_source_chars_per_file must be at least 200")
    if llm_retry_attempts < 1:
        raise ValueError("llm_retry_attempts must be at least 1")

    root = Path(run_dir)
    paths = code_task_paths(root)
    work_plan_path = paths.task_dir / "work_plan.json"
    markdown_path = paths.task_dir / "work_plan.md"
    if (work_plan_path.exists() or markdown_path.exists()) and not force:
        raise FileExistsError(
            f"Work plan already exists: {work_plan_path}. Pass --force to overwrite."
        )

    manifest, inputs, selected, context_pack_ref = _prepare_planning_context(
        root, use_llm=use_llm, max_files=max_files,
        max_source_chars_per_file=max_source_chars_per_file,
        message_callback=message_callback,
    )
    inputs["benchmark_command"] = _benchmark_command(manifest)
    new_files = _creation_paths(manifest, run_dir=root)
    if new_files:
        inputs["run_context"]["new_files"] = new_files
    plan_data, mode = _generate_plan_data(
        paths.meta_dir, inputs=inputs, selected_files=selected,
        ask=_ask_llm_for_work_plan, offline=_offline_work_plan,
        plan_name="work", usage_stage="code_task.work_plan",
        model=model, use_llm=use_llm, llm_client=llm_client,
        llm_retry_attempts=llm_retry_attempts,
        allow_llm_fallback=allow_llm_fallback, message_callback=message_callback,
    )

    work_plan = _normalize_work_plan_data(
        plan_data,
        inputs["index"],
        selected_files=selected,
        task_text=inputs["task_text"],
        benchmark_command=inputs["benchmark_command"],
        mode=mode,
        run_context=inputs["run_context"],
        context_pack=context_pack_ref,
        allowed_patterns=inputs["allowed_patterns"],
        protected_patterns=inputs["protected_patterns"],
    )
    write_json(work_plan_path, work_plan)
    write_text(markdown_path, render_work_plan_markdown(work_plan, task_text=inputs["task_text"]))
    _update_manifest_after_work_plan(
        root,
        manifest,
        work_plan,
        context_pack=context_pack_ref,
    )
    return CodeTaskWorkPlanResult(
        run_dir=root,
        work_plan_path=work_plan_path,
        work_plan_markdown_path=markdown_path,
        manifest_path=paths.manifest_path,
        mode=mode,
        selected_files=tuple(selected),
        item_count=len(_object_list(work_plan.get("items"))),
        pending_approval=bool(work_plan.get("approval", {}).get("required", True)),
    )


def render_work_plan_markdown(work_plan: dict[str, Any], *, task_text: str) -> str:
    """Render ``work_plan.json`` into a concise review document."""

    items = _object_list(work_plan.get("items"))
    lines = [
        "# Work Plan",
        "",
        f"Generated mode: `{work_plan.get('mode', 'offline')}`",
        f"Generated at: `{work_plan.get('generated_at', '')}`",
        "",
        "## Task",
        "",
        task_text.strip() or "No task text was provided.",
        "",
        "## Summary",
        "",
        _string(work_plan.get("summary")) or "No summary was generated.",
        "",
        "## Goal",
        "",
        _string(work_plan.get("goal")) or "No goal was generated.",
        "",
        "## Success Criteria",
        "",
        _bullet_list(_string_list(work_plan.get("success_criteria"))),
        "",
        "## Context Used",
        "",
        _context_markdown(work_plan),
        "",
        "## Work Items",
        "",
        _work_items_markdown(items),
        "",
        "## Risks",
        "",
        _bullet_list(_string_list(work_plan.get("risks"))) or "- No risks recorded.",
        "",
        "## Approval",
        "",
        _approval_markdown(work_plan),
        "",
    ]
    return "\n".join(lines)


def _ask_llm_for_work_plan(
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
    prompt = _work_plan_user_prompt(
        task_text=task_text,
        index=index,
        snippets=snippets,
        benchmark_command=benchmark_command,
        run_context=run_context,
        memory_context=memory_context,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
    )
    response = client.ask_json(CODE_TASK_WORK_PLAN_SYSTEM, prompt + retry_feedback, label="code-task-work-plan")
    return response if isinstance(response, dict) else {}


def _work_plan_user_prompt(
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
        "Create a staged work plan for implementing this code task. "
        "Return JSON only. Do not include Markdown fences. Do not write code, "
        "diffs, old/new edits, or full replacement functions.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "summary": string,\n'
        '  "goal": string,\n'
        '  "success_criteria": [string],\n'
        '  "new_files": [{"path": workspace_relative_path, "reason": string}],\n'
        '  "items": [\n'
        "    {\n"
        '      "id": "W1",\n'
        '      "objective": string,\n'
        '      "target_files": [workspace_relative_path],\n'
        '      "read_only_evidence": [workspace_relative_path],\n'
        '      "depends_on": ["W0"],\n'
        '      "validation": [string],\n'
        '      "done_criteria": [string],\n'
        '      "risk": string,\n'
        '      "parallelizable": boolean,\n'
        '      "budget_profile": "normal" | "large" | "absolute",\n'
        '      "requires_budget_override": boolean,\n'
        '      "suggested_budget_override": string,\n'
        '      "context_request": {"query": string, "files": [path], "symbols": [string]}\n'
        "    }\n"
        "  ],\n"
        '  "context_requests": [string],\n'
        '  "risks": [string],\n'
        '  "approval": {"required": true, "reason": string}\n'
        "}\n\n"
        "Planning rules:\n"
        "- Make 1 to 5 work items. Each item should be a small implementation batch that can produce code edits.\n"
        "- Do not create inspection-only, review-only, measurement-only, or documentation-only items. Put needed inspection in `context_request`.\n"
        "- The first item should normally be the smallest useful code change, not a broad analysis step.\n"
        "- If a useful change requires definition, caller, and configuration files to change together before it can pass the benchmark, put those tightly coupled files in the same item.\n"
        "- Do not split producer/caller/config edits into separate dependent items unless each item is independently runnable and useful after validation.\n"
        "- Use `task.md` as requirements, not as a patch. This work plan is the execution plan.\n"
        "- Use indexed paths or explicitly declared `new_files` in `target_files`. Preserve required "
        "new files from the run context; declare any additional new paths in `new_files`.\n"
        "- New paths are creation targets, not observed source. Use only indexed paths in "
        "`read_only_evidence` and context-request files.\n"
        "- Inventory `read_only_files` are evidence only. Never put them in `target_files`.\n"
        "- Prefer `budget_profile` = `normal`: a small cohesive change within the supplied file and character limits; use compact old/new edits.\n"
        "- Use `large` only when a single function or closely coupled change genuinely needs it.\n"
        "- Use `absolute` only for rare cases that should require explicit human approval.\n"
        "- If more context is needed, write a concrete `context_request` instead of guessing.\n"
        "- Keep every field concise. Avoid implementation prose longer than a short paragraph.\n"
        "- Include benchmark or validation commands when available.\n\n"
        f"Task:\n{task_text}\n\n"
        f"Benchmark command:\n{benchmark_command or 'None'}\n\n"
        f"Run context JSON:\n{json.dumps(run_context, indent=2, ensure_ascii=False)}\n\n"
        f"Task memory:\n{memory_context}\n\n"
        f"Codebase index summary JSON:\n{json.dumps(compact_index, separators=(',', ':'), ensure_ascii=False)}\n\n"
        "Selected Python API contract (derived from the exact snippets below):\n"
        f"{json.dumps(snippet_api_contract(snippets), indent=2, ensure_ascii=False)}\n\n"
        f"Selected source/evidence snippets:\n{snippet_text or 'No snippets selected.'}"
    )


def _offline_work_plan(
    *,
    task_text: str,
    index: dict[str, Any],
    selected_files: list[str],
    benchmark_command: str,
    run_context: dict[str, Any],
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
) -> dict[str, Any]:
    editable = [
        path for path in selected_files
        if path and is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
    ]
    evidence = [
        path for path in selected_files
        if path and not is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
    ]
    if not editable:
        editable = _first_editable_files(
            index,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
            limit=2,
        )
    validation = [benchmark_command] if benchmark_command else [
        "Run the recorded benchmark or the project's focused tests after applying edits."
    ]
    return {
        "summary": "Offline work plan generated from task text, codebase index, and selected context.",
        "goal": _first_sentence(task_text) or "Implement the requested code improvement safely.",
        "success_criteria": [
            "Only files inside code_task/workspace are edited.",
            "Protected tests, benchmarks, data, and configuration evidence remain read-only.",
            "The recorded benchmark or focused validation command passes after the patch.",
        ],
        "items": [
            {
                "id": "W1",
                "objective": "Inspect the primary editable files and implement the smallest behavior change that satisfies the task.",
                "target_files": list(dict.fromkeys([*run_context.get("new_files", []), *editable[:3]])),
                "read_only_evidence": evidence[:3],
                "depends_on": [],
                "validation": validation,
                "done_criteria": [
                    "The target behavior is implemented in editable source files.",
                    "Existing public APIs remain stable unless the task explicitly requires an API change.",
                    "Validation artifacts show the benchmark or focused tests completed.",
                ],
                "risk": _offline_risk(run_context),
                "parallelizable": False,
                "budget_profile": "normal",
                "requires_budget_override": False,
                "suggested_budget_override": "",
                "context_request": {
                    "query": "Collect local callers, imports, and tests connected to the selected target files.",
                    "files": editable[:3] + evidence[:3],
                    "symbols": [],
                },
            }
        ],
        "context_requests": [
            "Before editing, build a focused context pack for the selected target files and their callers/tests."
        ],
        "risks": [
            "The offline plan may miss hidden coupling in files that were not selected by lexical ranking.",
            "A too-small context pack can make the first edit proposal incomplete; request more context before patching when needed.",
        ],
        "approval": {
            "required": True,
            "reason": "Human review should confirm the target files and budget before any edit batch runs.",
        },
    }


def _normalize_work_plan_data(
    data: dict[str, Any],
    index: dict[str, Any],
    *,
    selected_files: list[str],
    task_text: str,
    benchmark_command: str,
    mode: str,
    run_context: dict[str, Any],
    context_pack: dict[str, Any] | None,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
) -> dict[str, Any]:
    known_paths = {str(item.get("path", "")) for item in _index_files(index) if item.get("path")}
    new_files = list(dict.fromkeys([
        *_creation_paths({}, planned=run_context.get("new_files")),
        *_creation_paths({}, planned=data.get("new_files")),
    ]))
    new_files = [path for path in new_files if is_edit_allowed_path(
        path, allowed_patterns=allowed_patterns, protected_patterns=protected_patterns)]
    items = _normalize_work_items(
        data.get("items"),
        known_paths,
        selected_files=selected_files,
        allowed_patterns=allowed_patterns,
        protected_patterns=protected_patterns,
        new_files=new_files,
    )
    if not items:
        fallback = _offline_work_plan(
            task_text=task_text,
            index=index,
            selected_files=selected_files,
            benchmark_command=benchmark_command or _benchmark_from_context(run_context),
            run_context={**run_context, "new_files": new_files},
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
        items = _normalize_work_items(
            fallback.get("items"),
            known_paths,
            selected_files=selected_files,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
            new_files=new_files,
        )

    approval = _object_dict(data.get("approval"))
    required = approval.get("required") is not False
    result = {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "mode": mode,
        "summary": _string(data.get("summary")) or "Code-task work plan generated.",
        "goal": _string(data.get("goal")) or _first_sentence(task_text) or "Implement the requested code task.",
        "success_criteria": _string_list(data.get("success_criteria")) or [
            "The requested behavior is implemented in editable workspace files.",
            "Validation or benchmark artifacts are recorded after the change.",
        ],
        "items": items,
        "context_requests": _string_list(data.get("context_requests")),
        "risks": _string_list(data.get("risks")),
        "approval": {
            "required": required,
            "status": "pending" if required else "not_required",
            "reason": _string(approval.get("reason")) or "Review work items before editing.",
        },
        "selected_files": selected_files,
        "context_pack": context_pack,
        "run_context": _manifest_run_context(run_context),
        "budget_profiles": _budget_profiles(),
    }
    if not result["context_requests"]:
        result["context_requests"] = [
            "Build or refresh a context pack for the first pending work item before asking for edits."
        ]
    if not result["risks"]:
        result["risks"] = ["Incomplete context can lead to incomplete edits; request more context when needed."]
    if new_files:
        result["new_files"] = new_files
    return result


def _creation_paths(manifest: dict[str, Any], *, planned: object = None,
                    run_dir: Path | None = None) -> list[str]:
    """Read frozen/explicit plan targets without inventing indexed source rows."""
    owner = _object_dict(_object_dict(manifest.get("code_task")).get("native_binding"))
    contract = _object_dict(_object_dict(owner.get("binding")).get("contract"))
    frozen = contract.get("new_files")
    rows = [*(frozen if isinstance(frozen, list) else []), *(planned if isinstance(planned, list) else [])]
    if run_dir is not None:
        paths = code_task_paths(run_dir)
        plan = _object_dict(manifest.get("plan"))
        if plan.get("status") == "approved":
            ref = plan.get("patch_plan", "code_task/patch_plan.md")
            if ref == "code_task/patch_plan.json" and (paths.task_dir / "patch_plan.json").is_file():
                declared = _object_dict(read_json(paths.task_dir / "patch_plan.json")).get("new_files")
                rows.extend(declared if isinstance(declared, list) else [])
            elif ref == "code_task/patch_plan.md" and (paths.task_dir / "patch_plan.md").is_file():
                sections = re.findall(r"(?ms)^## New Files[ \t]*\r?$\n(.*?)(?=^## |\Z)",
                                      read_text(paths.task_dir / "patch_plan.md"))
                if sections:
                    rows.extend(re.findall(r"(?m)^- `([^`\r\n]+)`:", sections[-1]))
        work_plan = _object_dict(manifest.get("work_plan"))
        if (_object_dict(work_plan.get("approval")).get("status") in {"approved", "not_required"}
                and (paths.task_dir / "work_plan.json").is_file()):
            declared = _object_dict(read_json(paths.task_dir / "work_plan.json")).get("new_files")
            rows.extend(declared if isinstance(declared, list) else [])
    paths = []
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else row
        if (isinstance(path, str) and safe_relative_path(path) == path and path
                and not any(char in path for char in ':*?[]\\\x00')
                and path.casefold() not in {name.casefold() for name in paths}):
            paths.append(path)
    return paths


def _normalize_work_items(
    value: object,
    known_paths: set[str],
    *,
    selected_files: list[str],
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
    new_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    raw_items = value if isinstance(value, list) else []
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        target_files = _known_paths(
            raw.get("target_files"),
            known_paths | set(new_files or ()),
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
            editable_only=True,
        )
        if not target_files:
            target_files = [
                path for path in selected_files
                if path in known_paths
                and is_edit_allowed_path(
                    path,
                    allowed_patterns=allowed_patterns,
                    protected_patterns=protected_patterns,
                )
            ][:2]
        evidence = _known_paths(
            raw.get("read_only_evidence"),
            known_paths,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
            editable_only=False,
        )
        budget = _string(raw.get("budget_profile")).lower() or "normal"
        if budget not in VALID_BUDGET_PROFILES:
            budget = "normal"
        # Keep the work plan
        # and the proposal budget consistent when a single cohesive item
        # names more files; otherwise the model receives an impossible task
        # and can only return an empty proposal.  The large profile still
        # carries its explicit approval gate at apply time.
        normal_max_files = budget_profiles_json()["normal"]["max_files"]
        if budget == "normal" and len(target_files) > normal_max_files:
            budget = "large"
        context_request = _context_request(raw.get("context_request"), known_paths)
        item_id = _string(raw.get("id")) or f"W{len(items) + 1}"
        item = {
            "id": _normalize_item_id(item_id, len(items) + 1),
            "status": "pending",
            "objective": _string(raw.get("objective")) or "Implement a focused part of the requested code task.",
            "target_files": target_files,
            "read_only_evidence": evidence,
            "depends_on": _work_item_refs(raw.get("depends_on")),
            "validation": _string_list(raw.get("validation")) or [
                "Run the recorded benchmark or focused tests after applying this batch."
            ],
            "done_criteria": _string_list(raw.get("done_criteria")) or [
                "The focused change is implemented and validation artifacts are recorded."
            ],
            "risk": _string(raw.get("risk")) or "Insufficient context may make the edit incomplete.",
            "parallelizable": bool(raw.get("parallelizable", False)),
            "budget_profile": budget,
            "requires_budget_override": bool(raw.get("requires_budget_override")) or budget != "normal",
            "suggested_budget_override": (
                _string(raw.get("suggested_budget_override"))
                or (
                    "This cohesive work item names more files than the normal "
                    "proposal budget; review the larger edit before applying."
                    if budget == "large" and len(target_files) > normal_max_files
                    else ""
                )
            ),
            "context_request": context_request,
        }
        items.append(item)
    return _renumber_duplicate_ids(items)




def _update_manifest_after_work_plan(
    run_dir: Path,
    manifest: dict[str, Any],
    work_plan: dict[str, Any],
    *,
    context_pack: dict[str, Any] | None,
) -> None:
    layout = _object_dict(manifest.get("layout"))
    layout.update(
        {
            "work_plan": "code_task/work_plan.json",
            "work_plan_markdown": "code_task/work_plan.md",
        }
    )
    manifest["layout"] = layout
    manifest["work_plan"] = {
        "status": "pending_approval" if work_plan["approval"]["required"] else "ready",
        "mode": work_plan.get("mode"),
        "generated_at": work_plan.get("generated_at"),
        "path": "code_task/work_plan.json",
        "markdown": "code_task/work_plan.md",
        "item_count": len(_object_list(work_plan.get("items"))),
        "selected_files": work_plan.get("selected_files", []),
        "context_pack": context_pack,
        "approval": work_plan.get("approval", {}),
    }
    manifest["status"] = "work_planned"
    save_code_task_manifest(run_dir, manifest)


def _work_items_markdown(items: list[dict[str, Any]]) -> str:
    if not items:
        return "- No work items generated."
    sections: list[str] = []
    for item in items:
        target = _inline_paths(_string_list(item.get("target_files"))) or "none"
        evidence = _inline_paths(_string_list(item.get("read_only_evidence"))) or "none"
        depends = ", ".join(_string_list(item.get("depends_on"))) or "none"
        sections.extend(
            [
                f"### {item.get('id', '')}: {_string(item.get('objective'))}",
                "",
                f"- Status: `{item.get('status', 'pending')}`",
                f"- Target files: {target}",
                f"- Read-only evidence: {evidence}",
                f"- Depends on: {depends}",
                f"- Budget profile: `{item.get('budget_profile', 'normal')}`",
                f"- Requires budget override: `{item.get('requires_budget_override', False)}`",
                f"- Parallelizable: `{item.get('parallelizable', False)}`",
                f"- Risk: {_string(item.get('risk')) or 'No risk recorded.'}",
                "- Validation:",
                _bullet_list(_string_list(item.get("validation"))),
                "- Done criteria:",
                _bullet_list(_string_list(item.get("done_criteria"))),
                "- Context request:",
                _context_request_markdown(_object_dict(item.get("context_request"))),
                "",
            ]
        )
    return "\n".join(sections).strip()


def _context_markdown(work_plan: dict[str, Any]) -> str:
    lines: list[str] = []
    context_pack = _object_dict(work_plan.get("context_pack"))
    if context_pack:
        lines.append(f"- Context pack: `{context_pack.get('path', '')}`")
        lines.append(f"- Prompt context: `{context_pack.get('prompt_context', '')}`")
    selected = _string_list(work_plan.get("selected_files"))
    lines.append("- Selected files: " + (_inline_paths(selected) if selected else "none"))
    return "\n".join(lines)


def _approval_markdown(work_plan: dict[str, Any]) -> str:
    approval = _object_dict(work_plan.get("approval"))
    required = approval.get("required") is not False
    reason = _string(approval.get("reason")) or "No reason recorded."
    if not required:
        return f"- Approval required: `False`\n- Reason: {reason}"
    return (
        "- Approval required: `True`\n"
        f"- Status: `{approval.get('status', 'pending')}`\n"
        f"- Reason: {reason}\n"
        "- Next: create a batch for a reviewed work item before asking the model for edits."
    )


def _context_request_markdown(value: dict[str, Any]) -> str:
    if not value:
        return "- No extra context requested."
    lines = []
    query = _string(value.get("query"))
    if query:
        lines.append(f"  - Query: {query}")
    files = _string_list(value.get("files"))
    if files:
        lines.append(f"  - Files: {_inline_paths(files)}")
    symbols = _string_list(value.get("symbols"))
    if symbols:
        lines.append("  - Symbols: " + ", ".join(f"`{symbol}`" for symbol in symbols))
    return "\n".join(lines) if lines else "- No extra context requested."


def _known_paths(
    value: object,
    known_paths: set[str],
    *,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
    editable_only: bool,
) -> list[str]:
    paths: list[str] = []
    for raw in _string_list(value):
        if raw not in known_paths or raw in paths:
            continue
        editable = is_edit_allowed_path(
            raw,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        )
        if editable_only and not editable:
            continue
        paths.append(raw)
    return paths


def _context_request(value: object, known_paths: set[str]) -> dict[str, Any]:
    data = _object_dict(value)
    files = [path for path in _string_list(data.get("files")) if path in known_paths]
    return {
        "query": _string(data.get("query")),
        "files": files,
        "symbols": _string_list(data.get("symbols"))[:12],
    }


def _renumber_duplicate_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        item_id = str(item.get("id") or f"W{index}")
        if item_id in seen:
            item_id = f"W{index}"
        item["id"] = _normalize_item_id(item_id, index)
        seen.add(str(item["id"]))
    return items


def _normalize_item_id(value: str, fallback_index: int) -> str:
    text = value.strip().upper()
    if re.fullmatch(r"W[0-9]{1,3}", text):
        return text
    return f"W{fallback_index}"


def _work_item_refs(value: object) -> list[str]:
    refs = []
    for item in _string_list(value):
        normalized = item.strip().upper()
        if re.fullmatch(r"W[0-9]{1,3}", normalized) and normalized not in refs:
            refs.append(normalized)
    return refs


def _benchmark_command(manifest: dict[str, Any]) -> str:
    benchmark = _object_dict(manifest.get("benchmark"))
    command = benchmark.get("command")
    return command.strip() if isinstance(command, str) else ""


def _benchmark_from_context(run_context: dict[str, Any]) -> str:
    baseline = _object_dict(run_context.get("baseline"))
    command = baseline.get("command_text")
    return command.strip() if isinstance(command, str) else ""


def _offline_risk(run_context: dict[str, Any]) -> str:
    baseline = _object_dict(run_context.get("baseline"))
    if baseline.get("status") and baseline.get("status") != "passed":
        return "The baseline is not passing, so the first batch may need to separate repair from improvement."
    return "Hidden coupling may exist outside the selected context."


def _first_editable_files(
    index: dict[str, Any],
    *,
    allowed_patterns: tuple[str, ...],
    protected_patterns: tuple[str, ...],
    limit: int,
) -> list[str]:
    result: list[str] = []
    for item in _index_files(index):
        path = str(item.get("path", ""))
        if path and is_edit_allowed_path(
            path,
            allowed_patterns=allowed_patterns,
            protected_patterns=protected_patterns,
        ):
            result.append(path)
        if len(result) >= limit:
            break
    return result


def _budget_profiles() -> dict[str, dict[str, Any]]:
    profiles = budget_profiles_json()
    profiles["normal"]["description"] = "Default compact cohesive batch within the supplied file and character limits."
    profiles["large"]["description"] = "For one cohesive function/module change that cannot be split cleanly."
    profiles["absolute"]["description"] = "Rare escape hatch for broad changes; should normally be rejected or split."
    return profiles


def _inline_paths(paths: list[str]) -> str:
    return ", ".join(f"`{path}`" for path in paths)
