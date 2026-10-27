"""Professional work on the task's native loop and journal.

The two receipts delimit one active scope; they are not a nested session or a
second authoring engine. All model decisions and file/process operations remain
ordinary native turns. A completed scope binds actual workspace output bytes.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.artifacts import write_atomically
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.specs import CommonToolSpec, ToolResult


class BeginProfessionalWork(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['source', 'document']
    goal: str = Field(min_length=1, max_length=16000,
        description='The concrete work to perform within the original task and feedback.')
    material_roots: list[str] = Field(default_factory=list, max_length=32,
        description='Additional authorized material or existing-result roots; original task mounts remain available.')
    project_path: str = Field(default='',
        description='An existing authorized project or results directory, if relevant. Omit for new work.')
    prior_draft: str = Field(default='',
        description='Existing editable manuscript to revise; its current version is bound by the owner.')

    @model_validator(mode='after')
    def meaningful(self) -> 'BeginProfessionalWork':
        if not self.goal.strip() or any(not path.strip() for path in self.material_roots):
            raise ValueError('Work goal and material locations must not be blank')
        if self.prior_draft and self.kind != 'document':
            raise ValueError('A prior manuscript belongs to document work')
        return self


class FinishProfessionalWork(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    candidate: str = Field(default='',
        description='Actual editable workspace file or project produced in this work; omit only for partial work.')
    attachments: list[str] = Field(default_factory=list, max_length=64,
        description='Actual related workspace files or directories to retain with the candidate.')
    gaps: list[str] = Field(default_factory=list, max_length=100,
        description='Unfinished work or unavailable material; do not treat a candidate as an independent review.')

    @model_validator(mode='after')
    def meaningful(self) -> 'FinishProfessionalWork':
        if any(not value.strip() for value in [*self.attachments, *self.gaps]):
            raise ValueError('Attachments and gaps must not be blank')
        if not self.candidate and not self.gaps:
            raise ValueError('Partial work without a candidate needs an explicit gap')
        return self


def professional_work_specs() -> list[CommonToolSpec]:
    return [
        CommonToolSpec(name='begin_professional_work', domain='professional_work',
            permission_level='plan', risk_level='low', input_schema=BeginProfessionalWork.model_json_schema(),
            description='Start coherent source implementation/analysis or document drafting/revision in the same native action loop. '
                'Give the work goal and relevant authorized roots, not selected file windows or transcribed evidence. '
                'The receipt only accepts work; subsequent native decisions perform it. Simple work may use direct operations.'),
        CommonToolSpec(name='finish_professional_work', domain='professional_work',
            permission_level='plan', risk_level='low', input_schema=FinishProfessionalWork.model_json_schema(),
            description='Return the actual editable candidate and related files from the active professional work, '
                'or return partial work with explicit gaps. The owner binds current bytes and actual journal observations. '
                'This ends professional work, not independent review, publication or task delivery.'),
    ]


class ProfessionalWorkTools:
    """Scope and candidate receipts, projected from the existing task journal."""

    def __init__(self, *, root: Path, workspace: Any, observations: Callable[[], list[dict]],
                 user_requests: Callable[[], list[str]], work_context: Callable[[list[str]], dict],
                 material_observations: Callable[[list[str]], list[dict]],
                 route_validator: Callable[[str], None]) -> None:
        self.root, self.workspace = Path(root), workspace
        self.observations, self.user_requests = observations, user_requests
        self.work_context, self.material_observations = work_context, material_observations
        self.route_validator = route_validator

    def _store(self, identity: str) -> ArtifactStore:
        return ArtifactStore(self.root / 'attempts' / identity)

    @staticmethod
    def _journal_record(record: str) -> str:
        return 'records/' + record.removeprefix('attempts/') if record.startswith('attempts/') else record

    def has_started(self, identity: str) -> bool:
        store = self._store(identity)
        return store.exists('professional_receipt.json') or store.exists('professional_begin.json')

    def can_reconcile(self, name: str, identity: str) -> bool:
        store = self._store(identity)
        if name not in {'begin_professional_work', 'finish_professional_work'}:
            return False
        if store.exists('professional_receipt.json'):
            return True
        if name == 'begin_professional_work':
            # The frozen request precedes any snapshot. If neither exists,
            # this admission has made no persistent or external change.
            return store.exists('professional_begin.json') or not store.exists('professional_prior.md')
        return False

    def selected(self) -> dict | None:
        for row in reversed(self.observations()):
            if row.get('status') != 'ok':
                continue
            if row.get('tool_name') == 'finish_professional_work':
                return None
            if row.get('tool_name') == 'begin_professional_work':
                return row
        return None

    def latest_result(self) -> dict | None:
        return next((row for row in reversed(self.observations())
            if row.get('tool_name') == 'finish_professional_work' and row.get('status') == 'ok'), None)

    def transition_record(self) -> str | None:
        return next((row['record'] for row in reversed(self.observations())
            if row.get('status') == 'ok' and row.get('tool_name') in
                {'begin_professional_work', 'finish_professional_work'}), None)

    def _scope_locations(self, scope: dict, extra: list[str] | None = None) -> tuple[list[str], list[dict]]:
        locations = [*scope['material_roots'], *([scope['project_path']] if scope['project_path'] else []),
            *([scope['prior_draft']['path']] if scope.get('prior_draft') else []), *(extra or [])]
        available, unavailable = [], []
        for locator in dict.fromkeys(locations):
            try:
                path = self.workspace.resolve_material_root(locator)
                if not path.exists():
                    raise ValueError('Location is no longer available')
                available.append(str(path))
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                unavailable.append({'path': locator, 'diagnostic': str(exc)})
        return available, unavailable

    def input_view(self, *, rows: list[dict], operation_history: list[dict] | None,
                   boundary: int, references: list[str], notes: list[dict]) -> dict | None:
        """Locate scoped journal receipts for the one shared input selector."""
        selected = self.selected()
        if selected is None:
            return None
        available, unavailable = self._scope_locations(selected['data'])
        records = list(references)
        diagnostic = ''
        if available:
            try:
                records.extend(row['record'] for row in self.material_observations(available)
                    if isinstance(row.get('record'), str))
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                diagnostic = str(exc)
            roots = [Path(path) for path in available]
            for entry in operation_history or []:
                observed = entry['observation']
                if entry['turn'] >= boundary or observed.get('tool') != 'run_process':
                    continue
                arguments = observed.get('arguments') or {}
                inputs = arguments.get('inputs') if isinstance(arguments.get('inputs'), list) else []
                outputs = arguments.get('outputs') if isinstance(arguments.get('outputs'), list) else []
                for locator in [arguments.get('cwd'), *inputs, *outputs]:
                    if not isinstance(locator, str):
                        continue
                    try:
                        path = self.workspace.resolve(locator)
                        if any(path == root or root.is_dir() and path.is_relative_to(root) for root in roots):
                            if isinstance(observed.get('record'), str):
                                records.append(observed['record'])
                            break
                    except (ValueError, OSError, RuntimeError):
                        continue
        records.extend(row['record'] for row in rows
            if row.get('turn', -1) >= boundary and row.get('role') == 'historical_observation'
            and row.get('sha256') and isinstance(row.get('record'), str))
        scoped = set(records)
        return {'references': records,
            'notes': [note for note in notes if note.get('records') and set(note['records']).issubset(scoped)],
            'operation_history': ([row for row in operation_history if row['turn'] >= boundary]
                if operation_history is not None else None),
            'diagnostic': diagnostic, 'unavailable_locations': unavailable}

    def purpose(self) -> str:
        selected = self.selected()
        return ('writing' if selected['data']['kind'] == 'document' else 'code') if selected else 'task'

    def instructions(self) -> str:
        selected = self.selected()
        if selected is None:
            return (' For coherent implementation, analysis, drafting or substantive revision, '
                'begin_professional_work can take responsibility for the actual work in this same loop. '
                'Use direct operations for simple work; do not treat acceptance as a result.')
        kind = selected['data']['kind']
        specific = ('Create or substantively revise one editable manuscript. Preserve the prior manuscript; '
            'write a separate candidate and retain its needed local assets. The original bytes are also '
            'available at prior_draft.snapshot in the task records. '
            if kind == 'document' else
            'Create or revise actual editable source, analysis or figure files. Diagnose actual execution '
            'through the ordinary approved process operation when needed. ')
        return (' You are carrying out the active professional work, not merely selecting a method. '
            'Use its current goal, original user requests, authorized material and actual feedback. '
            'Investigate originals and existing results as needed using the ordinary tools; reuse valid '
            'observations, and do not treat historical receipts as current facts. ' + specific +
            'When the candidate is usable, call finish_professional_work with actual workspace paths '
            'and remaining gaps. If work cannot be completed, return the usable candidate or explicit '
            'gaps. This is not a review, publication or delivery; the task continues on the same journal. '
            'Do not begin another professional work before returning this one.')

    def context(self) -> dict | None:
        selected = self.selected()
        if selected is None:
            result = self.latest_result()
            if result is None:
                return None
            data = deepcopy(result['data'])
            try:
                actual = self._versions([*([data['candidate']] if data['candidate'] else []),
                    *data['attachments']], identity='professional-view')
                current = actual == data['artifact_sha256']
            except (ValueError, OSError, RuntimeError):
                current = False
            return {'status': 'returned', 'record': self._journal_record(result['record']), **data,
                'candidate_versions_current': current}
        data = deepcopy(selected['data'])
        prior = data.get('prior_draft')
        if prior:
            try:
                data['prior_current'] = self.workspace.artifact_hashes([prior['path']])[prior['path']] == prior['sha256']
                self._verify_prior_snapshot(prior)
            except (ValueError, OSError):
                data['prior_current'] = False
        available, unavailable = self._scope_locations(data)
        try:
            feedback = self.work_context(available) if available else {'user_requests': self.user_requests()}
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            feedback = {'user_requests': self.user_requests(), 'diagnostic': str(exc)}
        # Requests are separate native messages; notes are admitted once by the
        # shared selector rather than copied into this work-context projection.
        feedback = {key: value for key, value in feedback.items() if key not in {'user_requests', 'working_notes'}}
        return {'status': 'active', 'record': self._journal_record(selected['record']), **data,
            'unavailable_locations': unavailable, 'original_feedback': feedback}

    def _locations(self, request: BeginProfessionalWork) -> tuple[list[str], str, dict | None]:
        mounts = [str(path) for name, path in self.workspace.mounts.items()
                  if name not in {'workspace', 'records'}]
        roots = []
        for locator in [*mounts, *request.material_roots]:
            path = self.workspace.resolve_material_root(locator)
            if not path.exists() or path.is_relative_to(self.workspace.mounts['records']):
                raise ValueError('A work material root is unavailable or is task history')
            roots.append(str(path))
        project = ''
        if request.project_path:
            path = self.workspace.resolve_material_root(request.project_path)
            if not path.is_dir() or path.is_relative_to(self.workspace.mounts['records']):
                raise ValueError('The project must be an available authorized directory')
            project = str(path)
        prior = None
        if request.prior_draft:
            path = self.workspace.resolve_material_root(request.prior_draft)
            if not path.is_file() or path.is_relative_to(self.workspace.mounts['records']):
                raise ValueError('The prior manuscript must be an available authorized file')
            prior = {'path': str(path), 'sha256': self.workspace.artifact_hashes([str(path)])[str(path)]}
        return list(dict.fromkeys(roots)), project, prior

    def _versions(self, locators: list[str], *, identity: str) -> dict[str, str]:
        versions: dict[str, str] = {}
        for locator in dict.fromkeys(locators):
            path = self.workspace.resolve(locator, write=True)
            if path.is_file():
                versions.update(self.workspace.artifact_hashes([str(path)]))
            elif path.is_dir():
                versions.update(self.workspace.bind_inventory(path, identity=identity)['file_hashes'])
            else:
                raise ValueError('A candidate or attachment is unavailable in the workspace')
        return versions

    def _verify_prior_snapshot(self, prior: dict) -> None:
        path = self.workspace.resolve(prior['snapshot'])
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != prior['sha256']:
                raise ValueError('The preserved original manuscript differs from its bound version')

    def _preserve_prior(self, store: ArtifactStore, prior: dict) -> None:
        if store.exists('professional_prior.md'):
            self._verify_prior_snapshot(prior)
            return
        path = self.workspace.resolve_material_root(prior['path'])
        raw = path.read_bytes()
        if (hashlib.sha256(raw).hexdigest() != prior['sha256']
                or self.workspace.artifact_hashes([str(path)])[str(path)] != prior['sha256']):
            raise ValueError('The prior manuscript changed before its original bytes were preserved')
        write_atomically(store.resolve('professional_prior.md'), raw)
        self._verify_prior_snapshot(prior)

    def _scope_rows(self, selected: dict) -> list[dict]:
        rows = self.observations()
        index = next((i for i, row in enumerate(rows) if row.get('record') == selected['record']), None)
        if index is None:
            raise ValueError('The accepted work receipt is no longer in the task journal')
        return rows[index + 1:]

    def _produced(self, candidate: Path, versions: dict[str, str], rows: list[dict]) -> bool:
        for row in rows:
            if row.get('status') != 'ok':
                continue
            name, data, arguments = row.get('tool_name'), row.get('data') or {}, row.get('arguments') or {}
            paths = []
            if name in {'write_file', 'edit_file'}:
                paths = [data.get('absolute_path') or data.get('path')]
            elif name == 'run_process' and data.get('returncode') == 0 and not data.get('stop_reason'):
                paths = arguments.get('outputs') or []
            elif name not in {'copy_file', 'copy_project', 'begin_professional_work', 'finish_professional_work'}:
                paths = row.get('artifacts') or []
            for locator in paths:
                if not isinstance(locator, str):
                    continue
                try:
                    path = self.workspace.resolve(locator, write=True)
                    relevant = (path == candidate or candidate.is_dir() and path.is_relative_to(candidate)
                        or path.is_dir() and candidate.is_relative_to(path))
                    bound = (str(path) in versions if path.is_file() else
                        path.is_dir() and any(Path(value).is_relative_to(path) for value in versions))
                    if relevant and bound:
                        if name in {'write_file', 'edit_file'} and data.get('sha256') != versions[str(path)]:
                            continue
                        if name == 'run_process':
                            self.workspace.check_artifact_producers([str(path)])
                        elif name not in {'write_file', 'edit_file'}:
                            # An artifact locator alone does not prove the
                            # current bytes were produced by this operation.
                            bound_versions = data.get('artifact_sha256')
                            if not isinstance(bound_versions, dict):
                                continue
                            matched = False
                            for output, digest in bound_versions.items():
                                owned = self.workspace.resolve(output, write=True)
                                if (owned == candidate or candidate.is_dir() and owned.is_relative_to(candidate)):
                                    if str(owned) in versions and versions[str(owned)] == digest:
                                        matched = True
                                        break
                            if not matched:
                                continue
                        return True
                except (ValueError, OSError, RuntimeError):
                    continue
        return False

    def _provenance(self, scope: dict, *, identity: str, outputs: list[str], rows: list[dict]) -> list[dict]:
        from simple_ar.tools.materials import validate_material_observation
        targets = list(outputs)
        targets.extend(row['arguments']['path'] for row in rows
            if row.get('tool_name') == 'read_file' and row.get('status') == 'ok'
            and isinstance((row.get('arguments') or {}).get('path'), str))
        available, _ = self._scope_locations(scope, targets)
        if not available:
            return []
        result, cache = [], {}
        try:
            references = self.material_observations(available)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return [{'status': 'unavailable', 'diagnostic': str(exc), 'scope': 'material receipt lookup'}]
        for reference in references:
            try:
                observed = validate_material_observation(self.workspace, reference, identity=identity, cache=cache)
                data = observed['data']
                result.append({'record': reference.get('record'), 'path': data.get('absolute_path'),
                    'sha256': data.get('sha256'), 'offset_bytes': data.get('offset_bytes', 0),
                    'next_offset_bytes': data.get('next_offset_bytes'), 'has_more': data.get('has_more'),
                    'status': 'matching'})
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                result.append({'record': reference.get('record'), 'status': 'unavailable', 'diagnostic': str(exc)})
        return result

    def _finish(self, request: FinishProfessionalWork, identity: str) -> ToolResult:
        selected = self.selected()
        if selected is None:
            raise ValueError('There is no active professional work to return')
        data = selected['data']
        prior = data.get('prior_draft')
        prior_current = True
        snapshot_current = True
        if prior:
            try:
                self._verify_prior_snapshot(prior)
            except (ValueError, OSError):
                snapshot_current = False
            try:
                prior_current = self.workspace.artifact_hashes([prior['path']])[prior['path']] == prior['sha256']
            except (ValueError, OSError):
                prior_current = False
        rows = self._scope_rows(selected)
        candidate = self.workspace.resolve(request.candidate, write=True) if request.candidate else None
        attachments = list(request.attachments)
        gaps = list(request.gaps)
        if not prior_current:
            gaps.append('The original manuscript changed or became unavailable during revision.')
        if not snapshot_current:
            gaps.append('The preserved original manuscript artifact is unavailable or changed.')
        if candidate is not None and data['kind'] == 'document' and candidate.is_file():
            from simple_ar.report.export import report_image_targets
            from simple_ar.tools.materials import relative_image_target
            markdown = candidate.read_text(encoding='utf-8')
            for target in report_image_targets(markdown):
                relative = relative_image_target(target)
                try:
                    image = self.workspace.resolve(str(candidate.parent / relative), write=True) if relative else None
                    if image is None or not image.is_file():
                        raise ValueError('Image is not an available workspace file')
                    attachments.append(str(image))
                except (ValueError, OSError):
                    gaps.append('Document image is unavailable: ' + target)
        attachments = list(dict.fromkeys(attachments))
        versions = self._versions([*([request.candidate] if request.candidate else []), *attachments], identity=identity)
        if candidate is not None:
            if not versions or not self._produced(candidate, versions, rows):
                raise ValueError('Candidate needs actual source or document work recorded in this scope; return partial gaps if unavailable')
            if data['kind'] == 'document':
                if not candidate.is_file() or (prior and candidate == Path(prior['path'])):
                    raise ValueError('Document work needs a separate editable manuscript file')
                with candidate.open(encoding='utf-8') as stream:
                    if not stream.read(1):
                        raise ValueError('Document candidate is empty')
                    while stream.read(65536):
                        pass
        records = [{'record': self._journal_record(row['record']), 'tool': row.get('tool_name'), 'status': row.get('status')}
                   for row in rows if row.get('tool_name') not in {'begin_professional_work', 'finish_professional_work'}]
        result = {'kind': data['kind'], 'goal': data['goal'],
            'candidate': request.candidate, 'attachments': attachments,
            'artifact_sha256': versions, 'prior_draft': prior, 'prior_current': prior_current,
            'prior_snapshot_current': snapshot_current, 'gaps': gaps,
            'outcome': 'partial' if gaps or not candidate else 'candidate',
            'work_record': self._journal_record(selected['record']), 'observations': records,
            'material_provenance': self._provenance(data, identity=identity,
                outputs=[*([request.candidate] if request.candidate else []), *attachments], rows=rows),
            'scope': 'Actual workspace versions and task journal receipts; not independent review, scientific verification or task delivery.'}
        return ToolResult(tool_name='finish_professional_work', data=result,
            artifacts=[*([request.candidate] if request.candidate else []), *attachments],
            summary='Professional work returned with actual candidate versions and remaining gaps.')

    def protect_prior(self, name: str, arguments: dict) -> str | None:
        selected = self.selected()
        prior = (selected or {}).get('data', {}).get('prior_draft')
        if not prior:
            return None
        targets = ([arguments.get('path')] if name in {'write_file', 'edit_file', 'copy_file'} else
                   arguments.get('outputs', []) if name == 'run_process' else [])
        for locator in targets:
            if isinstance(locator, str):
                try:
                    if self.workspace.resolve(locator, write=True) == Path(prior['path']):
                        return 'Preserve the original manuscript; write or copy a separate revision candidate.'
                except (ValueError, OSError):
                    pass
        return None

    def call(self, name: str, arguments: dict, *, identity: str) -> ToolResult:
        try:
            if name not in {'begin_professional_work', 'finish_professional_work'}:
                raise ValueError('Unknown professional work operation')
            store = self._store(identity)
            digest = hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode('utf-8')).hexdigest()
            if store.exists('professional_receipt.json'):
                retained = store.read_json('professional_receipt.json')
                if retained['tool_name'] != name or retained['arguments_sha256'] != digest:
                    raise ValueError('The retained work request differs; no replay')
                result = ToolResult.model_validate(retained['result'])
                versions = result.data.get('artifact_sha256', {})
                paths = [*([result.data['candidate']] if result.data.get('candidate') else []),
                         *result.data.get('attachments', [])]
                if versions and self._versions(paths, identity=identity) != versions:
                    raise ValueError('The retained professional candidate changed; no replay')
                prior = result.data.get('prior_draft')
                if prior:
                    if result.data.get('prior_snapshot_current', True):
                        self._verify_prior_snapshot(prior)
                    if (name == 'finish_professional_work' and result.data.get('prior_current', True)
                            and self.workspace.artifact_hashes([prior['path']])[prior['path']] != prior['sha256']):
                        raise ValueError('The retained prior manuscript changed; no replay')
                return result
            if name == 'begin_professional_work' and store.exists('professional_begin.json'):
                retained = store.read_json('professional_begin.json')
                if retained['tool_name'] != name or retained['arguments_sha256'] != digest:
                    raise ValueError('The retained work request differs; no replay')
                result = ToolResult.model_validate(retained['result'])
                if prior := result.data.get('prior_draft'):
                    self._preserve_prior(store, prior)
                store.write_json('professional_receipt.json', retained)
                return result
            if name == 'begin_professional_work':
                request = BeginProfessionalWork.model_validate(arguments)
                if self.selected() is not None:
                    raise ValueError('Return the active professional work before beginning another')
                self.route_validator('writing' if request.kind == 'document' else 'code')
                roots, project, prior = self._locations(request)
                if prior:
                    prior['snapshot'] = f'records/{identity}/professional_prior.md'
                result = ToolResult(tool_name=name, data={'kind': request.kind, 'goal': request.goal,
                    'material_roots': roots, 'project_path': project, 'prior_draft': prior,
                    'request_index': len(self.user_requests()) - 1, 'accepted': True},
                    summary='Work accepted; no candidate, review, publication or delivery has been produced.')
                retained = {'tool_name': name, 'arguments_sha256': digest, 'result': result.model_dump(mode='json')}
                store.write_json('professional_begin.json', retained)
                if prior:
                    self._preserve_prior(store, prior)
            else:
                result = self._finish(FinishProfessionalWork.model_validate(arguments), identity)
            store.write_json('professional_receipt.json', {'tool_name': name,
                'arguments_sha256': digest, 'result': result.model_dump(mode='json')})
            return result
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return ToolResult(tool_name=name, status='blocked', error=str(exc))
