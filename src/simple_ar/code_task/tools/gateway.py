from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from simple_ar.code_task.memory import code_task_memory_paths
from simple_ar.code_task.analysis.index import _iter_files, code_task_access_filter, read_code_task_text
from simple_ar.code_task.runtime.state import code_task_paths, code_task_workspace_boundary, workspace_file
from simple_ar.core.artifacts import read_json, read_jsonl
from simple_ar.tools.specs import ToolResult


class LocalCodeTaskToolGateway:
    """Read-only gateway for code-task memory and workspace lookup tools."""

    DEFAULT_SEARCH_EXTENSIONS = {".py", ".toml", ".json", ".md", ".txt", ".yaml", ".yml"}

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.paths = code_task_paths(self.run_dir)

    def _access_filter(self):
        return code_task_access_filter(self.paths.workspace_dir,
            boundary=code_task_workspace_boundary(self.run_dir))

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        try:
            data = self._dispatch(name, arguments or {})
        except FileNotFoundError as exc:
            return ToolResult(tool_name=name, status="not_found", error=str(exc))
        except ValueError as exc:
            return ToolResult(tool_name=name, status="blocked", error=str(exc))
        except Exception as exc:
            return ToolResult(tool_name=name, status="error", error=str(exc))
        return ToolResult(tool_name=name, status="ok", data=data)

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "read_code_task_memory":
            return self._read_memory()
        if name == "list_code_task_files":
            return {"files": self._list_files(arguments)}
        if name == "search_code_task_code":
            return {"matches": self._search_code(arguments)}
        if name == "read_code_task_file_range":
            return self._read_file_range(arguments)
        if name == "find_code_task_symbol":
            return {"symbols": self._find_symbol(arguments)}
        if name == "find_code_task_related_files":
            return {"files": self._find_related_files(arguments)}
        if name == "list_code_task_recent_edits":
            return self._list_recent_edits(arguments)
        raise RuntimeError(f"Unhandled code-task tool: {name}")

    def _read_memory(self) -> dict[str, Any]:
        paths = code_task_memory_paths(self.run_dir)
        memory_json = _read_optional_json(paths.task_memory_json)
        memory_markdown = _tail_text(paths.task_memory_md, limit=12000)
        compressed_json = _read_optional_json(paths.compressed_memory_json)
        compressed_markdown = _tail_text(paths.compressed_memory_md, limit=12000)
        return {
            "memory": memory_json,
            "markdown": memory_markdown,
            "compressed_memory": compressed_json,
            "compressed_markdown": compressed_markdown,
            "paths": {
                "memory_json": _rel(self.run_dir, paths.task_memory_json),
                "memory_markdown": _rel(self.run_dir, paths.task_memory_md),
                "compressed_memory_json": _rel(self.run_dir, paths.compressed_memory_json),
                "compressed_memory_markdown": _rel(self.run_dir, paths.compressed_memory_md),
                "edit_history": _rel(self.run_dir, paths.edit_history_jsonl),
                "review_findings": _rel(self.run_dir, paths.review_findings_jsonl),
                "repair_memory": _rel(self.run_dir, paths.repair_memory_jsonl),
            },
        }

    def _list_files(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        index = self._index()
        allowed = self._access_filter()
        role = str(arguments.get("role", "")).strip().lower()
        extensions = _extension_filter(arguments.get("extensions"))
        max_files = _bounded_int(arguments.get("max_files"), default=120, minimum=1, maximum=300)
        rows: list[dict[str, Any]] = []
        for item in _object_list(index.get("files")):
            path = str(item.get("path", ""))
            if not path or not allowed(self.paths.workspace_dir / path):
                continue
            if extensions and Path(path).suffix.lower() not in extensions:
                continue
            role_tags = [str(tag).lower() for tag in _string_list(item.get("role_tags"))]
            access_role = str(item.get("access_role", "")).lower()
            kind = str(item.get("kind", "")).lower()
            if role and role not in role_tags and role != access_role and role != kind:
                continue
            rows.append(
                {
                    "path": path,
                    "kind": item.get("kind", ""),
                    "bytes": item.get("bytes", 0),
                    "role_tags": item.get("role_tags", []),
                    "access_role": item.get("access_role", ""),
                    "summary": item.get("summary", ""),
                }
            )
            if len(rows) >= max_files:
                break
        return rows

    def _search_code(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("search_code_task_code requires a non-empty query.")
        max_matches = _bounded_int(arguments.get("max_matches"), default=30, minimum=1, maximum=100)
        case_sensitive = bool(arguments.get("case_sensitive", False))
        extensions = _extension_filter(arguments.get("extensions")) or self.DEFAULT_SEARCH_EXTENSIONS
        needle = query if case_sensitive else query.lower()
        matches: list[dict[str, Any]] = []
        for path in sorted(_iter_files(self.paths.workspace_dir, include_hidden_dirs=True, include_dotenv_examples=True,
                access_boundary=code_task_workspace_boundary(self.run_dir))):
            if path.suffix.lower() not in extensions or path.suffix.lower() in {'.pyc', '.pyo'}:
                continue
            text = read_code_task_text(path, utf8_errors='replace')
            if text is None:
                continue
            lines = text.splitlines()
            rel = path.relative_to(self.paths.workspace_dir).as_posix()
            for number, line in enumerate(lines, start=1):
                haystack = line if case_sensitive else line.lower()
                if needle in haystack:
                    matches.append({"path": rel, "line": number, "preview": line.strip()[:300]})
                    if len(matches) >= max_matches:
                        return matches
        return matches

    def _read_file_range(self, arguments: dict[str, Any]) -> dict[str, Any]:
        rel = _safe_relative_path(str(arguments.get("path", "")))
        if not rel:
            raise ValueError("Unsafe or missing workspace path.")
        path = workspace_file(self.paths.workspace_dir, rel)
        if not self._access_filter()(self.paths.workspace_dir / rel):
            raise ValueError('Private or unauthorized project paths are not exposed to task tools.')
        if path is None or not path.is_file():
            raise FileNotFoundError(f"Code-task workspace file not found: {rel}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start_line = _bounded_int(arguments.get("start_line"), default=1, minimum=1, maximum=max(1, len(lines)))
        max_lines = _bounded_int(arguments.get("max_lines"), default=80, minimum=1, maximum=200)
        start_index = start_line - 1
        selected = lines[start_index : start_index + max_lines]
        return {
            "path": rel,
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1 if selected else start_line,
            "total_lines": len(lines),
            "text": "\n".join(selected),
            "truncated": start_index + max_lines < len(lines),
        }

    def _find_symbol(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("find_code_task_symbol requires a non-empty query.")
        kind_filter = str(arguments.get("kind", "")).strip().lower()
        case_sensitive = bool(arguments.get("case_sensitive", False))
        max_matches = _bounded_int(arguments.get("max_matches"), default=25, minimum=1, maximum=80)
        needle = query if case_sensitive else query.lower()
        rows: list[dict[str, Any]] = []
        allowed = self._access_filter()
        for symbol in _object_list(self._repo_map().get("symbols")):
            path = str(symbol.get('path', ''))
            if not path or not allowed(self.paths.workspace_dir / path):
                continue
            kind = str(symbol.get("kind", "")).lower()
            if kind_filter and kind != kind_filter:
                continue
            text = " ".join(
                str(symbol.get(key, ""))
                for key in ("name", "qualified_name", "id", "path")
            )
            haystack = text if case_sensitive else text.lower()
            if needle not in haystack:
                continue
            rows.append(
                {
                    "id": symbol.get("id", ""),
                    "path": symbol.get("path", ""),
                    "kind": symbol.get("kind", ""),
                    "name": symbol.get("name", ""),
                    "qualified_name": symbol.get("qualified_name", ""),
                    "line_start": symbol.get("line_start"),
                    "line_end": symbol.get("line_end"),
                    "access_role": symbol.get("access_role", ""),
                }
            )
            if len(rows) >= max_matches:
                break
        return rows

    def _find_related_files(self, arguments: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(arguments.get("query", "")).strip()
        if not query:
            raise ValueError("find_code_task_related_files requires a non-empty query.")
        max_files = _bounded_int(arguments.get("max_files"), default=20, minimum=1, maximum=80)
        scores: dict[str, dict[str, Any]] = {}
        tokens = _tokens(query)
        allowed = self._access_filter()

        def add(path: str, *, score: float, source: str, summary: str = "") -> None:
            if not path or not allowed(self.paths.workspace_dir / path):
                return
            row = scores.setdefault(path, {"path": path, "score": 0.0, "sources": [], "summary": summary})
            row["score"] = float(row["score"]) + score
            if source not in row["sources"]:
                row["sources"].append(source)
            if summary and not row.get("summary"):
                row["summary"] = summary

        locate = _read_optional_json(self.paths.meta_dir / "locate_results.json")
        for key, source_name, weight in (
            ("editable_targets", "locate.editable_targets", 6.0),
            ("read_only_evidence", "locate.read_only_evidence", 3.0),
        ):
            for row in _object_list(locate.get(key)):
                add(str(row.get("path", "")), score=weight, source=source_name, summary=str(row.get("reason", "")))

        index = self._index()
        for item in _object_list(index.get("files")):
            path = str(item.get("path", ""))
            text = " ".join(
                [
                    path,
                    str(item.get("kind", "")),
                    str(item.get("summary", "")),
                    " ".join(_string_list(item.get("role_tags"))),
                    " ".join(_string_list(_object_dict(item.get("python")).get("imports"))),
                ]
            ).lower()
            score = sum(1.0 for token in tokens if token in text)
            if score:
                add(path, score=score, source="codebase_index", summary=str(item.get("summary", "")))

        repo_map = self._repo_map()
        for symbol in _object_list(repo_map.get("symbols")):
            text = " ".join(str(symbol.get(key, "")) for key in ("name", "qualified_name", "kind", "path")).lower()
            score = sum(1.5 for token in tokens if token in text)
            if score:
                add(str(symbol.get("path", "")), score=score, source="repo_map.symbols")

        rows = sorted(scores.values(), key=lambda row: (-float(row["score"]), str(row["path"])))
        return rows[:max_files]

    def _list_recent_edits(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = _bounded_int(arguments.get("limit"), default=10, minimum=1, maximum=50)
        include_reviews = bool(arguments.get("include_reviews", True))
        include_repairs = bool(arguments.get("include_repairs", True))
        paths = code_task_memory_paths(self.run_dir)
        edits = _tail_rows(paths.edit_history_jsonl, limit)
        reviews = _tail_rows(paths.review_findings_jsonl, limit) if include_reviews else []
        repairs = _tail_rows(paths.repair_memory_jsonl, limit) if include_repairs else []
        return {"edit_history": edits, "review_findings": reviews, "repair_memory": repairs}

    def _index(self) -> dict[str, Any]:
        return _read_required_json(self.paths.meta_dir / "codebase_index.json")

    def _repo_map(self) -> dict[str, Any]:
        path = self.paths.meta_dir / "repo_map.json"
        if path.is_file():
            return _read_required_json(path)
        return {"symbols": []}


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def _read_required_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Missing artifact: {path}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def _tail_rows(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = [row for row in read_jsonl(path) if isinstance(row, dict)]
    return rows[-limit:]


def _tail_text(path: Path, *, limit: int) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[-limit:]


def _extension_filter(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        extension if extension.startswith(".") else f".{extension}"
        for extension in (str(item).strip().lower() for item in value)
        if extension
    }


def _safe_relative_path(value: str) -> str:
    value = value.replace("\\", "/").strip().lstrip("/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _tokens(text: str) -> set[str]:
    raw = "".join(char.lower() if char.isalnum() or char in {"_", "-"} else " " for char in text)
    return {token for token in raw.replace("-", "_").split() if len(token) >= 2}


def _object_list(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        pass
    try:
        return "../" + path.relative_to(root.parent).as_posix()
    except ValueError:
        return str(path)
