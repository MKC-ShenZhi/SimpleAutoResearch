"""Post-execution interpretation using the existing bounded material investigator.

Native attempts own scheduling, registration and accounting. These receipts grant
neither delivery nor repair/rerun authority. Observation bodies remain in the
investigation receipt; investigation_observations exposes them only for reuse.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.materials import (
    MaterialSelection, freeze_work_context, inspect_materials, material_inventories,
    material_observation_seed, selected_material_windows,
)
from simple_ar.tools.professional_outcomes import accepted_native_result, registered_native_output
from simple_ar.tools.specs import ToolResult


RESULT_INVESTIGATION = 'execution_findings.v1'
_TOOL = 'investigate_implementation'
_KIND = 'implementation_findings'


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    execution_ref: str = Field(min_length=1, max_length=1000)


class _Evidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(min_length=1, max_length=1000)
    quote: str = Field(min_length=1, max_length=800)


class _Finding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statement: str = Field(min_length=1, max_length=1200)
    evidence: list[_Evidence] = Field(min_length=1, max_length=4)


class _Findings(MaterialSelection):
    findings: list[_Finding] = Field(default_factory=list, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=8)
    remaining_work: list[str] = Field(default_factory=list, max_length=8)
    disposition: Literal['outcome_ready', 'handoff_required', 'blocked'] | None = None

    @model_validator(mode='after')
    def final_shape(self) -> _Findings:
        if any(not text.strip() or len(text) > 800 for text in self.limitations + self.remaining_work):
            raise ValueError('Limitations and remaining work require bounded nonempty statements')
        prose = [*self.limitations, *self.remaining_work]
        for finding in self.findings:
            prose.append(finding.statement)
            prose.extend(evidence.quote for evidence in finding.evidence)
        if sum(len(text) for text in prose) > 12000:
            raise ValueError('Investigation conclusions and evidence quotes exceed 12000 characters')
        if not self.read_windows:
            if self.disposition is None or not self.materials:
                raise ValueError('Finalize an investigation disposition with observed materials')
            if self.disposition == 'outcome_ready' and (not self.findings or self.remaining_work):
                raise ValueError('Ready outcomes require supported findings and no remaining work')
            if self.disposition != 'outcome_ready' and not self.remaining_work:
                raise ValueError('Incomplete outcomes must describe remaining work')
        return self


def _identity(identity: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
        raise ValueError('Invalid investigation attempt identity')


def _hash(path: Any) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def _source(owner: Any, execution_ref: str) -> tuple[Any, dict, Any, dict]:
    from simple_ar.tools.producer_declarations import _proof
    from simple_ar.tools.implementation_run import candidate_source
    from simple_ar.tools.implementation_work import _execution, _retained_request
    proof = _proof(owner)
    saved, _ = _execution(proof, execution_ref, successful=True)
    candidate, _, source = candidate_source(proof, saved['prepared_result'])
    request = _retained_request(source)
    return candidate, saved, request, source


def investigation_required(owner: Any, execution_ref: str) -> bool:
    """Read the original retained request, never infer ownership from results."""
    from simple_ar.tools.producer_declarations import _proof
    from simple_ar.tools.implementation_run import candidate_source
    from simple_ar.tools.implementation_work import _retained_request
    proof = _proof(owner)
    record = owner.workspace.resolve(execution_ref)
    relative = record.relative_to(owner.root / 'attempts').parts
    if len(relative) != 2 or relative[1] != 'candidate_execution.json':
        raise ValueError('Use a registered implementation execution receipt')
    ref = proof._registered_ref(record, kind='prepared_implementation_execution', store_root=record.parent)
    if ref.schema != 'prepared_implementation_execution.v1':
        raise ValueError('Unknown execution receipt schema')
    saved = ArtifactStore(record.parent).read_json(ref)
    _, _, source = candidate_source(proof, saved['prepared_result'])
    request = _retained_request(source)
    marker = getattr(request, 'result_investigation', None)
    if marker is None:
        return False
    if marker != RESULT_INVESTIGATION or not request.complete_work:
        raise ValueError('Invalid original result investigation ownership contract')
    return True


def _binding(owner: Any, execution_ref: str) -> dict:
    candidate, saved, request, source = _source(owner, execution_ref)
    if getattr(request, 'result_investigation', None) != RESULT_INVESTIGATION or not request.complete_work:
        raise ValueError('Execution did not enter the result investigation contract')
    record = owner.workspace.resolve(execution_ref)
    canonical = f'records/{record.parent.name}/candidate_execution.json'
    prepared = owner.workspace.resolve(saved['prepared_result'])
    roots = [str(candidate), *request.material_roots,
        *(window.path for window in request.source_windows), str(record), *saved['output_sha256']]
    roots = list(dict.fromkeys(str(owner.workspace.resolve_material_root(path)) for path in roots))
    hashes = {**saved['approval']['binding']['source_sha256'], **saved['output_sha256']}
    hashes.update({row['path']: row['sha256'] for row in source.get('original_files', {}).values()})
    hashes.update({row['request']['path']: row['sha256']
        for row in source.get('source_windows', []) if 'request' in row})
    if owner.workspace.artifact_hashes(hashes) != hashes:
        raise ValueError('Original implementation inputs or execution outputs changed')
    return {'contract': RESULT_INVESTIGATION, 'execution_ref': canonical,
        'goal': request.goal, 'source_request': source['request'], 'roots': roots,
        'receipt_sha256': {str(path): _hash(path) for path in (
            record, record.parent / 'native_result.json', prepared, prepared.parent / 'request.json')},
        'artifact_sha256': hashes, 'output_sha256': saved['output_sha256']}


def _validate(owner: Any, response: dict, observations: list[dict], identity: str) -> list[dict]:
    selected = _Findings.model_validate(response)
    windows = selected_material_windows(owner.workspace, selected, observations, identity=identity)
    for finding in selected.findings:
        for evidence in finding.evidence:
            path = owner.workspace.resolve(evidence.path)
            if not any(owner.workspace.resolve(row['request']['path']) == path
                    and evidence.quote in row['data']['text'] for row in windows):
                raise ValueError('Finding quote must occur literally in a selected observed window')
    binding = ArtifactStore(owner.root / 'attempts' / identity / 'implementation_findings').read_json('request.json')
    outputs = {owner.workspace.resolve(path) for path in binding['output_sha256']}
    if selected.disposition == 'outcome_ready' and not any(
            owner.workspace.resolve(evidence.path) in outputs
            for finding in selected.findings for evidence in finding.evidence):
        raise ValueError('Ready findings require an actual execution output as observed evidence; '
            'source-only evidence must retain the missing result investigation as remaining work')
    return windows


def _view(identity: str, binding: dict, response: dict, windows: list[dict]) -> dict:
    selected = _Findings.model_validate(response)
    base = f'records/{identity}/implementation_findings'
    return {'contract': RESULT_INVESTIGATION, 'execution_ref': binding['execution_ref'],
        'record': f'{base}/result.json',
        **selected.model_dump(mode='json', exclude={'read_windows', 'materials'}),
        'materials': [row['request'] for row in windows],
        'observation_record': f'{base}/selection-observations.json',
        'observation_references': [{'record': f'{base}/selection-observations.json',
            'path': row['request']['path'], 'sha256': row['data']['sha256'],
            'offset_bytes': row['data'].get('offset_bytes', 0),
            'next_offset_bytes': row['data']['next_offset_bytes']} for row in windows],
        'verification': 'Professional interpretation of inspected evidence; no scientific certification, '
            'task finish, report requirement or repair/rerun authority.'}


def _result(attempt: ArtifactStore, domain: ArtifactStore, data: dict) -> ToolResult:
    ref = attempt.ref('implementation_findings/result.json', kind=_KIND, schema=RESULT_INVESTIGATION)
    return ToolResult(tool_name=_TOOL,
        data={**data, 'receipt_sha256': _hash(domain.resolve('result.json')),
            'observation_sha256': _hash(domain.resolve('selection-observations.json'))},
        artifacts=[data['record']], metadata={'capability_artifacts': [ref.to_dict()]},
        summary='Investigated the successful execution against its original goal; retained evidence and remaining needs.')


def _cached_response(domain: ArtifactStore, binding: dict) -> dict:
    if domain.read_json('request.json') != binding:
        raise ValueError('Investigation binding changed; preserve the original attempt')
    admission = domain.read_json('selection-input.json')
    if admission['binding'] != binding:
        raise ValueError('Investigation selection admission differs from its source binding')
    if not domain.exists('selection-response.json'):
        raise ValueError('Interrupted investigation; preserve retained decisions and accounting, no replay')
    return domain.read_json('selection-response.json')


def investigate_execution(owner: Any, arguments: dict, identity: str) -> ToolResult:
    """Run or reconstruct one bounded investigation; never replay incomplete calls."""
    _identity(identity)
    request = InvestigationRequest.model_validate(arguments)
    binding = _binding(owner, request.execution_ref)
    attempt = ArtifactStore(owner.root / 'attempts' / identity)
    domain = ArtifactStore(attempt.root / 'implementation_findings')
    session = ArtifactStore(owner.root).read_json('session_manifest.json')
    # A new identity cannot purchase another interpretation of the same run.
    # Include interrupted/failed registered attempts, not only successful outputs.
    for previous_id in session['budget']['recorded_attempts']:
        _identity(previous_id)
        if previous_id == identity:
            continue
        previous = ArtifactStore(owner.root / 'attempts' / previous_id)
        if previous.exists('implementation_findings/request.json'):
            prior = previous.read_json('implementation_findings/request.json')
            if prior.get('execution_ref') == binding['execution_ref']:
                raise ValueError('This execution already has an investigation attempt; reuse its accepted '
                    'findings or reconcile its original identity, without new calls or allowance')
    if any(row.attempt_id == identity and row.status in {'reserved', 'unknown'} for row in owner.ledger.entries):
        raise ValueError('Investigation accounting is unsettled; no replay')
    if domain.exists('request.json'):
        response = _cached_response(domain, binding)
    else:
        if domain.exists('selection-input.json') or domain.exists('result.json'):
            raise ValueError('Investigation has orphaned receipts; no replay')
        domain.write_json('request.json', binding)
        inventories = material_inventories(owner.workspace, binding['roots'], identity=identity)
        context = {'goal': binding['goal'], 'execution_ref': binding['execution_ref'],
            'inventories': inventories, 'response_schema': _Findings.model_json_schema(),
            'scope': 'Successful execution is a physical fact, not proof the goal was met. '
                'Inspect results and original conditions. Unread ranges remain unknown.'}
        if getattr(owner, 'work_context', None) is not None:
            context['work_context'] = freeze_work_context(owner.work_context, binding['roots'])
        seed = material_observation_seed(workspace=owner.workspace, inventories=inventories,
            provider=getattr(owner, 'material_observations', None), roots=binding['roots'], context=context)
        domain.write_json('selection-input.json', {'binding': binding, 'context': context,
            'journal_observations': seed['coverage'] if seed else None})
        factory = getattr(owner, 'client_factory', None)
        if factory is not None:
            client = factory(identity)
        else:
            from simple_ar.tools.implementation import ImplementationToolGateway
            client = ImplementationToolGateway._client(owner, identity)
        response = inspect_materials(workspace=owner.workspace, context=context,
            instructions='Return only the response schema. Findings need literal quotes from selected '
                'actually observed material windows. Diagnose missing or unmet goal requirements as '
                'remaining_work; never invent tool routes, composition diagnoses, execution permission '
                'or scientific certification. A successful outcome neither finishes the task nor requires '
                'a report. Read steps contain read_windows alone.',
            inventories=inventories, owner=domain, identity=identity, client=client,
            response_model=_Findings, label='implementation-findings', seed=seed,
            final_fields=('materials', 'findings', 'limitations', 'remaining_work', 'disposition'),
            validate_selection=lambda value, rows: _validate(owner, value, rows, identity),
            terminal_purpose='Interpret the actual execution outcome against the original goal, '
                'with supported findings, limitations and a bounded remaining-work disposition.')
        domain.write_json('selection-response.json', response)
    windows = _validate(owner, response, domain.read_json('selection-observations.json'), identity)
    if _binding(owner, request.execution_ref) != binding:
        raise ValueError('Investigation inputs changed during interpretation')
    data = _view(identity, binding, response, windows)
    if domain.exists('result.json'):
        if domain.read_json('result.json') != data:
            raise ValueError('Retained investigation result changed')
    else:
        domain.write_json('result.json', data)
    return _result(attempt, domain, data)


def accepted_investigation(owner: Any, execution_ref: str, *, include_observations: bool = False) -> dict | None:
    """Return compact context only; use investigation_observations for Writer reuse.

    Only unmarked original work returns None. Marked work requires one exact,
    registered, settled and current result from the existing session attempts.
    """
    if not investigation_required(owner, execution_ref):
        return None
    binding = _binding(owner, execution_ref)
    session = ArtifactStore(owner.root).read_json('session_manifest.json')
    matches = []
    for identity in session['budget']['recorded_attempts']:
        _identity(identity)
        attempt = ArtifactStore(owner.root / 'attempts' / identity)
        manifest = attempt.read_attempt_manifest()
        outputs = [ref for ref in manifest.outputs if ref.kind == _KIND]
        if not outputs:
            continue
        observed = attempt.read_json('native_result.json')
        if observed.get('tool_name') != _TOOL:
            raise ValueError('Findings output has a different native owner')
        if observed.get('data', {}).get('execution_ref') != binding['execution_ref']:
            continue
        accepted_native_result(owner.root, owner.session_id, observed, identity, ledger=owner.ledger)
        if observed.get('status') != 'ok' or observed.get('error'):
            raise ValueError('Investigation native outcome is not successful')
        path = attempt.root / 'implementation_findings' / 'result.json'
        registered = registered_native_output(owner.root, owner.session_id, path,
            kind=_KIND, store_root=attempt.root)
        if registered.schema != RESULT_INVESTIGATION or len(outputs) != 1:
            raise ValueError('Invalid investigation registration')
        domain = ArtifactStore(path.parent)
        response = _cached_response(domain, binding)
        windows = _validate(owner, response, domain.read_json('selection-observations.json'), identity)
        data = _view(identity, binding, response, windows)
        if domain.read_json('result.json') != data or _binding(owner, execution_ref) != binding:
            raise ValueError('Accepted investigation evidence or result changed')
        expected = _result(attempt, domain, data)
        if expected.model_dump(mode='json') != observed:
            raise ValueError('Investigation differs from its exact accepted native result')
        matches.append({**expected.data,
            'domain_record': expected.data['record'],
            'domain_receipt_sha256': expected.data['receipt_sha256'],
            'record': f'records/{identity}/native_result.json',
            'receipt_sha256': _hash(attempt.resolve('native_result.json')),
            **({'observations': [
            {'request': row['request'], 'data': row['data']} for row in windows]}
            if include_observations else {})})
    if len(matches) != 1:
        raise ValueError('Marked execution requires exactly one accepted current investigation')
    return matches[0]


def investigation_observations(owner: Any, investigation: dict) -> list[dict]:
    """Separately expose revalidated selected bodies; never put these in coordinator context."""
    accepted = accepted_investigation(owner, investigation['execution_ref'], include_observations=True)
    if accepted is None or {key: value for key, value in accepted.items() if key != 'observations'} != {
            key: value for key, value in investigation.items() if key != 'observations'}:
        raise ValueError('Observation reuse requires the exact accepted investigation context')
    return accepted['observations']


def investigations_for(owner: Any, targets: list[str], *, include_observations: bool = False) -> list[dict]:
    """Find registered investigations for actual candidate/output target roots.

    Relevance comes from registered execution/preparation receipts, not material
    roots (which may be shared by unrelated work), guessed directories or a new
    index. Relevant invalid/stale outcomes raise; callers may omit optional
    context explicitly. Validation never invokes material_observations.
    """
    from simple_ar.tools.producer_declarations import _proof
    roots = [owner.workspace.resolve(path) for path in targets]
    if not roots:
        return []
    proof = _proof(owner)
    session = ArtifactStore(owner.root).read_json('session_manifest.json')
    relevant = {}
    for identity in session['budget']['recorded_attempts']:
        _identity(identity)
        attempt = ArtifactStore(owner.root / 'attempts' / identity)
        manifest = attempt.read_attempt_manifest()
        if not any(ref.kind == _KIND for ref in manifest.outputs):
            continue
        observed = attempt.read_json('native_result.json')
        if observed.get('tool_name') != _TOOL:
            raise ValueError('Findings output has a different native owner')
        execution_ref = observed.get('data', {}).get('execution_ref')
        if not execution_ref:
            raise ValueError('Registered investigation has no execution source')
        execution = owner.workspace.resolve(execution_ref)
        relative = execution.relative_to(owner.root / 'attempts').parts
        if len(relative) != 2 or relative[1] != 'candidate_execution.json':
            raise ValueError('Investigation source is not an implementation execution receipt')
        ref = proof._registered_ref(execution, kind='prepared_implementation_execution', store_root=execution.parent)
        if ref.schema != 'prepared_implementation_execution.v1':
            raise ValueError('Unknown investigation execution schema')
        saved = ArtifactStore(execution.parent).read_json(ref)
        prepared = owner.workspace.resolve(saved['prepared_result'])
        preparation = ArtifactStore(prepared.parent.parent)
        source_ref = proof._registered_ref(prepared, kind='prepared_implementation', store_root=preparation.root)
        if source_ref.schema != 'prepared_implementation.v1':
            raise ValueError('Unknown investigation preparation schema')
        payload = preparation.read_json(source_ref)
        candidate = owner.workspace.resolve(payload['data']['project_dir'])
        outputs = [owner.workspace.resolve(path) for path in saved['output_sha256']]
        if not any(root == candidate or root.is_relative_to(candidate) or candidate.is_relative_to(root)
                or any(output == root or output.is_relative_to(root) for output in outputs) for root in roots):
            continue
        canonical = f'records/{execution.parent.name}/candidate_execution.json'
        relevant[canonical] = None
    results = []
    for execution_ref in relevant:
        result = accepted_investigation(owner, execution_ref, include_observations=include_observations)
        if result is not None:
            results.append(result)
    return results
