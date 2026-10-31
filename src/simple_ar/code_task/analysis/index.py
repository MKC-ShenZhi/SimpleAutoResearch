from __future__ import annotations

import ast
import hashlib
import os
import stat
import io
import tokenize
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from simple_ar.core.artifacts import is_private_path_name, write_json
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


def is_private_dotenv(name: str) -> bool:
    """Keep example templates searchable while excluding possible credentials."""
    return name.casefold().startswith('.env') and is_private_path_name(name, allow_env_example=True)


def code_task_access_filter(root: Path, *, boundary: Path | None = None) -> Callable[[Path], bool]:
    """Check both the requested and resolved project path before exposing it to a model."""
    lexical_root = Path(root).absolute()
    if boundary is None:
        raise ValueError('An authorized workspace boundary is required before reading project sources')
    boundary = Path(boundary).absolute()
    try:
        resolved_root = lexical_root.resolve()
        resolved_boundary = boundary.resolve()
        # A restored boundary is a saved path, not proof that its ancestors
        # are public. Check both spellings before narrowing to the project.
        root_parts = (boundary.parts, resolved_boundary.parts,
            lexical_root.relative_to(boundary).parts,
            resolved_root.relative_to(resolved_boundary).parts)
        root_allowed = ('..' not in lexical_root.parts and '..' not in boundary.parts
            and not any(is_private_path_name(part) for parts in root_parts for part in parts))
    except (OSError, ValueError, RuntimeError):
        root_allowed = False

    def allowed(path: Path) -> bool:
        if not root_allowed:
            return False
        path = Path(path).absolute()
        try:
            if '..' in path.parts:
                return False
            relative = path.relative_to(lexical_root)
            resolved = path.resolve().relative_to(resolved_root)
            is_file = not path.is_dir()
            # Check every component from the original grant: resolve() must
            # not erase an in-workspace link or dependency environment.
            parts = path.relative_to(boundary).parts
            for depth in range(len(parts) + 1):
                component = boundary.joinpath(*parts[:depth])
                try:
                    attrs = getattr(component.lstat(), 'st_file_attributes', 0)
                except FileNotFoundError:
                    attrs = 0  # Generated project roots can precede creation.
                if (component.is_symlink() or component.is_junction()
                        or attrs & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
                        or component.is_dir() and is_python_environment(component)):
                    return False
        except (OSError, ValueError, RuntimeError):
            return False
        for parts in (relative.parts, resolved.parts):
            if any(is_private_path_name(part, allow_env_example=is_file and index == len(parts)-1)
                    for index, part in enumerate(parts)):
                return False
        return True

    return allowed

# Shared with question-directed source lookup; retain its existing language
# coverage rather than inventing another binary-extension denylist.
SOURCE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".rb", ".r", ".jl", ".sh",
}


def read_code_task_text(path: Path, *, utf8_errors: Literal['replace', 'ignore'] | None = None) -> str | None:
    """Read source/context text, not arbitrary inventory artifacts.

    Reuse retrieval's UTF-8 sample/NUL detection for unknown formats. Known
    source encodings keep the previous replacement fallback; Python's coding
    declaration is handled by the standard tokenizer before that fallback.
    Explicit UTF-8 error handling preserves consumers that historically read
    arbitrary text with replacement or ignored undecodable bytes, without
    imposing the inventory's format sniff or Python encoding interpretation.
    """
    try:
        if utf8_errors is not None:
            return path.read_text(encoding='utf-8', errors=utf8_errors)
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
    access_boundary: Path | None = None,
) -> dict[str, Any]:
    """Build a compact, source-oriented index for a code-task workspace.

    Args:
        workspace_dir: Copied workspace that may be edited by later code-task
            stages. The original source directory is not read after init.
        output_path: Optional JSON destination for the generated index.
        access_boundary: Original workspace grant when indexing a nested
            project. Without it, workspace_dir must already be an admitted
            workspace root; this low-level inventory does not admit sources.

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

    allowed = code_task_access_filter(workspace, boundary=access_boundary or workspace)
    if not allowed(workspace):
        raise ValueError('Index project is private or outside its authorized workspace boundary')
    files = [_index_file(workspace, path) for path in _iter_files(workspace,
             access_boundary=access_boundary or workspace)]
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


def _iter_files(root: Path, *, excluded_names: frozenset[str] = frozenset(),
                include_hidden_dirs: bool = False,
                include_dotenv_examples: bool = False,
                excluded_dir_names: frozenset[str] = frozenset(),
                excluded_dir_suffixes: tuple[str, ...] = (),
                access_boundary: Path | None = None,
                path_filter: Callable[[Path], bool] | None = None) -> Iterator[Path]:
    """Own safe project enumeration with consumer-specific visibility policy.

    Filters apply before descending so review/search cannot visit dependency
    trees just to discard their contents later. Defaults retain the inventory
    used by copied workspaces and their historical source bindings.
    Directory-only exclusions never remove ordinary files of the same name.
    Hidden project directories may be visible; private .env paths, dependency
    environments, links and caller access filters remain excluded.
    """
    allowed = code_task_access_filter(root, boundary=access_boundary or root)
    if not allowed(root):
        return
    for current, dirnames, filenames in os.walk(root):
        current_path = Path(current)
        dirnames[:] = [
            dirname
            for dirname in dirnames
            if dirname not in IGNORED_DIR_NAMES
            and not dirname.casefold().startswith('.env')
            and (include_hidden_dirs or not dirname.startswith('.'))
            and dirname not in excluded_dir_names and not dirname.endswith(excluded_dir_suffixes)
            and dirname not in excluded_names
            and allowed(current_path / dirname)
            and (path_filter is None or path_filter(current_path / dirname))
        ]
        for filename in filenames:
            if (filename in IGNORED_FILE_NAMES or filename in excluded_names
                    or filename.casefold().startswith('.env')
                    and (not include_dotenv_examples or is_private_dotenv(filename))):
                continue
            path = current_path / filename
            if (allowed(path) and path.is_file()
                    and (path_filter is None or path_filter(path))):
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
