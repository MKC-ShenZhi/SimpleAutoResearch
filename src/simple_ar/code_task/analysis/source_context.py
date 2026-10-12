"""Bounded, read-only source lookup shared by editing and research design."""

import ast
import os
import re
from pathlib import Path
from typing import Any, Sequence

from simple_ar.code_task.analysis.index import IGNORED_DIR_NAMES, SOURCE_SUFFIXES, is_python_environment, read_code_task_text
from simple_ar.code_task.analysis.context import clip_source_snippet, definition_start_line
from simple_ar.code_task.analysis.interfaces import source_snippet_views
from simple_ar.code_task.editing.planning import select_relevant_files
from simple_ar.code_task.runtime.state import workspace_file


CONTEXT_SUFFIXES = SOURCE_SUFFIXES | {".toml", ".yaml", ".yml", ".md", ".txt", ".json", ".ini", ".cfg"}


def inferred_source_request(
    snippets: list[dict[str, Any]], preferred_files: list[str], max_files: int,
    *, index: dict[str, Any] | None = None, proposal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Locate named indexed definitions; reading is not edit authorization.

    Prefer a visible owner when names collide; otherwise only a unique owner
    qualifies. No inferred filename, imported binding or missing definition is
    treated as established. Explicit requests remain the caller's first choice.
    """
    targets = list(dict.fromkeys(str(item.get("path")) for item in source_snippet_views(snippets)
        if item.get("truncated") and item.get("path") in preferred_files))
    explanation = [str((proposal or {}).get("summary", "")),
                   *[row for row in ((proposal or {}).get("validation") or []) if isinstance(row, str)]]
    mentioned = set(re.findall(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", "\n".join(explanation)))
    owners: dict[str, list[str]] = {}
    for row in (index or {}).get("files", []):
        python = row.get("python") or {}
        definitions = [str(item.get("name", "")) for item in
                       [*python.get("functions", []), *python.get("classes", [])]]
        definitions.extend(f"{klass['name']}.{method['name']}" for klass in python.get("classes", [])
                           for method in klass.get("methods", []))
        for name in dict.fromkeys(definitions):
            if name in mentioned:
                owners.setdefault(name, []).append(row["path"])
    visible = {item["path"] for item in snippets}
    named: dict[str, list[str]] = {}
    for name, paths in owners.items():
        candidates = [path for path in paths if path in visible] if len(paths) > 1 else paths
        if len(candidates) == 1:
            named.setdefault(candidates[0], []).append(name)
    selected = list(dict.fromkeys([*(path for path in named if path not in visible),
        *(path for path in targets if path in named), *named]))[:max_files] if named else targets[:max_files]
    return {"files": selected, "query": "", "symbols": list(dict.fromkeys(
                name for path in selected for name in named.get(path, []))),
            "dependency_symbols": [],
            "reason": "Bounded lookup of definitions named in the proposal explanation" if named else
                      "Bounded continuation of truncated editable source"}


def diff_source_anchors(patch_diff: str) -> list[tuple[str, int]]:
    """Current-file line coordinates from unified diff hunks, not old source."""
    anchors: list[tuple[str, int]] = []
    path = ""
    for line in patch_diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t", 1)[0].removeprefix("b/")
        elif path and (match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)):
            anchors.append((path, max(1, int(match.group(1)))))
    return anchors


def source_context_for_files(
    workspace_dir: Path, selected_files: list[str], *, max_chars_per_file: int,
    anchors: Sequence[tuple[str, int]] = (), editable_files: list[str] | None = None,
    include_unanchored_tail: bool = False,
) -> list[dict[str, Any]]:
    """Exact, bounded current-source views shared by review and repair.

    Up to two anchors share each file's original character budget. Small
    files remain whole; stale coordinates fall back to a bounded prefix.
    Reading source never expands the caller's edit authorization.
    """
    snippets: list[dict[str, Any]] = []
    limit = max(0, max_chars_per_file)
    for rel_path in dict.fromkeys(selected_files):
        if limit == 0:
            continue
        lines = list(dict.fromkeys(line for anchor, line in anchors
            if anchor == rel_path or anchor.endswith("/" + rel_path)))[:2]
        text = read_source_context_text(workspace_dir, rel_path, indexed=True)
        if text is None:
            continue
        if len(text) <= limit:
            lines = []
        observed: list[dict[str, Any]] = []
        remaining = limit
        source = {"path": rel_path, "text": text, "source_chars": len(text),
                  "source_offset": 0, "start_line": 1}
        offsets = [0]
        for source_line in text.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(source_line))
        for position, line in enumerate(lines or [None]):
            if line is not None and (type(line) is not int or line + 75 < max(1, line - 4)):
                raise ValueError("A source line_range needs one file, 1-200 lines, and no search terms.")
            start_line = max(1, line - 4) if line is not None else 1
            if start_line >= len(offsets) and text:
                continue
            start = offsets[min(start_line - 1, len(offsets) - 1)]
            end = offsets[min(line + 75, len(offsets) - 1)] if line is not None else len(text)
            size = remaining // (len(lines) - position) if lines else remaining
            gaps = _unseen_intervals(start, end, [(row["source_offset"],
                row["source_offset"] + len(row["text"])) for row in observed])
            if gaps and size > 0:
                start, end = max(gaps, key=lambda gap: min(size, gap[1] - gap[0]))
                row = clip_source_snippet({**source, "text": text[start:end], "source_offset": start,
                    "start_line": text.count("\n", 0, start) + 1,
                    "truncated": start > 0 or end < len(text)}, max_chars=size)
                observed.append(row)
                remaining -= len(row["text"])
        if not observed and lines:
            observed = [clip_source_snippet(source, max_chars=limit)]
        if not lines and include_unanchored_tail and len(text) > limit and limit > 1:
            # Generated-project review has no diff. Preserve its head/tail
            # coverage without joining disjoint code or exceeding the budget.
            half = limit // 2
            offset = len(text) - (limit - half)
            observed = [clip_source_snippet({**source, "text": text[start:], "source_offset": start,
                "start_line": text.count("\n", 0, start) + 1, "truncated": True}, max_chars=size)
                for start, size in ((0, half), (offset, limit - half))]
        for row in observed:
            row["access_role"] = "editable" if editable_files is None or rel_path in editable_files else "read_only"
        snippets.extend(observed)
    return snippets


def read_source_context_text(workspace: Path, relative: str, *, indexed: bool = False) -> str | None:
    """Read within the existing source boundary before constructing any view.

    Indexed paths keep their historical size/format allowance. Neither an
    inventory entry nor a planned dependency authorizes private/environment
    trees or a path outside the workspace.
    """
    workspace = workspace.resolve()
    rel = Path(relative)
    path = workspace_file(workspace, relative)
    if (path is None or not path.is_file() or path.name.startswith(".env")
            or (not indexed and (path.suffix.lower() not in CONTEXT_SUFFIXES
                                 or path.stat().st_size > 500_000))
            or any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in rel.parts[:-1])
            or any(is_python_environment(workspace.joinpath(*rel.parts[:depth]))
                   for depth in range(1, len(rel.parts)))):
        return None
    return read_code_task_text(path)


def _construction_call_positions(text: str, symbols: list[str], query: str) -> list[int]:
    """Prefer a named Python call site when the question asks how it is built."""
    if not re.search(r"\b(?:build|construct|instantiat|creat|call.?site)", query.lower()):
        return []
    names = list(dict.fromkeys(
        symbol.rsplit(".", 1)[0] if symbol.endswith(".__init__") else symbol.rsplit(".", 1)[-1]
        for symbol in symbols if symbol
    ))
    if not names:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    calls: dict[str, list[int]] = {name: [] for name in names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name in calls and 1 <= node.lineno < len(offsets):
            calls[name].append(offsets[node.lineno - 1])
    return [position for name in names for position in sorted(calls[name])]


def _unseen_intervals(start: int, end: int, seen: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Subtract all observed spans, including overlapping observations."""
    intervals = []
    for lo, hi in sorted(seen):
        if hi <= start:
            continue
        if lo >= end:
            break
        if start < lo:
            intervals.append((start, lo))
        start = max(start, hi)
        if start >= end:
            break
    if start < end:
        intervals.append((start, end))
    return intervals


def _python_symbol_starts(
    text: str, symbols: list[str], seen: list[tuple[int, int]],
) -> list[int]:
    """Locate a named definition or continue its clipped body.

    A later call site is not a substitute for the unseen body of the requested
    method. Only definitions named by the caller qualify; unrelated truncated
    functions do not consume another source window.
    """
    if not symbols:
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    wanted = set(symbols)
    starts: list[int] = []

    def visit(nodes: list[ast.stmt], parents: tuple[str, ...] = ()) -> None:
        for node in nodes:
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            qualified = ".".join((*parents, node.name))
            if qualified in wanted or (not parents and node.name in wanted):
                begin = offsets[definition_start_line(node) - 1]
                end = offsets[min(node.end_lineno or node.lineno, len(offsets) - 1)]
                # A supplied middle fragment must not hide the remaining
                # body or let a tiny prefix consume the sole window.
                gaps = _unseen_intervals(begin, end, seen)
                starts.extend(lo for lo, hi in sorted(gaps, key=lambda gap: -(gap[1] - gap[0])))
            visit(node.body, (*parents, node.name))

    visit(tree.body)
    return list(dict.fromkeys(starts))


def source_file_inventory(
    workspace: Path, *, max_files: int = 400, required_paths: tuple[str, ...] = (),
) -> dict[str, Any]:
    """List candidate source files for design lookup without hashing dataset assets."""
    files: list[dict[str, Any]] = []
    scanned = 0
    for current, dirnames, filenames in os.walk(workspace):
        current_path = Path(current)
        dirnames[:] = sorted(
            (name for name in dirnames if name not in IGNORED_DIR_NAMES and not name.startswith(".")
             and not is_python_environment(current_path / name)),
            key=lambda name: (name.lower() in {"data", "datasets", "outputs", "runs", "results", "artifacts"}, name),
        )
        for name in sorted(filenames):
            scanned += 1
            if scanned > 10000:
                break
            path = Path(current) / name
            suffix = path.suffix.lower()
            if name.startswith(".env") or suffix not in CONTEXT_SUFFIXES:
                continue
            files.append({"path": path.relative_to(workspace).as_posix(),
                          "kind": "python" if suffix == ".py" else "text",
                          "role_tags": ["source"] if suffix in SOURCE_SUFFIXES else ["config"]})
        if scanned > 10000:
            break
    # The bounded inventory must not let configuration files crowd out source
    # code, regardless of the project's implementation language.
    files.sort(key=lambda row: ("source" not in row["role_tags"], row["path"]))
    selected = files[:max_files]
    # An explicitly declared active experiment config must not disappear in a
    # large Python project merely because the generic inventory is capped.
    root = workspace.resolve()
    known = {row["path"] for row in selected}
    for relative in required_paths[:8]:
        rel = Path(relative)
        path = (root / rel).resolve()
        normalized = rel.as_posix()
        if (rel.is_absolute() or ".." in rel.parts or path.name.startswith(".env")
                or not path.is_relative_to(root) or not path.is_file()
                or path.suffix.lower() not in {".toml", ".yaml", ".yml", ".json", ".ini", ".txt"}
                or path.stat().st_size > 500_000 or normalized in known):
            continue
        selected.append({"path": normalized, "kind": "text", "role_tags": ["config"]})
        known.add(normalized)
    return {"files": selected}


def requested_source_context(workspace: Path, index: dict[str, Any], request: dict[str, Any],
                             *, supplied: list[dict[str, str]], max_files: int, max_chars: int,
                             max_total_chars: int | None = None,
                             max_windows_per_file: int = 1) -> list[dict[str, Any]]:
    """Read unseen source windows without changing edit scope or cached source.

    ``max_windows_per_file`` defaults to the historical single window. Editors
    may spend a small, explicit extra window budget on a truncated target; the
    total character and result-count limits still apply.
    """
    if max_windows_per_file < 1:
        raise ValueError("max_windows_per_file must be positive.")
    literal = request.get("literal", "")
    if not isinstance(literal, str) or len(literal) > 200:
        raise ValueError("Source literal must be a string of at most 200 characters.")
    line_range = request.get("line_range")
    if line_range is not None:
        if (not isinstance(line_range, dict) or set(line_range) != {"start", "end"}
                or any(type(line_range[key]) is not int for key in ("start", "end"))
                or line_range["start"] < 1 or line_range["end"] < line_range["start"]
                or line_range["end"] - line_range["start"] >= 200
                or len(request.get("files", [])) != 1
                or literal or request.get("query") or request.get("symbols")):
            raise ValueError("A source line_range needs one file, 1-200 lines, and no search terms.")
    query = " ".join([request.get("query", ""), *request.get("symbols", []), literal]).strip()
    known = {str(item["path"]) for item in index.get("files", [])}
    requested_files = request.get("files", [])
    # The bounded inventory is a search aid, not a read authorization list.
    # A literal workspace path may be absent because the inventory was capped.
    candidates = list(requested_files)
    if not requested_files and query:
        candidates.extend(select_relevant_files(index, query, max_files=max_files))
    previous: dict[str, list[tuple[int, int]]] = {}
    for item in supplied:
        start = int(item.get("source_offset", 0))
        previous.setdefault(item["path"], []).append((start, start + len(item["text"])))
    symbols = [symbol.rsplit(".", 1)[-1] for symbol in request.get("symbols", [])]
    query_terms = [term for term in re.findall(r"[A-Za-z_][A-Za-z_0-9]*", request.get("query", ""))
                   if len(term) >= 5 and term.lower() not in {
                       "where", "which", "about", "their", "these", "those", "current",
                       "currently", "happen", "implementation", "source", "model", "method",
                   }]
    result = []
    remaining = max_total_chars if max_total_chars is not None else max_files * max_chars
    workspace = workspace.resolve()
    for relative in dict.fromkeys(candidates):
        if remaining <= 0:
            break
        text = read_source_context_text(workspace, relative, indexed=relative in known)
        if text is None:
            continue
        size = min(max_chars, remaining)
        seen = previous.get(relative, [])
        if line_range is not None:
            lines = text.splitlines(keepends=True)
            if line_range["start"] > len(lines):
                continue
            start = sum(len(line) for line in lines[:line_range["start"] - 1])
            end = sum(len(line) for line in lines[:min(line_range["end"], len(lines))])
            gaps = _unseen_intervals(start, end, seen)
            if gaps and size > 0:
                start, end = max(gaps, key=lambda gap: min(size, gap[1] - gap[0]))
                result.append(clip_source_snippet({"path": relative, "access_role": "read_only",
                    "text": text[start:end], "source_chars": len(text), "source_offset": start,
                    "start_line": text.count("\n", 0, start) + 1,
                    "truncated": start > 0 or end < len(text)}, max_chars=size))
            break
        def positions_for(terms: list[str]) -> list[int]:
            # Preserve the caller's term priority rather than selecting the
            # earliest generic mention anywhere in the file.
            return list(dict.fromkeys(match.start() for term in terms if term
                for match in re.finditer(re.escape(term), text, flags=re.IGNORECASE)))

        literal_positions = ([match.start() for match in re.finditer(re.escape(literal), text)]
                             if literal else [])
        call_positions = _construction_call_positions(text, request.get("symbols", []), request.get("query", "")) if Path(relative).suffix.lower() in {".py", ".pyi"} and not literal else []
        symbol_positions = positions_for(symbols) if not literal else []
        query_positions = positions_for(query_terms) if not literal else []
        positions = literal_positions + call_positions + query_positions + symbol_positions
        unseen = lambda items: [pos for pos in items if not any(lo <= pos < hi for lo, hi in seen)]
        if literal and not literal_positions:
            continue
        # An explicit identity outranks generic prose in a lookup question.
        # Queries supplement it; named Python definitions/calls remain primary.
        # Exact matches often name a use site; include its preceding setup and
        # call arguments as well as the continuation after it.
        starts = [max(0, min(pos - size // 2, len(text) - size)) for pos in unseen(literal_positions)]
        call_starts = [max(0, min(pos - size // 2, len(text) - size)) for pos in unseen(call_positions)]
        # An initial construction question still needs its call setup; once
        # source is observed, complete the requested unseen definition first.
        if not seen:
            starts += call_starts
        if not literal and Path(relative).suffix.lower() in {".py", ".pyi"}:
            starts += _python_symbol_starts(text, request.get("symbols", []), seen)
        if seen:
            starts += call_starts
        starts += [max(0, min(pos - size // 4, len(text) - size)) for pos in unseen(symbol_positions)]
        starts += [max(0, min(pos - size // 4, len(text) - size)) for pos in unseen(query_positions)]
        if not positions and not symbols and not query_terms:
            first = 0
            for lo, hi in sorted(seen):
                if first < lo:
                    break
                first = max(first, hi)
            starts = [min(first + offset * size, len(text))
                      for offset in range(max_windows_per_file)]
        elif positions and not starts and not literal:
            # A requested method can start in an already supplied window but
            # continue beyond its clipped end. Return one adjacent window.
            starts = [min(hi, max(0, len(text) - size)) for lo, hi in seen
                      if hi < len(text) and any(lo <= pos < hi and pos >= hi - size // 3 for pos in positions)]
        if max_windows_per_file > 1 and starts and len(text) > size and len(symbols) <= 1:
            # A symbol hit near the start of a long function is not the whole
            # behavior. When the caller explicitly budgets another window,
            # inspect the adjacent continuation before unrelated later hits.
            continuation = min(starts[0] + size, len(text) - size)
            if continuation > starts[0] and continuation not in starts:
                starts.insert(1, continuation)
        file_windows = 0
        for start in starts:
            if file_windows >= max_windows_per_file or len(result) >= max_files or remaining <= 0:
                break
            end = min(len(text), start + min(size, remaining))
            # Return one exact unseen interval. Adjacent observations can be
            # joined by the existing source-view owner; repeated bytes must
            # not spend the remaining read allowance again.
            gaps = _unseen_intervals(start, end, seen)
            if not gaps:
                continue
            start, end = max(gaps, key=lambda gap: gap[1] - gap[0])
            # The interval subtraction above already guarantees unseen bytes;
            # do not rescan every character or impose a second novelty quota.
            excerpt = text[start:end]
            result.append(clip_source_snippet({"path": relative, "access_role": "read_only",
                "text": excerpt, "source_chars": len(text), "source_offset": start,
                "start_line": text.count("\n", 0, start) + 1,
                "truncated": start > 0 or end < len(text)}, max_chars=len(excerpt)))
            seen.append((start, start + len(excerpt)))
            remaining -= len(excerpt)
            file_windows += 1
        if len(result) >= max_files:
            break
    return result
