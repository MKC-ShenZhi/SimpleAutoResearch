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
from simple_ar.tools.workspace import DeliveryRequest


def delivery_claim(request: DeliveryRequest, artifacts: list[str], resolve: Callable) -> dict:
    """Canonical wire claim shared by live binding and recorded opinion reading."""
    claim = request.model_dump(mode='json', exclude_none=True)
    claim['artifacts'] = artifacts
    if request.report is not None:
        if request.report.manuscript:
            claim['report']['manuscript'] = str(resolve(request.report.manuscript))
        if request.report.formats:
            claim['report']['formats'] = sorted(set(request.report.formats))
    if request.summary_source is not None:
        claim['summary_source']['path'] = str(resolve(request.summary_source.path))
    return claim


def delivery_requires_review(request: DeliveryRequest, protocol: str) -> bool:
    """Saved delivery admission and capacity obligation; partial remains an exit."""
    if protocol not in {'task.v1', 'task.v2', 'task.v3', 'task.v4', 'task.v5'}:
        raise ValueError('Unknown delivery review protocol')
    return not request.unresolved and (protocol in {'task.v3', 'task.v4', 'task.v5'} or (
        request.report is not None and request.report.status == 'canonical'
        and bool(request.report.manuscript)))


def prepare_delivery_review(workspace: Any, request: DeliveryRequest, artifacts: list[str],
                            user_requests: Callable[[], list[str]], *, protocol: str,
                            candidate: bool = False) -> dict | None:
    """Bind the acceptance owner's freshly resolved inventory, never cached authority.

    The caller resolves delivery eligibility, including registered current outputs.
    Paths and bytes are checked here again; review and commit retain their own
    freshness checks. This value adds no persisted state or acceptance decision.
    Keep the saved v1/v2/v3 wire representations unchanged.
    """
    if not candidate and not delivery_requires_review(request, protocol):
        return None
    if candidate and protocol not in {'task.v3', 'task.v4', 'task.v5'}:
        raise ValueError('Candidate investigation requires the whole-work review contract')
    artifacts = sorted({str(workspace.resolve(path)) for path in artifacts})
    requests = user_requests()
    if not isinstance(requests, list) or any(not isinstance(text, str) for text in requests):
        raise ValueError('Delivery review requires exact original request and feedback strings')
    if protocol in {'task.v3', 'task.v4', 'task.v5'}:
        claim = delivery_claim(request, artifacts, workspace.resolve)
        return {'schema_version': 'delivery_review.v3',
                **({'purpose': 'candidate', 'unfinished': list(request.unresolved)} if candidate else {}),
                **({'scope_policy': 'applicable.v2' if protocol == 'task.v5' else 'applicable.v1'}
                   if protocol in {'task.v4', 'task.v5'} else {}),
                'requests': list(requests),
                'claim': claim,
                'authorized_roots': {name: str(path) for name, path in workspace.mounts.items()},
                'artifacts': workspace.artifact_hashes(artifacts)}
    manuscript = str(workspace.resolve(request.report.manuscript))
    if manuscript not in artifacts:
        raise ValueError('Delivery review requires the actual manuscript among delivered artifacts')
    return {'schema_version': 'delivery_review.v2' if protocol == 'task.v2' else 'delivery_review.v1',
            'requests': list(requests),
            'manuscript': manuscript, 'formats': sorted(set(request.report.formats)),
            'authorized_roots': {name: str(path) for name, path in workspace.mounts.items()},
            'artifacts': workspace.artifact_hashes(artifacts)}


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

_APPLICABLE_GOAL = (
    'Determine and complete the checks necessary for the current completion claim against the '
    'exact original requests and feedback and actual delivered work. Own applicability and '
    'investigation in this same inspection, not a separate classification or producer declaration. '
    'A simple explanation may conclude promptly when the available facts suffice. Investigate '
    'scientific, numerical, behavioral and visual claims according to their actual substance, '
    'including necessary independent verification; generated outputs agreeing is not enough. '
    'Never waive checks based on file suffixes, task labels, inventory size or producer assurances. '
    'Use the offered authorized tools to read relevant current work and original evidence on demand. '
    'Historical source and receipt locators are navigation, not supplied evidence or a current pass. '
    'Preserve prior required concerns and explicitly recheck those actually resolved. For a '
    'file concern, select a current window of that subject or an owner-bound process or '
    'visual check of it; a fragment of a historical receipt does not supply its embedded '
    'source. Reassess claim-only concerns against the exact claim and requests. Conclude '
    'incomplete when necessary evidence or investigation is unavailable; distinguish inherent '
    'uncertainty and optional improvements from unfinished requested work. Return the same final '
    'inspection opinion, without imposing unrelated reading, execution or publication.'
)


def applicable_delivery_scope(binding: dict) -> bool:
    """Recognize an owner-only policy in the existing version-bound context.

    Absence preserves historical behavior; unknown policies must never silently
    fall back to either contract. Public InspectionRequest has no such field.
    """
    if 'scope_policy' not in binding:
        return False
    if (binding.get('schema_version') != 'delivery_review.v3'
            or binding['scope_policy'] not in {'applicable.v1', 'applicable.v2'}):
        raise ValueError('Unsupported delivery inspection scope policy')
    return True


def reassessment_has_subject_support(prior: dict, artifacts: dict,
                                     evidence_scopes: list[dict[str, str]]) -> bool:
    """Require a provenance link to each pending subject, not semantic proof.

    Scopes come from supplied observations and their owner-validated inputs,
    never from inventory membership or a model's assertion of relevance. The
    inspector still judges which windows/checks actually resolve the concern.
    Empty subjects identify the exact completion claim already in its input;
    they must not borrow a file finding's scope (or vice versa).
    """
    subjects, pending = prior.get('subjects'), prior.get('pending_subjects')
    if not isinstance(subjects, dict) or not isinstance(pending, list):
        return False
    if not subjects:
        return not pending
    if not pending or not set(pending) <= subjects.keys():
        return False
    return all(path in artifacts and any(scope.get(path) == artifacts[path]
        for scope in evidence_scopes) for path in pending)


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
    applicability: str | None = Field(default=None, exclude_if=lambda value: value is None)
    investigations: list[dict] | None = Field(default=None, exclude_if=lambda value: value is None)
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
    parent = None
    if not item.get('review_result'):
        # Historical/unreviewed deliveries had no opinion action. Preserve
        # their read-only behavior; only inspect a possible reused envelope.
        try:
            parent = reader.record(item['result'])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not isinstance(parent, dict) or not isinstance(parent.get('metadata') or {}, dict):
            return None
        possible = (parent.get('metadata') or {}).get('delivery_review') or {}
        if (not isinstance(possible, dict) or not isinstance(possible.get('binding'), dict)
                or possible['binding'].get('schema_version') != 'delivery_review.v3'
                or DeliveryReview._pending_envelope(possible)):
            return None
    calls = reader.record(turn['reply']).get('tool_calls') or []
    if not calls or calls[-1].get('id') != item.get('id'):
        raise ValueError('Delivery review has no matching terminal call')
    completion = tool_completion(calls[-1]['function']['name'], state['config'].get('tool_completions'))
    if completion is None or completion.kind != 'delivery':
        raise ValueError('Review is not attached to a delivery')
    if parent is None:
        parent = reader.record(item['result'])
    data, metadata = parent.get('data') or {}, parent.get('metadata') or {}
    delivered = data.get(completion.field) if completion.field else data
    if (parent.get('status') != 'ok' or parent.get('tool_name') != calls[-1]['function']['name']
            or not isinstance(delivered, dict)):
        raise ValueError('Review differs from the recorded terminal delivery')
    request = DeliveryRequest.model_validate(delivered)
    if request != DeliveryRequest.model_validate(reader.record('delivery.json')):
        raise ValueError('Review differs from the recorded terminal delivery')
    envelope = metadata.get('delivery_review') or {}
    binding = envelope.get('binding') or {}
    if binding.get('schema_version') != 'delivery_review.v3':
        return None  # Older review formats retain their own presentation.
    from simple_ar.core.capabilities import ArtifactStore
    from simple_ar.tools.professional_outcomes import accepted_review_journal
    store = ArtifactStore(reader.root)
    if DeliveryReview._pending_envelope(envelope):
        if not item.get('review_result'):
            return None  # No accepted opinion action was recorded.
        if envelope.get('identity') != Path(item['result']).parent.name:
            raise ValueError('Review does not belong to the terminal delivery')
        journal = accepted_review_journal(reader, store, item, parent)
    else:
        # A later terminal action can reuse a previously accepted opinion only
        # for the exact same request, claim and file versions. Find the actual
        # earlier opinion action; a copied result or model assertion is not one.
        from simple_ar.tools.professional_outcomes import accepted_native_result
        accepted_native_result(reader.root, manifest['session_id'],
            parent, Path(item['result']).parent.name)
        journal = None
        for earlier in reversed(state['turns'][:-1]):
            earlier_calls = reader.record(earlier['reply']).get('tool_calls') or []
            for candidate in reversed(earlier.get('tools', [])):
                if not candidate.get('review_result') or not any(
                        call.get('id') == candidate.get('id') for call in earlier_calls):
                    continue
                previous = reader.record(candidate['result'])
                prior = (previous.get('metadata') or {}).get('delivery_review') or {}
                if (not DeliveryReview._pending_envelope(prior)
                        or prior.get('identity') != envelope.get('identity')
                        or prior.get('binding') != binding
                        or Path(candidate['result']).parent.name != prior['identity']):
                    continue
                origin = accepted_review_journal(reader, store, candidate, previous)
                if origin['receipt'] == envelope.get('result'):
                    journal = origin
                    break
            if journal is not None:
                break
        if journal is None:
            raise ValueError('Reused review has no matching accepted opinion record')
    result = ToolResult.model_validate(journal['receipt'])
    assessment = _DeliveryAssessment.model_validate(result.metadata.get('delivery_assessment'))
    pinned = metadata.get('delivery_hashes')
    if (not result.ok or result.tool_name != 'inspect_artifacts' or not isinstance(pinned, dict)
            or {str(reader.resolve(path)): digest for path, digest in pinned.items()} != binding.get('artifacts')
            or assessment.artifacts != binding['artifacts']):
        raise ValueError('Review scope differs from the recorded delivery')
    claim = delivery_claim(request, sorted(binding['artifacts']), reader.resolve)
    if claim != binding.get('claim') or state['requests'] != binding.get('requests'):
        raise ValueError('Review claim differs from the recorded delivery request')
    if reader.record('session_manifest.json') != manifest or reader.record('actions.json') != state:
        raise ValueError('Task changed while reading the delivery review')
    return {'coverage': assessment.coverage, 'verdict': assessment.verdict,
        'limitations': assessment.limitations,
        **({'applicability': assessment.applicability} if assessment.applicability is not None else {}),
        **({'investigations': assessment.investigations} if assessment.investigations is not None else {}),
        'findings': [{'severity': row.severity, 'message': row.message} for row in assessment.findings],
        'record': journal['record'],
        'scope': 'Recorded model opinion at delivery time; not proof of current sources or scientific correctness.'}


class DeliveryReview:
    def __init__(self, workspace: Any, inspector: Any, *,
                 observations: Callable[[], list[dict]] | None = None,
                 protocol: str = 'task.v1', track_findings: bool = False,
                 include_package_feedback: bool = True) -> None:
        if protocol not in {'task.v1', 'task.v2', 'task.v3', 'task.v4', 'task.v5'}:
            raise ValueError('Unknown delivery review protocol')
        self.workspace, self.inspector = workspace, inspector
        self.observations = observations
        self.protocol = protocol
        self.track_findings = track_findings
        self.include_package_feedback = include_package_feedback

    def feedback_from_observation(self, observed: dict) -> dict | None:
        """Resolve original opinions once for revision and completion consumers."""
        envelope = (observed.get('metadata') or {}).get('delivery_review')
        if isinstance(envelope, dict) and self.include_package_feedback:
            accepted, accepted_result = None, None
            if self._pending_envelope(envelope):
                path = self.inspector._path(envelope['identity'], 'result')
                try:
                    raw = (self.inspector.store.read_json(path) if self.inspector.store.exists(path)
                           else envelope['result'])
                except _ERRORS:
                    raw = None
                accepted, intact, accepted_result = self._accepted_pending_opinion(observed, envelope, raw)
                if accepted is not True or not intact:
                    # A closed response can precede the action that accepts it.
                    # A previously accepted opinion may also have lost its
                    # original binding. Neither supplies guidance or a pass.
                    binding = envelope['binding']
                    return {'arguments': {}, 'opinion_available': False, 'opinion_accepted': accepted,
                        'delivery_context': deepcopy(binding), 'reviewed_subjects': {},
                        'reviewed_inventory': dict(binding.get('artifacts', {})),
                        'result': ToolResult(tool_name='inspect_artifacts', status='blocked',
                            error=('Independent opinion no longer matches its accepted record' if accepted is True
                                   else 'Independent opinion has no accepted action record' if accepted is False
                                   else 'Independent opinion acceptance cannot be verified')).model_dump(mode='json'),
                        **({'_accepted_result': accepted_result} if accepted_result is not None else {}),
                        'scope': 'Diagnostic only; the original review remains available for recovery.'}
            feedback = self.feedback_observation(envelope)
            if accepted is not None:
                feedback['opinion_accepted'] = accepted
                if not feedback.get('opinion_available') and accepted_result is not None:
                    feedback['_accepted_result'] = accepted_result
            return feedback
        if observed.get('tool_name') == 'inspect_artifacts':
            return self.inspector.feedback_from_observation(observed)
        return None

    def inspection_feedback(self, targets: list[str]) -> list[dict]:
        """Project original owner-verified opinions through the shared work-input port.

        Changed subjects keep guidance with explicit old/current versions, never
        acceptance. Source bodies remain in the original read observations.
        """
        roots = [self.workspace.resolve(path) for path in targets]
        selected = None
        result, concluded, repeated = [], set(), {}
        for observed in reversed(self.observations() if self.observations is not None else []):
            feedback = self.feedback_from_observation(observed)
            if feedback is None:
                continue
            if selected is None:
                selected = {path: self.workspace.artifact_hashes([str(path)])[str(path)]
                    for path in roots if path.is_file()}
            feedback.pop('_accepted_result', None)  # The original stays in its journal receipt.
            subjects = []
            targets_reviewed = feedback.pop('reviewed_subjects')
            inventory = feedback.pop('reviewed_inventory', {})
            package = (feedback.get('delivery_context') or {}).get('schema_version') == 'delivery_review.v3'
            metadata = feedback['result'].get('metadata') or {}
            assessments = metadata.get('assessments', [metadata.get('assessment')])
            signatures, duplicate_records = [], []
            for locator, digest in {**inventory, **targets_reviewed}.items():
                try:
                    subject = self.workspace.resolve(locator)
                except ValueError:
                    continue  # Old locators do not grant new access.
                copy = next((path for path, value in selected.items() if value == digest), None)
                has_guidance = any(finding.get('severity') in {'required', 'advisory'}
                    and locator in (finding.get('subjects') or {})
                    for assessment in assessments if isinstance(assessment, dict)
                    for finding in assessment.get('findings', []) if isinstance(finding, dict))
                # Revision can use advisory findings as well as required ones.
                # A later narrow pass cannot erase earlier guidance; duplicate
                # opinions retain their original receipts without repeated text.
                # A versioned envelope without a new scope or reassessment is
                # the same opinion; its schema tag alone is not new evidence.
                normalized = [{key:value for key,value in row.items() if key not in
                    {'schema_version', 'scope', 'reassessed_prior', 'review_id'}}
                    if isinstance(row, dict) and row.get('schema_version') in
                        {'artifact_assessment.v1', 'artifact_assessment.v2'}
                        and row.get('scope') is None and not row.get('reassessed_prior') else row
                    for row in assessments]
                signature = json.dumps([locator, digest, feedback.get('arguments'), normalized],
                    sort_keys=True, ensure_ascii=False) if has_guidance else None
                if signature in repeated:
                    duplicate_records.append(signature)
                    continue
                if (not package and (subject, digest) in concluded and not has_guidance or not (copy is not None or any(subject == root
                        or root.is_dir() and subject.is_relative_to(root) for root in roots))):
                    continue
                if signature is not None:
                    signatures.append(signature)
                current_path = copy or subject
                current = (self.workspace.artifact_hashes([str(current_path)])[str(current_path)]
                    if current_path.is_file() else None)
                subjects.append({'path': locator, 'reviewed_sha256': digest,
                    'selected_path': str(current_path), 'current_sha256': current,
                    'current_matches_review': current == digest,
                    'review_role': 'inspection_target' if locator in targets_reviewed else 'delivery_inventory_member'})
                conclusive = lambda row: row and row['coverage'] == 'complete' and row['verdict'] != 'incomplete'
                if (any(conclusive(row) and locator in row['subjects'] for row in assessments)
                        or inventory and assessments and all(conclusive(row) for row in assessments)):
                    concluded.add((subject, digest))
            # A local file opinion cannot retire a whole-claim opinion. Direct
            # answers and claim-level defects have no file scope at all, yet
            # remain relevant guidance for the next whole-work judgment.
            claim_findings = (feedback['result'].get('metadata') or {}).get('delivery_opinion', {}).get('findings', [])
            # A finding may join several files. Keep the original version and
            # current status of every related subject when any one is selected;
            # unrelated findings remain available in the retained receipt.
            selected_paths = {row['path'] for row in subjects}
            findings = [*claim_findings, *(finding for assessment in assessments
                if isinstance(assessment, dict) for finding in assessment.get('findings', [])
                if isinstance(finding, dict))]
            linked = set()
            for finding in findings:
                paths = set(finding.get('artifacts', finding.get('subjects', {})))
                if paths & selected_paths:
                    linked.update(paths)
            bindings = {**inventory, **targets_reviewed}
            for locator in sorted(linked - selected_paths):
                if locator not in bindings:
                    continue
                path, current = None, None
                try:
                    path = self.workspace.resolve(locator)
                    if path.is_file():
                        current = self.workspace.artifact_hashes([str(path)])[str(path)]
                except (ValueError, OSError):
                    pass  # An inaccessible related subject must not hide selected guidance.
                subjects.append({'path': locator, 'reviewed_sha256': bindings[locator],
                    'selected_path': str(path) if path is not None else None, 'current_sha256': current,
                    'current_matches_review': current == bindings[locator] if current is not None else None,
                    'review_role': 'related_subject'})
            record = 'records/' + observed['record'].removeprefix('attempts/')
            for signature in duplicate_records:
                references = repeated[signature].setdefault('related_opinion_records', [])
                if record not in references:
                    references.append(record)
            if subjects or package and (not inventory or not roots or any(not row['artifacts'] for row in claim_findings)):
                # An accepted independent opinion may live in a separate
                # action; embedded historical opinions remain in production.
                envelope = (observed.get('metadata') or {}).get('delivery_review')
                pending = isinstance(envelope, dict) and self._pending_envelope(envelope)
                source = self._opinion_record(observed.get('review_record') if pending else observed.get('record'))
                source_prefix = '' if pending or not isinstance(envelope, dict) else 'metadata.delivery_review.result.'
                view = self.inspector.feedback_view({**feedback, 'kind': 'inspection_feedback',
                    'subjects': subjects, 'record': record,
                    **({'source_record': source, 'source_prefix': source_prefix}
                       if source and feedback.get('opinion_available') else {})})
                result.append(view)
                for signature in signatures:
                    repeated[signature] = view
        return result

    def _validate_scope(self, binding: dict) -> None:
        if (applicable_delivery_scope(binding) != (self.protocol in {'task.v4', 'task.v5'})
                or self.protocol in {'task.v4', 'task.v5'} and binding.get('scope_policy') != (
                    'applicable.v2' if self.protocol == 'task.v5' else 'applicable.v1')):
            raise ValueError('Delivery scope policy differs from the saved review protocol')

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

    def _accepted_pending_opinion(self, observed: dict, envelope: dict, raw: dict | None
                                  ) -> tuple[bool | None, bool, dict | None]:
        """Prove historical acceptance without relying on current file versions."""
        record = observed.get('review_record')
        if not isinstance(record, str) or not record:
            return False, False, None
        try:
            from simple_ar.tools.professional_outcomes import accepted_scoped_opinion
            if (Path(observed.get('record', '')).name != 'native_result.json'
                    or Path(observed['record']).parent.name != envelope['identity']):
                return None, False, None
            root = self.inspector.store.root
            session_id = self.inspector.store.read_json('session_manifest.json')['session_id']
            _, accepted_raw, _ = accepted_scoped_opinion(root, self.workspace, session_id,
                record, envelope['identity'])
            if not isinstance(raw, dict) or accepted_raw != raw:
                return True, False, accepted_raw
            try:
                self._pending_input(envelope)
            except _ERRORS:
                return True, False, accepted_raw
            return True, True, accepted_raw
        except _ERRORS:
            # A recorded action whose proof is unreadable is unknown, not an
            # assertion that the opinion was never accepted.
            return None, False, None

    def _accepted_opinion_is_unrelated(self, raw: dict | None, binding: dict,
                                       selected: set[str], prior_events: list[dict]) -> bool:
        """Skip damaged history only when its accepted opinion proves no relevant concern."""
        try:
            assessment = _DeliveryAssessment.model_validate((raw.get('metadata') or {}).get('delivery_assessment'))
            if assessment.artifacts != binding['artifacts']:
                return False
            if assessment.reassessed_prior:
                known = {finding.get('finding_id') or finding_id(event, finding)
                    for event in prior_events for finding in event['findings']
                    if finding['severity'] == 'required'}
                if not set(assessment.reassessed_prior) <= known:
                    return False  # An unknown original concern cannot be classified as unrelated.
                _, pending = assessment_state(prior_events, resolve=self.workspace.resolve,
                    require_explicit_reassessment=True)
                relevant = {identity for path, findings in pending.items()
                    if path is None or path in selected for identity in findings}
                if relevant.intersection(assessment.reassessed_prior):
                    return False
            return all(row.subjects and not self._relevant(row.subjects, selected)
                       for row in assessment.findings if row.severity == 'required')
        except _ERRORS + (AttributeError,):
            return False

    def _accepted_package(self, observed: dict, envelope: dict, raw: dict, *,
                          accepted_pending: bool = False) -> tuple[bool | None, bool]:
        """Separate accepted history from applicability to the current bytes."""
        pending = self._pending_envelope(envelope)
        if pending and not accepted_pending:
            accepted, intact, _ = self._accepted_pending_opinion(observed, envelope, raw)
            if accepted is not True or not intact:
                return accepted, False
        try:
            result = self._verified(envelope['identity'], envelope['binding'], None if pending else envelope)
            return True, result.model_dump(mode='json') == raw
        except _ERRORS:
            # A previously accepted result can remain useful guidance after
            # sources change, without granting current retirement authority.
            return True, False

    def _reassessment_events(self, identity: str, scope: str, assessment: _DeliveryAssessment,
                             prior_events: list[dict], *, current: bool) -> list[dict]:
        """Project completed individual rechecks without completing the package.

        The inspector only emits IDs after validating selected evidence supply;
        new subject-scoped work also binds that evidence to pending subjects.
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
                feedback = None
                if self._pending_envelope(envelope):
                    # Establish whether a historical action was accepted before
                    # interpreting its mutable owner result or skipping a scope.
                    feedback = self.feedback_from_observation(observed)
                    if feedback.get('opinion_accepted') is False:
                        continue
                    if not feedback.get('opinion_available'):
                        if self._accepted_opinion_is_unrelated(feedback.get('_accepted_result'), binding, selected, events):
                            continue
                        raise ValueError('Retained delivery concern opinion cannot be verified')
                raw = (self.inspector.store.read_json(self.inspector._path(identity, 'result'))
                       if self._pending_envelope(envelope)
                       and self.inspector.store.exists(self.inspector._path(identity, 'result'))
                       else envelope['result'])
                value = (raw.get('metadata') or {}).get('delivery_assessment')
                if value is None:
                    continue  # Pending/failed work has no independent opinion.
                assessment = _DeliveryAssessment.model_validate(value)
                if raw.get('status') != 'ok' and not any(
                        row.severity == 'required' for row in assessment.findings):
                    # A rejected or interrupted response with no accepted
                    # finding is diagnostic, not an opinion that can block a
                    # fresh review. Earlier verified concerns remain in events.
                    continue
                if not (self._relevant(assessment.artifacts, selected)
                        or any(not row.subjects for row in assessment.findings)
                        or assessment.reassessed_prior):
                    continue
                feedback = feedback or self.feedback_from_observation(observed)
                if feedback.get('opinion_accepted') is False:
                    continue  # A closed but unaccepted response is not an opinion.
                if not feedback.get('opinion_available'):
                    raise ValueError('Retained delivery concern opinion cannot be verified')
                if assessment.artifacts != binding['artifacts']:
                    raise ValueError('Delivery concern inventory differs from its original scope')
                self._reassessed_ids(identity, assessment)
                accepted, applicable = self._accepted_package(observed, envelope, raw,
                    accepted_pending=feedback.get('opinion_accepted') is True)
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
            feedback = self.feedback_from_observation(observed)
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
        if binding.get('purpose') == 'candidate':
            return {'goal': (
                'Investigate the supplied candidate against the original requirements, checking '
                'the actual available artifacts and necessary original evidence. This is an early '
                'candidate investigation, not final delivery acceptance. Requested outputs not yet '
                'produced are unfinished work, not a reason to defer checks already possible. '
                'Report consequential defects, evidence gaps and limits for the current versions; '
                'do not certify missing publication or overall completion. ' + _APPLICABLE_GOAL),
                'targets': [], 'evidence': []}
        if applicable_delivery_scope(binding):
            return {'goal': _APPLICABLE_GOAL, 'targets': [], 'evidence': []}
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
        self._validate_scope(binding)
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
        self._validate_scope(binding)
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
            self._validate_scope(binding)
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
            retained = None
            for observed in reversed(observations):
                candidate = (observed.get('metadata') or {}).get('delivery_review')
                if (retained is None and getattr(self.inspector, 'material_policy', None) == 'operations.v3'
                        and isinstance(candidate, dict) and isinstance(candidate.get('identity'), str)
                        and isinstance(candidate.get('binding'), dict)
                        and self.inspector.store.exists(self.inspector._path(candidate['identity'], 'result'))):
                    retained = {'identity':candidate['identity'], 'binding':candidate['binding']}
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
                **({'retained_from':retained} if retained is not None else {}),
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
            self._validate_scope(binding)
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
                     if self.protocol in {'task.v3', 'task.v4', 'task.v5'} else
                     'Current canonical delivery lacks a complete independent whole-delivery review')]
        except _ERRORS as exc:
            return ['Delivery review cannot be verified: ' + str(exc)]
