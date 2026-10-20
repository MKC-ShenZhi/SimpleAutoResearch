"""Explicitly mounted workspace tools; authorization, not a filesystem sandbox."""
from __future__ import annotations

import hashlib
import io
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import sys
import tempfile
import zipfile
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field

from simple_ar.core.artifacts import read_json, replace_exact_spans
from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.core.process import ProcessSpec, process_observation, run_process, settle_process_record
from simple_ar.tools.specs import CommonToolSpec, ToolResult


_COPY_MAX_BYTES = 64 * 1024 * 1024
_FULL_DIGEST_MAX_BYTES = 64 * 1024 * 1024


def validate_delivery_patterns(patterns: list[str]) -> list[str]:
    """Caller-declared output paths, independent of task kind or chosen tools."""
    if not isinstance(patterns, list) or len(patterns) > 32:
        raise ValueError('Delivery files must be a list of at most 32 workspace-relative patterns')
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern.strip() or len(pattern) > 300
                or '\\' in pattern or ':' in pattern or '\x00' in pattern
                or PurePosixPath(pattern).is_absolute()
                or any(part in {'.', '..', ''} for part in pattern.split('/'))):
            raise ValueError('Delivery patterns must stay relative to the workspace without traversal')
    return list(dict.fromkeys(patterns))


def missing_delivery_files(workspace: Any, artifacts: list[str], patterns: list[str]) -> list[str]:
    """Check actual delivered nonempty files, not every file on disk or source PDFs.

    This is completeness only. Domain verification and independent content/visual
    review remain necessary; a matching filename is not a correctness certificate.
    """
    names = []
    for locator in artifacts:
        path = workspace.resolve(locator)
        if path.is_relative_to(workspace.workspace) and path.is_file() and path.stat().st_size:
            names.append(PurePosixPath(path.relative_to(workspace.workspace).as_posix()))
    return [pattern for pattern in validate_delivery_patterns(patterns)
        if not any(name.match(pattern) or pattern.startswith('**/') and name.match(pattern[3:])
            for name in names)]

class _Path(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(description="Relative paths default to the task workspace, e.g. reports/result.md. Explicit registered mount paths such as workspace/project or source0/README.md and observed absolute paths within those roots also work. Source mounts remain read-only; mount names are tool locators, not filesystem directories for program code.")


class _List(_Path):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=60, ge=1, le=200)
    recursive: bool = False
    file_glob: str = Field(default="*", max_length=100,
        description="Filename filter; recursive results include files only. Traversal is capped at 4096 entries.")


class FileWindow(_Path):
    offset_bytes: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=16000, ge=1, le=64000)
    full_digest: bool = Field(default=False, exclude_if=lambda value: value is False,
        description="Request a streamed full-file SHA256 for a file at most 64 MiB, regardless of the returned text-window size. Larger files are rejected. False preserves the normal digest policy: SHA256 only for files at most 256000 bytes.")
    start_line: int | None = Field(default=None, ge=1,
        description="One-based line from search results; cannot combine with a nonzero byte offset. Files up to 256000 bytes.")
    max_lines: int = Field(default=200, ge=1, le=1000,
        description="Line count for start_line reads (default 200); when explicitly supplied for a byte-origin read, also cap that window by lines. max_bytes still bounds bytes and may end within a line. Continue at next_offset_bytes.")

    def validate_read_size(self, size: int) -> None:
        """Shared file-dependent admission; content and digest still use the open file."""
        if self.full_digest and size > _FULL_DIGEST_MAX_BYTES:
            raise ValueError('Full-file digest exceeds 64 MiB')
        if self.start_line is not None and (self.offset_bytes or size > 256000):
            raise ValueError('Line reads require offset_bytes=0 and a file at most 256000 bytes; use byte windows otherwise')


class _Search(_Path):
    query: str = Field(min_length=1, max_length=512)
    file_glob: str = Field(default="*", max_length=100, description="Filename glob, e.g. *.py; not a shell command.")
    offset_files: int = Field(default=0, ge=0, le=4096)
    max_files: int = Field(default=64, ge=1, le=128)
    case_sensitive: bool = False


class _Project(_Path):
    data_paths: list[str] = Field(default_factory=list, max_length=15,
        description="Explicit authorized data files or directories to inspect for presence/size, not contents.")


class ReportDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['candidate', 'canonical']
    manuscript: str | None = None


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1)
    artifacts: list[str] = Field(default_factory=list,
        description="Existing workspace outputs (workspace-relative or explicitly mounted paths) or retained files in this task's evidence mount. Evidence remains read-only and is referenced, not copied or certified. Directories expand to a snapshot of at most 200 accessible files; private paths, virtual environments, Python bytecode caches and symlinks are excluded by the existing inventory rules. Oversized inventories must be narrowed explicitly; this is not an archive or correctness certificate.")
    unresolved: list[str] = Field(default_factory=list)
    report: ReportDelivery | None = Field(default=None, exclude_if=lambda value: value is None,
        description='For a versioned report obligation, explicitly deliver a candidate with unresolved work, or a canonical compose_document manuscript and its same-version publication. Candidate status never certifies a scientific report.')


class _Write(_Path):
    content: str = Field(max_length=256000)
    expected_sha256: str = Field(default="", description="Current digest required before replacing an existing file; empty for a new file.")
    delivery: DeliveryRequest | None = Field(default=None,
        description="Optional explicit final delivery when this write finishes the task. Include this file and actual existing artifacts; disclose unresolved work. Does not certify scientific correctness. Saves the normal delivery manifest without another model turn.")


class _Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: str = Field(min_length=1, max_length=256000, description="Exact unique text to replace; include context when ambiguous.")
    new_text: str = Field(max_length=256000)


class _Copy(_Path):
    source: str = Field(description='Authorized regular file, text or binary, at most 64 MiB; copied byte-for-byte without putting its contents in a model response.')
    source_sha256: str = Field(min_length=64, max_length=64, description='Observed full source digest; stale candidates are rejected.')
    expected_sha256: str = Field(default='', description='Observed destination digest required before replacing an existing file; empty for a new file.')


class _CopyProject(_Path):
    source: str = Field(description='Authorized project directory; originals remain unchanged.')
    archive_name: str | None = Field(default=None, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*\.zip$', max_length=100,
        exclude_if=lambda value: value is None,
        description='Optionally create a portable ZIP inside the new copy, containing exactly the copied files with relative paths. Existing copy exclusions and reported omissions apply; inspect them before claiming a complete package. No generated packaging script or process is needed.')
    max_files: int = Field(default=1000, ge=1, le=4096)
    max_total_bytes: int = Field(default=128 * 1024 * 1024, ge=1, le=512 * 1024 * 1024)
    max_file_bytes: int = Field(default=2_000_000, ge=1, le=64 * 1024 * 1024)


class _Edit(_Path):
    old_text: str | None = Field(default=None, min_length=1, max_length=256000)
    new_text: str | None = Field(default=None, max_length=256000)
    replacements: list[_Replacement] = Field(default_factory=list, max_length=40,
        description="One or more related non-overlapping edits against the same observed original. All edits succeed atomically or none do.")
    expected_sha256: str = Field(min_length=64, max_length=64, description="Current digest from read_file; stale edits are rejected.")


class _Run(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=40)
    cwd: str = "workspace"
    timeout_sec: int = Field(default=60, ge=1, le=300)
    delivery: DeliveryRequest | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Optional explicit final delivery after this authorized process succeeds. Select actual output paths, including files produced by the command, and disclose unresolved work. Nonzero exit, stopping or missing artifacts never delivers. Does not certify scientific correctness. Use only as the last action, not instead of inspecting unknown results.")


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, description="Only missing scientific semantics, scope or authorization; not model formatting errors.")


_TOOLS = {
    "list_directory": (_List, "List mounted entries, or discover files recursively by filename with explicit pagination and traversal limits; no file contents.", "read_only"),
    "read_file": (FileWindow, "Read a bounded file window by byte offset or one-based line; return digest and exact byte continuation.", "read_only"),
    "search_files": (_Search, "Search literal text with ripgrep in a bounded mounted file batch; return line locations and explicit coverage limits.", "read_only"),
    "inspect_project": (_Project, "Read project instructions, dependency declarations and Python entry candidates using the existing preparation inspector. Returns a compact overview and a retained full observation. No imports of project code, installation, downloads or execution; metadata describes the inspecting interpreter, not readiness of the task environment. Use its evidence to choose the next necessary preparation or validation action, not to repeat an inventory.", "read_only"),
    "write_file": (_Write, "Create or replace a workspace text file; existing files require the observed digest. If this is the final file, an explicit delivery can complete the task in the same operation.", "write_patch"),
    "copy_file": (_Copy, "Copy an observed file or generated candidate into the workspace without retransmitting its contents. Source digest must match; read_file with full_digest=true and a small max_bytes obtains a streamed full-file digest up to 64 MiB. Existing destinations require their digest and retain a byte backup through the shared atomic writer. Original sources stay unchanged. Does not execute or certify a repair.", "write_patch"),
    "copy_project": (_CopyProject, "Prepare a writable project copy and optional portable ZIP in one operation, including ordinary source, tests, configuration, documentation and binary assets. Destination must be a new workspace subdirectory. Uses existing conservative code-workspace copying: private/runtime paths, symlinks, caches/build outputs and oversized files are omitted and reported. Total bounds are checked before publishing. Keeps originals unchanged, records copied file and archive hashes, and reconciles the same interrupted operation only when its published files still match. Inspect omissions before claiming a complete deliverable. Not a complete Git clone, dependency install, execution or correctness certificate.", "write_patch"),
    "edit_file": (_Edit, "Replace one or multiple exact unique, non-overlapping spans in a workspace file atomically, with one observed digest and backup. Batch related changes rather than repeating calls.", "write_patch"),
    "run_process": (_Run, "Execute an argv without a shell in the workspace. Bare python/python3 use the task interpreter; its directory leads the filtered PATH for child commands. Use sys.executable for portable Python children. Requires explicit process authorization, not a sandbox. Optional explicit delivery completes the task after successful final work and binds actual output bytes without another model decision; execution alone is not delivery or verification.", "execution"),
    "deliver_result": (DeliveryRequest, "Deliver actual workspace artifacts and optional retained task-evidence references; name unresolved work honestly.", "plan"),
    "ask_user": (_Question, "Ask a concentrated question only when needed to proceed; pause with all work preserved.", "plan"),
}
_PRIVATE = {".git", ".aws", ".ssh", ".codex", ".agents", "__pycache__", ".venv", "venv"}


def _private_name(name: str) -> bool:
    name = name.lower()
    return name in _PRIVATE or name.startswith('.env')


def resolve_source_mount(value: Path) -> Path:
    """Admit a new read-only source before configuration or model side effects."""
    path = value.expanduser().absolute()
    resolved = path.resolve(strict=True)
    for candidate in (path, resolved):
        if path.is_symlink() or any(_private_name(part) for part in candidate.parts):
            raise ValueError(f'Private/runtime or symlink material is not a task input: {path}')
    if not (resolved.is_file() or resolved.is_dir()):
        raise ValueError(f'Material must be a regular file or directory: {resolved}')
    return resolved


def resolve_mounted_path(mounts: dict[str, Path], value: str, *, write: bool = False,
                         discovery: bool = False) -> Path:
    """Shared access rule for tools and read-only delivery navigation."""
    native = Path(value)
    rel = PurePosixPath(value)
    if not value or ".." in native.parts or ".." in rel.parts:
        raise ValueError("Use an authorized path without traversal")
    if native.is_absolute():
        owners = [(name, base) for name, base in mounts.items()
                  if native.is_relative_to(base) and (not write or name == "workspace")]
        if not owners:
            raise ValueError("Path is outside the authorized mounts or writable workspace")
        alias, base = max(owners, key=lambda item: len(item[1].parts))
        parts = native.relative_to(base).parts
    else:
        if "\\" in value or rel.is_absolute():
            raise ValueError("Use an absolute filesystem path or a forward-slash mount alias")
        parts = rel.parts
        alias = parts[0] if parts else 'workspace'
        if alias in mounts:
            parts = parts[1:]
        else:
            alias = 'workspace'  # Never infer a source root or host cwd.
        base = mounts.get(alias)
    if base is None or (write and alias != "workspace"):
        raise ValueError("Path is outside the authorized mounts or writable workspace")
    if any(_private_name(part) for part in parts):
        raise ValueError("Private/runtime paths are not exposed to task tools")
    if discovery:
        # Exact roots must obey the same exclusions as directory discovery.
        # Check the lexical chain before resolve() can erase an in-mount link.
        for depth in range(len(parts) + 1):
            ancestor = base.joinpath(*parts[:depth])
            if (ancestor.is_symlink() or ancestor.is_junction()
                    or (ancestor.is_dir() and (ancestor / 'pyvenv.cfg').is_file())):
                raise ValueError('Symlink or Python environment material is excluded from discovery')
    # Mount owners resolve authorized sources; do not adopt a subsequently
    # replaced workspace symlink as a new writable root.
    candidate = base.joinpath(*parts).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError("Resolved path escapes its authorized mount")
    if any(_private_name(part) for part in candidate.relative_to(base).parts):
        raise ValueError("Private/runtime paths are not exposed to task tools")
    return candidate


def workspace_tool_specs() -> list[CommonToolSpec]:
    specs = []
    for name, (model, description, permission) in _TOOLS.items():
        schema = model.model_json_schema()
        if name == "edit_file":
            # Advertise one spelling; saved calls still use the unchanged
            # _Edit validator, without rewriting queued arguments or receipts.
            for field in ("old_text", "new_text"):
                schema["properties"].pop(field)
            schema["properties"]["replacements"]["minItems"] = 1
            schema["required"].append("replacements")
        specs.append(CommonToolSpec(name=name, description=description, input_schema=schema,
            permission_level=permission, risk_level="high" if permission == "execution" else "low",
            domain="workspace"))
    return specs


def validate_workspace_arguments(name: str, arguments: dict[str, Any]) -> BaseModel:
    return _TOOLS[name][0].model_validate(arguments)


def workspace_history_identity(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Project identity from a successful receipt, never current file state.

    The caller owns status/error checks and the immutable record locator. Keep
    recorded values (including unknown digests), without resolving paths,
    hashing text or inferring absent fields. This projection is idempotent.
    Legacy byte reads lack offset_bytes: retain that invocation argument in
    the history index; decoded text cannot recover the original byte count.
    Write/edit receipts describe the resulting version, not the precondition
    or backup. Neither this index nor a digest grants access or a stale edit.
    """
    if name not in {'read_file', 'write_file', 'edit_file', 'copy_file'}:
        return {}
    fields = ('path', 'absolute_path', 'sha256')
    if name == 'read_file':
        fields += ('size_bytes', 'offset_bytes', 'next_offset_bytes', 'start_line', 'has_more')
    elif name == 'copy_file':
        fields += ('source', 'source_sha256')
    return {key: data[key] for key in fields if key in data}


def workspace_observation_view(result: dict, *, record: str, directory_path: str | None = None) -> dict:
    """Compact model views; directory rows are lossless, copy previews explicit.

    Callers retain the original receipt at record. This is presentation, not a
    tool result, delivery inventory, or accepted tool-argument transformation.
    Project-copy previews link to the complete immutable file/version inventory.
    """
    if result.get('tool_name') == 'copy_project' and result.get('status') == 'ok' and not result.get('error'):
        data = result.get('data', {})
        files = data.get('files')
        if isinstance(files, dict) and len(files) > 30 and record:
            return {**result, 'data': {key: value for key, value in data.items() if key != 'files'} | {
                'copied_file_count': len(files), 'copied_paths': list(files)[:30],
                'copied_paths_omitted': len(files) - 30, 'inventory_record': record,
                'inventory_scope': 'First 30 copied paths only; complete path/digest inventory is in the immutable receipt. This is not evidence that other paths are absent.'}}
    if result.get("tool_name") != "list_directory" or result.get("status") != "ok" or result.get("error"):
        return result
    data = result.get("data")
    if not isinstance(data, dict) or "path_view" in data or not isinstance(record, str) or not record:
        return result
    # Recursive historical receipts lack absolute_path. Their original
    # absolute invocation can supply the root, but aliases are not guessed.
    root, entries = data.get("absolute_path", directory_path), data.get("entries")
    if not isinstance(root, str) or not isinstance(entries, list) or not entries or "\0" in root:
        return result
    windows = PureWindowsPath(root).is_absolute()
    path_type = PureWindowsPath if windows else PurePosixPath
    if not path_type(root).is_absolute() or any(part in {".", ".."} for part in re.split(r"[\\/]", root)):
        return result
    separator = "\\" if windows and "\\" in root else "/"
    if root.endswith(("/", "\\") if windows else ("/",)):
        separator = ""
    prefix = root + separator
    columns, rows = None, []
    for entry in entries:
        if not isinstance(entry, dict) or "relative_path" in entry:
            return result
        keys = list(entry)
        entry_columns = ["relative_path" if key == "path" else key for key in keys]
        if columns is None:
            columns = entry_columns
        elif entry_columns != columns:
            # Do not pad absent fields with null or reorder original fields.
            return result
        path = entry.get("path")
        if not isinstance(path, str) or "\0" in path or not path.startswith(prefix):
            return result
        relative = path[len(prefix):]
        parts = re.split(r"[\\/]", relative) if windows else relative.split("/")
        if (not path_type(path).is_absolute() or any(part in {"", ".", ".."} for part in parts)
                or (windows and ":" in relative)):
            return result
        rows.append([relative if key == "path" else entry[key] for key in keys])
    view = {**result, "data": {**data, "absolute_path": root,
        "entries": {"columns": columns, "rows": rows}, "path_view": {
        "record": record, "separator": separator,
        "root_from_invocation": "absolute_path" not in data,
        "read": "Read data.entries.rows in order; each row maps positionally to data.entries.columns. Original receipt is at record.",
        "join": "entry.path = data.absolute_path + data.path_view.separator + entry.relative_path",
    }}}
    try:
        # Include the explanation and provenance in the net serialization cost.
        smaller = len(json.dumps(view, ensure_ascii=False)) < len(json.dumps(result, ensure_ascii=False))
        return view if smaller else result
    except (TypeError, ValueError):
        return result


class WorkspaceToolGateway:
    def has_started(self, identity: str) -> bool:
        return (self.root / 'tools' / 'processes' / identity / 'invocation.json').is_file()

    def can_reconcile(self, name: str, identity: str) -> bool:
        if name == 'copy_project':
            return (self.root / 'attempts' / identity / 'project-copy.json').is_file()
        if name in {'run_process', 'search_files'}:
            receipt = self.root / 'tools' / 'processes' / identity / 'invocation.json'
            if not receipt.is_file():
                return False
            if read_json(receipt).get('status') not in {'finished', 'stopped'}:
                raise RuntimeError('Process is live or unknown; cannot replay')
            return True
        return False

    def __init__(self, root: Path, sources: dict[str, Path], ledger: BudgetLedger,
                 project_python: Path | None = None, record_artifacts: Any | None = None,
                 create_workspace: bool = True) -> None:
        self.root = root.resolve()
        # Keep a venv's entry path, not its resolved system-Python symlink.
        self.python = (project_python or Path(sys.executable)).expanduser().absolute()
        if not self.python.is_file():
            raise ValueError("Task Python interpreter is unavailable")
        self.workspace = self.root / "workspace"
        if create_workspace:
            self.workspace.mkdir(exist_ok=True)
        elif not self.workspace.is_dir():
            raise ValueError('Existing task workspace is unavailable')
        self.mounts = {"workspace": self.workspace, **{name: path.resolve() for name, path in sources.items()}}
        if "workspace" in sources:
            raise ValueError("Sources cannot replace the writable workspace mount")
        if any(_private_name(path.name) for path in self.mounts.values()):
            raise ValueError("Private/runtime files cannot be mounted as task sources")
        self.ledger = ledger
        self.record_artifacts = record_artifacts

    def resolve(self, value: str, *, write: bool = False) -> Path:
        return resolve_mounted_path(self.mounts, value, write=write)

    def resolve_material_root(self, value: str) -> Path:
        return resolve_mounted_path(self.mounts, value, discovery=True)

    def locator(self, value: str) -> str:
        """Expose a short authorized mount spelling, never a new access grant."""
        path = self.resolve(value)
        return min((f'{name}/{path.relative_to(root).as_posix()}'
                    for name, root in self.mounts.items() if path.is_relative_to(root)),
                   key=lambda locator: (len(locator), locator))

    def artifact_hashes(self, locators: Iterable[str]) -> dict[str, str]:
        """Bind current authorized files without loading whole artifacts into memory."""
        hashes = {}
        for locator in locators:
            path = self.resolve(locator)
            if not path.is_file():
                raise ValueError(f'Retained artifact is missing: {locator}')
            with path.open('rb') as handle:
                hashes[locator] = hashlib.file_digest(handle, 'sha256').hexdigest()
        return hashes

    def bind_inventory(self, project: Path, *, identity: str, material_candidates: bool = False) -> dict:
        # Read-only inventory ownership is also used by legacy generated repair.
        def page(offset: int) -> dict:
            observed = self.call('list_directory', {'path': str(project),
                'recursive': True, 'limit': 200, 'offset': offset}, identity=identity)
            if observed.status != 'ok':
                raise ValueError(f'Project inventory unavailable: {observed.error}')
            return observed.data
        return bind_project_inventory(project, resolve=self.resolve, list_page=page,
            material_candidates=material_candidates)

    def resolve_source_record(self, value: str) -> Path:
        """Source provenance follows resolved ownership, not a path's spelling."""
        path = self.resolve(value)
        excluded = [self.workspace]
        if "records" in self.mounts:
            excluded.append(self.mounts["records"])
        if any(path.is_relative_to(root) for root in excluded):
            raise ValueError("Generated output or attempt history is not original-source evidence")
        return path

    def call(self, name: str, arguments: dict[str, Any], *, identity: str,
             read_cache: dict | None = None) -> ToolResult:
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid durable tool invocation identity")
            model = validate_workspace_arguments(name, arguments)
            metadata = {}
            if name == 'copy_project':
                return self._copy_project(model, identity)
            if name == "copy_file":
                source = self.resolve(model.source)
                with source.open('rb') as handle:
                    if os.fstat(handle.fileno()).st_size > _COPY_MAX_BYTES:
                        raise ValueError('Copy source exceeds 64 MiB')
                    raw = handle.read(_COPY_MAX_BYTES + 1)
                if len(raw) > _COPY_MAX_BYTES:
                    raise ValueError('Copy source exceeds 64 MiB')
                digest = hashlib.sha256(raw).hexdigest()
                if digest != model.source_sha256:
                    raise ValueError('Copy source changed; inspect its current version')
                target = self.resolve(model.path, write=True)
                copied = self._commit_file(target, model, raw, name=name, identity=identity,
                    max_bytes=_COPY_MAX_BYTES)
                return ToolResult(tool_name=name, status=copied.status, error=copied.error,
                    data={**copied.data, 'source': model.source, 'source_sha256': digest}
                    if copied.ok or 'conflict' in copied.data else copied.data)
            if name == "deliver_result":
                data = self.resolve_delivery(model)
            elif name == "ask_user":
                data = model.model_dump()
            elif name == "run_process":
                recovering = (self.root / "tools" / "processes" / identity / "invocation.json").exists()
                data = self._run(model, identity)
                if model.delivery is not None:
                    if data.get('returncode') != 0 or data.get('stop_reason'):
                        return ToolResult(tool_name=name, status='blocked', data=data,
                            error='Final process failed or stopped; no delivery. Inspect the retained process receipt and correct or explicitly deliver existing work separately.')
                    data['delivery'], metadata['delivery_hashes'] = self._process_delivery(model, identity, recovering=recovering)
                elif data.get('returncode') != 0 or data.get('stop_reason'):
                    return ToolResult(tool_name=name, status='error', data=data,
                        error='Process failed or stopped; inspect returncode and retained stderr before correcting the same work.',
                        summary='Process did not complete successfully; logs and invocation retained.')
            elif name == "search_files":
                data = self._search(model, identity)
            elif name == "inspect_project":
                from simple_ar.research.preparation import inspect_project_preparation
                from simple_ar.code_task.review_pipeline import build_review_index
                from simple_ar.core.artifacts import write_json

                target = self.resolve(model.path)
                data_paths = tuple(self.resolve(path) for path in model.data_paths)
                # Apply this gateway's existing private-path policy before
                # any source is read, not by filtering an already-read index.
                def accessible(path: Path) -> bool:
                    try:
                        self.resolve(str(path))
                    except ValueError:
                        return False
                    return True
                index = build_review_index(target, excluded_names=frozenset(_PRIVATE),
                                           path_filter=accessible)
                facts = inspect_project_preparation(target, data_paths=data_paths, index=index)
                # The attempt owns this derived observation, not the source
                # acquisition store. It cannot become original paper evidence.
                record = self.root / "attempts" / identity / "project_inspection.json"
                write_json(record, facts)
                data = {key: facts[key] for key in (
                    "schema_version", "project", "file_count", "runtime",
                    "data_paths", "notes", "limitations")}
                data["task_python"] = str(self.python)
                data["inspection_record"] = str(record)
                data["declared_project"] = facts["declared_project"]
                probe = facts["dependency_probe"]
                data["dependency_probe"] = {**probe, "packages": probe["packages"][:50]}
                if len(probe["packages"]) > 50:
                    data["dependency_probe"]["packages_omitted"] = len(probe["packages"]) - 50
                for key in ("entrypoint_candidates", "declared_entrypoints", "notebook_candidates",
                            "document_paths", "test_files"):
                    data[key] = facts[key][:30]
                    if len(facts[key]) > 30:
                        data[key + "_omitted"] = len(facts[key]) - 30
                # Text remains available at exact original paths and in the
                # attempt record; the overview is not a second summary model.
                data["reading_coverage"] = facts["reading_coverage"]
            else:
                target = self.resolve(model.path, write=name in {"write_file", "edit_file"})
                if name == "list_directory":
                    if model.recursive:
                        return ToolResult(tool_name=name, data=self._list_files(target, model))
                    children = sorted(target.iterdir(), key=lambda p: p.name)
                    children = [p for p in children if not _private_name(p.name)
                                and fnmatch.fnmatchcase(p.name, model.file_glob)]
                    selected = children[model.offset:model.offset + model.limit]
                    data = {"entries": [{"name": p.name, "path": str(p), "directory": p.is_dir(), "symlink": p.is_symlink()}
                                        for p in selected], "next_offset": model.offset + len(selected),
                            "has_more": model.offset + len(selected) < len(children)}
                elif name == "read_file":
                    # Digest is streamed; large files are not materialized or sent to the model.
                    with target.open("rb") as handle:
                        def version(stat: os.stat_result | None = None) -> tuple:
                            stat = stat if stat is not None else os.fstat(handle.fileno())
                            return (str(target), stat.st_dev, stat.st_ino, stat.st_size,
                                stat.st_mtime_ns, stat.st_ctime_ns)

                        before = version()
                        size = before[3]
                        model.validate_read_size(size)
                        digest = None
                        if size <= 256000 or model.full_digest:
                            # An internal caller owns this ephemeral dictionary.
                            # Cache only an actual digest of this opened version,
                            # independent of offset/byte/line limits. Every call
                            # still resolves authority, opens and reads its window.
                            digest = read_cache.get(before) if read_cache is not None else None
                            if digest is None:
                                digest = hashlib.file_digest(handle, "sha256").hexdigest()
                        offset = model.offset_bytes
                        if model.start_line is not None:
                            handle.seek(0)
                            lines = io.BytesIO(handle.read(256001)).readlines()
                            offset = sum(map(len, lines[:model.start_line - 1]))
                            raw = b"".join(lines[model.start_line - 1:model.start_line - 1 + model.max_lines])[:model.max_bytes]
                        else:
                            handle.seek(offset)
                            raw = handle.read(model.max_bytes)
                            if 'max_lines' in model.model_fields_set:
                                raw = b''.join(io.BytesIO(raw).readlines()[:model.max_lines])
                        if (version() != before or self.resolve(model.path) != target
                                or version(target.stat()) != before):
                            raise ValueError('File changed during material read; inspect its current window')
                        if read_cache is not None and digest is not None:
                            read_cache[before] = digest
                    data = {"text": raw.decode("utf-8", errors="replace"), "sha256": digest,
                            "size_bytes": size, "next_offset_bytes": offset + len(raw),
                            "has_more": offset + len(raw) < size}
                    if model.start_line is not None:
                        data.update(start_line=model.start_line, offset_bytes=offset)
                else:
                    delivery = None
                    if name == "write_file" and model.delivery is not None:
                        delivery = self.resolve_delivery(model.delivery, writing=target)
                    if name == "edit_file":
                        # Reuse the same atomic writer below, rather than a
                        # second patch/runtime path. Recheck after preparing
                        # edits so changes since this snapshot are rejected.
                        original, conflict = self._write_precondition(target, model, name)
                        if conflict is not None:
                            return conflict
                        assert original is not None
                        text = original.decode("utf-8")
                        if model.replacements:
                            if model.old_text is not None or model.new_text is not None:
                                raise ValueError("Use replacements or old_text/new_text, not both")
                            replacements = ((change.old_text, change.new_text) for change in model.replacements)
                        elif model.old_text is not None and model.new_text is not None:
                            # _Edit already validated these exact field bounds.
                            replacements = [(model.old_text, model.new_text)]
                        else:
                            raise ValueError("Supply replacements or both old_text and new_text")
                        text = replace_exact_spans(text, replacements)
                        model = _Write(path=model.path, expected_sha256=model.expected_sha256,
                            content=text)
                    encoded = model.content.encode("utf-8")
                    if len(encoded) > 256000:
                        raise ValueError("File exceeds the per-write byte limit")
                    committed = self._commit_file(target, model, encoded, name=name, identity=identity)
                    if not committed.ok:
                        return committed
                    data = committed.data
                    if delivery is not None:
                        data["delivery"] = delivery
            if name in {"read_file", "write_file", "edit_file", "list_directory"}:
                data["absolute_path"] = str(target)
            return ToolResult(tool_name=name, data=data, metadata=metadata)
        except (ValueError, KeyError, BudgetError) as exc:
            return ToolResult(tool_name=name, status="blocked", error=str(exc))
        except OSError as exc:
            return ToolResult(tool_name=name, status="error", error=str(exc))

    def _commit_file(self, target: Path, request: _Write | _Copy, encoded: bytes, *,
                     name: str, identity: str, max_bytes: int = 256000) -> ToolResult:
        original, conflict = self._write_precondition(target, request, name, max_bytes=max_bytes)
        if conflict is not None:
            return conflict
        if original is not None:
            backup = self.root / "tools" / "edits" / identity / "previous"
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(original)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ToolResult(tool_name=name, data={"path": request.path, "absolute_path": str(target),
            "sha256": hashlib.sha256(encoded).hexdigest()})

    def _write_precondition(self, target: Path, request: _Write | _Edit | _Copy,
                            name: str, *, max_bytes: int = 256000) -> tuple[bytes | None, ToolResult | None]:
        """Observe only an already authorized writable target, without mutation."""
        original, current = None, None
        if target.exists():
            if not target.is_file():
                raise ValueError("Write target must be a regular file")
            with target.open("rb") as handle:
                if os.fstat(handle.fileno()).st_size > max_bytes:
                    raise ValueError("Existing file exceeds the bounded copy size" if name == 'copy_file' else "Existing file exceeds the editable text bound")
                original = handle.read(max_bytes + 1)
            if len(original) > max_bytes:
                raise ValueError("Existing file exceeds the bounded copy size" if name == 'copy_file' else "Existing file exceeds the editable text bound")
            current = hashlib.sha256(original).hexdigest()
        expected = request.expected_sha256
        reason = ("missing_expected_sha256" if current is not None and not expected else
                  "sha256_mismatch" if current is not None and current != expected else
                  "target_missing" if current is None and (expected or name == "edit_file") else "")
        if not reason:
            return original, None
        return original, ToolResult(tool_name=name, status="blocked", data={
            "executed": False, "path": request.path, "absolute_path": str(target),
            "current_sha256": current, "expected_sha256": expected, "conflict": reason,
            "retry": "Snapshot only. Retry must explicitly supply the matching expected_sha256; any subsequent change is still rejected.",
        }, error="File write precondition failed: " + reason)

    def _copy_project(self, request: _CopyProject, identity: str) -> ToolResult:
        from simple_ar.code_task.generation.common import generation_candidate
        from simple_ar.code_task.workspace.copy import copy_code_workspace
        from simple_ar.core.artifacts import read_json, write_json

        source = self.resolve(request.source)
        target = self.resolve(request.path, write=True)
        if (target == self.workspace or target.is_relative_to(source)
                or source.is_relative_to(target) or not source.is_dir()):
            raise ValueError('Use a project directory and a distinct new workspace subdirectory')
        record = self.root / 'attempts' / identity / 'project-copy.json'
        if record.exists():
            saved = read_json(record)
            if saved['request'] != request.model_dump():
                raise ValueError('Project copy identity is bound to a different request')
            data = saved['data']
            expected = {str(target / path): digest for path, digest in data['files'].items()}
            # Never re-copy a failed/interrupted operation, or replace user edits.
            if (data['absolute_path'] != str(target) or not target.is_dir()
                    or any(Path(path).is_symlink() or self.resolve(path) != Path(path) for path in expected)
                    or self.artifact_hashes(expected) != expected):
                raise ValueError('Published project is missing or changed; inspect the retained copy before continuing')
            return ToolResult(tool_name='copy_project', data=data)
        if target.exists():
            raise ValueError('Project destination already exists; preserve its changes and use a new directory')

        def allowed(path: Path) -> bool:
            try:
                return self.resolve(str(path)) == path
            except (ValueError, OSError, RuntimeError):
                return False

        with generation_candidate(target) as candidate:
            report = copy_code_workspace(source, candidate, max_file_bytes=request.max_file_bytes,
                max_files=request.max_files, max_total_bytes=request.max_total_bytes, path_allowed=allowed)
            paths = [str(path) for path in candidate.rglob('*') if path.is_file()]
            if not paths:
                raise ValueError('Project selection contains no copyable files')
            hashes = self.artifact_hashes(paths)
            files = {str(Path(path).relative_to(candidate)): digest for path, digest in hashes.items()}
            if request.archive_name is not None:
                archive = candidate / request.archive_name
                if archive.exists():
                    raise ValueError('Archive name conflicts with a copied input; choose another name')
                with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED) as package:
                    for relative in sorted(files):
                        package.write(candidate / relative, arcname=Path(relative).as_posix())
                with archive.open('rb') as handle:
                    files[request.archive_name] = hashlib.file_digest(handle, 'sha256').hexdigest()
            data = {'absolute_path': str(target), 'source': str(source), 'copy': report.to_json(), 'files': files}
            # The existing attempt owns this receipt. Save before publishing so
            # a lost outer result can validate the tree, not blindly copy again.
            if target.exists():
                raise ValueError('Project destination appeared during copying; it was not replaced')
            write_json(record, {'request': request.model_dump(), 'data': data})
        return ToolResult(tool_name='copy_project', data=data)

    def resolve_delivery(self, request: DeliveryRequest, *, writing: Path | None = None,
                         current_outputs: tuple[Path, ...] = ()) -> dict[str, Any]:
        retained = tuple(self.record_artifacts() if self.record_artifacts is not None else ()) + current_outputs
        def resolve_artifact(value: str) -> Path:
            path = self.resolve(value)
            evidence = self.mounts.get('evidence')
            registered = any(path == output or (output.is_dir() and path.is_relative_to(output)) for output in retained)
            if not path.is_relative_to(self.workspace) and not registered and not (
                evidence == self.root / 'evidence' and path.is_relative_to(evidence)
            ):
                raise ValueError('Delivery requires workspace output, retained evidence or registered domain output; external sources and unregistered attempt records are not deliverables')
            return path

        paths, artifacts = [], []
        for value in request.artifacts:
            path = resolve_artifact(value)
            if path.is_dir():
                inventory = self._list_files(path, _List(path=value, recursive=True, limit=200))
                if inventory['has_more'] or inventory['inventory_truncated']:
                    raise ValueError('Delivery directory exceeds the bounded inventory; select smaller directories or explicit files')
                selected = [entry['path'] for entry in inventory['entries']]
                if writing is not None and writing.is_relative_to(path):
                    selected.append(str(writing))
                if not selected:
                    raise ValueError('Delivery directory contains no accessible artifact files')
            else:
                selected = [value]
            for locator in selected:
                resolved = resolve_artifact(locator)
                if resolved not in paths:
                    paths.append(resolved)
                    artifacts.append(locator)
        if writing is not None and writing not in paths:
            raise ValueError("Final delivery must include the file being written")
        if any(path != writing and not path.is_file() for path in paths):
            raise ValueError("Delivery references a missing workspace file")
        return {**request.model_dump(), 'artifacts': artifacts}

    def _list_files(self, target: Path, request: _List) -> dict[str, Any]:
        return list_project_files(target, request, resolve=self.resolve, listed_path=self._listed_path)

    def _listed_path(self, path: Path, requested: str) -> str:
        if not requested:
            self.resolve(requested)  # Reject an empty locator through the shared access rule.
        if Path(requested).is_absolute():
            return str(path)
        alias = next(iter(PurePosixPath(requested).parts), None)
        if alias not in self.mounts:
            return self.locator(str(path))
        return f"{alias}/{path.relative_to(self.mounts[alias]).as_posix()}"

    def _search(self, request: _Search, identity: str) -> dict[str, Any]:
        executable = shutil.which("rg")
        if executable is None:
            raise ValueError("search_files requires ripgrep (rg) on PATH; no implicit installation or fallback")
        if "\n" in request.query or "\r" in request.query:
            raise ValueError("Search one literal line at a time")
        target = self.resolve(request.path)
        files, seen, visited, skipped_large, has_more = [], 0, 0, 0, False
        # Enumerate only authorized, non-symlink files. rg receives explicit
        # filenames, never an unrestricted recursive root or user flags.
        walk = os.walk(target, followlinks=False) if target.is_dir() else [(target.parent, [], [target.name])]
        for directory, dirs, names in walk:
            dirs[:] = sorted(d for d in dirs if not _private_name(d)
                             and not (Path(directory) / d).is_symlink()
                             and not (Path(directory) / d / "pyvenv.cfg").is_file())
            for name in sorted(names):
                visited += 1
                if visited > 4096:
                    has_more = True
                    break
                path = Path(directory) / name
                if path.is_symlink() or not fnmatch.fnmatchcase(name, request.file_glob):
                    continue
                try:
                    relative = self._listed_path(path, request.path)
                    path = self.resolve(relative)
                except ValueError:
                    continue
                if not path.is_file():
                    continue
                if path.stat().st_size > 256000:
                    skipped_large += 1
                    continue
                seen += 1
                if seen <= request.offset_files:
                    continue
                if len(files) == request.max_files:
                    has_more = True
                    break
                files.append(str(path))
            if has_more:
                break
        if not files:
            return {"text": "", "searched_files": 0, "has_more_files": has_more,
                    "skipped_large_files": skipped_large, "next_offset_files": request.offset_files}
        argv = [executable, "--no-config", "--fixed-strings", "--line-number", "--with-filename",
                "--color=never", "--max-count=30", "--max-columns=300", "--max-columns-preview"]
        if not request.case_sensitive:
            argv.append("--ignore-case")
        # The public arbitrary-command argv bound is separate from this fixed
        # search command's explicitly bounded filename batch.
        command = _Run.model_construct(argv=argv + ["--", request.query] + files, cwd="workspace", timeout_sec=15)
        result = self._run(command, identity)
        with (self.root / result["receipt"]).parent.joinpath("stdout.log").open("rb") as handle:
            raw = handle.read(12001)
        text = raw[:12000].decode("utf-8", errors="replace")
        if not Path(request.path).is_absolute():
            for alias, root in self.mounts.items():
                text = text.replace(str(root) + os.sep, alias + "/")
        return {"text": text, "searched_files": len(files), "has_more_files": has_more,
                "next_offset_files": request.offset_files + len(files), "skipped_large_files": skipped_large,
                "inventory_truncated": visited > 4096,
                "output_truncated": len(raw) > 12000, "matches_per_file_limit": 30,
                "returncode": result["returncode"], "stop_reason": result["stop_reason"],
                "stderr": result["stderr"], "receipt": result["receipt"]}

    def _run(self, request: _Run, identity: str) -> dict[str, Any]:
        cwd = self.resolve(request.cwd, write=True)
        output = self.root / "tools" / "processes" / identity
        receipt = output / "invocation.json"
        if receipt.exists():
            record = read_json(receipt)
            if record["status"] not in {"finished", "stopped"}:
                raise ValueError("Existing invocation is live or unknown; cannot replay it")
            settle_process_record(record, self.ledger)
            return {**process_observation(record, output), "receipt": str(receipt.relative_to(self.root))}
        argv = self.process_argv(request.argv)
        result = run_process(ProcessSpec(argv=argv, cwd=cwd, timeout_sec=request.timeout_sec,
            env=self.process_environment(), output_dir=output, invocation_id=identity, tail_bytes=12000, log_bytes=200000),
            budget_ledger=self.ledger)
        return {**process_observation(result.record, output), "receipt": str(receipt.relative_to(self.root))}

    def _process_delivery(self, request: _Run, identity: str, *, recovering: bool) -> tuple[dict[str, Any], dict[str, str]]:
        """The process owner pins its declared final outputs before the outer observation.

        Never attach a new success claim to an old or interrupted unbound run.
        Recovery consumes recorded output bytes, not another process or model.
        """
        from simple_ar.core.artifacts import write_json

        record = self.root / 'tools' / 'processes' / identity / 'terminal_delivery.json'
        binding = request.model_dump(exclude_none=True)
        if recovering:
            if not record.is_file():
                raise ValueError('Successful process has no bound terminal delivery; retain its receipt and inspect outputs. No replay or retroactive delivery')
            saved = read_json(record)
            if saved['request'] != binding:
                raise ValueError('Retained terminal process request changed; no replay or delivery')
            delivery = self.resolve_delivery(DeliveryRequest.model_validate(saved['delivery']))
            if self.artifact_hashes(delivery['artifacts']) != saved['artifact_sha256']:
                raise ValueError('Terminal process outputs changed or are missing; no replay or delivery')
            return delivery, saved['artifact_sha256']
        assert request.delivery is not None
        delivery = self.resolve_delivery(request.delivery)
        hashes = self.artifact_hashes(delivery['artifacts'])
        write_json(record, {'request': binding, 'delivery': delivery, 'artifact_sha256': hashes})
        return delivery, hashes

    def process_argv(self, argv: list[str]) -> list[str]:
        """Bind the task interpreter and whole mount arguments, never source text."""
        result = [str(self.resolve(value)) if next(iter(PurePosixPath(value).parts), None)
                in self.mounts else value for value in argv]
        if result and result[0] in {"python", "python3"}:
            result[0] = str(self.python)
        return result

    def process_environment(self) -> dict[str, str]:
        """Same filtered task environment for approval and new process launches."""
        env = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT", "WINDIR") if name in os.environ}
        inherited_path = env.get("PATH", "")
        env["PATH"] = str(self.python.parent) + (os.pathsep + inherited_path if inherited_path else "")
        env.update(MPLBACKEND="Agg", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                   PYTHONDONTWRITEBYTECODE="1")
        return env



def bind_project_inventory(project: Path, *, resolve: Any, list_page: Any = None,
                           material_candidates: bool = False) -> dict:
    # Mounted pagination owns discovery/private exclusions. Code and legacy
    # receipts bind every visible file; writing filters before hash/read.
    rows, omitted, offset, total = [], [], 0, 0
    while True:
        inventory = (list_page(offset) if list_page else list_project_files(project,
            _List(path=str(project), recursive=True, limit=200, offset=offset),
            resolve=resolve, listed_path=lambda path, requested: str(path)))
        for row in inventory['entries']:
            if material_candidates:
                suffix = Path(row['path']).suffix.lower()
                # Known binary containers have no consumer in composition.
                # Other names remain possible text windows (including files
                # without extensions); eligibility is not a scientific role.
                figures = {'.png', '.jpg', '.jpeg', '.svg', '.pdf'}
                binary = {'.bin', '.pt', '.pth', '.ckpt', '.safetensors', '.npy', '.npz',
                    '.parquet', '.arrow', '.feather', '.h5', '.hdf5', '.pkl', '.pickle',
                    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar', '.exe', '.dll',
                    '.so', '.dylib', '.o', '.obj', '.a', '.lib', '.mp4', '.mp3', '.wav',
                    '.gif', '.webp', '.tif', '.tiff', '.bmp', '.woff', '.woff2'}
                reason = ('unsupported_format' if suffix in binary else
                    'consumer_size_limit' if row['size_bytes'] > (
                        16_000_000 if suffix in figures else _FULL_DIGEST_MAX_BYTES) else '')
                if reason:
                    omitted.append({**row, 'reason': reason})
                    continue
            rows.append(row)
            total += row['size_bytes']
        if inventory['inventory_truncated'] or len(rows) + len(omitted) > 4096:
            raise ValueError('Project exceeds 4096 traversal entries; choose a focused project_path')
        if not material_candidates and total > 128 * 1024 * 1024:
            raise ValueError('Project exceeds the 128 MiB inventory/hash budget; choose a focused project_path')
        if not inventory['has_more']:
            break
        if inventory['next_offset'] <= offset:
            raise ValueError('Project inventory did not advance')
        offset = inventory['next_offset']
    if material_candidates:
        return {'project_path': str(project), 'inventory_policy': 'writing_candidates.v1',
            'candidate_files': {row['path']: row['size_bytes'] for row in rows}, 'omitted_files': omitted,
            'inventory_scope': 'Bounded authorized candidate paths/sizes, not a complete content inventory. '
                'Known unsupported binary formats and files above read_file full-digest (64 MiB) '
                'or figure (16 MB) limits are listed as omissions without reading/hashing. '
                'Unselected contents are not hash-bound; actual selected inputs bind full digests before Writer. '
                'Unlisted does not mean absent. Private/runtime paths, symlinks and Python environments '
                'are excluded before inspection. Text windows report unread ranges, not full reading '
                'or verified measurements.'}
    hashes = {}
    for row in rows:
        source = resolve(row['path'])
        if not source.is_file():
            raise ValueError('Retained inventory file is missing')
        with source.open('rb') as handle:
            hashes[row['path']] = hashlib.file_digest(handle, 'sha256').hexdigest()
    if len(hashes) != len(rows) or any(resolve(row['path']).stat().st_size
            != row['size_bytes'] for row in rows):
        raise ValueError('Project inventory changed while hashing; use a new candidate identity')
    return {'project_path': str(project), 'file_hashes': hashes,
        'inventory_scope': 'Complete mounted visible file inventory within bounds; private/runtime paths, symlinks and Python environments are excluded.'}

def list_project_files(target: Path, request: _List, *, resolve: Any, listed_path: Any) -> dict[str, Any]:
    if not target.is_dir():
        raise ValueError("Recursive listing requires a mounted directory")
    entries, matched, visited = [], 0, 0
    for directory, dirs, names in os.walk(target, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not _private_name(d)
                         and not (Path(directory) / d).is_symlink()
                         and not (Path(directory) / d / "pyvenv.cfg").is_file())
        visited += len(dirs)
        for name in sorted(names):
            visited += 1
            if visited > 4096:
                break
            path = Path(directory) / name
            if _private_name(name) or path.is_symlink() or not fnmatch.fnmatchcase(name, request.file_glob):
                continue
            relative = listed_path(path, request.path)
            path = resolve(relative)
            if not path.is_file():
                continue
            matched += 1
            if matched <= request.offset:
                continue
            if len(entries) == request.limit:
                return {"entries": entries, "next_offset": request.offset + len(entries),
                        "has_more": True, "inventory_truncated": False}
            entries.append({"path": relative, "size_bytes": path.stat().st_size})
        if visited > 4096:
            break
    return {"entries": entries, "next_offset": request.offset + len(entries),
            "has_more": False, "inventory_truncated": visited > 4096}
