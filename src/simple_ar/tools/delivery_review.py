"""Whole-delivery review using the existing inspection owner and native receipts."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from simple_ar.core.assessments import assessment_issues
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.workspace import DeliveryRequest


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

    @model_validator(mode='after')
    def consistent(self):
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


class DeliveryReview:
    def __init__(self, workspace: Any, inspector: Any,
                 user_requests: Callable[[], list[str]], *,
                 observations: Callable[[], list[dict]] | None = None,
                 protocol: str = 'task.v1') -> None:
        if protocol not in {'task.v1', 'task.v2', 'task.v3'}:
            raise ValueError('Unknown delivery review protocol')
        self.workspace, self.inspector = workspace, inspector
        self.user_requests = user_requests
        self.observations = observations
        self.protocol = protocol

    def requires_review(self, request: DeliveryRequest) -> bool:
        """Shared admission and capacity obligation; partial remains an exit."""
        return not request.unresolved and (self.protocol == 'task.v3' or (
            request.report is not None and request.report.status == 'canonical'
            and bool(request.report.manuscript)))

    def _binding(self, delivery: dict, *, current_outputs: tuple[Path, ...] = ()) -> dict | None:
        request = DeliveryRequest.model_validate(delivery)
        if not self.requires_review(request):
            return None
        resolved = self.workspace.resolve_delivery(request, current_outputs=current_outputs)
        # Absolute authorized locators collapse aliases without replacing original bytes
        # with an export, a summary, or a reconstructed manuscript.
        artifacts = sorted({str(self.workspace.resolve(path)) for path in resolved['artifacts']})
        requests = self.user_requests()
        if not isinstance(requests, list) or any(not isinstance(text, str) for text in requests):
            raise ValueError('Delivery review requires exact original request and feedback strings')
        if self.protocol == 'task.v3':
            claim = request.model_dump(mode='json', exclude_none=True)
            claim['artifacts'] = artifacts
            if request.report is not None:
                if request.report.manuscript:
                    claim['report']['manuscript'] = str(self.workspace.resolve(request.report.manuscript))
                if request.report.formats:
                    claim['report']['formats'] = sorted(set(request.report.formats))
            if request.summary_source is not None:
                claim['summary_source']['path'] = str(self.workspace.resolve(request.summary_source.path))
            return {'schema_version': 'delivery_review.v3', 'requests': list(requests),
                    'claim': claim,
                    'authorized_roots': {name: str(path) for name, path in self.workspace.mounts.items()},
                    'artifacts': self.workspace.artifact_hashes(artifacts)}
        manuscript = str(self.workspace.resolve(request.report.manuscript))
        if manuscript not in artifacts:
            raise ValueError('Delivery review requires the actual manuscript among delivered artifacts')
        return {'schema_version': 'delivery_review.v2' if self.protocol == 'task.v2' else 'delivery_review.v1',
                'requests': list(requests),
                'manuscript': manuscript, 'formats': sorted(set(request.report.formats)),
                'authorized_roots': {name: str(path) for name, path in self.workspace.mounts.items()},
                'artifacts': self.workspace.artifact_hashes(artifacts)}

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
            self._package_assessment(result, binding)
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

    def resolve_observation(self, observed: dict, *, review_record: str | None = None) -> dict:
        """Project a closed shared review onto a copy of its original producer observation.

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
            return projected
        try:
            result = self._envelope_result(envelope, envelope['binding'], record=projected.get('record'))
            if result is not None and review_record is not None:
                from simple_ar.tools.professional_outcomes import accepted_native_result, native_result_path

                if not isinstance(review_record, str) or not review_record:
                    raise ValueError('Review record must identify an accepted opinion')
                root = self.inspector.store.root
                path = native_result_path(root, self.workspace, review_record)
                session_id = self.inspector.store.read_json('session_manifest.json')['session_id']
                action_store, _ = accepted_native_result(root, session_id,
                    result.model_dump(mode='json'), path.parent.name)
                action = action_store.read_json('action.json')
                if (action_store.read_attempt_manifest().capability != 'native_action'
                        or not isinstance(action, dict) or action.get('kind') != 'opinion'
                        or not isinstance(action.get('origin'), dict)
                        or action['origin'].get('scope') != envelope['identity']):
                    raise ValueError('Review record is not the accepted opinion for this inspection')
        except _ERRORS:
            return projected
        if result is not None:
            projected['metadata']['delivery_review'] = self._envelope(envelope['binding'],
                envelope['identity'], result)
        return projected

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

    def review(self, delivery: dict, identity: str, *, may_start: bool,
               current_outputs: tuple[Path, ...] = ()) -> dict | None:
        binding = self._binding(delivery, current_outputs=current_outputs)
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
            for observed in reversed(prior() if prior is not None else []):
                candidate = (observed.get('metadata') or {}).get('delivery_review')
                if not isinstance(candidate, dict) or candidate.get('binding') != binding:
                    continue
                try:
                    result = self._envelope_result(candidate, binding, record=observed.get('record'))
                except _ERRORS:
                    # Historical source versions may have changed. This permits a
                    # fresh parent identity, never a replay of the historical one.
                    # Do not fall back to an even older pass of this same bundle.
                    break
                if result is not None and not self._result_issues(result, binding):
                    return self._envelope(binding, candidate['identity'], result)
                # A fresh explicit delivery may complete unfinished investigation
                # or reconsider required findings. No old call is replayed and
                # no incomplete opinion clears the retained findings.
                break
            if not may_start:
                return None
            result = self.inspector.call('inspect_artifacts', self._arguments(binding), identity,
                                         delivery_context=deepcopy(binding))
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

    def issues(self, observations: list[dict], delivery: dict) -> list[str]:
        try:
            binding = self._binding(delivery)
            if binding is None:
                return []
            for observed in reversed(observations):
                candidate = (observed.get('metadata') or {}).get('delivery_review')
                if not isinstance(candidate, dict) or candidate.get('binding') != binding:
                    continue
                if observed.get('review_record') is not None:
                    resolved = self.resolve_observation(observed, review_record=observed['review_record'])
                    candidate = (resolved.get('metadata') or {}).get('delivery_review')
                    if self._pending_envelope(candidate):
                        return ['Independent opinion record cannot be verified for this delivery']
                result = self._envelope_result(candidate, binding, record=observed.get('record'))
                if result is None:
                    return ['Current delivery review is still pending']
                return self._result_issues(result, binding)
            return [('Current delivery lacks a complete independent whole-delivery review'
                     if self.protocol == 'task.v3' else
                     'Current canonical delivery lacks a complete independent whole-delivery review')]
        except _ERRORS as exc:
            return ['Delivery review cannot be verified: ' + str(exc)]
