"""Evidence binding, observation reuse, and delivery assessment helpers for InspectionToolGateway."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

from simple_ar.core.assessments import reassessment_has_subject_support
from simple_ar.core.budget import completed_llm_usage_is_accounted
from simple_ar.core.capabilities import ArtifactStore
from simple_ar.tools.inspection_contracts import (
    _BINARY,
    _ClaimDeliveryResponse,
    _DeliveryResponse,
    _DeliveryResponseLinked,
    _InvestigatedDeliveryResponse,
    _PRESENTATION_RESULTS,
    _PRESENTED_EVIDENCE_POLICIES,
    _PRIORITY_SOURCE_POLICIES,
    _PresentedClaimDeliveryResponse,
    _PresentedClaimDeliveryResponseV4,
    _PresentedClaimInvestigation,
    _Response,
    _ReviewResponse,
    _ReviewResponseV2,
    _SHARED_EXECUTION_PROTOCOLS,
    _ScopedDeliveryResponse,
    _SubjectDecision,
    _SubjectResponse,
)
from simple_ar.tools.materials import lossless_text_window
from simple_ar.tools.specs import (
    CLAIM_WORK_METHODS,
    GOAL_WORK_METHOD,
    NATIVE_WORK_METHODS,
    ToolResult,
    tool_result_payload,
)
from simple_ar.tools.workspace import FileWindow


class _InspectionEvidenceMixin:
    """Evidence observation, reuse, and delivery assessment methods."""

    def _check_shared_dependencies(self, saved: dict, *, current_inputs: bool,
                                   selected: list[dict] | None = None,
                                   include_delivery: bool = True,
                                   visited: set[tuple[str, str]] | None = None,
                                   journal_cache: dict | None = None,
                                   validation_records: dict | None = None) -> dict[str, str]:
        if saved.get('execution_protocol') not in _SHARED_EXECUTION_PROTOCOLS:
            return {}
        dependencies: dict[str, str] = {}
        journal_cache = {} if journal_cache is None else journal_cache
        local_validation = validation_records is None
        validation_records = {} if validation_records is None else validation_records

        def bind(path: str, digest: str) -> None:
            path = str(self.workspace.resolve(path))
            if path in dependencies and dependencies[path] != digest:
                raise ValueError('Selected inspection evidence has conflicting input versions')
            dependencies[path] = digest
        selected_paths = ({str(self.workspace.resolve(row['path'])) for row in selected or [] if row.get('path')}
            if current_inputs else set())
        if current_inputs and selected is not None and include_delivery and self._delivery_scope(saved):
            selected_paths.update(str(self.workspace.resolve(path)) for path in saved['delivery_context']['artifacts'])
        sources: dict[str, dict] = {}
        if (current_inputs and selected_paths and self._applicable_scope(saved)
                and not self._observed_sources(saved)):
            # Saved v1 reviews retain their original parent-source dependency
            # contract. New reviews treat receipt JSON as the observed object,
            # never as automatic supply of its embedded source windows.
            from simple_ar.tools.materials import validate_material_observation
            for entry in saved.get('source_history', {}).get('entries', []):
                for reference in entry['records']:
                    try:
                        record = str(self.workspace.resolve(reference['record']))
                    except (ValueError, OSError):
                        continue
                    if record not in selected_paths:
                        continue
                    observed = validate_material_observation(self.workspace, reference,
                        identity='inspection-source')
                    bind(entry['path'], observed['data']['sha256'])
                    for path, digest in self._reused_source_dependencies(reference, entry['path'],
                            validated=observed, visited=visited, journal_cache=journal_cache,
                            validation_records=validation_records).items():
                        bind(path, digest)
        if current_inputs and selected_paths:
            selected_versions = {row['path']: row['sha256'] for row in selected or []
                                 if row.get('path') and row.get('sha256')}
            if selected is not None and include_delivery and self._delivery_scope(saved):
                selected_versions.update(saved['delivery_context']['artifacts'])
            sources = self.workspace.artifact_sources(selected_versions,
                records=saved.get('source_invocations', []), cache=journal_cache)
            if not self._observed_sources(saved):
                for path, digest in self.workspace.artifact_dependencies(sources).items():
                    bind(path, digest)
            # Applicable review binds what was actually observed. A recorded
            # producer association is not adoption of a current computation:
            # changed ancestral inputs can be the very defect being reviewed.
            # Historical inspections retain their original dependency gate.
        rows, supplied = [], set()

        def check_owner_receipts(row: dict) -> None:
            if row.get('tool') == 'inspect_images':
                # Old child journals sampled these paths after the visual
                # owner returned. They cannot bind the owner's original bytes.
                return
            for locator, expected in row.get('owner_receipts', {}).items():
                owner_path = Path(locator)
                actual = (hashlib.sha256(owner_path.read_bytes()).hexdigest() if owner_path.is_absolute()
                    else self._digest(locator))
                if actual != expected:
                    raise ValueError('Inspection operation owner receipt changed; no replay')

        for number, digest in enumerate(saved.get('source_rounds', [])):
            path = self._path(saved['identity'], f'sources-{number}')
            trace = self._bound_json(path, digest, validation_records,
                'Inspection source investigation record changed; no replay')
            if (saved.get('source_priming_policy') in _PRIORITY_SOURCE_POLICIES
                    and not self._offered_coverage(saved)):
                cached_views = validation_records.setdefault('supplied', {}) if validation_records is not None else {}
                if path not in cached_views:
                    windows = set()
                    separated = saved.get('context_authority') == 'sources.v1'
                    for index, message in enumerate(trace.get('offer', {}).get('messages', [])):
                        if (message.get('role') != ('assistant' if separated else 'user')
                                or separated and index != 1):
                            continue
                        try:
                            view = json.loads(message['content']).get('working_view', {})
                            for entry in view.get('observations', []):
                                item = entry['observation']
                                windows.add((item['path'], item['sha256'], item['offset_bytes'],
                                             item['next_offset_bytes']))
                        except (ValueError, TypeError, KeyError, AttributeError):
                            continue
                    cached_views[path] = windows
                supplied.update(cached_views[path])
            closed = [trace[key] for key in ('model_receipt', 'checkpoint_receipt') if key in trace]
            closed.extend(row['receipt'] for row in trace.get('results', []) if 'receipt' in row)
            if include_delivery:
                for receipt in closed:
                    self._receipt_result(receipt, validation_records=validation_records)
            for index, row in enumerate(trace.get('results', [])):
                rows.append(row)
                if include_delivery:
                    check_owner_receipts(row)
                elif selected is not None and row.get('tool') == 'read_file':
                    # A retained failed or incomplete opinion may contain a
                    # useful read, but its parent envelope alone cannot prove
                    # the particular child action. Check only selected reads;
                    # unrelated history must not become a dependency.
                    result = row.get('result') or {}
                    data = result.get('data') or {}
                    if result.get('status') != 'ok':
                        continue
                    matches = [item for item in selected
                        if item.get('subject') == data.get('subject')
                        and item.get('path') == data.get('path')
                        and item.get('sha256') == data.get('sha256')]
                    if not any(any(
                            window.get('offset_bytes') == part.get('offset_bytes')
                            and window.get('review_next_offset_bytes') == part.get('next_offset_bytes')
                            and window.get('size_bytes') == part.get('size_bytes')
                            and window.get('review_text', window.get('text')) == part.get('text')
                            for window in item.get('windows', [])
                            for part in data.get('text_windows', [])) for item in matches):
                        continue
                    receipt, relation = self._history_receipt(saved, number, index, trace)
                    if receipt is None or relation not in {'identical', 'inspection_source_omitted'}:
                        raise ValueError('Selected original read lacks its accepted owner receipt')
        if not current_inputs:
            if local_validation:
                self._verify_bound_json(validation_records)
            return dependencies
        owner_ids = {Path(source['record']).parent.name for source in sources.values()
                     if source.get('tool_name') == 'run_process' and source.get('record')}
        for row in rows:
            receipt = row.get('receipt', {})
            process_directory = self.workspace.root / 'tools' / 'processes' / receipt.get('attempt', '')
            chosen = (receipt.get('attempt') in owner_ids
                or any(Path(path).parent == process_directory for path in selected_paths))
            if row.get('tool') == 'run_process' and receipt and chosen:
                if not include_delivery:
                    check_owner_receipts(row)
                for locator, digest in self._check_bound_process_inputs(receipt, row['arguments']).items():
                    bind(locator, digest)
        source_rows = list(selected or [])
        if (include_delivery and saved.get('source_priming_policy') in _PRIORITY_SOURCE_POLICIES
                and not self._offered_coverage(saved)):
            source_rows.extend(row for row in [*saved['targets'], *saved['evidence']] if row not in source_rows)
        from simple_ar.tools.materials import material_window_reference
        for row in source_rows:
            for window in row.get('windows', []):
                reference = material_window_reference(window)
                exposed = ((row.get('path'), row.get('sha256'), window.get('offset_bytes'),
                    window.get('review_next_offset_bytes')) in supplied)
                if reference and (row in (selected or []) or exposed) and (
                        saved.get('source_priming_policy') in _PRIORITY_SOURCE_POLICIES or not include_delivery):
                    if self._observed_sources(saved):
                        from simple_ar.tools.materials import validate_material_observation
                        observed = validate_material_observation(self.workspace, reference,
                            identity='inspection-source')
                        bind(row['path'], observed['data']['sha256'])
                    else:
                        for locator, digest in self._reused_source_dependencies(
                                reference, row['path'], visited=visited, journal_cache=journal_cache,
                                validation_records=validation_records).items():
                            bind(locator, digest)
        if local_validation:
            self._verify_bound_json(validation_records)
        if self.workspace.artifact_hashes(dependencies) != dependencies:
            raise ValueError('Selected inspection dependencies changed')
        return dependencies

    def _reused_source_dependencies(self, reference: dict, path: str, *,
                                    visited: set[tuple[str, str]] | None = None,
                                    validated: dict | None = None,
                                    journal_cache: dict | None = None,
                                    validation_records: dict | None = None) -> dict[str, str]:
        """Check known upstream owners of one retained read, without adopting its opinion."""
        from simple_ar.tools.materials import validate_material_observation

        seen = visited if visited is not None else set()
        key = (json.dumps(reference, sort_keys=True, ensure_ascii=False), str(self.workspace.resolve(path)))
        if key in seen:
            raise ValueError('Retained inspection sources contain a dependency cycle')
        seen.add(key)
        try:
            observed = validated or validate_material_observation(self.workspace, reference, identity='inspection-source')
            if str(self.workspace.resolve(observed['request']['path'])) != key[1]:
                raise ValueError('Retained source names a different current file')
            version = observed['data'].get('sha256')
            sources = self.workspace.artifact_sources({path: version}, cache=journal_cache)
            dependencies = self.workspace.artifact_dependencies(sources)
            if self.workspace.artifact_hashes(dependencies) != dependencies:
                raise ValueError('Recorded producer inputs/outputs changed: retained source dependencies')

            def merge(other: dict[str, str]) -> dict[str, str]:
                for locator, digest in other.items():
                    if locator in dependencies and dependencies[locator] != digest:
                        raise ValueError('Retained sources have conflicting input versions')
                    dependencies[locator] = digest
                return dependencies

            source = reference.get('content_reuse', {}).get('source')
            if source:
                return merge(self._reused_source_dependencies(source, source['request']['path'],
                    visited=seen, journal_cache=journal_cache,
                    validation_records=validation_records))
            if 'record' not in reference:
                return dependencies
            receipt_path = self.workspace.resolve(reference['record'])
            receipt_relative = receipt_path.relative_to(self.store.root.resolve()).as_posix()
            receipt = self._bound_json(receipt_relative, reference['receipt_sha256'], validation_records,
                'Parent material receipt changed')
            previous = receipt
            if receipt.get('tool_name') != 'inspect_artifacts':
                previous = (receipt.get('metadata') or {}).get('delivery_review', {}).get('result', {})
            binding = (previous.get('metadata') or {}).get('inspection_binding')
            artifact = (previous.get('data') or {}).get('record')
            if previous.get('tool_name') != 'inspect_artifacts' or not isinstance(binding, dict) or not artifact:
                return dependencies  # The byte read is known; no upstream inspection contract was recorded.
            parts = Path(artifact).parts
            if len(parts) != 4 or parts[0] != 'attempts' or parts[2] != 'inspection':
                raise ValueError('Retained inspection source has an invalid owner')
            scope = parts[1]
            input_path = self._path(scope, 'input')
            prior = self._bound_json(input_path, binding.get('input_sha256'), validation_records,
                'Retained inspection input changed')
            if (prior.get('identity') != scope or prior.get('targets') != previous['data'].get('targets')
                    or prior.get('evidence') != previous['data'].get('evidence')):
                raise ValueError('Retained reads differ from their inspection input')
            if receipt.get('tool_name') == 'inspect_artifacts':
                from simple_ar.tools.professional_outcomes import accepted_native_result, accepted_scoped_opinion
                session_id = self.store.read_json('session_manifest.json')['session_id']
                attempt = ArtifactStore(receipt_path.parent).read_attempt_manifest()
                if attempt.capability == 'native_action':
                    accepted_scoped_opinion(self.root, self.workspace, session_id,
                        reference['record'], scope, expected=receipt)
                else:
                    accepted_native_result(self.root, session_id, receipt, receipt_path.parent.name)
            matches = [row for row in [*prior['targets'], *prior['evidence']]
                if row.get('path') == key[1] and row.get('sha256') == observed['data'].get('sha256')]
            if not matches:
                raise ValueError('Retained read is absent from its inspection input')
            return merge(self._check_shared_dependencies(prior, current_inputs=True, selected=matches,
                include_delivery=False, visited=seen, journal_cache=journal_cache,
                validation_records=validation_records))
        finally:
            seen.remove(key)

    def _shared_final_bindings(self, saved: dict, response: dict, *,
                               validation_records: dict | None = None,
                               execution_scopes: bool = True) -> dict[str, dict[str, str]]:
        observed = {row['subject']:row for row in [*saved['targets'], *saved['evidence']]}
        journal_cache: dict = {}
        validation_records = {} if validation_records is None else validation_records

        def check(selected: list[dict]) -> dict[str, str]:
            return self._check_shared_dependencies(saved, current_inputs=True, selected=selected,
                journal_cache=journal_cache, validation_records=validation_records)

        if self._delivery_scope(saved):
            selected = [observed[key] for key in self._delivery_evidence(saved, response)]
            bindings = {'delivery':check(selected)}
            reassessed = bool(response.get('reassessed_prior') or any(
                row.get('reassessed_prior') for row in response.get('investigations', [])))
            if saved.get('inspection_material_policy') == 'operations.v3':
                facts = self._execution_facts(saved)
                for investigation in response['investigations']:
                    for record in investigation.get('execution_records', []):
                        if record not in facts:
                            raise ValueError('Selected independent execution is no longer applicable')
                        if (execution_scopes and saved.get('inspection_evidence_policy') in {'presented.v1', *_PRESENTED_EVIDENCE_POLICIES}
                                and self._applicable_scope(saved) and reassessed):
                            bindings['execution:' + record] = facts[record]['dependencies']
                        for path, digest in facts[record]['dependencies'].items():
                            if path in bindings['delivery'] and bindings['delivery'][path] != digest:
                                raise ValueError('Selected independent executions have conflicting input versions')
                            bindings['delivery'][path] = digest
            if self._applicable_scope(saved) and reassessed:
                # The package inventory is an acceptance/version obligation,
                # never a provenance edge from every evidence item to every
                # deliverable. Reuse the same owner dependency checks without
                # adding that inventory to each evidence item's reach.
                parent_receipts = set()
                for entry in saved.get('source_history', {}).get('entries', []):
                    for reference in entry['records']:
                        try:
                            parent_receipts.add(str(self.workspace.resolve(reference['record'])))
                        except (KeyError, ValueError, OSError):
                            continue  # Unselected inaccessible history supplies no support.
                for row in selected:
                    path = str(self.workspace.resolve(row['path']))
                    # A receipt may contain several source windows. Reading a
                    # fragment of its JSON proves neither that a particular
                    # embedded window was supplied nor that it was inspected.
                    # The receipt's own bytes remain version-bound; they do not
                    # supply embedded source bytes or support that source's scope.
                    dependencies = ({} if path in parent_receipts else
                        self._check_shared_dependencies(saved, current_inputs=True,
                            selected=[row], include_delivery=False,
                            journal_cache=journal_cache,
                            validation_records=validation_records))
                    bindings['evidence:' + row['subject']] = self._evidence_with_dependencies(
                        {row['path']:row['sha256']}, dependencies)
        elif self.protocol:
            bindings = {decision['subject']:check(
                [observed[decision['subject']], *[observed[key] for key in decision['evidence']]])
                for decision in response['decisions']}
        else:
            bindings = {'group':check(self._selected_inspection_rows(saved, response))}
        self._verify_bound_json(validation_records)
        for dependencies in bindings.values():
            if self.workspace.artifact_hashes(dependencies) != dependencies:
                raise ValueError('Inspection dependencies changed during validation')
        return bindings

    def _stored_shared_bindings(self, saved: dict, response: dict) -> dict[str, dict[str, str]]:
        if saved.get('execution_protocol') not in _SHARED_EXECUTION_PROTOCOLS:
            return {}
        if not saved.get('source_rounds'):
            raise ValueError('Inspection has no retained final decision')
        trace = self.store.read_json(self._path(saved['identity'], f"sources-{len(saved['source_rounds'])-1}"))
        admission = trace.get('admission', {})
        if admission.get('final') != response or not isinstance(admission.get('dependencies'), dict):
            raise ValueError('Inspection final evidence bindings are unavailable')
        return admission['dependencies']

    @staticmethod
    def _evidence_with_dependencies(evidence: dict[str, str], dependencies: dict[str, str]) -> dict[str, str]:
        result = dict(evidence)
        for path, digest in dependencies.items():
            if path in result and result[path] != digest:
                raise ValueError('Inspection evidence has conflicting input versions')
            result[path] = digest
        return result

    def _recover_shared_journal(self, saved: dict) -> None:
        """Complete an interrupted journal-to-input digest update, without rerunning a step."""
        input_path = self._path(saved['identity'], 'input')
        for number, expected in enumerate(saved.get('source_rounds', [])):
            trace = self._path(saved['identity'], f'sources-{number}')
            actual = self._digest(trace)
            if actual == expected:
                continue
            recovery = self.store.read_json(trace).get('recovery', {})
            if (recovery.get('previous_trace_sha256') != expected
                    or recovery.get('previous_input_sha256') != self._digest(input_path)):
                raise ValueError('Inspection source investigation record changed; no replay')
            state = recovery['observations']
            saved['targets'], saved['evidence'] = state['targets'], state['evidence']
            if 'source_invocations' in state:
                saved['source_invocations'] = state['source_invocations']
            saved['source_rounds'][number] = actual
            self.store.write_json(input_path, saved)

    @staticmethod
    def delivery_scope_view(binding: dict) -> dict:
        """Model navigation, never the version-bound acceptance contract."""
        return {'projection': 'delivery_scope', 'requests': binding['requests'],
            'claim': {key: value for key, value in binding['claim'].items() if key != 'artifacts'},
            'artifact_count': len(binding['artifacts']),
            **({key: deepcopy(binding[key]) for key in ('purpose', 'unfinished') if key in binding})}

    @staticmethod
    def compact_feedback(result: dict, *, historical: bool = False) -> dict:
        """Keep actionable guidance; full prior opinions remain in their receipts."""
        source = (result.get('data') or {}).get('source_observations') or {}
        opinion = (result.get('metadata') or {}).get('delivery_opinion') or {}
        investigations = opinion.get('investigations')
        outcomes = opinion.get('requested_outcomes')
        if not source.get('record') or not opinion:
            return result
        projected = deepcopy(result)
        target = projected['metadata']['delivery_opinion']
        if isinstance(investigations, list):
            target['investigations'] = [
                {key: value for key, value in item.items() if key in
                    {'claim', 'outcome', 'scope', 'necessary', 'basis', 'reassessed_prior'}}
                if isinstance(item, dict) and item.get('outcome') == 'supported' else item
                for item in investigations if not (historical and isinstance(item, dict)
                    and item.get('outcome') == 'supported' and not item.get('reassessed_prior'))]
        if historical and isinstance(outcomes, list):
            target['requested_outcomes'] = [item for item in outcomes
                if not isinstance(item, dict) or item.get('status') != 'delivered']
        locator = projected['data']['source_observations']
        locator['scope'] = (locator.get('scope', '') +
            (' Earlier supported investigations and delivered outcomes remain in the original receipt.'
             if historical else ' Details of supported investigations remain in the original receipt.'))
        return projected

    @staticmethod
    def coordination_result(result: ToolResult, *, receipt_path: str, receipt_prefix: str | None = None) -> dict:
        """Return inspection decisions and source locators, not another copy of its reading session."""
        projected = result.model_dump(mode='json')
        if not receipt_path:
            return projected  # No recoverable origin: keep the complete observation.
        source = projected['data'].get('source_observations', {})
        # Navigation is relative to the selected receipt, not to the opinion's
        # protocol. A legacy producer embeds the result; an owner receipt does not.
        nested = 'metadata.delivery_review.result.'
        same_origin = source.get('record') == receipt_path
        if receipt_prefix is None:
            receipt_prefix = nested if same_origin and any(
                field.startswith(nested) for field in source.get('fields', [])) else ''
        fields = (list(source.get('fields', [])) if same_origin else
            [receipt_prefix + field.removeprefix(nested) for field in source.get('fields', [])
                if field != 'metadata.delivery_review.binding'])
        package = result.metadata.get('delivery_assessment')
        if 'delivery_opinion' in result.metadata:
            projected['data']['source_observations'].update(record=receipt_path, fields=fields)
            return projected
        if package is not None:
            # The full inventory, source digests and physical usage are checked
            # by their owners and retained once in the raw receipt. Repeating
            # those proofs in every model turn crowds out the actual evidence.
            return {**projected,
                'data': {'source_observations': {'record': receipt_path,
                    'fields': [receipt_prefix + field for field in
                        ('data.targets', 'data.evidence', 'metadata.delivery_assessment', 'metadata.usage')],
                    'scope': 'Original windows, exact version bindings and usage remain in this receipt. '
                        'This projected opinion is guidance, not source evidence or current acceptance.'},
                    **({'rejected_opinion': deepcopy(result.data['rejected_opinion'])}
                       if 'rejected_opinion' in result.data else {})},
                'metadata': {'delivery_opinion': {
                    'projection': 'delivery_opinion',
                    'coverage': package['coverage'], 'verdict': package['verdict'],
                    'findings': [{'severity': row['severity'], 'message': row['message'],
                        'artifacts': list(row['subjects'])} for row in package['findings']],
                    **({'reassessed_prior': list(package['reassessed_prior'])}
                       if 'reassessed_prior' in package else {}),
                    **({'investigations': deepcopy(package['investigations'])}
                       if 'investigations' in package else {}),
                    **({'requested_outcomes': deepcopy(package['requested_outcomes'])}
                       if 'requested_outcomes' in package else {}),
                    **({'applicability': package['applicability']}
                       if 'applicability' in package else {}),
                    'limitations': package['limitations'],
                    'artifact_count': len(package['artifacts']),
                    'evidence_count': len(package['evidence_sha256'])}}}
        data, metadata = projected['data'], projected['metadata']
        for role in ('targets','evidence'):
            if role in data:
                data[role] = [{key:value for key,value in row.items() if key!='windows'}
                    for row in data[role]]
                field = receipt_prefix + 'data.' + role
                if field not in fields:
                    fields.append(field)
        if 'usage' in metadata:
            usage = metadata.pop('usage')
            metadata['usage_status'] = {status: sum(row['status'] == status for row in usage)
                for status in dict.fromkeys(row['status'] for row in usage)}
            metadata['usage_diagnostics'] = [row for row in usage
                if row['status'] != 'settled' or row.get('reason')]
            field = receipt_prefix + 'metadata.usage'
            if field not in fields:
                fields.append(field)
        data['source_observations'] = {**source, 'record':receipt_path, 'fields':fields,
            'scope':'Original windows and accounting remain in this receipt. Assessments and version bindings '
                'are guidance, not source evidence, current acceptance or proof of settlement. '
                'Read the original receipt or authorized source windows when their contents are needed.'}
        return projected

    @classmethod
    def feedback_view(cls, feedback: dict) -> dict:
        """Supply exact findings and affected-version status, with full proof by reference.

        Also handles retained input without rewriting its historical bytes.
        Callers still verify the original owner result before supplying feedback.
        """
        source_record = feedback.get('source_record')
        source_prefix = feedback.get('source_prefix')
        feedback = {key: value for key, value in feedback.items()
                    if key not in {'source_record', 'source_prefix'}}
        binding = feedback.get('delivery_context') or {}
        source = feedback['result'].get('data', {}).get('source_observations', {})
        nested = 'metadata.delivery_review.result.'
        # Prefer the retained material owner's locator. The outer feedback record
        # may be a producer whose delivery review is nested (or still pending).
        record = source_record or source.get('record') or feedback.get('record', '')
        if not record:
            return feedback
        prefix = source_prefix if source_record else None
        if prefix is None and not source.get('record'):
            prefix = nested if binding.get('schema_version') in {
                'delivery_review.v1', 'delivery_review.v2', 'delivery_review.v3'} else ''
        result = cls.coordination_result(ToolResult.model_validate(feedback['result']),
            receipt_path=record, receipt_prefix=prefix)
        subjects = feedback.get('subjects', [])
        selected = {row['path'] for row in subjects if row.get('review_role') != 'related_subject'}
        if feedback.get('kind') == 'inspection_feedback' and 'subjects' in feedback:
            # This is a revision view, not the authoritative receipt. Keep a
            # cross-file finding when any selected subject is involved, and
            # leave the full original opinion accessible through its locator.
            metadata = result.get('metadata') or {}
            opinion = metadata.get('delivery_opinion')
            if isinstance(opinion, dict):
                opinion['findings'] = [row for row in opinion.get('findings', [])
                    if not row.get('artifacts') or selected.intersection(row['artifacts'])]
                outcomes = opinion.get('requested_outcomes')
                if isinstance(outcomes, list) and any('artifacts' in row for row in outcomes):
                    # This is recurring guidance, not the acceptance record.
                    # Keep what was judged and why; detailed outcome-to-file
                    # bindings remain in the original review receipt.
                    opinion['requested_outcomes'] = [
                        {**{key: value for key, value in row.items() if key != 'artifacts'},
                         **({'artifact_count': len(row['artifacts'])} if 'artifacts' in row else {})}
                        for row in outcomes]
                    source = result['data']['source_observations']
                    source['scope'] += ' Exact files supporting each requested outcome remain in this receipt.'
            values = metadata.get('assessments')
            if isinstance(values, list):
                metadata['assessments'] = [
                    {**row, 'findings': [finding for finding in row.get('findings', [])
                        if selected.intersection(finding.get('subjects', {}))]}
                    for row in values if selected.intersection(row.get('subjects', {}))]
            elif isinstance(metadata.get('assessment'), dict):
                assessment = metadata['assessment']
                if selected.intersection(assessment.get('subjects', {})):
                    metadata['assessment'] = {**assessment, 'findings': [
                        finding for finding in assessment.get('findings', [])
                        if selected.intersection(finding.get('subjects', {}))]}
        if binding.get('schema_version') != 'delivery_review.v3':
            return {**feedback, 'result': result}
        if prefix:
            source = result['data']['source_observations']
            source['fields'].append('metadata.delivery_review.binding')
        opinion = result['metadata'].get('delivery_opinion', {})
        affected = {path for row in opinion.get('findings', []) for path in row['artifacts']}
        return {**feedback, 'delivery_context': cls.delivery_scope_view(binding), 'result': result,
            'subjects': [row for row in subjects if row['path'] in affected],
            'reviewed_version_status': {'matching': sum(row['current_matches_review'] is True for row in subjects),
                'changed_or_unavailable': sum(row['current_matches_review'] is not True for row in subjects)},
            'scope': feedback.get('scope', '') + ' Full scope, versions and source observations remain '
                'in the recorded receipt. Only affected subject versions are repeated here; '
                'an empty artifact list on a finding means a whole-claim concern.'}

    def has_started(self, identity: str) -> bool:
        return self.store.exists(self._path(identity, 'input'))

    def _usage(self, identity: str) -> list[dict[str, Any]]:
        # Closed references identify evidence; admitted common headers also bind
        # failed/unknown charges whose dispatch never returned a closed wrapper.
        presentation = _PRESENTATION_RESULTS.get()
        key = (self, 'usage', identity)
        if presentation is not None and key in presentation:
            return deepcopy(presentation[key])
        path = self._path(identity,'input')
        saved = self.store.read_json(path) if self.store.exists(path) else {}
        invocations = saved.get('source_invocations', [])
        if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS:
            entries = list(self.workspace.ledger.entries)
            attempts = {row['attempt'] for row in invocations}
            candidates = {value for entry in entries for value in (entry.attempt_id, entry.logical_call_id)
                if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', value)}
            headers = presentation.setdefault((self, 'usage_headers'), {}) if presentation is not None else {}
            for candidate in candidates - attempts:
                if candidate not in headers:
                    header_path = f'attempts/{candidate}/action.json'
                    headers[candidate] = self.store.read_json(header_path) if self.store.exists(header_path) else None
                header = headers[candidate]
                if (header and header.get('attempt') == candidate
                        and header.get('origin', {}).get('scope') == identity):
                    attempts.add(candidate)
            usage = [entry.to_dict() for entry in entries
                if entry.attempt_id == identity or entry.attempt_id in attempts
                or entry.logical_call_id in attempts]
        else:
            # Historical owners retain their original invocation-list accounting.
            usage = [entry.to_dict() for entry in self.workspace.ledger.entries
                    if entry.attempt_id == identity or entry.logical_call_id in invocations]
        if presentation is not None:
            presentation[key] = deepcopy(usage)
        return usage

    def _digest(self, path: str) -> str:
        return hashlib.sha256(self.store.resolve(path).read_bytes()).hexdigest()

    def _bound_json(self, path: str, digest: str, cache: dict | None, error: str) -> Any:
        """Read a version-bound record once within a dependency validation."""
        records = cache.setdefault('records', {}) if cache is not None else {}
        if path in records:
            expected, value, _ = records[path]
            if expected != digest:
                raise ValueError(error)
            return value
        raw = self.store.resolve(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(error)
        value = json.loads(raw.decode('utf-8'))
        records[path] = (digest, value, error)
        return value

    def _verify_bound_json(self, cache: dict) -> None:
        for path, (digest, _, error) in cache.get('records', {}).items():
            if self._digest(path) != digest:
                raise ValueError(error)

    def _accounted(self, usage: list[dict]) -> bool:
        return (completed_llm_usage_is_accounted(usage) if self.progressive else
            not any(row['status'] in {'reserved', 'unknown'} for row in usage))

    @staticmethod
    def _response_digest(receipt: dict[str, Any]) -> str:
        payload = {key: value for key, value in receipt.items() if key != 'result_sha256'}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()

    def _receipt(self, identity: str) -> dict[str, Any]:
        saved = self.store.read_json(self._path(identity, 'response'))
        if saved.get('identity') != identity:
            raise ValueError('Inspection response identity mismatch; no replay')
        if saved.get('input_sha256') != self._digest(self._path(identity, 'input')):
            raise ValueError('Retained inspection input bytes changed; no replay')
        usage = self._usage(identity)
        if saved.get('completed') is not True:
            pending = (saved.get('usage') != usage or
                any(row['status'] in {'reserved', 'unknown'} for row in usage))
            state = 'provider usage is pending or unknown' if pending else 'recorded usage has no pending or unknown entries'
            raise ValueError('No completed inspection opinion: ' + saved.get('error', 'no final response')
                + '; ' + state + '. No replay')
        if (saved.get('usage') != usage or not any(row['status'] == 'settled'
                    and row.get('logical_call_id') == f'inspect-artifacts-{identity}'
                    and row.get('actual', {}).get('llm_requests', 0) > 0
                    and row.get('actual', {}).get('total_tokens', 0) > 0 for row in usage)
                or not self._accounted(usage)):
            raise ValueError('Inspection provider usage is pending or unknown; no replay')
        return saved

    def _cached(self, identity: str) -> ToolResult:
        return self._retained_result(identity, current_inputs=True)

    @contextmanager
    def presentation_scope(self):
        """Share validated opinions during one read-only view or acceptance check."""
        if _PRESENTATION_RESULTS.get() is not None:
            yield
            return
        token = _PRESENTATION_RESULTS.set({})
        try:
            yield
        finally:
            _PRESENTATION_RESULTS.reset(token)

    @staticmethod
    def clear_presentation_scope() -> None:
        """Revalidate after a checkpoint may have changed the working state."""
        results = _PRESENTATION_RESULTS.get()
        if results is not None:
            results.clear()

    def _retained_result(self, identity: str, *, current_inputs: bool) -> ToolResult:
        results = _PRESENTATION_RESULTS.get()
        if results is None:
            return self._read_retained_result(identity, current_inputs=current_inputs)
        key = (self, identity, current_inputs)
        if key not in results:
            results[key] = self._read_retained_result(identity, current_inputs=current_inputs)
        return results[key].model_copy(deep=True)

    def _recorded_opinion(self, identity: str) -> tuple[dict, dict, ToolResult, bool]:
        """Resolve the owner's recorded opinion, without re-investigating its evidence."""
        results = _PRESENTATION_RESULTS.get()
        cache_key = (self, identity, 'recorded_opinion')
        if results is not None and cache_key in results:
            return deepcopy(results[cache_key])
        receipt = self._receipt(identity)
        result_path = self._path(identity, 'result')
        if receipt.get('result_sha256') != self._digest(result_path):
            raise ValueError('Inspection result bytes changed or were never completed; no replay')
        saved = self.store.read_json(self._path(identity, 'input'))
        if saved.get('protocol') != self.protocol:
            raise ValueError('Inspection identity is bound to a different protocol; no replay')
        result = ToolResult.model_validate(self.store.read_json(result_path))
        binding = result.metadata['inspection_binding']
        package = self._delivery_scope(saved)
        key = 'delivery_assessment' if package else 'assessments' if self.protocol else 'assessment'
        artifact = f'attempts/{identity}/inspection/{key}.json'
        if (saved['identity'] != identity or result.tool_name != 'inspect_artifacts'
                or binding['input_sha256'] != receipt['input_sha256']
                or binding['response_sha256'] != self._response_digest(receipt)
                or binding['assessment_sha256'] != self._digest(artifact)
                or self.store.read_json(artifact) != result.metadata[key]
                or result.data['targets'] != saved['targets']
                or result.data['evidence'] != saved['evidence']):
            raise ValueError('Inspection cached result does not match its inputs/response/artifact; no replay')
        if self.protocol and (binding.get('assessment_path') != artifact
                or result.data.get('record') != artifact or result.artifacts != [artifact]):
            raise ValueError('Inspection cached assessment path changed; no replay')
        opinion = saved, receipt, result, package
        if results is not None:
            results[cache_key] = deepcopy(opinion)
        return opinion

    def _read_retained_result(self, identity: str, *, current_inputs: bool) -> ToolResult:
        """Verify saved investigation evidence before applying an opinion."""
        saved, receipt, result, package = self._recorded_opinion(identity)
        for number, digest in enumerate(saved.get('source_rounds', [])):
            trace_path = self._path(identity, f'sources-{number}')
            if self._digest(trace_path) != digest:
                raise ValueError('Inspection source investigation record changed; no replay')
            for path, expected in self.store.read_json(trace_path).get('checkpoint', {}).items():
                if self._digest(path) != expected:
                    raise ValueError('Inspection checkpoint record changed; no replay')
        if saved.get('memory_protocol') == 'journal.v1':
            self._conversation_memory(saved, self._conversation_rows(saved))
        # Successful shared assessments revalidate the same selected sources
        # when their authoritative final bindings are reconstructed below.
        # Other outcomes still need the original journal dependency check.
        assessed = current_inputs and self.protocol and result.status == 'ok'
        if not (assessed and saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS):
            self._check_shared_dependencies(saved, current_inputs=current_inputs,
                selected=self._selected_inspection_rows(saved, receipt['response']) if current_inputs else None)
        if self.protocol and result.status == 'ok':
            assess = self._delivery_assessment if package else self._subject_assessments
            key = 'delivery_assessment' if package else 'assessments'
            expected, error = assess(saved, receipt, identity, '', check_versions=current_inputs)
            if error or result.metadata[key] != expected:
                raise ValueError('Inspection cached subject decisions differ from the retained response; no replay')
        elif current_inputs:
            self._check_inspection_versions(saved, identity,
                selected=self._selected_inspection_rows(saved, receipt['response']))
        return result

    def feedback_observation(self, identity: str, claimed: dict, *, expected_arguments: dict | None = None,
                             guidance_only: bool = False) -> dict:
        """Original, integrity-checked revision guidance; never an acceptance pass."""
        saved = {}
        try:
            saved = self.store.read_json(self._path(identity, 'input'))
            if expected_arguments is not None and saved.get('arguments') != expected_arguments:
                raise ValueError('Inspection feedback differs from the accepted call')
            if ('delivery_context' in saved
                    and saved.get('user_requests') != saved['delivery_context'].get('requests')):
                raise ValueError('Delivery inspection feedback differs from its original user requests')
            result = (self._recorded_opinion(identity)[2] if guidance_only
                      else self._retained_result(identity, current_inputs=False))
            if result.model_dump(mode='json') != claimed:
                raise ValueError('Inspection feedback differs from its retained owner result')
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            # An unfinished, unknown-use or invalid opinion cannot certify a
            # result, but neither is it a prerequisite for subsequent work.
            # Keep the diagnosis, not its unverified findings or a false pass.
            return {'arguments': saved.get('arguments', {}), 'opinion_available': False,
                **({'delivery_context': saved['delivery_context']} if 'delivery_context' in saved else {}),
                'reviewed_subjects': {row['path']: row['sha256'] for row in saved.get('targets', [])
                    if row.get('path') and row.get('sha256')},
                'result': ToolResult(tool_name='inspect_artifacts', status='blocked',
                    error='Retained independent opinion unavailable: '+str(exc)).model_dump(mode='json'),
                'scope': 'Diagnostic only; original failed records and usage remain retained. '
                    'No findings or current acceptance are supplied.'}
        return {'arguments': saved['arguments'],
            'opinion_available': True,
            **({'delivery_context': saved['delivery_context']} if 'delivery_context' in saved else {}),
            'result': self.coordination_result(result, receipt_path=self._path(identity, 'result')),
            'reviewed_subjects': {row['path']: row['sha256'] for row in saved['targets']
                if row.get('sha256')},
            'scope': 'Retained independent opinion about the bound original versions; '
                'not current acceptance, a source fact, or permission.'
                + (' Historical investigation evidence is not rechecked for this guidance.' if guidance_only else '')}

    def feedback_from_observation(self, observed: dict, *, guidance_only: bool = False) -> dict | None:
        """Resolve a native inspection once for revision and delivery consumers."""
        if observed.get('tool_name') != 'inspect_artifacts':
            return None
        binding = (observed.get('metadata') or {}).get('inspection_binding')
        if not binding:
            return None
        path = binding.get('assessment_path')
        parts = Path(path).parts if isinstance(path, str) else ()
        record = observed.get('record')
        if len(parts) == 4 and parts[0] == 'attempts':
            identity = parts[1]
        elif isinstance(record, str) and record:
            identity = Path(record).parent.name
        else:
            raise ValueError('Inspection feedback has no original owner record')
        arguments = observed.get('arguments')
        if not isinstance(arguments, dict):
            raise ValueError('Inspection feedback has no accepted arguments')
        return self.feedback_observation(identity, tool_result_payload(observed), expected_arguments=arguments,
                                         **({'guidance_only': True} if guidance_only else {}))

    def can_reconcile(self, name: str, identity: str) -> bool:
        if name != 'inspect_artifacts' or not self.has_started(identity):
            return False
        try:
            if not self.store.exists(self._path(identity, 'result')):
                return False
            self._cached(identity)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _observe(self, value: FileWindow | str, subject: str, identity: str,
                 remaining: list[int], *, read_limit: int | None = None,
                 operation_call: Callable | None = None) -> dict[str, Any]:
        full = isinstance(value, str)
        window = FileWindow(path=value) if full else value
        if self.progressive:
            window = window.model_copy(update={'max_bytes': 512 if full else
                min(window.max_bytes, read_limit or window.max_bytes)})
            full = False
        row: dict[str, Any] = {'subject': subject, 'request': window.model_dump(exclude_unset=True),
                               'complete': False, 'windows': []}
        try:
            row['path'] = str(self.workspace.resolve(window.path).resolve())
            arguments = window.model_dump(exclude_unset=True)
            arguments.update(full_digest=True,
                             max_bytes=max(1, min(64000 if full else window.max_bytes, remaining[0])))
            consumed = 0
            while True:
                result = (operation_call('read_file', arguments, identity) if operation_call is not None
                    else self.workspace.call('read_file', arguments, identity=identity))
                if not result.ok:
                    raise ValueError(result.error or 'Unreadable file')
                data = result.data
                if data['absolute_path'] != row['path'] or not data.get('sha256'):
                    raise ValueError('Read did not bind the actual file and full digest')
                if row.get('sha256', data['sha256']) != data['sha256']:
                    raise ValueError('File version changed between inspection windows')
                row['sha256'] = data['sha256']
                if remaining[0] <= 0:
                    raise ValueError('Inspection total text limit reached; digest only, no text reviewed')
                row['windows'].append({**data,
                    **({'read_request': dict(arguments)} if self.adaptive else {})})
                start = data.get('offset_bytes', arguments.get('offset_bytes', 0))
                row['windows'][-1]['offset_bytes'] = start
                count = data['next_offset_bytes'] - start
                remaining[0] -= count
                consumed += count
                text = data['text']
                next_offset = data['next_offset_bytes']
                # read_file decodes each byte window separately. Re-read a cut
                # UTF-8 suffix rather than mistaking it for binary content.
                if (full or self.progressive) and data['has_more'] and text.endswith('\ufffd') and '\ufffd' not in text[:-1]:
                    prefix = text[:-1]
                    trailing = count - len(prefix.encode('utf-8'))
                    if 0 < trailing <= 3:
                        text, next_offset = prefix, next_offset - trailing
                        consumed -= trailing
                row['windows'][-1]['review_text'] = text
                row['windows'][-1]['review_next_offset_bytes'] = next_offset
                if (Path(row['path']).suffix.lower() in _BINARY or not lossless_text_window({**data, 'text':text})
                        or any(ord(char) < 32 and char not in '\n\r\t\f' for char in text)):
                    raise ValueError('Binary or undecodable content: text inspection unavailable')
                if not data['has_more']:
                    row['complete'] = (row['windows'][0].get('offset_bytes', window.offset_bytes) == 0
                        and (window.start_line in (None, 1)) and consumed == data['size_bytes'])
                    break
                if not full or consumed >= 256000 or remaining[0] <= 0:
                    break
                if count <= 0:
                    raise ValueError('Read window made no progress')
                arguments.update(offset_bytes=next_offset,
                                 max_bytes=min(64000, 256000 - consumed, remaining[0]))
            if not row['complete']:
                row['limitation'] = 'Only the recorded text window(s) were read'
        except (OSError, ValueError, KeyError) as exc:
            row.update(complete=False, limitation=str(exc))
            # A binary window is retained for audit, never submitted as text evidence.
            row['readable'] = False
        else:
            row['readable'] = True
        return row

    def _reuse_observations(self, saved: dict, remaining: list[int]) -> None:
        """Supply prior raw reads through the common byte owner, never prior verdicts."""
        applicable = self._applicable_scope(saved)
        if applicable:
            # The parent index is navigation, never an adopted observation.
            # Current deliverable windows may be reused below only through the
            # same receipt/dependency/version checks as other original reads.
            paths = {}
            references = (self.material_observations([str(root) for root in self.workspace.mounts.values()])
                          if self.material_observations is not None else [])
            for reference in references:
                try:
                    request = FileWindow.model_validate(reference['request'])
                    path = str(self.workspace.resolve(request.path))
                    record = reference.get('record')
                    if record is not None:
                        self.workspace.resolve(record)  # Current authority, not a new mount.
                    entry = paths.setdefault(path, {'path': path, 'requested_byte_ranges': [],
                        'line_windows': [], 'records': []})
                    if request.start_line is None:
                        entry['requested_byte_ranges'].append((request.offset_bytes,
                            request.offset_bytes + request.max_bytes))
                    else:
                        window = request.model_dump(exclude_unset=True)
                        window.pop('path')
                        if window not in entry['line_windows']:
                            entry['line_windows'].append(window)
                    locator = {key: reference[key] for key in
                        ('record', 'receipt_sha256', 'input_index') if key in reference}
                    if record is not None:
                        locator['request'] = request.model_dump(exclude_unset=True)
                        if locator not in entry['records']:
                            entry['records'].append(locator)
                except (ValueError, OSError, KeyError, TypeError):
                    continue  # Inaccessible history grants neither navigation nor evidence.
            for entry in paths.values():
                entry['requested_byte_ranges'] = self._merged_ranges(entry['requested_byte_ranges'])
            saved['source_history'] = {'entries': list(paths.values()),
                'scope': 'Parent read locators only, not evidence or permission. Ranges are requested '
                    'windows, not proof of observed or supplied bytes. Use read_file on path and a '
                    'requested window for current bytes, or on a record to page '
                    'the original receipt (input_index selects its retained input when present). '
                    'Descriptors are grouped by authorized source path and selected using the existing '
                    'working-context input capacity, never a fixed reference count. Omitted descriptors '
                    'remain discoverable with list_directory/search_files in authorized source and '
                    'records roots. read_history addresses only this '
                    'inspection, not the parent task. Historical bytes never prove current claims '
                    'by themselves; reading a parent receipt supplies that record, not its '
                    'embedded original sources or a renewed computation.'}
        current = saved.get('source_priming_policy') in _PRIORITY_SOURCE_POLICIES
        if applicable and not current:
            return  # Historical/navigation-only inputs retain their supply contract.
        from simple_ar.tools.materials import material_observation_seed

        subjects = [row for row in ([*saved['targets'], *saved['evidence']] if current else saved['evidence'])
            if row.get('readable') and row.get('sha256')]
        paths = list(dict.fromkeys(row['path'] for row in saved['evidence']
            if row.get('readable') and row.get('sha256')))
        if remaining[0] <= 0:
            return
        targets = {row['path'] for row in saved['targets'] if row.get('path')}
        journal_cache: dict[str, Any] = {}

        def supply(seed: dict | None) -> None:
            for observed in (seed or {}).get('observations', []):
                if applicable and ('content_reuse' in observed or
                        'record' not in observed and not (self._observed_sources(saved)
                            and isinstance(observed.get('source_reference'), dict))):
                    # An inspected source needs its own original read receipt.
                    # A creation-frozen read has an explicit retained source;
                    # equal bytes at another path do not transfer provenance.
                    continue
                data, request = observed['data'], observed['request']
                path = data.get('absolute_path') or data.get('path') or request['path']
                if current and not self._observed_sources(saved):
                    try:
                        self._reused_source_dependencies(observed, path, validated=observed,
                            journal_cache=journal_cache)
                    except (ValueError, TypeError, KeyError, OSError, RuntimeError):
                        continue  # Unproved history remains in its original receipt.
                text = data['text']
                if (not text.strip() or not lossless_text_window(data) or Path(path).suffix.lower() in _BINARY
                        or any(ord(char) < 32 and char not in '\n\r\t\f' for char in text)):
                    continue
                start = data.get('offset_bytes', request.get('offset_bytes', 0))
                end = data['next_offset_bytes']
                owners = [row for row in subjects if row['path'] == path and row['sha256'] == data['sha256']]
                if end-start > remaining[0]:
                    continue
                if not owners:
                    number = max((int(row['subject'].split('-')[1])
                                  for row in saved['evidence']), default=0) + 1
                    row = {'subject': f"evidence-{number}",
                        'request': request, 'path': path, 'sha256': data['sha256'],
                        'readable': True, 'complete': False, 'windows': []}
                    saved['evidence'].append(row)
                    subjects.append(row)
                    owners = [row]
                admitted = False
                for row in owners:
                    if any(w['offset_bytes'] <= start and end <= w['review_next_offset_bytes']
                           for w in row['windows']):
                        continue
                    row['windows'].append({**data, 'offset_bytes': start, 'review_text': text,
                        'review_next_offset_bytes': end, 'read_request': request,
                        'source_reference': {key: value for key, value in observed.items() if key != 'data'}})
                    admitted = True
                if admitted:
                    remaining[0] -= end-start
            for row in subjects:
                row['complete'] = self._complete_target(row, subjects)
                if row['complete']:
                    row.pop('limitation', None)

        if not current:
            inventories = ([{'candidate_files': {row['path']: row['windows'][0]['size_bytes']
                            for row in subjects}}] if paths else None)
            supply(material_observation_seed(workspace=self.workspace, inventories=inventories,
                provider=self.material_observations, roots=paths or [str(root) for root in self.workspace.mounts.values()],
                excluded_paths=targets, context={}, remaining_capacity=min(96000, remaining[0])))
            return

        # Reuse originals selected by this inspection, not everything the parent
        # happened to read. Available capacity is not evidence relevance. Other
        # sources remain discoverable through source_history and workspace reads.
        default_window = FileWindow.model_fields['max_bytes'].default
        primary = set(targets) | set(paths)
        if self._delivery_scope(saved):
            primary.update(str(self.workspace.resolve(path)) for path in saved['delivery_context']['artifacts'])
        candidates = {}
        for locator in primary:
            try:
                path = self.workspace.resolve(locator)
                if path.is_file() and path.suffix.lower() not in _BINARY:
                    candidates[str(path)] = path.stat().st_size
            except (ValueError, OSError):
                continue
        if candidates and remaining[0] > default_window:
            supply(material_observation_seed(workspace=self.workspace,
                inventories=[{'candidate_files': candidates}], provider=self.material_observations,
                roots=list(candidates), excluded_paths=set(), context={},
                remaining_bytes=remaining[0] - default_window))

    def _complete_target(self, target: dict, rows: list[dict]) -> bool:
        """Actual decoded ranges, not a model declaration or a matching copy."""
        if not self.progressive:
            return bool(target['complete'] and target.get('readable'))
        windows = [window for row in rows if row.get('readable') and row.get('path') == target.get('path')
            and row.get('sha256') == target.get('sha256') for window in row['windows']]
        if not windows:
            return False
        sizes = {window['size_bytes'] for window in windows}
        if len(sizes) != 1:
            return False
        cursor = 0
        for start, end in sorted((w['offset_bytes'], w['review_next_offset_bytes']) for w in windows):
            if start > cursor:
                return False
            cursor = max(cursor, end)
        return cursor == next(iter(sizes))

    @staticmethod
    def _merged_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
        merged: list[tuple[int, int]] = []
        for start, end in sorted(set(ranges)):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    @staticmethod
    def _range_supplied(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
        # An empty file needs an actual supplied empty observation, not merely
        # the vacuous mathematical coverage of an empty interval.
        if start == end:
            return any(left <= start <= right for left, right in ranges)
        cursor = start
        for left, right in ranges:
            if right <= cursor:
                continue
            if left > cursor:
                return False
            cursor = max(cursor, right)
            if cursor >= end:
                return True
        return False

    def _offered_text_ranges(self, saved: dict, *, validation_records: dict | None = None
                             ) -> dict[tuple[str, str], list[tuple[int, int]]]:
        """Recover actual supplied source ranges from the existing request projections."""
        if not self._offered_coverage(saved):
            return {}
        if saved.get('execution_protocol') not in _SHARED_EXECUTION_PROTOCOLS:
            raise ValueError('Offered coverage requires the shared inspection journal')
        cache = validation_records if validation_records is not None else {}
        cache_key = (saved['identity'], tuple(saved.get('source_rounds', [])))
        cached = cache.setdefault('offered_text_ranges', {})
        if cache_key in cached:
            return cached[cache_key]

        original = {}
        for row in [*saved['targets'], *saved['evidence']]:
            if not row.get('readable') or not row.get('path') or not row.get('sha256'):
                continue
            for window in row.get('windows', []):
                start, end, size = (window.get('offset_bytes'),
                    window.get('review_next_offset_bytes'), window.get('size_bytes'))
                text = window.get('review_text', window.get('text'))
                if (any(type(value) is not int for value in (start, end, size))
                        or not 0 <= start <= end <= size or not isinstance(text, str)
                        or len(text.encode('utf-8')) != end-start):
                    continue
                item = (row['subject'], row['path'], row['sha256'], start, end, size)
                if item in original and original[item] != text:
                    raise ValueError('Inspection windows conflict at the same source identity')
                original[item] = text

        rounds = [self._closed_source_round(saved, number, allow_final=True, model_only=True,
                    validation_records=cache) for number in range(len(saved.get('source_rounds', [])))]
        actions: dict[tuple[int, int], dict | None] = {}

        def action(turn: int, index: int) -> dict | None:
            key = (turn, index)
            if key not in actions:
                trace = rounds[turn] if 0 <= turn < len(rounds) else None
                rows = trace.get('results', []) if trace else []
                calls = trace.get('response', {}).get('tool_calls') if trace else []
                if (not isinstance(calls, list) or not 0 <= index < min(len(rows), len(calls))
                        or not isinstance(calls[index], dict)
                        or not isinstance(calls[index].get('function'), dict)):
                    actions[key] = None
                else:
                    row, call = rows[index], calls[index]
                    if row.get('tool') not in {'read_file', 'read_history'}:
                        actions[key] = None
                    else:
                        receipt, relation = self._history_receipt(saved, turn, index, trace)
                        actions[key] = (row if receipt is not None
                            and relation in {'identical', 'inspection_source_omitted'}
                            and row['call_id'] == call.get('id')
                            and row['tool'] == call['function'].get('name') else None)
            return actions[key]

        supplied: dict[tuple[str, str], list[tuple[int, int]]] = {}

        def add(source: dict, *, text_slice: dict | None = None) -> None:
            item = (source.get('subject'), source.get('path'), source.get('sha256'),
                source.get('offset_bytes'), source.get('next_offset_bytes'), source.get('size_bytes'))
            text = original.get(item)
            if text is None:
                return
            start, end = item[3], item[4]
            if text_slice is not None:
                first, last, total = (text_slice.get('character_start'), text_slice.get('character_end'),
                    text_slice.get('total_characters'))
                if (any(type(value) is not int for value in (first, last, total))
                        or total != len(text) or not 0 <= first <= last <= total):
                    return
                start = item[3] + len(text[:first].encode('utf-8'))
                end = item[3] + len(text[:last].encode('utf-8'))
            supplied.setdefault((item[1], item[2]), []).append((start, end))

        identity = saved['identity']
        input_record = self._path(identity, 'input')
        prefix = re.escape(input_record.removesuffix('input.json'))
        history_pages: dict[tuple[int, int], list[tuple[int, int]]] = {}
        history_values: dict[tuple[int, int], dict] = {}
        history_actions: dict[str, list[tuple[int, int]]] = {}
        history_payloads: dict[tuple[int, int], tuple[bytes, str]] = {}
        seen_pages: set[tuple[int, int]] = set()
        for turn, trace in enumerate(rounds):
            for index, row in enumerate(trace.get('results', []) if trace else []):
                if row.get('tool') == 'read_history':
                    history_actions.setdefault(row.get('call_id'), []).append((turn, index))
        for number, trace in enumerate(rounds):
            if trace is None:
                continue
            offer = trace.get('offer') or {}
            # The common projector owns this selection. Unlike a saved read,
            # a selected source is part of this settled model request.
            for state in offer.get('projection', {}).get('selected_records', []):
                source = state.get('source', {})
                if (state.get('status') not in {'native', 'working_view'}
                        or state.get('version_status') != 'matching'
                        or source.get('kind') != 'source_text'):
                    continue
                record = source.get('record')
                if record != input_record:
                    match = re.fullmatch(prefix + r'sources-(\d+)\.json#results/(\d+)', str(record))
                    if not match:
                        continue
                    turn, index = map(int, match.groups())
                    row = action(turn, index) if turn < number else None
                    data = (row or {}).get('result', {}).get('data') or {}
                    if ((row or {}).get('tool') != 'read_file'
                            or any(data.get(field) != source.get(field) for field in ('subject', 'path', 'sha256'))
                            or not any(all(window.get(field) == source.get(field) for field in
                                ('offset_bytes', 'next_offset_bytes', 'size_bytes'))
                                for window in data.get('text_windows', []))):
                        continue
                add(source, text_slice=source.get('record_text_slice')
                    if state['status'] == 'working_view' else None)

            # History pages address canonical JSON, not source-file offsets.
            # Only a fully supplied, owner-verified read result can restore its
            # original source windows.
            for message in offer.get('messages', []):
                if not isinstance(message, dict) or message.get('role') != 'tool':
                    continue
                try:
                    visible = json.loads(message['content'])
                except (ValueError, TypeError, KeyError):
                    continue
                if (not isinstance(visible, dict) or visible.get('tool_name') != 'read_history'
                        or visible.get('status') != 'ok'):
                    continue
                matches = [(turn, index) for turn, index in history_actions.get(message.get('tool_call_id'), [])
                    if turn < number and rounds[turn]['results'][index].get('result') == visible]
                if len(matches) != 1 or action(*matches[0]) is None:
                    continue
                if matches[0] in seen_pages:
                    continue
                seen_pages.add(matches[0])
                page = visible.get('data') or {}
                turn, index = page.get('start_turn'), page.get('action')
                if (page.get('projection') != 'retained_value' or page.get('field') != 'result'
                        or type(turn) is not int or type(index) is not int or not 0 <= turn < number):
                    continue
                row = action(turn, index)
                if not row or row.get('tool') != 'read_file' or row.get('result', {}).get('status') != 'ok':
                    continue
                value = row['result']
                if (turn, index) not in history_payloads:
                    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                        separators=(',', ':')).encode('utf-8')
                    history_payloads[turn, index] = (raw, hashlib.sha256(raw).hexdigest())
                raw, digest = history_payloads[turn, index]
                start, end, size = page.get('offset_bytes'), page.get('next_offset_bytes'), page.get('size_bytes')
                if (any(type(value) is not int for value in (start, end, size))
                        or not 0 <= start <= end <= size or size != len(raw)
                        or page.get('record') != self._path(identity, f'sources-{turn}')
                        or page.get('sha256') != saved['source_rounds'][turn]
                        or page.get('pointer') != f'/results/{index}/result'
                        or page.get('projection_sha256') != digest
                        or not isinstance(page.get('text'), str)
                        or page['text'].encode('utf-8') != raw[start:end]):
                    continue
                history_pages.setdefault((turn, index), []).append((start, end))
                history_values[turn, index] = value

        for history_key, ranges in history_pages.items():
            value = history_values[history_key]
            raw, _ = history_payloads[history_key]
            if self._range_supplied(self._merged_ranges(ranges), 0, len(raw)):
                data = value.get('data') or {}
                for window in data.get('text_windows', []):
                    add({**window, **{field:data.get(field) for field in ('subject', 'path', 'sha256')}})
        result = {source:self._merged_ranges(ranges) for source, ranges in supplied.items()}
        cached[cache_key] = result
        return result
    def _offered_row_scope(self, row: dict, offered: dict) -> tuple[bool, str]:
        """Compare a recorded observation with supplied bytes, not a verdict."""
        recorded = self._merged_ranges([(window['offset_bytes'], window['review_next_offset_bytes'])
            for window in row.get('windows', [])])
        supplied = offered.get((row.get('path'), row.get('sha256')), [])
        complete = bool(row.get('readable') and recorded) and all(
            self._range_supplied(supplied, start, end) for start, end in recorded)
        def describe(ranges: list[tuple[int, int]]) -> str:
            return json.dumps(ranges[:32]) + (f' (+{len(ranges)-32} further ranges in original records)'
                if len(ranges) > 32 else '')
        return complete, (f"{row['subject']}: recorded read byte ranges {describe(recorded)}; "
            f"byte ranges supplied to settled inspection requests {describe(supplied)}. "
            'Ranges describe observation, not correctness; incomplete history projections remain unknown.')

    def _offered_target_complete(self, target: dict, rows: list[dict], offered: dict) -> bool:
        windows = [window for row in rows if row.get('readable') and row.get('path') == target.get('path')
            and row.get('sha256') == target.get('sha256') for window in row.get('windows', [])]
        sizes = {window['size_bytes'] for window in windows}
        return bool(target.get('readable') and len(sizes) == 1 and self._range_supplied(
            offered.get((target.get('path'), target.get('sha256')), []), 0, next(iter(sizes))))

    @staticmethod
    def _requested_window(saved: dict, target: dict, index: int) -> dict | None:
        """Derive an explicit window from the saved request and original read receipt.

        The model's opinion and later source reads cannot expand this scope. A
        line-limited request needs an uncropped initial read to establish its
        end; an ordinary byte request has an end fixed by the file size.
        """
        if saved.get('coverage_policy') != 'offered.v2':
            return None
        values = saved.get('arguments', {}).get('targets', [])
        if index >= len(values) or not isinstance(values[index], dict):
            return None
        windows = target.get('windows', [])
        if not target.get('readable') or not windows:
            return None
        request = FileWindow.model_validate(values[index])
        first = windows[0]
        actual = first.get('read_request') or {}
        size = first.get('size_bytes')
        start = first.get('offset_bytes')
        if type(size) is not int or type(start) is not int or not 0 <= start <= size:
            return None
        if request.start_line is not None and size and start == size:
            return None  # A line beyond EOF is not an inspected text window.
        if request.start_line is None and 'max_lines' not in request.model_fields_set:
            if start != request.offset_bytes:
                return None
            end = min(size, start + request.max_bytes)
        else:
            if (actual.get('max_bytes', 0) < request.max_bytes and first.get('has_more')):
                return None
            end = first.get('next_offset_bytes')
            if end != first.get('review_next_offset_bytes'):
                return None  # The read ended inside a UTF-8 code point.
        if type(end) is not int or not start <= end <= size:
            return None
        return {'start': start, 'end': end, 'size_bytes': size}

    def _check_versions(self, rows: list[dict[str, Any]], identity: str) -> None:
        for row in rows:
            if not row.get('sha256'):
                continue
            result = self.workspace.call('read_file', {'path': row['path'], 'max_bytes': 1,
                                                       'full_digest': True}, identity=identity)
            if (not result.ok or result.data.get('absolute_path') != row['path']
                    or result.data.get('sha256') != row['sha256']):
                raise ValueError('Inspection target/evidence version changed; use a new identity')

    @staticmethod
    def _admissible_references(saved: dict[str, Any]) -> dict[str, list[str]]:
        """Eligible observation subjects; relevance and certification remain review judgments."""
        observed = {row['subject']: row for row in [*saved['targets'], *saved['evidence']]}
        readable = {subject: row for subject, row in observed.items()
            if row.get('sha256') and row.get('readable')
            and any(window.get('review_text', window['text']).strip() for window in row['windows'])}
        return {target['subject']: [subject for subject, row in readable.items()
            if subject != target['subject'] and row['path'] != target['path']
            and row['sha256'] != target.get('sha256')]
            for target in saved['targets']}

    def _subject_decisions(self, saved: dict[str, Any], value: dict) -> list[_SubjectDecision]:
        model = (_ReviewResponseV2 if saved.get('coverage_policy') == 'offered.v2' else _ReviewResponse) if self.adaptive else _SubjectResponse
        decisions = model.model_validate(value).decisions
        targets = {row['subject']: row for row in saved['targets']}
        ids = [decision.subject for decision in decisions]
        if len(ids) != len(set(ids)) or set(ids) != set(targets):
            raise ValueError('Inspection requires exactly one decision per target')
        admissible = self._admissible_references(saved)
        invalid_references = []
        for decision in decisions:
            target = targets[decision.subject]
            if not target.get('sha256'):
                raise ValueError('Inspection decision target has no bound version')
            if (any(not finding.message.strip() for finding in decision.findings)
                    or any(not text.strip() for text in decision.limitations)):
                raise ValueError('Inspection findings and limitations must be nonblank')
            if self.adaptive and decision.conclusion == 'incomplete' and not decision.limitations:
                raise ValueError('Incomplete inspection must explain its remaining scope')
            seen, invalid, repeated = set(), [], []
            for source in decision.evidence:
                if source in seen:
                    repeated.append(source)
                elif source not in admissible[decision.subject]:
                    invalid.append(source)
                seen.add(source)
            if invalid or repeated:
                def bounded(ids: list[str]) -> dict:
                    return {'ids': [key[:120] for key in ids[:16]],
                        'omitted': max(0, len(ids)-16)}
                invalid_references.append({'subject': decision.subject,
                    'invalid': bounded(invalid), 'repeated': bounded(repeated),
                    'eligible': bounded(admissible[decision.subject])})
        if invalid_references:
            raise ValueError('Inspection evidence IDs must be unique and observed as readable independent text '
                'for each subject; use a bare observation subject ID, not a chunk_id, path or citation. '
                'Eligibility does not establish relevance or scientific certification. '
                + json.dumps({'subjects': invalid_references[:8],
                    'omitted_subjects': max(0, len(invalid_references)-8)}, ensure_ascii=False))
        return decisions

    def _investigation_contract(self, saved: dict, *, offering: bool = False) -> bool:
        if not self._applicable_scope(saved):
            return False
        for number, digest in enumerate(saved.get('source_rounds', [])):
            # The original offer is a version-bound owner record. Do not change
            # the response schema while resuming an already supplied contract.
            path = self._path(saved['identity'], f'sources-{number}')
            record = self._bound_json(path, digest, None, 'Inspection offer changed')
            for tool in record.get('offer', {}).get('tools', []):
                function = tool.get('function', {})
                if function.get('name') == 'record_inspection':
                    return 'applicability' in function['parameters'].get('required', [])
        return offering

    def _delivery_decision(self, saved: dict, value: dict, *, current_inputs: bool = True
                           ) -> _DeliveryResponse | _ClaimDeliveryResponse:
        linked = saved.get('finding_policy') == 'linked.v1'
        investigated = ('applicability' in value or self._investigation_contract(saved))
        decision = (self._claim_response_model(saved) if saved.get('work_method') in CLAIM_WORK_METHODS else
            _ScopedDeliveryResponse if saved.get('work_method') in NATIVE_WORK_METHODS else
            _InvestigatedDeliveryResponse if investigated else
            _DeliveryResponseLinked if linked else _DeliveryResponse).model_validate(value)
        if investigated:
            if not decision.applicability.strip() or len({row.claim for row in decision.investigations}) != len(decision.investigations):
                raise ValueError('Independent applicability and distinct investigated claims are required')
            for row in decision.investigations:
                if (not row.claim.strip() or not row.method.strip() or not row.reasoning.strip()
                        or len(set(row.evidence)) != len(row.evidence)
                        or not set(row.evidence) <= set(decision.evidence)):
                    raise ValueError('Investigation must bind its reasoning to selected actual observations')
                if row.outcome == 'supported' and not row.evidence:
                    raise ValueError('A supported investigation needs actual observed evidence')
                if (row.outcome == 'contradicted' and getattr(row, 'necessary', True) and not any(
                        finding.severity == 'required' for finding in decision.findings)):
                    raise ValueError('A contradicted requested claim must retain a required finding')
        if isinstance(decision, (_ScopedDeliveryResponse, _ClaimDeliveryResponse)):
            executed = None
            for i, row in enumerate(decision.investigations):
                if not row.scope.strip() or row.basis == 'not_checked' and row.outcome != 'unresolved':
                    raise ValueError('Unperformed checks remain unresolved with their actual scope')
                mixed = saved.get('inspection_material_policy') == 'operations.v3'
                if not mixed and row.basis != 'executed' and row.execution_records:
                    raise ValueError('Only actual executed checks can select execution records')
                if row.basis == 'executed' or mixed and row.execution_records:
                    if executed is None:
                        executed = (set(self._execution_facts(saved, current_inputs=current_inputs))
                            if mixed else self._executed_check_records(saved))
                    invalid = set(row.execution_records) - executed
                    if not row.execution_records or invalid or len(set(row.execution_records)) != len(row.execution_records):
                        detail = ('no records were supplied' if not row.execution_records else
                                  'records are repeated' if not invalid else
                                  'unavailable or non-execution records: ' + ', '.join(sorted(invalid)))
                        raise ValueError('Executed investigations require actual own successful action records; '
                            f'investigation {i + 1}: {detail}. Select applicable records from executed_checks.')
                findings = (row.findings if isinstance(decision, _ClaimDeliveryResponse) else
                    [f for f in decision.findings if f.investigation == i])
                if row.necessary and row.outcome == 'contradicted' and not any(f.severity == 'required' for f in findings):
                    raise ValueError('Each contradicted requested claim needs its own required finding')
                if (isinstance(decision, _ClaimDeliveryResponse) and any(f.severity == 'required' for f in findings)
                        and (not row.necessary or row.outcome != 'contradicted')):
                    raise ValueError('Required findings must bind an actually contradicted investigation')
                if isinstance(decision, _PresentedClaimDeliveryResponse) and any(
                        window.subject not in row.evidence or window.start >= window.end
                        for window in row.evidence_windows):
                    raise ValueError('Investigation windows must identify nonempty selected evidence ranges')
            if isinstance(decision, _ScopedDeliveryResponse):
                for finding in decision.findings:
                    if (finding.investigation >= len(decision.investigations)
                            or finding.severity == 'required'
                            and (not decision.investigations[finding.investigation].necessary
                                 or decision.investigations[finding.investigation].outcome != 'contradicted')):
                        raise ValueError('Required findings must bind an actually contradicted investigation')
        if linked:
            allowed = {row['id'] for row in saved['prior_required']}
            lists = [decision.reassessed_prior]
            if isinstance(decision, _PresentedClaimDeliveryResponse):
                lists.extend(row.reassessed_prior for row in decision.investigations)
            if any(len(set(ids)) != len(ids) or not set(ids) <= allowed for ids in lists):
                raise ValueError('Reassessed concerns must identify supplied prior findings')
        observed = {row['subject']:row for row in [*saved['targets'], *saved['evidence']]
            if row.get('readable') and row.get('sha256') and row.get('windows')}
        invalid = [key for key in decision.evidence if key not in observed]
        if len(set(decision.evidence)) != len(decision.evidence) or invalid:
            raise ValueError('Package evidence must identify unique actual readable observations: '
                + json.dumps({'invalid': invalid, 'eligible': list(observed)}, ensure_ascii=False))
        if decision.conclusion == 'complete' and saved['delivery_context']['artifacts'] and not decision.evidence:
            raise ValueError('A whole-work conclusion needs actual observed support, not inventory alone')
        artifacts = saved['delivery_context']['artifacts']
        if any(not finding.message.strip() or any(path not in artifacts for path in finding.artifacts)
                for finding in decision.findings):
            raise ValueError('Package findings must name affected bound artifacts and a concrete defect')
        if isinstance(decision, _PresentedClaimDeliveryResponseV4) and any(
                path not in artifacts for row in decision.requested_outcomes for path in row.artifacts):
            raise ValueError('Requested outcomes may name only artifacts in the proposed delivery')
        return decision

    def _retain_valid_investigations(self, saved: dict, value: dict, *,
                                     unavailable_evidence: dict[int, str] | None = None) -> tuple[dict, list[dict]]:
        """Keep independently supported checks when another check cannot be bound."""
        unavailable_evidence = unavailable_evidence or {}
        try:
            if unavailable_evidence:
                raise ValueError(next(iter(unavailable_evidence.values())))
            self._delivery_decision(saved, value)
            return value, []
        except ValueError as original:
            original_error = original
        result = deepcopy(value)
        resolutions = []
        for index, row in enumerate(result['investigations']):
            single = {**result, 'conclusion': 'incomplete', 'investigations': [row],
                      'reassessed_prior': []}
            try:
                if index in unavailable_evidence:
                    raise ValueError(unavailable_evidence[index])
                self._delivery_decision(saved, single)
            except ValueError as exc:
                resolutions.append({'investigation_index': index, 'resolution': 'unresolved',
                                    'reason': str(exc)})
                if row['necessary']:
                    result['conclusion'] = 'incomplete'
                result['limitations'].append(
                    f"Investigation '{row['claim']}' could not be bound to its submitted evidence: {exc}")
                row.update(outcome='unresolved', basis='not_checked', evidence=[], evidence_windows=[],
                    execution_records=[], reassessed_prior=[], findings=[],
                    scope='Not established by the accepted records.',
                    method='The independent response could not be validated for this investigation.',
                    reasoning='The original opinion remains in the inspection record.')
        if not resolutions or not any(row['outcome'] != 'unresolved' and row['evidence']
                                      for row in result['investigations']):
            raise original_error
        self._delivery_decision(saved, result)
        return result, resolutions

    def _executed_check_records(self, saved: dict) -> set[str]:
        """Reuse the common action owner's accepted receipts, never a model claim."""
        if saved.get('inspection_material_policy') == 'operations.v3':
            return set(self._execution_facts(saved))
        return {row['record'] for row in self._investigation_actions(saved)
            if row['call']['function']['name'] == 'run_process' and row['result'].get('status') == 'ok'
                and row['result'].get('data', {}).get('returncode') == 0
                and not row['result'].get('data', {}).get('stop_reason')}

    def _retained_inputs(self, saved: dict) -> list[dict]:
        """Read immutable independent journals, without adopting their opinions."""
        if saved.get('inspection_material_policy') != 'operations.v3':
            return []
        parents, seen = [], {saved['identity']}
        for reference in saved.get('retained_investigations', []):
            identity = reference['identity']
            if identity in seen or set(reference['sha256']) != {'input', 'response', 'result'}:
                raise ValueError('Retained investigation has a repeated or invalid owner')
            seen.add(identity)
            records = {}
            for leaf, digest in reference['sha256'].items():
                relative = self._path(identity, leaf)
                path = self.store.resolve(relative)
                if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)
                        if part.is_relative_to(self.root)):
                    raise ValueError('Retained investigation cannot traverse links')
                records[leaf] = self._bound_json(relative, digest, None, 'Retained investigation changed')
            previous = records['input']
            if (previous.get('identity') != identity or previous.get('protocol') != self.protocol
                    or previous.get('work_method') != GOAL_WORK_METHOD
                    or previous.get('execution_protocol') not in _SHARED_EXECUTION_PROTOCOLS
                    or records['response'].get('input_sha256') != reference['sha256']['input']
                    or records['response'].get('result_sha256') != reference['sha256']['result']
                    or not self._accounted(self._usage(identity))):
                raise ValueError('Retained investigation lacks its closed independent owner')
            try:
                # Withdrawal of source access also withdraws its cached bodies.
                for row in [*previous['targets'], *previous['evidence']]:
                    if row.get('path'):
                        self.workspace.resolve(row['path'])
            except (ValueError, OSError):
                continue
            for number in range(len(previous.get('source_rounds', []))):
                if (self._closed_source_round(previous, number, allow_final=True, model_only=True) is None
                        and not self._unissued_source_round(previous, number)):
                    raise ValueError('Retained investigation has unsettled source work')
            parents.append(previous)
        return parents

    def _inherit_investigation(self, saved: dict, reference: dict, remaining: list[int]) -> None:
        """Bind prior facts to this new review; prior verdicts remain immutable."""
        if saved.get('inspection_material_policy') != 'operations.v3':
            raise ValueError('This saved inspection does not support retained investigation')
        previous = self.store.read_json(self._path(reference['identity'], 'input'))
        # The delivery owner can bind a distinct early-candidate investigation
        # to a later final scope. Only its observations transfer; the current
        # reviewer still decides applicability and receives no inherited pass.
        previous_arguments = (reference.get('arguments', saved['arguments'])
            if saved.get('work_lifecycle') in {'integrated.v1', 'integrated.v2', 'integrated.v3'} else saved['arguments'])
        if (previous.get('delivery_context') != reference['binding']
                or previous.get('arguments') != previous_arguments):
            raise ValueError('Retained investigation differs from its delivery owner scope')
        pinned = {'identity':previous['identity'], 'sha256':{
            leaf:self._digest(self._path(previous['identity'], leaf)) for leaf in ('input', 'response', 'result')}}
        saved['retained_investigations'] = [*previous.get('retained_investigations', []), pinned]
        parents = self._retained_inputs(saved)
        delivery = saved.get('delivery_context', {})
        if delivery.get('scope_policy') == 'applicable.v2' and parents:
            versions = delivery['artifacts']
            for parent in reversed(parents):
                prior = parent.get('delivery_context', {}).get('artifacts', {})
                if not versions.keys() & prior.keys() or versions == prior:
                    continue
                # Changed and newly introduced sources are the starting point.
                saved['revision_sources'] = [path for path, digest in versions.items()
                    if path in prior and prior[path] != digest]
                saved['introduced_artifacts'] = [path for path in versions if path not in prior]
                saved['removed_artifacts'] = [path for path in prior if path not in versions]
                break
        owners = {(row['path'], row['sha256']):row for row in saved['evidence'] if row.get('sha256')}
        selected_paths = None
        if saved.get('source_priming_policy') in {'priority.v3', 'priority.v4'}:
            selected_paths = {row['path'] for row in [*saved['targets'], *saved['evidence']]
                if row.get('path')}
            if 'delivery_context' in saved:
                if 'revision_sources' not in saved:
                    selected_paths.update(saved['delivery_context']['artifacts'])
                selected_paths.update(self._delivery_source_paths(saved))
        number = max((int(row['subject'].split('-')[1]) for row in saved['evidence']), default=0)
        for parent in reversed(parents):
            for row in [*parent['targets'], *parent['evidence']]:
                if not row.get('readable') or not row.get('sha256'):
                    continue
                if 'revision_sources' in saved and selected_paths is not None and row['path'] not in selected_paths:
                    continue  # The original inspection supplies the history locator.
                try:
                    if self.workspace.artifact_hashes([row['path']])[row['path']] != row['sha256']:
                        continue
                except (ValueError, OSError):
                    continue
                key = (row['path'], row['sha256'])
                if key not in owners:
                    number += 1
                    owner = {**deepcopy(row), 'subject':f'evidence-{number}', 'windows':[], 'complete':False,
                        'retained_from':{'inspection':parent['identity'], 'subject':row['subject']}}
                    saved['evidence'].append(owner)
                    owners[key] = owner
                owner = owners[key]
                if selected_paths is not None and row['path'] not in selected_paths:
                    # Keep the source locator, not an unrelated prior body in
                    # every decision. Its original inspection remains readable.
                    continue
                for window in row['windows']:
                    start, end = window['offset_bytes'], window['review_next_offset_bytes']
                    count = end - start
                    if (not any(existing['offset_bytes'] <= start
                            and end <= existing['review_next_offset_bytes'] for existing in owner['windows'])
                            and count <= remaining[0]):
                        owner['windows'].append(deepcopy(window))
                        remaining[0] -= count

    def _execution_facts(self, saved: dict, *, current_inputs: bool = True,
                         retained: list[dict] | None = None) -> dict[str, dict]:
        """Qualify real executions against current inputs, separately from judgment."""
        facts = {}
        parents = self._retained_inputs(saved) if retained is None else retained
        for parent in [saved, *parents]:
            for row in self._investigation_actions(parent, receipts=True):
                result, data = row['result'], row['result'].get('data', {})
                if (row['call']['function']['name'] != 'run_process' or result.get('status') != 'ok'
                        or data.get('returncode') != 0 or data.get('stop_reason')):
                    continue
                try:
                    owners = self._shared_owner_receipts('run_process', ToolResult.model_validate(result),
                        row['receipt'], row['arguments'])
                    if owners != row['owner_receipts']:
                        raise ValueError('Independent execution owner records changed')
                    dependencies = (self._check_bound_process_inputs(row['receipt'], row['arguments'])
                        if current_inputs else {})
                    accepted = self._accepted_execution_binding(row['receipt'])
                    if current_inputs and parent is not saved and not (accepted.get('artifacts') or {}).get('inputs'):
                        # An undeclared read cannot be assigned a precise input
                        # dependency after the fact. Reuse only under the full
                        # unchanged observed scope; otherwise keep it historical.
                        dependencies.update({item['path']:item['sha256'] for item in
                            [*parent['targets'], *parent['evidence']] if item.get('sha256')})
                        if self.workspace.artifact_hashes(dependencies) != dependencies:
                            continue
                except (ValueError, OSError, KeyError, TypeError):
                    continue
                facts[row['record']] = {'inspection':parent['identity'], 'dependencies':dependencies,
                    'source_record':row['source_record'], 'argv':row['arguments'].get('argv', []),
                    'cwd':row['arguments'].get('cwd', 'workspace'), 'process_receipt':data.get('receipt')}
        return facts

    @staticmethod
    def _relevant_execution_sources(saved: dict, facts: dict[str, dict]) -> set[str]:
        paths = {path for finding in saved.get('prior_required', []) for path in finding.get('pending_subjects', [])}
        paths.update(row['path'] for row in saved['targets'] if row.get('path'))
        paths.update(row['path'] for row in saved.get('evidence', [])
            if row.get('path') and not row.get('retained_from'))
        paths.update(saved.get('delivery_context', {}).get('artifacts', {}))
        retained = {}
        if saved.get('source_priming_policy') == 'priority.v4':
            for row in saved.get('evidence', []):
                source = row.get('retained_from') or {}
                if row.get('path') and source.get('inspection'):
                    retained.setdefault(source['inspection'], set()).add(row['path'])
        return {fact['source_record'] for fact in facts.values()
            if paths.intersection(fact['dependencies'])
            or retained.get(fact.get('inspection'), set()).intersection(fact['dependencies'])}

    @staticmethod
    def _review_execution_names(facts: dict[str, dict]) -> dict[str, str]:
        """Resolve existing source-action and unambiguous process receipt locators."""
        names = {fact['source_record']:record for record, fact in facts.items()}
        receipts: dict[str, str | None] = {}
        for record, fact in facts.items():
            receipt = fact.get('process_receipt')
            if isinstance(receipt, str) and receipt:
                if receipt not in receipts:
                    receipts[receipt] = record
                elif receipts[receipt] != record:
                    receipts[receipt] = None
        names.update({receipt:record for receipt, record in receipts.items() if record is not None})
        return names

    def _delivery_assessment(self, saved: dict, receipt: dict, identity: str, failure: str,
                             *, check_versions: bool = True) -> tuple[dict, str]:
        """A whole-work opinion, never a representative file's extended pass."""
        delivery = saved['delivery_context']
        observed = {row['subject']:row for row in [*saved['targets'], *saved['evidence']]}
        error, decision, dependencies, offered, bindings = '', None, {}, {}, {}
        evidence_scopes = []
        validation_records: dict = {}
        try:
            if failure:
                raise ValueError(failure)
            decision = self._delivery_decision(saved, receipt['response'], current_inputs=check_versions)
            if self._offered_coverage(saved):
                offered = self._offered_text_ranges(saved, validation_records=validation_records)
            if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS:
                bindings = self._stored_shared_bindings(saved, receipt['response'])
                dependencies = bindings['delivery']
                if self._applicable_scope(saved) and (decision.reassessed_prior or
                        isinstance(decision, _PresentedClaimDeliveryResponse) and any(
                            row.reassessed_prior for row in decision.investigations)):
                    evidence_scopes = [bindings['evidence:' + key] for key in decision.evidence]
                    evidence_scopes.extend(scope for key, scope in bindings.items()
                        if key.startswith('execution:'))
            if check_versions:
                selected = self._selected_inspection_rows(saved, receipt['response'])
                self._check_inspection_versions(saved, identity, selected=selected)
                if (saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS
                        and bindings != self._shared_final_bindings(saved, receipt['response'],
                            validation_records=validation_records,
                            execution_scopes=any(key.startswith('execution:') for key in bindings))):
                    raise ValueError('Inspection selected producer bindings changed')
                if self.workspace.artifact_hashes(delivery['artifacts']) != delivery['artifacts']:
                    raise ValueError('Bound delivery changed during whole-work review')
            self._verify_bound_json(validation_records)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            error, decision = str(exc), None
        findings = [{'severity':row.severity,'message':row.message,
            'subjects':{path:delivery['artifacts'][path] for path in row.artifacts}}
            for row in decision.findings] if decision else []
        scopes = ([self._offered_row_scope(observed[key], offered) for key in decision.evidence]
            if decision and self._offered_coverage(saved) else [])
        linked = saved.get('finding_policy') == 'linked.v1'
        scoped = isinstance(decision, _PresentedClaimDeliveryResponse)
        full = dict(zip(decision.evidence, (covered for covered, _ in scopes))) if scoped else {}

        def supplied(row: _PresentedClaimInvestigation, key: str) -> bool:
            windows = [window for window in row.evidence_windows if window.subject == key]
            source = observed[key]
            recorded = self._merged_ranges([(window['offset_bytes'], window['review_next_offset_bytes'])
                for window in source['windows']])
            actual = offered.get((source.get('path'), source.get('sha256')), [])
            if windows:
                return all(self._range_supplied(recorded, window.start, window.end)
                           and self._range_supplied(actual, window.start, window.end) for window in windows)
            if saved.get('inspection_evidence_policy') in _PRESENTED_EVIDENCE_POLICIES:
                # An unqualified selection means the text actually offered,
                # not every historical window for this source. Judgment of
                # whether those bytes answer the claim belongs to the reviewer.
                return full[key] or any(max(start, left) < min(end, right)
                                        for start, end in recorded for left, right in actual)
            return full[key]

        supplied_for_package = (all(supplied(row, key) for row in decision.investigations for key in row.evidence)
            if scoped and saved.get('inspection_evidence_policy') in _PRESENTED_EVIDENCE_POLICIES
            else all(covered for covered, _ in scopes))
        complete_opinion = (scoped and saved.get('inspection_evidence_policy') in {'presented.v5', 'presented.v6'}
            and decision.conclusion == 'complete' and supplied_for_package
            and all(not row.necessary or row.outcome == 'supported' for row in decision.investigations))

        def supports_reassessment(prior: dict, sources: list[dict[str, str]]) -> bool:
            if complete_opinion:
                # The independent reviewer owns the meaning of a complete
                # finding, including missing deliverables added since the
                # original finding. At least one current deliverable must
                # actually have been supplied; the old subjects were the
                # observation scope, not necessarily the missing outcome.
                return not delivery['artifacts'] or any(scope.get(path) == digest
                    for scope in sources for path, digest in delivery['artifacts'].items())
            return reassessment_has_subject_support(prior, delivery['artifacts'], sources)

        # A partially completed package can still contain individually rechecked
        # concerns. Keep those only when the selected evidence was actually
        # supplied; this is an independent opinion, not a scientific certificate.
        reassessed = (list(decision.reassessed_prior)
            if decision and linked and supplied_for_package
                and (decision.evidence or not delivery['artifacts']) else [])
        if decision and linked and self._applicable_scope(saved):
            prior = {row['id']:row for row in saved['prior_required']}
            reassessed = [key for key in reassessed
                if supports_reassessment(prior[key], evidence_scopes)]
        unsupported_findings = []
        if scoped:
            findings = []
            for row in decision.investigations:
                if row.findings and (not row.evidence or not all(supplied(row, key) for key in row.evidence)):
                    unsupported_findings.append(row.claim)
                    continue
                findings.extend({'severity':finding.severity, 'message':finding.message,
                    'subjects':{path:delivery['artifacts'][path] for path in finding.artifacts}}
                    for finding in row.findings)
            linked_ids = list(dict.fromkeys(key for row in decision.investigations for key in row.reassessed_prior))
            reassessed = [key for key in reassessed if key not in linked_ids]
            for key in linked_ids:
                checks = [row for row in decision.investigations if key in row.reassessed_prior]
                if any(row.outcome != 'supported' or not row.evidence
                       or not all(supplied(row, subject) for subject in row.evidence) for row in checks):
                    continue
                supporting = (checks if saved.get('inspection_evidence_policy') != 'presented.v4' else
                    [row for row in decision.investigations if row.outcome == 'supported' and row.evidence
                     and all(supplied(row, subject) for subject in row.evidence)])
                row_scopes = [bindings['evidence:' + subject] for row in supporting for subject in row.evidence]
                row_scopes.extend(bindings['execution:' + record] for row in supporting
                    for record in row.execution_records if 'execution:' + record in bindings)
                if supports_reassessment(prior[key], row_scopes):
                    reassessed.append(key)
        pending = ({row['id'] for row in saved['prior_required']} - set(reassessed)
            if decision and linked else set())
        complete = (decision is not None and decision.conclusion == 'complete' and not pending
            and (all(not row.necessary or all(supplied(row, key) for key in row.evidence)
                     for row in decision.investigations) if scoped else
                 all(covered for covered, _ in scopes)))
        investigations = getattr(decision, 'investigations', None)
        if investigations is not None:
            complete = complete and all(not getattr(row, 'necessary', True) or row.outcome == 'supported'
                                        for row in investigations)
        outcomes = getattr(decision, 'requested_outcomes', None)
        if outcomes is not None:
            complete = complete and all(row.status == 'delivered' for row in outcomes)
        limitations = ([*decision.limitations, *(scope for _, scope in scopes),
            *([f'Requested outcome {row.status}: {row.outcome}. {row.explanation}'
                for row in outcomes if row.status != 'delivered'] if outcomes is not None else []),
            *['A finding lacks its selected supplied evidence: ' + claim for claim in unsupported_findings],
            *(['Unfinished independent investigation: ' + row.claim
                for row in investigations if row.outcome == 'unresolved' and getattr(row, 'necessary', True)]
                if investigations is not None else []),
            *([('Some recorded windows were not supplied; each check covers only its selected supplied ranges'
                if scoped else 'Selected evidence includes read windows not proven supplied to an inspection request')]
                if any(not covered for covered, _ in scopes) else []),
            *(['Prior required concerns remain to be rechecked: ' + ', '.join(sorted(pending))]
                if pending else [])]
            if decision else [error or 'Whole-work investigation incomplete'])
        return {'schema_version':'delivery_assessment.v1','artifacts':dict(delivery['artifacts']),
            'coverage':'complete' if complete else 'partial',
            'verdict':'needs_revision' if any(row['severity']=='required' for row in findings)
                else 'passed' if complete else 'incomplete',
            'findings':findings,
            **({'applicability': decision.applicability,
                'investigations': [row.model_dump(mode='json', exclude={'findings'})
                    for row in investigations]}
                if investigations is not None else {}),
            **({'requested_outcomes': [row.model_dump(mode='json') for row in outcomes]}
                if outcomes is not None else {}),
            **({'reassessed_prior': reassessed} if linked else {}),
            'evidence_sha256':self._evidence_with_dependencies(
                {observed[key]['path']:observed[key]['sha256'] for key in decision.evidence}, dependencies)
                if decision else {},
            'limitations':limitations}, error

    def _subject_assessments(self, saved: dict[str, Any], receipt: dict[str, Any],
                             identity: str, failure: str, *, check_versions: bool = True) -> tuple[list[dict], str]:
        observed = {row['subject']: row for row in [*saved['targets'], *saved['evidence']]}
        error, decisions, bindings, offered = '', {}, {}, {}
        validation_records: dict = {}
        try:
            if failure:
                raise ValueError(failure)
            decisions = {row.subject: row for row in self._subject_decisions(saved, receipt['response'])}
            if self._offered_coverage(saved):
                offered = self._offered_text_ranges(saved, validation_records=validation_records)
            if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS:
                bindings = self._stored_shared_bindings(saved, receipt['response'])
                if set(bindings) != set(decisions):
                    raise ValueError('Inspection selected producer bindings do not match its subjects')
            if check_versions:
                selected = self._selected_inspection_rows(saved, receipt['response'])
                self._check_inspection_versions(saved, identity, selected=selected)
                if (saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS
                        and bindings != self._shared_final_bindings(saved, receipt['response'],
                            validation_records=validation_records)):
                    raise ValueError('Inspection selected producer bindings changed')
            self._verify_bound_json(validation_records)
        except (ValueError, OSError, KeyError, TypeError) as exc:
            error, decisions = str(exc), {}
        assessments = []
        for index, target in enumerate(saved['targets']):
            if not target.get('sha256'):
                continue  # Unbound files cannot receive an assessment or a pass.
            decision = decisions.get(target['subject'])
            sources = [observed[key] for key in decision.evidence] if decision else []
            target_complete = (self._offered_target_complete(target, list(observed.values()), offered)
                if self._offered_coverage(saved) else self._complete_target(target, list(observed.values())))
            requested = saved.get('arguments', {}).get('targets', [])
            window_request = (saved.get('coverage_policy') == 'offered.v2' and index < len(requested)
                and isinstance(requested[index], dict))
            scope = self._requested_window(saved, target, index) if window_request else None
            if window_request:
                target_complete = bool(scope is not None and self._range_supplied(
                    offered.get((target.get('path'), target.get('sha256')), []), scope['start'], scope['end']))
            scopes = ([self._offered_row_scope(row, offered) for row in sources]
                if self._offered_coverage(saved) and decision else [])
            limitations = [f"{row['subject']}: {row['limitation']}"
                for row in [target, *sources] if row.get('limitation')
                    and not (self.progressive and row is target and target_complete)]
            if not sources:
                limitations.append('No independently read source evidence; scope is target-text review only')
            if self._offered_coverage(saved) and decision:
                limitations.append(self._offered_row_scope(target, offered)[1])
                limitations.extend(scope for _, scope in scopes)
                if not target_complete or any(not covered for covered, _ in scopes):
                    limitations.append('Requested target or selected evidence has read ranges not proven supplied to an inspection request')
            limitations.extend(decision.limitations if decision else [error])
            if scope is not None:
                limitations.append('Opinion covers only the requested text window, not the whole file')
            findings = [{'subjects': {target['path']: target['sha256']},
                'severity': row.severity, 'message': row.message} for row in decision.findings] if decision else []
            complete = bool(not error and target_complete and all(covered for covered, _ in scopes)
                and (not self.adaptive or decision.conclusion == 'complete'))
            verdict = ('incomplete' if error else
                'needs_revision' if any(row['severity'] == 'required' for row in findings) else
                'passed' if complete else 'incomplete')
            current_scope = saved.get('coverage_policy') == 'offered.v2'
            reassessed = ([item['id'] for item in saved.get('prior_required', [])
                if target['subject'] in item.get('subjects', []) and item.get('id')]
                if current_scope and decision and decision.reassessed_prior else [])
            assessments.append({'schema_version': 'artifact_assessment.v2' if current_scope else 'artifact_assessment.v1',
                'subjects': {target['path']: target['sha256']},
                'evidence_sha256': self._evidence_with_dependencies(
                    {row['path']: row['sha256'] for row in sources},
                    bindings.get(target['subject'], {}) if decision else {}),
                'coverage': ('scoped' if scope is not None else 'complete') if complete else 'partial',
                **({'scope': scope} if scope is not None else {}),
                **({'reassessed_prior': list(dict.fromkeys(reassessed)),
                    'review_id': identity + ':' + target['subject']} if current_scope else {}),
                'verdict': verdict, 'findings': findings,
                'limitations': [*limitations,
                    'Inspection uses retained text and recorded tool observations; results do not certify scientific correctness'
                    if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS else
                    'Text inspection only; no visual or scientific certification']})
        return assessments, error

    def _group_assessment(self, saved: dict[str, Any], receipt: dict[str, Any],
                          identity: str, failure: str) -> tuple[dict, str]:
        targets, evidence = saved['targets'], saved['evidence']
        limitations = [f"{row['subject']}: {row['limitation']}" for row in [*targets, *evidence]
                      if row.get('limitation')]
        independent = [row for row in evidence if row.get('independent') and row.get('readable')
                       and any(window['text'].strip() for window in row['windows'])]
        if not independent:
            limitations.append('No independently read source evidence; scope is target-text review only')
        complete = bool(targets) and all(row['complete'] and row.get('readable') for row in targets)
        error, findings, dependencies = '', [], {}
        try:
            if failure:
                raise ValueError(failure)
            response = _Response.model_validate(receipt['response'])
            if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS:
                bindings = self._stored_shared_bindings(saved, receipt['response'])
                dependencies = bindings['group']
            known = {row['subject']: row for row in targets}
            if any(row.subject not in known or not row.message.strip() for row in response.findings):
                raise ValueError('Inspection finding has an unknown target or empty message')
            if any(not text.strip() for text in response.limitations):
                raise ValueError('Inspection limitation must be nonempty')
            for finding in response.findings:
                row = known[finding.subject]
                if not row.get('sha256'):
                    raise ValueError('Inspection finding target has no bound version')
                findings.append({'subjects': {row['path']: row['sha256']},
                                 'severity': finding.severity, 'message': finding.message})
            limitations.extend(response.limitations)
            selected = self._selected_inspection_rows(saved, receipt['response'])
            self._check_inspection_versions(saved, identity, selected=selected)
            self._check_shared_dependencies(saved, current_inputs=True, selected=selected)
            if (saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS
                    and bindings != self._shared_final_bindings(saved, receipt['response'])):
                raise ValueError('Inspection selected producer bindings changed')
            verdict = ('needs_revision' if any(row['severity'] == 'required' for row in findings)
                       else 'incomplete' if not complete else 'passed')
        except (ValueError, OSError, KeyError) as exc:
            error, verdict = str(exc), 'incomplete'
            findings = []
            limitations.append(error)
        assessment = {'schema_version': 'artifact_assessment.v1',
            'subjects': {row['path']: row['sha256'] for row in targets if row.get('sha256')},
            'evidence_sha256': self._evidence_with_dependencies(
                {row['path']: row['sha256'] for row in evidence
                 if row.get('sha256') and row.get('independent')}, dependencies),
            'coverage': 'complete' if complete else 'partial',
            'verdict': verdict, 'findings': findings,
            'limitations': [*limitations,
                'Inspection uses retained text and recorded tool observations; results do not certify scientific correctness'
                if saved.get('execution_protocol') in _SHARED_EXECUTION_PROTOCOLS else
                'Text inspection only; no visual or scientific certification']}
        return assessment, error

    def _finish(self, saved: dict[str, Any], receipt: dict[str, Any], identity: str,
                *, failure: str = '', persist: bool = True) -> ToolResult:
        targets, evidence = saved['targets'], saved['evidence']
        package = self._delivery_scope(saved)
        if not package and not any(row.get('sha256') for row in targets):
            result = ToolResult(tool_name='inspect_artifacts',status='error',
                error='No readable target version was bound; correct the target selection before inspection',
                data={'targets':targets,'evidence':evidence},metadata={'usage':receipt.get('usage',[])})
            if persist:
                self.store.write_json(self._path(identity,'result'),result.model_dump(mode='json'))
                receipt['result_sha256'] = self._digest(self._path(identity,'result'))
                self.store.write_json(self._path(identity,'response'),receipt)
            return result
        if package:
            assessment, error = self._delivery_assessment(saved, receipt, identity, failure)
            verdict = assessment['verdict']
        elif self.protocol:
            assessment, error = self._subject_assessments(saved, receipt, identity, failure)
            verdict = ('incomplete' if error or any(row['verdict'] == 'incomplete' for row in assessment)
                else 'needs_revision' if any(row['verdict'] == 'needs_revision' for row in assessment) else 'passed')
        else:
            assessment, error = self._group_assessment(saved, receipt, identity, failure)
            verdict = assessment['verdict']
        key = 'delivery_assessment' if package else 'assessments' if self.protocol else 'assessment'
        attempt_store = ArtifactStore(self.root / 'attempts' / identity)
        ref_options = {
            'kind':'delivery_assessment' if package else 'artifact_assessments' if self.protocol else 'artifact_assessment',
            'schema':None if self.protocol else 'artifact_assessment.v1', 'producer':'inspect_artifacts'}
        if persist:
            ref = attempt_store.write_json(f'inspection/{key}.json', assessment, **ref_options)
        else:
            ref = attempt_store.ref(f'inspection/{key}.json', **ref_options)
        artifact_path = f'attempts/{identity}/{ref.path}'
        published = attempt_store.require(ref).read_bytes()
        if (not persist or saved.get('source_provenance') in {'versions.v1', 'versions.v2'}) and (
                json.loads(published) != assessment):
            raise ValueError('Inspection assessment differs from its completed response; no overwrite')
        assessment_digest = hashlib.sha256(published).hexdigest()
        diagnostics = {}
        if self.adaptive and error and saved.get('source_rounds'):
            trace_path = self._path(identity, f"sources-{len(saved['source_rounds'])-1}")
            trace = self.store.read_json(trace_path)
            finals = [call for call in trace.get('response', {}).get('tool_calls', [])
                if isinstance(call, dict) and isinstance(call.get('function'), dict)
                and call['function'].get('name') == 'record_inspection']
            if finals:
                call_id = finals[-1].get('id')
                last = next((row for row in trace.get('results', []) if row.get('call_id') == call_id), {})
                rejected = last.get('result', {})
                arguments = last.get('arguments', {})
                rows = ([arguments] if package else arguments.get('decisions', [])) if isinstance(arguments, dict) else []
                candidates = []
                for row in rows[:4] if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    findings = ([finding for check in row.get('investigations', [])
                        if isinstance(check, dict) and isinstance(check.get('findings'), list)
                        for finding in check['findings']]
                        if package and saved.get('work_method') in CLAIM_WORK_METHODS else row.get('findings', []))
                    candidates.append({**({'scope': 'proposed_delivery'} if package else
                        {'subject': str(row.get('subject', '<omitted>'))[:120]}),
                        'candidate_messages': [str(item.get('message', ''))[:320]
                            for item in (findings[:3] if isinstance(findings, list) else [])
                            if isinstance(item, dict)]})
                if rejected.get('status') == 'blocked':
                    diagnostics['rejected_opinion'] = {
                        'scope': 'Unadopted candidate opinion, not an assessment, fact or pass. '
                            'Requires valid reinspection; raw source trace is authoritative. '
                            'Excerpt is bounded to four decisions, three messages each, 320 characters per message.',
                        'record': trace_path, 'call_id': call_id,
                        'validation_error': rejected.get('error', failure)[:3000],
                        'candidate_excerpt': candidates}
        result = ToolResult(tool_name='inspect_artifacts', status='error' if error else 'ok',
            error=error, summary=f'Artifact inspection: {verdict}',
            data={'record': artifact_path, 'targets': targets, 'evidence': evidence, **diagnostics}, artifacts=[artifact_path],
            metadata={key: assessment, 'capability_artifacts': [ref.to_dict()],
                      'usage': receipt.get('usage', []), 'inspection_binding': {
                          'input_sha256': receipt['input_sha256'],
                          'response_sha256': self._response_digest(receipt),
                          **({'assessment_path': artifact_path} if self.protocol else {}),
                          'assessment_sha256': assessment_digest}})
        if persist:
            self.store.write_json(self._path(identity, 'result'), result.model_dump(mode='json'))
            receipt['result_sha256'] = self._digest(self._path(identity, 'result'))
            self.store.write_json(self._path(identity, 'response'), receipt)
        return result

    def _resume_shared_result(self, saved: dict, identity: str) -> ToolResult:
        """Close a proven final write gap, or return an intact failed diagnostic."""
        response_path = self._path(identity, 'response')
        result_path = self._path(identity, 'result')
        response_bytes = self.store.require(response_path).read_bytes()
        receipt = json.loads(response_bytes)
        if (receipt.get('identity') != identity
                or receipt.get('input_sha256') != self._digest(self._path(identity, 'input'))):
            raise ValueError('Inspection retained response differs from its original input; no overwrite')
        result_bytes = self.store.require(result_path).read_bytes()
        digest = hashlib.sha256(result_bytes).hexdigest()
        retained = json.loads(result_bytes)
        result = ToolResult.model_validate(retained)
        if 'result_sha256' in receipt and receipt['result_sha256'] != digest:
            raise ValueError('Inspection result bytes changed; no overwrite')
        if receipt.get('completed') is True:
            if result.ok and 'result_sha256' in receipt:
                return self._cached(identity)
            self._receipt(identity)
            # A complete provider response may still yield a closed diagnostic
            # (for example a changed input). That never enters acceptance or
            # becomes a reason to replay the already completed investigation.
            failure = '' if result.ok else result.error
        elif receipt.get('completed') is False:
            if result.ok:
                raise ValueError('An unfinished inspection cannot retain an accepted result')
            self._check_shared_dependencies(saved, current_inputs=False)
            failure = receipt.get('error', 'No final response')
        else:
            raise ValueError('Inspection response has no retained completion state')
        expected = self._finish(saved, deepcopy(receipt), identity, failure=failure, persist=False)
        expected_result = expected.model_dump(mode='json')
        if self._delivery_scope(saved) and expected_result['data'].get('rejected_opinion'):
            # Earlier package diagnostics retained the full trace but rendered
            # no excerpt. Accept that exact historical projection on load;
            # never rewrite it or turn its rejected findings into an assessment.
            historical = deepcopy(expected_result)
            historical['data']['rejected_opinion']['candidate_excerpt'] = []
            if retained == historical:
                expected_result = historical
        if retained != expected_result:
            raise ValueError('Inspection retained result differs from its response; no overwrite')
        if (self.store.require(result_path).read_bytes() != result_bytes
                or self.store.require(response_path).read_bytes() != response_bytes
                or receipt['input_sha256'] != self._digest(self._path(identity, 'input'))):
            raise ValueError('Inspection retained closure changed during recovery; no overwrite')
        if not result.ok:
            return result  # Diagnostic history never enters _cached acceptance.
        receipt['result_sha256'] = digest
        self.store.write_json(response_path, receipt)
        return self._cached(identity)

    def _prior(self, targets: list[dict[str, Any]], *, current_policy: bool = False,
               linked: bool = False) -> list[dict[str, Any]]:
        """Present applicable prior concerns without adopting their earlier verdict."""
        current = {row['path']: row['sha256'] for row in targets if row.get('sha256')}
        ids = {row['path']: row['subject'] for row in targets if row.get('sha256')}
        result = []
        if linked:
            concerns = self.prior_concerns(list(current)) if self.prior_concerns else []
            for concern in concerns:
                matched = []
                for path in concern.get('pending_subjects', concern['subjects']):
                    try:
                        subject = ids.get(str(self.workspace.resolve(path)))
                    except (ValueError, OSError):
                        continue
                    if subject is not None and subject not in matched:
                        matched.append(subject)
                if matched:
                    result.append({**concern, 'subjects': matched})
            return result
        assessments = self.prior_assessments() if self.prior_assessments else []
        if current_policy:
            from simple_ar.core.assessments import assessment_state
            _, pending = assessment_state(assessments,
                resolve=self.workspace.resolve if getattr(self, 'workspace', None) else None)
            grouped = {}
            for path in current:
                for identity, finding in pending.get(path, {}).items():
                    row = grouped.setdefault(identity, {'id': identity, 'subjects': [],
                        'severity': 'required', 'message': finding['message']})
                    row['subjects'].append(ids[path])
            return list(grouped.values())
        for assessment in assessments:
            assessment = assessment.get('metadata', {}).get('assessment', assessment)
            if assessment.get('schema_version') not in {'artifact_assessment.v1', 'artifact_assessment.v2'}:
                continue
            for finding in assessment.get('findings', []):
                subjects = finding.get('subjects', {})
                matched = [ids[path] for path, digest in subjects.items() if current.get(path) == digest]
                if (finding.get('severity') == 'required' and subjects
                        and len(matched) == len(subjects)
                        and isinstance(finding.get('message'), str) and finding['message'].strip()):
                    result.append({'subjects': matched,
                                   'severity': 'required', 'message': finding['message']})
        return result

    def work_inputs(self, targets: list[str], *, include_observations: bool = False) -> list[dict]:
        """Supply relevant current opinions to source work, not another copy of its evidence."""
        if include_observations:
            return []
        roots = [self.workspace.resolve(path) for path in targets]
        result = []
        for assessment in self.prior_assessments() if self.prior_assessments else []:
            if any(any(Path(path)==root or root.is_dir() and Path(path).is_relative_to(root)
                    for root in roots) for path in assessment['subjects']):
                result.append({'kind':'artifact_assessment','assessment':assessment,
                    'scope':'Current version-bound inspection opinion, not original source evidence, '
                        'execution permission or a certificate for changed files.'})
        return result

    def _mark_evidence(self, saved: dict[str, Any]) -> None:
        if self.protocol:
            return  # Independence is relative to each explicit subject decision.
        paths = {row.get('path') for row in saved['targets']}
        hashes = {row['sha256'] for row in saved['targets'] if row.get('sha256')}
        for row in saved['evidence']:
            row['independent'] = row.get('path') not in paths and row.get('sha256') not in hashes
            if not row['independent']:
                row['limitation'] = 'Target itself or identical bytes cannot be independent evidence'

    def _target_observation(self, saved: dict[str, Any], row: dict) -> dict | None:
        """Assign decoded same-file windows to their target, only for new progressive work."""
        if not self.progressive or not row.get('readable') or not row.get('sha256'):
            return None
        owners = [target for target in saved['targets'] if target.get('readable')
            and target.get('path') == row.get('path') and target.get('sha256') == row['sha256']]
        for target in owners:
            target['windows'].extend(window for window in row['windows'] if window not in target['windows'])
            target['complete'] = self._complete_target(target, [target])
            if target['complete']:
                target.pop('limitation', None)
        return owners[0] if owners else None

    @staticmethod
    def _prior_references(saved: dict[str, Any]) -> dict[str, str]:
        """Map reviewer-facing concern names to the owner's recorded identities."""
        rows = saved['prior_required']
        if saved.get('prior_reference_policy') == 'short.v1':
            return {f'concern-{i + 1}': row['id'] for i, row in enumerate(rows)}
        return {row['id']: row['id'] for row in rows}
