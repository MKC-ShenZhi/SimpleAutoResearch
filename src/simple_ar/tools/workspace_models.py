"""Schemas, path resolution, and tool specs for WorkspaceToolGateway."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from simple_ar.core.artifacts import is_private_path_name as _private_name
from simple_ar.tools.specs import CommonToolSpec

_COPY_MAX_BYTES = 64 * 1024 * 1024
_FULL_DIGEST_MAX_BYTES = 64 * 1024 * 1024


class _ProcessArtifactRecord(NamedTuple):
    binding: dict | None
    output_versions: dict
    complete_outputs: dict | None
    successful_outputs: dict | None
    recorded_outputs: dict | None


def _process_artifact_record(record: dict) -> _ProcessArtifactRecord:
    """Interpret one invocation without reading its inputs or output files.

    Declared outputs still establish ownership for failed or interrupted runs.
    Raw output versions also locate historical edges even if the invocation
    failed. A working-view binding additionally requires every digest to be
    well formed. Current and historical checks validate each consumed output
    separately, so a sibling's digest does not invalidate that output.
    """
    binding = (record.get('input_binding') or {}).get('workspace_artifacts')
    if binding is None:
        return _ProcessArtifactRecord(None, {}, None, None, None)
    if binding.get('version') != 1:
        raise ValueError('Unsupported process artifact binding version')
    output = record.get('output_binding') or {}
    if not isinstance(output, dict):
        output = {}
    versions = output.get('outputs', {})
    if not isinstance(versions, dict):
        versions = {}
        complete = False
    else:
        complete = (output.get('version') == 1
            and set(versions) == set(binding['outputs']))
    successful = (record.get('status') == 'finished' and record.get('returncode') == 0
                  and not record.get('stop_reason') and not record.get('output_binding_error'))
    complete_outputs = versions if complete else None
    successful_outputs = complete_outputs if successful else None
    recorded_outputs = (successful_outputs if successful_outputs is not None
        and all(isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest)
                for digest in successful_outputs.values()) else None)
    return _ProcessArtifactRecord(binding, versions, complete_outputs, successful_outputs, recorded_outputs)


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
    max_bytes: int = Field(default=16000, ge=1, le=256000,
        description="Requested UTF-8 byte window, up to 256000 bytes. Choose enough for the current work; page larger files and respect the model input capacity.")
    full_digest: bool = Field(default=False, exclude_if=lambda value: value is False,
        description="Request a streamed full-file SHA256 for a file at most 64 MiB, regardless of the returned text-window size. Larger files are rejected. False preserves the normal digest policy: SHA256 only for files at most 256000 bytes.")
    start_line: int | None = Field(default=None, ge=1,
        description="One-based line from search results; cannot combine with a nonzero byte offset. Files up to 256000 bytes.")
    max_lines: int = Field(default=200, ge=1, le=1000,
        description="Line count for start_line reads (default 200); when explicitly supplied for a byte-origin read, also cap that window by lines. max_bytes still bounds bytes and may end within a line. Continue at next_offset_bytes.")

    def validate_read_size(self, size: int, *, full_digest_max_bytes: int | None = None) -> None:
        """Shared file-dependent admission; content and digest still use the open file."""
        limit = _FULL_DIGEST_MAX_BYTES if full_digest_max_bytes is None else full_digest_max_bytes
        if self.full_digest and size > limit:
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


class _ProjectResults(_Path):
    deliveries: bool = Field(default=False,
        description='Set true to list retained delivery versions of one selected task directory.')
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12, ge=1, le=40)


class ReportDelivery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['candidate', 'canonical']
    manuscript: str | None = None
    formats: list[Literal['markdown','tex','pdf']] = Field(default_factory=list,max_length=3,
        exclude_if=lambda value:not value,
        description='Requested output formats claimed by this delivery. PDF/TeX require a current publication of this manuscript. Empty retains historical declarations.')


class DeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1)
    summary_source: FileWindow | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Optional exact UTF-8 window of an actual delivered text file. After final work succeeds, the delivery owner replaces summary with this window and checks its existing delivery byte binding. No semantic representativeness or scientific correctness is certified. Omit to retain the free summary contract.')
    artifacts: list[str] = Field(default_factory=list,
        description="Existing workspace outputs (workspace-relative or explicitly mounted paths) or retained files in this task's evidence mount. Evidence remains read-only and is not certified. Directories expand to at most 200 accessible files; private paths, environments, bytecode caches and symlinks are excluded by existing inventory rules. New formal deliveries retain exact selected file bytes at the original terminal attempt. Storage failure prevents commit. Old deliveries without retained bytes are not reconstructed. This is not a whole-workspace archive or correctness certificate.")
    project_root: str | None = Field(default=None, min_length=1, exclude_if=lambda value: value is None,
        description='Optional workspace project root for later reuse. Only delivered artifacts below this root are included; other files, permissions and execution state are not transferred. State missing dependencies or data in the delivery when relevant.')
    unresolved: list[str] = Field(default_factory=list,
        description='Requested work still unfinished or a required defect still unresolved; nonempty makes delivery partial. Put inherent evidence limits in limitations instead.')
    limitations: list[str] = Field(default_factory=list,exclude_if=lambda value:not value,
        description='Scope, unavailable evidence and uncertainty honestly stated in the completed work. These do not by themselves mean the requested task is unfinished, and cannot waive required findings.')
    independent_review: bool | None = Field(default=None, exclude_if=lambda value: value is None,
        description='For tasks that allow a review decision, set false only when independent review is unnecessary for the request and result. Omit or set true to request it. This cannot waive required findings or change saved task policies.')
    report: ReportDelivery | None = Field(default=None, exclude_if=lambda value: value is None,
        description='For a formal report, identify the current editable manuscript and requested formats. Canonical means current required checks passed; candidate means unfinished work disclosed. Publication must match that manuscript; no scientific certification.')

    @field_serializer('summary_source')
    def serialize_summary_source(self, value: FileWindow | None) -> dict[str, Any] | None:
        # Explicit max_lines bounds byte windows; an omitted default must not
        # become an explicit bound when a receipt is parsed during recovery.
        return value.model_dump(exclude_unset=True) if value is not None else None


class CandidateReviewRequest(DeliveryRequest):
    """Investigate available work without claiming a completed delivery."""
    review_scope: Literal['candidate', 'delivery'] = Field(default='candidate',
        description='Use candidate to investigate the selected work without claiming the whole task is ready; '
        'use delivery only when this claim and inventory are intended as the complete final delivery. '
        'If omitted for a new model decision, the review covers the candidate.')
    summary: str = Field(min_length=1,
        description='The current claim to investigate in the available candidate, not a declaration that the task is complete.')
    artifacts: list[str] = Field(min_length=1,
        description='Select at least one actual existing workspace output or retained task-evidence file to investigate now. Do not list planned or missing files. A complete final inventory, finished publication and producer self-audit are not prerequisites. Existing path permissions and bounded directory expansion still apply.')
    unresolved: list[str] = Field(default_factory=list,
        description='Known requested work or required corrections still unfinished. These gaps do not prevent independent investigation of the available candidate.')
    limitations: list[str] = Field(default_factory=list, exclude_if=lambda value: not value,
        description='Known evidence limits, scope and uncertainty relevant to the current claim; these do not certify correctness or waive required findings.')
    independent_review: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None,
        description='An explicit candidate review always requests independent investigation.')


class ReviewedDeliveryRequest(DeliveryRequest):
    """Deliver a new claim, or select an existing independently reviewed one."""

    summary: str = Field(default='', description='Completion claim; omit when selecting a reviewed candidate.')
    reviewed_candidate: str | None = Field(default=None, min_length=1,
        exclude_if=lambda value: value is None,
        description='A passed delivery-scope review_result record or inspection identity. Deliver its unchanged complete claim and inventory without copying them. A local candidate review cannot represent the full delivery. Omit all other delivery fields when using this.')

    @model_validator(mode='after')
    def select_claim(self) -> 'ReviewedDeliveryRequest':
        if self.reviewed_candidate is not None:
            if (self.summary or self.summary_source is not None or self.artifacts or self.project_root is not None
                    or self.unresolved or self.limitations or self.report is not None
                    or self.independent_review is not None):
                raise ValueError('A reviewed candidate cannot be combined with a new delivery claim')
        elif not self.summary.strip():
            raise ValueError('Provide a delivery summary or select a reviewed candidate')
        return self


def prepared_report_delivery_schema(schema: dict, field: str) -> None:
    """Annotate the declared completion path, without relying on model names.

    This is only the loaded contract's presentation. Validation, serialization
    and historical wire models stay unchanged. Unrelated extension definitions
    must not acquire report semantics just because they share a name.
    """
    def objects(node: object, seen: frozenset[int] = frozenset()):
        if not isinstance(node, dict) or id(node) in seen:
            return
        seen = seen | {id(node)}
        yield node
        ref = node.get('$ref', '')
        if isinstance(ref, str) and ref.startswith('#/'):
            target = schema
            for part in ref[2:].split('/'):
                target = target.get(part.replace('~1', '/').replace('~0', '~'), {}) if isinstance(target, dict) else {}
            yield from objects(target, seen)
        for key in ('anyOf', 'oneOf', 'allOf'):
            for branch in node.get(key, []):
                yield from objects(branch, seen)

    completion = schema.get('properties', {}).get(field, {}) if field else schema
    for delivery in objects(completion):
        report = delivery.get('properties', {}).get('report')
        if not isinstance(report, dict):
            continue
        report['description'] = (
            'Prepared report proposed for delivery: identify the actual editable manuscript and '
            'requested formats. Disclose unfinished requested work in unresolved. '
            'Edition labels cannot certify checks or waive required findings. '
            'Only the independent owner conclusion can accept completion; publication must match this manuscript.')
        for model in objects(report):
            status = model.get('properties', {}).get('status')
            if isinstance(status, dict) and status.get('enum') in (['candidate', 'canonical'], ['canonical', 'candidate']):
                status['description'] = (
                    'Report edition label only: candidate for a draft/candidate, canonical for a final edition. '
                    'Neither asserts checks passed or decides task completion. Both enter the same applicable '
                    'independent owner review when requested work is ready and unresolved is empty.')


class _Write(_Path):
    content: str = Field(max_length=256000)
    expected_sha256: str = Field(default="", description="Current digest required before replacing an existing file; empty for a new file.")
    delivery: DeliveryRequest | None = Field(default=None,
        description="Optional explicit final delivery when this write finishes the task. Include this file and actual existing artifacts; disclose unresolved work. Does not certify scientific correctness. Saves the normal delivery manifest without another model turn.")


class _Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    old_text: str = Field(min_length=1, max_length=256000, description="Exact unique text to replace; include context when ambiguous.")
    new_text: str = Field(max_length=256000)


class _AnchoredReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_anchor: str = Field(min_length=1, max_length=4096, description="Exact unique text immediately before the block. This text is kept.")
    end_anchor: str = Field(min_length=1, max_length=4096, description="Exact unique text immediately after the block. This text is kept.")
    new_text: str = Field(max_length=256000, description="Replacement for everything between the anchors, including surrounding whitespace.")

    def exact_replacement(self, text: str) -> tuple[str, str]:
        def unique_offset(anchor: str) -> int:
            offset = text.find(anchor)
            if offset < 0 or text.find(anchor, offset + 1) >= 0:
                raise ValueError('Both edit anchors must occur exactly once')
            return offset

        start, end = map(unique_offset, (self.start_anchor, self.end_anchor))
        if end < start + len(self.start_anchor):
            raise ValueError('Edit anchors must be in order and not overlap')
        return (text[start:end + len(self.end_anchor)],
                self.start_anchor + self.new_text + self.end_anchor)


class _Copy(_Path):
    source: str = Field(description='Reuse the exact path from read_file, including a bare mount alias when it points to a file. Copies authorized regular files up to 64 MiB byte-for-byte.')
    source_sha256: str = Field(min_length=64, max_length=64, description='Observed full source digest; stale candidates are rejected.')
    expected_sha256: str = Field(default='', description='Observed destination digest required before replacing an existing file; empty for a new file.')


class _CopyProject(_Path):
    source: str = Field(description='Authorized project directory; originals remain unchanged.')
    delivery_version: int | None = Field(default=None, ge=1, exclude_if=lambda value: value is None,
        description='Copy an earlier task’s retained delivery. Use its task directory as source and select the version listed by list_project_results.')
    delivery_group: Literal['code_project', 'artifacts'] | None = Field(default=None,
        exclude_if=lambda value: value is None,
        description='With delivery_version, copy its declared project (default) or all retained artifacts in their saved layout, including older deliveries without a declared project.')
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


class _ContextEdit(_Edit):
    replacements: list[_Replacement | _AnchoredReplacement] = Field(default_factory=list, max_length=40,
        description="Related non-overlapping replacements against one original. Use unique anchors for long blocks.")
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64,
        description='Optional whole-file version pin.')


class _Run(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    argv: list[str] = Field(min_length=1, max_length=40)
    literal_argv: bool = Field(default=False, exclude_if=lambda value: not value,
        description='Use literal OS arguments relative to cwd, without mount-alias expansion. Bare python/python3 still selects the task interpreter. Does not add authority or sandboxing.')
    host_access: bool = Field(default=False, exclude_if=lambda value: not value,
        description='Request host execution when the task needs resources outside the workspace boundary, such as GPU or network. Requires explicit approval of this command; the host process is not sandboxed.')
    cwd: str = "workspace"
    timeout_sec: int = Field(default=60, ge=1,
        description='Positive wall-clock timeout; the task process budget and execution approval still apply.')
    inputs: list[str] = Field(default_factory=list, max_length=64, exclude_if=lambda value: not value,
        description='Optional existing regular-file inputs read at the start, including pre-existing scripts and data. An in-place edit can declare the same file as input and output. Files created by this command are outputs, even if the command later uses them. No dependency inference or extra permission. At most 64 MiB total per binding.')
    input_snapshot: dict[str, str] | None = Field(default=None, exclude_if=lambda value: value is None,
        description='Internal reference to an approved implementation execution owner; not an additional input or authority grant.')
    outputs: list[str] = Field(default_factory=list, max_length=64, exclude_if=lambda value: not value,
        description='Optional explicit workspace regular-file output locators. Bytes captured only after success in the process receipt; directories/globs are not expanded. Does not require other commands to declare outputs or certify correctness.')
    delivery: DeliveryRequest | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Optional explicit final delivery after this authorized process succeeds. Select actual output paths, including files produced by the command, and disclose unresolved work. Nonzero exit, stopping or missing artifacts never delivers. Does not certify scientific correctness. Use only as the last action, not instead of inspecting unknown results.")


class _Question(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, description="Only missing scientific semantics, scope or authorization; not model formatting errors.")


_TOOLS = {
    "list_directory": (_List, "List mounted entries, or discover files recursively by filename with explicit pagination and traversal limits; no file contents.", "read_only"),
    "read_file": (FileWindow, "Read a bounded file window by byte offset or one-based line; return digest and exact byte continuation.", "read_only"),
    "compile_python": (_Path, "Check Python syntax in a workspace file without executing it or writing bytecode.", "read_only"),
    "search_files": (_Search, "Search literal text with ripgrep in a bounded mounted file batch; return line locations and explicit coverage limits.", "read_only"),
    "inspect_project": (_Project, "Read project instructions, dependency declarations and Python entry candidates using the existing preparation inspector. It also lists indexed project SKILL.md methods: read relevant files and judge applicability before use; their instructions grant no permissions. Returns a compact overview and a retained full observation. No imports of project code, installation, downloads or execution; metadata describes the inspecting interpreter, not readiness of the task environment. Use its evidence to choose the next necessary preparation or validation action, not to repeat an inventory.", "read_only"),
    "list_project_results": (_ProjectResults, "Find earlier tasks in an authorized project task directory. Use deliveries=true to see retained versions and their delivered SKILL.md paths. Use copy_project to adopt a version, then read its method and investigate applicability to the current task. This reads saved results and grants no access, approvals, budget or execution authority.", "read_only"),
    "write_file": (_Write, "Create or replace a workspace text file; existing files require the observed digest.", "write_patch"),
    "copy_file": (_Copy, "Copy an observed file or generated candidate into the workspace without retransmitting its contents. Source digest must match; read_file with full_digest=true and a small max_bytes obtains a streamed full-file digest up to 64 MiB. Existing destinations require their digest and retain a byte backup through the shared atomic writer. Original sources stay unchanged. Does not execute or certify a repair.", "write_patch"),
    "copy_project": (_CopyProject, "Prepare a writable project copy and optional portable ZIP in one operation, including ordinary source, tests, configuration, documentation and binary assets. Destination must be a new workspace subdirectory. Uses existing conservative code-workspace copying: private/runtime paths, symlinks, caches/build outputs and oversized files are omitted and reported. Total bounds are checked before publishing. Keeps originals unchanged, records copied file and archive hashes, and reconciles the same interrupted operation only when its published files still match. Inspect omissions before claiming a complete deliverable. Not a complete Git clone, dependency install, execution or correctness certificate.", "write_patch"),
    "edit_file": (_Edit, "Replace one or multiple exact unique, non-overlapping spans in a workspace file atomically, with one observed digest and backup. Batch related changes rather than repeating calls.", "write_patch"),
    "run_process": (_Run, "Execute an argv without a shell in the workspace. Bare python/python3 use the task interpreter; its directory leads the filtered PATH for child commands. Use sys.executable for portable Python children. Declare known existing inputs and generated outputs when their relationship matters for reuse or publication; a script created by this command is an output, not a pre-existing input. Execution alone does not verify the result.", "execution"),
    "deliver_result": (DeliveryRequest, "Deliver actual workspace artifacts and optional retained task-evidence references; name unresolved work honestly.", "plan"),
    "review_result": (DeliveryRequest, "Submit a usable candidate and its actual completion claim for independent checking before final delivery. This does not finish the task. Keep the same claim and artifacts when delivering to reuse an applicable opinion; revise only affected work after findings.", "plan"),
    "ask_user": (_Question, "Ask a concentrated question only when needed to proceed; pause with all work preserved.", "plan"),
}
def resolve_source_mount(value: Path) -> Path:
    """Admit a new read-only source before configuration or model side effects."""
    path = value.expanduser().absolute()
    resolved = path.resolve(strict=True)
    for candidate in (path, resolved):
        if (any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents))
                or any(_private_name(part) for part in candidate.parts)):
            raise ValueError(f'Private/runtime or symlink material is not a task input: {path}')
    if not (resolved.is_file() or resolved.is_dir()):
        raise ValueError(f'Material must be a regular file or directory: {resolved}')
    return resolved


def resolve_mounted_path(mounts: dict[str, Path], value: str, *, write: bool = False,
                         discovery: bool = False, authorize: Any = None) -> Path:
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
    if parts and base.is_file():
        raise ValueError(f"Mount {alias!r} is a file. Use {alias!r} as the path without a filename suffix.")
    if any(_private_name(part) for part in parts):
        raise ValueError("Private/runtime paths are not exposed to task tools")
    if authorize is not None:
        authorize(base.joinpath(*parts))
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


def workspace_tool_specs(*, candidate_review: bool = False,
                         process_artifact_paths: str | None = None,
                         candidate_review_protocol: str | None = None,
                         sandbox_execution: bool = False) -> list[CommonToolSpec]:
    if candidate_review_protocol not in (None, 'candidate.v1'):
        raise ValueError('Unsupported saved candidate review protocol')
    specs = []
    for name, (model, description, permission) in _TOOLS.items():
        if name == 'review_result' and not candidate_review:
            continue  # Historical and standalone owners retain their offered tools.
        if name == 'review_result' and candidate_review_protocol == 'candidate.v1':
            model = CandidateReviewRequest
            description = ('Investigate a current candidate and its actual available files against the original '
                'request. Known unfinished work, missing requested publication and incomplete final packaging '
                'do not prevent checks already possible. Submit this operation alone after observing production. '
                'It returns independent findings without delivering. Choose review_scope=delivery only for '
                'the intended complete final claim and inventory, so an unchanged delivery can reuse its '
                'opinion; otherwise the selected candidate is checked without implying final packaging. '
                'A pending review itself is not unfinished requested work.')
        if name == 'deliver_result' and candidate_review_protocol == 'candidate.v1':
            model = ReviewedDeliveryRequest
            description = ('Deliver actual artifacts and disclose unfinished work, or select a passed '
                'review_result by its record or identity to deliver the same reviewed claim and inventory. '
                'A changed request or artifact requires a current applicable review.')
        if candidate_review_protocol == 'candidate.v1' and name == 'edit_file':
            model = _ContextEdit
            description = ('Replace text in a workspace file using exact old text or two short unique anchors. '
                'For a long block, supply start_anchor and end_anchor to replace only the text between them. '
                'The tool checks the current text and commits non-overlapping replacements together. '
                'A whole-file version pin is optional.')
        schema = model.model_json_schema()
        if candidate_review_protocol == 'candidate.v1' and name in {'write_file', 'copy_file'}:
            schema['properties']['expected_sha256']['description'] = (
                'Observed SHA256 of the destination version being changed. Reuse an existing observation '
                'while that version is unchanged; a new read is not required each turn. '
                'Execution checks the hash atomically and rejects changed bytes. '
                'Empty for a new destination file.')
        if name == 'run_process':
            description += (' The tool already captures stdout and stderr. Prefer direct argv. '
                'If a shell pipeline is necessary, preserve upstream failures with pipefail.')
            description += (' Task authorization permits execution in the workspace without per-command approval. '
                'Processes cannot access original source mounts, the network or host GPU; copy needed sources '
                'to the workspace. Host process and CPU discovery via /proc is unavailable; '
                'use an explicit bounded worker count if the program needs one. '
                'Request host_access when the work needs a wider scope.'
                if sandbox_execution else ' Requires exact process approval and is not a sandbox.')
            # Professional execution supplies this owner reference internally.
            # Ordinary commands retain their explicit file-list contract.
            schema['properties'].pop('input_snapshot')
            if not sandbox_execution:
                schema['properties'].pop('host_access')
            if process_artifact_paths == 'cwd.v1':
                for field in ('inputs', 'outputs'):
                    schema['properties'][field]['description'] += (
                        ' Use the workspace_path returned by a write, edit or copy for an existing workspace file. '
                        ' Plain relative paths are relative to cwd, like command file arguments. '
                        'Explicit mount aliases (workspace/..., source0/...) and absolute paths '
                        'keep mount semantics; use ./ for a cwd-relative name that matches an alias.')
        if name == "edit_file":
            # Advertise one spelling; saved calls still use the unchanged
            # _Edit validator, without rewriting queued arguments or receipts.
            for field in ("old_text", "new_text"):
                schema["properties"].pop(field)
            schema["properties"]["replacements"]["minItems"] = 1
            schema["required"].append("replacements")
        specs.append(CommonToolSpec(name=name, description=description, input_schema=schema,
            permission_level=permission, risk_level="high" if permission == "execution" else "low",
            domain="workspace", completion=(
                {'kind': 'question', 'closing': True} if name == 'ask_user' else
                {'kind': 'review'} if name == 'review_result' else
                {'closing': True} if name == 'deliver_result' else
                {'field': 'delivery', 'closing': True} if name in {'write_file', 'run_process'} else None)))
    return specs


def validate_workspace_arguments(name: str, arguments: dict[str, Any], *,
                                 candidate_review_protocol: str | None = None) -> BaseModel:
    if candidate_review_protocol not in (None, 'candidate.v1'):
        raise ValueError('Unsupported saved candidate review protocol')
    if name == 'review_result' and candidate_review_protocol == 'candidate.v1':
        return CandidateReviewRequest.model_validate(arguments)
    if name == 'deliver_result' and candidate_review_protocol == 'candidate.v1':
        return ReviewedDeliveryRequest.model_validate(arguments)
    if name == 'edit_file' and candidate_review_protocol == 'candidate.v1':
        return _ContextEdit.model_validate(arguments)
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
    if result.get('tool_name') == 'run_process' and result.get('status') == 'error' and record:
        data = result.get('data') or {}
        projected = dict(data)
        for stream, limit in (('stdout', 1200), ('stderr', 3200)):
            text = data.get(stream)
            if isinstance(text, str) and len(text) > limit:
                projected.pop(stream)
                projected[stream + '_tail'] = {'text': text[-limit:],
                    'omitted_characters': len(text) - limit, 'record': record, 'field': 'data.' + stream}
        return {**result, 'data': projected}
    if (result.get('tool_name') == 'edit_file' and result.get('status') == 'ok'
            and not result.get('error') and isinstance(record, str) and record):
        data = result.get('data')
        if isinstance(data, dict) and 'authored_text' in data:
            return {**result, 'data': {
                key: value for key, value in data.items() if key != 'authored_text'} | {
                    'authored_text_source': {'record': record, 'field': 'data.authored_text',
                        'scope': 'Complete authored edit result retained at this field; not a read or independent evidence. Body is not supplied by this view.'}}}
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


class PartialProjectDelivery(ValueError):
    """A copied project cannot be reused whole, but delivered members may be portable."""

