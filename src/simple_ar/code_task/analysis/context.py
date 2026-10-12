from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simple_ar.core.artifacts import read_json, read_jsonl, read_text, write_json, write_jsonl, write_text
from simple_ar.code_task.analysis.locate import locate_code_task_context
from simple_ar.code_task.analysis.index import SOURCE_SUFFIXES, read_code_task_text
from simple_ar.retrieval.index import _looks_like_text
from simple_ar.code_task.runtime.state import (
    code_task_paths,
    is_relative_to,
    load_code_task_manifest,
    save_code_task_manifest,
    utcnow_iso,
    workspace_file,
)


DEFAULT_CONTEXT_TOP_K = 8
DEFAULT_MAX_FILES = 8
DEFAULT_MAX_SOURCE_CHARS_PER_FILE = 4_000
DEFAULT_MAX_TOTAL_CHARS = 20_000


@dataclass(frozen=True)
class CodeTaskContextPackResult:
    """Result returned after building a bounded code-task context pack.

    Args:
        run_dir: Code-task run directory.
        context_dir: Directory containing the generated context pack.
        context_pack_path: JSON manifest for the context pack.
        prompt_context_path: Markdown prompt-ready context.
        snippets_path: JSONL rows containing selected source snippets.
        locate_results_path: Source locate artifact used for selection.
        selected_files: Workspace-relative files included in the context pack.
    """

    run_dir: Path
    context_dir: Path
    context_pack_path: Path
    prompt_context_path: Path
    snippets_path: Path
    locate_results_path: Path
    selected_files: tuple[str, ...]


@dataclass(frozen=True)
class LoadedCodeTaskContextPack:
    """Latest context pack loaded from a code-task run.

    Args:
        run_dir: Code-task run directory.
        context_pack_path: Absolute path to ``context_pack.json``.
        prompt_context_path: Absolute path to ``prompt_context.md``.
        snippets_path: Absolute path to ``selected_snippets.jsonl``.
        context_pack: Parsed context-pack JSON object.
        snippets: Parsed selected snippet rows.
        selected_files: Workspace-relative selected file paths.
    """

    run_dir: Path
    context_pack_path: Path
    prompt_context_path: Path
    snippets_path: Path
    context_pack: dict[str, Any]
    snippets: tuple[dict[str, Any], ...]
    selected_files: tuple[str, ...]

    def selected_paths(self, *, max_files: int) -> list[str]:
        """Retain saved selection order and the established positive file limit."""
        return list(dict.fromkeys(self.selected_files))[:max(1, max_files)]

    def manifest_reference(self, run_dir: Path) -> dict[str, Any]:
        """Shared saved-pack reference for planning, work planning and editing."""
        def relative(path: Path) -> str:
            try:
                return path.resolve().relative_to(run_dir.resolve()).as_posix()
            except ValueError:
                return str(path)

        budget = self.context_pack.get("budget")
        return {"path": relative(self.context_pack_path),
                "prompt_context": relative(self.prompt_context_path),
                "snippets": relative(self.snippets_path),
                "selected_files": list(self.selected_files),
                "budget": budget if isinstance(budget, dict) else {}}


def build_code_task_context_pack(
    run_dir: Path,
    *,
    query: str | None = None,
    top_k: int = DEFAULT_CONTEXT_TOP_K,
    max_files: int = DEFAULT_MAX_FILES,
    max_source_chars_per_file: int = DEFAULT_MAX_SOURCE_CHARS_PER_FILE,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    refresh_map: bool = False,
    preferred_paths: tuple[str, ...] | None = None,
) -> CodeTaskContextPackResult:
    """Build a prompt-ready context pack from locate results and workspace files.

    Args:
        run_dir: Code-task run directory created by ``code-task init``.
        query: Optional locate query. When omitted, ``code_task/task.md`` is
            used by the locate step.
        top_k: Candidate budget passed to locate for each candidate group.
        max_files: Maximum snippets included across editable and evidence
            groups.
        max_source_chars_per_file: Per-file snippet character budget.
        max_total_chars: Total text budget across all snippets.
        refresh_map: Rebuild repo-map artifacts before locating context.
        preferred_paths: Current batch targets to read first. By default use
            existing indexed paths in the saved plan's Files To Modify section.
            Reading priority does not grant edit permission.

    Returns:
        Paths and selected files for the generated context pack.

    Raises:
        ValueError: If any budget is less than one.
        FileNotFoundError: If required code-task artifacts are missing.
        RuntimeError: If ``run_dir`` is not a code-task workflow.
    """

    if top_k < 1:
        raise ValueError("top_k must be at least 1")
    if max_files < 1:
        raise ValueError("max_files must be at least 1")
    if max_source_chars_per_file < 1:
        raise ValueError("max_source_chars_per_file must be at least 1")
    if max_total_chars < 1:
        raise ValueError("max_total_chars must be at least 1")

    root = Path(run_dir)
    paths = code_task_paths(root)
    manifest = load_code_task_manifest(root)
    requirements = manifest.get("context_requirements")
    required_paths = (
        [str(path) for path in requirements.get("read_only_paths", []) if isinstance(path, str)]
        if isinstance(requirements, dict) and isinstance(requirements.get("read_only_paths"), list)
        else []
    )
    locate = locate_code_task_context(
        root,
        query=query,
        top_k=top_k,
        refresh_map=refresh_map,
        include_read_only=True,
    )
    locate_data = read_json(locate.results_path)
    if not isinstance(locate_data, dict):
        raise RuntimeError(f"Expected JSON object in {locate.results_path}")
    repo_map = read_json(paths.meta_dir / "repo_map.json")
    known_files = {
        str(item.get("path")): item for item in repo_map.get("files", [])
        if isinstance(item, dict)
    } if isinstance(repo_map, dict) else {}
    plan_path = paths.task_dir / "patch_plan.md"
    preferred = (planned_context_paths(read_text(plan_path), set(known_files))
                 if preferred_paths is None and plan_path.is_file() else list(preferred_paths or ()))
    prioritized = [dict(known_files[path], path=path,
                        reasons=["current planned target; read priority only"])
                   for path in dict.fromkeys(preferred) if path in known_files]
    # The plan can identify a file absent from coarse keyword retrieval. Read it
    # before incidental dependency/environment matches, retaining index roles.
    locate_data["editable_targets"] = [row for row in prioritized if row.get("access_role") == "editable"] + _object_list(locate_data.get("editable_targets"))
    locate_data["read_only_evidence"] = [row for row in prioritized if row.get("access_role") != "editable"] + _object_list(locate_data.get("read_only_evidence"))
    seeds = prioritized or _object_list(locate_data.get("editable_targets"))[:1]
    seed_paths = {row["path"] for row in seeds}
    # One-hop indexed imports provide collaborating files that coarse keyword
    # ranking misses. No traversal, new graph, permissions or extra file budget.
    locate_data["preferred_context"] = prioritized
    # Reuse the existing symbol inventory to start a source window at a named
    # definition, instead of spending it on an unrelated file prefix.
    locate_data["symbols"] = repo_map.get("symbols", []) if isinstance(repo_map, dict) else []
    locate_data["import_context"] = [dict(known_files[path], reasons=[f"static local import of {seed['path']}; read priority only"])
        for seed in seeds for path in known_files[seed["path"]].get("local_import_paths", [])
        if path in known_files and path not in seed_paths]
    missing = [path for path in required_paths if path not in known_files]
    if missing:
        raise ValueError(f"Required code-task context is absent from the workspace index: {missing}")
    if len(set(required_paths)) >= max_files:
        raise ValueError("Context file budget cannot hold required evidence and an editable target.")
    for path in required_paths:
        target = workspace_file(paths.workspace_dir, path)
        if (target is None or target.name.startswith(".env") or not target.is_file()
                or target.stat().st_size > 500_000):
            raise ValueError(f"Required code-task context is unsafe or unreadable: {path}")
    required_candidates = [
        {"path": path, "access_role": "read_only_evidence", "score": 0,
         "reasons": ["required read-only context"]}
        for path in dict.fromkeys(required_paths)
    ]

    context_dir = _next_context_dir(paths.task_dir / "context_packs")
    snippets_path = context_dir / "selected_snippets.jsonl"
    context_pack_path = context_dir / "context_pack.json"
    prompt_context_path = context_dir / "prompt_context.md"

    snippets, omitted = _collect_snippets(
        paths.workspace_dir,
        locate_data,
        required_candidates=required_candidates,
        max_files=max_files,
        max_chars_per_file=max_source_chars_per_file,
        max_total_chars=max_total_chars,
    )
    absent = [path for path in required_paths if path not in {row["path"] for row in snippets}]
    if absent:
        raise ValueError(f"Required code-task context could not be read: {absent}")
    context_pack = {
        "schema_version": 1,
        "generated_at": utcnow_iso(),
        "query": locate_data.get("query", ""),
        "source": {
            "locate_results": "code_task/meta/locate_results.json",
            "repo_map": "code_task/meta/repo_map.json",
        },
        "budget": {
            "top_k": top_k,
            "max_files": max_files,
            "max_source_chars_per_file": max_source_chars_per_file,
            "max_total_chars": max_total_chars,
            "used_chars": sum(int(row.get("chars", 0)) for row in snippets),
        },
        "selected_files": [
            _snippet_manifest_row(row)
            for row in snippets
        ],
        "required_read_only_paths": list(dict.fromkeys(required_paths)),
        "preferred_paths": [row["path"] for row in prioritized],
        "omitted": omitted,
        "artifacts": {
            "selected_snippets": "selected_snippets.jsonl",
            "prompt_context": "prompt_context.md",
        },
    }
    write_jsonl(snippets_path, snippets)
    write_json(context_pack_path, context_pack)
    write_text(prompt_context_path, render_prompt_context(context_pack, snippets))
    _update_manifest_after_context(root, manifest, context_dir, context_pack)
    return CodeTaskContextPackResult(
        run_dir=root,
        context_dir=context_dir,
        context_pack_path=context_pack_path,
        prompt_context_path=prompt_context_path,
        snippets_path=snippets_path,
        locate_results_path=locate.results_path,
        selected_files=tuple(str(row["path"]) for row in snippets),
    )


def planned_context_paths(patch_plan: str, known_paths: set[str]) -> list[str]:
    """Read target references from the canonical saved plan, not its run log.

    Only exact indexed paths in Files To Modify qualify. Task excerpts,
    validation commands and unknown/outside paths cannot become targets here.
    Existing plans need no migration or second persisted source of truth.
    """
    section = re.search(r"(?mi)^## Files To Modify[ \t]*\r?$", patch_plan)
    if section is None:
        return []
    body = patch_plan[section.end():]
    end = re.search(r"(?m)^## ", body)
    body = body[:end.start()] if end else body
    return list(dict.fromkeys(path for path in re.findall(r"`([^`\r\n]+)`", body)
                              if path in known_paths))


def load_latest_code_task_context_pack(run_dir: Path) -> LoadedCodeTaskContextPack | None:
    """Load the latest context pack recorded in ``manifest.json``.

    Args:
        run_dir: Code-task run directory.

    Returns:
        Loaded context-pack data, or ``None`` when no latest context pack has
        been generated yet.

    Raises:
        RuntimeError: If the manifest points outside the run directory or a
            context-pack artifact is malformed.
    """

    root = Path(run_dir)
    manifest = load_code_task_manifest(root)
    context_section = manifest.get("context_pack")
    if not isinstance(context_section, dict):
        return None
    latest = context_section.get("latest")
    if not isinstance(latest, str) or not latest.strip():
        return None
    context_pack_path = _run_file(root, latest)
    if context_pack_path is None or not context_pack_path.is_file():
        return None
    context_pack = read_json(context_pack_path)
    if not isinstance(context_pack, dict):
        raise RuntimeError(f"Expected JSON object in {context_pack_path}")

    artifacts = context_pack.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    snippets_name = artifacts.get("selected_snippets", "selected_snippets.jsonl")
    prompt_name = artifacts.get("prompt_context", "prompt_context.md")
    snippets_path = _context_child(context_pack_path.parent, str(snippets_name))
    prompt_context_path = _context_child(context_pack_path.parent, str(prompt_name))
    snippets = tuple(row for row in read_jsonl(snippets_path) if isinstance(row, dict))
    # Reject a legacy pack that decoded inventory binaries as source. The
    # existing ensure owner builds a NEW pack; old receipts/snippets stay intact.
    # Known source paths need no new source read just to consume cached text.
    for row in snippets:
        target = workspace_file(code_task_paths(root).workspace_dir, str(row.get("path", "")))
        if "\0" in str(row.get("text", "")) or (target is not None and target.is_file()
                and target.suffix.lower() not in SOURCE_SUFFIXES and not _looks_like_text(target)):
            return None
    return LoadedCodeTaskContextPack(
        run_dir=root,
        context_pack_path=context_pack_path,
        prompt_context_path=prompt_context_path,
        snippets_path=snippets_path,
        context_pack=context_pack,
        snippets=snippets,
        selected_files=tuple(_snippet_paths(snippets)),
    )


def ensure_code_task_context_pack(
    run_dir: Path, *, query: str, max_files: int, max_source_chars_per_file: int,
) -> LoadedCodeTaskContextPack | None:
    """Share planning retrieval; rebuild only absent/non-text legacy packs.

    Ordinary and staged model planning need the same named definitions, local
    collaborators and exact offsets before decomposing edits. Offline planning
    keeps its existing no-retrieval path. A rebuild uses the existing numbered
    directories and manifest references, with the caller's file/character allowance.
    """
    loaded = load_latest_code_task_context_pack(run_dir)
    if loaded is None:
        build_code_task_context_pack(run_dir, query=query, top_k=max(8, max_files * 2),
            max_files=max_files, max_source_chars_per_file=max_source_chars_per_file,
            max_total_chars=max_files * max_source_chars_per_file)
        loaded = load_latest_code_task_context_pack(run_dir)
    return loaded


def render_prompt_context(
    context_pack: dict[str, Any],
    snippets: list[dict[str, Any]],
) -> str:
    """Render a context pack into Markdown suitable for an LLM prompt."""

    editable = [row for row in snippets if row.get("access_role") == "editable"]
    evidence = [row for row in snippets if row.get("access_role") != "editable"]
    budget = context_pack.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    omitted = context_pack.get("omitted")
    omitted = omitted if isinstance(omitted, dict) else {}
    lines = [
        "# Code Task Context Pack",
        "",
        f"Generated: `{context_pack.get('generated_at', '')}`",
        "",
        "## Query",
        "",
        _truncate(str(context_pack.get("query", "")).strip(), 1000) or "(empty)",
        "",
        "## Budget",
        "",
        f"- Selected files: `{len(snippets)}` / `{budget.get('max_files', '')}`",
        f"- Used chars: `{budget.get('used_chars', 0)}` / `{budget.get('max_total_chars', '')}`",
        "",
        "## Editable Targets",
        "",
        _render_snippet_group(editable),
        "",
        "## Read-Only Evidence",
        "",
        _render_snippet_group(evidence),
        "",
        "## Omitted",
        "",
        f"- Candidate files omitted by file budget: `{omitted.get('candidate_files', 0)}`",
        f"- Snippets omitted by char budget: `{omitted.get('char_budget_files', 0)}`",
        f"- Missing/unreadable files: `{omitted.get('missing_or_unreadable_files', 0)}`",
        "",
    ]
    return "\n".join(lines)


def _collect_snippets(
    workspace_dir: Path,
    locate_data: dict[str, Any],
    *,
    required_candidates: list[dict[str, Any]] | None = None,
    max_files: int,
    max_chars_per_file: int,
    max_total_chars: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = _ordered_candidates(locate_data, required_candidates or [])
    snippets: list[dict[str, Any]] = []
    omitted = {
        "candidate_files": 0,
        "char_budget_files": 0,
        "missing_or_unreadable_files": 0,
        "details": [],
    }
    used_chars = 0
    seen: set[str] = set()
    for candidate in candidates:
        path = str(candidate.get("path", "")).strip()
        if not path or path in seen:
            continue
        seen.add(path)
        if len(snippets) >= max_files:
            omitted["candidate_files"] += 1
            _append_detail(omitted, path, "file_budget")
            continue
        remaining = max_total_chars - used_chars
        if remaining <= 0:
            omitted["char_budget_files"] += 1
            _append_detail(omitted, path, "total_char_budget")
            continue
        file_path = workspace_file(workspace_dir, path)
        if file_path is None or not file_path.is_file():
            omitted["missing_or_unreadable_files"] += 1
            _append_detail(omitted, path, "missing_or_outside_workspace")
            continue
        text = read_code_task_text(file_path)
        if text is None:
            omitted["missing_or_unreadable_files"] += 1
            _append_detail(omitted, path, "non_text_or_unreadable")
            continue
        limit = min(max_chars_per_file, remaining)
        start = _named_definition_offset(text, path, str(locate_data.get("query", "")),
            _object_list(locate_data.get("symbols")))
        snippet_text = text[start:start + limit]
        truncated = start > 0 or start + len(snippet_text) < len(text)
        snippets.append(
            {
                "schema_version": 1,
                "path": path,
                "access_role": candidate.get("access_role", "editable"),
                "score": candidate.get("score", 0),
                "reasons": candidate.get("reasons", []),
                "chars": len(snippet_text),
                "source_chars": len(text),
                "truncated": truncated,
                "source_offset": start,
                "start_line": text.count("\n", 0, start) + 1,
                "end_line": text.count("\n", 0, start + max(0, len(snippet_text) - 1)) + 1,
                "has_unread_tail": start + len(snippet_text) < len(text),
                "text": snippet_text,
            }
        )
        used_chars += len(snippet_text)
    return snippets, omitted


def definition_start_line(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Decorators are part of a Python definition's executable contract."""
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])


def _named_definition_offset(text: str, path: str, query: str, symbols: list[dict[str, Any]]) -> int:
    """Prefer exact named definitions from the existing map; do not infer intent.

    Qualified names disambiguate methods. A bare name shared by several
    definitions is not guessed. A stale/invalid line anchor falls back to the
    historical prefix; the window is reading evidence, not edit permission.
    """
    definitions = [row for row in symbols if row.get("path") == path]
    counts: dict[str, int] = {}
    for row in definitions:
        name = str(row.get("name", ""))
        counts[name] = counts.get(name, 0) + 1
    candidates = []
    lines = text.splitlines(keepends=True)
    for row in definitions:
        name, qualified = str(row.get("name", "")), str(row.get("qualified_name", ""))
        choices = [qualified] if qualified and qualified != name else []
        if name and counts[name] == 1:
            choices.append(name)
        line = row.get("line_start")
        if type(line) is not int or not 1 <= line <= len(lines):
            continue
        if not re.match(rf"\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b", lines[line - 1]):
            continue
        for choice in choices:
            hit = re.search(rf"(?<!\w){re.escape(choice)}(?!\w)", query, flags=re.IGNORECASE)
            if hit:
                candidates.append((hit.start(), -len(choice), line))
    if not candidates:
        return 0
    line = min(candidates)[2]
    try:
        node = next((node for node in ast.walk(ast.parse(text))
                     if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.lineno == line), None)
        if node is not None:
            line = definition_start_line(node)
    except SyntaxError:
        pass  # Preserve the existing validated declaration anchor for incomplete source.
    return sum(len(row) for row in lines[:line - 1])


def clip_source_snippet(row: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    """Preserve exact source coordinates through planning/editing projections.

    Text markers are presentation, not source characters. Never insert them
    into a window that later supplies continuation offsets or exact edits.
    Legacy prefix windows have offset zero; no migration or new persisted state.
    """
    text = row["text"][:max_chars]
    offset = row.get("source_offset", 0)
    source_chars = row.get("source_chars", offset + len(row["text"]))
    result = {**row, "text": text, "source_offset": offset, "source_chars": source_chars,
        "truncated": bool(row.get("truncated")) or len(text) < len(row["text"]),
        "has_unread_tail": offset + len(text) < source_chars}
    if type(row.get("start_line")) is int:
        result["end_line"] = row["start_line"] + text[:max(0, len(text) - 1)].count("\n")
    return result


def refresh_source_snippets(
    previous: list[dict[str, Any]], incoming: list[dict[str, Any]], *,
    preferred_paths: set[str], max_total_chars: int,
) -> list[dict[str, Any]]:
    """Bound the current prompt, not the lifetime of observed source.

    New requested evidence takes precedence over old navigation snippets. Exact
    coordinates and roles survive clipping; the caller retains original records.
    """
    rows = incoming + sorted(previous, key=lambda row: row["path"] not in preferred_paths)
    selected, seen = [], set()
    remaining = max(0, max_total_chars)
    for row in rows:
        identity = (row["path"], row.get("access_role"), row.get("source_offset", 0), row["text"])
        if remaining <= 0:
            break
        if identity in seen or not row["text"]:
            continue
        seen.add(identity)
        selected.append(clip_source_snippet(row, max_chars=remaining))
        remaining -= len(selected[-1]["text"])
    return selected


def read_source_snippets(
    workspace_dir: Path, selected_files: list[str], *, max_chars_per_file: int,
    editable_files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Read bounded exact prefixes; the caller owns edit authorization.

    This is the index-selection fallback for planning and editing. Use the
    same coordinates as saved context packs, not presentation markers in code.
    """
    snippets: list[dict[str, Any]] = []
    for rel_path in selected_files:
        path = workspace_file(workspace_dir, rel_path)
        if path is None or not path.is_file() or path.name.startswith(".env"):
            continue
        text = read_code_task_text(path)
        if text is None:
            continue
        snippets.append(clip_source_snippet(
            {"path": rel_path, "text": text, "source_chars": len(text), "start_line": 1,
             "access_role": "editable" if editable_files is None or rel_path in editable_files else "read_only"},
            max_chars=max(200, max_chars_per_file),
        ))
    return snippets


def _ordered_candidates(
    locate_data: dict[str, Any], required_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    editable = _object_list(locate_data.get("editable_targets"))
    evidence = _object_list(locate_data.get("read_only_evidence"))
    combined: list[dict[str, Any]] = []
    combined.extend(editable[:1])
    combined.extend(required_candidates)
    combined.extend(_object_list(locate_data.get("preferred_context")))
    combined.extend(_object_list(locate_data.get("import_context")))
    combined.extend(editable[1:])
    combined.extend(evidence)
    return combined


def _next_context_dir(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    max_id = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        match = re.fullmatch(r"context-(\d{3})", child.name)
        if match:
            max_id = max(max_id, int(match.group(1)))
    return root / f"context-{max_id + 1:03d}"


def _snippet_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": row.get("path"),
        "access_role": row.get("access_role"),
        "score": row.get("score"),
        "chars": row.get("chars"),
        "source_chars": row.get("source_chars"),
        "truncated": row.get("truncated"),
        "source_offset": row.get("source_offset", 0),
        "start_line": row.get("start_line"),
        "end_line": row.get("end_line"),
        "has_unread_tail": row.get("has_unread_tail"),
        "reasons": row.get("reasons", []),
    }


def _update_manifest_after_context(
    run_dir: Path,
    manifest: dict[str, Any],
    context_dir: Path,
    context_pack: dict[str, Any],
) -> None:
    rel_context = context_dir.relative_to(run_dir).as_posix()
    layout = manifest.get("layout")
    if not isinstance(layout, dict):
        layout = {}
    layout.update(
        {
            "context_packs": "code_task/context_packs",
            "latest_context_pack": f"{rel_context}/context_pack.json",
            "latest_prompt_context": f"{rel_context}/prompt_context.md",
        }
    )
    manifest["layout"] = layout
    manifest["context_pack"] = {
        "status": "completed",
        "generated_at": context_pack.get("generated_at"),
        "latest": f"{rel_context}/context_pack.json",
        "prompt_context": f"{rel_context}/prompt_context.md",
        "selected_files": [
            row.get("path")
            for row in _object_list(context_pack.get("selected_files"))
            if row.get("path")
        ],
        "budget": context_pack.get("budget", {}),
    }
    save_code_task_manifest(run_dir, manifest)


def _run_file(run_dir: Path, relative_path: str) -> Path | None:
    rel = Path(relative_path)
    if rel.is_absolute() or ".." in rel.parts:
        return None
    root = run_dir.resolve()
    path = (root / rel).resolve()
    if not is_relative_to(path, root):
        return None
    return path


def _context_child(context_dir: Path, name: str) -> Path:
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise RuntimeError(f"Context-pack artifact path escapes context directory: {name}")
    root = context_dir.resolve()
    path = (root / rel).resolve()
    if not is_relative_to(path, root):
        raise RuntimeError(f"Context-pack artifact path escapes context directory: {name}")
    if not path.is_file():
        raise FileNotFoundError(f"Missing context-pack artifact: {path}")
    return path


def _snippet_paths(snippets: tuple[dict[str, Any], ...]) -> list[str]:
    paths: list[str] = []
    for row in snippets:
        path = row.get("path")
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return paths


def _render_snippet_group(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "- No snippets selected."
    blocks: list[str] = []
    for row in rows:
        path = str(row.get("path", ""))
        role = str(row.get("access_role", ""))
        reason = "; ".join(str(item) for item in row.get("reasons", []) if item)
        header = [
            f"### `{path}`",
            "",
            f"- Role: `{role}`",
            f"- Score: `{row.get('score', 0)}`",
            f"- Chars: `{row.get('chars', 0)}` / `{row.get('source_chars', 0)}`",
        ]
        if reason:
            header.append(f"- Why: {_truncate(reason, 220)}")
        header.extend(["", "~~~text", str(row.get("text", "")), "~~~"])
        blocks.append("\n".join(header))
    return "\n\n".join(blocks)


def _append_detail(omitted: dict[str, Any], path: str, reason: str) -> None:
    details = omitted.get("details")
    if not isinstance(details, list):
        details = []
        omitted["details"] = details
    details.append({"path": path, "reason": reason})


def _object_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _truncate(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."
