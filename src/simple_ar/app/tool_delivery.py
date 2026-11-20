"""Delivery binding, reusable output extraction, and terminal commit logic for ToolApplication."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from simple_ar.app.professional_conversation import INTEGRATED_WORK_LIFECYCLES
from simple_ar.app.tool_contracts import _REPORT_PROTOCOL
from simple_ar.core.capabilities import ArtifactRef, ArtifactStore
from simple_ar.tools.materials import journal_tool_calls
from simple_ar.tools.specs import ToolCall, ToolCompletion, tool_completion
from simple_ar.tools.workspace import DeliveryRequest, validate_workspace_arguments


class _ToolDeliveryMixin:
    """Delivery, reuse, and terminal commit methods for ToolApplication."""

    def _bind_delivery(self, call: dict, payload: dict, store: ArtifactStore, *, start_review: bool = False) -> dict:
        """One delivery projection for normal execution and receipt recovery."""
        if (payload.get('status') == 'ok' and self.operations is not None and self.state['turns']
                and self._completion(call['function']['name']) is not None):
            try:
                selected_work = self.operations.selected_work_service(call, self.state['turns'][-1],
                    self.session.store, self.state['requests'])
                spec = self.gateway.registry.get(call['function']['name'])
                if (selected_work and self.operations.direct_completion_allowed(spec)
                        and self._terminal_intent(call)):
                    selected_work = False
                service_error = self.operations.service_result_error(
                    spec, payload.get('data') or {}, selected_work=selected_work)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                service_error = str(exc)
            if service_error:
                payload.update(status='blocked', error=service_error)
                return payload
        delivery = self._delivery(call, payload)
        if (payload.get('status') == 'ok' and self._terminal_intent(call)
                and self._completion(call['function']['name']).kind == 'delivery' and delivery is None):
            payload.update(status='blocked', error='Tool omitted its declared delivery; existing outputs are retained, not completed.')
            return payload
        if payload.get('status') == 'ok' and delivery is not None:
            outputs = tuple(store.require(ArtifactRef.from_dict(row))
                for row in payload.get('metadata', {}).get('capability_artifacts', []))
            try:
                request = (self.operations.delivery.resolve_request(delivery, self._operation_observations())
                           if self.operations is not None and delivery.get('reviewed_candidate') is not None
                           else DeliveryRequest.model_validate(delivery))
            except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                payload.update(status='blocked', error=str(exc))
                return payload
            if self.state['config'].get('report_protocol') == _REPORT_PROTOCOL:
                try:
                    canonical = self.workspace.resolve_delivery(request, current_outputs=outputs)
                    self._validated_report_delivery(canonical['artifacts'], claim=request.report,
                        partial=bool(request.unresolved),
                        publication=payload if call['function']['name'] == 'export_report' else None)
                except (ValueError, OSError, KeyError, RuntimeError) as exc:
                    payload.update(status='blocked', error=str(exc))
                    return payload
            if self.operations is not None:
                record = (store.root.relative_to(self.session.store.root) / 'native_result.json').as_posix()
                try:
                    review_scope = None
                    if (self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES
                            and self._completion(call['function']['name']).kind == 'review'):
                        arguments = self._recorded_arguments(call)
                        review_scope = arguments.get('review_scope') if isinstance(arguments, dict) else None
                        turn = self.state['turns'][-1]
                        decision = str(Path(turn['reply']).with_name('decision_budget.json'))
                        if (review_scope is None and self.session.store.exists(decision)
                                and self.session.store.read_json(decision).get('candidate_review_scope') == 'explicit.v1'):
                            review_scope = 'candidate'
                    delivery = self.operations.delivery.bind_result(request, payload, store.root.name,
                        current_outputs=outputs,
                        current_result=self._operation_observation(payload, call, record),
                        retain=lambda: store.write_json('native_result.json', payload, kind='native_observation'),
                        review_capacity=lambda: self._review_capacity(self._client(store.root.name),
                            candidate=self._completion(call['function']['name']).kind == 'review',
                            partial=bool(request.unresolved)),
                        may_start=start_review,
                        **({'candidate': True, 'review_scope': review_scope} if self.state['config'].get('work_lifecycle') in INTEGRATED_WORK_LIFECYCLES
                            and self._completion(call['function']['name']).kind == 'review' else {}))
                except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
                    payload.update(status='blocked', error=str(exc))
                    return payload
            else:
                # Saved canonical Writer contracts retain their original admission.
                delivery = self.workspace.resolve_delivery(request, current_outputs=outputs)
                retained = payload.setdefault('metadata', {}).get('delivery_hashes')
                try:
                    hashes = self.workspace.delivery_hashes(delivery, retained)
                    delivery['summary'] = self.workspace.delivery_summary(delivery, hashes)
                except (ValueError, OSError) as exc:
                    payload.update(status='blocked', error=str(exc))
                    return payload
                payload['metadata']['delivery_hashes'] = hashes
            if not self._completion(call['function']['name']).field:
                payload['data'] = delivery
            else:
                payload['data'][self._completion(call['function']['name']).field] = delivery
        return payload

    @staticmethod
    def _native_artifacts(store: ArtifactStore, payload: dict, *, name: str = '',
                          kind: str = 'native_observation') -> tuple[ArtifactRef, ...]:
        """Normal commit and crash recovery register the same domain outputs."""
        artifacts = (store.ref('native_result.json', kind=kind),)
        if name == 'implement_code_task' and store.exists('implementation.json'):
            artifacts += (store.ref('implementation.json', kind='implementation_result', schema='research_implementation.v1'),)
        if name in {'prepare_code_task', 'implement_code_task'} and store.exists('execution.json'):
            artifacts += (store.ref('execution.json', kind='prepared_execution', schema='prepared_execution.v1'),)
        if name in {'begin_professional_work', 'finish_professional_work', 'prepare_implementation', 'return_source_work'} and store.exists('professional_receipt.json'):
            artifacts += (store.ref('professional_receipt.json', kind='professional_scope_receipt'),)
        if name in {'begin_professional_work', 'prepare_implementation'} and store.exists('professional_begin.json'):
            artifacts += (store.ref('professional_begin.json', kind='professional_scope_request'),)
        if name in {'begin_professional_work', 'prepare_implementation'} and store.exists('professional_prior.md'):
            artifacts += (store.ref('professional_prior.md', kind='professional_prior'),)
        for row in payload.get('metadata', {}).get('capability_artifacts', []):
            declared = ArtifactRef.from_dict(row)
            store.require(declared)
            if declared not in artifacts:
                artifacts += (declared,)
        return artifacts

    def _completion(self, name: str) -> ToolCompletion | None:
        return tool_completion(name, self.state['config'].get('tool_completions'))

    def _delivery_arguments(self, call: dict[str, Any]) -> Any:
        arguments = json.loads(call['function']['arguments'])
        contract = self._completion(call['function']['name'])
        return arguments.get(contract.field) if contract and contract.field else arguments

    def _terminal_intent(self, call: dict[str, Any]) -> bool:
        contract = self._completion(call['function']['name'])
        if contract is None or contract.kind == 'review':
            return False
        if not contract.field:
            return True
        try:
            arguments = json.loads(call['function']['arguments'])
            return isinstance(arguments, dict) and arguments.get(contract.field) is not None
        except (ValueError, TypeError):
            return False  # Argument validation owns malformed proposals.

    def _delivery(self, call: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any] | None:
        contract = self._completion(call['function']['name'])
        if contract is None or contract.kind not in {'delivery', 'review'}:
            return None
        data = payload.get('data') or {}
        return data.get(contract.field) if contract.field else data

    def _execution_evidence(self, *, require_current_origin: bool = True) -> dict[str, Any]:
        """Bind request identity to evidence verified by the professional owners."""
        code = self.implementation.report_evidence([
            self.session.store.read_json(item['result'])
            for turn in self._evidence_turns('implementation') for item in turn['tools']],
            require_current_origin=require_current_origin)
        observations = self._scientific_observations()
        measurements = self.scientific.measurement_refs(observations) if self.scientific is not None else []
        analyses = self.scientific.analysis_records(observations) if self.scientific is not None else []
        if not code['implementation_refs'] and not measurements:
            raise ValueError('Required report needs actual execution evidence')
        report = self._report_projection()
        current_report = report['requirement'] is not None and report['evidence_contract'] == 'execution.v1'
        contract = self.scientific.execution_contract if self.scientific is not None else None
        scientific = (self.scientific.report_evidence(measurements, code['implementation_refs'],
            analysis_records=analyses if current_report else None)
            if contract is not None else {})
        return {**scientific,
            **({'evidence_contract': 'execution.v1'} if current_report else {}),
            **({'analysis_records': analyses} if not current_report
                and 'conditions' in (contract or {}) else {}),
            'request_count': len(self.state['requests']),
            'requests_sha256': hashlib.sha256(json.dumps(self.state['requests'], ensure_ascii=False).encode()).hexdigest(),
            'implementation_refs': code['implementation_refs'],
            'evidence_sha256': {**scientific.get('evidence_sha256', {}), **code['evidence_sha256']}}

    def _report_inputs(self, *, report: dict | None = None) -> dict[str, Any] | None:
        from simple_ar.report.legacy_contract import bind_report_inputs
        def material_observations():
            observations = []
            active = {domain: {id(turn) for turn in self._evidence_turns(domain)}
                for domain in ('sources', 'analysis', 'scientific')}
            scientific_records = ({row['record'] for row in self._scientific_observations()
                if row.get('contract_current', True)}
                if self.state['config'].get('scientific_condition_reuse') == 'unchanged.v1' else None)
            for turn in self.state['turns']:
                for item in turn['tools']:
                    row = self.session.store.read_json(item['result'])
                    spec = self.gateway.registry.get(row.get('tool_name', ''))
                    if (row.get('status') == 'ok' and spec is not None and spec.domain in active
                            and (item['result'] in scientific_records if spec.domain == 'scientific'
                                 and scientific_records is not None else id(turn) in active[spec.domain])):
                        observations.append(row)
            return observations
        return bind_report_inputs(config=self.state['config'], report=report if report is not None else self._report_projection(),
            workspace=self.workspace, requests=self.state['requests'],
            material_observations=material_observations, execution_evidence=self._execution_evidence,
            implementation=self.implementation)

    def _validated_report_delivery(self, artifacts: list[str], *, claim: Any = None,
                                   partial: bool = False, publication: dict | None = None,
                                   proposed_export: dict | None = None) -> None:
        from simple_ar.report.legacy_contract import validate_report_delivery
        report = self._report_projection()
        def observations():
            return [self.session.store.read_json(item['result']) for turn in reversed(self.state['turns'])
                if report['kind'] == 'conversion' or turn.get('request_count', 1) == len(self.state['requests'])
                for item in reversed(turn['tools'])]
        validate_report_delivery(config=self.state['config'], report=report, observations=observations,
            artifacts=artifacts, workspace=self.workspace, writing=self.writing, publication=self.publication,
            report_inputs=self._report_inputs, claim=claim, partial=partial,
            publication_result=publication, proposed_export=proposed_export)

    def reusable_materials(self) -> dict[str, list[Path]]:
        """Delegate retained artifact ownership; execution assembly grants no reuse proof."""
        from simple_ar.tools.workspace import RetainedDelivery
        return RetainedDelivery(self.session.store.root).reusable_materials()

    def reusable_code_project(self) -> tuple[Path, dict | None, dict] | None:
        """Read a delivered project; only exact projects carry a frozen contract.

        A native project returns (path, None, {}): reusable read-only material,
        with no inherited validator, interpreter, permission or budget.
        """
        if (self.operations is not None and self.state['config'].get('code_task') is None
                and self.state['config'].get('source_generation_protocol') is None):
            from simple_ar.tools.workspace import RetainedDelivery
            candidate = RetainedDelivery(self.session.store.root).reusable_code_project(
                check_producers=self.workspace.check_artifact_producers)
            return (candidate, None, {}) if candidate is not None else None
        if self.implementation is None:
            return None
        if (self.session.manifest.status not in {'completed', 'partial'}
                or self.state['pending'] is not None or not self.state['turns']
                or not self.session.store.exists('delivery.json')):
            return None
        try:
            turn = self.state['turns'][-1]
            if (turn['remaining'] or not turn['tools']
                    or turn.get('request_count', 1) != len(self.state['requests'])):
                return None
            calls = self.session.store.read_json(turn['reply']).get('tool_calls') or []
            observed = turn['tools'][-1]
            if not calls or calls[-1]['id'] != observed['id']:
                return None
            payload = self.session.store.read_json(observed['result'])
            delivery = self._delivery(calls[-1], payload)
            if payload.get('status') != 'ok' or delivery is None:
                return None
            request = DeliveryRequest.model_validate(delivery)
            saved = self.session.store.read_json('delivery.json')
            if self.workspace.resolve_delivery(request) != saved:
                return None
            if self.workspace.artifact_hashes(saved['artifacts']) != (payload.get('metadata') or {}).get('delivery_hashes'):
                return None
            if not self.implementation.exact:
                from simple_ar.tools.implementation_work import _accepted
                record = self.session.store.resolve(observed['result'])
                _accepted(self.implementation, payload, record.parent.name)
                candidate = self.implementation.reusable_project(saved)
                return (candidate, None, {}) if candidate is not None else None
            # A committed candidate is an independent snapshot. Its saved
            # evidence/contract and current candidate bytes still bind reuse;
            # only a new delivery needs the live origin/approval recheck.
            result = self.implementation.validated_delivery([
                self.session.store.read_json(item['result'])
                for turn in self._evidence_turns('implementation') for item in turn['tools']],
                saved['artifacts'], require_current_origin=False)
            from simple_ar.code_task.runtime.state import code_task_paths, load_code_task_manifest
            run = Path(result['code_task_run_dir'])
            candidate = code_task_paths(run).workspace_dir.resolve()
            if not candidate.is_dir() or not candidate.is_relative_to(self.workspace.workspace.resolve()):
                return None
            manifest = load_code_task_manifest(run)
            if self.scientific is not None and self.scientific.execution_contract is not None:
                from simple_ar.code_task.execution.environment import native_validation_command
                from simple_ar.code_task.execution.runner import _argv_command
                manifest = {**manifest, 'benchmark': {**manifest['benchmark'],
                    'command': _argv_command(native_validation_command(manifest, candidate))}}
            return (candidate, manifest,
                dict(self.implementation.exact.edit_policy or {}))
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            if not self.implementation.exact:
                raise  # Native ambiguity/invalid proof must not become a silent selection.
            return None

    def reusable_execution(self) -> dict[str, Any] | None:
        """Read the delivered candidate's formal protocol, not its validator.

        New tasks get no old budget or process output directory. The original
        approved argv template retains output placeholders for a fresh run.
        """
        if (self.implementation is None or not self.implementation.exact or self.reusable_code_project() is None
                or self.state['config'].get('execution_contract') is None
                or 'conditions' in self.state['config']['execution_contract']):
            return None
        evidence = self._execution_evidence(require_current_origin=False)
        if self.scientific.delivery_requirement(evidence):
            return None
        selected = evidence['implementation_refs'][-1]
        refs = [ref for ref in evidence['measurement_refs']
            if (self.session.store.read_json(ref).get('implementation_ref') or {}).get('path') == selected]
        store = ArtifactStore(self.session.store.resolve(refs[-1]).parent)
        binding = store.read_json('scientific_request.json')['execution_approval']['binding']
        execution = {**self.state['config']['execution_contract'], 'command': list(binding['argv']), 'cwd': binding['cwd']}
        protocol = execution.get('protocol')
        if protocol and protocol.get('protected_assets'):
            from simple_ar.code_task.execution.environment import project_command
            execution['protocol'] = {**protocol, 'protected_assets': [
                {**row, 'path': project_command([row['path']], Path(self.state['config']['execution_contract']['cwd']),
                    Path(binding['cwd']))[0]} for row in protocol['protected_assets']]}
        return execution

    def measurement_completion_actions(self) -> list[dict[str, Any]]:
        """Project an explicit caller contract's remaining local comparison.

        Used by initial creation and resume; no process, model, new state or
        retry. The ordinary action journal still owns execution and delivery.
        """
        config = self.state['config']
        if (config['model'] is not None or config.get('report_requirement')
                or 'conditions' not in (config.get('execution_contract') or {})
                or self.session.manifest.status != 'paused' or self.state['pending'] is not None
                or not self.state['turns'] or self.state['turns'][-1]['remaining']):
            return []
        observations = self._operation_observations(domain='scientific')
        locators = self.scientific.measurement_refs(observations)
        selected = self.scientific.condition_results(locators)
        if len(selected) != len(config['execution_contract']['conditions']):
            return []
        # The deterministic comparison can finish itself, but cannot claim
        # unrelated structured outputs merely because its measurements exist.
        brief_ref = self.session.manifest.state_refs.get('brief')
        measurement_only = True
        if brief_ref is not None:
            from simple_ar.app.research_intake import requested_research_outputs
            from simple_ar.research.workflow_contracts import ResearchBrief
            brief = ResearchBrief.from_dict(self.session.store.read_json(brief_ref))
            measurement_only = set(requested_research_outputs(brief)) == {'experiments'}
        arguments = self.scientific.analysis_arguments(locators)
        if measurement_only:
            arguments['delivery'] = {'summary': self.state['requests'][-1], 'artifacts': []}
        elif self.scientific.matching_analysis(self.scientific.analysis_records(observations), arguments) is not None:
            return []  # The comparison is retained; other requested work remains.
        return [{'tool_name': 'analyze_measurements', 'arguments': arguments}]

    def _terminal_error(self, call: dict[str, Any]) -> str:
        if not self._terminal_intent(call):
            return ''
        turn = self.state['turns'][-1]
        calls = self.session.store.read_json(turn['reply']).get('tool_calls') or []
        terminals = [item for item in calls if self._terminal_intent(item)]
        if len(terminals) != 1 or calls[-1]['id'] != call['id']:
            return 'Request exactly one delivery/question as the last action; this terminal action was not executed.'
        if self._completion(call['function']['name']).kind == 'question':
            return ''
        if self.operations is not None:
            try:
                retained = next((self._delivery(call,self.session.store.read_json(item['result']))
                    for item in turn['tools'] if item['id']==call['id']),None)
                delivery = retained or self._delivery_arguments(call)
                observations = self._operation_observations()
                proposed = (ToolCall(tool_name=call['function']['name'],
                    arguments=json.loads(call['function']['arguments'])) if retained is None else None)
                if retained is not None:
                    self.operations.delivery.validate_actual(
                        delivery, observations, include_inspection=True)
                    return ''
                return '; '.join(self.operations.delivery.issues(
                    observations, delivery, proposed=proposed))
            except (ValueError,OSError,KeyError,TypeError,RuntimeError) as exc:
                return str(exc)
        versioned_report = self.state['config'].get('report_protocol') == _REPORT_PROTOCOL
        report = self._report_projection()
        report_required = report['kind'] == 'report'
        terminal_item = next((item for item in turn['tools'] if item['id'] == call['id']), None)
        terminal = self.session.store.read_json(terminal_item['result']) if terminal_item is not None else None
        retained = self._delivery(call, terminal) if terminal is not None else None
        if report_required or versioned_report:
            try:
                arguments = json.loads(call['function']['arguments'])
                request = DeliveryRequest.model_validate(retained or self._delivery_arguments(call))
            except (ValueError, TypeError, KeyError):
                return ''  # The tool's argument validator will reject this proposal.
            try:
                if versioned_report or report_required and not request.unresolved:
                    self._validated_report_delivery(request.artifacts, claim=request.report,
                        partial=bool(request.unresolved), proposed_export=arguments
                            if versioned_report and retained is None and call['function']['name'] == 'export_report' else None)
            except (ValueError, OSError, KeyError, RuntimeError) as exc:
                return str(exc)
        observations = []
        current = {item['id']: (terminal if item is terminal_item
            else self.session.store.read_json(item['result'])) for item in turn['tools']}
        active = {domain: {id(row) for row in self._evidence_turns(domain)}
            for domain in ('sources', 'analysis', 'scientific', 'implementation')}
        for accepted in self.state['turns']:
            current_request = accepted.get('request_count', 1) == len(self.state['requests'])
            for item, original in journal_tool_calls(self.session.store, accepted):
                spec = self.gateway.registry.get(original['function']['name'])
                if spec is not None and spec.domain in active and id(accepted) not in active[spec.domain]:
                    continue
                observed = (current[item['id']] if accepted is turn
                    else self.session.store.read_json(item['result']))
                try:
                    arguments = json.loads(original['function']['arguments'])
                except (ValueError, TypeError):
                    arguments = {}
                observations.append({**observed, 'arguments': arguments, 'record': item['result'],
                    'current_request': current_request})
        proposed = (ToolCall(tool_name=call['function']['name'],
            arguments=json.loads(call['function']['arguments'])) if terminal is None else None)
        try:
            failed = self.gateway.delivery_issues(observations, proposed=proposed,
                delivery=retained or self._delivery_arguments(call),
                require_current_origin=report['kind'] != 'conversion')
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            return str(exc)
        for identity, observed in current.items():
            data = observed.get('data') or {}
            if (observed.get('status') != 'ok' or data.get('returncode', 0) != 0
                    or (data.get('stop_reason') and not (observed.get('tool_name') == 'implement_code_task'
                        and data.get('status') == 'validated' and data.get('stop_reason') == 'stop_point'))):
                failed.append(identity)
        if failed:
            try:
                if (terminal is not None and terminal.get('status') == 'ok' and retained is not None
                        and DeliveryRequest.model_validate(retained).unresolved):
                    return ''  # A domain may force honest partial delivery.
                if DeliveryRequest.model_validate(self._delivery_arguments(call)).unresolved:
                    return ''
            except (ValueError, TypeError, KeyError):
                pass
            return f'Accepted actions have unresolved failures ({", ".join(failed)}); inspect results or disclose unresolved work before delivery.'
        return ''

    def _unacted_reply_count(self) -> int:
        count = 0
        for turn in reversed(self.state['turns']):
            if turn.get('request_count',1) != len(self.state['requests']) or turn['tools'] or turn['remaining']:
                break
            reply = self.session.store.read_json(turn['reply'])
            if reply.get('tool_calls') or not isinstance(reply.get('content'),str) or not reply['content'].strip():
                break
            count += 1
        return count

    def _delivery_feedback(self) -> dict | None:
        return self.operations.delivery.feedback(self._operation_observations(),
            self.session.store.read_json('delivery.json') if self.session.store.exists('delivery.json') else None)

    def _partial_resource_continuation(self) -> str | None:
        """Find an authorized continuation of the last partial delivery."""
        if not self.state['turns']:
            return None
        turn = self.state['turns'][-1]
        if (turn['remaining'] or not turn['tools']
                or turn.get('request_count', 1) != len(self.state['requests'])):
            return None
        record = turn['tools'][-1]['result']
        terms = self.session.manifest.budget.continuation_authorizations
        return next((identity for identity in reversed(self._professional_resource_authorizations())
            if terms[identity].get('resume_after') == record), None)

    def _commit_terminal(self, progress: Callable[[str], None]) -> bool:
        """Consume a closed current-revision observation before any new decision."""
        if not self.state['turns'] or self.state['pending'] is not None:
            return False
        if (self.session.manifest.status not in {'partial', 'completed'}
                and self._partial_resource_continuation() is not None):
            return False  # The committed partial was explicitly reopened, not replayed.
        turn = self.state['turns'][-1]
        if (turn['remaining'] or not turn['tools']
                or turn.get('request_count', 1) != len(self.state['requests'])):
            return False
        calls = self.session.store.read_json(turn['reply']).get('tool_calls') or []
        observed = turn['tools'][-1]
        if not calls or calls[-1]['id'] != observed['id']:
            return False
        call = calls[-1]
        payload = self.session.store.read_json(observed['result'])
        if not self._terminal_intent(call) or payload.get('status') != 'ok':
            return False
        if self.operations is not None and self._completion(call['function']['name']).kind == 'delivery':
            # Execution and fulfillment have different owners. Only a closed,
            # byte-bound result may return a semantic rejection to the loop;
            # corruption or unconfirmed work still stops without replay.
            delivery = self._delivery(call,payload)
            hashes = (payload.get('metadata') or {}).get('delivery_hashes')
            if delivery is None or hashes is None or self.workspace.artifact_hashes(
                    delivery['artifacts']) != hashes:
                raise ValueError('Delivery version is unbound or changed; inspect retained results before continuing')
        terminal_error = self._terminal_error(call)
        if terminal_error:
            if self.operations is None or self.session.manifest.status in {'completed','partial'}:
                raise ValueError(f'Retained terminal observation cannot be committed: {terminal_error}')
            if turn.get('origin') == 'caller':
                reason = 'Explicit delivery not accepted; completed work retained: ' + terminal_error
                self.session.pause(reason)
                progress(reason)
                return True
            if self.session.manifest.status == 'paused':
                self.session.continue_with_revision('Continue after rejected delivery; completed producer work is not replayed')
            progress('Delivery not accepted; completed work retained: '+terminal_error)
            return False
        if self._completion(call['function']['name']).kind == 'question':
            data = validate_workspace_arguments('ask_user', payload['data']).model_dump()
            self.session.pause(data['question'])
            progress(data['question'])
        else:
            delivery = self._delivery(call, payload)
            if delivery is None:
                return False
            request = DeliveryRequest.model_validate(delivery)
            data = request.model_dump()
            status = 'partial' if data['unresolved'] else 'completed'
            if (self.session.manifest.status == status
                    and self.session.store.exists('delivery.json')
                    and self.session.store.read_json('delivery.json') == data):
                return True  # Already committed, including historical unbound deliveries.
            data = self.workspace.resolve_delivery(request)
            # Operation acceptance already owns completeness for this current
            # inventory. Historical non-operation tasks retain their old gate.
            if self.operations is None and not request.unresolved:
                from simple_ar.tools.workspace import missing_delivery_files
                missing = missing_delivery_files(self.workspace, data['artifacts'],
                    self.state['config'].get('delivery_files') or [])
                if missing:
                    raise ValueError('Delivery is missing required workspace outputs: ' + ', '.join(missing)
                        + '; finish the work or explicitly disclose partial delivery')
            hashes = (payload.get('metadata') or {}).get('delivery_hashes')
            if hashes is None or self.workspace.artifact_hashes(data['artifacts']) != hashes:
                raise ValueError('Delivery version is unbound or changed; inspect retained results before continuing')
            from simple_ar.tools.delivery_versions import DeliveryVersions
            DeliveryVersions(self.session.store.root).retain(observed['result'], delivery, hashes,
                turn.get('request_count', 1))
            if self.workspace.artifact_hashes(data['artifacts']) != hashes:
                raise ValueError('Delivery changed during byte retention; retained version available, no delivery commit')
            self.session.store.write_json('delivery.json', data)
            self.session.complete('Delivery manifest saved; scientific quality requires independent checking')
            if data['unresolved']:
                self.session.manifest.status = 'partial'
                self.session.save()
            progress(data['summary'])
        return True
