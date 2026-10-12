from __future__ import annotations

import ast
import hashlib
import os
import io
import tokenize
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from simple_ar.core.artifacts import write_json
from simple_ar.retrieval.index import _looks_like_text, json_summary, kind_for_path
from simple_ar.code_task.runtime.state import utcnow_iso as _utcnow_iso


IGNORED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".simple_ar_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}

IGNORED_FILE_NAMES = {
    ".env",
    ".git",
}

# Shared with question-directed source lookup; retain its existing language
# coverage rather than inventing another binary-extension denylist.
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".r", ".jl", ".sh",
}


def read_code_task_text(path: Path) -> str | None:
    """Read source/context text, not arbitrary inventory artifacts.

    Reuse retrieval's UTF-8 sample/NUL detection for unknown formats. Known
    source encodings keep the previous replacement fallback; Python's coding
    declaration is handled by the standard tokenizer before that fallback.
    """
    try:
        if path.suffix.lower() not in SOURCE_SUFFIXES and not _looks_like_text(path):
            return None
        data = path.read_bytes()
        if b"\0" in data:
            return None
        if path.suffix.lower() in {".py", ".pyi"}:
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
                text = data.decode(encoding)
            except (SyntaxError, UnicodeError, LookupError):
                text = data.decode("utf-8", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "\n")
    except OSError:
        return None


def is_python_environment(directory: Path) -> bool:
    """A venv's standard marker identifies installed dependencies, regardless of name."""
    return (directory / "pyvenv.cfg").is_file()


def build_codebase_index(
    workspace_dir: Path,
    *,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """Build a compact, source-oriented index for a code-task workspace.

    Args:
        workspace_dir: Copied workspace that may be edited by later code-task
            stages. The original source directory is not read after init.
        output_path: Optional JSON destination for the generated index.

    Returns:
        JSON-serializable index with file inventory, hashes, role tags, and
        Python AST summaries.

    Raises:
        FileNotFoundError: If ``workspace_dir`` does not exist.
        NotADirectoryError: If ``workspace_dir`` is not a directory.
    """
    workspace = Path(workspace_dir)
    if not workspace.exists():
        raise FileNotFoundError(f"Workspace does not exist: {workspace}")
    if not workspace.is_dir():
        raise NotADirectoryError(f"Workspace path is not a directory: {workspace}")

    files = [_index_file(workspace, path) for path in _iter_files(workspace)]
    files.sort(key=lambda item: item["path"])

    python_files = [item for item in files if item["kind"] == "python"]
    test_files = [item for item in files if "test" in item["role_tags"]]
    import_counter: Counter[str] = Counter()
    entrypoints: list[str] = []
    for item in python_files:
        python = item.get("python")
        if not isinstance(python, dict):
            continue
        for name in python.get("imports", []):
            import_counter[str(name)] += 1
        if python.get("has_main_guard") is True:
            entrypoints.append(str(item["path"]))

    top_level_entries = sorted({Path(str(item["path"])).parts[0] for item in files})
    index = {
        "schema_version": 1,
        "generated_at": _utcnow_iso(),
        "workspace": str(workspace),
        "project": {
            "file_count": len(files),
            "python_file_count": len(python_files),
            "test_file_count": len(test_files),
            "total_bytes": sum(int(item["bytes"]) for item in files),
            "top_level_entries": top_level_entries,
            "entrypoint_candidates": entrypoints,
            "common_imports": [
                {"name": name, "count": count}
                for name, count in import_counter.most_common(20)
            ],
        },
        "files": files,
    }
    if output_path is not None:
        write_json(output_path, index)
    return index


def project_python_files(root: Path) -> list[Path]:
    """Share the index's source boundary with static checks and repair lookup."""
    return sorted(path for path in _iter_files(root) if path.suffix == ".py")


def _iter_files(root: Path) -> Iterator[Path]:
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in IGNORED_DIR_NAMES and not dirname.startswith(".")
            and not is_python_environment(current_path / dirname)
        ]
        for filename in filenames:
            if filename in IGNORED_FILE_NAMES or filename.startswith(".env"):
                continue
            path = current_path / filename
            if path.is_file():
                yield path


def _index_file(root: Path, path: Path) -> dict[str, Any]:
    rel_path = path.relative_to(root).as_posix()
    stat = path.stat()
    kind = kind_for_path(path)
    python_summary = _python_summary(path) if kind == "python" else None
    item: dict[str, Any] = {
        "path": rel_path,
        "kind": kind,
        "bytes": stat.st_size,
        "sha256": _sha256(path),
        "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
            timespec="seconds"
        ),
        "role_tags": _role_tags(rel_path, kind),
        "summary": _summary_for_file(path, kind, python_summary=python_summary),
    }
    if python_summary is not None:
        item["python"] = python_summary
    return item


def _python_summary(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
        compile(tree, str(path), "exec", dont_inherit=True)
    except SyntaxError as exc:
        return {
            "syntax_ok": False,
            "syntax_error": {
                "message": exc.msg,
                "line": exc.lineno,
                "offset": exc.offset,
            },
            "imports": [],
            "functions": [],
            "classes": [],
            "has_main_guard": False,
        }

    imports = sorted(_imports(tree))
    functions = [_function_row(node) for node in tree.body if _is_function(node)]
    classes = [_class_row(node) for node in tree.body if isinstance(node, ast.ClassDef)]
    return {
        "syntax_ok": True,
        "imports": imports,
        "import_references": [
            {"module": alias.name, "level": 0, "names": []}
            for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        ] + [
            {"module": node.module or "", "level": node.level,
             "names": [alias.name for alias in node.names if alias.name != "*"]}
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ],
        "functions": functions,
        "classes": classes,
        "has_main_guard": _has_main_guard(tree),
    }


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def _function_row(node: ast.AST) -> dict[str, Any]:
    name = getattr(node, "name", "")
    args = getattr(node, "args", None)
    arg_names = [arg.arg for arg in getattr(args, "args", [])] if args is not None else []
    return {
        "name": name,
        "line_start": getattr(node, "lineno", None),
        "line_end": getattr(node, "end_lineno", getattr(node, "lineno", None)),
        "args": arg_names,
        "is_async": isinstance(node, ast.AsyncFunctionDef),
        "is_test": name.startswith("test_"),
    }


def _class_row(node: ast.ClassDef) -> dict[str, Any]:
    methods = [_function_row(child) for child in node.body if _is_function(child)]
    return {
        "name": node.name,
        "line_start": node.lineno,
        "line_end": getattr(node, "end_lineno", node.lineno),
        "methods": methods,
    }


def _is_function(node: ast.AST) -> bool:
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


def _has_main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            return True
    return False


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    left = node.left
    if not (isinstance(left, ast.Name) and left.id == "__name__"):
        return False
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Eq):
        return False
    if len(node.comparators) != 1:
        return False
    comparator = node.comparators[0]
    return isinstance(comparator, ast.Constant) and comparator.value == "__main__"


def _role_tags(relative_path: str, kind: str) -> list[str]:
    path = Path(relative_path)
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    tags = {kind}
    if kind == "python":
        tags.add("source")
    if "tests" in parts or name.startswith("test_") or name.endswith("_test.py"):
        tags.add("test")
    if name in {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "uv.lock"}:
        tags.add("config")
    if kind == "markdown" or name in {"readme", "readme.md"}:
        tags.add("docs")
    if name in {"main.py", "cli.py", "__main__.py"}:
        tags.add("entrypoint")
    return sorted(tags)


def _summary_for_file(
    path: Path,
    kind: str,
    *,
    python_summary: dict[str, Any] | None = None,
) -> str:
    if kind == "other":
        return ""
    text = read_code_task_text(path)
    if text is None:
        return ""
    if kind == "json":
        summary = json_summary(text[:8192])
        if summary:
            return summary
    if kind == "python":
        python = python_summary or _python_summary(path)
        if python.get("syntax_ok") is not True:
            return "python syntax error"
        pieces: list[str] = []
        functions = python.get("functions", [])
        classes = python.get("classes", [])
        imports = python.get("imports", [])
        if classes:
            pieces.append(
                "classes: "
                + ", ".join(str(item.get("name", "")) for item in classes[:5] if isinstance(item, dict))
            )
        if functions:
            pieces.append(
                "functions: "
                + ", ".join(str(item.get("name", "")) for item in functions[:5] if isinstance(item, dict))
            )
        if imports:
            pieces.append("imports: " + ", ".join(str(name) for name in imports[:5]))
        if pieces:
            return _truncate("; ".join(pieces), 180)
    for line in text.splitlines():
        stripped = " ".join(line.strip().split())
        if stripped:
            return _truncate(stripped, 180)
    return ""


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _truncate(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."
