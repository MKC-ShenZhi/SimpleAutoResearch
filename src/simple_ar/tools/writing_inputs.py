"""Document input resolution, prior-input continuity, and coordination helpers for WritingToolGateway."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from simple_ar.core.capabilities import ArtifactStore
from simple_ar.report.composition_receipts import (
    check_work_receipt,
    composition_owner,
    read_composition_receipt,
    read_composition_review,
)
from simple_ar.report.legacy_contract import ProfessionalReview
from simple_ar.tools.professional_outcomes import accepted_native_result
from simple_ar.tools.publication import ExportRequest
from simple_ar.tools.specs import ToolResult
from simple_ar.tools.workspace import DeliveryRequest, FileWindow
from simple_ar.tools.writing_contracts import (
    ComposeFigure,
    ComposeRequest,
    CompositionPublication,
    DocumentWorkRequest,
    SelectedSource,
    _DOCUMENT_WORK_CONTRACT,
    _DOCUMENT_WORK_CONTRACTS,
    _DocumentInputs,
    _DocumentSourceGap,
)


class _WritingInputsMixin:
    """Document source admission and input continuity methods."""

    def _admit_document_source(self, owner: ArtifactStore, arguments: dict,
                               binding: dict | None, selection: dict | None,
                               report: dict | None) -> tuple[dict | None, dict | None]:
        """One version gate; retain intent before attempting internal resolution.

        The admission shares the existing selection receipt. Investigation later
        fills that receipt; it is not a second work queue or recovery state.
        Saved recipes and selections without this field keep their old identity.
        """
        saved = binding if binding is not None else selection
        source = saved.get('work_source') if saved is not None else None
        if source is None:
            if (saved is not None or self.document_source_contract is None
                    or not set(arguments) <= DocumentWorkRequest.model_fields.keys()
                    or (self.root / 'attempts' / owner.root.name / 'native_result.json').exists()):
                return None, selection
            contract = self.document_source_contract
        else:
            contract = source['contract']
        self._check_document_source_contract(contract)
        intent = DocumentWorkRequest.model_validate(arguments).model_dump(mode='json')
        if source is not None:
            if source.get('resolution_contract') not in {None, 'selected_recipe.v1'}:
                raise ValueError('Unknown document recipe resolution contract')
            if source['intent'] != intent:
                raise ValueError('Retained document work intent changed')
            for path in [*(source['access_roots'] or []), *source['investigation_roots']]:
                self.workspace.resolve_material_root(path)
            return source, selection
        roots = intent['material_roots'] or (report or {}).get('selection_paths') or [
            str(path) for name, path in self.workspace.mounts.items() if name not in {'workspace', 'records'}]
        roots = [*roots, *(report or {}).get('selection_paths', [])]
        roots = list(dict.fromkeys(str(self.workspace.resolve_material_root(path)) for path in roots))
        source = {'contract': contract, 'intent': intent, 'investigation_roots': roots,
            'access_roots': None if intent.get('prior_result') or intent['prior_draft'] else roots,
            'resolution_contract': 'selected_recipe.v1'}
        from simple_ar.report.review_evidence import SCOPED_DELIVERY_INSPECTION
        admission = {'document_work': _DOCUMENT_WORK_CONTRACT, 'work_source': source,
            'inspection_contract': SCOPED_DELIVERY_INSPECTION}
        if self.work_context is not None:
            from simple_ar.tools.materials import freeze_work_context
            owner.write_json('selection-input.json', admission)
            targets = [*roots, *([intent['prior_draft']] if intent['prior_draft'] else [])]
            admission['work_context'] = freeze_work_context(self.work_context, list(dict.fromkeys(targets)))
        owner.write_json('selection-input.json', admission)
        return source, admission

    @staticmethod
    def _check_document_source_contract(contract: str) -> None:
        if contract != 'document_source.v1':
            raise ValueError('Unknown document source admission contract')

    def _document_investigation(self, request: ComposeRequest, work_source: dict,
                                 parent: ArtifactStore, binding: dict, review: dict) -> _DocumentInputs:
        """Keep the diagnosed candidate as context while investigating current roots."""
        data = parent.read_json('result.json')['data']
        draft = data['editable_markdown']
        diagnosis = {'owner': parent.root.name,
            'receipt_sha256': {name: hashlib.sha256(parent.resolve(name).read_bytes()).hexdigest()
                for name in ('request.json', 'result.json')},
            'artifact_sha256': {draft: data['artifact_sha256'][draft]},
            'review': review}
        if data.get('review_path'):
            path = data['review_path']
            diagnosis['artifact_sha256'][path] = data['artifact_sha256'][path]
        if self.workspace.artifact_hashes(diagnosis['artifact_sha256']) != diagnosis['artifact_sha256']:
            raise ValueError('Document investigation candidate or negative diagnosis changed')
        if 'diagnosis' in work_source and work_source['diagnosis'] != diagnosis:
            raise ValueError('Retained document investigation diagnosis changed')
        return _DocumentInputs(request.model_copy(update={'prior_result': '', 'prior_draft': draft,
            'reuse_prior_inputs': False, 'material_roots': work_source['investigation_roots'],
            'sources': [], 'materials': []}), investigation=diagnosis)

    def _document_attachment_gap(self, binding: dict, review: dict) -> bool | None:
        """Only actual attachment findings can name an actual producer owner.

        None means no attachment defect; False means supplied defective bytes
        without a proven task producer. General requests for more evidence never
        become implementation repair authority through this classification.
        """
        defects = [row for row in review.get('delivery_review', {}).get('defects', [])
            if row.get('type') == 'delivery_artifact_defect' and row.get('targets')
            and row.get('finding', {}).get('delivery_targets')]
        if not defects:
            return None
        subjects = {self.workspace.resolve(row['path']): row
            for row in binding.get('delivery_review', {}).get('artifacts', [])}
        producer = False
        for defect in defects:
            for target in defect.get('targets', []):
                path = self.workspace.resolve(target['path'])
                subject = subjects.get(path)
                if subject is None or target['sha256'] != subject['sha256']:
                    raise ValueError('Attachment diagnosis differs from its frozen subject')
                if target.get('producer') is not None:
                    actual = self.workspace.artifact_provenance([target['path']])[target['path']]
                    if (actual['sha256'] != target['sha256']
                            or not self._same_delivery_producer(actual['producer'], target['producer'])
                            or not self._same_delivery_producer(subject['producer'], target['producer'])):
                        raise ValueError('Attachment diagnosis has no unchanged actual producer')
                    producer = True
        return producer

    def _document_access_roots(self, store: ArtifactStore, binding: dict,
                               seen: frozenset[str] = frozenset()) -> list[str] | None:
        """Recover scope from actual frozen provenance, never missing-field defaults."""
        if store.root.name in seen:
            raise ValueError('Cyclic document access scope lineage')
        seen = seen | {store.root.name}
        if 'work_source' in binding:
            self._check_document_source_contract(binding['work_source']['contract'])
            scope = binding['work_source']['access_roots']
            return list(scope) if scope is not None else None
        if 'material_roots' in binding['request']:
            return list(binding['request']['material_roots'])
        if store.exists('selection-input.json'):
            selection = store.read_json('selection-input.json')
            if 'work_source' in selection:
                self._check_document_source_contract(selection['work_source']['contract'])
                scope = selection['work_source']['access_roots']
                return list(scope) if scope is not None else None
            if 'material_roots' in selection['request']:
                return list(selection['request']['material_roots'])
        lineage = binding.get('prior_input_reuse')
        if lineage is None:
            return None  # Unknown scope is not unrestricted scope.
        parent_id = lineage['owner']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', parent_id):
            raise ValueError('Invalid document access scope owner')
        parent = ArtifactStore(self.root / 'tools/writing' / parent_id)
        expected = lineage['receipt_sha256']
        if 'request.json' not in expected:
            raise ValueError('Document access scope has no bound parent request')
        parent_binding = parent.read_json('request.json')
        for name in ('request.json', 'selection-input.json'):
            if (name == 'selection-input.json' and parent.exists(name) and name not in expected
                    and 'material_roots' not in parent_binding['request'] and 'work_source' not in parent_binding):
                raise ValueError('Document access scope selection is not hash bound')
            if name in expected and hashlib.sha256(parent.resolve(name).read_bytes()).hexdigest() != expected[name]:
                raise ValueError('Document access scope receipts changed')
        return self._document_access_roots(parent, parent_binding, seen)

    def _document_source_observation(self, identity: str) -> tuple[str | None, dict | None]:
        """Locate a draft's physical receipt in the existing registration journal.

        A manuscript names its logical owner. Its first native operation may
        only have suspended for publication; it is not the later completion.
        Inspect registered operations, never guess from candidate directories.
        Exact callers without a native completion retain canonical-owner proof.
        """
        store = ArtifactStore(self.root / 'tools/writing' / identity)
        saved = store.read_json('result.json') if store.exists('result.json') else None
        journal = ArtifactStore(self.root)
        registered = (journal.read_json('session_manifest.json')['budget']['recorded_attempts']
            if journal.exists('session_manifest.json') else [])
        for physical in reversed(registered):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', physical):
                raise ValueError('Invalid registered document operation identity')
            native = ArtifactStore(self.root / 'attempts' / physical)
            if not native.exists('native_result.json'):
                continue
            observed = native.read_json('native_result.json')
            if observed.get('tool_name') != 'compose_document':
                continue
            if saved is None:
                if physical != identity:
                    continue
            elif (observed.get('data') != saved.get('data')
                    or observed.get('artifacts') != saved.get('artifacts')
                    or observed.get('status') != saved.get('status', 'ok')
                    or observed.get('error', '') != saved.get('error', '')):
                continue
            accepted_native_result(self.root, self.session_id, observed, physical, ledger=self.ledger)
            if composition_owner(root=self.root, workspace=self.workspace,
                    session_id=self.session_id, identity=physical).root != store.root:
                raise ValueError('Registered document operation has a different logical owner')
            return physical, observed
        if identity in registered or (
                self.root / 'attempts' / identity / 'native_result.json').exists():
            raise ValueError('Registered draft has no exact completed physical observation; preserve its receipts')
        return None, None

    def _resolve_document_inputs(self, request: ComposeRequest, identity: str, *,
                                 report: dict | None, original: dict | None,
                                 selection: dict | None, document_work: bool,
                                 caller_fields: frozenset[str], work_source: dict | None,
                                 normalize_arguments: Callable[[dict], dict] | None) -> _DocumentInputs:
        """Resolve a work source once; only the resulting recipe reaches Writer."""
        result = _DocumentInputs(request)
        if work_source is not None:
            # Both locator forms identify an owner. For a complete checked but
            # unadopted revision, an explicit result keeps that candidate even
            # when correction wording changes. Completed drafts retain their
            # ordinary feedback semantics.
            parent_id = native_id = None
            observed = None
            if request.prior_result:
                value = request.prior_result
                path = self.workspace.resolve(str(self.root / value) if value.startswith('attempts/') else value)
                relative = path.relative_to((self.root / 'attempts').resolve())
                if len(relative.parts) != 2 or relative.name != 'native_result.json':
                    raise ValueError('Document source requires a native composition receipt')
                native_id = relative.parts[0]
                observed = json.loads(path.read_bytes())
                accepted_native_result(self.root, self.session_id, observed, native_id, ledger=self.ledger)
                if observed.get('tool_name') != 'compose_document':
                    raise ValueError('Document source is not a composition')
                parent_id = composition_owner(root=self.root, workspace=self.workspace,
                    session_id=self.session_id, identity=native_id).root.name
            elif request.prior_draft:
                path = self.workspace.resolve(request.prior_draft)
                if path in {self.workspace.resolve(f'workspace/manuscripts/{path.parent.name}/{leaf}')
                        for leaf in ('manuscript.md', 'candidate.md')}:
                    parent_id = path.parent.name
            if parent_id is not None:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', parent_id) or parent_id == identity:
                    raise ValueError('Document source requires a distinct same-task owner')
                parent = ArtifactStore(self.root / 'tools/writing' / parent_id)
                binding = parent.read_json('request.json')
                scope = self._document_access_roots(parent, binding)
                requested = {self.workspace.resolve_material_root(path) for path in request.material_roots}
                admitted = ({self.workspace.resolve_material_root(path) for path in scope} if scope is not None else set())
                admitted.add(self.workspace.resolve(f'workspace/manuscripts/{parent_id}'))
                same_scope = (not requested or scope is not None and requested <= admitted)
                previous_focus = binding.get('work_source', {}).get('intent', {}).get('material_roots')
                if requested and previous_focus:
                    same_scope = same_scope and requested == {
                        self.workspace.resolve_material_root(path) for path in previous_focus}
                previous_report = binding.get('report_binding')
                # A new admitted feedback event can change prose/publication
                # intent while retaining the exact evidence and Writer controls.
                # Historical recovery still uses full binding equality below.
                feedback_fields = {'requests', 'requests_sha256', 'request_count', 'obligation'}
                same_controls = previous_report == report or (
                    isinstance(previous_report, dict) and isinstance(report, dict) and
                    {key: value for key, value in previous_report.items() if key not in feedback_fields} ==
                    {key: value for key, value in report.items() if key not in feedback_fields})
                selected_recipe = work_source.get('resolution_contract') == 'selected_recipe.v1'
                original_goal = binding.get('work_source', {}).get('intent', {}).get('goal') or binding['request']['goal']
                same_intent = (isinstance(report, dict) and isinstance(previous_report, dict)
                    and same_controls and previous_report.get('requests') == report.get('requests')
                    or report is None and previous_report is None and work_source['intent']['goal'] == original_goal)
                frozen_investigation = ('diagnosis' in work_source and (original is not None or selection is not None))
                if selected_recipe and same_intent and scope is not None and requested:
                    # Directory aliases and enclosing project paths do not make
                    # the selected evidence new. Every requested root must still
                    # overlap the old admitted scope, and every retained input
                    # must remain inside the requested focus. Actual access and
                    # byte proofs are checked again by the normal recipe reader.
                    previous_draft = binding['request'].get('prior_draft')
                    # The explicitly bound manuscript directory supplies prior
                    # prose and delivery copies, not a new primary-evidence
                    # focus. Explicit files elsewhere still require coverage.
                    context_root = self.workspace.resolve(f'workspace/manuscripts/{parent_id}')
                    evidence_focus = requested - {context_root}
                    evidence = {self.workspace.resolve_source_record(row['path'])
                        if index < len(binding['request']['sources']) else self.workspace.resolve(row['path'])
                        for index, row in enumerate(binding['inputs'])}
                    # Exported attachment copies are retained delivery bytes,
                    # not additional source locations for the next investigation.
                    for copy in binding.get('document_work', {}).get('materials', []):
                        evidence.discard(self.workspace.resolve(copy['path']))
                        evidence.add(self.workspace.resolve(copy['source']))
                    for row in binding['inputs']:
                        if row.get('manuscript_path'):
                            evidence.discard(self.workspace.resolve(
                                f"workspace/manuscripts/{parent_id}/{row['manuscript_path']}"))
                    if previous_draft:
                        evidence.discard(self.workspace.resolve(previous_draft))
                    same_scope = bool(evidence) and (not evidence_focus or all(any(path.is_relative_to(root)
                        for root in evidence_focus) for path in evidence) and all(
                        root in admitted or any(path.is_relative_to(root) for path in evidence)
                        for root in evidence_focus) and all(any(
                            root.is_relative_to(old) or old.is_relative_to(root)
                            for old in admitted if old != context_root) for root in evidence_focus))
                if frozen_investigation:
                    same_scope = False  # Replay the saved selection decision, not today's eligibility policy.
                draft = f'workspace/manuscripts/{parent_id}/manuscript.md'
                if observed is None:
                    native_id, observed = self._document_source_observation(parent_id)
                    if observed is None:
                        # Canonical draft reuse retains the completed producer
                        # proof used by exact callers, not a fabricated native result.
                        _, _, data = self._composition_receipt(parent_id)
                        observed = {'tool_name': 'compose_document', 'status': 'ok', 'data': data,
                            'artifacts': parent.read_json('result.json')['artifacts']}
                if observed.get('tool_name') != 'compose_document':
                    raise ValueError('Document source native receipt belongs to another operation')
                data = observed.get('data') or {}
                draft = data.get('editable_markdown') or draft
                interrupted = observed.get('status') != 'ok' or self._inspection_interrupted(data)
                if observed.get('status') == 'ok':
                    self._composition_receipt(native_id or parent_id, observed)
                    review = self.final_review(data)
                    if not same_scope or not same_controls:
                        # Explicit new scope investigates current permitted
                        # material. Existing defect history still follows any
                        # unchanged attachment actually selected for delivery.
                        return self._document_investigation(request, work_source, parent, binding, review)
                    attachment = self._document_attachment_gap(binding, review)
                    if attachment is not None:
                        if native_id is None:
                            raise ValueError('Attachment repair requires the actual registered composition operation')
                        gap = _DocumentSourceGap(f'records/{native_id}/native_result.json', review, producer=attachment)
                        origin = self._document_investigation(request, work_source, parent, binding, review).investigation
                        native = ArtifactStore(self.root / 'attempts' / native_id)
                        accepted = native.read_json('native_result.json')
                        _, digest = accepted_native_result(self.root, self.session_id, accepted, native_id)
                        admission = ArtifactStore(self.root / 'tools/writing' / identity)
                        gap.producer_diagnosis = {'contract': 'document_source_gap.v1',
                            'source_result': gap.source_result, 'source_sha256': digest,
                            'origin': origin, 'admission_sha256': hashlib.sha256(
                                admission.resolve('selection-input.json').read_bytes()).hexdigest()}
                        raise gap
                    if self._review_needs_material_work(review):
                        return self._document_investigation(request, work_source, parent, binding, review)
                elif not same_scope or not same_controls:
                    result.recipe = request.model_copy(update={'prior_result': '',
                        'prior_draft': draft if self.workspace.resolve(draft).is_file() else '',
                        'reuse_prior_inputs': False, 'material_roots': work_source['investigation_roots']})
                    return result
                method = (report or {}).get('requirement', {}).get('method')
                if report is None:
                    from simple_ar.report.writing import ARTICLE_OWNER, SECTION_ARTICLE_OWNER
                    method = {ARTICLE_OWNER: 'article', SECTION_ARTICLE_OWNER: 'sections'}.get(
                        binding.get('writing_owner'), method)
                feedback_requested = work_source['intent']['goal'] not in {'', original_goal}
                if isinstance(report, dict) and isinstance(previous_report, dict):
                    feedback_requested = feedback_requested or report.get('requests') != previous_report.get('requests')
                checked_section_revision = (not interrupted and method == 'sections'
                    and bool(data.get('unresolved'))
                    and self._section_author_source(parent)[1].get('checked_revision') is not None)
                checked_work = (binding.get('work_result_contract') == 'checked_candidate.v1'
                    and data.get('editable_markdown') == f'workspace/manuscripts/{parent_id}/candidate.md')
                same_structure = (request.writing_method in (None, binding['request'].get('writing_method'), method)
                    and (not request.required_headings or request.required_headings ==
                         binding['request'].get('required_headings', [])))
                retained_candidate = data.get('retained_candidate', {}).get('markdown')
                explicit_candidate = request.prior_draft == retained_candidate
                if selected_recipe and request.prior_draft and retained_candidate:
                    explicit_candidate = self.workspace.resolve(request.prior_draft) == self.workspace.resolve(retained_candidate)
                if (not selected_recipe or same_structure) and (interrupted or (method in {'article', 'sections'}
                        and native_id is not None
                        and (not data.get('unresolved') or checked_section_revision or checked_work)
                        and (not feedback_requested or (request.prior_result
                            or explicit_candidate) and (checked_section_revision or checked_work)))):
                    continued = request.model_copy(update={'prior_result': f'records/{native_id}/native_result.json',
                        'prior_draft': '', 'material_roots': [], 'reuse_prior_inputs': False})
                    (result.recipe, result.binding, result.linkage, result.candidate) = self._continued_document_inputs(
                        continued, identity, (report or {}).get('requirement'), allow_completed=not interrupted,
                        allow_standalone=True, checked_work_admitted=(original is None
                            or original.get('continued_source_review') == 'checked_work.v1'),
                        author_only=original is None or original.get('candidate_source', {}).get('contract')
                            == 'retained_section_author.v1')
                    return result
                _, _, retained = self._composition_receipt(parent_id)
                review = self.final_review(retained)
                request = request.model_copy(update={'prior_result': '', 'prior_draft': draft,
                    'material_roots': [], 'reuse_prior_inputs': True})
                result.feedback = (self._bound_revision_feedback(request, parent, binding, retained, review)
                    if selected_recipe and previous_report == report else
                    self._document_revision_feedback(request, previous_report))
        # Historical source resolution is an adapter: saved request and selection
        # bytes are replayed without gaining the new admission or scope semantics.
        if request.prior_result:
            if not document_work:
                raise ValueError('prior_result requires professional document-work ownership')
            (result.recipe, result.binding, result.linkage, result.candidate) = self._continued_document_inputs(
                request, identity, (report or {}).get('requirement'),
                inspect_only=original is None or original.get('continued_source_review') == 'inspect_only.v1',
                checked_work_admitted=(original is None
                    or original.get('continued_source_review') == 'checked_work.v1'),
                author_only=original is None or original.get('candidate_source', {}).get('contract')
                    == 'retained_section_author.v1')
            return result
        feedback = original.get('revision_feedback') if original is not None else result.feedback
        if feedback is not None:
            if (feedback.get('contract') != 'prior_prose_review.v1'
                    or (original is not None and work_source is None and request.material_roots != feedback['requested_roots'])
                    or self.workspace.artifact_hashes([feedback['path']])[feedback['path']] != feedback['sha256']):
                raise ValueError('Retained prose revision request or original review changed')
        elif (original is None and selection is None and document_work
                and not caller_fields.intersection({'materials', 'sources', 'figures', 'title',
                    'delivery_artifacts', 'reuse_prior_inputs', 'adopt_draft', 'producer_repair'})):
            feedback = self._document_revision_feedback(request, report)
        result.feedback = feedback
        if feedback is not None:
            request = request.model_copy(update={'material_roots': [], 'reuse_prior_inputs': True})
        if (document_work and request.prior_draft and not request.reuse_prior_inputs
                and not (request.material_roots or request.materials or request.sources)):
            path = self.workspace.resolve(request.prior_draft)
            if path in {self.workspace.resolve(f'workspace/manuscripts/{path.parent.name}/{leaf}')
                    for leaf in ('manuscript.md', 'candidate.md')}:
                request = request.model_copy(update={'reuse_prior_inputs': True})
        if request.reuse_prior_inputs:
            request, result.binding, result.linkage = self._prior_inputs(request, identity,
                original.get('prior_input_reuse') if original is not None else None,
                normalize_arguments=normalize_arguments)
            if document_work:
                inherited = result.binding['request']
                request = request.model_copy(update={'title': request.title or inherited['title'],
                    'delivery_artifacts': request.delivery_artifacts if request.delivery_artifacts is not None
                        else inherited.get('delivery_artifacts') or [],
                    'figures': request.figures or [ComposeFigure.model_validate(row)
                        if isinstance(row, dict) else row for row in inherited.get('figures', [])]})
        result.recipe = request
        return result

    def _prior_inputs(self, request: ComposeRequest, identity: str,
                      retained_linkage: dict | None = None, *,
                      normalize_arguments: Callable[[dict], dict] | None = None) -> tuple[ComposeRequest, dict, dict]:
        """Read a completed same-task producer, not certify journal adoption.

        Explicit input reuse grants no read authority, delivery status or budget.
        A domain-complete result can outlive an interrupted caller commit; its
        original evidence still has to pass current permissions and byte checks.
        """
        from simple_ar.report.writing import completed_composition_writer

        path = self.workspace.resolve(request.prior_draft)
        prior_id = path.parent.name
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', prior_id)
                or prior_id == identity
                or path not in {self.workspace.resolve(f'workspace/manuscripts/{prior_id}/{leaf}')
                    for leaf in ('manuscript.md', 'candidate.md')}):
            raise ValueError('Prior input reuse requires a distinct same-task manuscript or checked candidate')
        store = ArtifactStore(self.root / 'tools/writing' / prior_id)
        if store.root.resolve().parent != (self.root / 'tools/writing').resolve():
            raise ValueError('Prior composition owner escapes this task')
        # Fixed owner files only: neither search history nor infer a missing receipt.
        names = ('request.json', 'result.json', 'documents.json', 'writer/writer.json',
            'writer/report_inputs.json')
        if not all(store.exists(name) for name in names):
            raise ValueError('Prior input reuse requires a complete writing-owner receipt; no recovery')
        # Bind every retained parent receipt, including section calls, review
        # corrections/revisions and delegated selection, not just writer.json.
        paths = [path for path in store.root.rglob('*') if path.is_file()]
        if any(not path.resolve().is_relative_to(store.root.resolve()) for path in paths):
            raise ValueError('Prior composition receipt escapes its owner')
        hashes = {path.relative_to(store.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}
        if retained_linkage is not None:
            # Old opt-in requests froze six fixed receipts. Recheck exactly
            # that saved contract on replay; do not migrate their identity.
            recorded = retained_linkage['receipt_sha256']
            if any(hashes.get(name) != digest for name, digest in recorded.items()):
                raise ValueError('Prior composition receipt hashes changed')
            hashes = {name: hashes[name] for name in recorded}
        store, binding, data = self._composition_receipt(prior_id)
        if self.workspace.resolve(data['editable_markdown']) != path:
            raise ValueError('Prior composition has no matching version-bound author locator')
        if binding.get('writing_owner') == 'professional_report.v1':
            report = binding.get('report_binding', {})
            if report.get('evidence_contract') != 'materials.v1':
                raise ValueError('Explicit input reuse supports material professional reports only; '
                    'execution-report projections require their original owner, no fresh selection fallback')
            if self.workspace.artifact_hashes(report['evidence_sha256']) != report['evidence_sha256']:
                raise ValueError('Prior professional report evidence changed')
            if data.get('report_binding') != report:
                raise ValueError('Prior professional report receipt binding changed')
        completed_composition_writer(store=store, binding=binding, identity=prior_id)
        artifacts = data.get('artifact_sha256')
        # Old figure inputs and prior prose are context, not the reusable
        # evidence recipe. Current figures must be supplied explicitly.
        saved = binding['request']
        self._check_input_bytes(binding['inputs'][:len(saved['sources']) + len(saved['materials'])])
        if any(not store.exists(name) or hashlib.sha256(store.resolve(name).read_bytes()).hexdigest() != digest
               for name, digest in hashes.items()):
            raise ValueError('Prior composition receipts changed during completed-producer validation')
        # This is the owner's resolved recipe, not another caller request:
        # delegated selection retains its roots alongside resolved windows.
        # Validate only the existing typed selections; preserve the original
        # binding intact for Writer equality and byte linkage above/below.
        resolved = self._resolved_prior_recipe(request, saved,
            preserve_format=binding.get('work_source', {}).get('contract') == 'document_source.v1')
        sources, materials = resolved.sources, resolved.materials
        linkage = {'owner': prior_id, 'receipt_sha256': hashes, 'artifact_sha256': artifacts,
            'inputs': binding['inputs'][:len(sources) + len(materials)]}
        if request.producer_repair:
            return self._repaired_prior_inputs(request, resolved, binding, linkage,
                normalize_arguments=normalize_arguments)
        return resolved, binding, linkage

    def _resolved_prior_recipe(self, request: ComposeRequest, saved: dict, *,
                               preserve_format: bool = False) -> ComposeRequest:
        """Reuse selected original windows, never draft text as primary evidence."""
        sources = [SelectedSource.model_validate(row) for row in saved['sources']]
        materials = [FileWindow.model_validate(row) for row in saved['materials']]
        if not (sources or materials):
            raise ValueError('Prior composition has no resolved source/window recipe; execution-only '
                'professional reports are not supported by explicit input reuse, no fresh selection fallback')
        # Keep the saved selection representation. The common source/window
        # reader below consumes its resolved IDs and exact observed byte ranges.
        resolved = request.model_copy(update={'sources': sources, 'materials': materials})
        if preserve_format and request.writing_method is None and saved.get('writing_method') is not None:
            resolved = resolved.model_copy(update={'writing_method': saved['writing_method']})
        for field in ('required_headings', 'max_cited_sources'):
            if preserve_format and field not in request.model_fields_set and saved.get(field):
                resolved = resolved.model_copy(update={field: saved[field]})
        for window in materials:
            material = self.workspace.resolve(window.path)
            if (material.is_relative_to(self.workspace.resolve('workspace/manuscripts'))
                    or saved['prior_draft'] and material == self.workspace.resolve(saved['prior_draft'])):
                raise ValueError('Prior draft prose cannot be reused as primary material evidence')
        return resolved

    @staticmethod
    def _section_author_source(store: ArtifactStore, *, author_only: bool = False) -> tuple[dict, dict]:
        """Bind the existing author/checkpoint port once for both consumers."""
        from simple_ar.report.writing import section_author_source
        source, candidate = section_author_source(store, author_only=author_only)
        if candidate.get('checked_revision') is not None:
            from simple_ar.report.agent import _final_sequence
            from simple_ar.report.schema import ReportMemory, ReportSectionDraft
            from simple_ar.report.writing import validate_document_inspection
            candidate['sections'] = [row.model_dump(mode='json') for row in _final_sequence(
                ReportMemory.model_validate(candidate['memory']).section_plan,
                [ReportSectionDraft.model_validate(row) for row in candidate['sections']])]
            review = candidate['checked_revision']['independent_review']
            validate_document_inspection(store, store.read_json('request.json'),
                {'delivery_review': review['delivery_review']}, review_result=review,
                reviewed_sections=candidate['sections'])
        return source, candidate

    def _continued_document_inputs(self, request: ComposeRequest, identity: str,
                                  requirement: dict | None, *,
                                  inspect_only: bool = True, allow_completed: bool = False,
                                  allow_standalone: bool = False,
                                  checked_work_admitted: bool = True,
                                  author_only: bool = False) -> tuple[ComposeRequest, dict, dict, dict]:
        """Continue an author-complete failed owner, not its failed inspection.

        A native receipt locates the fixed same-task owner. The report operation
        proves author responses; this gateway revalidates its original material
        recipe and current permissions/bytes before any paid work. Nothing is
        written into or silently adopted from the stopped owner.
        """
        from simple_ar.report.writing import retained_article_candidate
        value = request.prior_result
        # Native receipts use task-relative ArtifactRefs; tools also expose the
        # read-only records mount. Resolve both through the same access gate.
        path = self.workspace.resolve(str(self.root / value) if value.startswith('attempts/') else value)
        try:
            relative = path.relative_to((self.root / 'attempts').resolve())
        except ValueError:
            raise ValueError('prior_result requires a same-task native composition result') from None
        if (len(relative.parts) != 2 or relative.name != 'native_result.json'
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', relative.parts[0])
                or relative.parts[0] == identity):
            raise ValueError('prior_result requires a distinct same-task composition result')
        observed = json.loads(path.read_bytes())
        if observed.get('tool_name') != 'compose_document':
            raise ValueError('prior_result requires a composition result')
        accepted_native_result(self.root, self.session_id, observed, relative.parts[0], ledger=self.ledger)
        store = composition_owner(root=self.root, workspace=self.workspace,
            session_id=self.session_id, identity=relative.parts[0])
        if store.root.resolve().parent != (self.root / 'tools/writing').resolve():
            raise ValueError('Stopped composition owner escapes this task')
        binding = store.read_json('request.json')
        from simple_ar.report.writing import ARTICLE_OWNER, SECTION_ARTICLE_OWNER
        standalone = (allow_standalone and binding.get('writing_owner') in {ARTICLE_OWNER, SECTION_ARTICLE_OWNER}
            and binding.get('report_binding') is None and requirement is None)
        method = binding.get('report_binding', {}).get('requirement', {}).get('method')
        if standalone:
            method = 'sections' if binding['writing_owner'] == SECTION_ARTICLE_OWNER else 'article'
        section_candidate = None
        if method == 'article':
            source = retained_article_candidate(store, binding, **({'allow_standalone': True} if standalone else {}))
            names = ['request.json', 'documents.json', *(file.relative_to(store.root).as_posix()
                for file in (store.root / 'writer').rglob('*.json'))]
            candidate_contract = 'retained_article_author.v1'
        elif method == 'sections':
            pending_inspection = any(not path.with_name('response.json').exists()
                and json.loads(path.read_bytes()).get('options', {}).get('label') in {
                    'report-document-reviewer', 'report-document-verifier'}
                for path in (store.root / 'writer/model_calls').glob('*/request.json'))
            source, section_candidate = self._section_author_source(store,
                author_only=author_only and pending_inspection)
            if source['contract'] == 'retained_section_author.v1':
                self._unstarted_section_inspection(store, section_candidate)
            names = list(source['sha256'])
            candidate_contract = source['contract']
        else:
            raise ValueError('Stopped composition has no supported author owner')
        failed_author = (observed.get('status') == 'error' and observed.get('error')
            and observed.get('data', {}).get('incomplete'))
        checked_revision = (section_candidate or {}).get('checked_revision')
        checked_work = None
        # A new work order may use the completed negative check. Reconstructing
        # an old paid request must retain its original check/author source.
        if checked_work_admitted and (observed.get('data', {}).get('failure_kind') == 'image_reference'
                or observed.get('status') == 'ok' and binding.get('work_result_contract') == 'checked_candidate.v1'
                    and observed.get('data', {}).get('editable_markdown') ==
                        f"workspace/manuscripts/{store.root.name}/candidate.md"):
            from simple_ar.report.writing import checked_writing_candidate
            checked_work = checked_writing_candidate(store, binding)
        pending_author = (observed.get('status') == 'ok' and (
            method == 'article' and observed.get('data', {}).get('unresolved')
                and Path(source['path']).parent != store.root / 'writer'
            or method == 'sections' and checked_revision is not None or checked_work is not None))
        completed_author = allow_completed and observed.get('status') == 'ok' and not observed.get('data', {}).get('unresolved')
        if not failed_author and not pending_author and not completed_author:
            raise ValueError('prior_result requires retained failed author work or an unadopted complete revision; '
                'use prior_draft for completed compositions')
        if pending_author or completed_author:
            self._composition_receipt(relative.parts[0], observed)
        if inspect_only and method == 'article' and not completed_author and checked_work is None:
            source = retained_article_candidate(store, binding, unchecked_revision=True,
                **({'allow_standalone': True} if standalone else {}))
        receipt_hashes = {name: hashlib.sha256(store.resolve(name).read_bytes()).hexdigest() for name in names}
        if not standalone and (requirement is None or binding.get('report_binding', {}).get('requirement') != requirement
                or binding['report_binding'].get('evidence_contract') != 'materials.v1'):
            raise ValueError('Continued author work must retain its original material report controls')
        saved = binding['request']
        resolved = self._resolved_prior_recipe(request, saved,
            preserve_format=binding.get('work_source', {}).get('contract') == 'document_source.v1')
        inputs = binding['inputs'][:len(resolved.sources) + len(resolved.materials)]
        self._check_input_bytes(inputs)
        if not standalone and self.workspace.artifact_hashes(binding['report_binding']['evidence_sha256']) != binding['report_binding']['evidence_sha256']:
            raise ValueError('Stopped professional work evidence changed; request fresh material investigation')
        source_paths = [row['path'] for row in binding['inputs']]
        self.workspace.check_artifact_producers(source_paths)
        versions = {row['path']: row['sha256'] for row in binding['inputs'] if row.get('sha256') is not None}
        if self.workspace.artifact_hashes(versions) != versions:
            raise ValueError('Stopped professional work inputs or attachments changed')
        resolved = resolved.model_copy(update={'title': saved['title'],
            'target_words': request.target_words or saved['target_words'],
            **({'max_cited_sources': saved.get('max_cited_sources', 0),
                'required_headings': saved.get('required_headings', [])}
                if inspect_only and (method == 'article' or candidate_contract == 'retained_section_author.v1') else {}),
            'figures': [ComposeFigure.model_validate(row) if isinstance(row, dict) else row for row in saved['figures']],
            'publication': (request.publication if request.publication is not None else
                CompositionPublication.model_validate(saved['publication']) if saved.get('publication') else None),
            'delivery_artifacts': saved.get('delivery_artifacts') or []})
        if standalone and method == 'article' and checked_work is None:
            resolved = resolved.model_copy(update={'professional_review': ProfessionalReview(max_revision_rounds=0)})
        linkage = {'owner': store.root.name, 'receipt_sha256': receipt_hashes,
            'artifact_sha256': versions, 'inputs': inputs, 'candidate_contract': candidate_contract,
            'result_ref': self.workspace.locator(str(path)), 'result_sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        if store.root.name != relative.parts[0]:
            linkage['native_owner'] = relative.parts[0]
        if checked_work is not None:
            linkage['checked_work'] = True
        return resolved, binding, linkage, source

    def _repaired_prior_inputs(self, request: ComposeRequest, resolved: ComposeRequest,
                               binding: dict, linkage: dict, *,
                               normalize_arguments: Callable[[dict], dict] | None = None) -> tuple[ComposeRequest, dict, dict]:
        """Transport one proven recipe to its replacement producer; no selection."""
        execution = self.workspace.resolve(request.producer_repair)
        relative = execution.relative_to(self.root / 'attempts').parts
        if len(relative) != 2 or relative[1] != 'candidate_execution.json':
            raise ValueError('Producer reuse needs a registered successful execution receipt')
        observed = ArtifactStore(execution.parent).read_json('native_result.json')
        repair = self._producer_repair(observed)
        expected = (normalize_arguments(repair['arguments']) if normalize_arguments else repair['arguments']) if repair else None
        if (repair is None or repair['owner'] != linkage['owner']
                or self.workspace.resolve(repair['execution_ref']) != execution
                or ComposeRequest.model_validate(expected).model_dump() != request.model_dump()):
            raise ValueError('Producer reuse differs from the proven original composition and repair lineage')
        # The old binding remains immutable. This in-memory consumer recipe is
        # checked by the same source/window reader and persisted in prior_input_reuse.
        evidence = json.loads(json.dumps(binding))
        inputs = json.loads(json.dumps(linkage['inputs']))
        sources, materials, changed_materials, changed_chunks = [], [], [], []
        replacements, relocated_materials = [], []
        for index, source in enumerate(resolved.sources):
            locator = repair['replace'](source.document_record)
            sources.append(source.model_copy(update={'document_record': locator}))
            if locator != source.document_record:
                path = self.workspace.resolve_source_record(locator)
                if not path.is_file() or path.stat().st_size > 8_000_000:
                    raise ValueError('Repaired source record is unavailable or exceeds its original reader bound')
                from simple_ar.research.documents.ingest import DocumentBundle
                old_bundle = DocumentBundle.from_handoff_dict(json.loads(
                    self.workspace.resolve_source_record(source.document_record).read_bytes()))
                new_bundle = DocumentBundle.from_handoff_dict(json.loads(path.read_bytes()))
                selected = inputs[index].get('selected_chunk_ids', source.chunk_ids)
                old_chunks = {row.chunk_id: row for row in old_bundle.chunks}
                new_chunks = {row.chunk_id: row for row in new_bundle.chunks}
                if any(key not in old_chunks or old_chunks.get(key) != new_chunks.get(key) for key in selected):
                    raise ValueError('Repaired source cannot prove the same selected passages; no guessed chunk remapping')
                documents = {old_chunks[key].document_id for key in selected}
                def source_identity(bundle: Any) -> tuple:
                    return ([replace(row, local_path=None).to_row() for row in bundle.records if row.document_id in documents],
                        [row for row in bundle.sections if row.document_id in documents and row.section == 'front_matter'])
                if source_identity(old_bundle) != source_identity(new_bundle):
                    raise ValueError('Repaired source changed selected original document identities')
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                replacements.append({'old_path': inputs[index]['path'], 'old_sha256': inputs[index]['sha256'],
                    'path': locator, 'sha256': digest})
                inputs[index].update(path=locator, sha256=digest)
        for number, window in enumerate(resolved.materials, 1):
            index = len(sources) + number - 1
            locator = repair['replace'](window.path)
            if locator == window.path:
                materials.append(window)
                continue
            old = inputs[index]
            previous_path, current_path = self.workspace.resolve(window.path), self.workspace.resolve(locator)
            if repair['old_versions'][previous_path] == repair['new_versions'][current_path]:
                # A new locator is not a new selection when the proven complete
                # file bytes are identical. Retain offset/end, line selection,
                # window digest and original read bound exactly.
                materials.append(window.model_copy(update={'path': locator}))
                inputs[index] = {**old, 'path': locator}
                replacements.append({'old_path': old['path'], 'old_sha256': old['sha256'],
                    'path': locator, 'sha256': old['sha256']})
                relocated_materials.append({'document_id': f'compose-material-{number}',
                    'old_path': old['path'], 'path': locator})
                continue
            # Offsets in changed bytes cannot prove the same semantic selection.
            # A complete selected file is a whole-file evidence scope, not an
            # immutable byte length. Allow its proven replacement to grow under
            # FileWindow's shared reader ceiling. Partial selections retain
            # their original bound; never guess shifted offsets or clip tails.
            selection = old.get('selection', {})
            whole = (selection.get('offset_bytes') == 0 and selection.get('has_more') is False
                and selection.get('next_offset_bytes') == selection.get('size_bytes'))
            updated = FileWindow(path=locator, offset_bytes=0,
                max_bytes=max(window.max_bytes, current_path.stat().st_size) if whole else window.max_bytes,
                full_digest=True)
            read = self.workspace.call('read_file', updated.model_dump(exclude_unset=True),
                identity='writing-producer-reuse')
            if (read.status != 'ok' or not read.data.get('text', '').strip()
                    or '\x00' in read.data['text'] or read.data.get('has_more')
                    or read.data.get('offset_bytes', 0) != 0
                    or read.data['next_offset_bytes'] != read.data['size_bytes']):
                raise ValueError('Complete repaired material exceeds the original selected-window bound or is unavailable')
            view = read.data
            selection = {key: view[key] for key in ('size_bytes', 'next_offset_bytes', 'has_more', 'start_line') if key in view}
            selection.update(offset_bytes=0,
                window_sha256=hashlib.sha256(view['text'].encode('utf-8')).hexdigest())
            replacements.append({'old_path': old['path'], 'old_sha256': old['sha256'],
                'path': locator, 'sha256': view['sha256']})
            inputs[index] = {'path': locator, 'sha256': view['sha256'], 'selection': selection}
            materials.append(updated)
            changed_materials.append(f'compose-material-{number}')
        evidence['request'].update(sources=[row.model_dump() for row in sources],
            materials=[row.model_dump() for row in materials])
        evidence['inputs'][:len(inputs)] = inputs
        linkage = {**linkage, 'inputs': inputs, 'producer_repair': {
            'contract': 'producer_recipe.v1', 'execution_ref': repair['execution_ref'],
            'execution_sha256': repair['execution_sha256'], 'review_ref': repair['review_ref'],
            'review_sha256': hashlib.sha256(self.workspace.resolve(repair['review_ref']).read_bytes()).hexdigest(),
            'historical_project': str(repair['old_project']), 'current_project': str(repair['new_project']),
            'replacements': replacements, 'relocated_materials': relocated_materials,
            'source_replacements': repair['source_replacements'],
            'changed_materials': changed_materials, 'changed_chunks': list(dict.fromkeys(changed_chunks))}}
        return resolved.model_copy(update={'sources': sources, 'materials': materials}), evidence, linkage

    def _document_revision_feedback(self, request: ComposeRequest, report_binding: dict | None) -> dict | None:
        """Reuse a proven recipe for a review fix, not a new user request.

        Repeated roots are access scope, not an instruction to rediscover the
        same evidence. Missing evidence, changed scope and attachment defects
        still require the responsible professional's investigation/repair.
        """
        if not report_binding or not request.prior_draft:
            return None
        path = self.workspace.resolve(request.prior_draft)
        if path not in {self.workspace.resolve(f'workspace/manuscripts/{path.parent.name}/{leaf}')
                for leaf in ('manuscript.md', 'candidate.md')}:
            return None
        store = ArtifactStore(self.root / 'tools/writing' / path.parent.name)
        if not all(store.exists(name) for name in ('request.json', 'result.json')):
            return None
        store, binding, data = self._composition_receipt(path.parent.name)
        if self._inspection_interrupted(data):
            return None
        if binding.get('report_binding') != report_binding or not binding.get('document_work'):
            return None  # A new feedback event never silently inherits old scope.
        roots = lambda values: {self.workspace.resolve_material_root(value) for value in values}
        # The same request may point at fewer original access roots and its
        # current manuscript directory. Neither adds new evidence. Any genuinely
        # new root still requires investigation; scope changes are rejected above.
        scope = self._document_access_roots(store, binding)
        admitted_roots = roots(scope) | {path.parent} if scope is not None else {path.parent}
        if request.material_roots and (scope is None or not roots(request.material_roots) <= admitted_roots):
            return None
        review = self.final_review(data)
        return self._bound_revision_feedback(request, store, binding, data, review)

    def _bound_revision_feedback(self, request: ComposeRequest, store: ArtifactStore,
                                 binding: dict, data: dict, review: dict) -> dict | None:
        """One proof for editorial opinions, shared by recipe reuse and exact callers."""
        if (review.get('review_status') != 'needs_revision'
                or self._review_needs_material_work(review)):
            return None
        from simple_ar.report.writing import validate_document_inspection
        self._check_input_bytes(data['inputs'])
        if data['editable_markdown'] == f"{data['version']}/candidate.md":
            self._check_work_receipt(store, binding, data)
        else:
            self._check_document_copy_review(data)
            validate_document_inspection(store, binding, data, review_result=review)
        review_path = self.workspace.resolve(data['review_path'])
        return {'contract': 'prior_prose_review.v1', 'requested_roots': request.material_roots,
            'path': str(review_path), 'sha256': hashlib.sha256(review_path.read_bytes()).hexdigest(),
            'section_reviews': review['section_reviews'],
            'scope': 'Version-bound editorial feedback, not primary evidence or permission. '
                'The original user request and evidence recipe are unchanged; recheck the resulting draft.'}

    def can_reconcile(self, name: str, identity: str) -> bool:
        """Allow dispatch to this owner, without certifying a completed article."""
        return name in {'compose_document', 'review_document'} and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', identity)) and (
            (self.root / 'tools' / 'writing' / identity / 'request.json').is_file()
            or name == 'compose_document' and (self.root / 'tools/writing' / identity / 'resume.json').is_file())

    def coordination_result(self, result: ToolResult, *, receipt_path: str | None = None) -> dict[str, Any]:
        """Present an observation, never replace a canonical ToolResult receipt.

        Call after persistence/delivery binding, only at the model presentation
        boundary. Recovery, reuse, publication and artifact registration must
        continue consuming the original result. ``receipt_path`` can point to
        the application's native receipt (including failures); successful owner
        results otherwise identify their original writing receipt by version.
        No model calls, new checkpoints, or inferred publication approval.
        """
        from copy import deepcopy

        if result.tool_name not in {'compose_document', 'review_document'}:
            return result.model_dump(mode='json')
        data = result.data
        version = data.get('version', '')
        match = re.fullmatch(r'workspace/manuscripts/([A-Za-z0-9][A-Za-z0-9_-]{0,100})', version)
        if receipt_path is None and match:
            receipt_path = f'tools/writing/{match[1]}/result.json'
        # All actionable diagnostics are verbatim. Evidence recipes, selection
        # transcripts and hashes belong to the linked receipt, not a second
        # coordinator interpretation of the professional owner's materials.
        visible = {key: deepcopy(data[key]) for key in (
            'version', 'editable_markdown', 'review_path', 'scope', 'unresolved',
            'constraints_checked', 'review_status', 'section_reviews',
            'mechanical_constraints', 'delivery',
            'delivery_review',
            'work_status', 'publication_stage',
            'incomplete', 'failure_kind', 'next_action', 'retained_candidate', 'pending_candidate') if key in data}
        review = data.get('independent_review')
        if isinstance(review, dict) and 'review_status' in review:
            visible.setdefault('review_status', review['review_status'])
        if match and result.status == 'ok' and data.get('editable_markdown') == f'{version}/candidate.md':
            try:
                self._composition_receipt(match[1], result.model_dump(mode='json'))
            except (OSError, ValueError, KeyError, TypeError):
                visible.pop('retained_candidate', None)
        if data.get('failure_kind') == 'image_reference' and receipt_path:
            identity = Path(receipt_path).parent.name
            if self.can_return_gap(result.model_dump(mode='json'), identity):
                from simple_ar.report.writing import checked_writing_candidate
                owner = ArtifactStore(self.root / 'tools/writing' / identity)
                candidate = checked_writing_candidate(owner, owner.read_json('request.json'))
                visible['retained_candidate'] = {'status': 'checked_not_adopted', 'adopted': False,
                    'review_status': 'needs_revision', 'prior_result': f'records/{identity}/native_result.json',
                    'scope': 'Saved complete author and negative inspection; original error is retained. '
                        'Continue this candidate through an admitted professional request, not by replaying the failed operation.'}
                visible['unresolved'] = [finding for row in candidate['independent_review']['section_reviews']
                    for finding in row['findings']]
        elif result.status != 'ok' and receipt_path:
            try:
                gap = self._author_completion_gap(result.model_dump(mode='json'), Path(receipt_path).parent.name)
            except (OSError, ValueError, KeyError, TypeError):
                gap = None
            if gap is not None:
                visible['mechanical_constraints'] = gap
                visible['retained_candidate'] = {'status': 'author_not_adopted', 'adopted': False,
                    'prior_result': gap['prior_result'], 'scope': gap['scope']}
        if (match and result.status == 'ok' and data.get('unresolved')
                and 'retained_candidate' not in visible
                and (self.root / 'attempts' / match[1] / 'native_result.json').is_file()):
            try:
                store, binding, _ = self._composition_receipt(match[1], result.model_dump(mode='json'))
                if binding.get('section_delivery_review_contract'):
                    _, candidate = self._section_author_source(store)
                    if checked := candidate.get('checked_revision'):
                        visible['retained_candidate'] = {
                            'status': 'checked_not_adopted', 'adopted': False,
                            'prior_result': f'records/{match[1]}/native_result.json',
                            'revision_iteration': checked['revision_iteration'],
                            'review_status': 'needs_revision',
                            'scope': 'Complete changed author body with a retained negative inspection; '
                                'continue through prior_result without selecting or redrafting its original. '
                                'Required findings remain unresolved; this is not accepted delivery.'}
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Invalid or incomplete work gains no continuation claim.
        inspection = data.get('delivery_review')
        if isinstance(inspection, dict) and inspection.get('contract') == 'delivery_artifacts.v1':
            # Execution graphs and full producer inputs remain in the receipt.
            # Repeating them here can evict the very inspection disposition the
            # coordinator needs. Keep every actionable defect/gap verbatim.
            coverage: dict[str, int] = {}
            for artifact in inspection.get('artifacts', []):
                key = artifact.get('coverage', 'unknown')
                coverage[key] = coverage.get(key, 0) + 1
            visible['delivery_review'] = {key: deepcopy(inspection[key]) for key in
                ('contract', 'status', 'defects', 'uninspected') if key in inspection}
            visible['delivery_review']['artifact_coverage_counts'] = coverage
            visible['delivery_review']['visually_reviewed_count'] = sum(
                row.get('visual_reviewed') is True for row in inspection.get('artifacts', []))
        # A small receipt-derived disposition also survives eviction of the
        # complete observation. Absence never means acceptance; consumers must
        # still validate the linked canonical records, versions and findings.
        outcome = {key: deepcopy(data[key]) for key in
            ('review_status', 'incomplete', 'failure_kind', 'pending_candidate') if key in data}
        if 'review_status' in visible:
            outcome['review_status'] = visible['review_status']
        if isinstance(inspection, dict) and 'status' in inspection:
            outcome['delivery_review_status'] = inspection['status']
        if 'unresolved' in data:
            outcome['unresolved_count'] = len(data['unresolved'])
        return {'tool_name': result.tool_name, 'status': result.status,
            'summary': result.summary, 'error': result.error,
            'artifacts': list(result.artifacts), 'data': visible,
            **({'outcome': outcome} if outcome else {}),
            'receipt_path': receipt_path}

    def _composition_receipt(self, identity: str, observed: dict | None = None) -> tuple[ArtifactStore, dict, dict]:
        return read_composition_receipt(root=self.root, workspace=self.workspace, session_id=self.session_id,
            identity=identity, observed=observed)

    def _check_work_receipt(self, store: ArtifactStore, binding: dict, data: dict) -> dict:
        return check_work_receipt(root=self.root, workspace=self.workspace, session_id=self.session_id,
            store=store, binding=binding, data=data)

    def _save_negative_work(self, owner: ArtifactStore, binding: dict, *, prefix: str,
                            figure_inputs: list, document_materials: dict | None,
                            physical_identity: str | None = None) -> ToolResult:
        """Finish checked work before attempting publication of the adopted body."""
        from simple_ar.report.writing import checked_writing_candidate
        from simple_ar.report.schema import ReviewerFinding, finding_requires_resolution
        candidate = checked_writing_candidate(owner, binding)
        review = candidate['independent_review']
        self._check_input_bytes(binding['inputs'])
        self.workspace.check_artifact_producers(row['path'] for row in binding['inputs'])
        output = ArtifactStore(self.workspace.resolve(prefix, write=True))
        texts = {'candidate.md': candidate['report_body_markdown'],
            'pending_candidate.json': json.dumps(candidate, ensure_ascii=False, indent=2) + '\n',
            'review.json': json.dumps(review, ensure_ascii=False, indent=2) + '\n'}
        artifacts = []
        for name, text in texts.items():
            if output.exists(name):
                if output.read_text(name) != text:
                    raise ValueError('Retained negative work changed; preserve the original')
            else:
                output.write_text(name, text)
            artifacts.append(f'{prefix}/{name}')
        for _, target, raw in figure_inputs:
            path = self.workspace.resolve(f'{prefix}/{target}', write=True)
            if path.exists() and path.read_bytes() != raw:
                raise ValueError('Retained work asset differs from its bound bytes')
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            artifacts.append(f'{prefix}/{target}')
        if document_materials is not None:
            artifacts.extend(row['path'] for row in document_materials['materials'])
            artifacts.extend(path for unit in document_materials.get('producer_units', []) for path in unit['files'])
        artifacts = list(dict.fromkeys(artifacts))
        data = {'version': prefix, 'inputs': binding['inputs'], 'editable_markdown': f'{prefix}/candidate.md',
            'artifact_sha256': self.workspace.artifact_hashes(artifacts),
            'review_path': f'{prefix}/review.json', 'pending_candidate': f'{prefix}/pending_candidate.json',
            'independent_review': review,
            'retained_candidate': {'status': 'checked_not_adopted', 'adopted': False,
                'markdown': f'{prefix}/candidate.md', 'review_status': 'needs_revision',
                'prior_result': f'records/{physical_identity or owner.root.name}/native_result.json'},
            'unresolved': [finding for row in review['section_reviews'] for finding in row['findings']
                if finding_requires_resolution(ReviewerFinding.model_validate(finding))],
            'scope': 'Complete independently checked work with unresolved findings; candidate is not adopted or publication-ready.'}
        if review.get('delivery_review', {}).get('defects'):
            data['unresolved'].extend(review['delivery_review']['defects'])
        if not data['unresolved']:
            raise ValueError('Negative work must retain its required findings')
        if 'delivery_review' in review:
            data['delivery_review'] = review['delivery_review']
        if binding.get('report_binding') is not None:
            data['report_binding'] = binding['report_binding']
        if document_materials is not None:
            data['document_work'] = document_materials
            data['title'] = binding['request']['title']
        result = {'artifacts': artifacts, 'data': data,
            'summary': 'Complete checked candidate saved; required findings remain. No canonical manuscript or publication was issued.'}
        self._check_work_receipt(owner, binding, data)
        owner.write_json('result.json', result)
        return ToolResult(tool_name='compose_document', **result)

    @staticmethod
    def _publication_options(binding: dict) -> dict:
        """One title/presentation policy for preparation and final publication."""
        options = CompositionPublication.model_validate(binding['request']['publication']).model_dump(mode='json')
        if binding.get('document_work', {}).get('contract') in _DOCUMENT_WORK_CONTRACTS:
            title_contract = binding['document_work'].get('publication_title_contract')
            if title_contract not in {None, 'canonical_heading.v1'}:
                raise ValueError('Unknown document publication title contract')
            if not options['title'] and title_contract != 'canonical_heading.v1':
                options['title'] = binding['request']['title']
        return options

    def continuation(self, observed: dict, attempt_id: str) -> dict | None:
        """Read retained successful composition; propose export via normal dispatch.

        No recovery runs, model calls, writes, or goal interpretation. The parent
        owns approval and execution. Publication revalidates its normal inputs.
        Historical requests without explicit publication produce no continuation.
        Document-work finish binds delivery to the export proposal; composition
        itself never becomes a terminal delivery observation.
        """
        if observed.get('status') == 'error':
            return self._unstarted_inspection_action(observed, attempt_id)
        if (observed.get('tool_name') != 'compose_document' or observed.get('status') != 'ok'
                or observed.get('error')
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', attempt_id)):
            return None
        try:
            if observed.get('data', {}).get('work_status') == 'awaiting_publication_preparation':
                stage, _ = self._publication_stage(observed, attempt_id)
                return {'tool_name': 'export_report', 'arguments': ExportRequest(
                    markdown=stage['markdown'], candidate_result=f'records/{attempt_id}/native_result.json',
                    **stage['publication']).model_dump(mode='json')}
            store, binding, data = self._composition_receipt(attempt_id, observed)
            attempt_id = store.root.name
            options = binding['request'].get('publication')
            if options is None and not binding['request'].get('finish'):
                return None
            markdown = f'workspace/manuscripts/{attempt_id}/manuscript.md'
            if (data.get('editable_markdown') != markdown
                    or data.get('unresolved') or data.get('incomplete')
                    or data.get('delivery_review', {}).get('status', 'reviewed') != 'reviewed'):
                return None
            review = self.final_review(data)
            if review.get('review_status') != 'reviewed':
                return None
            arguments = (ExportRequest(markdown=markdown,
                **self._publication_options(binding)).model_dump(mode='json')
                if options is not None else {})
            if binding.get('document_work', {}).get('contract') in _DOCUMENT_WORK_CONTRACTS:
                if binding['request'].get('finish'):
                    # Inspect the actual retained independent response, not a
                    # status flag, before expressing this unit's final delivery.
                    self._check_bound_delivery(store, binding, data, review=review, independent=True)
                    if data.get('document_work') != binding['document_work']:
                        return None
                    for row in binding['document_work']['materials']:
                        if data['artifact_sha256'].get(row['path']) != row['sha256']:
                            return None
                    delivery = DeliveryRequest(summary=binding['request']['goal'],
                        artifacts=list(observed['artifacts']), report={'status': 'canonical',
                            'manuscript': markdown}).model_dump(mode='json')
                    if options is None:
                        formats = (binding.get('report_binding') or {}).get('obligation', {}).get('formats', [])
                        if any(fmt != 'markdown' for fmt in formats):
                            return None
                        return {'tool_name': 'deliver_result', 'arguments': delivery}
                    arguments['delivery'] = delivery
            if options is None:
                return None  # Historical/exact work cannot acquire implicit finish authority.
            return {'tool_name': 'export_report', 'arguments': arguments}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def final_review(self, data: dict) -> dict:
        return read_composition_review(self.workspace, data)

    def _composition_publication(self, store: ArtifactStore, binding: dict, data: dict, *,
                                 review: dict | None = None) -> dict | None:
        """One retained publication proof for fresh checking and final export."""
        review = self.final_review(data) if review is None else review
        receipt = review.get('inspection_source') or {}
        prepared = None
        if receipt:
            path = store.resolve(receipt['request_path']).resolve()
            if not path.is_relative_to(store.root.resolve()):
                raise ValueError('Publication inspection request is outside the composition')
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != receipt['request_sha256']:
                raise ValueError('Publication inspection request changed')
            prepared = json.loads(json.loads(raw)['prompt']).get('prepared_publication')
        if prepared is None:
            if binding.get('publication_inspection_contract') == 'prepared_document.v1':
                raise ValueError('Retained publication inspection view is missing')
            return None
        if self.prepared_publication is None:
            raise ValueError('Prepared publication validation is unavailable')
        current = self.prepared_publication(prepared['prepared_result'])
        if (current != prepared or hashlib.sha256(self.workspace.resolve(data['editable_markdown']).read_bytes()).hexdigest()
                != current['source']['assembly_identity']['body_sha256']):
            raise ValueError('Composition differs from its independently inspected publication')
        return current

    def _review_publication(self, prior_draft: str, markdown: str,
                            original: dict | None) -> dict | None:
        """Read the exact retained inspection view, without adoption or export."""
        keys = ('prepared_result', 'prepared_result_sha256', 'view_path', 'view_sha256')
        if original is not None:
            if 'prepared_publication' not in original:
                return None  # Historical source-only reviews keep their saved scope.
            reference = original['prepared_publication']
            if (not isinstance(reference, dict) or set(reference) != set(keys)
                    or any(not isinstance(reference[key], str) or not reference[key] for key in keys)):
                raise ValueError('Invalid retained review publication reference')
        else:
            if self.publication_inspection != 'prepared_document.v1':
                return None
            reference = None
        path = self.workspace.resolve(prior_draft)
        identity = path.parent.name
        if path not in {self.workspace.resolve(f'workspace/manuscripts/{identity}/{leaf}')
                for leaf in ('manuscript.md', 'candidate.md')}:
            if reference is not None:
                raise ValueError('Bound review publication requires its exact retained manuscript')
            return None
        store, binding, data = self._composition_receipt(identity)
        if path != self.workspace.resolve(data['editable_markdown']):
            raise ValueError('Review draft differs from its retained composition')
        if ('independent_review' not in data and 'review_path' not in data
                and reference is None and binding.get('publication_inspection_contract') is None):
            return None
        current = self._composition_publication(store, binding, data)
        if current is None:
            if reference is not None:
                raise ValueError('Retained review publication view is missing')
            return None
        if reference is not None and reference != {key: current[key] for key in keys}:
            raise ValueError('Retained review publication view changed')
        body_sha = current['source']['assembly_identity']['body_sha256']
        if hashlib.sha256(markdown.encode('utf-8')).hexdigest() != body_sha:
            raise ValueError('Review body differs from the prepared source assembly body')
        return current
