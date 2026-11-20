"""Review gating, repair history, and publication handoff helpers for WritingToolGateway."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.report.composition_receipts import (
    check_composition_inputs,
    composition_owner,
    read_composition_candidate,
    read_repair_review,
)
from simple_ar.tools.professional_outcomes import accepted_native_result, stop_decision
from simple_ar.tools.publication import ExportRequest
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.writing_contracts import (
    ComposeRequest,
    DocumentWorkRequest,
    _DOCUMENT_WORK_CONTRACTS,
)


class _WritingReviewMixin:
    """Review-gating, delivery checking, and publication suspension methods."""

    def _author_completion_gap(self, observed: dict, attempt_id: str) -> dict | None:
        """A proved, inspected author may still fail the output contract.

        This is working context, never adoption or permission to retry an
        operation. Check the actual output, not exception text or a caller's
        description of its failure. Missing author/reviewer responses retain
        their original stop and accounting semantics.
        """
        if observed.get('status') == 'ok' or not observed.get('data', {}).get('incomplete'):
            return None
        accepted_native_result(self.root, self.session_id, observed, attempt_id)
        store = ArtifactStore(self.root / 'tools/writing' / attempt_id)
        binding = store.read_json('request.json')
        requirement = binding.get('report_binding', {}).get('requirement')
        method = (requirement or {}).get('method') or binding['request'].get('writing_method')
        if method != 'sections':
            return None
        request = ComposeRequest(goal=binding['request']['goal'],
            prior_result=f'records/{attempt_id}/native_result.json')
        _, _, linkage, source = self._continued_document_inputs(request,
            attempt_id + '-proof', requirement, inspect_only=False, allow_standalone=True)
        checkpoint = store.read_json('writer/sections.json')['completed']
        if not checkpoint.get('document_review_done') or checkpoint.get('pending_draft') is not None:
            return None
        # Unlike check-only recovery, this handoff must not outlive an
        # unfinished inspection operation, even if an older body was reviewed.
        if any(str(Path(name).with_name('response.json')).replace('\\', '/') not in source['sha256']
                for name in source['sha256'] if name.startswith('writer/model_calls/') and name.endswith('/request.json')):
            return None
        from simple_ar.report.writing import retained_section_candidate, validate_document_inspection, validate_output_outline, _review_pending
        candidate = retained_section_candidate(source)
        review = (candidate.get('checked_revision', {}).get('independent_review')
            or store.read_json('writer/writer.json').get('independent_review'))
        # Negative content findings already have their own work/repair path.
        # This port only classifies a separately proved output-contract defect.
        if (not isinstance(review, dict) or review.get('review_status') != 'reviewed'
                or self._review_needs_material_work(review)):
            return None
        from simple_ar.report.agent import _final_sequence
        from simple_ar.report.schema import ReportMemory, ReportSectionDraft, ReportSectionReview
        sections = [row.model_dump(mode='json') for row in _final_sequence(
            ReportMemory.model_validate(candidate['memory']).section_plan,
            [ReportSectionDraft.model_validate(row) for row in candidate['sections']])]
        validate_document_inspection(store, binding, {'delivery_review': review.get('delivery_review')},
            review_result=review, reviewed_sections=sections)
        if _review_pending([ReportSectionReview.model_validate(row) for row in review['section_reviews']]):
            return None
        outline = (requirement or {}).get('outline') or [
            {'heading': heading, 'required': True} for heading in binding['request'].get('required_headings', [])]
        if not outline:
            return None
        from simple_ar.report.assembler import assemble_report_sections, ReportAssemblyMode
        body = assemble_report_sections(title=binding['request']['title'],
            sections=[ReportSectionDraft.model_validate(row) for row in sections],
            assembly_mode=ReportAssemblyMode.SECTION_DRAFTS)
        try:
            validate_output_outline(outline, body)
        except ValueError as error:
            return {'contract': 'author_completion_gap.v1', 'adopted': False,
                'prior_result': f'records/{attempt_id}/native_result.json',
                'source_sha256': linkage['result_sha256'], 'output_outline': outline,
                'diagnostic': str(error),
                'scope': 'Complete author and original inspection are proved; this output still violates '
                    'its bound structure. Use the retained body as revision context under the requested '
                    'structure, then independently inspect the new work. The original failure remains failed; '
                    'no automatic retry, inherited approval or additional allowance.'}
        return None

    def can_return_gap(self, observed: dict, attempt_id: str) -> bool:
        """Completed inspections and proved output gaps can inform the owner.

        This never adopts the draft, retries the Writer or authorizes repairs.
        Unavailable/incomplete responses cannot become a semantic handoff.
        """
        if (observed.get('tool_name') != 'compose_document'
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id)):
            return False
        try:
            if observed.get('status') != 'ok' or observed.get('error'):
                # Read a legacy assembly failure only through its registered
                # receipt and the saved complete negative author/check proof.
                if observed.get('data', {}).get('failure_kind') != 'image_reference':
                    return self._author_completion_gap(observed, attempt_id) is not None
                accepted_native_result(self.root, self.session_id, observed, attempt_id)
                from simple_ar.report.writing import checked_writing_candidate
                store = ArtifactStore(self.root / 'tools/writing' / attempt_id)
                binding = store.read_json('request.json')
                checked_writing_candidate(store, binding)
                self._check_input_bytes(binding['inputs'])
                self.workspace.check_artifact_producers(row['path'] for row in binding['inputs'])
                return True
            store, binding, data = self._composition_receipt(attempt_id, observed)
            if data.get('editable_markdown') == f"{data['version']}/candidate.md":
                self._check_input_bytes(data['inputs'])
                self.workspace.check_artifact_producers(row['path'] for row in data['inputs'])
                # This is work, not delivery of a newly certified copy package.
                # Current source/copy bytes are bound above; final package and
                # copy-review authority remain check_delivery's responsibility.
                return True  # _composition_receipt proved the complete negative work, even with zero rounds.
            if self._inspection_interrupted(data):
                return False
            review = self.repair_review(data)
            if review.get('review_status') != 'needs_revision':
                return False
            candidate = self.repair_candidate(data)
            checked_revision = False
            if binding.get('section_delivery_review_contract') and not self._review_needs_material_work(review):
                # A consumed local correction is not a failed investigation.
                # Return only real progress with its complete negative proof,
                # never an unchanged draft or an unchecked/unknown response.
                _, authored = self._section_author_source(store)
                checked = authored.get('checked_revision')
                if checked is None:
                    return False
                review = checked['independent_review']
                candidate = authored
                checked_revision = True
            self._check_input_bytes(data['inputs'])
            self._check_document_copy_review(data)
            from simple_ar.report.writing import validate_document_inspection
            if not checked_revision:
                validate_document_inspection(store, binding, {**data,
                    'delivery_review': review['delivery_review']}, review_result=review,
                    reviewed_sections=candidate['sections'] if candidate else None)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def repair_candidate(self, data: dict) -> dict | None:
        return read_composition_candidate(self.workspace, data)

    def repair_review(self, data: dict) -> dict:
        return read_repair_review(self.workspace, data)

    @staticmethod
    def _review_needs_material_work(review: dict) -> bool:
        from simple_ar.report.review_evidence import attachment_review_blocks_editing
        return bool(attachment_review_blocks_editing(review.get('delivery_review', {}))
            or any(section.get('context_requests') or any(
                finding.get('required_action') == 'verify'
                or finding.get('type') in {'evidence_gap', 'source_verification_incomplete'}
                or finding.get('delivery_targets') for finding in section.get('findings', []))
                for section in review.get('section_reviews', [])))

    @staticmethod
    def _inspection_interrupted(data: dict) -> bool:
        """An old negative review does not complete an interrupted candidate check."""
        return any(row.get('type') in {'document_review_unavailable', 'document_revision_unavailable'}
            for row in data.get('unresolved', []) if isinstance(row, dict))

    def stop_after_review(self, observed: dict, attempt_id: str) -> dict | None:
        """Keep admitted investigation and writing outcomes with their owner.

        The parent first offers a proven producer repair. This fallback never
        converts a defective review into a completed delivery or a new model turn.
        """
        if (observed.get('tool_name') != 'compose_document'
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id)):
            return None
        try:
            store = composition_owner(root=self.root, workspace=self.workspace,
                session_id=self.session_id, identity=attempt_id)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        composed = store.exists('request.json')
        if not composed and (not store.exists('selection-input.json')
                or store.read_json('selection-input.json').get('document_work') != 'document_work.v2'):
            return None  # Rejected preflight/old selection contracts remain unchanged.
        data = observed.get('data') or {}
        reason, detail = None, None
        if observed.get('status') != 'ok' or observed.get('error'):
            reason = data.get('owner_stop_reason') or {'resource_exhausted': 'shared_budget_exhausted',
                'accounting_unresolved': 'accounting_unresolved'}.get(data.get('failure_kind'), 'owner_failed')
            detail = data.get('next_action') or observed.get('error') or 'Writing owner did not complete'
            if data.get('failure_kind') == 'image_reference' and self.can_return_gap(observed, attempt_id):
                reason, detail = 'work_gap', ('Saved complete candidate has a negative inspection; '
                    'legacy publication of the unadopted original failed. Preserve both; no replay or resource grant.')
        elif (data.get('incomplete') or data.get('unresolved')
                or data.get('delivery_review', {}).get('status', 'reviewed') != 'reviewed'):
            reason, detail = 'work_gap', 'Writing review retains unresolved findings; no proven producer continuation is available'
        elif not composed:
            reason, detail = 'owner_state_unresolved', 'Admitted material investigation has no bound composition; preserve its decisions and accounting'
        if composed and observed.get('status') == 'ok' and not observed.get('error'):
            try:
                store, binding, retained = self._composition_receipt(attempt_id, observed)
                review = self.final_review(retained)
                if review.get('review_status') != 'reviewed':
                    reason, detail = 'work_gap', 'Writing review retains unresolved findings; no proven producer continuation is available'
                elif reason is None:
                    self._check_bound_delivery(store, binding, retained, review=review)
                    if binding['request'].get('publication'):
                        reason, detail = 'owner_state_unresolved', 'Requested publication has no validated export continuation'
            except (OSError, ValueError, KeyError, TypeError):
                reason, detail = 'owner_state_unresolved', 'Writing receipt or current review cannot be validated; preserve retained work'
        if (reason == 'work_gap' and self._review_capacity() is False
                and self._budget_review_action(observed, attempt_id) is not None):
            reason, detail = 'shared_budget_exhausted', (
                'A complete article revision awaits independent inspection, but current shared '
                'LLM request/token capacity is exhausted; explicit additional resources are required')
        if reason:
            return stop_decision(self, observed, attempt_id, reason, detail)
        return None

    def _review_ledgers(self):
        """One ledger in current tasks; older writing may have a separate owner."""
        return tuple(dict.fromkeys(ledger for ledger in (self.ledger, getattr(self.workspace, 'ledger', None))
                                   if ledger is not None))

    def _review_capacity(self) -> bool | None:
        """Read shared/owner accounting; None means continuation is unsafe."""
        if self.ledger is None:
            return None
        ledgers = self._review_ledgers()
        if any(row.status in {'reserved', 'unknown'}
                for ledger in ledgers for row in ledger.entries):
            return None
        return all(remaining is None or remaining >= 1
            for ledger in ledgers
            for resource in ('llm_requests', 'total_tokens')
            for remaining in (ledger.remaining(resource),))

    def _budget_review_action(self, observed: dict, attempt_id: str, *,
                              automatic: bool = False) -> dict | None:
        """Prove one retained pending revision and its unchanged original recipe.

        This only describes work. The application owns the explicit new grant;
        neither this proof nor its callers reserve resources or write receipts.
        """
        data = observed.get('data') or {}
        if (not self.document_work
                or observed.get('tool_name') != 'compose_document'
                or not (self._inspection_interrupted(data)
                    or observed.get('status') == 'error' and data.get('incomplete'))):
            return None
        try:
            from simple_ar.report.writing import retained_article_candidate, article_inspection_enabled, professional_writer_config
            if observed.get('status') == 'ok':
                store, binding, data = self._composition_receipt(attempt_id, observed)
            else:
                store = composition_owner(root=self.root, workspace=self.workspace,
                    session_id=self.session_id, identity=attempt_id)
                binding = store.read_json('request.json')
            accepted_native_result(self.root, self.session_id, observed, attempt_id)
            if (binding.get('document_work', {}).get('contract') not in _DOCUMENT_WORK_CONTRACTS
                    or not article_inspection_enabled(professional_writer_config(binding))):
                return None
            method = binding.get('report_binding', {}).get('requirement', {}).get('method')
            if automatic and (binding.get('continued_source_review') is not None or method != 'sections'):
                return None
            if method == 'sections':
                source, candidate = self._section_author_source(store, author_only=True)
                self._unstarted_section_inspection(store, candidate)
            else:
                source = retained_article_candidate(store, binding, unchecked_revision=True)
            saved = binding['request']
            arguments = {'prior_result': f'records/{attempt_id}/native_result.json',
                'goal': saved['goal'], 'finish': saved.get('finish', False)}
            if saved.get('publication') is not None:
                arguments['publication'] = saved['publication']
            current = self.report_inputs() if self.report_inputs else None
            if current != binding.get('report_binding'):
                return None
            resolved = self._resolve_document_inputs(ComposeRequest.model_validate(arguments),
                attempt_id + '-inspection', report=current, original=None, selection=None,
                document_work=True, caller_fields=frozenset(arguments), work_source=None,
                normalize_arguments=None)
            if resolved.candidate != source or resolved.binding != binding:
                return None
            if observed.get('status') == 'ok':
                self._check_document_copy_review(data)
            return {'tool_name': 'compose_document', 'arguments': arguments}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _unstarted_section_inspection(self, store: ArtifactStore, candidate: dict) -> None:
        """Prove a never-dispatched check, not merely a missing response.

        Atomic author calls must have matching settled provider accounting.
        The exact pending inspection must have no reservation in either ledger.
        Any completed check of this body wins over a stale checkpoint. This
        reads the existing journal only; it cannot release or replay a call.
        """
        if self._review_capacity() is None:
            raise ValueError('Pending inspection has unresolved or unavailable accounting')
        entries = [entry for ledger in self._review_ledgers() for entry in ledger.entries]
        expected = {row['section_id']: {'section_id': row['section_id'],
            'heading': row['heading'], 'markdown': row['draft_markdown']} for row in candidate['sections']}
        pending, accounted_author = 0, False
        for path in (store.root / 'writer/model_calls').glob('*/request.json'):
            request = json.loads(path.read_bytes())
            label = request.get('options', {}).get('label', '')
            logical = request.get('options', {}).get('budget_call_id',
                f'{store.root.name}:writing:{path.parent.name}')
            calls = [entry for entry in entries if entry.logical_call_id == logical]
            if label.startswith(('report-writer', 'report-integrator', 'report-reviser',
                    'report-document-reviser', 'report-document-joint-reviser')):
                accounted_author = accounted_author or any(entry.status == 'settled'
                    and entry.session_id == self.session_id and entry.attempt_id == store.root.name
                    and entry.actual_source == 'provider' and entry.actual.get('llm_requests', 0) >= 1
                    for entry in calls)
            elif label in {'report-document-reviewer', 'report-document-verifier'}:
                payload, _ = json.JSONDecoder().raw_decode(request['prompt'][request['prompt'].index('{'):])
                rows = payload.get('sections', [])
                matches = (len(rows) == len(expected)
                    and {row['section_id']: {key: row[key] for key in (
                        'section_id', 'heading', 'markdown')} for row in rows} == expected)
                if path.with_name('response.json').exists():
                    if matches:
                        raise ValueError('Complete response already exists for this candidate inspection')
                else:
                    if (not matches or calls or request.get('response_record_contract') != 'atomic_complete.v2'
                            or request.get('input_snapshot') != store.read_json('writer/report_inputs.json')['snapshot_id']):
                        raise ValueError('Pending inspection is not a proved unstarted check of this author')
                    pending += 1
        if pending != 1 or not accounted_author:
            raise ValueError('Inspection continuation requires one unstarted check and accounted complete author')

    def _unstarted_inspection_action(self, observed: dict, attempt_id: str) -> dict | None:
        """Continue proved author work once; failed check-only work stays stopped."""
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id)
                or self._review_capacity() is not True):
            return None
        return self._budget_review_action(observed, attempt_id, automatic=True)

    def resume_after_budget(self, observed: dict, attempt_id: str) -> dict | None:
        """Propose inspection only after the app grants additional resources."""
        return self._budget_review_action(observed, attempt_id) if self._review_capacity() is True else None

    def _check_bound_delivery(self, store: ArtifactStore, binding: dict, data: dict, *,
                              review: dict | None = None, independent: bool = False) -> None:
        if (not data.get('editable_markdown')
                or data['editable_markdown'] == f"{data.get('version')}/candidate.md"):
            raise ValueError('Unresolved work is not an adopted canonical delivery')
        from simple_ar.report.export import missing_report_images
        missing = missing_report_images(self.workspace.resolve(data['editable_markdown']).read_text(encoding='utf-8'),
            binding.get('delivery_review', {}).get('required_figures', []))
        if missing:
            raise ValueError('Declared report figures are absent from the manuscript: ' + ', '.join(missing))
        if data.get('delivery_review', {}).get('status', 'reviewed') != 'reviewed':
            raise ValueError('Joint delivery inspection has unresolved or uninspected attachments')
        snapshot = binding.get('delivery_review', {})
        if snapshot.get('producer_completion') == 'goal_findings.v1':
            from simple_ar.tools.producer_declarations import freeze_producer_context
            context = freeze_producer_context(self, snapshot['artifacts'])
            if any(snapshot.get(key) != value for key, value in context.items()):
                raise ValueError('Original producer notes or their source versions changed')
        if independent or snapshot.get('producer_completion') == 'goal_findings.v1':
            review = self.final_review(data) if review is None else review
            if review.get('review_status') != 'reviewed' or data.get('unresolved') or data.get('incomplete'):
                raise ValueError('Goal/findings delivery requires a current completed independent review')
            from simple_ar.report.writing import validate_document_inspection
            validate_document_inspection(store, binding, data, review_result=review)
        self._check_input_bytes(data['inputs'])
        self._check_document_copy_review(data)
        self.workspace.check_artifact_producers(row['path'] for row in data['inputs'])

    def _check_document_copy_review(self, data: dict) -> None:
        """Check the exact copy relationship shown to Writer and reviewer.

        Callers already validate current input/artifact bytes. No new hash
        scope or source role is inferred from content equality here.
        """
        work = data.get('document_work', {})
        if 'producer_units' in work:
            from simple_ar.tools.producer_declarations import professional_units
            units = professional_units(self, [], [], retained=work['producer_units'])
            for unit in units:
                if any(data['artifact_sha256'].get(path) != digest for path, digest in unit['files'].items()):
                    raise ValueError('Delivery omits part of its registered professional unit')
        if work.get('contract') != 'document_work.v2':
            return
        from simple_ar.report.review_evidence import delivery_subject_view
        expected = {}
        for row in [*work['materials'], *[{'source': row['path'],
                'path': f"{data['version']}/{row['manuscript_path']}", 'sha256': row['sha256']}
                for row in data['inputs'] if 'manuscript_path' in row]]:
            target = self.workspace.resolve(row['path'])
            binding = (self.workspace.resolve(row['source']), row['sha256'])
            if target in expected and expected[target] != binding:
                raise ValueError('Document copy has conflicting declared origins')
            expected[target] = binding
            if data['artifact_sha256'].get(row['path']) != row['sha256']:
                raise ValueError('Delivered copy is not a current canonical artifact')
        inputs = self.workspace.resolved_versions({row['path']: row['sha256'] for row in data['inputs']
            if row.get('sha256') is not None})
        actual = {}
        for row in data.get('delivery_review', {}).get('artifacts', []):
            source = self.workspace.resolve(row['path'])
            for copy in delivery_subject_view(row, coverage=True).get('copies', []):
                target = self.workspace.resolve(copy['path'])
                binding = (source, copy['sha256'])
                if target in actual and actual[target] != binding:
                    raise ValueError('A delivery copy aliases multiple inspected subjects')
                if inputs.get(source) != copy['sha256'] or inputs.get(target) != copy['sha256']:
                    raise ValueError('Copy review has no matching original and copy input versions')
                actual[target] = binding
        if actual != expected:
            raise ValueError('Joint review copy mapping differs from the complete document package')

    def delivery_adoptions(self, observations: list[dict], delivery: dict | None,
                           proposed: Any = None) -> list[dict]:
        """Read-only adoption proof from this delivery's actual joint review."""
        from simple_ar.tools.producer_declarations import delivery_adoptions
        return delivery_adoptions(self, observations, delivery, proposed,
            check_publication=self.check_publication_delivery)

    def check_delivery_set(self, identity: dict, artifacts: list[str],
                           publication_artifacts: set[Path]) -> None:
        """Check extras against the already-validated composition identity.

        Publication derivatives are supplied by their verified owner. Only
        explicitly bound original figures may alias canonical figure copies;
        equal content alone never gives an arbitrary file a scientific role.
        """
        version = identity.get('composition', '')
        match = re.fullmatch(r'workspace/manuscripts/([A-Za-z0-9][A-Za-z0-9_-]{0,100})', version)
        if match is None:
            raise ValueError('Joint delivery set requires the validated canonical composition identity')
        store = ArtifactStore(self.root / 'tools/writing' / match[1])
        receipt = store.resolve('result.json').read_bytes()
        data = json.loads(receipt)['data']
        if (identity.get('artifact_sha256') != data.get('artifact_sha256')
                or identity.get('receipt_sha256') != hashlib.sha256(receipt).hexdigest()):
            raise ValueError('Canonical composition identity changed before checking delivery extras')
        delivered = {self.workspace.resolve(path) for path in artifacts}
        for unit in data.get('document_work', {}).get('producer_units', []):
            if not {self.workspace.resolve(path) for path in unit['files']} <= delivered:
                raise ValueError('Deliver the complete registered professional unit, not a selected subset')
        allowed = {self.workspace.resolve(path): digest for path, digest in data['artifact_sha256'].items()}
        request = store.read_json('request.json')
        for row in request['inputs']:
            if 'manuscript_path' in row:
                copied = f"{version}/{row['manuscript_path']}"
                if data['artifact_sha256'].get(copied) == row.get('sha256'):
                    allowed[self.workspace.resolve(row['path'])] = row['sha256']
        inspection = data.get('delivery_review', {})
        if inspection.get('status') == 'reviewed':
            from simple_ar.report.review_evidence import delivery_target_pointer
            for index, row in enumerate(request['delivery_review']['artifacts']):
                if delivery_target_pointer(row, index) is not None:
                    allowed[self.workspace.resolve(row['path'])] = row['sha256']
                    for copy in row.get('copies', []):
                        allowed[self.workspace.resolve(copy['path'])] = copy['sha256']
        publication = {path.resolve() for path in publication_artifacts}
        for locator in artifacts:
            path = self.workspace.resolve(locator)
            if path in publication:
                continue
            expected = allowed.get(path)
            if expected is None:
                raise ValueError(f'Delivery attachment lacks same-version joint inspection: {locator}')
            if self.workspace.artifact_hashes([locator])[locator] != expected:
                raise ValueError(f'Delivery attachment changed after inspection: {locator}')

    def _same_delivery_producer(self, left: dict | None, right: dict | None) -> bool:
        if left is None or right is None:
            return left is right
        fields = {'inputs', 'outputs', 'receipt'}
        return ({key: value for key, value in left.items() if key not in fields}
                == {key: value for key, value in right.items() if key not in fields}
            and (self.root / left['receipt']).resolve() == (self.root / right['receipt']).resolve()
            and all(self.workspace.resolved_versions(left[name]) == self.workspace.resolved_versions(right[name])
                    for name in ('inputs', 'outputs')))

    def _same_delivery_snapshot(self, left: dict, right: dict) -> bool:
        history = {'artifacts', 'prior_defects', 'prior_defect_refs'}
        if ({key: value for key, value in left.items() if key not in history}
                != {key: value for key, value in right.items() if key not in history}
                or len(left['artifacts']) != len(right['artifacts'])):
            return False
        for a, b in zip(left['artifacts'], right['artifacts']):
            excluded = {'path', 'producer', 'consumers'}
            if (self.workspace.resolve(a['path']) != self.workspace.resolve(b['path'])
                    or {key: value for key, value in a.items() if key not in excluded}
                        != {key: value for key, value in b.items() if key not in excluded}
                    or not self._same_delivery_producer(a['producer'], b['producer'])):
                return False
            owners_a, owners_b = a.get('consumers', []), b.get('consumers', [])
            if (any(not any(self._same_delivery_producer(owner, other) for other in owners_b) for owner in owners_a)
                    or any(not any(self._same_delivery_producer(owner, other) for other in owners_a) for owner in owners_b)):
                return False
        return True

    def _repair_review_history(self, snapshot: dict, previous: dict, repair: dict) -> None:
        """Reference historical diagnoses; inspect only the actual current subjects."""
        from simple_ar.report.review_evidence import delivery_target_pointer, SCOPED_DELIVERY_INSPECTION
        scoped = snapshot.get('text_admission', {}).get('contract') == SCOPED_DELIVERY_INSPECTION
        current = {self.workspace.resolve(row['path']): (index, row)
            for index, row in enumerate(snapshot['artifacts'])}
        old_project = self.workspace.resolve(repair['historical_project'])
        new_project = self.workspace.resolve(repair['current_project'])
        refs, unchanged = [], []
        review = self.repair_review(previous)
        pending = previous.get('pending_candidate')
        diagnosis_ref = pending or repair['review_ref']
        diagnosis_sha = (previous['artifact_sha256'][pending] if pending else repair['review_sha256'])
        diagnosis_pointer = '/independent_review' if pending else '/data'
        for number, defect in enumerate(review.get('delivery_review', {}).get('defects', [])):
            finding = defect['finding']
            reference = {'review_ref': diagnosis_ref, 'review_sha256': diagnosis_sha,
                'pointer': f'{diagnosis_pointer}/delivery_review/defects/{number}/finding',
                'finding': {key: finding[key] for key in ('finding_id', 'type', 'severity', 'required_action') if key in finding},
                'targets': [{'path': row['path'], 'sha256': row['sha256']} for row in defect['targets']],
                'current_targets': []}
            if len(finding.get('message', '')) <= 1200:
                reference['finding']['message'] = finding.get('message', '')
            same, pointers = [], []
            for target in defect['targets']:
                path = self.workspace.resolve(target['path'])
                if path.is_relative_to(old_project):
                    path = self.workspace.resolve(str(new_project / path.relative_to(old_project)))
                if path not in current:
                    raise ValueError('Repaired composition omitted a previously accused delivery subject')
                index, row = current[path]
                reference['current_targets'].append({'path': row['path'], 'sha256': row['sha256']})
                if row['sha256'] == target['sha256']:
                    pointer = delivery_target_pointer(row, index) if scoped else f'/delivery_artifacts/{index}/text'
                    if pointer is None:
                        raise ValueError('Unchanged defective subject has no current inspection view')
                    same.append({key: row[key] for key in ('path', 'sha256', 'producer', 'consumers') if key in row})
                    pointers.append(pointer)
            refs.append(reference)
            if same and finding.get('delivery_targets'):
                # Unchanged defective bytes remain blocking even after relocation.
                # The complete original finding/quotes stay at the bound review;
                # this is retained authority, not a new reviewer opinion.
                unchanged.append({'type': defect['type'], 'targets': same,
                    'finding': {**reference['finding'], 'delivery_targets': pointers}, 'finding_ref': reference})
        snapshot['prior_defect_refs'] = refs
        snapshot['prior_defects'] = unchanged

    def _document_copy_review_history(self, snapshot: dict, previous: dict,
                                      prior_work: dict | None, current_work: dict,
                                      repair: dict | None = None) -> None:
        """Carry unchanged copy defects along the already-bound source recipe.

        Preserve the original finding and add its current subject view. Equal
        hashes alone never relate unrelated sources or transfer process roles.
        """
        if not prior_work or prior_work.get('contract') not in _DOCUMENT_WORK_CONTRACTS:
            return
        if previous.get('document_work') != prior_work:
            raise ValueError('Prior document material recipe differs from its result')
        old_copies = {self.workspace.resolve(row['path']): row for row in prior_work['materials']}
        old_sources = {self.workspace.resolve(row['source']): row for row in prior_work['materials']}
        current = {self.workspace.resolve(row['path']): (index, row)
            for index, row in enumerate(snapshot['artifacts'])}
        for index, row in enumerate(snapshot['artifacts']):
            for copy in row.get('copies', []):
                current[self.workspace.resolve(copy['path'])] = (index, row)
        prior = list(snapshot.get('prior_defects', []))
        snapshot['prior_defects'] = prior
        for defect in previous.get('delivery_review', {}).get('defects', []):
            if not defect.get('finding', {}).get('delivery_targets'):
                continue  # Historical inferred findings gain no target authority.
            same, pointers = [], []
            for target in defect.get('targets', []):
                target_path = self.workspace.resolve(target['path'])
                old = old_copies.get(target_path) or old_sources.get(target_path)
                if old is None or target['sha256'] != old['sha256']:
                    continue
                if (previous['artifact_sha256'].get(old['path']) != old['sha256']
                        or self.workspace.artifact_hashes([old['path']])[old['path']] != old['sha256']):
                    raise ValueError('Prior defective material copy changed after its review')
                source = self.workspace.resolve(old['source'])
                if repair:
                    historical = self.workspace.resolve(repair['historical_project'])
                    if source.is_relative_to(historical):
                        source = self.workspace.resolve(str(self.workspace.resolve(repair['current_project'])
                            / source.relative_to(historical)))
                matches = [row for row in current_work['materials']
                    if self.workspace.resolve(row['source']) == source and row['sha256'] == old['sha256']]
                if len(matches) > 1:
                    raise ValueError('Ambiguous current document copy for a prior defective source')
                if not matches:
                    continue  # New bytes require the current independent review.
                copy = matches[0]
                item = current.get(self.workspace.resolve(copy['path']))
                from simple_ar.report.review_evidence import delivery_target_pointer, delivery_subject_view
                if (item is None or item[1]['sha256'] != old['sha256']
                        or delivery_target_pointer(item[1], item[0]) is None):
                    raise ValueError('Current defective copy is absent from joint inspection')
                index, row = item
                subject = delivery_subject_view(row)
                if subject not in same:
                    same.append(subject)
                pointers.append(delivery_target_pointer(row, index))
            if not same:
                continue
            if defect not in prior:
                prior.append(defect)
            rebound = {'type': defect['type'], 'targets': same,
                'finding': {**defect['finding'], 'delivery_targets': list(dict.fromkeys(pointers))},
                'finding_ref': {'finding': defect['finding'], 'targets': defect['targets']}}
            if rebound not in prior:
                prior.append(rebound)

    def resubmit_after_repair(self, new_execution: dict) -> dict | None:
        """Resume the proven composition recipe, not another material investigation."""
        repaired = self._producer_repair(new_execution)
        if repaired is None:
            return None
        return {'tool_name': 'compose_document', 'arguments': repaired['arguments']}

    def consume_outcome(self, observed: dict, source: str) -> dict | None:
        """Accept a verified producer revision without repeating material selection."""
        if observed.get('tool_name') == 'investigate_implementation' and observed.get('status') == 'ok':
            from simple_ar.tools.implementation_findings import accepted_investigation
            from simple_ar.tools.implementation_work import _execution
            from simple_ar.tools.producer_declarations import _proof
            from simple_ar.tools.professional_outcomes import native_result_path
            path = native_result_path(self.root, self.workspace, source)
            accepted_native_result(self.root, self.session_id, observed, path.parent.name, ledger=self.ledger)
            proof = _proof(self)
            reference = observed['data']['execution_ref']
            findings = accepted_investigation(proof, reference)
            if findings is None or findings['disposition'] != 'outcome_ready':
                return None
            _, execution = _execution(proof, reference, successful=True)
            return self.resubmit_after_repair(execution)
        if (observed.get('tool_name') == 'export_report' and observed.get('status') == 'ok'
                and observed.get('data', {}).get('candidate_preparation') and self.prepared_publication is not None):
            prepared = self.prepared_publication(source)
            if prepared.get('adopted') is not False:
                raise ValueError('Publication preparation cannot grant adoption')
            return {'tool_name': 'compose_document', 'arguments': {'resume_work': prepared['prepared_result']}}
        return self.resubmit_after_repair(observed)

    def _producer_repair(self, new_execution: dict) -> dict | None:
        """Propose a new composition from accepted repair lineage, never execute.

        The implementation owner validates/authorizes repair_review; it is a
        native composition receipt locator in the new prepared source binding.
        Missing, ambiguous or structurally changed lineages return to the parent.
        """
        if new_execution.get('tool_name') != 'execute_implementation' or new_execution.get('status') != 'ok':
            return None
        try:
            from simple_ar.tools.producer_declarations import _proof
            from simple_ar.tools.implementation_work import _execution, _request, _review_execution
            from simple_ar.tools.implementation_run import candidate_source
            execution = self.workspace.resolve(new_execution['data']['execution_ref'])
            relative = execution.relative_to(self.root / 'attempts').parts
            if len(relative) != 2 or relative[1] != 'candidate_execution.json':
                return None
            # Reuse the implementation owner's read-only registration,
            # accounting and physical-receipt checks, without creating another
            # gateway/runtime or weakening failure diagnosis for this consumer.
            proof = _proof(self)
            saved, observed = _execution(proof, new_execution['data']['execution_ref'], successful=True)
            if observed != new_execution:
                return None
            from simple_ar.tools.implementation_findings import accepted_investigation
            findings = accepted_investigation(proof, new_execution['data']['execution_ref'])
            if findings is not None and findings['disposition'] != 'outcome_ready':
                return None
            prepared = self.workspace.resolve(saved['prepared_result'])
            request, _ = _request(proof, saved['prepared_result'])
            current, locator, seen = request, saved['prepared_result'], set()
            excluded = {'project_path', 'files', 'repair_execution', 'repair_review'}
            review_ref, old_run = None, None
            hops = 0
            while request.work_contract in {'shared.v1', 'shared.v2'} or hops < request.max_repairs:
                hops += 1
                path = self.workspace.resolve(locator)
                if path in seen or not current.complete_work:
                    return None
                seen.add(path)
                _, _, source = candidate_source(proof, locator)
                if current.repair_review:
                    prior, _ = _review_execution(proof, current.repair_review)
                elif current.repair_execution:
                    prior, _ = _execution(proof, current.repair_execution)
                else:
                    return None
                prior_path = self.workspace.resolve(prior['prepared_result'])
                linkage = source.get('prior_candidate', {})
                previous, previous_project = _request(proof, prior['prepared_result'])
                if (prior_path in seen or self.workspace.resolve(linkage.get('result_ref', '')) != prior_path
                        or linkage.get('result_sha256') != hashlib.sha256(prior_path.read_bytes()).hexdigest()
                        or linkage.get('source_binding_sha256') != hashlib.sha256(
                            (prior_path.parent / 'request.json').read_bytes()).hexdigest()
                        or self.workspace.resolve(current.project_path) != previous_project
                        or not previous.complete_work
                        or previous.model_dump(exclude=excluded) != request.model_dump(exclude=excluded)):
                    return None
                if current.repair_review:
                    review_ref = current.repair_review
                    old_run = prior
                    break  # Nearest quality diagnosis on this exact lineage.
                locator, current = prior['prepared_result'], previous
            if review_ref is None:
                return None
            review_path = self.workspace.resolve(review_ref)
            reviewed = review_path.relative_to(self.root / 'attempts').parts
            if len(reviewed) != 2 or reviewed[1] != 'native_result.json':
                return None
            observation = ArtifactStore(review_path.parent).read_json(review_path.name)
            if observation.get('tool_name') != 'compose_document' or observation.get('status') != 'ok':
                return None
            owner, original, _ = self._composition_receipt(reviewed[0], observation)
            result = owner.read_json('result.json')
            # _review_execution already proved this exact successful producer
            # against the independent defect receipt, including unique ownership.
            old_prepared = self.workspace.resolve(old_run['prepared_result'])
            old_project = self.workspace.resolve(ArtifactStore(old_prepared.parent).read_json('result.json')['data']['project_dir'])
            new_project = self.workspace.resolve(ArtifactStore(prepared.parent).read_json('result.json')['data']['project_dir'])
            old_versions = self.workspace.resolved_versions({**old_run['approval']['binding']['source_sha256'],
                **old_run['output_sha256']})
            new_versions = self.workspace.resolved_versions({**saved['approval']['binding']['source_sha256'],
                **saved['output_sha256']})
            for expected in (saved['approval']['binding']['source_sha256'], saved['output_sha256']):
                if self.workspace.resolved_versions(self.workspace.artifact_hashes(expected)) != self.workspace.resolved_versions(expected):
                    return None
            if (self.workspace.resolved_versions(self.workspace.artifact_hashes(result['artifacts']))
                    != self.workspace.resolved_versions(result['data']['artifact_sha256'])):
                return None

            def replace(locator: str) -> str:
                path = self.workspace.resolve_material_root(locator)
                if path.is_relative_to(old_project):
                    previous = path
                    path = self.workspace.resolve_material_root(str(new_project / path.relative_to(old_project)))
                    if (previous not in old_versions or path not in new_versions or not path.is_file()
                            or self.workspace.resolved_versions(self.workspace.artifact_hashes([str(path)])).get(path) != new_versions[path]):
                        raise ValueError('Repair no longer supplies a bound writing input')
                    return str(path)
                return locator

            arguments = dict(original['request'])
            arguments.pop('prior_result', None)  # A changed producer needs a new correction, not old-candidate adoption.
            if not (arguments.get('materials') or arguments.get('sources')):
                return None
            arguments.update(material_roots=[], materials=[], sources=[],
                prior_draft=result['data']['editable_markdown'], reuse_prior_inputs=True, adopt_draft=False,
                producer_repair=new_execution['data']['execution_ref'])
            arguments['figures'] = [replace(row) if isinstance(row, str) else {**row, 'path': replace(row['path'])}
                                    for row in arguments.get('figures', [])]
            if arguments.get('delivery_artifacts') is not None:
                arguments['delivery_artifacts'] = [replace(path) for path in arguments['delivery_artifacts']]
            ComposeRequest.model_validate(arguments)
            _, candidate, source = candidate_source(proof, saved['prepared_result'])
            context = {self.workspace.resolve(row['path']) for row in source.get('original_files', {}).values()}
            context.update(self.workspace.resolve(row['request']['path'])
                for row in source.get('source_windows', []) if 'request' in row)
            # Registered plan-only repairs deliberately carry no editable text.
            # Their complete prior membership is the existing source authority,
            # not a reason to rediscover/retransmit every unchanged original.
            from simple_ar.tools.implementation_preparation import REGISTERED_PROJECT_GENERATION
            if source.get('generation_method') == REGISTERED_PROJECT_GENERATION:
                from simple_ar.code_task.generation.selection import registered_project_files
                prior = source['prior_candidate']
                context.update(self.workspace.resolve(str(Path(prior['project_root']) / name))
                    for name in registered_project_files(self.workspace, prior))
            generated = {self.workspace.resolve(str(new_project / row['path'])): row['sha256']
                for row in candidate['data']['files']}
            source_replacements = {}
            for path in context:
                if not path.is_relative_to(old_project) or path not in old_versions:
                    continue
                replacement = self.workspace.resolve(str(new_project / path.relative_to(old_project)))
                if (new_versions.get(path) == old_versions[path] and replacement in generated
                        and new_versions.get(replacement) == generated[replacement]):
                    source_replacements[str(path)] = {'old_sha256': old_versions[path],
                        'path': str(replacement), 'sha256': generated[replacement]}
            return {'arguments': arguments, 'owner': owner.root.name, 'replace': replace,
                'execution_ref': new_execution['data']['execution_ref'], 'review_ref': review_ref,
                'execution_sha256': hashlib.sha256(execution.read_bytes()).hexdigest(),
                'old_project': old_project, 'new_project': new_project,
                'old_versions': old_versions, 'new_versions': new_versions,
                'source_replacements': source_replacements}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _freeze_delivery_review(self, paths: list[str], identity: str, *,
                                copies: list[dict] | None = None,
                                producer_units: list[dict] | None = None,
                                text_contract: str | None = None) -> dict:
        """Freeze exact subjects under their snapshot's text-admission contract.

        Complete registered units use one source-capability-sized byte budget,
        not a file-layout-dependent window limit. Imported subjects keep the
        historical per-file/aggregate policy, also within that whole budget.
        """
        from simple_ar.report.review_evidence import (
            DELIVERY_REVIEW_CONTRACT, DELIVERY_COPY_CONTRACT, SCOPED_DELIVERY_INSPECTION)
        unit_versions = {}
        if producer_units:
            from simple_ar.tools.producer_declarations import professional_units
            for unit in professional_units(self, [], [], retained=producer_units):
                for locator, digest in unit['files'].items():
                    path = self.workspace.resolve_material_root(locator)
                    if path in unit_versions and unit_versions[path] != digest:
                        raise ValueError('Professional delivery units disagree on an attachment version')
                    unit_versions[path] = digest
        unit_paths = set(unit_versions)
        # One whole-unit text budget, aligned with the code source capability.
        # Different subjects keep their own text/provenance even at equal SHA.
        unit_mode = bool(unit_paths)
        scoped = text_contract == SCOPED_DELIVERY_INSPECTION
        if text_contract not in {None, 'professional_unit_text.v1', 'professional_unit_text.v2', SCOPED_DELIVERY_INSPECTION}:
            raise ValueError('Unknown joint inspection text admission contract')
        if text_contract is not None and not unit_mode and not scoped:
            raise ValueError('Whole-unit text admission requires its frozen producer units')
        admission_contract = text_contract or 'professional_unit_text.v2'
        copy_sources, aliases = {}, {}
        if copies is not None:
            supplied = {self.workspace.resolve_material_root(path) for path in paths}
            for copy in copies:
                source = self.workspace.resolve_material_root(copy['source'])
                target = self.workspace.resolve_material_root(copy['path'])
                if (source == target or not ({source, target} & supplied)
                        or not source.is_file() or not target.is_file()):
                    raise ValueError('Delivery copy must be a supplied file with a distinct original subject')
                proof = (source, copy['sha256'])
                if target in copy_sources and copy_sources[target] != proof:
                    raise ValueError('Delivery copy has inconsistent source/version bindings')
                copy_sources[target] = proof
                alias = {'path': str(target), 'sha256': copy['sha256']}
                rows = aliases.setdefault(source, [])
                if alias not in rows:
                    rows.append(alias)
            original_paths = []
            for locator in paths:
                path = self.workspace.resolve_material_root(locator)
                original_paths.append(str(copy_sources.get(path, (path, None))[0]))
            paths = list(dict.fromkeys(original_paths))
        if copies is not None or unit_mode:
            subject_limit = 200 if unit_mode else 32
            if len({self.workspace.resolve_material_root(path) for path in paths}) > subject_limit:
                raise ValueError(f'Joint inspection exceeds {subject_limit} original subjects')
        if unit_paths - {self.workspace.resolve_material_root(path) for path in paths}:
            raise ValueError('Complete professional unit inspection omits declared original subjects')
        if len(paths) != len(set(paths)):
            raise ValueError('Joint inspection attachments must be unique')
        if scoped:
            # Allocate text to subjects without a table view first, then reuse
            # the reader for tables when needed. Caller ordering must not
            # decide whether the same complete set fits inspection.
            paths = sorted(paths, key=lambda locator: (
                self.workspace.resolve_material_root(locator).suffix.lower() in {'.csv', '.tsv', '.json'},
                str(self.workspace.resolve_material_root(locator))))
        provenance = getattr(self.workspace, 'artifact_provenance', None)
        versions = provenance(paths) if provenance is not None else {}
        rows, remaining, external_remaining = [], 256000 if unit_mode or scoped else 96000, 96000
        profile_remaining = 96000
        for locator in paths:
            path = self.workspace.resolve_material_root(locator)
            if any(row['path'] == str(path) for row in rows):
                continue  # Different authorized locator spellings, one subject.
            if not path.is_file():
                raise ValueError('Joint inspection requires explicit files, not directories')
            digest = self.workspace.artifact_hashes([locator])[locator]
            if path in unit_versions and digest != unit_versions[path]:
                raise ValueError('Professional attachment differs from its frozen unit version')
            version = versions.get(locator, {})
            if version and version.get('sha256') != digest:
                raise ValueError('Joint inspection provenance differs from attachment bytes')
            row = {'path': str(path), 'sha256': digest, 'producer': version.get('producer'),
                'coverage': 'unsupported', 'visual_reviewed': False}
            if scoped:
                row['inspection_contract'] = SCOPED_DELIVERY_INSPECTION
            if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.pdf', '.svg', '.webp'}:
                row['coverage'] = 'visual_uninspected'
            elif scoped:
                from simple_ar.tools.materials import inspect_bound_material
                row.update(inspect_bound_material(self.workspace, locator, digest,
                    text_bytes=remaining, profile_bytes=profile_remaining))
                remaining -= len(row.get('text', '').encode('utf-8'))
                if 'profile' in row:
                    profile_remaining -= len(json.dumps(row['profile'], ensure_ascii=False).encode('utf-8'))
            elif path.suffix.lower() in {'.csv', '.tsv', '.json', '.py', '.r', '.jl', '.sh',
                    '.txt', '.md', '.toml', '.yaml', '.yml', '.tex', '.bib'} or (
                        path in unit_paths and admission_contract == 'professional_unit_text.v2'):
                if unit_mode and path in unit_paths:
                    if path.stat().st_size > remaining:
                        raise ValueError('Complete professional unit inspection exceeds 256000 total text bytes')
                    # Discovery resolution and artifact_hashes above are the
                    # same read authority as workspace.read_file. This is an
                    # already-declared complete attachment, not a new window.
                    from simple_ar.tools.materials import read_bound_material_bytes
                    raw = read_bound_material_bytes(self.workspace, locator, digest, max_bytes=remaining)
                    from simple_ar.tools.materials import material_text_view
                    row.update(material_text_view(raw, reject_controls=admission_contract == 'professional_unit_text.v2'))
                    remaining -= len(raw)
                elif (unit_mode and path.stat().st_size <= min(32000, external_remaining)
                        and path.stat().st_size > remaining):
                    raise ValueError('Complete professional unit inspection exceeds 256000 total text bytes')
                elif path.stat().st_size > min(32000, remaining, external_remaining):
                    row['coverage'] = 'oversized'
                else:
                    read = self.workspace.call('read_file', {'path': locator,
                        'max_bytes': min(32000, max(1, remaining)), 'full_digest': True}, identity=identity)
                    if not read.ok:
                        raise ValueError('Joint inspection attachment cannot be read: ' + read.error)
                    if read.data.get('sha256') != digest:
                        raise ValueError('Joint inspection attachment changed during reading')
                    text = read.data['text']
                    row['coverage'] = 'incomplete' if read.data.get('has_more') or '\x00' in text or '\ufffd' in text else 'complete'
                    if row['coverage'] == 'complete':
                        row['text'] = text
                        remaining -= len(text.encode('utf-8'))
                        external_remaining -= len(text.encode('utf-8'))
            rows.append(row)
        if copies is not None:
            subjects = {self.workspace.resolve(row['path']): row for row in rows}
            expected_copies = {}
            for source, delivered in aliases.items():
                row = subjects.get(source)
                if row is None or any(copy['sha256'] != row['sha256'] for copy in delivered):
                    raise ValueError('Delivery copy version differs from the inspected original')
                for copy in delivered:
                    target = self.workspace.resolve(copy['path'])
                    if target.stat().st_size != source.stat().st_size:
                        raise ValueError('Delivery copy size differs from the inspected original')
                    expected_copies[copy['path']] = copy['sha256']
                row.update(copy_contract=DELIVERY_COPY_CONTRACT, copies=delivered,
                    copy_scope='This text is supplied once from the original subject. The listed actual delivery copies '
                        'are bound to the same complete bytes by source/path/SHA256 and size checks. Inspect their '
                        'content through this text and target this subject text for defects. Producer/consumer and '
                        'execution identities belong only to the original, never to its copies.')
                if scoped:
                    row['copy_scope'] = ('Original and actual delivery copies are byte-equivalent by '
                        'source/path/SHA256 and size checks. Inspect the supplied text or table profile '
                        'within its stated coverage; target that admitted view for defects. Producer '
                        'and execution identities belong only to the original, never to its copies.')
            if self.workspace.artifact_hashes(expected_copies) != expected_copies:
                raise ValueError('Delivery copies changed while freezing their equivalent source text')
            self.workspace.check_artifact_producers(expected_copies)
        if (self.workspace.resolved_versions(self.workspace.artifact_hashes([row['path'] for row in rows]))
                != self.workspace.resolved_versions({row['path']: row['sha256'] for row in rows})):
            raise ValueError('Joint inspection attachments changed during freezing')
        result = {'contract': DELIVERY_REVIEW_CONTRACT, 'artifacts': rows}
        if scoped:
            result['text_admission'] = {'contract': SCOPED_DELIVERY_INSPECTION,
                'max_text_bytes': 256000, 'max_profile_bytes': 96000, 'table_reader_max_mb': 20,
                'scope': 'Original subjects remain version-bound. Full text, complete-table structure statistics, '
                    'bounded examples and uninspected content have distinct scopes; profiles do not verify every value, '
                    'statistical conclusions or scientific correctness. Unsupported views stay incomplete.'}
        elif unit_mode:
            result['text_admission'] = {'contract': admission_contract, 'max_text_bytes': 256000,
                'scope': 'Complete original unit texts within one total byte limit; external materials retain '
                    '32000-per-file/96000-total admission. Distinct paths retain distinct text and producer roles. '
                    'Actual model input and shared ledger preflight still apply.'}
        return result

    def _joint_delivery_paths(self, selected: list[dict], extras: list[str], *,
                              producer_repair: dict | None = None,
                              delivery_copies: set[Path] | None = None,
                              producer_units: list[dict] | None = None,
                              declared_subjects_only: bool = False) -> tuple[list[str], dict]:
        """Use declared units; expand legacy subjects only outside those units."""
        # Derived whole-unit membership is not a model request parameter.
        paths, consumers = list(dict.fromkeys([*extras,
            *(path for unit in producer_units or [] for path in unit['files'])])), {}
        unit_members = {self.workspace.resolve_material_root(path)
            for unit in producer_units or [] for path in unit['files']}
        if producer_units:
            from simple_ar.tools.producer_declarations import professional_unit_input_owners
            consumers.update(professional_unit_input_owners(self, producer_units))
        declared = {self.workspace.resolve_material_root(path) for path in paths}
        process_inputs = {}

        def command_inputs(producer: dict) -> tuple[list[str], Path] | None:
            receipt = self.root / producer['receipt']
            key = (str(receipt), producer['receipt_sha256'])
            if key not in process_inputs:
                raw = receipt.read_bytes()
                if hashlib.sha256(raw).hexdigest() != producer['receipt_sha256']:
                    raise ValueError('Actual process receipt changed before subject selection')
                process = json.loads(raw)
                argv, cwd = process.get('argv'), process.get('cwd')
                process_inputs[key] = ((argv, self.workspace.resolve_material_root(cwd))
                    if isinstance(argv, list) and argv and all(isinstance(value, str) for value in argv)
                    and isinstance(cwd, str) else None)
            return process_inputs[key]

        def command_consumes(path: Path, command: tuple[list[str], Path] | None) -> bool:
            if command is None:
                return True  # Unknown command roles cannot exclude a dependency.
            argv, cwd = command
            for argument in argv:
                value = argument.split('=', 1)[1] if argument.startswith('-') and '=' in argument else argument
                try:
                    explicit = self.workspace.resolve_material_root(str(cwd / value))
                except (ValueError, OSError):
                    continue  # Literal flags/interpreter/code are not inferred paths.
                if explicit == path or explicit.is_dir() and path.is_relative_to(explicit):
                    return True
            return False

        def superseded_context(path: Path, digest: str, producer: dict) -> bool:
            if not producer_repair:
                return False
            replacement = producer_repair.get('source_replacements', {}).get(str(path))
            if not replacement or replacement['old_sha256'] != digest:
                return False
            execution = self.workspace.resolve(producer_repair['execution_ref'])
            if hashlib.sha256(execution.read_bytes()).hexdigest() != producer_repair['execution_sha256']:
                raise ValueError('Repaired producer execution changed before subject selection')
            saved = ArtifactStore(execution.parent).read_json(execution.name)
            if (producer['invocation_id'] != execution.parent.name
                    or producer['receipt'] != saved['receipt']
                    or self.workspace.resolved_versions(producer['inputs']).get(self.workspace.resolve(replacement['path']))
                        != replacement['sha256']):
                return False
            command = command_inputs(producer)
            if command is None or command != (saved['argv'], self.workspace.resolve_material_root(saved['cwd'])):
                return False  # Insufficient role proof: retain the old input.
            # Only a proven design-context source with an actual new-candidate
            # counterpart may be omitted. This is argv role handling, not a
            # dynamic import/dependency scanner or an execution sandbox.
            return not command_consumes(path, command)
        candidates = []
        for row in selected:
            path = self.workspace.resolve_material_root(row['path'])
            if path.is_relative_to(self.workspace.workspace) and path.is_file():
                candidates.append(row['path'])
        versions = self.workspace.artifact_provenance(candidates)
        for locator, version in versions.items():
            if declared_subjects_only and self.workspace.resolve_material_root(locator) not in declared:
                # Provenance was checked above. Evidence-only results are not
                # implicitly promoted to this delivery or its attachment review.
                continue
            producer = version['producer']
            if producer is None:
                continue
            if locator not in paths:
                paths.append(locator)
            unit_subject = self.workspace.resolve_material_root(locator) in unit_members
            for name, digest in producer['inputs'].items():
                legacy_script = Path(name).suffix.lower() in {'.py', '.r', '.jl', '.sh'}
                if not legacy_script and not declared_subjects_only:
                    continue  # Do not resolve private owner receipt JSON as material.
                try:
                    path = self.workspace.resolve_material_root(name)
                except ValueError:
                    if legacy_script:
                        raise
                    # Provenance validates private owner receipts; they do not
                    # become authorized material through a command reference.
                    continue
                if (unit_subject or not legacy_script) and not command_consumes(path, command_inputs(producer)):
                    # Complete units own their membership. Consumed design
                    # ancestors are not extra deliverables, but actual command
                    # references outside a unit remain inspection subjects,
                    # regardless of suffix under the declared-subject contract.
                    # Provenance still validates every consumed byte version;
                    # saved historical inspections retain their original scope.
                    continue
                if superseded_context(path, digest, producer):
                    continue
                # Owner receipts and original source mounts are evidence, not
                # generated scientific attachments or editable code targets.
                if path.is_relative_to(self.workspace.workspace):
                    if self.workspace.artifact_hashes([name])[name] != digest:
                        raise ValueError('Producer source changed before joint inspection')
                    if name not in paths:
                        paths.append(name)
                    owners = consumers.setdefault(str(path), [])
                    if not any(self._same_delivery_producer(producer, owner) for owner in owners):
                        owners.append(producer)
        subject_count = (len(paths) if delivery_copies is None else len({self.workspace.resolve(path)
            for path in paths if self.workspace.resolve(path) not in delivery_copies}))
        subject_limit = 200 if producer_units else 32
        if subject_count > subject_limit:
            raise ValueError(f'Joint inspection exceeds {subject_limit} files; split the professional work unit')
        return paths, consumers

    @staticmethod
    def _same_report_content(saved: dict, current: dict, intent: dict | None) -> bool:
        if saved == current:
            return True
        # Explicit formatting/conversion intent can reuse a finished body;
        # evidence, frozen method/review, source bytes and execution remain bound.
        feedback_only = {'requests', 'requests_sha256', 'request_count', 'obligation'}
        return bool(isinstance(saved, dict) and isinstance(current, dict)
            and intent and intent['kind'] == 'conversion' and
            {key: value for key, value in saved.items() if key not in feedback_only} ==
            {key: value for key, value in current.items() if key not in feedback_only})

    def preparation_source(self, markdown: str, result_ref: str) -> dict:
        """Prove retained work for rendering, without granting adoption/delivery."""
        path = self.workspace.resolve(str(self.root / result_ref) if result_ref.startswith('attempts/') else result_ref)
        relative = path.relative_to((self.root / 'attempts').resolve())
        if len(relative.parts) != 2 or relative.name != 'native_result.json':
            raise ValueError('Preparation requires a same-task native writing receipt')
        observed = json.loads(path.read_bytes())
        identity = relative.parts[0]
        accepted_native_result(self.root, self.session_id, observed, identity)
        if observed.get('data', {}).get('work_status') == 'awaiting_publication_preparation':
            stage, binding = self._publication_stage(observed, identity)
            if self.workspace.resolve(markdown) != self.workspace.resolve(stage['markdown']):
                raise ValueError('Preparation uses a different candidate than the suspended work')
            return {'kind': 'publication_candidate', 'manuscript': stage['markdown'],
                'source_result': self.workspace.locator(str(path)),
                'source_result_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'composition': stage['owner'], 'receipt_sha256': stage['stage_sha256'],
                'artifact_sha256': stage['artifact_sha256'], 'adopted': False,
                'assembly_identity': stage['candidate']['assembly']['identity'],
                'publication_options': stage['publication'],
                'figure_caption_owner': 'manifest' if stage['captions'] else 'markdown',
                **({'provided_figure_captions': {row['path']: row['caption']
                    for row in stage['captions']}} if stage['captions'] else {})}
        store, binding, data = self._composition_receipt(identity, observed)
        if self.workspace.resolve(markdown) != self.workspace.resolve(data['editable_markdown']):
            raise ValueError('Preparation uses a different candidate than its writing receipt')
        self._check_input_bytes(binding['inputs'])
        self.workspace.check_artifact_producers(row['path'] for row in binding['inputs'])
        from simple_ar.report.writing import provided_caption_bindings
        from simple_ar.report.capability import supplied_caption_manifest
        from simple_ar.report.schema import ReportContext
        snapshot = ArtifactStore(store.root / 'writer').read_json('report_inputs.json')
        article = snapshot.get('inputs', {}).get('article', {})
        context = (ReportContext.model_validate(snapshot['context']) if 'context' in snapshot else
            ReportContext(topic=article['title'], report_mode='supplied_materials',
                papers=article['bibliography'], citation_key_map=article['citation_key_map'],
                results={'provided_figure_captions': provided_caption_bindings(binding)}))
        captions = {row['path']: row['caption'] for row in supplied_caption_manifest(context)}
        return {'kind': 'publication_candidate', 'manuscript': data['editable_markdown'],
            'source_result': self.workspace.locator(str(path)), 'source_result_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'composition': data['version'], 'receipt_sha256': hashlib.sha256(store.resolve('result.json').read_bytes()).hexdigest(),
            'artifact_sha256': data['artifact_sha256'], 'adopted': False,
            **({'figure_caption_owner': 'manifest', 'provided_figure_captions': captions} if captions else {})}

    def _publication_stage(self, observed: dict, identity: str) -> tuple[dict, dict]:
        """Prove immutable handback; the live Writer checkpoint may later advance."""
        accepted_native_result(self.root, self.session_id, observed, identity)
        if (observed.get('tool_name') != 'compose_document' or observed.get('status') != 'ok'
                or observed.get('error') or observed['data'].get('work_status') != 'awaiting_publication_preparation'):
            raise ValueError('Not a suspended document inspection')
        ref = observed['data']['publication_stage']
        stage_path = self.root/'attempts'/identity/'document_work.json'
        if ref != self.workspace.locator(str(stage_path)):
            raise ValueError('Document stage is outside its physical operation')
        stage = json.loads(stage_path.read_bytes())
        digest = hashlib.sha256(stage_path.read_bytes()).hexdigest()
        if (observed['data']['stage_sha256'] != digest
                or observed.get('artifacts') != list(stage['artifact_sha256'])
                or self.workspace.artifact_hashes(stage['artifact_sha256']) != stage['artifact_sha256']):
            raise ValueError('Suspended candidate or stage changed')
        owner = ArtifactStore(self.root/'tools/writing'/stage['owner'])
        binding = owner.read_json('request.json')
        if (binding.get('publication_inspection_contract') != 'prepared_document.v1'
                or hashlib.sha256(owner.resolve('request.json').read_bytes()).hexdigest() != stage['request_sha256']):
            raise ValueError('Original document work contract changed')
        self._check_input_bytes(binding['inputs'])
        for key, digest in stage['checkpoint_sha256'].items():
            if hashlib.sha256((self.root/'tools/writing'/identity/'publication_checkpoint'/key).read_bytes()).hexdigest() != digest:
                raise ValueError('Suspended Writer checkpoint archive changed')
        return {**stage, 'stage_sha256': observed['data']['stage_sha256']}, binding

    def _suspend_publication(self, *, owner: ArtifactStore, binding: dict, payload: dict,
                             physical_identity: str, arguments: dict, figure_inputs: list,
                             prepared: tuple[str, ...]) -> ToolResult:
        """Hand a complete candidate to normal publication dispatch, not a new editor."""
        from simple_ar.tools.publication import export_input_binding
        identity = owner.root.name
        stage_store = ArtifactStore(self.root/'tools/writing'/physical_identity)
        operation = ArtifactStore(self.root/'attempts'/physical_identity)
        output = ArtifactStore(self.workspace.resolve(
            f'workspace/document_work/{identity}/{physical_identity}', write=True))
        files = {'candidate.md': payload['assembly']['report_body_markdown'].encode('utf-8'),
            'references.bib': payload['assembly']['publication_inputs']['references_bib'].encode('utf-8')}
        captions = payload['assembly']['publication_inputs']['figure_captions']
        if captions:
            files['figures/figures_manifest.json'] = (json.dumps({'figures': captions},
                ensure_ascii=False, indent=2)+'\n').encode('utf-8')
        files.update({target: raw for _, target, raw in figure_inputs})
        for key, raw in files.items():
            target = output.resolve(key)
            if target.exists() and target.read_bytes() != raw:
                raise ValueError('Suspended publication candidate changed; no overwrite')
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(raw)
        checkpoint = {}
        for source in sorted((owner.root/'writer').rglob('*.json')):
            key = source.relative_to(owner.root/'writer').as_posix()
            raw = source.read_bytes()
            target = stage_store.resolve('publication_checkpoint/'+key)
            if target.exists() and target.read_bytes() != raw:
                raise ValueError('Saved handback checkpoint changed')
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(raw)
            checkpoint[key] = hashlib.sha256(raw).hexdigest()
        options = self._publication_options(binding)
        markdown = self.workspace.locator(str(output.resolve('candidate.md')))
        request = ExportRequest(markdown=markdown, **options)
        inputs = export_input_binding(output.resolve('candidate.md'), request)['inputs']
        artifacts = [self.workspace.locator(str(output.resolve(key))) for key, digest in inputs.items()
            if digest is not None]
        work = binding.get('document_work')
        if work is not None:
            artifacts.extend(row['path'] for row in work['materials'])
            artifacts.extend(path for unit in work.get('producer_units', []) for path in unit['files'])
            artifacts = list(dict.fromkeys(artifacts))
        hashes = self.workspace.artifact_hashes(artifacts)
        stage = {'contract': 'prepared_document.v1', 'owner': identity, 'arguments': arguments,
            'candidate': payload, 'markdown': markdown, 'captions': captions,
            'artifact_sha256': hashes, 'checkpoint_sha256': checkpoint,
            'request_sha256': hashlib.sha256(owner.resolve('request.json').read_bytes()).hexdigest(),
            'prepared': list(prepared), 'publication': options}
        if operation.exists('document_work.json') and operation.read_json('document_work.json') != stage:
            raise ValueError('Publication handback changed; preserve its original operation')
        if not operation.exists('document_work.json'):
            operation.write_json('document_work.json', stage)
        return ToolResult(tool_name='compose_document', artifacts=artifacts,
            data={'work_status': 'awaiting_publication_preparation', 'adopted': False,
                'publication_stage': self.workspace.locator(str(operation.resolve('document_work.json'))),
                'stage_sha256': hashlib.sha256(operation.resolve('document_work.json').read_bytes()).hexdigest(),
                'editable_markdown': markdown, 'artifact_sha256': hashes,
                **({'document_work': work} if work is not None else {})},
            summary='Complete candidate saved. Prepare its requested publication before independent inspection; no adoption or final delivery.')

    def _resume_publication(self, arguments: dict, *, identity: str) -> ToolResult:
        DocumentWorkRequest.model_validate(arguments)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity):
            raise ValueError('Invalid publication continuation identity')
        if self.prepared_publication is None:
            raise ValueError('No registered publication inspection owner')
        prepared = self.prepared_publication(arguments['resume_work'])
        source = prepared['source']
        path = self.workspace.resolve(source['source_result'])
        observed = json.loads(path.read_bytes())
        stage, _ = self._publication_stage(observed, path.parent.name)
        owner = ArtifactStore(self.root/'tools/writing'/stage['owner'])
        current = ArtifactStore(self.root/'tools/writing'/identity)
        if identity in {stage['owner'], path.parent.name} or current.exists('request.json'):
            raise ValueError('Continuation requires its own physical operation, not replacement of an existing owner')
        link = {'owner': stage['owner'], 'prepared_result': prepared['prepared_result'],
            'prepared_sha256': prepared['prepared_result_sha256'],
            'stage_sha256': stage['stage_sha256']}
        if current.exists('resume.json'):
            retained_link = current.read_json('resume.json')
            # Old handbacks may have saved the journal spelling. Compare its
            # admitted canonical address without rewriting the historical link.
            from simple_ar.tools.professional_outcomes import native_result_path
            retained_path = native_result_path(self.root, self.workspace, retained_link['prepared_result'])
            canonical_link = {**retained_link, 'prepared_result': self.workspace.locator(str(retained_path))}
            if canonical_link != link:
                raise ValueError('Retained document continuation changed')
            if current.exists('result.json'):
                saved = current.read_json('result.json')
                if saved.get('status', 'ok') == 'ok':
                    self._composition_receipt(identity, {'tool_name': 'compose_document', **saved})
                else:
                    # A crash may precede native observation registration. This
                    # cache can reconstruct only failure, never retry or adoption.
                    native = ArtifactStore(self.root/'attempts'/identity)
                    if (not saved.get('data', {}).get('incomplete') or saved.get('data', {}).get('delivery')
                            or native.exists('native_result.json') and native.read_json('native_result.json')
                                != {'tool_name': 'compose_document', **saved}):
                        raise ValueError('Stopped document continuation differs from its native outcome')
                return ToolResult(tool_name='compose_document', **saved)
        else:
            # Initial admission must match the exact paused checkpoint. A later
            # same-operation recovery uses the existing Writer's no-blind-retry rules.
            actual = {file.relative_to(owner.root/'writer').as_posix():
                hashlib.sha256(file.read_bytes()).hexdigest() for file in (owner.root/'writer').rglob('*.json')}
            if actual != stage['checkpoint_sha256'] or owner.exists('result.json'):
                raise ValueError('Document checkpoint already advanced; cannot fork or reset its revision allowance')
            current.write_json('resume.json', link)
        refs = tuple(dict.fromkeys([*stage['prepared'], prepared['prepared_result']]))
        result = self.call('compose_document', stage['arguments'], identity=stage['owner'],
            _continuation={'physical_identity': identity, 'prepared': refs})
        if result.data.get('work_status') != 'awaiting_publication_preparation':
            current.write_json('result.json', result.model_dump(mode='json', exclude={'tool_name'}))
        return result

    def _publication_lookup(self, refs: tuple[str, ...]) -> Callable[[dict], dict | None]:
        def lookup(payload):
            for ref in refs:
                prepared = self.prepared_publication(ref)
                source = prepared['source']
                path = self.workspace.resolve(source['source_result'])
                stage, _ = self._publication_stage(json.loads(path.read_bytes()), path.parent.name)
                if stage['candidate'] == payload:
                    return prepared
            return None
        return lookup

    def publication_source(self, markdown: str, binding: dict | None, *, intent: dict | None = None,
                           adopted: dict | None = None, observation: dict | None = None,
                           review: dict | None = None) -> dict:
        """Prove one canonical authoring version, never infer identity from prose."""
        path = self.workspace.resolve(markdown)
        candidate_root = self.workspace.workspace/'manuscripts'
        if path.is_relative_to((self.workspace.workspace/'document_work').resolve()):
            raise ValueError('Suspended publication candidates require their preparation receipt and cannot be adopted by conversion')
        if path.is_relative_to(candidate_root.resolve()):
            parts = path.relative_to(candidate_root.resolve()).parts
            if len(parts) > 2 and parts[1] == 'preparations':
                raise ValueError('Suspended publication candidates require their preparation receipt and cannot be adopted by conversion')
        identity = path.parent.name
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity)
                or path not in {self.workspace.resolve(f'workspace/manuscripts/{identity}/{leaf}')
                    for leaf in ('manuscript.md', 'candidate.md')}):
            from simple_ar.tools.publication import ExportRequest, export_input_binding
            # Publication owns original-text conversion input bytes too. A raw
            # input identity never acquires a scientific composition identity.
            inputs = export_input_binding(path, ExportRequest(markdown=markdown),
                output_root=self.workspace.workspace / 'publications')['inputs']
            result = {'kind': 'conversion', 'manuscript': self.workspace.locator(str(path)), 'inputs': inputs}
            if adopted is not None and result != adopted:
                raise ValueError('Adopted conversion source or input bytes changed')
            return result
        store, request, data = self._composition_receipt(identity, observation)
        if path != self.workspace.resolve(data['editable_markdown']):
            raise ValueError('Requested authoring version differs from its current writing receipt')
        if data.get('unresolved'):
            raise ValueError('Canonical report has unresolved writing findings/review; deliver a candidate with explicit unresolved work')
        if not self._same_report_content(data.get('report_binding'), binding, intent):
            raise ValueError('Canonical report receipt, source binding or authoring artifacts changed')
        prepared = request.get('publication_inspection_contract') == 'prepared_document.v1'
        independent = review is not None
        if prepared and review is None:
            review = self.final_review(data)
        self._check_bound_delivery(store, request, data, review=review, independent=independent)
        from simple_ar.report.writing import check_retained_composition_writer
        check_retained_composition_writer(store=store, binding=request, identity=identity)
        result = {'composition': data['version'], 'manuscript': data['editable_markdown'],
            'receipt_sha256': hashlib.sha256(store.resolve('result.json').read_bytes()).hexdigest(),
            'artifact_sha256': data['artifact_sha256']}
        from simple_ar.report.writing import provided_caption_bindings
        if provided_caption_bindings(request):
            result['figure_caption_owner'] = 'manifest'
        if prepared:
            current = self._composition_publication(store, request, data, review=review)
            result['prepared_publication'] = {key: current[key] for key in
                ('prepared_result', 'prepared_result_sha256', 'view_path', 'view_sha256')}
            result['publication_options'] = current['source']['publication_options']
        if adopted is not None and result != adopted:
            raise ValueError('Adopted canonical manuscript identity changed')
        return result

    def report_delivery(self, observations: Iterable[dict], binding: dict, artifacts: list[str], *,
                        manuscript: str | None = None, intent: dict | None = None) -> dict:
        """Own accepted composition, original evidence and current version checks."""
        delivered = {self.workspace.resolve(path) for path in artifacts}
        for row in observations:
            data = row.get('data') or {}
            if (row.get('tool_name') != 'compose_document' or row.get('status') != 'ok'
                    or not self._same_report_content(data.get('report_binding', {}), binding, intent)
                    or not data.get('editable_markdown')
                    or self.workspace.resolve(data['editable_markdown']) not in delivered
                    or manuscript is not None and self.workspace.resolve(manuscript) != self.workspace.resolve(data['editable_markdown'])
                    or data.get('unresolved') and not binding.get('report_protocol')):
                continue
            if binding.get('report_protocol'):
                return self.publication_source(data['editable_markdown'], binding, intent=intent, observation=row)
            store = ArtifactStore(self.root / 'tools/writing' / Path(data['version']).name)
            saved = store.read_json('result.json')
            request = store.read_json('request.json')
            if (saved.get('data') != data or saved.get('artifacts') != row['artifacts']
                    or request.get('report_binding') != data.get('report_binding')
                    or self.workspace.artifact_hashes(row['artifacts']) != data.get('artifact_sha256')):
                continue
            self._check_bound_delivery(store, request, data)
            from simple_ar.report.writing import check_legacy_writer_receipt
            check_legacy_writer_receipt(store)
            return {'manuscript': data['editable_markdown']}
        raise ValueError('Report delivery needs a current canonical composition receipt, intact manuscript and original evidence; deliver an explicit candidate when incomplete')

    def _check_input_bytes(self, inputs: list[dict[str, Any]]) -> None:
        check_composition_inputs(self.workspace, inputs)
