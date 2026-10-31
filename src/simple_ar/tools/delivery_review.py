"""Whole-delivery review using the existing inspection owner and native receipts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.assessments import assessment_issues, assessment_state, current_assessments, finding_id
from simple_ar.tools.specs import ToolResult, tool_completion


_GOAL = (
    'Independently review the entire current report delivery against the exact original user '
    'requests and feedback, requested formats, manuscript and all delivered artifacts. Own the '
    'full investigation: locate and read the actual relevant sources, code, tables and retained '
    'observations using authorized read-only tools. Check whether the actual claims, methods, '
    'support, consistency and delivered scope satisfy those requests; do not reduce this task '
    'to selected numbers or a local artifact check. Requests express requirements, not evidence. '
    'Directory catalogues locate sources but are not independent evidence; earlier model '
    'opinions are not scientific certificates. Do not execute code. Do not impose PDF, tables '
    'or any other format or presentation absent from the requests. Record concrete required '
    'defects and disclose necessary unfinished investigation as incomplete; a complete reading '
    'alone does not complete the review. No scientific or visual certification is granted.'
)
_ERRORS = (ValueError, OSError, KeyError, TypeError, RuntimeError)


_CLAIM_GOAL = (
    'Independently review the current completion claim against the exact original user requests '
    'and feedback and the entire bound delivery inventory. Investigate the actual relevant '
    'sources, code, tables and retained observations with authorized tools. Determine whether '
    'the delivered work and its claims satisfy the requests, including necessary unfinished '
    'investigation and concrete required defects. A local artifact pass, inventory membership '
    'or complete reading alone cannot establish whole-delivery completion. Requests are '
    'requirements, not evidence; prior model opinions are not certificates. Require only '
    'formats and publication obligations actually applicable to these requests. Disclose '
    'unsupported conclusions and incomplete investigation; grant no scientific or visual certification.'
)


class _DeliveryFinding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    severity: Literal['required', 'advisory']
    message: str = Field(min_length=1)
    subjects: dict[str, str]  # Empty means the bound completion claim itself.


class _DeliveryAssessment(BaseModel):
    """Validate the package opinion without manufacturing file assessments."""
    model_config = ConfigDict(extra='forbid', strict=True)
    schema_version: Literal['delivery_assessment.v1']
    artifacts: dict[str, str]
    coverage: Literal['complete', 'partial']
    verdict: Literal['passed', 'needs_revision', 'incomplete']
    findings: list[_DeliveryFinding]
    evidence_sha256: dict[str, str]
    limitations: list[str]
    reassessed_prior: list[str] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode='after')
    def consistent(self):
        if self.reassessed_prior is not None and (
                len(set(self.reassessed_prior)) != len(self.reassessed_prior)
                or any(len(value) != 64 or any(c not in '0123456789abcdef' for c in value)
                       for value in self.reassessed_prior)):
            raise ValueError('Reassessed delivery concerns require distinct retained finding IDs')
        for digest in [*self.artifacts.values(), *self.evidence_sha256.values()]:
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Delivery assessment requires actual full-file SHA256 versions')
        for finding in self.findings:
            if not finding.message.strip() or any(self.artifacts.get(path) != digest
                    for path, digest in finding.subjects.items()):
                raise ValueError('Delivery finding must identify affected bound artifacts')
        required = any(row.severity == 'required' for row in self.findings)
        if self.verdict == 'passed' and (required or self.coverage != 'complete'):
            raise ValueError('Partial coverage or required delivery findings cannot pass')
        if required and self.verdict != 'needs_revision':
            raise ValueError('Required delivery findings need revision')
        return self


def recorded_delivery_review(reader: Any) -> dict | None:
    """Present the accepted terminal review as a recorded opinion, without replay.

    ``reader`` owns the task's accessible paths. This does not establish that
    evidence or live files remain current, or that the opinion is correct.
    """
    state, manifest = reader.state, reader.manifest
    if (manifest.get('status') not in {'completed', 'partial'} or state.get('pending')
            or not state.get('turns')):
        return None
    turn = state['turns'][-1]
    if (turn.get('remaining') or not turn.get('tools')
            or turn.get('request_count', 1) != len(state.get('requests', []))):
        return None
    item = turn['tools'][-1]
    if not item.get('review_result'):
        return None
    calls = reader.record(turn['reply']).get('tool_calls') or []
    if not calls or calls[-1].get('id') != item.get('id'):
        raise ValueError('Delivery review has no matching terminal call')
    completion = tool_completion(calls[-1]['function']['name'], state['config'].get('tool_completions'))
    if completion is None or completion.kind != 'delivery':
        raise ValueError('Review is not attached to a delivery')
    parent = reader.record(item['result'])
    data, metadata = parent.get('data') or {}, parent.get('metadata') or {}
    delivered = data.get(completion.field) if completion.field else data
    from simple_ar.tools.workspace import DeliveryRequest
    if (parent.get('status') != 'ok' or parent.get('tool_name') != calls[-1]['function']['name']
            or not isinstance(delivered, dict)
            or DeliveryRequest.model_validate(delivered) != DeliveryRequest.model_validate(reader.record('delivery.json'))):
        raise ValueError('Review differs from the recorded terminal delivery')
    envelope = metadata.get('delivery_review') or {}
    binding = envelope.get('binding') or {}
    if binding.get('schema_version') != 'delivery_review.v3':
        return None  # Older review formats retain their own presentation.
    if (envelope.get('identity') != Path(item['result']).parent.name
            or not DeliveryReview._pending_envelope(envelope)):
        raise ValueError('Review does not belong to the terminal delivery')
    from simple_ar.core.capabilities import ArtifactStore
    from simple_ar.tools.materials import accepted_review_journal
    journal = accepted_review_journal(reader, ArtifactStore(reader.root), item, parent)
    result = ToolResult.model_validate(journal['receipt'])
    assessment = _DeliveryAssessment.model_validate(result.metadata.get('delivery_assessment'))
    pinned = metadata.get('delivery_hashes')
    if (not result.ok or result.tool_name != 'inspect_artifacts' or not isinstance(pinned, dict)
            or {str(reader.resolve(path)): digest for path, digest in pinned.items()} != binding.get('artifacts')
            or assessment.artifacts != binding['artifacts']):
        raise ValueError('Review scope differs from the recorded delivery')
    claim = DeliveryRequest.model_validate(delivered).model_dump(mode='json', exclude_none=True)
    claim['artifacts'] = sorted(binding['artifacts'])
    if claim.get('report', {}).get('manuscript'):
        claim['report']['manuscript'] = str(reader.resolve(claim['report']['manuscript']))
    if claim.get('report', {}).get('formats'):
        claim['report']['formats'] = sorted(set(claim['report']['formats']))
    if claim.get('summary_source') is not None:
        claim['summary_source']['path'] = str(reader.resolve(claim['summary_source']['path']))
    if claim != binding.get('claim') or state['requests'] != binding.get('requests'):
        raise ValueError('Review claim differs from the recorded delivery request')
    if reader.record('session_manifest.json') != manifest or reader.record('actions.json') != state:
        raise ValueError('Task changed while reading the delivery review')
    return {'coverage': assessment.coverage, 'verdict': assessment.verdict,
        'limitations': assessment.limitations,
        'findings': [{'severity': row.severity, 'message': row.message} for row in assessment.findings],
        'record': journal['record'],
        'scope': 'Recorded model opinion at delivery time; not proof of current sources or scientific correctness.'}


class DeliveryReview:
    def __init__(self, workspace: Any, inspector: Any, *,
                 observations: Callable[[], list[dict]] | None = None,
                 protocol: str = 'task.v1', track_findings: bool = False) -> None:
        if protocol not in {'task.v1', 'task.v2', 'task.v3'}:
            raise ValueError('Unknown delivery review protocol')
        self.workspace, self.inspector = workspace, inspector
        self.observations = observations
        self.protocol = protocol
        self.track_findings = track_findings

    def _reassessed_ids(self, identity: str, assessment: _DeliveryAssessment) -> list[str]:
        """Only the original inspection work order may authorize a reassessment."""
        ids = assessment.reassessed_prior or []
        if ids:
            saved = self.inspector.store.read_json(self.inspector._path(identity, 'input'))
            offered = {row['id'] for row in saved.get('prior_required', [])}
            if not set(ids) <= offered:
                raise ValueError('Delivery reassessment references a concern not supplied at inspection start')
        return ids

    def _relevant(self, subjects: dict, selected: set[str]) -> bool:
        for path in subjects:
            try:
                if str(self.workspace.resolve(path)) in selected:
                    return True
            except (ValueError, OSError):
                pass  # Unrelated historical paths do not grant access.
        return False

    def _opinion_record(self, record: Any) -> str | None:
        """Use an existing authorized native record, never invent a source copy."""
        if not isinstance(record, str):
            return None
        if record.startswith('attempts/'):
            record = 'records/' + record.removeprefix('attempts/')
        if not record.startswith('records/') or Path(record).name != 'native_result.json':
            return None
        try:
            if not self.workspace.resolve(record).is_file():
                return None
        except (ValueError, OSError):
            return None
        return record

    def _accepted_package(self, observed: dict, envelope: dict, raw: dict) -> tuple[bool, bool]:
        """Acceptance and current applicability are different owner proofs."""
        if not self._pending_envelope(envelope):
            try:
                self._verified(envelope['identity'], envelope['binding'], envelope)
                return True, True
            except _ERRORS:
                # Original feedback was verified before this call. Stale
                # historical findings remain concerns, never retirement proof.
                return True, False
        record = observed.get('review_record')
        if not isinstance(record, str) or not record:
            return False, False
        _, resolved = self._resolve_observation(observed, review_record=record)
        if resolved is not None:
            return True, True
        # _resolve_observation also checks current source bytes. A genuinely
        # accepted historical opinion may now be stale: prove the accepted
        # action independently, retaining findings without retirement power.
        try:
            from simple_ar.tools.professional_outcomes import accepted_scoped_opinion
            self._pending_input(envelope)
            if (Path(observed.get('record', '')).name != 'native_result.json'
                    or Path(observed['record']).parent.name != envelope['identity']):
                return False, False
            root = self.inspector.store.root
            accepted_scoped_opinion(root, self.workspace,
                self.inspector.store.read_json('session_manifest.json')['session_id'],
                record, envelope['identity'], expected=raw)
            return True, False
        except _ERRORS:
            return False, False

    def _reassessment_events(self, identity: str, scope: str, assessment: _DeliveryAssessment,
                             prior_events: list[dict], *, current: bool) -> list[dict]:
        """Project completed individual rechecks without completing the package.

        The inspector only emits IDs after validating selected evidence supply.
        Partial packages additionally need actual bound evidence. Scope comes
        exclusively from the original, integrity-checked inspection work order.
        """
        if not current or not assessment.reassessed_prior or (
                assessment.coverage != 'complete' and not assessment.evidence_sha256):
            return []
        saved = self.inspector.store.read_json(self.inspector._path(identity, 'input'))
        frozen = {row['id']: row for row in saved.get('prior_required', [])}
        originals = {finding_id(event, finding): finding for event in prior_events
                     for finding in event['findings'] if finding['severity'] == 'required'}
        bound = {str(self.workspace.resolve(path)): digest
                 for path, digest in assessment.artifacts.items()}
        events = []
        for prior_id in assessment.reassessed_prior:
            row, original = frozen[prior_id], originals.get(prior_id)
            if (original is None or row.get('message') != original['message']
                    or row.get('severity') != 'required'):
                continue
            paths = row.get('pending_subjects')
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                continue  # Older work orders have no authenticated item scope.
            if not original['subjects']:
                if paths or row.get('subjects') != {}:
                    continue
                subjects = {}
            else:
                allowed = {}
                for path, digest in original['subjects'].items():
                    try:
                        allowed[str(self.workspace.resolve(path))] = digest
                    except (ValueError, OSError):
                        pass  # Revoked siblings are not exposed or reassessed.
                try:
                    selected = {str(self.workspace.resolve(path)) for path in paths}
                    advertised = {str(self.workspace.resolve(path)): digest
                                  for path, digest in row.get('subjects', {}).items()}
                except (ValueError, OSError):
                    continue
                if (not selected or not selected <= allowed.keys() or not selected <= bound.keys()
                        or advertised != {path: allowed[path] for path in selected}):
                    continue
                subjects = {path: bound[path] for path in sorted(selected)}
            events.append({'review_id': f'delivery:{identity}:{scope}:reassessed:{prior_id}',
                'schema_version': 'delivery_assessment.v1', 'subjects': subjects,
                'coverage': 'complete', 'verdict': 'passed', 'findings': [],
                'reassessed_prior': [prior_id]})
        return events

    def _concern_events(self, artifacts: Any, observations: list[dict]) -> list[dict]:
        """Project verified owner opinions in journal order; never persist state.

        The caller supplies accepted native observations. Feedback verifies the
        original opinion, separately from its applicability to current bytes.
        File and claim events deliberately have different identities/scopes.
        """
        selected = {str(self.workspace.resolve(path)) for path in artifacts}
        events = []
        for observed in observations:
            metadata = observed.get('metadata') or {}
            envelope = metadata.get('delivery_review')
            if isinstance(envelope, dict):
                binding = envelope['binding']
                if binding.get('schema_version') != 'delivery_review.v3':
                    continue
                identity = envelope['identity']
                raw = (self.inspector.store.read_json(self.inspector._path(identity, 'result'))
                       if self._pending_envelope(envelope)
                       and self.inspector.store.exists(self.inspector._path(identity, 'result'))
                       else envelope['result'])
                value = (raw.get('metadata') or {}).get('delivery_assessment')
                if value is None:
                    continue  # Pending/failed work has no independent opinion.
                assessment = _DeliveryAssessment.model_validate(value)
                if not (self._relevant(assessment.artifacts, selected)
                        or any(not row.subjects for row in assessment.findings)
                        or assessment.reassessed_prior):
                    continue
                feedback = self.feedback_observation(envelope)
                if not feedback.get('opinion_available'):
                    raise ValueError('Retained delivery concern opinion cannot be verified')
                if assessment.artifacts != binding['artifacts']:
                    raise ValueError('Delivery concern inventory differs from its original scope')
                self._reassessed_ids(identity, assessment)
                accepted, applicable = self._accepted_package(observed, envelope, raw)
                if not accepted:
                    continue  # Unregistered independent replies are diagnostic only.
                # Applicability is established by the existing verified review
                # path, which already checks all bound outputs and evidence.
                current = applicable and raw.get('status') == 'ok'
                scope = hashlib.sha256(json.dumps(binding, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
                completed = self._reassessment_events(identity, scope, assessment, events, current=current)
                for kind, subjects in (('files', assessment.artifacts), ('claim', {})):
                    findings = [row.model_dump(mode='json') for row in assessment.findings
                                if bool(row.subjects) == (kind == 'files')]
                    if kind == 'files' and not subjects:
                        continue
                    events.append({'review_id': f'delivery:{identity}:{scope}:{kind}',
                        'schema_version': 'delivery_assessment.v1', 'subjects': subjects,
                        'coverage': assessment.coverage if current else 'partial',
                        'verdict': assessment.verdict if current else 'incomplete',
                        'findings': findings, 'reassessed_prior': [],
                        'record': self._opinion_record(observed.get('review_record')
                            if self._pending_envelope(envelope) else observed.get('record'))})
                events.extend(completed)
                continue
            if observed.get('tool_name') != 'inspect_artifacts':
                continue
            values = metadata.get('assessments', [metadata.get('assessment')])
            if not any(isinstance(row, dict) and self._relevant(row.get('subjects', {}), selected)
                       for row in values):
                continue
            feedback = self.inspector.feedback_from_observation(observed)
            if feedback is None or not feedback.get('opinion_available'):
                raise ValueError('Retained artifact concern opinion cannot be verified')
            native = {**feedback['result'], 'record': observed.get('record')}
            events.extend({**row.model_dump(mode='json'),
                'record': self._opinion_record(observed.get('record'))}
                for row in current_assessments(self.workspace, [native], retain_stale_findings=True))
        return events

    def pending_concerns(self, artifacts: Any, observations: list[dict] | None = None) -> list[dict]:
        """Return unresolved original concerns, not defects asserted on new bytes."""
        if not self.track_findings:
            return []
        rows = observations if observations is not None else (
            self.observations() if self.observations is not None else [])
        selected = {str(self.workspace.resolve(path)) for path in artifacts}
        events = self._concern_events(artifacts, rows)
        records = {}
        for event in events:
            for finding in event['findings']:
                if finding['severity'] == 'required' and event.get('record'):
                    records.setdefault(finding_id(event, finding), event['record'])
        _, pending = assessment_state(events,
            resolve=self.workspace.resolve, require_explicit_reassessment=True)
        concerns = {}
        for path, findings in pending.items():
            if path is not None and path not in selected:
                continue
            for identity, finding in findings.items():
                row = concerns.setdefault(identity, {'id': identity, 'message': finding['message'],
                    'severity': 'required', 'subjects': deepcopy(finding['subjects']),
                    'pending_subjects': [],
                    **({'record': records[identity]} if identity in records else {})})
                if path is not None:
                    row['pending_subjects'].append(path)
        for row in concerns.values():
            row['pending_subjects'].sort()
            # Identity was computed from the intact original finding above.
            # Public work orders expose only authorized, still-pending paths.
            visible = {}
            for path, digest in row['subjects'].items():
                try:
                    if str(self.workspace.resolve(path)) in row['pending_subjects']:
                        visible[path] = digest
                except (ValueError, OSError):
                    pass
            row['subjects'] = visible
        return [concerns[identity] for identity in sorted(concerns)]

    def _concern_issues(self, observations: list[dict], binding: dict) -> list[str]:
        return ['Delivery retains a concern requiring explicit reassessment: ' + row['message']
                for row in self.pending_concerns(binding['artifacts'], observations)]

    @staticmethod
    def _arguments(binding: dict) -> dict:
        if binding['schema_version'] == 'delivery_review.v3':
            return {'goal': _CLAIM_GOAL, 'targets': [], 'evidence': []}
        return {'goal': _GOAL, 'targets': [binding['manuscript']], 'evidence': []}

    def _package_assessment(self, result: ToolResult, binding: dict) -> _DeliveryAssessment:
        assessment = _DeliveryAssessment.model_validate(result.metadata.get('delivery_assessment'))
        if assessment.artifacts != binding['artifacts']:
            raise ValueError('Delivery assessment does not cover the exact bound inventory')
        if self.workspace.artifact_hashes(assessment.evidence_sha256) != assessment.evidence_sha256:
            raise ValueError('Delivery assessment source evidence changed')
        return assessment

    def _result_issues(self, result: ToolResult, binding: dict) -> list[str]:
        if result.tool_name != 'inspect_artifacts' or not result.ok:
            return ['Delivery review failed: ' + (result.error or result.status)]
        if binding['schema_version'] == 'delivery_review.v3':
            assessment = self._package_assessment(result, binding)
            issues = ['Delivery retains a required finding: ' + row.message
                      for row in assessment.findings if row.severity == 'required']
            if assessment.coverage != 'complete' or assessment.verdict != 'passed':
                issues.append('Current completion claim lacks a complete passed whole-delivery review')
            return issues
        return assessment_issues(self.workspace, [result.model_dump(mode='json')],
            [binding['manuscript']], require_pass=[binding['manuscript']])

    def _verified(self, identity: str, binding: dict, claimed: dict | None = None) -> ToolResult:
        # A retained failure is diagnostic only: no final opinion is needed to
        # report it. Every possible acceptance still goes through _cached's
        # completed response, usage, integrity and version verification.
        path = self.inspector._path(identity, 'result')
        retained = (ToolResult.model_validate(self.inspector.store.read_json(path))
                    if self.inspector.store.exists(path) else None)
        result = retained if retained is not None and not retained.ok else self.inspector._cached(identity)
        saved = self.inspector.store.read_json(self.inspector._path(identity, 'input'))
        if (saved.get('identity') != identity or saved.get('delivery_context') != binding
                or saved.get('arguments') != self._arguments(binding)
                or saved.get('user_requests') != binding['requests']):
            raise ValueError('Delivery review does not match the retained full delivery context')
        if claimed is not None and (claimed.get('identity') != identity
                or claimed.get('binding') != binding
                or (result.ok and claimed.get('error'))
                or claimed.get('result') != result.model_dump(mode='json')):
            raise ValueError('Delivery review metadata differs from the actual inspection receipt')
        if self.workspace.artifact_hashes(binding['artifacts']) != binding['artifacts']:
            raise ValueError('Delivery artifacts changed during review')
        if binding['schema_version'] == 'delivery_review.v3' and result.ok:
            assessment = self._package_assessment(result, binding)
            if self.track_findings:
                self._reassessed_ids(identity, assessment)
        return result

    @staticmethod
    def _envelope(binding: dict, identity: str, result: ToolResult, *, error: str = '') -> dict:
        value = {'binding': deepcopy(binding), 'identity': identity,
                 'result': result.model_dump(mode='json')}
        if not result.ok and not error:
            error = result.error or result.status
        if error:
            value['error'] = error
        return value

    @staticmethod
    def _pending_envelope(envelope: Any) -> bool:
        """Recognize only the original shared-inspection start marker."""
        if (not isinstance(envelope, dict) or set(envelope) != {'binding', 'identity', 'result'}
                or not isinstance(envelope['identity'], str) or not isinstance(envelope['binding'], dict)):
            return False
        return envelope['result'] == ToolResult(tool_name='inspect_artifacts',
            data={'inspection_pending': envelope['identity']}).model_dump(mode='json')

    def _pending_input(self, envelope: dict) -> dict:
        identity, binding = envelope['identity'], envelope['binding']
        saved = self.inspector.store.read_json(self.inspector._path(identity, 'input'))
        if (saved.get('execution_protocol') != 'shared.v1' or saved.get('identity') != identity
                or saved.get('delivery_context') != binding or saved.get('arguments') != self._arguments(binding)
                or saved.get('user_requests') != binding['requests']):
            raise ValueError('Pending delivery review differs from its retained inspection scope')
        return saved

    def _envelope_result(self, envelope: dict, binding: dict, *, record: str | None = None) -> ToolResult | None:
        """Verify a final claim, or resolve an original pending marker to its owner result."""
        identity = envelope['identity']
        if envelope.get('binding') != binding:
            raise ValueError('Delivery review differs from the requested binding')
        if not self._pending_envelope(envelope):
            return self._verified(identity, binding, envelope)
        if record is not None and (not isinstance(record, str) or Path(record).name != 'native_result.json'
                or Path(record).parent.name != identity):
            raise ValueError('Pending delivery review does not belong to its producer observation')
        self._pending_input(envelope)
        if not self.inspector.store.exists(self.inspector._path(identity, 'result')):
            return None
        return self._verified(identity, binding)

    def _resolve_observation(self, observed: dict, *, review_record: str | None = None) -> tuple[dict, ToolResult | None]:
        """Project a closed shared review and return the opinion already verified for it.

        The caller owns proof of the producer record. When provided, its path
        also binds a pending review to the producer attempt. A supplied review
        record must be the accepted opinion action for the same inspection and
        contain the exact owner result. This method does not register, mutate or
        certify production. Unfinished or unverifiable reviews retain their marker.
        """
        projected = deepcopy(observed)
        metadata = projected.get('metadata') or {}
        envelope = metadata.get('delivery_review')
        if not self._pending_envelope(envelope):
            return projected, None
        try:
            result = self._envelope_result(envelope, envelope['binding'], record=projected.get('record'))
            if result is not None and review_record is not None:
                from simple_ar.tools.professional_outcomes import accepted_scoped_opinion

                if not isinstance(review_record, str) or not review_record:
                    raise ValueError('Review record must identify an accepted opinion')
                root = self.inspector.store.root
                session_id = self.inspector.store.read_json('session_manifest.json')['session_id']
                accepted_scoped_opinion(root, self.workspace, session_id, review_record,
                    envelope['identity'], expected=result.model_dump(mode='json'))
        except _ERRORS:
            return projected, None
        if result is not None:
            projected['metadata']['delivery_review'] = self._envelope(envelope['binding'],
                envelope['identity'], result)
        return projected, result

    def resolve_observation(self, observed: dict, *, review_record: str | None = None) -> dict:
        return self._resolve_observation(observed, review_record=review_record)[0]

    def _retained(self, identity: str) -> ToolResult:
        """Preserve failed work for diagnosis only; this never supplies acceptance."""
        path = self.inspector._path(identity, 'result')
        if self.inspector.store.exists(path):
            return ToolResult.model_validate(self.inspector.store.read_json(path))
        return ToolResult(tool_name='inspect_artifacts', status='blocked',
                          error='Delivery inspection started without a completed result; no replay')

    def feedback_observation(self, envelope: dict) -> dict:
        """Return the inspector's original opinion with its full delivery scope.

        A revision changes the inventory, so the current _verified acceptance
        check must not be used to erase earlier findings. The inspector still
        verifies its original input, response, result and accounted usage.
        """
        identity, binding = envelope['identity'], envelope['binding']
        claimed = envelope['result']
        if self._pending_envelope(envelope):
            try:
                self._pending_input(envelope)
                path = self.inspector._path(identity, 'result')
                if self.inspector.store.exists(path):
                    claimed = self.inspector.store.read_json(path)
            except _ERRORS:
                pass  # The inspector returns unavailable guidance for the pending marker.
        feedback = self.inspector.feedback_observation(identity, claimed)
        original = feedback.get('delivery_context') or {}
        if (original != binding or feedback.get('arguments') != self._arguments(binding)):
            feedback.update(opinion_available=False,
                result=ToolResult(tool_name='inspect_artifacts', status='blocked',
                    error='Delivery feedback differs from its retained review scope').model_dump(mode='json'),
                scope='Diagnostic only; no verified whole-delivery opinion is available.')
        # Inventory membership locates relevant feedback; it is never a file pass.
        # v3's package opinion stays in the original inspector receipt.
        return {**feedback, 'delivery_context': deepcopy(original),
            'reviewed_inventory': dict(original.get('artifacts', {}))}

    def review(self, binding: dict | None, identity: str, *, may_start: bool) -> dict | None:
        """Review a freshly prepared owner scope; it is not an acceptance receipt."""
        if binding is None:
            return None
        result = None
        try:
            if self.inspector.has_started(identity):
                if self.inspector.store.read_json(self.inspector._path(identity, 'input')).get(
                        'execution_protocol') == 'shared.v1' and not self.inspector.store.exists(
                            self.inspector._path(identity, 'result')):
                    result = self.inspector.call('inspect_artifacts', self._arguments(binding), identity,
                        delivery_context=deepcopy(binding))
                    return self._envelope(binding, identity, result)
                result = self._verified(identity, binding)
                return self._envelope(binding, identity, result)
            prior = self.observations
            observations = prior() if prior is not None else []
            pending = (self.pending_concerns(binding['artifacts'], observations)
                       if self.track_findings else [])
            for observed in reversed(observations):
                candidate = (observed.get('metadata') or {}).get('delivery_review')
                if not isinstance(candidate, dict) or candidate.get('binding') != binding:
                    continue
                try:
                    if self.track_findings and self._pending_envelope(candidate):
                        if not observed.get('review_record'):
                            break
                        _, result = self._resolve_observation(observed,
                            review_record=observed['review_record'])
                    else:
                        result = self._envelope_result(candidate, binding, record=observed.get('record'))
                except _ERRORS:
                    # Historical source versions may have changed. This permits a
                    # fresh parent identity, never a replay of the historical one.
                    # Do not fall back to an even older pass of this same bundle.
                    break
                if result is not None and not self._result_issues(result, binding) and not pending:
                    return self._envelope(binding, candidate['identity'], result)
                # A fresh explicit delivery may complete unfinished investigation
                # or reconsider required findings. No old call is replayed and
                # no incomplete opinion clears the retained findings.
                break
            if not may_start:
                return None
            result = self.inspector.call('inspect_artifacts', self._arguments(binding), identity,
                delivery_context=deepcopy(binding),
                **({'prior_required': deepcopy(pending)} if self.track_findings else {}))
            if result.ok and result.data.get('inspection_pending') == identity:
                return self._envelope(binding, identity, result)
            if not result.ok:
                # Preserve the actual execution failure before attempting any
                # acceptance-only final-receipt checks. No retry or replay.
                return self._envelope(binding, identity, result)
            verified = self._verified(identity, binding)
            if verified.model_dump(mode='json') != result.model_dump(mode='json'):
                raise ValueError('Returned delivery review differs from its retained result')
            return self._envelope(binding, identity, verified)
        except _ERRORS as exc:
            if result is None:
                try:
                    result = self._retained(identity)
                except _ERRORS:
                    result = ToolResult(tool_name='inspect_artifacts', status='blocked', error=str(exc))
            return self._envelope(binding, identity, result, error=str(exc))

    def issues(self, observations: list[dict], binding: dict | None) -> list[str]:
        try:
            if binding is None:
                return []
            for observed in reversed(observations):
                candidate = (observed.get('metadata') or {}).get('delivery_review')
                if not isinstance(candidate, dict) or candidate.get('binding') != binding:
                    continue
                if self._pending_envelope(candidate) and (
                        self.track_findings or observed.get('review_record') is not None):
                    if self.track_findings and not observed.get('review_record'):
                        return ['Independent opinion record cannot be verified for this delivery']
                    resolved, result = self._resolve_observation(observed, review_record=observed['review_record'])
                    candidate = (resolved.get('metadata') or {}).get('delivery_review')
                    if result is None or self._pending_envelope(candidate):
                        return ['Independent opinion record cannot be verified for this delivery']
                    return [*self._result_issues(result, binding),
                            *self._concern_issues(observations, binding)] if self.track_findings else self._result_issues(result, binding)
                result = self._envelope_result(candidate, binding, record=observed.get('record'))
                if result is None:
                    return ['Current delivery review is still pending']
                return [*self._result_issues(result, binding),
                        *self._concern_issues(observations, binding)] if self.track_findings else self._result_issues(result, binding)
            return [('Current delivery lacks a complete independent whole-delivery review'
                     if self.protocol == 'task.v3' else
                     'Current canonical delivery lacks a complete independent whole-delivery review')]
        except _ERRORS as exc:
            return ['Delivery review cannot be verified: ' + str(exc)]
