"""Attempt-local incremental source editing through the existing byte owner.

Only decision-step receipts and ordinary edit backups are durable. A draft is
never a prepared result, and this helper cannot resume, execute or adopt one.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
from typing import Any, Callable

from simple_ar.core.artifacts import write_json
from simple_ar.code_task.generation.run_plan import ImplementationRunPlan


class CandidateDraft:
    def __init__(self, sources: Any, artifact_dir: Path, validate_destination: Callable[[str], Any],
                 *, retained_step: int | None = None) -> None:
        self.sources, self.workspace = sources, sources.workspace
        self.artifact_dir, self.validate_destination = artifact_dir, validate_destination
        self.root = self.workspace.resolve(f'workspace/implementation-drafts/{sources.identity}', write=True)
        if retained_step is not None and not self.root.is_dir():
            raise ValueError('Retained candidate draft is missing; no adoption or replay')
        if self.root.exists() and retained_step is None:
            raise ValueError('Existing candidate draft is not resumable; inspect the original attempt, no replay')
        self.root.mkdir(parents=True, exist_ok=retained_step is not None)
        # Current mutation receipts, exposed in each decision input; no second
        # persistent manifest, source cache, allowance or adoption state.
        self.files: dict[str, str] = {}
        if retained_step is not None:
            for step in range(1, retained_step):
                record = artifact_dir / 'decision-steps' / str(step) / 'candidate-operation.json'
                if not record.exists():
                    continue
                from simple_ar.core.artifacts import read_json
                saved = read_json(record)
                if saved['action'] not in {'stage_candidate_file', 'write_candidate_file', 'edit_candidate_file'}:
                    continue
                result = saved['result']
                if result['status'] != 'ok' or result.get('error'):
                    continue
                name = Path(saved['request']['path']).relative_to(self.root).as_posix()
                self.path(name)
                expected = saved['request'].get('expected_sha256', '')
                if expected != self.files.get(name, ''):
                    raise ValueError('Retained candidate mutation chain changed; no replay')
                self.files[name] = result['data']['sha256']
            self.validate()

    def context(self) -> dict:
        return {'files': [{'path': path, 'sha256': digest} for path, digest in self.files.items()],
            'scope': 'Private editable draft, not executable or published. Paths are candidate-relative. '
                'Stage existing source before editing; omitted registered members survive publication. '
                'submit_candidate must reference every staged/written file at its current SHA256.'}

    def path(self, name: str) -> Path:
        ImplementationRunPlan.validate_outputs([name])
        self.validate_destination(name)
        path = self.workspace.resolve(str(self.root / name), write=True)
        if not path.is_relative_to(self.root):
            raise ValueError('Candidate edit escaped its private draft')
        return path

    def validate(self) -> None:
        inventory = self.workspace.bind_inventory(self.root, identity=self.sources.identity)
        if inventory['file_hashes'] != {str(self.path(path)): digest for path, digest in self.files.items()}:
            raise ValueError('Candidate draft files are missing, changed or unrecorded; no adoption')

    def call(self, name: str, arguments: dict, decision: int) -> dict:
        self.validate()
        path = self.path(arguments['path'])
        relative = arguments['path']
        operation = {'stage_candidate_file': 'write_file', 'write_candidate_file': 'write_file',
            'edit_candidate_file': 'edit_file', 'read_candidate_file': 'read_file'}[name]
        bound = None
        if name == 'stage_candidate_file':
            if relative in self.files:
                raise ValueError('Source is already staged; read or edit its current draft')
            originals, _ = self.sources.bind_response(
                {'files': [{'path': relative, 'replacements': []}]}, commit=False)
            bound = originals.get(relative)
            if bound is None:
                raise ValueError('Stage requires an authorized existing editable source')
            request = {'path': str(path), 'content': bound['text']}
        else:
            if name != 'write_candidate_file' and relative not in self.files:
                raise ValueError('Read/edit requires a staged or written candidate file')
            if name == 'write_candidate_file' and relative not in self.files:
                originals, _ = self.sources.bind_response({'files': [{'path': relative}]}, commit=False)
                if relative in originals:
                    raise ValueError('Stage existing source before editing; source content is never overwritten')
            request = {**arguments, 'path': str(path)}
            if operation in {'write_file', 'edit_file'} and relative in self.files:
                if arguments.get('expected_sha256') != self.files[relative]:
                    raise ValueError('Draft edit requires its current recorded SHA256')
                if operation == 'write_file' and relative in self.sources.originals:
                    self._require_original_read(relative)
            if operation == 'read_file':
                request['full_digest'] = True
        ImplementationRunPlan.validate_outputs([*self.files, *([] if relative in self.files else [relative])])
        if len(self.files) + (relative not in self.files) > 12:
            raise ValueError('Candidate draft exceeds 12 editable sources')
        identity = 'draft-' + hashlib.sha256(self.sources.identity.encode()).hexdigest()[:20] + f'-{decision}'
        result = self.workspace.call(operation, request, identity=identity)
        write_json(self.artifact_dir / 'decision-steps' / str(decision) / 'candidate-operation.json',
            {'action': name, 'identity': identity, 'request': request, 'result': result.model_dump(mode='json')})
        if not result.ok:
            raise ValueError(f'Candidate byte operation rejected: {result.error}')
        if operation != 'read_file':
            self.files[relative] = result.data['sha256']
            if bound is not None:
                self.sources.originals[relative] = bound
        return {**result.model_dump(mode='json'), 'candidate_draft': self.context()}

    def _require_original_read(self, name: str) -> None:
        from simple_ar.tools.materials import observed_window_key
        from simple_ar.tools.workspace import FileWindow

        original = self.sources.originals[name]
        coverage = []
        for row in self.sources.observations:
            if 'request' not in row:
                continue
            path, start, end, digest = observed_window_key(self.workspace,
                FileWindow.model_validate(row['request']), row)
            if path == str(self.workspace.resolve(original['path'])) and digest == original['sha256']:
                coverage.append((start, end))
        covered = 0
        for start, end in sorted(coverage):
            if start > covered:
                break
            covered = max(covered, end)
        if not coverage or covered < len(original['text'].encode('utf-8')):
            raise ValueError('Replacing a staged original requires complete actual original-read coverage; '
                'use exact edits or read the necessary original ranges first')

    def assemble(self, response: dict) -> dict:
        rows = response['files']
        expected = {row['path']: row['sha256'] for row in rows}
        if len(expected) != len(rows) or expected != self.files:
            raise ValueError('Submit must name every candidate draft file at its current recorded SHA256')
        self.validate()
        self.sources.validate()
        contents = []
        for name, digest in expected.items():
            with self.path(name).open('rb') as handle:
                raw = handle.read(256001)
            if len(raw) > 256000 or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('Submitted candidate bytes changed or exceed the text bound')
            contents.append({'path': name, 'content': raw.decode('utf-8')})
        return {**response, 'files': contents}


def incremental_actions(tools: list[dict]) -> list[dict]:
    """Reuse byte-owner schemas, removing delivery and all external scope."""
    from simple_ar.tools.workspace import workspace_tool_specs

    tools = deepcopy(tools)
    submit = next(row['function'] for row in tools if row['function']['name'] == 'submit_candidate')
    submit['description'] = 'Validate and publish the actual edited draft by path/current SHA256; no file contents or replacements.'
    submit['parameters']['properties']['files']['items'] = {'type': 'object', 'properties': {
        'path': {'type': 'string'}, 'sha256': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}},
        'required': ['path', 'sha256'], 'additionalProperties': False}
    tools.append({'type': 'function', 'function': {'name': 'stage_candidate_file',
        'description': 'Bind and stage one existing editable source byte-for-byte, without retransmitting its contents.',
        'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}},
            'required': ['path'], 'additionalProperties': False}}})
    for spec in workspace_tool_specs():
        if spec.name not in {'read_file', 'write_file', 'edit_file'}:
            continue
        schema = deepcopy(spec.input_schema)
        schema['properties'].pop('delivery', None)
        if spec.name == 'write_file':
            schema.pop('$defs', None)  # Only the removed delivery used these definitions.
        schema['properties']['path']['description'] = 'Literal candidate-relative file in this attempt private draft only.'
        tools.append({'type': 'function', 'function': {'name': spec.name.replace('_file', '_candidate_file'),
            'description': spec.description.split(' If this is the final file')[0] +
                ' Scoped to draft bytes only; no source writes, execution or delivery.', 'parameters': schema}})
    return tools


def retained_submission(artifact_dir: Path, ledger: Any, identity: str) -> tuple[int, dict, dict]:
    """Prove one completed final reply; never resume a read/edit/model request."""
    from simple_ar.core.artifacts import read_json
    from simple_ar.core.budget import completed_llm_usage_is_accounted
    from simple_ar.code_task.generation.writer import _parse_implementation_action

    steps = sorted(int(path.name) for path in (artifact_dir / 'decision-steps').iterdir()
        if path.is_dir() and path.name.isdecimal())
    if not steps or steps != list(range(1, steps[-1] + 1)):
        raise ValueError('Interrupted generation has no complete final submission; no replay')
    step = artifact_dir / 'decision-steps' / str(steps[-1])
    entries = [row.to_dict() for row in ledger.entries if row.attempt_id == identity]
    label = f'implementation-unit-decision-{steps[-1]}'
    if (not completed_llm_usage_is_accounted(entries) or not any(
            row['logical_call_id'] == label and row['status'] == 'settled'
            and row['actual'].get('llm_requests', 0) > 0 and row['actual'].get('total_tokens', 0) > 0
            for row in entries) or (step / 'native-error.json').exists()):
        raise ValueError('Final native reply is incomplete or unsettled; no replay')
    message = read_json(step / 'native-response.json')
    name, _, _ = _parse_implementation_action(message, read_json(step / 'native-request.json')['tools'])
    if name != 'submit_candidate' or ((step / 'validation.json').exists()
            and read_json(step / 'validation.json').get('status') != 'accepted'):
        raise ValueError('Retained decision is not a completed final candidate submission; no replay')
    return steps[-1], message, read_json(step / 'input.json')
