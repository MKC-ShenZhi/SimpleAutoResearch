"""Explicitly mounted workspace tools; authorization, not a filesystem sandbox."""
from __future__ import annotations

from copy import deepcopy
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
from typing import Any, Iterable, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from simple_ar.core.artifacts import (is_private_path_name as _private_name,
    file_stat_version, read_json, replace_exact_spans)
from simple_ar.core.budget import BudgetError, BudgetExceededError, BudgetLedger
from simple_ar.core.process import ProcessSpec, process_observation, run_process, settle_process_record
from simple_ar.tools.specs import CommonToolSpec, ToolResult, tool_completion



from simple_ar.tools.workspace_models import (  # noqa: F401
    CandidateReviewRequest,
    DeliveryRequest,
    FileWindow,
    PartialProjectDelivery,
    ReportDelivery,
    ReviewedDeliveryRequest,
    _AnchoredReplacement,
    _COPY_MAX_BYTES,
    _ContextEdit,
    _Copy,
    _CopyProject,
    _Edit,
    _FULL_DIGEST_MAX_BYTES,
    _List,
    _Path,
    _ProcessArtifactRecord,
    _Project,
    _ProjectResults,
    _Question,
    _Replacement,
    _Run,
    _Search,
    _TOOLS,
    _Write,
    _process_artifact_record,
    missing_delivery_files,
    prepared_report_delivery_schema,
    resolve_mounted_path,
    resolve_source_mount,
    validate_delivery_patterns,
    validate_workspace_arguments,
    workspace_history_identity,
    workspace_observation_view,
    workspace_tool_specs,
)
from simple_ar.tools.workspace_delivery import (  # noqa: F401
    RetainedDelivery,
    bind_project_inventory,
    current_delivery_turn,
    list_project_files,
    read_terminal_delivery,
)


class WorkspaceToolGateway:
    @classmethod
    def for_task(cls, root: Path, config: dict, ledger: BudgetLedger, *,
                 record_artifacts: Any = None, create_workspace: bool = True) -> "WorkspaceToolGateway":
        """Open the native workspace with the task's saved execution settings."""
        evidence = root / 'evidence'
        if create_workspace:
            evidence.mkdir(exist_ok=True)
        return cls(root, {**{alias: Path(path) for alias, path in config['sources'].items()},
            'evidence': evidence, 'records': root / 'attempts'}, ledger,
            project_python=Path(config['project_python']) if config.get('project_python') else None,
            record_artifacts=record_artifacts, create_workspace=create_workspace,
            process_artifact_paths=config.get('process_artifact_paths'),
            candidate_review_protocol=config.get('candidate_review_protocol'),
            sandbox_execution=config.get('sandbox_execution', False))

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
                 create_workspace: bool = True, process_artifact_paths: str | None = None,
                 candidate_review_protocol: str | None = None,
                 sandbox_execution: bool = False) -> None:
        if process_artifact_paths not in (None, 'cwd.v1'):
            raise ValueError('Unsupported saved process artifact paths')
        self.process_artifact_paths = process_artifact_paths
        if candidate_review_protocol not in (None, 'candidate.v1'):
            raise ValueError('Unsupported saved candidate review protocol')
        self.candidate_review_protocol = candidate_review_protocol
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
        self.sandbox_execution = sandbox_execution

    def process_sandbox(self, cwd: Path, *, extra_read: tuple[Path, ...] = (),
                        python: Path | None = None, writable: Path | None = None):
        if not self.sandbox_execution:
            return None
        from simple_ar.core.process_sandbox import ProcessSandbox
        workspace = self.workspace.resolve()
        working = cwd.resolve()
        relative = working.relative_to(workspace) if working.is_relative_to(workspace) else Path('.')
        # Independent inspection owns only its own scratch directory. It can
        # read the task workspace but cannot change the candidate under review.
        permitted = (writable.resolve() if writable is not None else
            workspace.joinpath(*relative.parts[:2])
            if len(relative.parts) >= 2 and relative.parts[0] == 'inspection-work' else workspace)
        return ProcessSandbox(workspace=workspace, writable=permitted, python=python or self.python,
                              extra_read=extra_read)

    def within_task_scope(self, name: str, binding: dict[str, Any]) -> bool:
        if not self.sandbox_execution or name != 'run_process':
            return False
        arguments = json.loads(binding['call']['function']['arguments'])
        return not arguments.get('host_access', False)

    def resolve(self, value: str, *, write: bool = False) -> Path:
        diagnostic = self._process_diagnostic_path(value, write=write)
        if diagnostic is not None:
            return diagnostic
        return resolve_mounted_path(self.mounts, value, write=write)

    def _process_diagnostic_path(self, value: str, *, write: bool = False) -> Path | None:
        """Read an invocation's own diagnostics, without changing saved mounts.

        A receipt identifies logs, not execution success or scientific evidence.
        No task-root alias, process inventory, output directory or replay is granted.
        """
        path = Path(value)
        processes = self.root / 'tools' / 'processes'
        if not path.is_absolute() or not path.is_relative_to(processes):
            return None
        parts = path.relative_to(processes).parts
        if write or len(parts) not in {1, 2}:
            raise ValueError('Only invocation diagnostics may be read; no process-root access or writes')
        # This temporary resolver scope is never published as a mount/binding.
        # Starting at tools checks its links as well as processes and the leaf.
        scope = {'diagnostic': self.root / 'tools'}
        directory = processes / parts[0]
        receipt = resolve_mounted_path(scope, str(directory / 'invocation.json'), discovery=True)
        if not receipt.is_file():
            raise ValueError('Diagnostic directory has no existing process invocation receipt')
        record = read_json(receipt)
        if (not isinstance(record, dict) or record.get('schema_version') != 'process_invocation.v1'
                or record.get('invocation_id') != parts[0]):
            raise ValueError('Diagnostic directory has no matching process invocation receipt')
        if len(parts) == 2 and parts[1] != 'invocation.json':
            if parts[1] not in {'stdout.log', 'stderr.log'}:
                raise ValueError('Only invocation.json and its recorded stdout/stderr logs are diagnostic files')
            streams = record.get('streams')
            stream = streams.get(parts[1][:-4]) if isinstance(streams, dict) else None
            if not isinstance(stream, dict) or stream.get('log_path') != str(directory / parts[1]):
                raise ValueError('Diagnostic log is not identified by this process receipt')
        admitted = resolve_mounted_path(scope, value, discovery=True)
        if not (admitted.is_dir() if len(parts) == 1 else admitted.is_file()):
            raise ValueError('Recorded process diagnostic is unavailable')
        return admitted

    def _diagnostic_visible(self, path: Path) -> bool:
        try:
            return self._process_diagnostic_path(str(path)) is not None
        except (OSError, ValueError):
            return False

    def resolve_material_root(self, value: str) -> Path:
        return resolve_mounted_path(self.mounts, value, discovery=True)

    def material_boundary(self, value: str) -> Path:
        """Return the existing mount grant after admitting a discovery root."""
        path = self.resolve_material_root(value)
        return max((root for root in self.mounts.values() if path.is_relative_to(root)),
                   key=lambda root: len(root.parts))

    def locator(self, value: str) -> str:
        """Expose a short authorized mount spelling, never a new access grant."""
        path = self.resolve(value)
        return min((f'{name}/{path.relative_to(root).as_posix()}'
                    for name, root in self.mounts.items() if path.is_relative_to(root)),
                   key=lambda locator: (len(locator), locator), default=str(path))

    def resolved_versions(self, versions: dict[str, str]) -> dict[Path, str]:
        """Normalize authorized aliases; never read bytes or transfer roles."""
        result = {}
        for locator, digest in versions.items():
            path = self.resolve(locator)
            if path in result and result[path] != digest:
                raise ValueError('Conflicting hashes for aliases of the same artifact')
            result[path] = digest
        return result

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

    def working_result(self, observations: Iterable[dict], *, saved_delivery: dict | None,
                       completions: dict | None = None) -> dict[str, Any]:
        """Project existing owner receipts, without checking bytes or adopting work.

        A recorded version is not a current version check. Only referenced files
        are resolved; process declarations and directory contents are never outputs.
        """
        identities, artifacts, last_delivery, latest_produced = [], [], None, None
        session_id = None
        last_operation, next_action = None, ''
        for event in observations:
            observed = event['observation']
            name, data = observed.get('tool_name'), observed.get('data') or {}
            last_operation = {'tool_name': name, 'status': observed.get('status'),
                              'failure_kind': data.get('failure_kind')}
            next_action = data.get('next_action') or next_action
            successful = observed.get('status') == 'ok'
            metadata = observed.get('metadata') or {}
            owner = {'record': 'records/' + event['record'].removeprefix('attempts/'), 'tool_name': name,
                     'request_count': event['request_count']}
            groups = []
            pinned = metadata.get('delivery_hashes') if successful else None
            completion = tool_completion(name, completions)
            delivery = (data.get(completion.field) if completion.field else data
                        ) if completion is not None and completion.kind == 'delivery' else None
            if successful and isinstance(delivery, dict) and delivery == saved_delivery:
                last_delivery = {**owner, 'summary': delivery.get('summary', ''),
                    'report': delivery.get('report'), 'artifact_count': len(delivery.get('artifacts', [])),
                    'unresolved': delivery.get('unresolved', []), 'limitations': delivery.get('limitations', []),
                    'version_binding': 'recorded' if isinstance(pinned, dict) and bool(pinned)
                        and set(pinned) == set(delivery.get('artifacts', [])) and all(
                        isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest)
                        for digest in pinned.values()) else 'unknown',
                    'acceptance': 'saved_delivery_only',
                    'scope': 'Recorded delivery for this request count; no current acceptance or byte verification.'}
            if isinstance(pinned, dict):
                groups.append((owner, pinned, 'delivery_binding'))
            # Professional owners may have recorded their actual output bytes
            # in the accepted observation. Registration without an original
            # observation binding is not enough to upgrade historical files.
            versions = data.get('artifact_sha256')
            inspection = metadata.get('inspection_binding') or {}
            if isinstance(inspection, dict) and data.get('record') and inspection.get('assessment_sha256'):
                versions = {**(versions if isinstance(versions, dict) else {}),
                            data['record']: inspection['assessment_sha256']}
            owner_output = successful and name not in {'finish_professional_work', 'return_source_work'} and isinstance(versions, dict)
            process_output = successful and name == 'run_process' and isinstance(data.get('receipt'), str)
            if owner_output or process_output:
                try:
                    from simple_ar.tools.professional_outcomes import accepted_native_result
                    if session_id is None:
                        session_id = read_json(self.root / 'session_manifest.json')['session_id']
                    accepted_native_result(self.root, session_id, observed, Path(event['record']).parent.name,
                                           require_content_binding=True)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    pass
                else:
                    if owner_output:
                        groups.append((owner, versions, 'professional_output_binding'))
                    if process_output:
                        # The invocation owns its outputs, including recovered
                        # runs. Reuse the accepted observation above.
                        try:
                            receipt = self._process_diagnostic_path(str(self.root / data['receipt']))
                            if receipt is not None and receipt.name == 'invocation.json':
                                raw = receipt.read_bytes()
                                if hashlib.sha256(raw).hexdigest() != data.get('receipt_sha256'):
                                    raise ValueError('Process receipt is not bound to the accepted observation')
                                process = json.loads(raw.decode('utf-8'))
                                outputs = _process_artifact_record(process).recorded_outputs
                                if (receipt.parent.name == Path(event['record']).parent.name
                                        and process.get('invocation_id') == receipt.parent.name and outputs is not None):
                                    groups.append(({**owner, 'receipt': str(receipt)},
                                                   outputs, 'process_output_binding'))
                        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                            pass  # Unbound historical execution is not production proof.
            # Existing native artifact identities remain visible even without a
            # version binding; primitive write receipts already own their digest.
            native = dict.fromkeys(observed.get('artifacts') or [])
            if successful and name in {'write_file', 'edit_file', 'copy_file'} and data.get('absolute_path'):
                groups.append((owner, {data['absolute_path']: data.get('sha256')}, 'write_binding'))
            groups.append((owner, native, 'native_artifact'))
            produced, seen = [], set()
            for provenance, versions, kind in groups:
                for locator, digest in versions.items():
                    if not isinstance(locator, str):
                        continue
                    bound = isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest) is not None
                    row = {'locator': locator, 'recorded_sha256': digest if bound else None,
                           'version_binding': 'recorded' if bound else 'unknown',
                           'current_version': 'unchecked', 'provenance': {**provenance, 'kind': kind}}
                    try:
                        path = self.resolve(locator)
                        if not path.is_relative_to(self.workspace):
                            continue  # Source aliases never become current workspace work.
                        row['locator'] = 'workspace/' + path.relative_to(self.workspace).as_posix()
                        row['absolute_path'] = str(path)
                        if path in seen:
                            continue  # Prefer the owner binding over an unversioned alias.
                        seen.add(path)
                        row['presence'] = 'present' if path.is_file() else 'missing'
                        if row['presence'] == 'present' and row['locator'] not in artifacts:
                            artifacts.append(row['locator'])
                    except (OSError, ValueError):
                        row['presence'] = 'inaccessible'
                    identities.append(row)
                    if bound:
                        produced.append(row)
            if produced:
                latest_produced = {**owner,
                    **({'receipt': produced[0]['provenance']['receipt']}
                       if 'receipt' in produced[0]['provenance'] else {}), 'identities': produced}
        return {'artifacts': artifacts, 'identities': identities, 'last_delivery': last_delivery,
                'latest_produced': latest_produced, 'acceptance': 'not_inferred',
                'last_operation': last_operation,
                'next_action': next_action or 'Resume this saved task after addressing the stop condition; completed work and accounting are retained.'}

    def _version_hashes(self, locators: Iterable[str]) -> dict[str, str]:
        """Bound only declared files, with finite streamed I/O."""
        hashes, total = {}, 0
        for locator in locators:
            path = resolve_mounted_path(self.mounts, locator, discovery=True)
            if not path.is_file():
                raise ValueError(f'Declared process artifact is missing: {locator}')
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                while raw := handle.read(65536):
                    total += len(raw)
                    if total > _FULL_DIGEST_MAX_BYTES:
                        raise ValueError('Declared process artifact binding exceeds 64 MiB')
                    digest.update(raw)
            hashes[locator] = digest.hexdigest()
        return hashes

    def approval_binding(self, call: dict[str, Any]) -> dict[str, Any]:
        """Bind the effective operation without granting execution or retaining mutable caller data."""
        # Pin the effective command/environment and directly mentioned text files.
        arguments = validate_workspace_arguments("run_process",
            json.loads(call["function"]["arguments"])).model_dump()
        observed = {}
        cwd = self.resolve(arguments.get("cwd", "workspace"), write=True)
        argv = self.process_argv(arguments.get("argv", []),
            resolve_mounts=not arguments.get('literal_argv', False))
        for value in argv:
            # argv includes literals and source text (e.g. python -c), not
            # exclusively filenames. The whole command is pinned below;
            # only existing, directly referenced files add content digests.
            try:
                candidate = (cwd / value).resolve()
                if candidate.is_file() and candidate.stat().st_size <= 256000:
                    with candidate.open("rb") as handle:
                        observed[value] = hashlib.file_digest(handle, "sha256").hexdigest()
            except (OSError, ValueError):
                continue
        payload = {"call": deepcopy(call), "files": observed,
            "argv": argv, "cwd": str(cwd), "environment": self.process_environment()}
        artifacts = self.process_artifact_binding(arguments)
        if artifacts is not None:
            payload['artifacts'] = artifacts
        return payload

    @staticmethod
    def approval_digest(binding: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    def approval_token(self, call: dict[str, Any]) -> str:
        """Identify the actual command and its bound inputs for approval."""
        return self.execution_approval(call)['token']

    def execution_approval(self, call: dict[str, Any], *, pending: dict | None = None,
                           owner: Any = None) -> dict[str, Any]:
        """Read the effective binding and token together, without approving work.

        The caller supplies its policy-authorized professional owner, if any.
        No application load, dispatch, ledger settlement or record write occurs.
        A nested action owns its own attempt and retained approval; the parent
        inspection must never supply their identity or historical token shape.
        """
        if owner is None:
            binding = self.approval_binding(call)
            return {'binding': binding, 'token': self.approval_digest(binding)}
        active = (pending or {}).get('action') or pending or {}
        name = call['function']['name']
        binding = owner.approval_binding(json.loads(call['function']['arguments']),
            identity=active['attempt'],
            **({'for_preparation': True} if name == 'prepare_code_task' else {}))
        retained = active.get('execution_approval') or active.get('implementation_approval')
        if (name == 'execute_implementation' and active.get('call') == call
                and retained and 'binding' in retained
                and 'process_artifacts' not in retained['binding']):
            # Recompute current bytes, preserving only the original schema.
            # A legacy approval is never upgraded to a stronger proof on read.
            binding = {key: value for key, value in binding.items() if key != 'process_artifacts'}
        return {'binding': binding,
            'token': self.approval_digest({'call': call, 'inputs': binding})}

    def resolve_process_artifact(self, value: str, cwd: str, *, write: bool = False) -> Path:
        """One coordinate rule for approval, execution and observation provenance."""
        if (self.process_artifact_paths == 'cwd.v1' and not Path(value).is_absolute()
                and (value.startswith('./') or value.split('/')[0] not in self.mounts)):
            if not value or '\\' in value:
                raise ValueError('Use a nonempty forward-slash artifact path')
            value = str(self.resolve(cwd, write=True) / value)
        return resolve_mounted_path(self.mounts, value, write=write, discovery=True)

    def process_artifact_binding(self, arguments: dict[str, Any], *, identity: str | None = None) -> dict[str, Any] | None:
        """Read-only approval hook; absent declarations preserve historical tokens."""
        request = _Run.model_validate(arguments)
        if not request.inputs and not request.outputs and request.input_snapshot is None:
            return None
        if request.input_snapshot is not None:
            if request.inputs:
                raise ValueError('Use an approved implementation snapshot or direct process inputs, not both')
            from simple_ar.tools.implementation_run import resolve_execution_snapshot

            versions = resolve_execution_snapshot(self, request.input_snapshot,
                process=request.model_dump(exclude_none=True), identity=identity)
            inputs = {self.locator(path): digest for path, digest in versions.items()}
        else:
            inputs = list(dict.fromkeys(self.locator(str(self.resolve_process_artifact(value, request.cwd)))
                                        for value in request.inputs))
            inputs = self._version_hashes(inputs)
        outputs = list(dict.fromkeys(self.locator(str(self.resolve_process_artifact(value, request.cwd, write=True)))
                                     for value in request.outputs))
        if any(self.resolve(value).exists() and not self.resolve(value).is_file() for value in outputs):
            raise ValueError('Declared process outputs must be regular files')
        return {'version': 1, 'request': request.model_dump(exclude_none=True),
                'inputs': inputs, 'outputs': outputs,
                **({'input_snapshot': request.input_snapshot} if request.input_snapshot is not None else {})}

    def _completed_process_outputs(self, binding: dict | None, identity: str | None) -> dict[str, str]:
        """Read output versions only from this operation's successful receipt."""
        if binding is None or identity is None:
            return {}
        receipt = self.root / 'tools' / 'processes' / identity / 'invocation.json'
        if not receipt.is_file():
            return {}
        record = read_json(receipt)
        interpreted = _process_artifact_record(record)
        if record.get('invocation_id') != identity or interpreted.binding != binding:
            return {}
        return interpreted.successful_outputs or {}

    def _check_process_inputs(self, binding: dict[str, Any], *, identity: str | None = None,
                              completed_outputs: dict[str, str] | None = None) -> dict[str, str]:
        """Check unchanged inputs and accepted post-versions for in-place outputs."""
        if binding.get('input_snapshot') is not None:
            current = self.process_artifact_binding(binding['request'], identity=identity)
            if current != binding:
                raise ValueError('Approved implementation snapshot changed')
            return current['inputs']
        else:
            current = self._version_hashes(binding['inputs'])
            outputs = (self._completed_process_outputs(binding, identity)
                       if completed_outputs is None else completed_outputs)
            changed = [name for name, digest in binding['inputs'].items()
                if current.get(name) != digest and not (name in binding['outputs']
                    and name in outputs and current.get(name) == outputs[name])]
            if changed:
                raise ValueError('Declared producer inputs/outputs changed: ' + json.dumps({
                    'producer': identity, 'changed_inputs': changed,
                    'declared_outputs': binding['outputs']}, ensure_ascii=False)
                    + '. Regenerate the affected outputs from current inputs in an explicitly approved '
                    'process declaring those outputs; no current publication or automatic rerun')
            return current

    def artifact_sources(self, versions: dict[str, str], *, records: Iterable[dict] = (),
                         cache: dict | None = None) -> dict[str, dict]:
        """Interpret accepted output versions and explicit copies from existing receipts.

        A path and a current hash alone never identify a producer. Each edge is
        taken from its owner before the consuming event; unbound, overwritten,
        or ambiguously ordered versions are ordinary material. The index exists
        only for this read and never upgrades historical receipts.
        """
        from simple_ar.core.capabilities import ArtifactStore
        from simple_ar.tools.professional_outcomes import accepted_native_result

        cache = {} if cache is None else cache
        session_id = ArtifactStore(self.root).read_json('session_manifest.json')['session_id']
        extra = list(records)
        key = tuple((row.get('attempt'), row.get('record'), row.get('record_sha256'), row.get('action_sha256'))
                    for row in extra if isinstance(row, dict))
        processes = self.root / 'tools' / 'processes'

        def process_snapshot():
            if not processes.is_dir():
                return [], ()
            rows, stamp = [], []
            for receipt, record, binding, versions_, successful in self._artifact_producer_records(
                    processes, snapshot=stamp):
                rows.append((receipt, record, binding, versions_, successful))
            return rows, tuple(sorted(stamp))

        try:
            process_rows, process_stamp = process_snapshot()
            inventory_uncertain = False
        except (ValueError, OSError, KeyError, TypeError):
            process_rows, process_stamp, inventory_uncertain = [], None, True
        cached = cache.get(('artifact_sources', key))
        if cached is not None and (inventory_uncertain or cached['process_stamp'] != process_stamp):
            cached = None
        if cached is None:
            store = ArtifactStore(self.root)
            events, seen = [], set()
            journal_path = self.root / 'actions.json'
            journal_raw = journal_path.read_bytes() if journal_path.is_file() else None
            journal_digest = hashlib.sha256(journal_raw).hexdigest() if journal_raw is not None else None

            def add(locator: str, call: dict | None = None, reference: dict | None = None,
                    position: int | None = None) -> None:
                try:
                    path = self.resolve(str(self.root / locator) if locator.startswith('attempts/') else locator)
                    relative = path.relative_to((self.root / 'attempts').resolve())
                    if len(relative.parts) != 2 or relative.name != 'native_result.json' or str(path) in seen:
                        return
                    if reference is not None:
                        action_path = self.root / 'attempts' / reference['attempt'] / 'action.json'
                        if (hashlib.sha256(path.read_bytes()).hexdigest() != reference.get('record_sha256')
                                or hashlib.sha256(action_path.read_bytes()).hexdigest() != reference.get('action_sha256')):
                            return
                    seen.add(str(path))
                    observed = read_json(path)
                    if not isinstance(observed, dict):
                        return
                    if not observed.get('artifacts') and observed.get('tool_name') not in {
                            'write_file', 'edit_file', 'copy_file', 'copy_project', 'run_process',
                            'execute_implementation'}:
                        return
                    identity = relative.parts[0]
                    trusted = True
                    try:
                        accepted_native_result(self.root, session_id, observed, identity,
                                               require_content_binding=True)
                    except (ValueError, OSError, KeyError, TypeError, RuntimeError):
                        trusted = False
                    action_path = path.parent / 'action.json'
                    action = read_json(action_path) if action_path.is_file() else {}
                    original_action, original_identity, recovery_ids = action, identity, set()
                    while isinstance(original_action, dict) and original_action.get('recovered_from'):
                        previous = original_action['recovered_from']
                        if (not isinstance(previous, str) or previous in recovery_ids
                                or original_identity != previous + '-receipt'):
                            raise ValueError('Unproved recovered action')
                        recovery_ids.add(previous)
                        original_identity = previous
                        original_action = read_json(self.root / 'attempts' / previous / 'action.json')
                    if call is None:
                        call = action.get('call')
                    arguments = json.loads((call or {}).get('function', {}).get('arguments', '{}'))
                    if not isinstance(arguments, dict):
                        arguments = {}
                    data, metadata = observed.get('data') or {}, observed.get('metadata') or {}
                    name = observed.get('tool_name')
                    # Acceptance preserves the actual outcome, not success.
                    # A known conflict did not write; other failed operations
                    # may still have published declared artifacts.
                    if observed.get('status') != 'ok':
                        if data.get('conflict'):
                            return
                        trusted = False
                    outputs, inputs, links, previous = {}, {}, {}, {}

                    def output(name: str, digest: Any) -> None:
                        if isinstance(name, str):
                            try:
                                outputs[self.resolve(name)] = digest if isinstance(digest, str) and re.fullmatch(
                                    r'[0-9a-f]{64}', digest) else None
                            except (ValueError, OSError):
                                pass

                    # An unproved artifact is a barrier, not a producer.
                    for artifact in observed.get('artifacts') or []:
                        output(artifact, None)
                    if name == 'run_process' and isinstance(arguments.get('outputs'), list):
                        for declared in arguments['outputs']:
                            output(str(self.resolve_process_artifact(declared,
                                arguments.get('cwd', 'workspace'), write=True)), None)
                    hashes = data.get('artifact_sha256')
                    if trusted and isinstance(hashes, dict) and name not in {'finish_professional_work', 'return_source_work'}:
                        for name_, digest in hashes.items():
                            try:
                                if self.resolve(name_) in outputs:
                                    output(name_, digest)
                            except (ValueError, OSError, TypeError):
                                continue
                    if trusted and name in {'write_file', 'edit_file', 'copy_file'}:
                        output(data.get('absolute_path'), data.get('sha256'))
                        model = _ContextEdit if name == 'edit_file' else _TOOLS[name][0]
                        admitted = model.model_validate(arguments)
                        before = data.get('previous_sha256', admitted.expected_sha256)
                        target = self.resolve(admitted.path)
                        if before:
                            previous[target] = before
                        if before == data.get('sha256'):
                            links[target] = (target, before)
                    if trusted and name == 'copy_file' and data.get('source_sha256') == data.get('sha256'):
                        target, source = self.resolve(data['absolute_path']), self.resolve(data['source'])
                        inputs[source] = data['source_sha256']
                        links[target] = (source, data['source_sha256'])
                    if trusted and name == 'work_document':
                        from simple_ar.tools.document_work import DocumentWorkToolGateway
                        try:
                            copies = [(self.resolve(row['path']), self.resolve(row['source']), row['source_sha256'])
                                for row in DocumentWorkToolGateway.artifact_copies(observed)]
                            if (len({target for target, _, _ in copies}) != len(copies)
                                    or any(target == source or outputs.get(target) != digest
                                        for target, source, digest in copies)):
                                raise ValueError('Document copy targets differ from registered output versions')
                            for target, source, digest in copies:
                                if source in inputs and inputs[source] != digest:
                                    raise ValueError('Document copy source has inconsistent versions')
                                inputs[source] = digest
                                links[target] = (source, digest)
                        except (ValueError, OSError, KeyError, TypeError):
                            # A malformed accepted writer remains a version
                            # barrier; never fall through to an earlier owner.
                            trusted, inputs, links = False, {}, {}
                    if trusted and name == 'copy_project' and isinstance(data.get('files'), dict):
                        target, source = self.resolve(data['absolute_path']), self.resolve(data['source'])
                        if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                            trusted = False  # The archive/member distinction was not retained.
                        for member, digest in data['files'].items():
                            child = self.resolve(str(target / member))
                            if not child.is_relative_to(target):
                                continue
                            output(str(child), digest if trusted else None)
                            if trusted and not data.get('retained_delivery') and member != arguments.get('archive_name'):
                                original = self.resolve(str(source / member))
                                if original.is_relative_to(source):
                                    inputs[original] = digest
                                    links[child] = (original, digest)
                    binding = metadata.get('inspection_binding') or {}
                    if trusted and isinstance(binding, dict) and isinstance(data.get('record'), str):
                        output(data['record'], binding.get('assessment_sha256'))
                    source_inputs = data.get('input_sha256')
                    if trusted and isinstance(source_inputs, dict):
                        declared_inputs = {self.resolve(path_): digest for path_, digest in source_inputs.items()
                                  if isinstance(path_, str) and isinstance(digest, str)
                                  and re.fullmatch(r'[0-9a-f]{64}', digest)}
                        if (len(declared_inputs) != len(source_inputs)
                                or any(path_ in inputs and inputs[path_] != digest
                                       for path_, digest in declared_inputs.items())):
                            trusted = False
                        inputs.update(declared_inputs)
                    if trusted and name == 'inspect_artifacts' and binding.get('assessment_sha256'):
                        assessments = metadata.get('assessments')
                        if assessments is None:
                            assessments = [metadata.get('delivery_assessment') or metadata.get('assessment')]
                        for assessment in assessments if isinstance(assessments, list) else []:
                            if not isinstance(assessment, dict):
                                continue
                            for field in ('subjects', 'artifacts', 'evidence_sha256'):
                                for name_, digest in (assessment.get(field) or {}).items():
                                    source_path = self.resolve(name_)
                                    if source_path in inputs and inputs[source_path] != digest:
                                        trusted = False
                                    inputs[source_path] = digest
                    process_record = None
                    if name in {'run_process', 'execute_implementation'} and isinstance(data.get('receipt'), str):
                        receipt = self.root / data['receipt']
                        expected_dir = self.root / 'tools' / 'processes'
                        if (receipt.parent.parent == expected_dir and receipt.name == 'invocation.json'
                                and receipt.parent.name == original_identity):
                            try:
                                raw_receipt = receipt.read_bytes()
                                process_record = json.loads(raw_receipt.decode('utf-8'))
                                interpreted = _process_artifact_record(process_record)
                            except (ValueError, OSError, TypeError):
                                process_record = None
                                interpreted = None
                            if (process_record is not None and process_record.get('invocation_id') == receipt.parent.name
                                    and interpreted.binding is not None):
                                # A failed or incomplete declared writer is an
                                # unknown version boundary, even when the old
                                # bytes happen to remain on disk.
                                bound = (trusted and isinstance(data.get('receipt_sha256'), str)
                                    and hashlib.sha256(raw_receipt).hexdigest() == data['receipt_sha256'])
                                versions = (interpreted.recorded_outputs or {}) if bound else {}
                                for name_ in interpreted.binding['outputs']:
                                    output(name_, versions.get(name_))
                                inputs = {self.resolve(name_): digest for name_, digest in
                                          interpreted.binding['inputs'].items()}
                                trusted = bound and interpreted.recorded_outputs is not None
                    if outputs:
                        attempt = ArtifactStore(self.root / 'attempts' / original_identity).read_attempt_manifest()
                        events.append({'record': str(path), 'observed': observed, 'trusted': trusted,
                            'outputs': outputs, 'inputs': inputs, 'links': links, 'previous': previous,
                            'tool_name': name, 'process': process_record, 'reference': reference,
                            'position': position, 'start': attempt.created_at, 'end': attempt.updated_at})
                except (ValueError, OSError, KeyError, TypeError, RuntimeError):
                    return  # Inaccessible unrelated history does not acquire ownership.

            try:
                journal = json.loads(journal_raw.decode('utf-8')) if journal_raw is not None else {}
                journal_position = 0
                for turn in journal.get('turns', []):
                    try:
                        reply = store.read_json(turn['reply'])
                        calls = {row['id']: row for row in reply.get('tool_calls', [])}
                    except (ValueError, OSError, KeyError, TypeError):
                        calls = {}
                    for item in turn.get('tools', []):
                        journal_position += 1
                        locator = item.get('result')
                        if not isinstance(locator, str):
                            continue
                        # Child actions are ordered by the parent's accepted
                        # inspection journal, before its final observation.
                        try:
                            parent = store.read_json(locator)
                            nested = (parent.get('metadata') or {}).get('delivery_review', {}).get('result', parent)
                            review_locator = item.get('review_result')
                            if isinstance(review_locator, str):
                                review_path = self.resolve(str(self.root / review_locator)
                                    if review_locator.startswith('attempts/') else review_locator)
                                review = read_json(review_path)
                                accepted_native_result(self.root, session_id, review, review_path.parent.name,
                                                       require_content_binding=True)
                                review_action = read_json(review_path.parent / 'action.json')
                                review_parts = Path((review.get('data') or {}).get('record', '')).parts
                                if (len(review_parts) < 4 or review_parts[0] != 'attempts'
                                        or review_parts[2] != 'inspection'
                                        or (review_action.get('origin') or {}).get('scope') != review_parts[1]):
                                    raise ValueError('Separate inspection action has a different scope')
                                nested = review
                            artifact = (nested.get('data') or {}).get('record', '')
                            parts = Path(artifact).parts
                            binding = (nested.get('metadata') or {}).get('inspection_binding') or {}
                            if len(parts) >= 4 and parts[0] == 'attempts' and parts[2] == 'inspection':
                                input_path = self.root / 'tools' / 'inspection' / parts[1] / 'input.json'
                                raw = input_path.read_bytes()
                                if hashlib.sha256(raw).hexdigest() == binding.get('input_sha256'):
                                    for child in json.loads(raw.decode('utf-8')).get('source_invocations', []):
                                        if isinstance(child, dict) and isinstance(child.get('record'), str):
                                            add(child['record'], reference=child)
                        except (ValueError, OSError, KeyError, TypeError, AttributeError):
                            pass
                        add(locator, calls.get(item.get('id')), position=journal_position)
            except (ValueError, OSError, KeyError, TypeError):
                pass
            for row in extra:
                if isinstance(row, dict) and isinstance(row.get('record'), str):
                    add(row['record'], reference=row)
            # Operations without an accepted native result may still have
            # touched declared outputs. Include them as unknown boundaries,
            # rather than making them disappear from the version history.
            known = {row['process']['invocation_id'] for row in events if row['process'] is not None}
            if not inventory_uncertain:
                try:
                    for receipt, record, binding, _, _ in process_rows:
                        if record['invocation_id'] in known:
                            continue
                        outputs = {self.resolve(name): None for name in binding['outputs']}
                        if outputs:
                            events.append({'record': str(receipt), 'observed': None, 'trusted': False,
                                'outputs': outputs, 'inputs': {}, 'links': {}, 'tool_name': 'run_process',
                                'process': record, 'reference': None, 'position': None,
                                'start': record.get('started_at'), 'end': record.get('finished_at')})
                except (ValueError, OSError, KeyError, TypeError):
                    inventory_uncertain = True
            cached = {'events': events, 'journal_sha256': journal_digest,
                      'inventory_uncertain': inventory_uncertain, 'process_stamp': process_stamp}
            cache[('artifact_sources', key)] = cached

        def verify_source(event: dict) -> None:
            # Revalidate only owners actually reached; unrelated damaged
            # receipts must not block the selected material.
            reference = event['reference']
            if reference is not None:
                action_path = self.root / 'attempts' / reference['attempt'] / 'action.json'
                if (hashlib.sha256(Path(event['record']).read_bytes()).hexdigest() != reference['record_sha256']
                        or hashlib.sha256(action_path.read_bytes()).hexdigest() != reference['action_sha256']):
                    raise ValueError('Accepted child receipt changed during provenance lookup')
            if event['process'] is not None:
                process = event['process']
                receipt = self.root / 'tools' / 'processes' / process['invocation_id'] / 'invocation.json'
                raw = receipt.read_bytes()
                path = Path(event['record'])
                accepted_native_result(self.root, session_id, event['observed'], path.parent.name,
                                       require_content_binding=True)
                if (json.loads(raw.decode('utf-8')) != process or hashlib.sha256(raw).hexdigest()
                        != event['observed'].get('data', {}).get('receipt_sha256')):
                    raise ValueError('Process owner receipt changed during provenance lookup')
            else:
                path = Path(event['record'])
                accepted_native_result(self.root, session_id, event['observed'], path.parent.name,
                                       require_content_binding=True)

        history = [{'position': event['position'], 'start': event['start'], 'end': event['end'],
                    'outputs': event['outputs'], 'inputs': event['inputs'],
                    'previous': event.get('previous', {}),
                    'links': {path: [link] for path, link in event['links'].items()}, 'source': event}
                   for event in cached['events']]
        resolved = {}
        for name, digest in versions.items():
            path = self.resolve(name)
            if path in resolved and resolved[path] != digest:
                raise ValueError('Conflicting versions of one selected artifact')
            resolved[path] = digest
        sources = ({path: {'status': 'unknown', 'dependencies': {}} for path in resolved}
                   if cached['inventory_uncertain'] else
                   self._check_retained_producers(resolved, history, provenance=True, verify_source=verify_source))
        journal_path = self.root / 'actions.json'
        current = hashlib.sha256(journal_path.read_bytes()).hexdigest() if journal_path.is_file() else None
        if current != cached['journal_sha256']:
            raise ValueError('Operation journal changed during provenance lookup')
        if not cached['inventory_uncertain'] and process_snapshot()[1] != cached['process_stamp']:
            raise ValueError('Process inventory changed during provenance lookup')
        return {name: sources[self.resolve(name)] for name in versions}

    def artifact_dependencies(self, sources: dict[str, dict], *,
                              stop_at: frozenset[Path] = frozenset()) -> dict[str, str]:
        """Project consumed inputs from version-bound lineage, never display summaries.

        Copies preserve the original observation boundary; replacing their old
        source path is not a change to the copy. An edit's prior bytes are an
        authoring precondition, not a computational input of the new text.
        A consumer may validate a supplied artifact as a versioned input
        without requiring its historical build inputs to remain current.
        """
        dependencies = {}
        for source in sources.values():
            graph = source.get('lineage')
            if graph is None:
                continue  # Unrecorded material has no invented process ancestry.
            nodes = {node['id']: node for node in graph['nodes']}
            pending, visited = [graph['root']], set()
            while pending:
                identity = pending.pop()
                if identity in visited:
                    continue
                visited.add(identity)
                node = nodes[identity]
                if node['state'] != 'recorded':
                    continue  # A diagnostic file is readable, not producer proof.
                for edge in node['edges']:
                    if edge['kind'] == 'previous':
                        continue
                    upstream = nodes[edge['node']]
                    if edge['kind'] == 'input':
                        path, digest = str(upstream['path']), upstream['sha256']
                        producer_outputs = (node.get('producer') or {}).get('outputs', {})
                        overwritten = any(self.resolve(name) == upstream['path'] and version != digest
                                          for name, version in producer_outputs.items())
                        if not overwritten:
                            if path in dependencies and dependencies[path] != digest:
                                raise ValueError('Selected artifacts consume conflicting historical input versions: ' + path)
                            dependencies[path] = digest
                    if edge['kind'] != 'input' or upstream['path'] not in stop_at:
                        pending.append(edge['node'])
        return dependencies

    def check_artifact_producers(self, locators: Iterable[str], *, cache: dict | None = None,
                                 refresh: bool = False,
                                 allow_authored_replacement: bool = True,
                                 retained_versions: dict[Path, str] | None = None,
                                 retained_history: list[dict] | None = None,
                                 producer_index_cache: dict | None = None,
                                 retained_reader: RetainedDelivery | None = None) -> dict | None:
        """Reject known stale declared edges; untracked files remain unverified.

        Inspect only existing process-owner receipts, never workspace contents.
        The latest declared producer owns each output, including interrupted or
        failed runs. A later successful explicit run can supersede an older one.
        Each requested output must match its owner; process success and complete
        output binding still apply to the whole invocation.
        Explicit later edits are new work, not proof that a process produced them;
        older delivery contracts may retain their original path-owner gate.
        Retained delivery instead follows version edges at their record boundary;
        it never reduces past ownership to the latest path owner.
        """
        if retained_versions is not None:
            return self._check_retained_producers(retained_versions, retained_history or [],
                reader=retained_reader)
        directory = self.root / 'tools' / 'processes'
        if not directory.is_dir():
            if producer_index_cache is not None:
                producer_index_cache.setdefault('producers', {})
                producer_index_cache.setdefault('observed', set()).update(
                    resolve_mounted_path(self.mounts, name, discovery=True) for name in locators)
            return
        if cache is None:
            cache = {}
        if refresh:
            cache.clear()
            if producer_index_cache is not None:
                producer_index_cache.clear()
        producers = cache.get('producers')
        if producer_index_cache is not None and 'producers' in producer_index_cache:
            shared = producer_index_cache['producers']
            if producers is not None and producers is not shared and producers != shared:
                raise ValueError('Producer indexes differ during validation')
            producers = shared
        if producers is None:
            producers = self._artifact_producers(directory)
        cache['producers'] = producers
        if producer_index_cache is not None:
            producer_index_cache.setdefault('producers', producers)
        visited = cache.setdefault('checked', set())
        # Publish newly checked paths only after the whole call succeeds. A
        # later root failure must not leave earlier roots or dependencies cached.
        checked_paths = set()
        visiting = set()
        # Share receipt/input validation within this call, by object identity
        # rather than invocation_id. Output bytes are checked per owned path:
        # a sibling may have been superseded or removed since this receipt.
        checked_records = set()

        def authored_after_process(path: Path) -> bool:
            # Use the existing journal's versioned owner, not a process-only
            # inventory, when a later actual edit replaced a failed/stale output.
            if 'records' not in self.mounts or not (self.root / 'actions.json').is_file():
                return False
            name = self.locator(str(path))
            source = self.artifact_sources(self._version_hashes([name]), cache=cache)[name]
            graph = source.get('lineage') or {}
            node = next((item for item in graph.get('nodes', []) if item['id'] == graph.get('root')), {})
            return (node.get('state') == 'recorded'
                and (node.get('owner') or {}).get('tool_name') in {'write_file', 'edit_file'}
                and not any(edge['kind'] != 'previous' for edge in node.get('edges', [])))

        def check(path: Path) -> None:
            if producer_index_cache is not None:
                producer_index_cache.setdefault('observed', set()).add(path)
            if (path in visited or path in checked_paths or path in visiting or path not in producers
                    or allow_authored_replacement and path in cache.get('superseded', ())):
                return
            try:
                check_owner(path)
            except ValueError:
                if not allow_authored_replacement or not authored_after_process(path):
                    raise
                cache.setdefault('superseded', set()).add(path)

        def check_owner(path: Path) -> None:
            _, record, binding = producers[path]
            if record is None:
                raise ValueError(f'Ambiguous producer timestamps for {path.name}; no current publication')
            owner = (id(record), id(binding))
            if record.get('output_binding_error'):
                raise ValueError(f"Declared producer has no successful output binding for {path.name}: {record['output_binding_error']}; no replay")
            outputs = _process_artifact_record(record).successful_outputs
            if outputs is None:
                raise ValueError(f'Declared producer has no successful output binding: {path.name}; inspect receipt, no replay')
            if owner not in checked_records:
                self._check_process_inputs(binding, identity=record['invocation_id'])
            owned_outputs = {name: digest for name, digest in outputs.items()
                             if resolve_mounted_path(self.mounts, name, discovery=True) == path}
            if not owned_outputs:
                raise ValueError(f'Declared producer ownership changed for {path.name}')
            current = self._version_hashes(owned_outputs)
            changed = [name for name, digest in owned_outputs.items() if current.get(name) != digest]
            if changed:
                raise ValueError('Declared producer outputs changed: ' + json.dumps({
                    'producer': record['invocation_id'], 'changed_outputs': changed,
                    'declared_inputs': list(binding['inputs']), 'declared_outputs': binding['outputs']}, ensure_ascii=False)
                    + '; no current publication or automatic rerun')
            if owner in checked_records:
                checked_paths.add(path)
                return
            visiting.add(path)
            try:
                for locator in binding['inputs']:
                    check(resolve_mounted_path(self.mounts, locator, discovery=True))
            finally:
                visiting.remove(path)
            checked_records.add(owner)
            checked_paths.add(path)

        for locator in locators:
            check(resolve_mounted_path(self.mounts, locator, discovery=True))
        visited.update(checked_paths)

    def verify_artifact_producer_index(self, producer_index_cache: dict) -> None:
        """Reject ownership changes during one read-only dependency validation."""
        previous = producer_index_cache.get('producers')
        if previous is None:
            return
        directory = self.root / 'tools' / 'processes'
        current = self._artifact_producers(directory) if directory.is_dir() else {}
        if any(previous.get(path) != current.get(path)
               for path in producer_index_cache.get('observed', ())):
            raise ValueError('Producer ownership changed during validation; inspect current receipts')
        # The index can stay identical while a consumed input or output changes.
        # Recheck the observed closure with a fresh walk, including snapshots.
        self.check_artifact_producers(
            (str(path) for path in producer_index_cache.get('observed', ())),
            cache={'producers': current})

    def artifact_provenance(self, locators: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Resolve existing process ownership, not inferred file relationships.

        This is a read-only projection of the same receipts used for freshness
        checks. Untracked inputs remain untracked; content review cannot turn
        them into executed or verified results.
        """
        names = list(dict.fromkeys(locators))
        hashes = self.artifact_hashes(names)
        cache: dict[str, Any] = {}
        self.check_artifact_producers(names, cache=cache)
        producers = cache.get('producers', {})
        result = {}
        for name in names:
            row: dict[str, Any] = {'path': name, 'sha256': hashes[name], 'producer': None}
            path = self.resolve(name)
            entry = None if path in cache.get('superseded', ()) else producers.get(path)
            if entry is not None:
                _, record, binding = entry
                identity = record['invocation_id']
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
                    raise ValueError('Invalid registered producer identity')
                receipt = self.root / 'tools' / 'processes' / identity / 'invocation.json'
                if read_json(receipt) != record:
                    raise ValueError('Producer changed during provenance inspection')
                outputs = _process_artifact_record(record).successful_outputs
                if outputs is None:
                    raise ValueError('Producer has no successful recorded output binding')
                row['producer'] = {
                    'invocation_id': identity,
                    'receipt': str(receipt.relative_to(self.root)),
                    'receipt_sha256': hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    'inputs': dict(binding['inputs']),
                    'outputs': dict(outputs),
                }
            result[name] = row
        return result

    def _check_retained_producers(self, versions: dict[Path, str], history: list[dict], *,
                                  provenance: bool = False, verify_source: Any = None,
                                  reader: RetainedDelivery | None = None) -> dict:
        """Traverse a temporary version graph made only from existing receipts.

        A node is a path/version at a record boundary. Copies link equal bytes
        across paths; real modifications create new, unverified nodes. Process
        ownership is chosen before that boundary, never from today's path owner.
        Missing/overlapping timing cannot prove ownership and stays unknown.

        Provenance results retain the source view and add lineage.root/nodes.
        Node IDs are local to this lookup; each node binds path, sha256 and a
        consuming-event boundary. Its state describes its own owner association;
        ancestry_states preserves upstream failures/ambiguity separately from
        ordinary untracked material. Flat dependencies are compatibility only.
        """
        from datetime import datetime, timedelta

        def instant(value):
            try:
                parsed = datetime.fromisoformat(value)
                return parsed if parsed.tzinfo is not None else None
            except (TypeError, ValueError):
                return None

        positions = {}
        for row in history:
            if 'process_ref' in row:
                positions.setdefault(row['process_ref'], row['position'])
        events = [dict(row) for row in history if 'process_ref' not in row]
        records = {}
        directory = self.root / 'tools' / 'processes'
        if directory.is_dir() and not provenance:
            producer_records = (self._artifact_producer_records(directory) if reader is None else
                                self._artifact_producer_records(directory, reader=reader))
            for receipt, record, binding, output_versions, successful in producer_records:
                ref = receipt.relative_to(self.root).as_posix()
                records[ref] = record
                inputs = self.resolved_versions(binding['inputs']) if reader is None else {}
                if reader is not None:
                    for locator, digest in binding['inputs'].items():
                        path = reader.resolve(locator)
                        if path in inputs and inputs[path] != digest:
                            raise ValueError('Conflicting hashes for aliases of the same artifact')
                        inputs[path] = digest
                events.append({'position': positions.get(ref), 'start': record.get('started_at'),
                    'end': record.get('finished_at'), 'process': record, 'binding': binding,
                    'successful_outputs': successful,
                    'outputs': {resolve_mounted_path(self.mounts, name, write=True, discovery=True,
                        authorize=reader.admit if reader is not None else None): output_versions.get(name)
                                for name in binding['outputs']},
                    'inputs': inputs})
        outputs, inputs = {}, {}
        for event in events:
            event['start'], event['end'] = instant(event.get('start')), instant(event.get('end'))
            for path in event['outputs']:
                outputs.setdefault(path, []).append(event)
            for path in event['inputs']:
                inputs.setdefault(path, []).append(event)

        def ended_before(end, start):
            if end is None or start is None:
                return False
            # Attempt records can have only second precision while process
            # timestamps have microseconds. Treat the end of that second as
            # the upper bound instead of inventing a definite order.
            uncertainty = timedelta(seconds=1) if end.microsecond == 0 else timedelta(microseconds=1)
            return end + uncertainty <= start

        def precedes(left, right):
            if right is None:
                return True
            if left.get('position') is not None and right.get('position') is not None:
                return left['position'] < right['position']
            if provenance:
                return ended_before(left['end'], right['start'])
            return (left['end'] is not None and right['start'] is not None
                    and left['end'] < right['start'])

        def candidate_before(left, right):
            if not provenance or right is None or (left.get('position') is not None
                                                    and right.get('position') is not None):
                return precedes(left, right)
            # For an unjournaled process, an overlap or missing timestamp is
            # ambiguous and must compete with other owners. Only a process
            # definitely started after this boundary can be excluded.
            return not ended_before(right['end'], left['start'])

        def latest(candidates):
            maximal = [row for row in candidates
                       if not any(other is not row and precedes(row, other) for other in candidates)]
            return maximal[0] if len(maximal) == 1 else None

        verified = set()

        def verify(event):
            if id(event) in verified:
                return
            if provenance:
                verify_source(event['source'])
                verified.add(id(event))
                return
            if event.get('consistent') is False:
                raise ValueError('Retained version differs from its recorded owner')
            if 'record' in event:
                if reader is not None:
                    actual = reader.record(event['record'])
                else:
                    path = resolve_mounted_path({'workspace': self.root}, event['record'], discovery=True)
                    actual = read_json(path)
                if not event['consistent'] or actual != event['expected_record']:
                    raise ValueError('Delivered project copy has inconsistent ownership')
                records[event['record']] = actual
            verified.add(id(event))

        if provenance:
            # This graph describes recorded versions, not today's files. Keep
            # owner association separate from the state of its ancestry; an
            # ordinary input and a failed declared writer are different facts.
            event_ids = {id(event): index for index, event in enumerate(events)}
            nodes, node_ids, active = [], {}, set()

            def lineage(path, digest, boundary):
                boundary_id = event_ids[id(boundary)] if boundary is not None else None
                key = (path, digest, boundary_id)
                if key in node_ids:
                    if key in active:
                        raise ValueError('Recorded artifact lineage contains a dependency cycle')
                    return node_ids[key]
                number = len(nodes)
                node_ids[key] = number
                node = {'id': number, 'path': path, 'sha256': digest,
                        'boundary': boundary_id, 'state': 'ordinary',
                        'owner': None, 'producer': None, 'edges': [], 'ancestry_states': []}
                nodes.append(node)
                candidates = [row for row in outputs.get(path, [])
                              if row is not boundary and candidate_before(row, boundary)]
                event = latest(candidates)
                if event is None:
                    if candidates:
                        node['state'] = 'ambiguous'
                    return number
                owner = event['source']
                node['owner'] = {'event': event_ids[id(event)], 'record': owner['record'],
                                 'tool_name': owner['tool_name'], 'trusted': owner['trusted']}
                process = owner.get('process')
                interpreted = _process_artifact_record(process) if process is not None else None
                if interpreted is not None:
                    identity = process['invocation_id']
                    node['producer'] = {'invocation_id': identity,
                        'receipt': f'tools/processes/{identity}/invocation.json',
                        'receipt_sha256': ((owner.get('observed') or {}).get('data') or {}).get('receipt_sha256'),
                        'inputs': dict((interpreted.binding or {}).get('inputs', {})),
                        'outputs': dict(interpreted.output_versions)}
                if boundary is not None and not precedes(event, boundary):
                    node['state'] = 'ambiguous'
                elif (interpreted is not None and interpreted.binding is not None
                      and interpreted.recorded_outputs is None):
                    # Failed/incomplete declared writes remain visible even when
                    # they left the previous bytes in place. Never walk behind one.
                    node['state'] = 'failed'
                elif not owner['trusted']:
                    node['state'] = 'unbound'
                elif event['outputs'][path] != digest:
                    node['state'] = 'mismatch'
                else:
                    verify(event)
                    node['state'] = 'recorded'
                    active.add(key)
                    try:
                        links = event.get('links', {}).get(path, [])
                        for source_path, expected in links:
                            node['edges'].append({'kind': 'copy',
                                'node': lineage(source_path, expected, event)})
                        copied_inputs = ({link for group in event.get('links', {}).values() for link in group}
                                         if links else set())
                        for source_path, expected in event['inputs'].items():
                            if (source_path, expected) not in copied_inputs:
                                node['edges'].append({'kind': 'input',
                                    'node': lineage(source_path, expected, event)})
                        previous = event.get('previous', {})
                        if path in previous and (path, previous[path]) not in links:
                            node['edges'].append({'kind': 'previous',
                                'node': lineage(path, previous[path], event)})
                    finally:
                        active.remove(key)
                    states = set()
                    for edge in node['edges']:
                        if edge['kind'] == 'previous':
                            continue  # Superseded bytes are diagnostic, not consumed inputs.
                        upstream = nodes[edge['node']]
                        states.add(upstream['state'])
                        states.update(upstream['ancestry_states'])
                    node['ancestry_states'] = sorted(states)
                return number

            projected = {}

            def source_view(number):
                # Compatibility display, not validation authority. Lineage keeps
                # every version; a flat map cannot represent conflicting ones.
                if number in projected:
                    return projected[number]
                node = nodes[number]
                if node['state'] != 'recorded':
                    result = {'status': 'unknown', 'dependencies': {}}
                else:
                    copies = [edge for edge in node['edges'] if edge['kind'] == 'copy']
                    if copies:
                        source = source_view(copies[0]['node'])
                        result = ({**source, 'copy_record': node['owner']['record']}
                                  if source['status'] != 'unknown' else dict(source))
                    else:
                        versions = {}
                        for edge in node['edges']:
                            if edge['kind'] != 'input':
                                continue
                            upstream = nodes[edge['node']]
                            versions.setdefault(upstream['path'], set()).add(upstream['sha256'])
                            source = source_view(edge['node'])
                            for name, value in source['dependencies'].items():
                                versions.setdefault(name, set()).add(value)
                            for name, values in source.get('dependency_conflicts', {}).items():
                                versions.setdefault(name, set()).update(values)
                        result = {'status': 'recorded', 'record': node['owner']['record'],
                                  'tool_name': node['owner']['tool_name'],
                                  'dependencies': {name: next(iter(values)) for name, values in versions.items()
                                                   if len(values) == 1}}
                        conflicts = {name: sorted(values) for name, values in versions.items() if len(values) > 1}
                        if conflicts:
                            result['dependency_conflicts'] = conflicts
                projected[number] = result
                return result

            roots = {path: lineage(path, digest, None) for path, digest in versions.items()}
            found = {}
            for path, number in roots.items():
                reachable, pending = set(), [number]
                while pending:
                    current = pending.pop()
                    if current not in reachable:
                        reachable.add(current)
                        pending.extend(edge['node'] for edge in nodes[current]['edges'])
                found[path] = {**source_view(number), 'lineage': {'root': number,
                    'nodes': [nodes[index] for index in sorted(reachable)]}}
            return found

        # Each copied inventory supplies one immutable context for its sibling
        # members. Keep later observations when revisiting the same source path.
        contexts = {0: dict(versions)}
        projections = {}

        def project(event, context, rewound=None):
            key = (id(event), context, rewound)
            if key not in projections:
                projected = len(contexts)
                contexts[projected] = {**event['inputs'], **contexts[context]}
                if rewound is not None:
                    contexts[projected][rewound] = event['inputs'][rewound]
                projections[key] = projected
            return projections[key]

        checked = {}

        def check(path, digest, boundary, context):
            candidates = [row for row in outputs.get(path, []) if candidate_before(row, boundary)]
            event = latest(candidates)
            if event is None:
                return None
            key = (path, digest, id(boundary), id(event), context)
            if key in checked:
                return checked[key]
            verify(event)
            if 'process' in event and event['successful_outputs'] is None:
                # A failed/incomplete writer is a boundary even if its raw
                # recorded bytes differ. Never bypass it via an earlier anchor.
                raise ValueError(f'Declared producer has no successful output binding: {path.name}; inspect receipt, no replay')
            owned = event['outputs'][path]
            links = event.get('links', {}).get(path, [])
            if ((owned is not None and owned != digest)
                    or 'process' not in event and not links):
                # A different version or reintroduced content cannot erase an
                # already observed byte version. Without an earlier observation
                # or explicit alias edge, newly authored bytes remain unknown.
                anchor = latest([row for row in inputs.get(path, [])
                                 if row['inputs'][path] == digest and precedes(row, event)])
                if anchor is not None:
                    verify(anchor)
                    check(path, digest, anchor, context)
                result = None
            elif 'process' not in event:
                for link in links:
                    check(*link, event, project(event, context))
                # No link means newly created bytes, not verified computation.
                result = None
            else:
                for source, expected in event['inputs'].items():
                    observed = contexts[context].get(source)
                    overwritten = (source in event['outputs'] and observed is not None
                                   and observed == event['outputs'][source])
                    if observed is not None and observed != expected and not overwritten:
                        raise ValueError('Declared producer inputs/outputs changed: ' + str(source)
                                         + '; no current publication or automatic rerun')
                    check(source, expected, event, project(event, context, source) if overwritten else context)
                result = None
            # Retained checks are per output/version/boundary/context. A process
            # with another unrelated output must not short-circuit this node.
            checked[key] = result
            return result

        for path, digest in versions.items():
            check(path, digest, None, 0)
        return records

    def _artifact_producers(self, directory: Path) -> dict:
        producers = {}
        for _, record, binding, _, _ in self._artifact_producer_records(directory):
            order = record.get('started_at', '')
            for locator in binding['outputs']:
                path = resolve_mounted_path(self.mounts, locator, write=True, discovery=True)
                if path not in producers or order > producers[path][0]:
                    producers[path] = (order, record, binding)
                elif order == producers[path][0]:
                    # Wall-clock precision is not a causal ordering guarantee.
                    # Never let an invocation name break a provenance tie.
                    producers[path] = (order, None, None)
        return producers

    def _artifact_producer_records(self, directory: Path, *, reader: RetainedDelivery | None = None,
                                   snapshot: list | None = None
                                   ) -> Iterable[tuple[Path, dict, dict, dict, dict | None]]:
        """One bounded reader for current owners and retained version history."""
        total = 0
        if reader is not None:
            reader.admit(directory)
        for count, entry in enumerate(directory.iterdir(), 1):
            if count > 4096:
                raise ValueError('Process receipt inventory exceeds 4096 entries')
            if entry.is_symlink() or entry.is_junction():
                raise ValueError('Process receipt directory cannot be a link')
            if reader is not None:
                reader.admit(entry)
            receipt = entry / 'invocation.json'
            if not receipt.is_file():
                continue
            if receipt.is_symlink():
                raise ValueError('Process receipt cannot be a link')
            if reader is not None:
                reader.admit(receipt)
            total += receipt.stat().st_size
            if total > _FULL_DIGEST_MAX_BYTES:
                raise ValueError('Process receipt inventory exceeds 64 MiB')
            raw = receipt.read_bytes() if snapshot is not None else None
            record = (reader.record(receipt.relative_to(reader.root).as_posix())
                      if reader is not None else json.loads(raw.decode('utf-8')) if raw is not None
                      else read_json(receipt))
            interpreted = _process_artifact_record(record)
            if interpreted.binding is None:
                continue  # Never invent historical dependency coverage.
            if snapshot is not None:
                snapshot.append((str(receipt), hashlib.sha256(raw).hexdigest()))
            yield (receipt, record, interpreted.binding,
                   interpreted.output_versions, interpreted.successful_outputs)

    def bind_inventory(self, project: Path, *, identity: str,
                       material_candidates: bool | Literal['implementation'] = False) -> dict:
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

    def call(self, name: str, arguments: dict[str, Any], *, identity: str) -> ToolResult:
        try:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", identity):
                raise ValueError("Invalid durable tool invocation identity")
            model = validate_workspace_arguments(name, arguments,
                candidate_review_protocol=self.candidate_review_protocol)
            metadata = {}
            if name == "compile_python":
                target = self.resolve(model.path, write=True)
                with target.open('rb') as handle:
                    raw = handle.read(256001)
                if len(raw) > 256000:
                    raise ValueError('Python file exceeds the syntax check size limit')
                try:
                    compile(raw, str(target), 'exec', dont_inherit=True)
                except SyntaxError as exc:
                    return ToolResult(tool_name=name, status='error', error=f'Line {exc.lineno}: {exc.msg}')
                return ToolResult(tool_name=name, status='ok', data={'path': model.path})
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
            if name in {"deliver_result", "review_result"}:
                if isinstance(model, CandidateReviewRequest):
                    # Normalize once at the result boundary. The original
                    # candidate request stays in its accepted action receipt;
                    # existing delivery/inspection owners bind these actual bytes.
                    model = DeliveryRequest.model_validate(model.model_dump(exclude={'review_scope'}))
                if isinstance(model, ReviewedDeliveryRequest):
                    data = ({'reviewed_candidate': model.reviewed_candidate} if model.reviewed_candidate
                        else self.resolve_delivery(DeliveryRequest.model_validate(model.model_dump())))
                else:
                    data = self.resolve_delivery(model)
            elif name == "ask_user":
                data = model.model_dump()
            elif name == "run_process":
                recovering = (self.root / "tools" / "processes" / identity / "invocation.json").exists()
                wall = self.ledger.remaining('process_wall_seconds') if self.ledger is not None else None
                calls = self.ledger.remaining('process_invocations') if self.ledger is not None else None
                if (not recovering and (calls is None or calls >= 1)
                        and wall is not None and 1 <= wall < model.timeout_sec):
                    return ToolResult(tool_name=name, status='blocked',
                        error='Process timeout exceeds the remaining wall allowance. Choose a shorter check '
                              'if it still answers the question, or report the missing allowance.',
                        data={'max_timeout_sec': int(wall), 'executed': False})
                data = self._run(model, identity, sandboxed=self.sandbox_execution and not model.host_access)
                if not recovering:
                    receipt = self.root / data['receipt']
                    data['receipt_sha256'] = hashlib.sha256(receipt.read_bytes()).hexdigest()
                if data.get('output_binding_error'):
                    return ToolResult(tool_name=name, status='error', data=data,
                        error=data['output_binding_error'],
                        summary=('Command reported exit 0, but required artifact binding failed. Use stdout and stderr '
                                 'to diagnose the cause. For a shell pipeline, exit 0 may only describe its last command.'))
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
            elif name == "list_project_results":
                data = self._project_results(model)
            elif name == "inspect_project":
                from simple_ar.research.preparation import inspect_project_preparation
                from simple_ar.code_task.review_pipeline import build_review_index
                from simple_ar.core.artifacts import write_json

                target = self.resolve(model.path)
                boundary = self.material_boundary(str(target))
                data_paths = tuple(self.resolve(path) for path in model.data_paths)
                # CodeTask's example-file visibility cannot expand this
                # gateway's grant. Delegate to the existing mount resolver.
                def accessible(path: Path) -> bool:
                    try:
                        self.resolve(str(path))
                    except ValueError:
                        return False
                    return True
                index = build_review_index(target, access_boundary=boundary, path_filter=accessible)
                facts = inspect_project_preparation(target, data_paths=data_paths, index=index,
                                                    access_boundary=boundary)
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
                for key in ("entrypoint_candidates", "declared_entrypoints", "notebook_candidates", "project_skills",
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
                    if target.is_relative_to(self.root / 'tools' / 'processes'):
                        children = [p for p in children if self._diagnostic_visible(p)]
                    selected = children[model.offset:model.offset + model.limit]
                    data = {"entries": [{"name": p.name, "path": str(p), "directory": p.is_dir(), "symlink": p.is_symlink()}
                                        for p in selected], "next_offset": model.offset + len(selected),
                            "has_more": model.offset + len(selected) < len(children)}
                elif name == "read_file":
                    raw, data = self._read_file_window(target, model)
                    data = {"text": raw.decode("utf-8", errors="replace"), "path": model.path, **data}
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
                            replacements = (
                                change.exact_replacement(text) if isinstance(change, _AnchoredReplacement)
                                else (change.old_text, change.new_text) for change in model.replacements)
                        elif model.old_text is not None and model.new_text is not None:
                            # _Edit already validated these exact field bounds.
                            replacements = [(model.old_text, model.new_text)]
                        else:
                            raise ValueError("Supply replacements or both old_text and new_text")
                        text = replace_exact_spans(text, replacements)
                        model = _Write(path=model.path, expected_sha256=hashlib.sha256(original).hexdigest(),
                            content=text)
                    encoded = model.content.encode("utf-8")
                    if len(encoded) > 256000:
                        raise ValueError("File exceeds the per-write byte limit")
                    committed = self._commit_file(target, model, encoded, name=name, identity=identity)
                    if not committed.ok:
                        return committed
                    data = committed.data
                    if name == 'edit_file':
                        # Snapshot precisely the bytes accepted by the atomic
                        # writer, never a later reread or reconstructed patch.
                        data['authored_text'] = encoded.decode('utf-8')
                        if self.candidate_review_protocol == 'candidate.v1':
                            data['previous_sha256'] = model.expected_sha256
                    if delivery is not None:
                        data["delivery"] = delivery
            if name in {"read_file", "write_file", "edit_file", "list_directory"}:
                data["absolute_path"] = str(target)
            return ToolResult(tool_name=name, data=data, metadata=metadata)
        except (ValueError, KeyError, BudgetError) as exc:
            return ToolResult(tool_name=name, status="blocked", error=str(exc),
                data={'failure_kind': 'resource_exhausted'} if isinstance(exc, BudgetExceededError) else {})
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
            "workspace_path": "workspace/" + target.relative_to(self.workspace).as_posix(),
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
        reason = ("missing_expected_sha256" if current is not None and not expected
                  and not isinstance(request, _ContextEdit) else
                  "sha256_mismatch" if current is not None and expected is not None and current != expected else
                  "target_missing" if current is None and (expected or name == "edit_file") else "")
        if not reason:
            return original, None
        return original, ToolResult(tool_name=name, status="blocked", data={
            "executed": False, "path": request.path, "absolute_path": str(target),
            "current_sha256": current, "expected_sha256": expected, "conflict": reason,
            "retry": "Snapshot only. Retry must explicitly supply the matching expected_sha256; any subsequent change is still rejected.",
        }, error="File write precondition failed: " + reason)

    def copy_registered_project(self, source: Path, candidate: Path,
                                files: dict[str, str]) -> None:
        """Materialize exact registered members using the existing copy owner.

        This is an internal publication operation, not filesystem discovery or
        a new public grant. Registered membership replaces incidental source
        discovery; mounted private/link/environment rules and copy bounds still
        apply. The existing materializer remains the sole byte-copy writer.
        """
        from simple_ar.code_task.workspace.copy import CopyReport, _CopySelection, _materialize_selection
        from simple_ar.code_task.generation.run_plan import ImplementationRunPlan

        ImplementationRunPlan.validate_outputs(list(files))
        source = self.resolve(str(source))
        destination = candidate.absolute()
        candidate = self.resolve(str(candidate), write=True)
        if (candidate == self.workspace or candidate.is_relative_to(source) or source.is_relative_to(candidate)
                or candidate != destination or not source.is_dir()
                or candidate.exists() and any(candidate.iterdir())):
            raise ValueError('Registered project copy requires a source and empty publication candidate')
        if not files or len(files) > 4096:
            raise ValueError('Registered project exceeds existing workspace copy bounds')
        selected = tuple((self.resolve_material_root(str(source / name)), Path(name)) for name in files)
        sizes = [path.stat().st_size for path, _ in selected]
        if any(size > 64 * 1024 * 1024 for size in sizes) or sum(sizes) > 128 * 1024 * 1024:
            raise ValueError('Registered project exceeds existing workspace copy bounds')
        expected = {str(source / name): digest for name, digest in files.items()}
        if self.artifact_hashes(expected) != expected:
            raise ValueError('Registered project inputs changed before copying')
        def allowed(path: Path) -> bool:
            try:
                return (self.resolve_material_root(str(path)) == path
                    and path.relative_to(source).as_posix() in files)
            except (ValueError, OSError, RuntimeError):
                return False

        for name in files:
            self.resolve(str(candidate / name), write=True)
        _materialize_selection(_CopySelection(source, candidate, selected, CopyReport(0, 0, 0, ()),
            max_file_bytes=64 * 1024 * 1024, max_total_bytes=128 * 1024 * 1024, path_allowed=allowed))
        actual = self.bind_inventory(candidate, identity='registered-project-copy')['file_hashes']
        if actual != {str(candidate / name): digest for name, digest in files.items()}:
            raise ValueError('Registered project copy omitted or changed members; candidate was not published')
        if self.artifact_hashes(expected) != expected:
            raise ValueError('Registered project inputs changed during copying')

    def _project_results(self, request: _ProjectResults) -> dict:
        from simple_ar.tools.delivery_versions import DeliveryVersions
        from simple_ar.tools.project_results import discover_project_tasks

        root = self.resolve_material_root(request.path)

        def admit(path: Path) -> Path:
            return self.resolve_material_root(str(path))

        if request.deliveries:
            versions = DeliveryVersions(root, authorize=admit).catalog()
            rows = [{
                **{key: row[key] for key in ('version', 'status', 'project_root', 'artifact_count', 'unresolved')},
                'summary': (row['summary'] or '')[:360],
                'method_count': len(row['method_paths']),
                'method_paths': row['method_paths'][:12],
            } for row in reversed(versions)]
            skipped = 0
        else:
            tasks, skipped = discover_project_tasks(root, authorize=admit)
            rows = [{'path': self.locator(str(task.path)), 'goal': task.goal[:360],
                     'status': task.status} for task in tasks]
        end = request.offset + request.limit
        return {'path': request.path, 'entries': rows[request.offset:end], 'skipped': skipped,
                'next_offset': end if end < len(rows) else None}

    def _copy_project(self, request: _CopyProject, identity: str) -> ToolResult:
        from simple_ar.code_task.generation.common import generation_candidate
        from simple_ar.code_task.workspace.copy import CopyReport, copy_code_workspace
        from simple_ar.core.artifacts import read_json, write_json

        if request.delivery_group is not None and request.delivery_version is None:
            raise ValueError('Select a retained delivery version to copy a delivery group')
        source = self.resolve(request.source)
        target = self.resolve(request.path, write=True)
        if (target == self.workspace or source == target
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
            retained = None
            if request.delivery_version is None:
                report = copy_code_workspace(source, candidate, max_file_bytes=request.max_file_bytes,
                    max_files=request.max_files, max_total_bytes=request.max_total_bytes, path_allowed=allowed)
                hashes = self.artifact_hashes(str(path) for path in candidate.rglob('*') if path.is_file())
                files = {str(Path(path).relative_to(candidate)): digest for path, digest in hashes.items()}
            else:
                from simple_ar.tools.delivery_versions import DeliveryVersions

                owner = DeliveryVersions(source, authorize=lambda path: self.resolve_material_root(str(path)))
                projection, members = owner.snapshot(request.delivery_version, [request.delivery_group or 'code_project'],
                    max_bytes=request.max_total_bytes)
                if len(members) > request.max_files or any(len(raw) > request.max_file_bytes for _, raw in members):
                    raise ValueError('Retained project exceeds the selected copy limits')
                project_root = PurePosixPath(projection.get('project_root', ''))
                files = {}
                for locator, raw in members:
                    relative = PurePosixPath(locator).relative_to(project_root)
                    output = resolve_mounted_path({'workspace': candidate}, f'workspace/{relative.as_posix()}', write=True)
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with output.open('xb') as stream:
                        stream.write(raw)
                    files[relative.as_posix()] = projection['files'][locator]['sha256']
                report = CopyReport(len(files), sum(len(raw) for _, raw in members), 0, ())
                retained = {'session': projection['session'], 'version': projection['version']}
            if not files:
                raise ValueError('Project selection contains no copyable files')
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
            if retained is not None:
                data['retained_delivery'] = retained
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
        if request.project_root is not None:
            project = self.resolve(request.project_root)
            if not project.is_dir() or not project.is_relative_to(self.workspace):
                raise ValueError('Reusable project root must be a workspace directory')
            if not any(path.is_relative_to(project) for path in paths):
                raise ValueError('Reusable project must include at least one delivered artifact')
        return {**request.model_dump(), 'artifacts': artifacts}

    def _read_file_window(self, target: Path, request: FileWindow, *,
                          require_digest: bool = False) -> tuple[bytes, dict[str, Any]]:
        """Read one authorized opened version; no decoding, accounting or records.

        Delivery needs a streamed digest even for a large byte window. Ordinary
        read_file retains its original optional full-digest policy.
        """
        with target.open('rb') as handle:
            def version(stat: os.stat_result | None = None) -> tuple:
                stat = stat if stat is not None else os.fstat(handle.fileno())
                return (str(target), *file_stat_version(stat))

            before = version()
            path_before = version(target.stat())
            # Handle and path timestamps are not interchangeable on every
            # platform. Compare file identity/size/mtime across them, then
            # compare each source to itself after the read.
            if before[:-1] != path_before[:-1]:
                raise ValueError('File changed during material read; inspect its current window')
            size = before[3]
            request.validate_read_size(size, full_digest_max_bytes=_FULL_DIGEST_MAX_BYTES)
            digest = None
            if size <= 256000 or request.full_digest or require_digest:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
            offset = request.offset_bytes
            if request.start_line is not None:
                handle.seek(0)
                lines = io.BytesIO(handle.read(256001)).readlines()
                offset = sum(map(len, lines[:request.start_line - 1]))
                raw = b''.join(lines[request.start_line - 1:
                    request.start_line - 1 + request.max_lines])[:request.max_bytes]
            else:
                handle.seek(offset)
                raw = handle.read(request.max_bytes)
                if 'max_lines' in request.model_fields_set:
                    raw = b''.join(io.BytesIO(raw).readlines()[:request.max_lines])
            after = version()
            path_after = version(target.stat())
            if after != before or self.resolve(request.path) != target or path_after != path_before:
                # Distributed filesystems (e.g. JuiceFS) can flush deferred write
                # metadata on the first read, advancing mtime/ctime without
                # altering file identity, size, or bytes. Accept only when path,
                # inode, size, and re-verified bytes all remain unchanged and
                # subsequent stat has stabilized.
                same_identity = (
                    self.resolve(request.path) == target
                    and after[:4] == before[:4]
                    and path_after[:4] == path_before[:4]
                    and after[:-1] == path_after[:-1]
                )
                verified = False
                if same_identity:
                    if digest is not None:
                        handle.seek(0)
                        digest_after = hashlib.file_digest(handle, 'sha256').hexdigest()
                        verified = digest_after == digest
                    else:
                        handle.seek(offset)
                        raw_after = handle.read(len(raw))
                        verified = raw_after == raw
                    if verified:
                        verified = (version() == after and version(target.stat()) == path_after
                                    and self.resolve(request.path) == target)
                if not verified:
                    raise ValueError('File changed during material read; inspect its current window')
        data = {'sha256': digest, 'size_bytes': size,
            'next_offset_bytes': offset + len(raw), 'has_more': offset + len(raw) < size}
        if request.start_line is not None:
            data.update(start_line=request.start_line, offset_bytes=offset)
        return raw, data

    def delivery_hashes(self, delivery: dict, retained: dict[str, str] | None) -> dict[str, str]:
        """Bind the current inventory while preserving any accepted version."""
        hashes = self.artifact_hashes(delivery['artifacts'])
        if retained is not None and hashes != retained:
            raise ValueError('Final work outputs changed before delivery binding; no delivery or replay')
        return hashes

    def delivery_summary(self, delivery: dict[str, Any], hashes: dict[str, str]) -> str:
        """Materialize only at the common post-production delivery binding.

        resolve_delivery also serves producer preflight, where a new file may
        not exist yet. Never read a summary there. The caller supplies the normal
        delivery hashes, not a second summary version or a new source authority.
        """
        if delivery.get('summary_source') is None:
            return delivery['summary']
        window = FileWindow.model_validate(delivery['summary_source'])
        target = self.resolve(window.path)
        locator = next((value for value in delivery['artifacts'] if self.resolve(value) == target), None)
        if locator is None:
            raise ValueError('Summary source must belong to the actual delivery')
        raw, observed = self._read_file_window(target, window, require_digest=True)
        if observed['sha256'] != hashes.get(locator):
            raise ValueError('Summary source changed during delivery binding')
        # Replacement decoding would silently change the selected bytes. A
        # split UTF-8 character, binary data or empty window must fail closed.
        text = raw.decode('utf-8')
        if not text.strip() or any((ord(char) < 32 or 127 <= ord(char) <= 159)
                and char not in '\n\r\t\f' for char in text):
            raise ValueError('Summary source requires a nonempty UTF-8 text window without control characters')
        return text

    def _list_files(self, target: Path, request: _List) -> dict[str, Any]:
        return list_project_files(target, request, resolve=self.resolve, listed_path=self._listed_path,
            path_allowed=self._diagnostic_visible
                if target.is_relative_to(self.root / 'tools' / 'processes') else None)

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

    def _run(self, request: _Run, identity: str, *, sandboxed: bool = False) -> dict[str, Any]:
        cwd = self.resolve(request.cwd, write=True)
        argv = self.process_argv(request.argv, resolve_mounts=not request.literal_argv)
        output = self.root / "tools" / "processes" / identity
        receipt = output / "invocation.json"
        if receipt.exists():
            record = read_json(receipt)
            if record["status"] not in {"finished", "stopped"}:
                raise ValueError("Existing invocation is live or unknown; cannot replay it")
            # Even historical searches without artifact declarations retain
            # their actual command. An attempt identity is not a query cache.
            if (record.get('invocation_id') != identity or record.get('argv') != argv
                    or record.get('cwd') != str(cwd.resolve()) or record.get('timeout_sec') != request.timeout_sec):
                raise ValueError('Retained process command changed; no reconciliation or replay')
            settle_process_record(record, self.ledger)
            interpreted = _process_artifact_record(record)
            binding, outputs = interpreted.binding, interpreted.complete_outputs
            if binding is not None:
                if binding['request'] != request.model_dump(exclude_none=True):
                    raise ValueError('Retained process artifact request changed; no replay')
                current = self.process_artifact_binding(request.model_dump(exclude_none=True), identity=identity)
                if ({key: value for key, value in current.items() if key != 'inputs'}
                        != {key: value for key, value in binding.items() if key != 'inputs'}):
                    raise ValueError('Retained process inputs changed; no replay')
                self._check_process_inputs(binding, identity=identity,
                    completed_outputs=interpreted.successful_outputs or {})
                if record.get('returncode') == 0 and not record.get('stop_reason') and not record.get('output_binding_error'):
                    if outputs is None or self._version_hashes(outputs) != outputs:
                        raise ValueError('Retained process outputs changed or unbound; no replay')
            elif request.inputs or request.outputs or request.input_snapshot is not None:
                raise ValueError('Historical process lacks declared artifact binding; no retroactive binding or replay')
            return {**process_observation(record, output), "receipt": str(receipt.relative_to(self.root)),
                **({'output_binding_error': record['output_binding_error']} if record.get('output_binding_error') else {})}
        # Public process arguments were validated at admission. Internal fixed
        # searches have their own filename bound and no artifact declarations;
        # do not reinterpret that owned command as another public invocation.
        binding = (self.process_artifact_binding(request.model_dump(exclude_none=True), identity=identity)
                   if request.inputs or request.outputs or request.input_snapshot is not None else None)
        result = run_process(ProcessSpec(argv=argv, cwd=cwd, timeout_sec=request.timeout_sec,
            env=self.process_environment(), output_dir=output, invocation_id=identity, tail_bytes=12000, log_bytes=200000,
            input_binding={'workspace_artifacts': binding} if binding is not None else None,
            sandbox=self.process_sandbox(cwd) if sandboxed else None),
            budget_ledger=self.ledger)
        if binding is not None and result.returncode == 0 and not result.stop_reason:
            from simple_ar.core.artifacts import write_json

            record = read_json(receipt)
            phase = 'outputs'
            try:
                record['output_binding'] = {'version': 1, 'outputs': self._version_hashes(binding['outputs'])}
                phase = 'inputs'
                self._check_process_inputs(binding, identity=identity,
                    completed_outputs=record['output_binding']['outputs'])
            except (OSError, ValueError) as exc:
                record.pop('output_binding', None)
                record['output_binding_error'] = str(exc)
                record['output_binding_error_kind'] = phase
            write_json(receipt, record)
            if record.get('output_binding_error'):
                return {**process_observation(record, output), "receipt": str(receipt.relative_to(self.root)),
                    'output_binding_error': record['output_binding_error']}
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

    def process_argv(self, argv: list[str], *, resolve_mounts: bool = True) -> list[str]:
        """Bind the task interpreter and whole mount arguments, never source text."""
        result = [str(self.resolve(value)) if resolve_mounts and next(iter(PurePosixPath(value).parts), None)
                in self.mounts else value for value in argv]
        if result and result[0] in {"python", "python3"}:
            result[0] = str(self.python)
        return result

    def process_environment(self) -> dict[str, str]:
        """Same filtered task environment for approval and new process launches."""
        env = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT", "WINDIR") if name in os.environ}
        inherited_path = env.get("PATH", "")
        env["PATH"] = str(self.python.parent) + (os.pathsep + inherited_path if inherited_path else "")
        # Tools may need a writable cache home; never inherit the operator's
        # profile or rely on a library's relative fallback when HOME is absent.
        env.update(HOME=str(self.workspace), MPLBACKEND="Agg", OPENBLAS_NUM_THREADS="1",
                   OMP_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
        return env



