from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.artifacts import append_jsonl, read_json, read_jsonl, read_text, write_json, write_text
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    load_code_task_manifest,
    save_code_task_manifest,
    utcnow_iso,
)


MEMORY_SCHEMA_VERSION = "code_task_memory.v1"
COMPACTED_MEMORY_SCHEMA_VERSION = "code_task_memory_compaction.v1"
DEFAULT_MAX_CONTEXT_CHARS = 12_000
DEFAULT_MAX_EVENTS_BEFORE_COMPACTION = 60
DEFAULT_KEEP_RECENT_EVENTS = 12
DEFAULT_KEEP_RECENT_ROWS = 8


class TaskMemoryEvent(BaseModel):
    """Compact event row kept in ``code_task/memory/task_memory.json``."""

    model_config = ConfigDict(extra="ignore")

    key: str
    event_type: str
    summary: str
    status: str = ""
    created_at: str = Field(default_factory=utcnow_iso)
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskMemory(BaseModel):
    """Short-term memory for one code-task run.

    This is an index over canonical artifacts, not a replacement for them.
    Large source snippets, prompts, stdout, and stderr should stay in their
    original artifacts and be referenced by path only.
    """

    model_config = ConfigDict(extra="ignore")

    schema_version: str = MEMORY_SCHEMA_VERSION
    updated_at: str = Field(default_factory=utcnow_iso)
    objective: str = ""
    constraints: list[str] = Field(default_factory=list)
    events: list[TaskMemoryEvent] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class EditHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    created_at: str = Field(default_factory=utcnow_iso)
    changed_files: list[str] = Field(default_factory=list)
    reason: str = ""
    proposal: str = ""
    patch_diff: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    created_at: str = Field(default_factory=utcnow_iso)
    severity: Literal["blocking", "warning", "info"] = "info"
    category: str = "general"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    recommendation: str = ""
    source: str = "simple_ar"


class RepairMemoryEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str
    created_at: str = Field(default_factory=utcnow_iso)
    failure_summary: str
    attempted_fix: str = ""
    status: str = ""
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompactedTouchedFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str
    role: str = ""
    status: str = ""
    risk: str = ""


class CompactedEvidenceRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    summary: str
    evidence: list[str] = Field(default_factory=list)


class CompactedReviewFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    severity: Literal["blocking", "warning", "info"] = "info"
    summary: str
    evidence: list[str] = Field(default_factory=list)


class CompactedRepairLesson(BaseModel):
    model_config = ConfigDict(extra="ignore")

    failure: str
    fix_attempt: str = ""
    outcome: str = ""
    evidence: list[str] = Field(default_factory=list)


class CompactedTaskMemory(BaseModel):
    """Longer-lived compressed memory for one code-task run."""

    model_config = ConfigDict(extra="ignore")

    schema_version: str = COMPACTED_MEMORY_SCHEMA_VERSION
    updated_at: str = Field(default_factory=utcnow_iso)
    source: Literal["deterministic", "llm"] = "deterministic"
    objective: str = ""
    hard_constraints: list[str] = Field(default_factory=list)
    current_strategy: list[str] = Field(default_factory=list)
    touched_files: list[CompactedTouchedFile] = Field(default_factory=list)
    validated_facts: list[CompactedEvidenceRow] = Field(default_factory=list)
    failed_attempts: list[CompactedEvidenceRow] = Field(default_factory=list)
    review_findings: list[CompactedReviewFinding] = Field(default_factory=list)
    repair_lessons: list[CompactedRepairLesson] = Field(default_factory=list)
    open_risks: list[str] = Field(default_factory=list)
    artifact_pointers: list[str] = Field(default_factory=list)


class CodeTaskMemoryPaths(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    memory_dir: Path
    task_memory_json: Path
    task_memory_md: Path
    compressed_memory_json: Path
    compressed_memory_md: Path
    edit_history_jsonl: Path
    review_findings_jsonl: Path
    repair_memory_jsonl: Path
    archive_dir: Path


def _memory_dir_for_run(run_dir: Path) -> Path:
    """Return the active memory directory for standalone or embedded code-task runs.

    Standalone code-task runs keep memory under ``code_task/memory``. When the
    code-task engine is embedded inside the 8-stage pipeline, the code-task run
    lives at ``06-code/code_task_run`` and the active memory belongs beside the
    stage artifacts at ``06-code/memory``. That keeps stage-local continuity
    easy to inspect without burying it under the implementation detail run.
    """

    root = Path(run_dir)
    if root.name == "code_task_run" and root.parent.name == "06-code":
        return root.parent / "memory"
    return code_task_paths(root).task_dir / "memory"


def code_task_memory_paths(run_dir: Path) -> CodeTaskMemoryPaths:
    memory_dir = _memory_dir_for_run(Path(run_dir))
    return CodeTaskMemoryPaths(
        memory_dir=memory_dir,
        task_memory_json=memory_dir / "task_memory.json",
        task_memory_md=memory_dir / "task_memory.md",
        compressed_memory_json=memory_dir / "compressed_memory.json",
        compressed_memory_md=memory_dir / "compressed_memory.md",
        edit_history_jsonl=memory_dir / "edit_history.jsonl",
        review_findings_jsonl=memory_dir / "review_findings.jsonl",
        repair_memory_jsonl=memory_dir / "repair_memory.jsonl",
        archive_dir=memory_dir / "archive",
    )


def ensure_task_memory(run_dir: Path) -> TaskMemory:
    """Load or create short-term memory for a code-task run."""

    root = Path(run_dir)
    paths = code_task_memory_paths(root)
    paths.memory_dir.mkdir(parents=True, exist_ok=True)
    memory = _load_task_memory(paths.task_memory_json)
    if not memory.objective:
        memory.objective = _read_optional_text(code_task_paths(root).task_dir / "task.md").strip()
    if not memory.constraints:
        memory.constraints = _constraints_from_manifest(root)
    memory.updated_at = utcnow_iso()
    _write_task_memory(root, memory)
    return memory


def record_code_task_memory_event(
    run_dir: Path,
    *,
    event_type: str,
    summary: str,
    status: str = "",
    artifacts: list[str] | tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    key: str = "",
) -> TaskMemory:
    """Record a compact stage decision or outcome in task memory."""

    root = Path(run_dir)
    memory = ensure_task_memory(root)
    artifact_rows = _dedupe_strings(artifacts)
    event = TaskMemoryEvent(
        key=key or _stable_key(event_type, summary, artifact_rows),
        event_type=event_type,
        summary=_clip(summary, 500),
        status=status,
        artifacts=artifact_rows,
        metadata=_small_metadata(metadata or {}),
    )
    existing = {row.key: index for index, row in enumerate(memory.events)}
    if event.key in existing:
        memory.events[existing[event.key]] = event
    else:
        memory.events.append(event)
    memory.events = memory.events[-80:]
    memory.artifacts = _dedupe_strings([*memory.artifacts, *artifact_rows])[-120:]
    memory.updated_at = utcnow_iso()
    _write_task_memory(root, memory)
    maybe_compact_task_memory(root)
    return memory


def record_edit_history(
    run_dir: Path,
    *,
    changed_files: list[str] | tuple[str, ...],
    reason: str,
    proposal: str = "",
    patch_diff: str = "",
    metadata: dict[str, Any] | None = None,
    key: str = "",
) -> EditHistoryEntry:
    """Append or replace one edit-history entry and refresh task memory."""

    root = Path(run_dir)
    files = _dedupe_strings(changed_files)
    entry = EditHistoryEntry(
        key=key or _stable_key("edit", reason, files),
        changed_files=files,
        reason=_clip(reason, 500),
        proposal=proposal,
        patch_diff=patch_diff,
        metadata=_small_metadata(metadata or {}),
    )
    paths = code_task_memory_paths(root)
    _append_unique_jsonl(paths.edit_history_jsonl, entry.model_dump(mode="json"), key=entry.key)
    record_code_task_memory_event(
        root,
        event_type="edit_applied",
        summary=entry.reason or f"Applied edits to {len(files)} file(s).",
        status="done",
        artifacts=[path for path in (proposal, patch_diff) if path],
        metadata={"changed_files": files},
        key=f"memory:{entry.key}",
    )
    return entry


def record_review_finding(run_dir: Path, finding: ReviewFinding | dict[str, Any]) -> ReviewFinding:
    """Append or replace one structured review finding."""

    root = Path(run_dir)
    row = finding if isinstance(finding, ReviewFinding) else ReviewFinding(**finding)
    paths = code_task_memory_paths(root)
    _append_unique_jsonl(paths.review_findings_jsonl, row.model_dump(mode="json"), key=row.key)
    record_code_task_memory_event(
        root,
        event_type="review_finding",
        summary=row.summary,
        status=row.severity,
        artifacts=row.evidence,
        metadata={"category": row.category, "source": row.source},
        key=f"memory:{row.key}",
    )
    return row


def record_repair_memory(
    run_dir: Path,
    *,
    failure_summary: str,
    attempted_fix: str = "",
    status: str = "",
    artifacts: list[str] | tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
    key: str = "",
) -> RepairMemoryEntry:
    """Append or replace repair memory and refresh task memory."""

    root = Path(run_dir)
    artifact_rows = _dedupe_strings(artifacts)
    entry = RepairMemoryEntry(
        key=key or _stable_key("repair", failure_summary, artifact_rows),
        failure_summary=_clip(failure_summary, 500),
        attempted_fix=_clip(attempted_fix, 500),
        status=status,
        artifacts=artifact_rows,
        metadata=_small_metadata(metadata or {}),
    )
    paths = code_task_memory_paths(root)
    _append_unique_jsonl(paths.repair_memory_jsonl, entry.model_dump(mode="json"), key=entry.key)
    record_code_task_memory_event(
        root,
        event_type="repair",
        summary=entry.attempted_fix or entry.failure_summary,
        status=status,
        artifacts=artifact_rows,
        metadata=entry.metadata,
        key=f"memory:{entry.key}",
    )
    return entry


def maybe_compact_task_memory(
    run_dir: Path,
    *,
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    max_events: int = DEFAULT_MAX_EVENTS_BEFORE_COMPACTION,
    keep_recent_events: int = DEFAULT_KEEP_RECENT_EVENTS,
    keep_recent_rows: int = DEFAULT_KEEP_RECENT_ROWS,
) -> bool:
    """Compact old active memory into a durable run-local summary when needed.

    Compaction is deterministic and cheap by default. It preserves operational
    facts, failure lessons, touched files, and artifact pointers, then trims the
    active prompt window to the most recent entries.
    """

    root = Path(run_dir)
    paths = code_task_memory_paths(root)
    memory = _load_task_memory(paths.task_memory_json)
    if not memory.events:
        return False
    edit_rows = _read_jsonl_safe(paths.edit_history_jsonl)
    finding_rows = _read_jsonl_safe(paths.review_findings_jsonl)
    repair_rows = _read_jsonl_safe(paths.repair_memory_jsonl)
    active_context = _render_task_memory_context(
        run_dir=root,
        memory=memory,
        compressed=_load_compacted_memory(paths.compressed_memory_json),
        findings=finding_rows[-6:],
        repairs=repair_rows[-6:],
        edits=edit_rows[-6:],
        events=memory.events[-10:],
    )
    should_compact = (
        len(active_context) > max_context_chars
        or len(memory.events) > max_events
        or len(edit_rows) > 40
        or len(finding_rows) > 20
        or len(repair_rows) > 20
    )
    if not should_compact:
        return False

    older_events = memory.events[: max(0, len(memory.events) - keep_recent_events)]
    older_edits = edit_rows[: max(0, len(edit_rows) - keep_recent_rows)]
    older_findings = finding_rows[: max(0, len(finding_rows) - keep_recent_rows)]
    older_repairs = repair_rows[: max(0, len(repair_rows) - keep_recent_rows)]
    if not (older_events or older_edits or older_findings or older_repairs):
        return False

    previous = _load_compacted_memory(paths.compressed_memory_json)
    compressed = _deterministic_compaction(
        memory=memory,
        previous=previous,
        events=older_events,
        edits=older_edits,
        findings=older_findings,
        repairs=older_repairs,
    )
    compressed.updated_at = utcnow_iso()
    _write_compacted_memory(root, compressed)
    _archive_compaction_input(
        root,
        compressed=compressed,
        event_count=len(older_events),
        edit_count=len(older_edits),
        finding_count=len(older_findings),
        repair_count=len(older_repairs),
    )

    memory.events = memory.events[-keep_recent_events:]
    memory.artifacts = _dedupe_strings([*compressed.artifact_pointers, *memory.artifacts])[-120:]
    memory.updated_at = utcnow_iso()
    _write_task_memory(root, memory)
    _rewrite_jsonl(paths.edit_history_jsonl, edit_rows[-keep_recent_rows:])
    _rewrite_jsonl(paths.review_findings_jsonl, finding_rows[-keep_recent_rows:])
    _rewrite_jsonl(paths.repair_memory_jsonl, repair_rows[-keep_recent_rows:])
    _update_manifest_memory_layout(root)
    return True


def task_memory_context(run_dir: Path, *, max_events: int = 10, max_findings: int = 6, max_repairs: int = 6) -> str:
    """Project retained memory without treating a read as a new event.

    Missing memory is initialized by its existing owner; incomplete historical
    fields can be filled in the view without rewriting the saved record.
    """

    root = Path(run_dir)
    paths = code_task_memory_paths(root)
    memory = (_load_task_memory(paths.task_memory_json)
              if paths.task_memory_json.is_file() else ensure_task_memory(root))
    if not memory.objective:
        memory.objective = _read_optional_text(code_task_paths(root).task_dir / "task.md").strip()
    if not memory.constraints:
        memory.constraints = _constraints_from_manifest(root)
    compressed = _load_compacted_memory(paths.compressed_memory_json)
    findings = _read_jsonl_safe(paths.review_findings_jsonl)[-max_findings:]
    repairs = _read_jsonl_safe(paths.repair_memory_jsonl)[-max_repairs:]
    edits = _read_jsonl_safe(paths.edit_history_jsonl)[-max_repairs:]
    events = memory.events[-max_events:]
    return _render_task_memory_context(
        run_dir=root,
        memory=memory,
        compressed=compressed,
        findings=findings,
        repairs=repairs,
        edits=edits,
        events=events,
    )


def _render_task_memory_context(
    *,
    run_dir: Path,
    memory: TaskMemory,
    compressed: CompactedTaskMemory | None,
    findings: list[dict[str, Any]],
    repairs: list[dict[str, Any]],
    edits: list[dict[str, Any]],
    events: list[TaskMemoryEvent],
) -> str:
    lines = [
        "# Code Task Memory",
        "",
        "Use this memory to preserve task continuity. Artifact paths are the source of truth; do not treat memory summaries as proof.",
        "",
        "## Objective",
        "",
        _clip(memory.objective.strip(), 1000) or "(no objective recorded)",
        "",
    ]
    if compressed is not None:
        lines.extend(["## Long-Term Compressed Memory", ""])
        lines.extend(_compressed_context_lines(compressed))
        lines.append("")
    lines.extend(
        [
            "## Constraints",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in (memory.constraints or ["No constraints recorded."]))
    lines.extend(["", "## Recent Decisions And Outcomes", ""])
    if events:
        lines.extend(
            f"- `{event.event_type}` `{event.status or 'noted'}`: {event.summary}"
            + (_artifact_suffix(event.artifacts))
            for event in events
        )
    else:
        lines.append("- No prior events.")
    lines.extend(["", "## Review Findings", ""])
    if findings:
        lines.extend(
            f"- `{row.get('severity', 'info')}` {row.get('summary', '')}"
            + _artifact_suffix(_string_list(row.get("evidence")))
            for row in findings
        )
    else:
        lines.append("- No review findings recorded.")
    lines.extend(["", "## Repair Attempts", ""])
    if repairs:
        lines.extend(
            f"- `{row.get('status', 'noted')}` {row.get('failure_summary', '')}; fix: {row.get('attempted_fix', '')}"
            + _artifact_suffix(_string_list(row.get("artifacts")))
            for row in repairs
        )
    else:
        lines.append("- No repair attempts recorded.")
    lines.extend(["", "## Previous Repair Context", ""])
    lines.extend(_previous_repair_context_lines(repairs=repairs, edits=edits, events=events))
    return "\n".join(lines).strip() + "\n"


def _previous_repair_context_lines(
    *,
    repairs: list[dict[str, Any]],
    edits: list[dict[str, Any]],
    events: list[TaskMemoryEvent],
) -> list[str]:
    """Render the continuity context repair prompts need before another attempt."""

    if not repairs and not edits:
        return ["- No previous repair context is available yet."]

    lines = [
        "- Before proposing another repair, verify whether the previous localization hypothesis was disproved by repeated failure evidence.",
    ]
    signatures = [_repair_signature(row) for row in repairs if _repair_signature(row)]
    if len(signatures) >= 2 and _similar_repair_signal(signatures[-1], signatures[-2]):
        lines.append(
            "- Repeated failure signal detected: the latest two repair records look similar. Do not simply retry the same target or strategy without checking producer/consumer contracts and data flow."
        )

    latest_repairs = repairs[-3:]
    if latest_repairs:
        lines.append("- Recent repair attempts:")
        for row in latest_repairs:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            changed = _string_list(metadata.get("changed_files"))
            attempted = str(row.get("attempted_fix", "")).strip()
            failure = str(row.get("failure_summary", "")).strip()
            status = str(row.get("status", "noted") or "noted")
            parts = [f"`{status}`"]
            if failure:
                parts.append(_clip(failure, 220))
            if attempted:
                parts.append(f"fix={_clip(attempted, 220)}")
            if changed:
                parts.append("changed=" + ", ".join(f"`{path}`" for path in changed[:8]))
            lines.append("  - " + "; ".join(parts))

    latest_edits = edits[-3:]
    if latest_edits:
        lines.append("- Recent applied edit history:")
        for row in latest_edits:
            changed = _string_list(row.get("changed_files"))
            reason = str(row.get("reason", "")).strip()
            detail = _clip(reason, 220) if reason else "applied edits"
            if changed:
                detail += "; changed=" + ", ".join(f"`{path}`" for path in changed[:8])
            lines.append(f"  - {detail}")

    failed_events = [
        event for event in events
        if str(event.status).lower() in {"failed", "blocked", "blocking", "error"}
    ][-3:]
    if failed_events:
        lines.append("- Recent failed outcomes:")
        for event in failed_events:
            lines.append(f"  - `{event.event_type}` `{event.status}`: {_clip(event.summary, 220)}")
    return lines


def _repair_signature(row: dict[str, Any]) -> str:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for key in ("stderr_signature", "failure_signature", "error_signature"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return str(row.get("failure_summary", "")).strip().lower()


def _similar_repair_signal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    left_terms = set(left.split())
    right_terms = set(right.split())
    if not left_terms or not right_terms:
        return False
    overlap = len(left_terms & right_terms)
    return overlap >= 4 and overlap / max(1, min(len(left_terms), len(right_terms))) >= 0.6


def _compressed_context_lines(compressed: CompactedTaskMemory) -> list[str]:
    lines = [
        f"- Source: `{compressed.source}`; updated: `{compressed.updated_at}`.",
    ]
    if compressed.objective:
        lines.extend(["", "### Durable Objective", "", _clip(compressed.objective, 800)])
    if compressed.hard_constraints:
        lines.extend(["", "### Durable Constraints"])
        lines.extend(f"- {item}" for item in compressed.hard_constraints[:10])
    if compressed.current_strategy:
        lines.extend(["", "### Historical Implementation Decisions"])
        lines.extend(f"- {item}" for item in compressed.current_strategy[:12])
    if compressed.touched_files:
        lines.extend(["", "### Touched Files"])
        for row in compressed.touched_files[-18:]:
            detail = "; ".join(part for part in (row.role, row.status, row.risk) if part)
            lines.append(f"- `{row.path}`" + (f": {detail}" if detail else ""))
    if compressed.validated_facts:
        lines.extend(["", "### Validated Facts"])
        lines.extend(
            f"- {row.summary}" + _artifact_suffix(row.evidence)
            for row in compressed.validated_facts[-10:]
        )
    if compressed.failed_attempts or compressed.repair_lessons:
        lines.extend(["", "### Failure And Repair Lessons"])
        lines.extend(
            f"- Failed: {row.summary}" + _artifact_suffix(row.evidence)
            for row in compressed.failed_attempts[-8:]
        )
        lines.extend(
            f"- Repair: {row.failure}; fix: {row.fix_attempt}; outcome: {row.outcome}"
            + _artifact_suffix(row.evidence)
            for row in compressed.repair_lessons[-8:]
        )
    if compressed.review_findings:
        lines.extend(["", "### Review Findings To Remember"])
        lines.extend(
            f"- `{row.severity}` {row.summary}" + _artifact_suffix(row.evidence)
            for row in compressed.review_findings[-10:]
        )
    if compressed.open_risks:
        lines.extend(["", "### Open Risks"])
        lines.extend(f"- {item}" for item in compressed.open_risks[-10:])
    return lines


def render_task_memory_markdown(memory: TaskMemory) -> str:
    lines = [
        "# Code Task Memory",
        "",
        f"Updated: `{memory.updated_at}`",
        "",
        "## Objective",
        "",
        memory.objective.strip() or "(no objective recorded)",
        "",
        "## Constraints",
        "",
    ]
    lines.extend(f"- {item}" for item in (memory.constraints or ["No constraints recorded."]))
    lines.extend(["", "## Recent Events", ""])
    if memory.events:
        for event in memory.events[-20:]:
            lines.append(
                f"- `{event.created_at}` `{event.event_type}` `{event.status or 'noted'}`: {event.summary}"
                + _artifact_suffix(event.artifacts)
            )
    else:
        lines.append("- No events recorded.")
    lines.extend(["", "## Artifact Pointers", ""])
    lines.extend(f"- `{path}`" for path in memory.artifacts[-30:] if path)
    if not memory.artifacts:
        lines.append("- No artifact pointers recorded.")
    lines.append("")
    return "\n".join(lines)


def _render_compacted_memory_markdown(compressed: CompactedTaskMemory) -> str:
    lines = [
        "# Compressed Code Task Memory",
        "",
        f"Updated: `{compressed.updated_at}`",
        f"Source: `{compressed.source}`",
        "",
    ]
    lines.extend(_compressed_context_lines(compressed))
    lines.extend(["", "## Artifact Pointers", ""])
    if compressed.artifact_pointers:
        lines.extend(f"- `{path}`" for path in compressed.artifact_pointers[-80:])
    else:
        lines.append("- No artifact pointers recorded.")
    lines.append("")
    return "\n".join(lines)


def _write_task_memory(run_dir: Path, memory: TaskMemory) -> None:
    paths = code_task_memory_paths(run_dir)
    paths.memory_dir.mkdir(parents=True, exist_ok=True)
    write_json(paths.task_memory_json, memory.model_dump(mode="json"))
    write_text(paths.task_memory_md, render_task_memory_markdown(memory))
    _update_manifest_memory_layout(run_dir)


def _load_task_memory(path: Path) -> TaskMemory:
    if not path.is_file():
        return TaskMemory()
    try:
        data = read_json(path)
    except Exception:
        return TaskMemory()
    if not isinstance(data, dict):
        return TaskMemory()
    try:
        return TaskMemory.model_validate(data)
    except Exception:
        return TaskMemory()


def _load_compacted_memory(path: Path) -> CompactedTaskMemory | None:
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    try:
        return CompactedTaskMemory.model_validate(data)
    except Exception:
        return None


def _deterministic_compaction(
    *,
    memory: TaskMemory,
    previous: CompactedTaskMemory | None,
    events: list[TaskMemoryEvent],
    edits: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    repairs: list[dict[str, Any]],
) -> CompactedTaskMemory:
    compressed = previous.model_copy(deep=True) if previous is not None else CompactedTaskMemory()
    compressed.objective = memory.objective or compressed.objective
    compressed.hard_constraints = _merge_unique([*compressed.hard_constraints, *memory.constraints], limit=24)

    strategy_rows = [
        f"{event.event_type}: {event.summary}"
        for event in events
        if event.event_type
        in {
            "work_plan",
            "batch",
            "patch_plan",
            "edit_proposal",
            "edit_applied",
            "repair",
            "repair_proposal",
        }
    ]
    compressed.current_strategy = _merge_unique(
        [*compressed.current_strategy, *strategy_rows],
        limit=28,
    )

    touched = {row.path: row.model_copy(deep=True) for row in compressed.touched_files if row.path}
    for edit in edits:
        reason = _clip(str(edit.get("reason", "")), 220)
        for path in _string_list(edit.get("changed_files")):
            touched[path] = CompactedTouchedFile(path=path, role="changed_file", status=reason)
    compressed.touched_files = list(touched.values())[-100:]

    artifact_rows: list[str] = [*compressed.artifact_pointers, *memory.artifacts]
    for event in events:
        artifact_rows.extend(event.artifacts)
    for edit in edits:
        artifact_rows.extend(_string_list(edit.get("changed_files")))
        artifact_rows.extend(_string_list(edit.get("artifacts")))
        for key in ("proposal", "patch_diff"):
            if edit.get(key):
                artifact_rows.append(str(edit.get(key)))
    for row in findings:
        artifact_rows.extend(_string_list(row.get("evidence")))
    for row in repairs:
        artifact_rows.extend(_string_list(row.get("artifacts")))
    compressed.artifact_pointers = _merge_unique(artifact_rows, limit=160)

    fact_rows = [
        CompactedEvidenceRow(
            summary=f"{event.event_type}: {event.summary}",
            evidence=event.artifacts,
        )
        for event in events
        if str(event.status).lower() in {"passed", "done", "approved", "ok"}
        and event.event_type in {"probe", "baseline", "plan", "apply_edits", "validation", "patched_run", "repair"}
    ]
    compressed.validated_facts = _merge_evidence_rows(
        [*compressed.validated_facts, *fact_rows],
        limit=48,
    )

    failed_rows = [
        CompactedEvidenceRow(
            summary=f"{event.event_type}: {event.summary}",
            evidence=event.artifacts,
        )
        for event in events
        if str(event.status).lower() in {"failed", "blocked", "blocking", "error"}
    ]
    compressed.failed_attempts = _merge_evidence_rows(
        [*compressed.failed_attempts, *failed_rows],
        limit=48,
    )

    review_rows = [
        CompactedReviewFinding(
            severity=_review_severity(row.get("severity")),
            summary=_clip(str(row.get("summary", "")), 360),
            evidence=_string_list(row.get("evidence"))[:8],
        )
        for row in findings
        if str(row.get("summary", "")).strip()
    ]
    compressed.review_findings = _merge_review_rows(
        [*compressed.review_findings, *review_rows],
        limit=48,
    )

    repair_rows = [
        CompactedRepairLesson(
            failure=_clip(str(row.get("failure_summary", "")), 320),
            fix_attempt=_clip(str(row.get("attempted_fix", "")), 320),
            outcome=_clip(str(row.get("status", "")), 160),
            evidence=_string_list(row.get("artifacts"))[:8],
        )
        for row in repairs
        if str(row.get("failure_summary", "")).strip() or str(row.get("attempted_fix", "")).strip()
    ]
    compressed.repair_lessons = _merge_repair_rows(
        [*compressed.repair_lessons, *repair_rows],
        limit=48,
    )

    blocking = [
        _clip(str(row.get("summary", "")), 260)
        for row in findings
        if str(row.get("severity", "")).lower() == "blocking" and str(row.get("summary", "")).strip()
    ]
    compressed.open_risks = _merge_unique([*compressed.open_risks, *memory.open_questions, *blocking], limit=36)
    return compressed


def _write_compacted_memory(run_dir: Path, compressed: CompactedTaskMemory) -> None:
    paths = code_task_memory_paths(run_dir)
    paths.memory_dir.mkdir(parents=True, exist_ok=True)
    write_json(paths.compressed_memory_json, compressed.model_dump(mode="json"))
    write_text(paths.compressed_memory_md, _render_compacted_memory_markdown(compressed))
    _update_manifest_memory_layout(run_dir)


def _archive_compaction_input(
    run_dir: Path,
    *,
    compressed: CompactedTaskMemory,
    event_count: int,
    edit_count: int,
    finding_count: int,
    repair_count: int,
) -> None:
    paths = code_task_memory_paths(run_dir)
    paths.archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow_iso().replace(":", "").replace("+", "Z")
    write_json(
        paths.archive_dir / f"compaction-{stamp}.json",
        {
            "schema_version": "code_task_memory_compaction_archive.v1",
            "created_at": utcnow_iso(),
            "compressed_memory": _artifact_ref(run_dir, paths.compressed_memory_json),
            "compacted_counts": {
                "events": event_count,
                "edit_history": edit_count,
                "review_findings": finding_count,
                "repair_memory": repair_count,
            },
            "source": compressed.source,
        },
    )


def _rewrite_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        write_text(path, "")
        return
    write_text(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def _update_manifest_memory_layout(run_dir: Path) -> None:
    try:
        manifest = load_code_task_manifest(run_dir)
    except Exception:
        return
    layout = manifest.get("layout")
    layout = layout if isinstance(layout, dict) else {}
    paths = code_task_memory_paths(run_dir)
    layout.update(
        {
            "task_memory": _artifact_ref(run_dir, paths.task_memory_json),
            "task_memory_markdown": _artifact_ref(run_dir, paths.task_memory_md),
            "compressed_memory": _artifact_ref(run_dir, paths.compressed_memory_json),
            "compressed_memory_markdown": _artifact_ref(run_dir, paths.compressed_memory_md),
            "edit_history": _artifact_ref(run_dir, paths.edit_history_jsonl),
            "review_findings": _artifact_ref(run_dir, paths.review_findings_jsonl),
            "repair_memory": _artifact_ref(run_dir, paths.repair_memory_jsonl),
        }
    )
    manifest["layout"] = layout
    manifest["memory"] = {
        "status": "ready",
        "path": _artifact_ref(run_dir, paths.task_memory_json),
        "markdown": _artifact_ref(run_dir, paths.task_memory_md),
        "compressed": _artifact_ref(run_dir, paths.compressed_memory_json),
        "updated_at": utcnow_iso(),
    }
    save_code_task_manifest(run_dir, manifest)


def _artifact_ref(run_dir: Path, path: Path) -> str:
    root = Path(run_dir)
    target = Path(path)
    try:
        return target.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return "../" + target.relative_to(root.parent).as_posix()
    except ValueError:
        return str(target)


def _merge_unique(values: list[str] | tuple[str, ...], *, limit: int) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clip(str(value), 800).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
    return rows[-limit:]


def _merge_evidence_rows(rows: list[CompactedEvidenceRow], *, limit: int) -> list[CompactedEvidenceRow]:
    merged: dict[str, CompactedEvidenceRow] = {}
    for row in rows:
        if not row.summary:
            continue
        existing = merged.get(row.summary)
        if existing is None:
            merged[row.summary] = row
        else:
            existing.evidence = _merge_unique([*existing.evidence, *row.evidence], limit=12)
    return list(merged.values())[-limit:]


def _merge_review_rows(rows: list[CompactedReviewFinding], *, limit: int) -> list[CompactedReviewFinding]:
    merged: dict[str, CompactedReviewFinding] = {}
    for row in rows:
        if not row.summary:
            continue
        key = f"{row.severity}:{row.summary}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
        else:
            existing.evidence = _merge_unique([*existing.evidence, *row.evidence], limit=12)
    return list(merged.values())[-limit:]


def _merge_repair_rows(rows: list[CompactedRepairLesson], *, limit: int) -> list[CompactedRepairLesson]:
    merged: dict[str, CompactedRepairLesson] = {}
    for row in rows:
        key = f"{row.failure}:{row.fix_attempt}:{row.outcome}"
        if not key.strip(":"):
            continue
        existing = merged.get(key)
        if existing is None:
            merged[key] = row
        else:
            existing.evidence = _merge_unique([*existing.evidence, *row.evidence], limit=12)
    return list(merged.values())[-limit:]


def _review_severity(value: object) -> Literal["blocking", "warning", "info"]:
    text = str(value or "").strip().lower()
    if text in {"blocking", "warning", "info"}:
        return text  # type: ignore[return-value]
    if text in {"error", "failed", "failure", "critical"}:
        return "blocking"
    if text in {"warn", "risk"}:
        return "warning"
    return "info"


def _constraints_from_manifest(run_dir: Path) -> list[str]:
    try:
        manifest = load_code_task_manifest(run_dir)
    except Exception:
        return []
    constraints: list[str] = []
    workspace = manifest.get("workspace")
    if isinstance(workspace, dict):
        mode = workspace.get("mode") or workspace.get("strategy")
        if mode:
            constraints.append(f"Workspace mode: {mode}.")
    scope = manifest.get("edit_scope")
    if isinstance(scope, dict):
        allowed = _string_list(scope.get("allowed_patterns") or scope.get("allowed"))
        protected = _string_list(scope.get("protected_patterns") or scope.get("protected"))
        if allowed:
            constraints.append("Editable scope: " + ", ".join(f"`{item}`" for item in allowed[:8]) + ".")
        if protected:
            constraints.append("Protected scope: " + ", ".join(f"`{item}`" for item in protected[:8]) + ".")
    benchmark = manifest.get("benchmark")
    if isinstance(benchmark, dict) and benchmark.get("command"):
        constraints.append(f"Benchmark command: `{benchmark.get('command')}`.")
    return constraints


def _append_unique_jsonl(path: Path, row: dict[str, Any], *, key: str) -> None:
    rows = _read_jsonl_safe(path)
    kept = [item for item in rows if item.get("key") != key]
    path.parent.mkdir(parents=True, exist_ok=True)
    if kept:
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in kept)
        write_text(path, text)
    else:
        write_text(path, "")
    append_jsonl(path, row)


def _read_jsonl_safe(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        return [row for row in read_jsonl(path) if isinstance(row, dict)]
    except Exception:
        return []


def _read_optional_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return read_text(path)
    except OSError:
        return ""


def _stable_key(prefix: str, summary: str, artifacts: list[str] | tuple[str, ...]) -> str:
    payload = "\n".join([prefix, summary, *artifacts])
    digest = hashlib.sha1(payload.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _small_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[str(key)] = _clip(str(value), 300) if isinstance(value, str) else value
        elif isinstance(value, list):
            result[str(key)] = _dedupe_strings(value)[:20]
        elif isinstance(value, dict):
            result[str(key)] = {str(k): _clip(str(v), 160) for k, v in list(value.items())[:20]}
    return result


def _dedupe_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip().replace("\\", "/")
        if text and text not in seen:
            seen.add(text)
            rows.append(text)
    return rows


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _artifact_suffix(artifacts: list[str]) -> str:
    if not artifacts:
        return ""
    shown = ", ".join(f"`{item}`" for item in artifacts[:4])
    if len(artifacts) > 4:
        shown += f", ... +{len(artifacts) - 4}"
    return f" ({shown})"


def _clip(text: str, limit: int) -> str:
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: max(0, limit - 3)].rstrip() + "..."
