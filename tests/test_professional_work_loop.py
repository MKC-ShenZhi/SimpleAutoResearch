"""Focused continuation integration tests; safe for the server's offline suite.

Physical flow tests fake only provider transport; generation, receipts, approval,
tiny processes, accounting and reload use their real owners. The resource-stop
test injects a failed tool result to isolate the top-level pause boundary.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from simple_ar.app.tool_application import ToolApplication
from simple_ar.integrations.llm import LLMClient, LLMSettings
from simple_ar.tools.specs import ToolResult
from simple_ar.report.review_evidence import restore_delivery_producers
from tests.report_review_fixtures import changed_record
from tests.test_tool_agent_handoff import _interrupt_outcome_commit


_SAVED_RESULTS = Path(__file__).resolve().parents[1] / 'examples/saved-results-report/report/inputs'
_PRODUCER_DECLARATIONS = [
    'The requested table and figure artifacts have not yet been generated and checked.',
    'Costs are measured per model, not per policy; policy costs remain unknown.',
]
_PRODUCER = '''import argparse
import csv
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

parser = argparse.ArgumentParser()
parser.add_argument('--metrics', required=True)
parser.add_argument('--records', required=True)
parser.add_argument('--wrong-table', action='store_true')
parser.add_argument('--omit-table', action='store_true')
parser.add_argument('--grayscale', action='store_true')
args = parser.parse_args()
rows = list(csv.DictReader(Path(args.metrics).open(encoding='utf-8', newline='')))
records = json.loads(Path(args.records).read_text(encoding='utf-8'))
points = [dict(model=r['model'], policy=r['policy'], rmse=float(r['rmse'])) for r in rows]
with Path('figure-data.csv').open('w', encoding='utf-8', newline='') as stream:
    writer = csv.DictWriter(stream, fieldnames=['model', 'policy', 'rmse'])
    writer.writeheader()
    writer.writerows(points)
lines = ['| Model | Policy | RMSE |', '| --- | --- | ---: |']
for index, row in enumerate(rows):
    value = float(row['rmse']) + (0.5 if args.wrong_table and index == 0 else 0)
    lines.append('| ' + row['model'] + ' | ' + row['policy'] + ' | ' + format(value, '.3f') + ' |')
if not args.omit_table:
    Path('comparison.md').write_text('\\n'.join(lines) + '\\n', encoding='utf-8')
Path('cost-scope.json').write_text(json.dumps(dict(
    measured_model_costs=records['total_median_cost_seconds'],
    timing_scope=records['timing_scope'], policy_level_costs=None)), encoding='utf-8')
fig, ax = plt.subplots(figsize=(6, 3), constrained_layout=True)
for model in ('linear', 'quadratic'):
    selected = [row for row in points if row['model'] == model]
    ax.plot([row['policy'] for row in selected], [row['rmse'] for row in selected],
        label=model, marker='o' if model == 'linear' else 's',
        linestyle='-' if model == 'linear' else '--',
        color=('black' if model == 'linear' else '0.5') if args.grayscale else None)
ax.set_ylabel('Recorded RMSE')
ax.legend()
fig.savefig('main.png', dpi=100, metadata={'Description': json.dumps(points),
    'Style': 'grayscale' if args.grayscale else 'color'})
plt.close(fig)
print('Saved recorded measurements; no fitting or experiment rerun.')
'''


class ProfessionalOutcomeBoundaryTests(unittest.TestCase):
    """Read-only adoption and stop proofs, without a model or action scheduler."""

    def setUp(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityResult
        from simple_ar.tools.gateway import CommonToolGateway
        from simple_ar.tools.registry import ToolRegistry
        from simple_ar.tools.specs import CommonToolSpec

        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = 'attempts/inspection/native_result.json'
        self.owner = SimpleNamespace(root=self.root, session_id='task')
        self.observed = ToolResult(tool_name='inspect_material', data={'issues': ['retained gap']}).model_dump(mode='json')
        self.store = ArtifactStore(self.root / 'attempts/inspection')
        ref = self.store.write_json('native_result.json', self.observed, kind='native_observation')
        self.store.write_attempt_manifest(AttemptManifest(attempt_id='inspection', capability='native_tool',
            status='completed', outputs=(ref,)))
        self.store.write_capability_result(CapabilityResult(status='completed', artifacts=(ref,)))
        ArtifactStore(self.root).write_json('session_manifest.json', {
            'session_id': 'task', 'budget': {'recorded_attempts': ['inspection']}})
        registry = ToolRegistry()
        registry.register(CommonToolSpec(name='inspect_material', description='Simple producer', domain='inspection'))
        self.producer = SimpleNamespace()
        self.consumer = SimpleNamespace(consume_outcome=Mock(return_value={
            'tool_name': 'prepare_implementation', 'arguments': {'repair_review': 'registered-evidence'}}))
        self.gateway = CommonToolGateway(self.root, registry=registry,
            domain_gateways={'inspection': self.producer, 'repair': self.consumer})

    def _action(self, *, consume=True, resume=False):
        return self.gateway.professional_action(self.observed, self.source,
            resume=resume, consume_outcome=consume)

    def test_simple_producer_can_hand_off_once_without_acquiring_a_lifecycle(self):
        self.gateway._domain_gateways['repair_alias'] = self.consumer
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(self._action(resume=True),
            (self.consumer, self.consumer.consume_outcome.return_value))
        self.consumer.consume_outcome.assert_called_once_with(self.observed, self.source)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)
        self.consumer.consume_outcome.reset_mock()
        self.assertEqual(self._action(consume=False), (self.producer, None))
        self.consumer.consume_outcome.assert_not_called()
        # Adoption does not register or authorize the proposed operation.
        self.assertIsNone(self.gateway.registry.get('prepare_implementation'))

    def test_producer_keeps_first_refusal_and_original_resume_hook(self):
        self.producer.continuation = Mock(return_value={'stop': {'reason': 'original_owner_stop'}})
        self.producer.resume_after_budget = Mock(return_value={'tool_name': 'inspect_material', 'arguments': {}})
        self.assertEqual(self._action(), (self.producer, self.producer.continuation.return_value))
        self.assertEqual(self._action(resume=True), (self.producer, self.producer.resume_after_budget.return_value))
        self.consumer.consume_outcome.assert_not_called()
        self.producer.continuation.return_value = None
        self.assertEqual(self._action(), (self.consumer, self.consumer.consume_outcome.return_value))

    def test_ambiguous_adoption_returns_to_coordination_and_absent_adoption_keeps_fallback(self):
        fallback = self.producer.stop_after_review = Mock(return_value={'stop': {'reason': 'work_gap'}})
        other = SimpleNamespace(consume_outcome=Mock(return_value=self.consumer.consume_outcome.return_value))
        self.gateway._domain_gateways['other'] = other
        self.assertEqual(self._action(), (self.producer, None))
        fallback.assert_not_called()  # Even equal proposals from distinct owners are ambiguous.
        self.consumer.consume_outcome.return_value = other.consume_outcome.return_value = None
        self.assertEqual(self._action(), (self.producer, fallback.return_value))
        del self.producer.stop_after_review
        self.assertEqual(self._action(), (self.producer, None))

    def test_stop_proof_uses_actual_native_registration_and_exact_receipt_version(self):
        from simple_ar.tools.professional_outcomes import check_professional_stop, stop_decision

        # No code gateway, workspace or ledger is needed to retain a stop.
        before = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        action = stop_decision(self.owner, self.observed, 'inspection', 'work_gap', 'Retain findings')
        stop = action['stop']
        self.assertEqual((stop['contract'], stop['owner'], stop['completed']),
            ('professional_stop.v1', 'inspect_material', False))
        self.assertEqual(stop['diagnostics'], {'issues': ['retained gap']})
        check_professional_stop(self.root, 'task', self.source, stop)
        with self.assertRaisesRegex(ValueError, 'exact accepted native observation'):
            stop_decision(self.owner, {**self.observed, 'error': 'unaccepted diagnosis'},
                'inspection', 'owner_failed', 'Do not adopt forged evidence')
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, before)
        path = self.store.resolve('native_result.json')
        original = path.read_bytes()
        # Same parsed observation is still a different version of the raw receipt.
        path.write_bytes(original + b'\n')
        with self.assertRaisesRegex(ValueError, 'accepted source'):
            check_professional_stop(self.root, 'task', self.source, stop)
        path.write_bytes(original)
        manifest = self.store.read_attempt_manifest()
        self.store.write_attempt_manifest(replace(manifest, outputs=()))
        with self.assertRaisesRegex(ValueError, 'registered output'):
            check_professional_stop(self.root, 'task', self.source, stop)
        with self.assertRaisesRegex(ValueError, 'registered output'):
            stop_decision(self.owner, self.observed, 'inspection', 'work_gap', 'Retain findings')


class ProfessionalWorkLoopTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'task'
        self.client = LLMClient(LLMSettings(api_key='offline-fixture', model='fixture',
            api_mode='chat', max_output_tokens=2048, retry_attempts=1,
            context_input_tokens=100000))
        self.services = SimpleNamespace(llm_client=self.client)
        self.prompts = []
        self.responses = []
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.native_chat_turn = LLMClient.chat_turn
        # Guard coordinator dispatch independently of the transport's prompt check.
        self.coordinator = self.enterContext(patch.object(LLMClient, 'chat_turn',
            side_effect=AssertionError('Professional continuation requested a coordinator decision')))

    def test_saved_document_offer_does_not_gain_candidate_continuation_after_upgrade(self):
        app = self._create()
        app.state['turns'] = [{'origin': 'request_owner', 'reply': 'attempts/frozen-reply/native_result.json'}]
        call = {'function': {'name': 'compose_document',
            'arguments': json.dumps({'prior_result': 'records/stopped/native_result.json'})}}
        for version, refused in (('work.v1', True), ('work.v2', False), ('work.unknown', True)):
            with self.subTest(contract=version):
                app.session.store.write_json('attempts/frozen-reply/decision_budget.json', {
                    'tool_admission': 'offered.v1', 'document_request': version,
                    'input_projection': {'tool_names': ['compose_document']}})
                result = app._offered_tool_refusal(call)
                self.assertEqual(result is not None, refused)
                if result is not None:
                    self.assertEqual(result.status, 'blocked')
                    self.assertFalse(result.metadata['dispatched'])
        call['function']['arguments'] = json.dumps({'prior_draft': 'workspace/draft.md'})
        app.session.store.write_json('attempts/frozen-reply/decision_budget.json', {
            'tool_admission': 'offered.v1', 'document_request': 'work.v1',
            'input_projection': {'tool_names': ['compose_document']}})
        self.assertIsNone(app._offered_tool_refusal(call))
        self.send.assert_not_called()
        self.coordinator.assert_not_called()

    def _provider(self, mode, request, **kwargs):
        self.assertEqual(mode, 'chat')
        self.assertFalse(request.get('tools'), 'Coordinator dispatch reached the provider')
        system = request['messages'][0]['content']
        prompt, _ = json.JSONDecoder().raw_decode(request['messages'][1]['content'])
        if 'findings' in prompt.get('response_schema', {}).get('properties', {}):
            self.assertIn('scientific certification', system)
        else:
            self.assertIn('Implement one coherent, bounded code work unit', system)
        self.prompts.append(prompt)
        self.assertTrue(self.responses, 'Unexpected additional professional provider call')
        response = self.responses.pop(0)
        if callable(response):
            response = response(prompt)
        return {'choices': [{'message': {'role': 'assistant',
            'content': json.dumps(response)}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

    def _create(self, *, allow_execution=True, professional_inputs=True,
                tool_contracts='on_demand', process_invocations=3, professional_work='owners.v3',
                producer_completion=None, investigate_results=False):
        app = ToolApplication.create(self.root, 'Compute and save sum(range(7))',
            model='fixture', sources={}, services=self.services,
            professional_inputs=professional_inputs, allow_execution=allow_execution,
            investigate_results=investigate_results,
            tool_contracts=tool_contracts,
            max_turns=10, max_tokens=600000, max_attempts=40,
            process_limits={'process_invocations': process_invocations, 'process_wall_seconds': 30})
        if professional_inputs and professional_work is not None:
            # A saved historical fixture, not a production new-task switch.
            app.state['config']['professional_work'] = professional_work
            if producer_completion is None:
                app.state['config'].pop('producer_completion', None)
            app._save()
            return self._load()
        return app

    def _load(self):
        return ToolApplication.load(self.root, services=self.services)

    @staticmethod
    def _prepare(*, repairs=0):
        return {'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Compute and save sum(range(7))',
            'complete_work': True, 'max_repairs': repairs}}

    @staticmethod
    def _proposal(code, *, replacements=None):
        return {'summary': 'Save the computed integer', 'unresolved': [],
            'files': [{'path': 'calculate.py', **({'content': code}
                if replacements is None else {'replacements': replacements})}],
            'execution_plan': {'argv': ['python', 'calculate.py'],
                'outputs': ['result.txt'], 'timeout_sec': 3}}

    @staticmethod
    def _observations(app):
        return [app.session.store.read_json(tool['result'])
            for turn in app.state['turns'] for tool in turn['tools']]

    def _successful_execution(self, app):
        observations = self._observations(app)
        return bool(observations and observations[-1]['tool_name'] == 'execute_implementation'
            and observations[-1]['status'] == 'ok'
            and observations[-1]['data'].get('returncode') == 0)

    def _run(self, app, **kwargs):
        return app.run(progress=lambda _: None,
            should_stop=lambda: self._successful_execution(app), **kwargs)

    def _pending_execution(self, app):
        pending = app.state['pending']
        self.assertIsNotNone(pending, app.result_snapshot())
        self.assertEqual(pending['kind'], 'tool')
        self.assertEqual(pending['call']['function']['name'], 'execute_implementation')
        return pending, app.approval_token(pending['call'])

    def _process_receipts(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes()
            for path in (self.root / 'tools' / 'processes').glob('*/invocation.json')}

    def test_owned_execution_investigates_actual_results_before_coordinator_or_delivery(self):
        from simple_ar.tools.implementation_findings import accepted_investigation
        for value, disposition in ((21, 'outcome_ready'), (17, 'handoff_required')):
            with self.subTest(value=value):
                self.root = self.root / f'actual-{value}'
                app = self._create(professional_work='requests.v1',
                    producer_completion='goal_findings.v1', investigate_results=True)
                code = f"from pathlib import Path\nPath('result.txt').write_text('{value}')\n"
                proposal = self._proposal(code)
                proposal['unresolved'] = ['Proposed command has not run']
                self.responses = [proposal]
                self._run(app, actions=[self._prepare()])
                _, token = self._pending_execution(app)
                prepared = self._observations(app)[-1]
                app = self._load()
                self._run(app, approved_execution=token)
                execution = self._observations(app)[-1]
                reference = execution['data']['execution_ref']
                self.assertEqual(execution['tool_name'], 'execute_implementation')
                self.assertEqual(execution['data']['result_investigation'], 'execution_findings.v1')
                with self.assertRaises(ValueError):
                    accepted_investigation(app.implementation, reference)
                target = str(Path(prepared['data']['project_dir']) / 'result.txt')
                window = {'path': target, 'max_bytes': 100}
                source_window = {'path': str(Path(target).with_name('calculate.py')), 'max_bytes': 1000}
                def final(prompt):
                    evidence = [row['observation_id'] for row in prompt['evidence_index']
                        if row['path'] == target]
                    self.assertTrue(evidence)
                    self.assertNotIn('materials', prompt['response_schema']['properties'])
                    self.assertNotIn('disposition', prompt['response_schema']['properties'])
                    from simple_ar.tools.implementation_findings import findings_response_model
                    model = findings_response_model(prompt['observations'], [target], {})
                    for statement, references in ((' ', evidence), ('Claim', ['unobserved'])):
                        with self.assertRaises(ValueError):
                            model.model_validate({'findings': [{'statement': statement,
                                'evidence': references}]})
                    return {'findings': [{'statement': f'The actual saved result is {value}.',
                            'evidence': evidence}],
                        'limitations': ['The fixture checks an integer only; no scientific or visual certification.'],
                        'remaining_work': ([] if value == sum(range(7)) else
                            ['The executed value does not meet the original sum(range(7)) goal.']),
                        # An advisory provider status does not override actual remaining work.
                        'disposition': 'outcome_ready'}

                def source_only(prompt):
                    evidence, = [row['observation_id'] for row in prompt['evidence_index']
                        if row['path'] == source_window['path']]
                    return {'findings': [{'statement': 'The program imports pathlib.',
                        'evidence': [evidence]}], 'limitations': [], 'remaining_work': []}
                self.responses = [{'read_windows': [source_window, window,
                    *([dict(window, max_bytes=120)] if value == 21 else [])]},
                    *([source_only] if value == 21 else []), final]
                app.run(progress=lambda _: None, should_stop=lambda: bool(self._observations(app)
                    and self._observations(app)[-1]['tool_name'] == 'investigate_implementation'))
                observed = self._observations(app)[-1]
                self.assertEqual(observed['status'], 'ok', observed)
                self.assertEqual(observed['data']['disposition'], disposition)
                self.assertFalse(self.responses)
                inspected = accepted_investigation(app.implementation, reference, include_observations=True)
                evidence = inspected['findings'][0]['evidence'][0]
                self.assertEqual(evidence['path'], target)
                self.assertEqual(evidence['sha256'], hashlib.sha256(str(value).encode()).hexdigest())
                self.assertTrue(evidence['observation_id'].startswith('observed-'))
                self.assertNotIn('quote', evidence)
                self.assertEqual(len(inspected['materials']), 1)
                self.assertEqual(len(inspected['findings'][0]['evidence']), 2 if value == 21 else 1)
                self.assertEqual(inspected['disposition'], disposition)
                self.assertTrue(inspected['observations'])
                with patch.object(app.writing, 'resubmit_after_repair', return_value=None) as resubmit:
                    app.writing.consume_outcome(observed, inspected['record'])
                    if disposition == 'outcome_ready':
                        resubmit.assert_called_once_with(execution)
                    else:
                        resubmit.assert_not_called()
                # Writer consumes actual producer findings and reusable reads,
                # without a coordinator-authored evidence/file recipe.
                context = app._work_context([str(Path(target).parent)])
                self.assertTrue(context['professional_findings'])
                reused = app._material_observations([str(Path(target).parent)])
                self.assertTrue(any(str(value) == row.get('data', {}).get('text') for row in reused))
                ledger, calls = app.ledger.to_dict(), self.send.call_count
                app = self._load()
                cached = app.implementation.call('investigate_implementation',
                    {'execution_ref': reference}, identity=Path(inspected['record']).parts[1])
                self.assertEqual(cached.model_dump(mode='json'), observed)
                with changed_record(Path(target), 'changed after investigation'):
                    with self.assertRaises(ValueError):
                        accepted_investigation(app.implementation, reference)
                self.assertEqual(app.ledger.to_dict(), ledger)
                self.assertEqual(self.send.call_count, calls)
                for name, arguments in (('execute_implementation', {'prepared_result': prepared['data']['prepared_result']}),
                        ('investigate_implementation', {'execution_ref': reference})):
                    count = len(self._observations(app))
                    app.run(actions=[{'tool_name': name, 'arguments': arguments},
                        {'tool_name': 'read_file', 'arguments': window}], progress=lambda _: None,
                        should_stop=lambda: len(self._observations(app)) > count)
                    refused = self._observations(app)[-1]
                    self.assertEqual(refused['status'], 'blocked', refused)
                    self.assertIn('must be last', refused['error'])
                    app._cancel_unstarted('Fixture retains the rejected batch without executing trailing reads')
                    app._save()
                self.assertEqual(app.ledger.to_dict(), ledger)
                self.assertEqual(self.send.call_count, calls)
                self.coordinator.assert_not_called()
                self.assertFalse((self.root / 'delivery.json').exists())

    def test_source_only_and_legacy_execution_do_not_acquire_result_investigation(self):
        app = self._create(professional_work='requests.v1',
            producer_completion='goal_findings.v1', investigate_results=True)
        self.responses = [self._proposal("from pathlib import Path\nPath('result.txt').write_text('21')\n")]
        app.run(actions=[{'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Provide the source only', 'complete_work': False}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        source, = self._observations(app)
        self.assertEqual(source['status'], 'ok', source)
        self.assertNotIn('result_investigation', source['data'])
        self.assertIsNone(app.implementation.continuation(source,
            Path(source['data']['prepared_result']).parts[1]))
        self.coordinator.assert_not_called()

    def test_goal_findings_execution_retains_notes_without_adoption_or_report(self):
        from tests.test_tool_agent_handoff import ConversationContextTests

        app = self._create(professional_work='requests.v1', producer_completion='goal_findings.v1')
        broken = "raise RuntimeError('physical fixture failure')\n"
        fixed = "from pathlib import Path\nPath('result.txt').write_text('21')\n"
        proposal = self._proposal(fixed, replacements=[{'old_text': broken, 'new_text': fixed}])
        proposal['unresolved'] = ['The planned command has not run',
            'Derived data equivalence needs inspection']
        first = self._proposal(broken)
        first['unresolved'] = list(proposal['unresolved'])
        self.responses = [first, proposal]
        self._run(app, actions=[self._prepare(repairs=1)])
        _, failed_token = self._pending_execution(app)
        app = self._load()
        self._run(app, approved_execution=failed_token)
        failed = self._observations(app)[-2]
        self.assertEqual(failed['tool_name'], 'execute_implementation')
        self.assertNotEqual(failed['data']['returncode'], 0)
        self.assertTrue(failed['data']['issues'])
        prepared = self._observations(app)[-1]
        self.assertEqual(prepared['data']['producer_completion'], 'goal_findings.v1')
        code = str(Path(prepared['data']['project_dir']) / 'calculate.py')
        delivery = {'summary': 'Source and output; physical execution only, no scientific certification',
            'artifacts': [code]}
        self.assertTrue(app.implementation.delivery_issues_for(self._observations(app), delivery, []),
            'A stage note cannot substitute for the required actual execution')
        pending, token = self._pending_execution(app)
        app = self._load()
        self._run(app, approved_execution=token)
        executed = self._observations(app)[-1]
        self.assertEqual(executed['data']['unresolved'], proposal['unresolved'])
        self.assertEqual(executed['data']['producer_completion'], 'goal_findings.v1')
        delivery['artifacts'].append(str(Path(prepared['data']['project_dir']) / 'result.txt'))
        self.assertFalse(app.implementation.delivery_issues_for(self._observations(app), delivery, []))
        from simple_ar.tools.producer_declarations import declaration_group, freeze_producer_context
        self.assertIsNone(declaration_group(app.writing, executed['data']['execution_ref']))
        notes = freeze_producer_context(app.writing, [{'path': code}])['producer_notes']
        self.assertEqual(notes[0]['notes'], proposal['unresolved'])
        self.assertEqual(notes[0]['goal'], self._prepare()['arguments']['goal'])
        self.assertTrue(notes[0]['prepared_sha256'])
        ledger, calls = app.ledger.to_dict(), self.send.call_count
        record = app.workspace.resolve(executed['data']['execution_ref'])
        saved = json.loads(record.read_bytes())
        saved['producer_completion'] = None
        with changed_record(record, json.dumps(saved).encode()):
            self.assertTrue(app.implementation.delivery_issues_for(self._observations(app), delivery, []))
        source = app.workspace.resolve(prepared['data']['source_binding'])
        with changed_record(source, source.read_bytes() + b'\n'):
            self.assertTrue(app.implementation.delivery_issues_for(self._observations(app), delivery, []))
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        self.assertIsNone(app.writing.declaration_adoption_contract)

        # Ordinary reads can push the entire professional unit beyond even
        # the recent 16-turn archive index. Their bodies are not outcomes.
        document = app.workspace.workspace / 'ordinary-document.txt'
        document.write_text('RAW_DOCUMENT_BODY_NOT_A_SCIENTIFIC_RESULT\n' * 20, encoding='utf-8')
        fixture = ConversationContextTests()
        for number in range(18):
            arguments = {'path': str(document), 'max_bytes': 2000}
            read = app.workspace.call('read_file', arguments, identity=f'ordinary-{number}')
            self.assertEqual(read.status, 'ok', read)
            app.state['turns'].append(fixture._record_native_turn(app, 'read_file', arguments,
                read.data, f'ordinary-{number}'))
        app._save()
        app = self._load()
        ledger, calls = app.ledger.to_dict(), self.send.call_count
        state = json.dumps(app.state, sort_keys=True)
        originals = {path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        expected = []
        for turn_number, turn in enumerate(app.state['turns']):
            for tool in turn['tools']:
                raw = app.session.store.read_json(tool['result'])
                record = 'records/' + tool['result'].removeprefix('attempts/')
                view = app.gateway.coordination_result(raw, record)
                if raw['tool_name'] == 'read_file':
                    self.assertEqual(view, raw)
                    self.assertNotIn('outcome', view)
                    continue
                self.assertEqual(raw['data']['unresolved'], proposal['unresolved'])
                self.assertNotIn('unresolved', view['data'])
                self.assertEqual(view['data']['producer_notes'], proposal['unresolved'])
                self.assertEqual(view['data'].get('issues', []), raw['data'].get('issues', []))
                outcome = view['outcome']
                phase = 'prepared' if raw['tool_name'] == 'prepare_implementation' else 'executed'
                self.assertEqual(outcome['work_phase'], phase)
                if phase == 'executed':
                    self.assertEqual(outcome['status'], raw['data']['status'])
                self.assertEqual(outcome['producer_note_count'], len(proposal['unresolved']))
                self.assertEqual(outcome['issue_count'], len(raw['data'].get('issues', [])))
                locator = 'prepared_result' if phase == 'prepared' else 'execution_ref'
                self.assertEqual(outcome[locator], raw['data'][locator])
                self.assertTrue(outcome['material_roots'])
                candidate = raw['data']['project_dir'] if phase == 'prepared' else raw['data']['cwd']
                self.assertIn(app.workspace.resolve(candidate),
                    [app.workspace.resolve(root) for root in outcome['material_roots']])
                scope = str(outcome['verification']).lower()
                self.assertIn('scientific', scope)
                self.assertTrue('unverified' in scope or 'no ' in scope, scope)
                expected.append({'turn': turn_number, 'request_count': turn.get('request_count', 1),
                    'record': record, 'tool': raw['tool_name'], 'outcome': outcome})
                # A consumer may edit its detached view without rewriting raw.
                view['data']['producer_notes'].append('consumer-only mutation')
                self.assertEqual(app.session.store.read_json(tool['result']), raw)

        self.assertEqual(len(expected), 4)  # prepare, failed run, repair, successful run
        self.assertGreater(expected[1]['outcome']['issue_count'], 0)
        self.assertEqual(expected[-1]['outcome']['issue_count'], 0)
        self.assertEqual(expected[-1]['outcome']['status'], 'run_succeeded')
        self.assertTrue(all(entry.status == 'settled' for entry in app.ledger.entries))
        self.assertEqual(len(self._process_receipts()), 2)
        self.assertEqual((Path(prepared['data']['project_dir']) / 'result.txt').read_text(), '21')
        # Historical receipts without the completion contract keep their raw shape.
        legacy = json.loads(json.dumps(executed))
        legacy['data'].pop('producer_completion')
        self.assertEqual(app.gateway.coordination_result(legacy, 'records/legacy/native_result.json'), legacy)

        client = LLMClient(replace(self.client._settings, context_input_tokens=16000))
        messages = app._conversation_messages(client=client,
            native_history_start_turn=len(app.state['turns']), schemas=[])
        context = json.loads(messages[1]['content'])
        self.assertGreater(context['earlier_turns']['start_turn'], expected[-1]['turn'])
        self.assertEqual(context['professional_outcomes'], expected)
        self.assertNotIn('RAW_DOCUMENT_BODY_NOT_A_SCIENTIFIC_RESULT', json.dumps(messages))
        self.assertFalse(context['resources']['context_exceeds_input_target'])
        self.assertLessEqual(context['resources']['approximate_next_input_tokens'],
            client._settings.context_input_tokens)
        self.assertEqual(json.dumps(app.state, sort_keys=True), state)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*') if path.is_file()}, originals)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(calls, 2)
        self.coordinator.assert_not_called()
        self.assertFalse(app.session.store.exists('delivery.json'))

    def test_goal_findings_source_only_does_not_require_writer_or_note_copying(self):
        app = self._create(allow_execution=False, professional_work='requests.v1',
            producer_completion='goal_findings.v1')
        self.responses = [{'summary': 'Requested source only',
            'unresolved': ['The planned command has not run'],
            'files': [{'path': 'calculate.py', 'content': 'print(21)\n'}]}]
        action = {'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Supply source only; execution is not requested', 'complete_work': False}}
        app.run(actions=[action], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        prepared = self._observations(app)[-1]
        code = str(Path(prepared['data']['project_dir']) / 'calculate.py')
        delivery = {'summary': 'Prepared source only; execution and scientific correctness unverified',
            'artifacts': [code]}
        self.assertFalse(app.implementation.delivery_issues_for(self._observations(app), delivery, []))
        self.assertEqual(prepared['data']['unresolved'], ['The planned command has not run'])
        self.assertIn('unverified', prepared['data']['verification'])
        self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual([row['tool_name'] for row in self._observations(app)], ['prepare_implementation'])
        from simple_ar.tools.producer_declarations import freeze_producer_context
        notes = freeze_producer_context(app.writing, [{'path': code}])['producer_notes']
        self.assertEqual(notes[0]['notes'], prepared['data']['unresolved'])
        self.assertNotIn('execution', notes[0])

    def test_goal_findings_source_only_cannot_waive_explicit_execution_request(self):
        app = self._create(allow_execution=False, professional_work='requests.v1',
            producer_completion='goal_findings.v1')
        self.responses = [self._proposal('print(21)\n')]
        action = {'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Execute the requested program and save its result',
            'complete_work': False, 'execution_requested': True}}
        app.run(actions=[action], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        prepared = self._observations(app)[-1]
        self.assertEqual(prepared['status'], 'ok', prepared)
        delivery = {'summary': 'A source candidate is not the requested execution',
            'artifacts': [str(Path(prepared['data']['project_dir']) / 'calculate.py')]}
        self.assertTrue(app.implementation.delivery_issues_for(self._observations(app), delivery, []))
        self.assertEqual(self._process_receipts(), {})

    def test_goal_findings_context_stays_guarded_before_and_after_physical_execution(self):
        from simple_ar.core.capabilities import ArtifactStore

        app = self._create(professional_work='requests.v1', producer_completion='goal_findings.v1')
        diagnostic = app.workspace.workspace / 'diagnosis.txt'
        diagnostic.write_text('original diagnosis', encoding='utf-8')
        proposal = self._proposal("from pathlib import Path\n"
            "Path('result.txt').write_text('21')\n"
            f"Path({str(diagnostic)!r}).write_text('unexpected context mutation')\n")
        self.responses = [
            {'read_windows': [{'path': str(diagnostic), 'max_bytes': 1000}]},
            {'focus_observations': []}, proposal]
        request = self._prepare()
        request['arguments']['material_roots'] = [str(diagnostic)]
        self._run(app, actions=[request])
        pending, token = self._pending_execution(app)
        approved = app._owner_execution_binding(pending['call'])
        self.assertIn(str(diagnostic), approved['context_sha256'])
        self.assertFalse(any(row.get('text') == 'original diagnosis'
            for row in self.prompts[-1]['source_windows']))
        prepared = next(row for row in self._observations(app) if row['tool_name'] == 'prepare_implementation')
        source_record = app.workspace.resolve(prepared['data']['source_binding'])
        source = json.loads(source_record.read_bytes())
        self.assertTrue(any(row.get('text') == 'original diagnosis' for row in source['source_windows']))
        # Even a journaled-but-unstarted approval must recheck context bytes.
        execution_store = ArtifactStore(self.root / 'context-guard-probe')
        approval = {'binding': approved, 'token': token}
        execution_store.write_json('execution_input.json', approval)
        with changed_record(diagnostic, b'changed before physical execution'):
            from simple_ar.tools.implementation_run import execute_prepared
            with self.assertRaisesRegex(ValueError, 'changed before execution'):
                execute_prepared(app.implementation, SimpleNamespace(
                    attempt=SimpleNamespace(attempt_id='context-guard-probe'), store=execution_store),
                    json.loads(pending['call']['function']['arguments']), approval=approval)
        self.assertFalse(self._process_receipts())
        app = self._load()
        app.run(approved_execution=token, progress=lambda _: None)
        executed = self._observations(app)[-1]
        self.assertEqual(executed['tool_name'], 'execute_implementation')
        self.assertEqual(executed['data']['returncode'], 0)
        self.assertEqual(executed['data']['status'], 'changed_inputs')
        self.assertFalse(executed['data']['output_sha256'])
        self.assertIn('context changed during execution', executed['error'])
        self.assertEqual(len(self._process_receipts()), 1)
        self.assertEqual(self.send.call_count, 3)

    def test_native_code_repairs_with_fresh_approval_and_shared_accounting(self):
        broken = ("from pathlib import Path\n"
            "Path('partial').mkdir()\n"
            "Path('partial/unfinished.csv').write_text('value\\n21\\n')\n"
            "raise RuntimeError('fixture failure before output')\n")
        intermediate = "raise ValueError('a different technical failure')\n"
        fixed = ("from pathlib import Path\n"
            "p = Path('result.txt')\n"
            "assert not p.exists(), 'physical replay'\n"
            "p.write_text(str(sum(range(7))), encoding='utf-8')\n")
        self.responses = [self._proposal(broken), self._proposal(intermediate,
            replacements=[{'old_text': broken, 'new_text': intermediate}]),
            self._proposal(fixed, replacements=[{'old_text': intermediate, 'new_text': fixed}])]
        app = self._create()
        # Workspace is excluded from default original-source discovery. An
        # explicit native window, not retained coordinator prose, supplies it.
        material = app.workspace.workspace / 'owner-input/limit.txt'
        material.parent.mkdir()
        material.write_text('exclusive_upper_bound=7\n', encoding='utf-8')
        material_bytes = material.read_bytes()
        self.assertEqual(app.state['config']['professional_work'], 'owners.v3')
        # The new contract owns the outcome against the shared budget, not an
        # independent max_repairs allowance (even an explicitly saved zero).
        request = self._prepare(repairs=0)
        window = {'path': 'workspace/owner-input/limit.txt', 'max_bytes': 1000, 'full_digest': True}
        request['arguments']['source_windows'] = [window]
        status = app.run(actions=[{'tool_name': 'read_file', 'arguments': window}], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(status['status'], 'paused', status)
        self.assertEqual(self.send.call_count, 0)
        read_record = app.state['turns'][0]['tools'][0]['result']
        read_bytes = app.session.store.resolve(read_record).read_bytes()
        app = self._load()
        # Explicitly archive the read from the coordinator projection. The
        # owner must receive its actual window without another LLM investigation.
        # Revalidation reads/hashes of the original bytes remain legitimate.
        hidden = app._messages(client=self.client, native_history_start_turn=1)
        self.assertNotIn('exclusive_upper_bound=7', json.dumps(hidden))
        self.assertEqual(self._run(app, actions=[request])['status'], 'paused')
        supplied = [row for row in self.prompts[0]['source_windows'] if 'request' in row
            and app.workspace.resolve(row['request']['path']) == material]
        self.assertTrue(supplied, 'The real code consumer lost its explicit native workspace material')
        for row in supplied:
            self.assertEqual(row['text'], material_bytes.decode('utf-8'))
            self.assertEqual(row['sha256'], hashlib.sha256(material_bytes).hexdigest())
        self.assertEqual(material.read_bytes(), material_bytes)
        self.assertEqual(app.session.store.resolve(read_record).read_bytes(), read_bytes)
        first_pending, first_token = self._pending_execution(app)
        first_id = first_pending['attempt']
        observed_read, first_prepared = self._observations(app)
        self.assertEqual((observed_read['tool_name'], observed_read['status']), ('read_file', 'ok'))
        self.assertEqual(app.state['turns'][1]['origin'], 'caller')
        first_candidate = Path(first_prepared['data']['project_dir'])
        self.assertEqual(first_prepared['status'], 'ok', first_prepared)
        binding = json.loads((app.workspace.resolve(
            first_prepared['data']['prepared_result']).parent / 'request.json').read_bytes())
        self.assertEqual(binding['request']['work_contract'], 'shared.v2')
        self.assertEqual(binding['request']['candidate_admission'], 'python_candidate_admission.v1')
        self.assertFalse((first_candidate / 'result.txt').exists())
        self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)

        app = self._load()
        self.assertEqual(self._pending_execution(app)[1], first_token)
        self._run(app)  # Reload and resumption cannot grant implicit approval.
        self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)
        status = self._run(app, approved_execution=first_token)
        self.assertEqual(status['status'], 'paused', status)
        second_pending, second_token = self._pending_execution(app)
        self.assertNotEqual(first_token, second_token)
        self.assertNotEqual(first_id, second_pending['attempt'])
        observations = self._observations(app)
        self.assertEqual((first_candidate / 'partial/unfinished.csv').read_text(), 'value\n21\n')
        repaired_candidate = Path(observations[-1]['data']['project_dir'])
        self.assertFalse((repaired_candidate / 'partial').exists(),
            'Failed-run artifacts stay retained, not silently copied as accepted source or results')
        self.assertEqual([row['tool_name'] for row in observations],
            ['read_file', 'prepare_implementation', 'execute_implementation', 'prepare_implementation'])
        failed = observations[2]
        self.assertNotEqual(failed['data']['returncode'], 0, failed)
        self.assertIn('fixture failure before output', failed['data']['stderr'])
        self.assertTrue(self.prompts[1]['execution_observations'])
        self.assertIn('fixture failure before output',
            json.dumps(self.prompts[1]['execution_observations']))
        self.assertEqual(self.prompts[1]['original_files']['calculate.py']['text'], broken)
        self.assertEqual(self.send.call_count, 2)
        receipts_after_failure = self._process_receipts()
        self.assertEqual(len(receipts_after_failure), 1)
        self.assertNotEqual(first_candidate, repaired_candidate)

        app = self._load()
        before = app.ledger.to_dict()
        self._run(app, approved_execution=first_token)  # Old approval is not transferable.
        self.assertEqual(app.ledger.to_dict(), before)
        self.assertEqual(self._process_receipts(), receipts_after_failure)
        self.assertFalse((repaired_candidate / 'result.txt').exists())
        self.assertEqual(self._pending_execution(app)[1], second_token)
        self.assertEqual(self.send.call_count, 2)
        self._run(app, approved_execution=second_token)
        third_pending, third_token = self._pending_execution(app)
        self.assertNotIn(third_token, {first_token, second_token})
        self.assertNotEqual(third_pending['attempt'], second_pending['attempt'])
        observations = self._observations(app)
        second_failed = observations[-2]
        self.assertEqual(second_failed['tool_name'], 'execute_implementation')
        self.assertNotEqual(second_failed['data']['returncode'], 0)
        self.assertIn('a different technical failure', second_failed['data']['stderr'])
        self.assertEqual(self.prompts[2]['original_files']['calculate.py']['text'], intermediate)
        self.assertIn('a different technical failure', json.dumps(self.prompts[2]['execution_observations']))
        self.assertEqual(self.send.call_count, 3)
        receipts_after_second_failure = self._process_receipts()
        self.assertEqual(len(receipts_after_second_failure), 2)
        final_candidate = Path(observations[-1]['data']['project_dir'])
        app = self._load()
        ledger_before_third_approval = app.ledger.to_dict()
        self._run(app, approved_execution=second_token)
        self.assertEqual(self._pending_execution(app)[1], third_token)
        self.assertEqual(self._process_receipts(), receipts_after_second_failure)
        self.assertEqual(app.ledger.to_dict(), ledger_before_third_approval)
        self.assertFalse((final_candidate / 'result.txt').exists())
        self._run(app, approved_execution=third_token)
        self.assertTrue(self._successful_execution(app), app.result_snapshot())
        self.assertEqual((final_candidate / 'result.txt').read_text(), '21')
        self.assertFalse((repaired_candidate / 'result.txt').exists())
        self.assertFalse((first_candidate / 'result.txt').exists())
        self.assertFalse(app.session.store.exists('delivery.json'))
        receipts = self._process_receipts()
        self.assertEqual(len(receipts), 3)
        for path, raw in receipts_after_second_failure.items():
            self.assertEqual(receipts[path], raw)
        provider_entries = [entry for entry in app.ledger.entries
            if entry.actual.get('llm_requests')]
        process_entries = [entry for entry in app.ledger.entries
            if entry.actual.get('process_invocations')]
        self.assertEqual(len(provider_entries), 3)
        self.assertEqual(sum(entry.actual['total_tokens'] for entry in provider_entries), 360)
        self.assertEqual(len(process_entries), 3)
        for entry in provider_entries + process_entries:
            self.assertEqual(entry.status, 'settled')
        for entry in provider_entries:
            self.assertEqual(entry.session_id, app.session.manifest.session_id)
        for raw in receipts.values():
            receipt = json.loads(raw)
            entry, = [entry for entry in process_entries
                if entry.reservation_id == 'process-' + receipt['invocation_id']]
            self.assertEqual(entry.actual, {'process_invocations': 1,
                'process_wall_seconds': receipt['duration_sec']})

        # Five synthetic turns do not become coordinator decisions, but
        # all three physical generation requests consume the shared allowance.
        self.assertEqual([turn.get('origin') for turn in app.state['turns']],
            ['caller', 'caller'] + ['professional'] * 5)
        self.assertEqual(app._request_capacity(), 7)
        self.assertEqual(app._work_focus()['purpose'], 'task')
        self._assert_professional_conversation(app)
        ledger = app.ledger.to_dict()
        app = self._load()
        self._run(app, approved_execution=third_token)
        self.assertEqual(self._process_receipts(), receipts)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, 3)
        self.coordinator.assert_not_called()

    def test_owner_stop_receipts_preserve_stagnation_budget_and_historical_ceiling(self):
        base = self.root
        for mode, reason, executions in (
                ('stagnated', 'repair_stagnated', 2),
                ('budget', 'shared_budget_exhausted', 1),
                ('historical', 'repair_ceiling', 1)):
            with self.subTest(mode=mode):
                self.root = base / mode
                self.prompts.clear()
                self.send.reset_mock()
                broken = "raise RuntimeError('same technical failure')\n"
                changed = broken + '# Different bytes do not prove diagnostic progress.\n'
                self.responses = [self._proposal(broken)]
                if executions == 2:
                    self.responses.append(self._proposal(changed,
                        replacements=[{'old_text': broken, 'new_text': changed}]))
                app = self._create(process_invocations=1 if mode == 'budget' else 3)
                if mode == 'historical':
                    # Freeze the old contract before any owner work; loading
                    # must not upgrade its explicit zero repair allowance.
                    app.state['config']['professional_work'] = 'owners.v1'
                    app._save()
                    app = self._load()
                self._run(app, actions=[self._prepare(repairs=0)])
                for index in range(executions):
                    _, token = self._pending_execution(app)
                    count = len(self._observations(app))
                    app.run(approved_execution=token, progress=lambda _: None,
                        should_stop=lambda: len(self._observations(app)) > count)
                    observed = self._observations(app)[-1]
                    self.assertEqual(observed['tool_name'], 'execute_implementation')
                    self.assertNotEqual(observed['data']['returncode'], 0, observed)
                    if index + 1 < executions:
                        self._run(app)
                attempt = Path(observed['data']['execution_ref']).parent.name
                stop = app.implementation.continuation(observed, attempt)['stop']
                self.assertEqual((stop['contract'], stop['reason'], stop['completed']),
                    ('professional_stop.v1', reason, False))
                record = app.workspace.resolve(stop['source_result'])
                raw = record.read_bytes()
                self.assertEqual(json.loads(raw), observed)
                self.assertEqual(stop['source_sha256'], hashlib.sha256(raw).hexdigest())
                request = json.loads((app.workspace.resolve(
                    observed['data']['prepared_result']).parent / 'request.json').read_bytes())['request']
                if mode == 'historical':
                    self.assertNotIn('work_contract', request)
                    self.assertNotIn('candidate_admission', request)
                    self.assertEqual(request['max_repairs'], 0)
                else:
                    self.assertEqual(request['work_contract'], 'shared.v2')
                ledger, receipts = app.ledger.to_dict(), self._process_receipts()
                self.assertEqual(len(receipts), executions)
                stop_journal = None
                stopped_turns = None
                for _ in range(2):
                    app = self._load()
                    self.assertEqual(app.implementation.continuation(observed, attempt), {'stop': stop})
                    status = self._run(app)
                    self.assertEqual(status['status'], 'paused', status)
                    self.assertIn(reason, app.session.manifest.status_reason)
                    self.assertIsNone(app.state['pending'])
                    last = app.state['turns'][-1]
                    self.assertEqual((last['origin'], last['remaining'], last['tools']),
                        ('professional', [], []))
                    journal = app.session.store.resolve(last['reply']).read_bytes()
                    self.assertEqual(json.loads(journal)['owner_stop'], stop)
                    self.assertEqual(json.loads(journal)['tool_calls'], [])
                    self.assertEqual(json.loads(journal)['resource_authorizations'], [])
                    if stop_journal is None:
                        stop_journal, stopped_turns = journal, len(app.state['turns'])
                    self.assertEqual(journal, stop_journal)
                    self.assertEqual(len(app.state['turns']), stopped_turns)
                    self.assertEqual(app.ledger.to_dict(), ledger)
                    self.assertEqual(self._process_receipts(), receipts)
                    self.assertEqual(record.read_bytes(), raw)
                    self.assertFalse(app.session.store.exists('delivery.json'))
                self.assertEqual(self.send.call_count, executions)
                self.assertFalse(self.responses)
                self.assertTrue(all(entry.status == 'settled' for entry in app.ledger.entries))
                stop_path = app.session.store.resolve(last['reply'])
                old_entries = {entry.reservation_id: entry.to_dict() for entry in app.ledger.entries}
                requests = list(app.state['requests'])
                grant = {'resource_allowances': {'process_invocations': 1},
                    'authorization_id': 'offline-owner-process-continuation',
                    'authorization_reason': 'One explicitly authorized additional offline process.'}
                if mode == 'budget':
                    # A different actual failure spends the new process grant,
                    # so replaying this authorization must not replenish it.
                    revised = "raise ValueError('different failure after resource authorization')\n"
                    self.responses = [self._proposal(revised,
                        replacements=[{'old_text': broken, 'new_text': revised}])]
                app = self._load()
                status = self._run(app, **grant)  # No feedback or new action.
                self.assertEqual(status['status'], 'paused', status)
                self.assertEqual(app.state['requests'], requests)
                terms = app.session.manifest.budget.continuation_authorizations[grant['authorization_id']]
                authorization, = [entry for entry in app.ledger.entries
                    if entry.reservation_id == grant['authorization_id']]
                self.assertEqual(authorization.actual_source, 'user_authorization')
                self.assertEqual(authorization.reserved, terms['resource_allowances'])
                self.assertEqual(authorization.reserved, grant['resource_allowances'])
                self.assertEqual(stop_path.read_bytes(), stop_journal)
                self.assertEqual(record.read_bytes(), raw)
                self.assertEqual(self._process_receipts(), receipts)
                if mode == 'budget':
                    pending, authorized_token = self._pending_execution(app)
                    self.assertNotEqual(authorized_token, token)
                    prepared = self._observations(app)[-1]
                    self.assertEqual(prepared['tool_name'], 'prepare_implementation')
                    self.assertEqual(prepared['status'], 'ok', prepared)
                    binding = json.loads((app.workspace.resolve(
                        prepared['data']['prepared_result']).parent / 'request.json').read_bytes())['request']
                    self.assertEqual(binding['repair_execution'], observed['data']['execution_ref'])
                    self.assertEqual(binding['work_contract'], 'shared.v2')
                    self.assertEqual(app.state['turns'][-1]['origin'], 'professional')
                    self.assertEqual(self.send.call_count, executions + 1)
                    self.assertEqual(self.prompts[-1]['original_files']['calculate.py']['text'], broken)
                    app = self._load()
                    self.assertEqual(self._pending_execution(app)[0]['attempt'], pending['attempt'])
                    status = self._run(app, approved_execution=authorized_token)
                    self.assertEqual(status['status'], 'paused', status)
                    latest = self._observations(app)[-1]
                    self.assertNotEqual(latest['data']['returncode'], 0, latest)
                    self.assertIn('different failure after resource authorization', latest['data']['stderr'])
                    self.assertIsNone(app.state['pending'])
                    new_stop = app.session.store.read_json(app.state['turns'][-1]['reply'])
                    self.assertEqual(new_stop['owner_stop']['reason'], 'shared_budget_exhausted')
                    self.assertEqual(new_stop['resource_authorizations'], [grant['authorization_id']])
                    self.assertNotEqual(new_stop['source_result'], json.loads(stop_journal)['source_result'])
                    self.assertEqual(app.ledger.remaining('process_invocations'), 0)
                    self.assertEqual(len(self._process_receipts()), executions + 1)
                else:
                    self.assertIsNone(app.state['pending'])
                    self.assertEqual(len(app.state['turns']), stopped_turns)
                    self.assertIn(reason, app.session.manifest.status_reason)
                    self.assertEqual(self.send.call_count, executions)
                current_entries = {entry.reservation_id: entry.to_dict() for entry in app.ledger.entries}
                for reservation_id, original_entry in old_entries.items():
                    self.assertEqual(current_entries[reservation_id], original_entry)
                authorized_ledger, authorized_receipts = app.ledger.to_dict(), self._process_receipts()
                authorized_turns, calls = len(app.state['turns']), self.send.call_count
                for options in ({}, grant):  # Neither reload nor the same grant restarts work.
                    app = self._load()
                    status = self._run(app, **options)
                    self.assertEqual(status['status'], 'paused', status)
                    self.assertIsNone(app.state['pending'])
                    self.assertEqual(app.ledger.to_dict(), authorized_ledger)
                    self.assertEqual(self._process_receipts(), authorized_receipts)
                    self.assertEqual(len(app.state['turns']), authorized_turns)
                    self.assertEqual(self.send.call_count, calls)
                    self.assertEqual(stop_path.read_bytes(), stop_journal)
                    self.assertEqual(record.read_bytes(), raw)
                    self.assertEqual(app.state['requests'], requests)
                self.assertFalse(self.responses)
                self.coordinator.assert_not_called()

    def _assert_professional_conversation(self, app):
        messages = app._messages(client=app._client())
        self.assertFalse([message for message in messages if message['role'] == 'assistant'])
        self.assertFalse([message for message in messages if message['role'] == 'tool'])
        work = []
        for message in messages:
            if message['role'] == 'user':
                try:
                    body = json.loads(message['content'])
                except ValueError:
                    continue
                if 'professional_work' in body:
                    work.extend(body['operations'])
        self.assertEqual([row['tool'] for row in work],
            ['execute_implementation', 'prepare_implementation', 'execute_implementation',
                'prepare_implementation', 'execute_implementation'])
        self.assertEqual([row['result'] for row in work], self._observations(app)[2:])
        self.assertEqual(json.loads(messages[1]['content'])['resources']['model_turns_remaining'], 7)
        for turn in app.state['turns'][2:]:
            reply = app.session.store.read_json(turn['reply'])
            self.assertIn('source_result', reply)
            self.assertNotIn('reasoning', reply)
            self.assertNotIn('reasoning_content', reply)
        # Thinking-mode validation must not demand fabricated provider reasoning
        # for operations scheduled deterministically by a professional owner.
        thinking = LLMClient(replace(self.client._settings, thinking_mode='enabled'))
        projected = app._messages(client=thinking)
        self.assertFalse([message for message in projected if message['role'] == 'assistant'])

    def test_document_investigation_failure_stays_with_owner_without_reselection(self):
        app = self._create()
        material = app.workspace.workspace / 'saved.txt'
        material.write_text('A supplied recorded value, not an executable instruction.', encoding='utf-8')
        client = SimpleNamespace(ask_json=Mock(side_effect=ValueError('retained professional selection failure')))
        with patch.object(app.writing, 'client_factory', return_value=client):
            status = app.run(actions=[{'tool_name': 'compose_document', 'arguments': {
                'goal': 'Explain the saved material', 'material_roots': [str(material)]}}], progress=lambda _: None)
        self.assertEqual(status['status'], 'paused', status)
        source = self._observations(app)[0]
        self.assertEqual((source['tool_name'], source['status']), ('compose_document', 'error'))
        attempt = Path(app.state['turns'][0]['tools'][0]['result']).parent.name
        owner = self.root / 'tools/writing' / attempt
        self.assertTrue((owner / 'selection-input.json').is_file())
        self.assertFalse((owner / 'request.json').exists())
        stop = app.session.store.read_json(app.state['turns'][-1]['reply'])['owner_stop']
        self.assertEqual(stop['reason'], 'owner_failed')
        self.assertEqual(stop['owner'], 'compose_document')
        self.assertFalse(stop['completed'])
        original, ledger = (owner / 'selection-input.json').read_bytes(), app.ledger.to_dict()
        app = self._load()
        app.run(progress=lambda _: None)
        self.assertEqual((owner / 'selection-input.json').read_bytes(), original)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(client.ask_json.call_count, 1)
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def test_completed_semantic_gap_returns_to_request_owner_not_global_retry(self):
        app = self._create(professional_work=None)
        app.run(actions=[{'tool_name': 'write_file', 'arguments': {
            'path': 'workspace/retained.txt', 'content': 'unchanged diagnostic subject'}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        source = self.root / app.state['turns'][-1]['tools'][-1]['result']
        original, ledger = source.read_bytes(), app.ledger.to_dict()
        owner = SimpleNamespace(can_return_gap=Mock(return_value=True))
        # Domain proof itself is exercised with an actual retained negative
        # review in WritingCoordinationTests; this test owns scheduling only.
        action = {'stop': {'contract': 'professional_stop.v1', 'completed': False,
            'reason': 'work_gap', 'source_result': f'records/{source.parent.name}/native_result.json',
            'source_sha256': hashlib.sha256(original).hexdigest()}}
        with patch.object(app.gateway, 'professional_action', return_value=(owner, action)), \
                patch.object(app, '_pause_professional_stop') as pause:
            self.assertFalse(app._continue_professional_work())
            pause.assert_not_called()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(app.ledger.to_dict(), ledger)
            action['stop']['source_sha256'] = 'changed'
            with self.assertRaisesRegex(ValueError, 'accepted source'):
                app._continue_professional_work()
            action['stop']['source_sha256'] = hashlib.sha256(original).hexdigest()
            action['stop']['reason'] = 'owner_failed'
            self.assertFalse(app._continue_professional_work())
            for reason in ('shared_budget_exhausted', 'accounting_unresolved'):
                action['stop']['reason'] = reason
                self.assertTrue(app._continue_professional_work())
            self.assertEqual(pause.call_count, 2)
            action['stop']['reason'] = 'work_gap'
            owner.can_return_gap.return_value = False
            self.assertTrue(app._continue_professional_work())
            owner.can_return_gap.return_value = True
            app.state['config']['professional_work'] = 'owners.v3'
            self.assertTrue(app._continue_professional_work())
            self.assertEqual(pause.call_count, 4)
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def _paused_noncode_domain_gap(self, reason='work_gap'):
        from simple_ar.tools.professional_outcomes import stop_decision

        app = self._create(professional_work=None)
        app.run(actions=[{'tool_name': 'write_file', 'arguments': {
            'path': 'workspace/retained.txt', 'content': 'Retained negative inspection subject'}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        source = app.state['turns'][-1]['tools'][-1]['result']
        observed = app.session.store.read_json(source)
        owner = SimpleNamespace(root=self.root, session_id=app.session.manifest.session_id,
            can_return_gap=Mock(return_value=True), continuation=Mock(return_value=None),
            resume_after_budget=Mock(return_value=None), can_resume_after_budget=Mock(return_value=False))
        action = stop_decision(owner, observed, Path(source).parent.name,
            reason, 'Complete independent inspection retains required corrections')
        owner.stop_after_review = Mock(return_value=action)
        app._pause_professional_stop(source, action)
        return app, owner, source

    def test_paused_noncode_gap_resumes_to_parent_without_grant_or_owner_replay(self):
        app, owner, source = self._paused_noncode_domain_gap()
        journal = app.state['turns'][-1]['reply']
        original = {name: app.session.store.resolve(name).read_bytes() for name in (source, journal)}
        ledger_bytes = (self.root / 'budget_ledger.json').read_bytes()
        ledger, turns, requests = app.ledger.to_dict(), list(app.state['turns']), list(app.state['requests'])
        app = self._load()  # A real retained pause must pass run()'s resume gate.
        self.assertEqual(app.session.manifest.status, 'paused')
        self.assertEqual(app._professional_resource_authorizations(), [])
        # Only domain semantic eligibility is supplied by the fixture. Native
        # registration, stop validation, gateway fallback and resume are real.
        # Neither tool names nor a code/report domain select this behavior.
        spec = app.gateway.registry.get('write_file')
        with patch.object(spec, 'domain', 'independent_material_inspection'), \
                patch.object(app.gateway, '_domain_gateways', {'independent_material_inspection': owner}), \
                patch.object(app.gateway, 'professional_action', wraps=app.gateway.professional_action) as action, \
                patch.object(app, '_attempt', side_effect=RuntimeError('Parent coordination boundary')) as attempt:
            result = app.run(progress=lambda _: None)
        self.assertIn('Parent coordination boundary', result['status_reason'])
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(attempt.call_args.args[0], 'native_turn')
        self.assertEqual(app.state['pending']['kind'], 'model')
        action.assert_called_once_with(json.loads(original[source]), source, resume=True, consume_outcome=True)
        owner.continuation.assert_not_called()
        owner.resume_after_budget.assert_called_once_with(json.loads(original[source]), Path(source).parent.name)
        owner.stop_after_review.assert_called_once_with(json.loads(original[source]), Path(source).parent.name)
        owner.can_resume_after_budget.assert_not_called()
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual((self.root / 'budget_ledger.json').read_bytes(), ledger_bytes)
        self.assertEqual(app.state['turns'], turns)
        self.assertEqual(app.state['requests'], requests)
        for name, raw in original.items():
            self.assertEqual(app.session.store.resolve(name).read_bytes(), raw)
        self.assertFalse(json.loads(original[journal])['owner_stop']['completed'])
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def test_paused_gap_refuses_ineligible_modes_reasons_and_invalid_source_proof(self):
        app, owner, source = self._paused_noncode_domain_gap()
        journal = app.state['turns'][-1]['reply']
        original = app.session.store.resolve(journal).read_bytes()
        source_bytes = app.session.store.resolve(source).read_bytes()
        ledger_bytes = (self.root / 'budget_ledger.json').read_bytes()
        ledger, turns = app.ledger.to_dict(), list(app.state['turns'])
        cases = [('requests.v1', 'work_gap', False), ('requests.v1', 'owner_failed', False),
            *[(mode, 'work_gap', True) for mode in ('owners.v3', 'outcomes.v2')],
            *[('requests.v1', reason, True) for reason in ('permission_unavailable',
                'accounting_unresolved', 'owner_state_unresolved', 'shared_budget_exhausted')],
            ('requests.v1', 'invalid_source', True)]
        for mode, reason, eligible in cases:
            with self.subTest(mode=mode, reason=reason, eligible=eligible):
                app = self._load()
                app.state['config']['professional_work'] = mode
                retained = json.loads(original)
                retained['owner_stop']['reason'] = 'work_gap' if reason == 'invalid_source' else reason
                if reason == 'invalid_source':
                    retained['owner_stop']['source_sha256'] = 'changed'
                app.session.store.write_json(journal, retained, kind='professional_actions')
                owner.can_return_gap.reset_mock()
                owner.can_return_gap.return_value = eligible
                with patch.object(app.gateway, 'domain_owner', return_value=owner), \
                        patch.object(app.gateway, 'professional_action') as action, \
                        patch.object(app, '_attempt') as attempt:
                    status = app.run(progress=lambda _: None)
                self.assertEqual(status['status'], 'paused', status)
                self.assertIsNone(app.state['pending'])
                action.assert_not_called()
                attempt.assert_not_called()
                if reason == 'invalid_source':
                    self.assertIn('accepted source', status['status_reason'])
                if mode != 'requests.v1' or reason not in {'work_gap', 'owner_failed'}:
                    owner.can_return_gap.assert_not_called()
                self.assertEqual(app.ledger.to_dict(), ledger)
                self.assertEqual((self.root / 'budget_ledger.json').read_bytes(), ledger_bytes)
                self.assertEqual(app.state['turns'], turns)
                self.assertEqual(app.session.store.resolve(source).read_bytes(), source_bytes)
                app.session.store.resolve(journal).write_bytes(original)
        owner.resume_after_budget.assert_not_called()
        owner.can_resume_after_budget.assert_not_called()
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def test_legacy_owner_failed_returns_proven_work_without_rewriting_stop_or_replaying(self):
        app, owner, source = self._paused_noncode_domain_gap(reason='owner_failed')
        journal = app.state['turns'][-1]['reply']
        protected = {name: app.session.store.resolve(name).read_bytes() for name in (source, journal)}
        ledger = (self.root / 'budget_ledger.json').read_bytes()
        app = self._load()
        spec = app.gateway.registry.get('write_file')
        with patch.object(spec, 'domain', 'independent_material_inspection'), \
                patch.object(app.gateway, '_domain_gateways', {'independent_material_inspection': owner}), \
                patch.object(app, '_attempt', side_effect=RuntimeError('Request owner boundary')) as attempt:
            status = app.run(progress=lambda _: None)
        self.assertIn('Request owner boundary', status['status_reason'])
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(attempt.call_args.args[0], 'native_turn')
        self.assertEqual(json.loads(protected[journal])['owner_stop']['reason'], 'owner_failed')
        for name, raw in protected.items():
            self.assertEqual(app.session.store.resolve(name).read_bytes(), raw)
        self.assertEqual((self.root / 'budget_ledger.json').read_bytes(), ledger)
        owner.continuation.assert_not_called()
        owner.can_resume_after_budget.assert_not_called()
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def test_legacy_gap_resume_requires_new_grant_and_original_owner_proof(self):
        app = self._create(professional_work=None)
        app.run(actions=[{'tool_name': 'write_file', 'arguments': {
            'path': 'workspace/retained.txt', 'content': 'Unchanged retained subject'}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        source = app.state['turns'][-1]['tools'][-1]['result']
        path = app.session.store.resolve(source)
        original, ledger = path.read_bytes(), app.ledger.to_dict()
        proof = Mock(return_value=True)
        owner = SimpleNamespace(can_resume_after_budget=proof)
        stop = {'contract': 'professional_stop.v1', 'completed': False, 'reason': 'work_gap',
            'source_result': f'records/{path.parent.name}/native_result.json',
            'source_sha256': hashlib.sha256(original).hexdigest()}
        retained = {'owner_stop': stop, 'source_result': source, 'resource_authorizations': []}
        with patch.object(app, '_professional_resource_authorizations', return_value=[]) as grants, \
                patch.object(app.gateway, 'domain_owner', return_value=owner):
            self.assertFalse(app._professional_stop_can_resume(retained))
            proof.assert_not_called()
            grants.return_value = ['explicit-grant']
            self.assertTrue(app._professional_stop_can_resume(retained))
            proof.assert_called_once_with(json.loads(original), path.parent.name)
            proof.reset_mock()
            retained['resource_authorizations'] = ['explicit-grant']
            self.assertFalse(app._professional_stop_can_resume(retained))
            proof.assert_not_called()
            retained['resource_authorizations'] = []
            proof.return_value = False
            self.assertFalse(app._professional_stop_can_resume(retained))
            for reason in ('owner_failed', 'accounting_unresolved', 'repair_stagnated'):
                stop['reason'] = reason
                proof.reset_mock()
                self.assertFalse(app._professional_stop_can_resume(retained))
                proof.assert_not_called()
            stop['reason'], stop['source_sha256'] = 'work_gap', 'changed'
            with self.assertRaisesRegex(ValueError, 'accepted source'):
                app._professional_stop_can_resume(retained)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.coordinator.assert_not_called()
        self.send.assert_not_called()

    def test_completed_initial_article_resumes_check_and_delivery_without_coordinator(self):
        self._completed_author_resumes_check_and_delivery('article')

    def test_unstarted_section_inspection_preserves_author_and_original_stop(self):
        self._completed_author_resumes_check_and_delivery('sections')

    def test_inspection_only_negative_result_cannot_reopen_section_revision_rounds(self):
        self._completed_author_resumes_check_and_delivery('sections', negative=True)

    def _completed_author_resumes_check_and_delivery(self, method, *, negative=False):
        from simple_ar.tools.writing import freeze_report_requirement

        inputs = self.root.parent / 'saved-material'
        inputs.mkdir()
        source = inputs / 'record.txt'
        source.write_text('value=2.5\nscope=saved observation; no new experiment\n', encoding='utf-8')
        original = source.read_bytes()
        section_config = {}
        if method == 'sections':
            template = self.root.parent / 'saved-sections.md'
            template.write_text('# Saved observation\n\n## Result\nExplain the record.\n\n'
                '## Scope\nState what was run.\n', encoding='utf-8')
            criteria = template.with_name('saved-review.md')
            criteria.write_text('Check the result and execution scope against the saved record.\n', encoding='utf-8')
            section_config = {'template': str(template), 'criteria': str(criteria), 'outline_strategy': 'template',
                'max_review_iterations': 2}
        requirement = freeze_report_requirement({'outline_strategy': 'auto',
            'draft_scope': 'document', 'review_scope': 'document', 'reviewer': 'llm',
            'document_review': True, 'max_review_iterations': 0,
            'allow_source_backtracking': False, 'allow_llm_fallback': False,
            'figures': {'enabled': False}, **section_config}, method=method)
        app = ToolApplication.create(self.root, 'Explain the saved observation without new experiments.',
            model='fixture', sources={'inputs': inputs}, services=self.services,
            professional_inputs=True, allow_execution=False, report_requirement=requirement,
            max_turns=4, max_tokens=600000, max_attempts=40)
        if method == 'sections':
            app.state['config']['professional_work'] = 'requests.v1'
            app._save()
        body = 'The supplied record states value=2.5. This is a saved observation; no new experiment was run.'
        sections = [{'section_id': 'result', 'draft_markdown': body},
            {'section_id': 'scope', 'draft_markdown': 'No new experiment was run.'}]
        replies = [
            {'read_windows': [{'path': str(source)}]},
            {'title': 'Saved observation', 'materials': [str(source)], 'delivery_artifacts': []},
            {'sections': sections} if method == 'sections' else {'draft_markdown': body},
        ]

        def provider(mode, request, **kwargs):
            self.assertFalse(request.get('tools'), 'Candidate continuation must not invoke coordination')
            self.assertTrue(replies, 'Unexpected additional provider operation')
            return {'choices': [{'message': {'role': 'assistant', 'content': json.dumps(replies.pop(0))},
                'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

        self.send.side_effect = provider
        native_ask = LLMClient.ask_json
        refused = False

        def first_check_unavailable(client, *args, **options):
            nonlocal refused
            if options.get('label') == ('report-document-reviewer' if method == 'sections'
                    else 'article-independent-review') and not refused:
                from simple_ar.core.budget import BudgetExceededError
                refused = True
                # Inject only the pre-provider refusal. The real transport,
                # author receipts, caller grant and subsequent review remain
                # in the normal task/ledger path; no fake accepted review.
                if method == 'sections':
                    from simple_ar.integrations.llm import LLMError
                    raise LLMError('Budget refused before provider attempt 1') from BudgetExceededError('Offline capacity')
                raise BudgetExceededError('Offline pre-provider capacity refusal')
            return native_ask(client, *args, **options)

        self.enterContext(patch.object(LLMClient, 'ask_json', first_check_unavailable))
        with patch.object(app.writing, '_unstarted_inspection_action', return_value=None):
            stopped = app.run(actions=[{'tool_name': 'compose_document', 'arguments': {
                'goal': app.state['requests'][0], 'material_roots': ['inputs'], 'finish': True}}],
                progress=lambda _: None)
        self.assertEqual(stopped['status'], 'paused', stopped)
        self.assertEqual(self.send.call_count, 3, self._observations(app))
        self.assertFalse(replies)
        stop = app.session.store.read_json(app.state['turns'][-1]['reply'])
        self.assertEqual(stop['owner_stop']['reason'],
            'owner_failed' if method == 'sections' else 'shared_budget_exhausted')
        observation = app.session.store.read_json(stop['source_result'])
        self.assertEqual(observation['data']['failure_kind'],
            'composition_incomplete' if method == 'sections' else 'resource_exhausted')
        identity = Path(stop['source_result']).parent.name
        owner = self.root / 'tools/writing' / identity
        protected = {p: p.read_bytes() for p in owner.rglob('*') if p.is_file()}
        protected[self.root / stop['source_result']] = (self.root / stop['source_result']).read_bytes()
        old_ledger = app.ledger.to_dict()
        app = self._load()
        if method == 'article':
            app.run(progress=lambda _: None)
            self.assertEqual(app.ledger.to_dict(), old_ledger)
            self.assertEqual(self.send.call_count, 3)
        else:
            # Unknown/reserved or an already-attempted physical check cannot
            # become new work merely because its response file is absent.
            gateway = app.writing
            for state in ('reserved', 'unknown', 'settled'):
                with self.subTest(state=state):
                    before = gateway.ledger.to_dict()
                    pending = next(p for p in (owner / 'writer/model_calls').glob('*/request.json')
                        if json.loads(p.read_bytes())['options']['label'] == 'report-document-reviewer')
                    gateway.ledger.reserve('pending-check', {'llm_requests': 1}, session_id=gateway.session_id,
                        attempt_id=identity, logical_call_id=f'{identity}:writing:{pending.parent.name}')
                    if state == 'unknown':
                        gateway.ledger.mark_unknown('pending-check', reason='Fixture lost response')
                    elif state == 'settled':
                        gateway.ledger.settle('pending-check', {'llm_requests': 1}, actual_source='provider')
                    self.assertIsNone(gateway.continuation(observation, identity))
                    gateway.ledger._entries.pop('pending-check')
                    gateway.ledger.save()
                    self.assertEqual(gateway.ledger.to_dict(), before)
            from simple_ar.report.writing import section_author_source
            from simple_ar.core.capabilities import ArtifactStore
            with self.assertRaisesRegex(ValueError, 'incomplete model operation'):
                section_author_source(ArtifactStore(owner))  # Historical v1 stays strict.
            _, authored = section_author_source(ArtifactStore(owner), author_only=True)
            gateway._unstarted_section_inspection(ArtifactStore(owner), authored)
            self.assertIsNotNone(gateway.continuation(observation, identity))
            pending = next(p for p in (owner / 'writer/model_calls').glob('*/request.json')
                if json.loads(p.read_bytes())['options']['label'] == 'report-document-reviewer')
            response = {'section_reviews': [{'section_id': sid, 'verdict': 'pass', 'findings': []}
                for sid in ('result', 'scope')]}
            receipt = {'response': response, **{name+'_sha256': hashlib.sha256(json.dumps(value,
                sort_keys=True, ensure_ascii=False).encode()).hexdigest() for name, value in (
                    ('request', json.loads(pending.read_bytes())), ('response', response))}}
            pending.with_name('response.json').write_text(json.dumps(receipt), encoding='utf-8')
            try:
                self.assertIsNone(gateway.continuation(observation, identity))
                self.assertFalse(gateway.can_resume_after_budget(observation, identity))
            finally:
                pending.with_name('response.json').unlink()
        reviews = [{'section_id': sid, 'verdict': 'pass', 'findings': []}
            for sid in (['result', 'scope'] if method == 'sections' else ['document'])]
        if negative:
            reviews[0].update(verdict='revise_required', findings=[{'finding_id': 'style-follow-up',
                'type': 'style', 'severity': 'minor', 'required_action': 'revise',
                'section_id': 'result', 'message': 'Explain this observation more clearly.',
                'draft_quotes': [{'section_id': 'result', 'quote': 'The supplied record states value=2.5.'}]}])
        replies.append({'section_reviews': reviews})
        final = (app.run(progress=lambda _: None,
            should_stop=lambda: negative and self.send.call_count >= 4) if method == 'sections' else
            app.run(resource_allowances={'llm_requests': 1, 'total_tokens': 200000},
                authorization_id='offline-candidate-inspection',
                authorization_reason='One offline check of completed work; preserve failed review and usage.',
                progress=lambda _: None))
        self.assertEqual(final['status'], 'paused' if negative else 'completed', final)
        self.assertEqual(self.send.call_count, 4)
        self.assertFalse(replies)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual({p: p.read_bytes() for p in protected}, protected)
        self.assertEqual(app.ledger.to_dict()['entries'][:len(old_ledger['entries'])], old_ledger['entries'])
        result = next(row for row in reversed(self._observations(app)) if row['tool_name'] == 'compose_document')
        current = self.root / 'tools/writing' / Path(result['data']['version']).name
        self.assertEqual(json.loads((current / 'request.json').read_bytes())['continued_source_review'], 'inspect_only.v1')
        written = json.loads((current / 'writer/writer.json').read_bytes())
        self.assertEqual(written['sections'][0]['draft_markdown'], body)
        if method == 'sections':
            self.assertEqual(json.loads((current / 'writer/report_inputs.json').read_bytes())['config']['max_review_iterations'], 0)
            self.assertFalse(any(row['action'] in {'draft', 'document_joint_revise'} for row in written['iterations']))
            if negative:
                self.assertEqual(result['data']['editable_markdown'], result['data']['version']+'/candidate.md')
                self.assertEqual(written['independent_review']['review_status'], 'needs_revision')
                # Prior prose/delivery copies are context, not a new evidence
                # focus. Adding that exact directory must not trigger selection.
                from simple_ar.tools.writing import ComposeRequest
                intent = {'goal': 'Apply the current editorial findings without changing the saved result.',
                    'prior_draft': result['data']['editable_markdown'],
                    'material_roots': ['inputs', result['data']['version']]}
                request = ComposeRequest.model_validate(intent)
                report = app.writing.report_inputs()
                admission = app.writing._admit_document_source(ArtifactStore(self.root/'tools/writing/feedback-proof'),
                    intent, None, None, report)
                for roots in (intent['material_roots'], [result['data']['version']]):
                    resolved = app.writing._resolve_document_inputs(request.model_copy(update={'material_roots': roots}),
                        'feedback-proof', report=report, original=None, selection=None, document_work=True,
                        caller_fields=frozenset(intent), work_source={**admission, 'intent':{**intent,'material_roots':roots}},
                        normalize_arguments=None)
                    self.assertIsNone(resolved.investigation)
                    self.assertIsNotNone(resolved.binding)
                    self.assertEqual(len(resolved.recipe.materials), 1)
        self.assertEqual(self._process_receipts(), {})
        self.coordinator.assert_not_called()

    def test_native_source_only_preparation_respects_execution_permission(self):
        app = self._create(allow_execution=False)
        self.responses = [{'summary': 'Source only; no execution authorized',
            'unresolved': list(_PRODUCER_DECLARATIONS),
            'files': [{'path': 'calculate.py', 'content': 'print(sum(range(7)))\n'}]}]
        action = {'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Compute and save sum(range(7))', 'complete_work': False}}
        status = app.run(actions=[action], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)
                and self._observations(app)[-1]['tool_name'] == 'prepare_implementation'))
        self.assertEqual(status['status'], 'paused', status)
        self.assertEqual([(row['tool_name'], row['status']) for row in self._observations(app)],
            [('prepare_implementation', 'ok')])
        self.assertFalse(app._continue_professional_work())
        self.assertEqual([row.get('origin') for row in app.state['turns']], ['caller'])
        self.assertIsNone(app.state['pending'])
        self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)
        prepared = self._observations(app)[0]
        code = str(Path(prepared['data']['project_dir']) / 'calculate.py')
        claimed_report = {'summary': 'A report label is not independent review',
            'artifacts': [code], 'report': {'status': 'canonical', 'manuscript': code}}
        self.assertEqual(app.writing.delivery_adoptions(self._observations(app), claimed_report), [])
        self.assertTrue(app.implementation.delivery_issues_for(
            self._observations(app), claimed_report, []), 'Unreviewed code-only declarations cannot be adopted')
        # An unrelated later preparation B must not hide delivered A. Both
        # records come through the public owner; neither is an injected pass.
        original = json.loads(json.dumps(prepared))
        self.responses = [{'summary': 'Independent source-only B', 'unresolved': [],
            'files': [{'path': 'independent.py', 'content': "print('independent B')\n"}]}]
        count = len(self._observations(app))
        status = app.run(actions=[{'tool_name': 'prepare_implementation', 'arguments': {
            'goal': 'Prepare an independent greeting script', 'complete_work': False}}],
            progress=lambda _: None, should_stop=lambda: len(self._observations(app)) > count)
        self.assertEqual(status['status'], 'paused', status)
        unrelated = self._observations(app)[-1]
        self.assertEqual(unrelated['status'], 'ok', unrelated)
        self.assertEqual(unrelated['data']['unresolved'], [])
        other_code = str(Path(unrelated['data']['project_dir']) / 'independent.py')
        mixed = {'summary': 'Both actual source candidates', 'artifacts': [code, other_code]}
        only_b = {'summary': 'Only independent B is delivered', 'artifacts': [other_code]}
        self.assertTrue(app.implementation.delivery_issues_for(self._observations(app), mixed, []),
            'Later clean B must not hide delivered A declarations')
        self.assertFalse(app.implementation.delivery_issues_for(self._observations(app), only_b, []),
            'An omitted historical A must not become a permanent unrelated obligation')
        # The app may retire A from its observation projection. Ownership must
        # still come from A's unchanged registered preparation, not that list.
        source = app.workspace.resolve(prepared['data']['prepared_result'])
        registered = app.implementation._registered_ref(source,
            kind='prepared_implementation', store_root=source.parent.parent)
        retained = {path: path.read_bytes() for path in (
            source, source.parent / 'request.json', source.parent.parent / 'native_result.json')}
        ledger = app.ledger.to_dict()
        app = self._load()
        filtered = [row for row in self._observations(app)
            if row.get('data', {}).get('prepared_result') != prepared['data']['prepared_result']]
        self.assertEqual(filtered, [unrelated])
        old_a = {'summary': 'Deliver the retired but still registered A source', 'artifacts': [code]}
        for proposal in (old_a, mixed):
            with self.subTest(retired_a_delivery=proposal['summary']):
                self.assertTrue(app.implementation.delivery_issues_for(filtered, proposal, []),
                    'Filtering A observations must not retire its delivered-source declarations')
        self.assertFalse(app.implementation.delivery_issues_for(filtered, only_b, []))
        self.assertEqual(app.implementation._registered_ref(source,
            kind='prepared_implementation', store_root=source.parent.parent), registered)
        self.assertEqual({path: path.read_bytes() for path in retained}, retained)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self._observations(app)[0], original)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self._process_receipts(), {})
        self.coordinator.assert_not_called()

    def test_python_name_admission_rejects_unusedpd_without_executing_candidate(self):
        from simple_ar.code_task.generation.dependencies import build_dependency_advice
        from simple_ar.code_task.generation.selection import ImplementationSources
        from simple_ar.code_task.generation.validation import check_candidate_sources
        from simple_ar.code_task.generation.writer import write_implementation_unit

        sentinel = self.root.parent / 'candidate-must-not-execute.txt'
        preamble = ("from pathlib import Path\nPath(" + repr(str(sentinel))
            + ").write_text('executed', encoding='utf-8')\n")
        broken = preamble + 'import pandas as pd\nunusedpd.DataFrame([1])\n'
        report = check_candidate_sources({'calculate.py': broken})
        self.assertEqual(report['checker']['name'], 'pyflakes')
        self.assertTrue(report['checker']['version'])
        self.assertEqual((report['status'], report['kind'], report['filename']),
            ('rejected', 'python_names_failed', 'calculate.py'))
        issue, = report['issues']
        self.assertEqual(issue['rule'], 'UndefinedName')
        self.assertEqual((issue['path'], issue['line']), ('calculate.py', 4))
        self.assertIn('unusedpd', issue['message'])
        self.assertFalse(issue['baseline'])
        self.assertEqual(issue['file_sha256'], hashlib.sha256(broken.encode('utf-8')).hexdigest())
        # Source imports are never executed. Fixing the binding may pass static
        # admission without proving pandas is importable in the target Python.
        fixed = broken.replace('unusedpd.DataFrame', 'pd.DataFrame')
        admitted = check_candidate_sources({'calculate.py': fixed})
        self.assertEqual((admitted['status'], admitted['issues']), ('accepted', []))
        # Preserved originals keep explicit baseline diagnostics, but a changed
        # file cannot inherit that exemption merely because its error is alike.
        baseline = check_candidate_sources({'calculate.py': broken}, originals={'calculate.py': broken})
        self.assertEqual(baseline['status'], 'accepted')
        self.assertTrue(baseline['issues'][0]['baseline'])
        changed = check_candidate_sources({'calculate.py': broken + '# modified\n'},
            originals={'calculate.py': broken})
        self.assertEqual(changed['status'], 'rejected')
        self.assertFalse(changed['issues'][0]['baseline'])
        app = self._create()
        identity = 'candidate-admission'
        candidate = app.workspace.workspace / 'implementations' / identity
        artifacts = self.root / 'attempts' / identity / 'implementation'
        # A foreign interpreter is deliberately not inspected or executed. Its
        # unknown dependency inventory must survive into the actual writer.
        target_python = str(self.root.parent / 'other-environment' / 'python')
        advice = build_dependency_advice('Use pandas', python_executable=target_python)
        sources = ImplementationSources(workspace=app.workspace, project=None,
            roots=[], windows=[], originals={}, identity=identity)
        self.responses = [self._proposal(broken), self._proposal(fixed)]
        provider = self.send.side_effect

        def before_publication(*args, **kwargs):
            self.assertFalse(candidate.exists(), 'Rejected source was published before correction')
            self.assertFalse(sentinel.exists())
            self.assertEqual(self._process_receipts(), {})
            return provider(*args, **kwargs)

        self.send.side_effect = before_publication
        result = write_implementation_unit(project_dir=candidate, goal='Use pandas',
            files=None, source_windows=sources.windows, original_files=sources.originals,
            client=app.implementation._client(identity), artifact_dir=artifacts,
            task_python=target_python, execution_requested=True,
            validate_destination=lambda path: app.workspace.resolve(str(candidate / path)),
            investigation=sources, candidate_admission=True, dependency_advice=advice)
        self.assertEqual(self.send.call_count, 2)
        self.assertEqual(self.prompts[0]['validation_observations'], [])
        diagnosis, = self.prompts[1]['validation_observations']
        saved_rejection = json.loads((artifacts / 'decision-steps/1/validation.json').read_bytes())
        self.assertEqual(diagnosis, saved_rejection)
        self.assertEqual(diagnosis['status'], 'rejected')
        self.assertEqual(diagnosis['issues'], report['issues'])
        self.assertEqual(json.loads((artifacts / 'decision-steps/1/response.json').read_bytes()),
            self._proposal(broken))
        for prompt in self.prompts:
            dependency = prompt['dependency_observation']
            self.assertEqual(dependency['target_python_executable'], target_python)
            self.assertEqual(dependency['environment_scope'], 'target_not_inspected')
            self.assertFalse(dependency['target_matches_inspecting_interpreter'])
            self.assertIsNone(dependency['environment_package_count'])
            self.assertIsNone(dependency['observed_distributions'])
        self.assertEqual(json.loads((artifacts / 'dependency_observation.json').read_bytes()), advice)
        admission = result['candidate_admission']
        raw = Path(admission['report']).read_bytes()
        self.assertEqual(admission['sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual((admission['status'], json.loads(raw)['issues']), ('accepted', []))
        self.assertEqual((candidate / 'calculate.py').read_bytes(), fixed.encode('utf-8'))
        self.assertFalse((candidate / 'result.txt').exists())
        self.assertFalse(sentinel.exists())
        self.assertEqual(self._process_receipts(), {})
        entries = [entry for entry in app.ledger.entries if entry.actual.get('llm_requests')]
        self.assertEqual(len(entries), 2)
        self.assertEqual(sum(entry.actual['total_tokens'] for entry in entries), 240)
        self.assertTrue(all(entry.status == 'settled' for entry in entries))
        self.coordinator.assert_not_called()

    def test_request_owner_surface_keeps_raw_caller_and_legacy_access(self):
        app = self._create(professional_work=None)
        self.assertEqual(app.state['config']['professional_work'], 'requests.v1')
        schemas = {row['function']['name']: row['function'] for row in app.schemas}
        self.assertIsNone(app.gateway.registry.get('focus_work'))
        self.assertNotIn('focus_work', schemas)
        self.assertNotIn('focus_work', app._specialist_catalog())
        self.assertTrue({'prepare_implementation', 'compose_document'} <= schemas.keys(),
            'Native professional entry contracts must be visible by default')
        self.assertTrue({'read_file', 'list_directory'} <= schemas.keys(),
            'Observing professional text must not require generating another program')
        properties = schemas['prepare_implementation']['parameters']['properties']
        self.assertFalse({'work_contract', 'candidate_admission'} & properties.keys(),
            'Internal saved owner contracts must not become provider-selected permissions')
        raw = {'write_file', 'edit_file', 'run_process'}
        self.assertFalse(raw & schemas.keys())
        self.assertFalse(raw & app._specialist_catalog().keys())
        app.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': sorted(raw)}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        app = self._load()
        self.assertFalse(raw & {row['function']['name'] for row in app.schemas},
            'Caller selection does not expand the ordinary consumer API')
        count = len(self._observations(app))
        app.run(actions=[{'tool_name': 'write_file', 'arguments': {
            'path': 'workspace/raw.py', 'content': 'print(sum(range(7)))\n'}},
            {'tool_name': 'run_process', 'arguments': {'argv': ['python', 'raw.py'], 'timeout_sec': 3}}],
            progress=lambda _: None)
        self.assertEqual(self._observations(app)[-1]['status'], 'ok')
        pending = app.state['pending']
        self.assertIsNotNone(pending, app.result_snapshot())
        self.assertEqual(pending['call']['function']['name'], 'run_process')
        app.run(approved_execution=app.approval_token(pending['call']), progress=lambda _: None,
            should_stop=lambda: len(self._observations(app)) == count + 2)
        observed = self._observations(app)[-1]
        self.assertEqual((observed['tool_name'], observed['status']), ('run_process', 'ok'), observed)
        self.assertEqual(observed['data']['stdout'].strip(), '21')
        self.assertEqual(len(self._process_receipts()), 1)
        self.assertFalse([row for row in self._observations(app) if row['tool_name'] == 'prepare_implementation'])
        count = len(self._observations(app))
        app.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': ['compose_document']}}],
            progress=lambda _: None, should_stop=lambda: len(self._observations(app)) == count + 1)
        advanced = next(row['function'] for row in app.schemas
            if row['function']['name'] == 'compose_document')
        self.assertNotIn('title', advanced['parameters']['properties'])
        self.assertFalse({'materials', 'figures', 'sources', 'delivery_artifacts'}
            & advanced['parameters']['properties'].keys())
        self.assertIn('finish', advanced['parameters']['properties'])
        self.assertEqual(advanced['description'], schemas['compose_document']['description'],
            'Advanced arguments must not revert the professional owner to a partial-work contract')
        self.send.assert_not_called()
        self.coordinator.assert_not_called()

        self.root = self.root.parent / 'frozen-legacy-raw'
        app = self._create()  # Deliberately selected owners.v3 remains unchanged.
        self.assertTrue(raw <= app._specialist_catalog().keys())
        app.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': sorted(raw)}}],
            progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        self.assertTrue(raw <= {row['function']['name'] for row in self._load().schemas})

        # Native providers can invent names from the discovery directory even
        # when the actual function contract was not supplied. The action owner
        # must not dispatch these as though on-demand loading had occurred.
        self.root = self.root.parent / 'offered-contract-task'
        app = self._create()
        call = {'id': 'invented-write', 'type': 'function', 'function': {
            'name': 'write_file', 'arguments': json.dumps({
                'path': 'workspace/unoffered.txt', 'content': 'must not execute'})}}

        def invented_reply(messages, *, tools, **kwargs):
            self.assertNotIn('write_file', {row['function']['name'] for row in tools})
            return {'role': 'assistant', 'content': '', 'tool_calls': [call]}

        self.coordinator.side_effect = invented_reply
        status = app.run(progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(status['status'], 'paused', status)
        observed, = self._observations(app)
        self.assertEqual(observed['status'], 'blocked', observed)
        self.assertIn('not supplied', observed['error'])
        self.assertFalse(observed['data']['executed'])
        self.assertFalse((app.workspace.workspace / 'unoffered.txt').exists())
        binding_path = str(Path(app.state['turns'][0]['reply']).with_name('decision_budget.json'))
        binding = app.session.store.read_json(binding_path)
        self.assertEqual(binding['tool_admission'], 'offered.v1')
        app = self._load()
        self.assertIn('not supplied', app._offered_tool_error(call))
        # Old accepted replies never carried this new admission promise.
        binding.pop('tool_admission')
        app.session.store.write_json(binding_path, binding)
        self.assertEqual(app._offered_tool_error(call), '')

        # A new decision in an old no-reservation configuration still seals its
        # offer. Interrupt AFTER the refusal bytes but BEFORE journal commit:
        # recovery must adopt the refusal, never call the execution owner.
        self.root = self.root.parent / 'offered-execution-recovery'
        app = self._create()
        app.state['config'].pop('completion_policy')
        app._save()
        app = self._load()
        call = {'id': 'invented-execution', 'type': 'function', 'function': {
            'name': 'execute_implementation', 'arguments': '{}'}}

        def invented_execution(messages, *, tools, **kwargs):
            self.assertNotIn('execute_implementation', {row['function']['name'] for row in tools})
            return {'role': 'assistant', 'content': '', 'tool_calls': [call]}

        self.coordinator.side_effect = invented_execution
        app.run(progress=lambda _: None, should_stop=lambda: bool(app.state['turns']))
        binding_path = str(Path(app.state['turns'][0]['reply']).with_name('decision_budget.json'))
        self.assertEqual(app.session.store.read_json(binding_path)['tool_admission'], 'offered.v1')
        with _interrupt_outcome_commit():
            app.run(progress=lambda _: None)
        self.assertEqual(app.state['pending']['kind'], 'tool')
        refusal_path = self.root / 'attempts' / app.state['pending']['attempt'] / 'native_result.json'
        refusal_bytes, before_ledger = refusal_path.read_bytes(), app.ledger.to_dict()
        app = self._load()
        before_calls = self.coordinator.call_count
        with patch.object(app.implementation, 'execute_tool',
                side_effect=AssertionError('Admission refusal must not enter execution recovery')):
            app.run(progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(self._observations(app)[0]['status'], 'blocked')
        self.assertIsNone(app.state['pending'])
        self.assertEqual(refusal_path.read_bytes(), refusal_bytes)
        self.assertEqual(app.ledger.to_dict(), before_ledger)
        self.assertEqual(self.coordinator.call_count, before_calls)

    def test_request_owner_load_and_same_decision_raw_inventions_are_refused(self):
        app = self._create(professional_work=None, tool_contracts='full')
        self.assertTrue({'read_file', 'list_directory'} <= {row['function']['name'] for row in app.schemas})
        self.assertFalse({'copy_file', 'run_process', 'execute_implementation'}
            & {row['function']['name'] for row in app.schemas})
        # The registry and permission policy still belong to experts/callers.
        for name in ('read_file', 'copy_file', 'write_file', 'run_process', 'execute_implementation'):
            self.assertTrue(app.gateway.policy.is_allowed(app.gateway.registry.get(name))[0])

        self.root = self.root.parent / 'request-owner-offer'
        app = self._create(professional_work=None)
        (app.workspace.workspace / 'expert-note.txt').write_text('Recorded professional observation.', encoding='utf-8')
        calls = [{'id': name, 'type': 'function', 'function': {
            'name': name, 'arguments': json.dumps(arguments)}} for name, arguments in (
                ('read_file', {'path': 'workspace/expert-note.txt'}),
                ('load_tools', {'names': ['read_file', 'run_process']}),
                ('write_file', {'path': 'workspace/invented.txt', 'content': 'forbidden consumer'}),
                ('prepare_implementation', {'goal': 'Save a table', 'files': [
                    {'path': 'answer.py', 'purpose': 'Coordinator prewrites the expert recipe'}]}),
                ('load_tools', {'names': ['inspect_table']}),
                ('inspect_table', {'path': 'workspace/expert-note.txt'}))]
        for index, call in enumerate(calls):
            call['id'] += '-' + str(index)
        self.coordinator.side_effect = lambda *args, **kwargs: {
            'role': 'assistant', 'content': '', 'tool_calls': calls}
        app.run(progress=lambda _: None,
            should_stop=lambda: len(self._observations(app)) == len(calls))
        rows = self._observations(app)
        self.assertEqual([row['status'] for row in rows], ['ok', 'blocked', 'blocked', 'blocked', 'ok', 'blocked'])
        self.assertEqual(rows[0]['data']['text'], 'Recorded professional observation.')
        self.assertIn('expert-owned', rows[1]['error'])
        self.assertIn('not supplied', rows[2]['error'])
        self.assertIn('not an expert input recipe', rows[3]['error'])
        self.assertIn('not supplied', rows[5]['error'], 'Loading cannot change the original decision offer')
        self.assertFalse((app.workspace.workspace / 'invented.txt').exists())
        self.assertEqual(app.state['turns'][0]['origin'], 'request_owner')
        self.assertIn('review_document', app._specialist_catalog())
        self.assertFalse({'write_file', 'run_process'} & app._specialist_catalog().keys())
        self.assertIn('inspect_table', {row['function']['name'] for row in self._load().schemas})
        self.send.assert_not_called()

    def test_request_owner_composes_available_capabilities_from_actual_outcomes(self):
        # One native loop, real expert generation/execution and native ledger;
        # only the existing physical provider transport is replaced.
        self.enterContext(patch.object(LLMClient, 'chat_turn', self.native_chat_turn))
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.root = self.root.parent / ('missing-products' if missing else 'existing-products')
                app = self._create(professional_work=None)
                table = app.workspace.workspace / 'ready.csv'
                if not missing:
                    table.write_text('value\n21\n', encoding='utf-8')
                native_names = []

                def provider(mode, request, **kwargs):
                    if not request.get('tools'):
                        return self._provider(mode, request, **kwargs)
                    self.assertIn('Own the user\'s complete professional work request',
                        request['messages'][0]['content'])
                    schemas = {row['function']['name']: row['function'] for row in request['tools']}
                    catalog = json.loads(request['messages'][1]['content'])['available_specialists']
                    self.assertTrue({'prepare_implementation', 'compose_document', 'load_tools'} <= schemas.keys())
                    self.assertTrue({'research', 'find_source_passages', 'measure_experiment',
                        'analyze_measurements', 'inspect_table', 'export_report'} <= catalog.keys())
                    self.assertTrue({'read_file', 'list_directory'} <= schemas.keys())
                    rows = self._observations(app)
                    if len(app.state['requests']) > 1:
                        name, arguments = 'ask_user', {'question': 'Which additional comparison is required?'}
                    elif missing and not any(row['tool_name'] == 'execute_implementation' for row in rows):
                        name, arguments = 'prepare_implementation', {
                            'goal': 'Create the missing value table and SVG figure for the recorded value 21.'}
                        contract = schemas[name]['parameters']
                        self.assertTrue(contract['properties']['complete_work']['default'])
                        self.assertNotIn('files', contract['properties'])
                    elif 'inspect_table' not in schemas:
                        name, arguments = 'load_tools', {'names': ['inspect_table']}
                    else:
                        name = 'inspect_table'
                        path = str(table) if not missing else str(Path(next(row for row in rows
                            if row['tool_name'] == 'execute_implementation')['data']['cwd']) / 'table.csv')
                        arguments = {'path': path}
                    native_names.append(name)
                    return {'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': [{
                        'id': 'request-' + str(len(native_names)), 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(arguments)}}]},
                        'finish_reason': 'tool_calls'}], 'usage': {
                            'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

                code = ("from pathlib import Path\n"
                    "Path('table.csv').write_text('value\\n21\\n', encoding='utf-8')\n"
                    "Path('main.svg').write_text('<svg xmlns=\"http://www.w3.org/2000/svg\"></svg>', encoding='utf-8')\n")
                proposal = self._proposal(code)
                proposal['execution_plan']['outputs'] = ['table.csv', 'main.svg']
                self.responses = [proposal] if missing else []
                self.send.side_effect = provider
                baseline_calls = self.send.call_count
                app.run(progress=lambda _: None,
                    should_stop=lambda: any(row['tool_name'] == 'inspect_table' for row in self._observations(app)))
                if missing:
                    pending, token = self._pending_execution(app)
                    self.assertEqual(app.state['turns'][0]['origin'], 'request_owner')
                    prepared = self._observations(app)[0]['data']['prepared_result']
                    binding = json.loads((app.workspace.resolve(prepared).parent / 'request.json').read_bytes())
                    self.assertTrue(binding['request']['complete_work'])
                    self.assertEqual(binding['request']['work_contract'], 'shared.v2')
                    app = self._load()
                    app.run(approved_execution=token, progress=lambda _: None,
                        should_stop=lambda: any(row['tool_name'] == 'inspect_table' for row in self._observations(app)))
                self.assertEqual(native_names, ['prepare_implementation', 'load_tools', 'inspect_table']
                    if missing else ['load_tools', 'inspect_table'])
                self.assertEqual(self._observations(app)[-1]['status'], 'ok')
                before = app.ledger.to_dict()
                app = self._load()
                self.assertEqual(app.ledger.to_dict(), before)
                app.run(feedback='Add the comparison requested next; reuse the existing products.',
                    progress=lambda _: None)
                self.assertEqual(app.state['turns'][-1]['origin'], 'request_owner')
                self.assertEqual(len(app.state['requests']), 2)
                self.assertEqual(sum(entry.actual.get('llm_requests', 0) for entry in app.ledger.entries),
                    self.send.call_count - baseline_calls)
                self.assertTrue(all(entry.status == 'settled' for entry in app.ledger.entries))

    def test_request_owner_lazy_offer_cost_history_and_replaceable_selection(self):
        from simple_ar.integrations.llm import estimate_tokens
        from tests.test_tool_agent_handoff import ConversationContextTests
        app = self._create(professional_work=None)
        core = {row['function']['name'] for row in app.schemas}
        optional = {'review_document', 'export_report', 'measure_experiment', 'analyze_measurements', 'inspect_table'}
        self.assertTrue({'prepare_implementation', 'compose_document', 'read_file', 'list_directory',
            'load_tools', 'read_history', 'update_work_plan', 'deliver_result', 'ask_user'} <= core)
        self.assertFalse(optional & core)
        self.assertTrue(optional <= app._specialist_catalog().keys())
        self.assertTrue(all('description' in app._specialist_catalog()[name] for name in optional))

        def select(names):
            count = len(self._observations(app))
            app.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': names}}],
                progress=lambda _: None, should_stop=lambda: len(self._observations(app)) > count)

        select(['review_document'])
        self.assertIn('review_document', {row['function']['name'] for row in self._load().schemas})
        self.assertNotIn('description', app._specialist_catalog()['review_document'])
        select(['inspect_table'])
        self.assertNotIn('review_document', {row['function']['name'] for row in app.schemas})
        self.assertIn('inspect_table', {row['function']['name'] for row in app.schemas})
        select([])
        self.assertEqual({row['function']['name'] for row in app.schemas}, core)

        # Same recorded observations, client target and projection algorithm;
        # the counterfactual changes only which consumer schemas are offered.
        fixture = ConversationContextTests()
        for number in range(3):
            path = app.workspace.workspace / f'observation-{number}.txt'
            path.write_text((f'Original observation {number}. ' * 720), encoding='utf-8')
            arguments = {'path': str(path), 'max_bytes': 64000, 'full_digest': True}
            read = app.workspace.call('read_file', arguments, identity=f'observation-{number}')
            self.assertEqual(read.status, 'ok', read)
            app.state['turns'].append(fixture._record_native_turn(app, 'read_file', arguments,
                read.data, f'observation-{number}'))
        client = LLMClient(replace(self.client._settings, context_input_tokens=24000))
        ledger = app.ledger.to_dict()
        records = {path: path.read_bytes() for path in app.session.store.root.rglob('*') if path.is_file()}
        lazy_schemas = app.schemas
        lazy = json.loads(app._messages(client=client)[1]['content'])
        all_consumers = {spec.name for spec in app.gateway.list_specs()
                if app.gateway.policy.is_allowed(spec)[0] and app.gateway.registry.request_visible(spec.name)}
        with patch.object(app, '_contract_presentation', return_value=(all_consumers, {})):
            expanded_schemas = app.schemas
            expanded = json.loads(app._messages(client=client)[1]['content'])
        self.assertLess(estimate_tokens(json.dumps(lazy_schemas)), estimate_tokens(json.dumps(expanded_schemas)))
        self.assertLess(lazy['retained_turn_start'], expanded['retained_turn_start'])
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual({path: path.read_bytes() for path in records}, records)
        self.send.assert_not_called()
        self.coordinator.assert_not_called()

        # Explicit full retains all eligible consumers without exposing expert
        # file mutation/execution or changing the default task's saved mode.
        self.root = self.root.parent / 'explicit-full-consumer'
        full = self._create(professional_work=None, tool_contracts='full')
        names = {row['function']['name'] for row in full.schemas}
        self.assertTrue(optional <= names)
        self.assertFalse({'write_file', 'edit_file', 'run_process', 'execute_implementation'} & names)
        self.assertEqual(full._specialist_catalog(), {})
        self.assertEqual(app.state['config']['tool_contracts'], 'on_demand')

    def test_request_owner_source_only_and_exact_caller_defaults_are_preserved(self):
        for caller in (False, True):
            with self.subTest(caller=caller):
                self.root = self.root.parent / ('caller-source' if caller else 'owner-source')
                app = self._create(professional_work=None)
                arguments = {'goal': 'Prepare source only'}
                if not caller:
                    arguments['complete_work'] = False
                self.responses = [{'summary': 'Prepared source', 'unresolved': [],
                    'files': [{'path': 'calculate.py', 'content': 'value = 21\n'}]}]
                self.coordinator.side_effect = lambda *args, **kwargs: {
                    'role': 'assistant', 'content': '', 'tool_calls': [{
                        'id': 'source-only', 'type': 'function', 'function': {
                            'name': 'prepare_implementation', 'arguments': json.dumps(arguments)}}]}
                app.run(actions=[{'tool_name': 'prepare_implementation', 'arguments': arguments}] if caller else None,
                    progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
                observed, = self._observations(app)
                self.assertEqual(observed['status'], 'ok', observed)
                self.assertFalse(app.implementation.continuation(observed,
                    Path(app.state['turns'][0]['tools'][0]['result']).parent.name))
        self.assertEqual(self._process_receipts(), {})

    def test_document_work_order_cannot_reintroduce_a_coordinator_input_recipe(self):
        app = self._create()
        call = {'id': 'preselected-document', 'type': 'function', 'function': {
            'name': 'compose_document', 'arguments': json.dumps({
                'goal': 'Explain the supplied measurements',
                'material_roots': ['source0'],
                'delivery_artifacts': ['guessed-result.csv']})}}
        self.coordinator.side_effect = lambda *args, **kwargs: {
            'role': 'assistant', 'content': '', 'tool_calls': [call]}
        app.run(progress=lambda _: None, should_stop=lambda: bool(self._observations(app)))
        observed, = self._observations(app)
        self.assertEqual(observed['status'], 'blocked', observed)
        self.assertIn('not coordinator-selected evidence', observed['error'])
        self.assertFalse((self.root / 'tools/writing').exists())
        self.send.assert_not_called()
        ledger = app.ledger.to_dict()
        app = self._load()
        self.assertEqual(app._offered_tool_refusal(call).model_dump(), observed)
        self.assertEqual(app.ledger.to_dict(), ledger)
        # The narrower work order is not a retroactive change to a saved
        # exact-input session, nor a restriction on explicit caller operations.
        app.state['turns'][-1]['origin'] = 'caller'
        self.assertIsNone(app._offered_tool_refusal(call))
        app.state['config']['professional_inputs'] = 'owner.v2'
        app._save()
        app = self._load()
        count = len(self._observations(app))
        app.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': ['compose_document']}}],
            progress=lambda _: None, should_stop=lambda: len(self._observations(app)) == count + 1)
        schema = next(row['function']['parameters'] for row in app.schemas
            if row['function']['name'] == 'compose_document')
        self.assertIn('delivery_artifacts', schema['properties'])

    def test_resource_exhausted_observation_pauses_and_survives_reload_with_queue_and_ledger(self):
        base = self.root
        for professional_inputs in (True, False):
            with self.subTest(professional_inputs=professional_inputs):
                self.root = base / ('professional' if professional_inputs else 'historical')
                app = self._create(professional_inputs=professional_inputs)
                if not professional_inputs:
                    self.assertNotIn('professional_work', app.state['config'])
                # Explicit offline accounting fixture, not a claimed physical
                # execution: a nonempty exhausted ledger must survive the stop.
                amounts = {'process_invocations': 3, 'process_wall_seconds': 3}
                app.ledger.reserve('fixture-prior-use', amounts, purpose='offline-test-fixture')
                app.ledger.settle('fixture-prior-use', amounts, actual_source='offline-test-fixture')
                ledger = app.ledger.to_dict()
                ledger_path = app.ledger.storage_path
                ledger_bytes = ledger_path.read_bytes()
                failure = ToolResult(tool_name='prepare_implementation', status='error',
                    data={'failure_kind': 'resource_exhausted', 'executed': False},
                    error='No process invocation capacity remains; existing work is retained.')
                actions = [
                    # Source-only preparation may precede queued work. The v2
                    # complete-work default correctly rejects that batch before
                    # dispatch, which is not this resource-stop boundary.
                    {'tool_name': 'prepare_implementation', 'arguments': {
                        'goal': 'Prepare bounded code', 'complete_work': False}},
                    {'tool_name': 'write_file', 'arguments': {
                        'path': 'workspace/must-remain-queued.txt', 'content': 'Must not be dispatched'}}]
                app._submit_actions(actions)
                remaining = json.loads(json.dumps(app.state['turns'][-1]['remaining'][1:]))
                with patch.object(app.gateway, 'call', return_value=failure) as dispatch:
                    status = app.run(progress=lambda _: None)
                dispatch.assert_called_once()
                self.assertEqual(dispatch.call_args.args[0].tool_name, 'prepare_implementation')
                self.assertEqual(status['status'], 'paused', status)
                self.assertIn('Explicit resource authorization', app.session.manifest.status_reason)
                self.assertIsNone(app.state['pending'])
                self.assertEqual(app.state['turns'][-1]['remaining'], remaining)
                self.assertEqual(self._observations(app), [failure.model_dump(mode='json')])
                self.assertEqual(len(app.state['turns'][-1]['tools']), 1)
                record = app.session.store.resolve(app.state['turns'][-1]['tools'][0]['result'])
                record_bytes = record.read_bytes()
                actions_bytes = (self.root / 'actions.json').read_bytes()
                self.assertEqual(app.ledger.to_dict(), ledger)
                self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
                self.assertFalse((app.workspace.workspace / 'must-remain-queued.txt').exists())
                self.assertFalse(app.session.store.exists('delivery.json'))
                self.assertEqual(self._process_receipts(), {})
                # Loading an accepted failure and an ordinary resume preserve
                # both histories; neither authorizes more work or a model call.
                for _ in range(2):
                    app = self._load()
                    with patch.object(app.gateway, 'call',
                            side_effect=AssertionError('Paused queued work was dispatched')) as resumed:
                        status = app.run(progress=lambda _: None)
                    resumed.assert_not_called()
                    self.assertEqual(status['status'], 'paused', status)
                    self.assertIsNone(app.state['pending'])
                    self.assertEqual(app.state['turns'][-1]['remaining'], remaining)
                    self.assertEqual(self._observations(app), [failure.model_dump(mode='json')])
                    self.assertEqual(record.read_bytes(), record_bytes)
                    self.assertEqual((self.root / 'actions.json').read_bytes(), actions_bytes)
                    self.assertEqual(app.ledger.to_dict(), ledger)
                    self.assertEqual(ledger_path.read_bytes(), ledger_bytes)
                self.send.assert_not_called()
                self.coordinator.assert_not_called()

    def test_historical_task_blocks_complete_work_before_dispatch_and_hides_options(self):
        app = self._create(professional_inputs=False, tool_contracts='full')
        self.assertNotIn('professional_work', app.state['config'])
        app = self._load()
        schemas = {row['function']['name']: row['function']['parameters']
            for row in app.schemas}
        for field in ('complete_work', 'max_repairs', 'repair_execution'):
            self.assertNotIn(field, schemas['prepare_implementation']['properties'])
        self.assertNotIn('publication', schemas['compose_document']['properties'])
        before = app.ledger.to_dict()
        status = app.run(actions=[self._prepare(repairs=1)], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(status['status'], 'paused', status)
        observed, = self._observations(app)
        self.assertEqual(observed['status'], 'blocked', observed)
        self.assertFalse(observed['data']['executed'])
        self.assertIn('historical task', observed['error'])
        self.assertIsNone(app.state['pending'])
        self.assertFalse(app._continue_professional_work())
        self.assertEqual(app.ledger.to_dict(), before)
        self.assertEqual(self._process_receipts(), {})
        self.assertFalse((app.workspace.workspace / 'implementations').exists())
        self.send.assert_not_called()
        self.coordinator.assert_not_called()

    def test_reload_reuses_uncommitted_professional_submission_before_dispatch(self):
        self.responses = [self._proposal("from pathlib import Path\nPath('result.txt').write_text('21')\n")]
        app = self._create()
        # An actual saved legacy configuration, before any accepted tool state.
        app.state['config']['professional_work'] = 'continuations.v1'
        app._save()
        app = self._load()
        app.run(actions=[self._prepare()], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(self._observations(app)[0]['status'], 'ok')
        self.assertEqual(len(app.state['turns']), 1)
        # Crash after writing the real continuation payload but before actions.json
        # commits its turn. No continuation implementation is mocked here.
        with patch.object(app, '_save', side_effect=KeyboardInterrupt('journal boundary')):
            with self.assertRaises(KeyboardInterrupt):
                app._continue_professional_work()
        submission = self.root / 'tools/submissions/professional-1.json'
        retained = submission.read_bytes()
        ledger = app.ledger.to_dict()
        app = self._load()
        self.assertEqual(len(app.state['turns']), 1)
        self._run(app)
        pending, token = self._pending_execution(app)
        attempt = pending['attempt']
        self.assertEqual(submission.read_bytes(), retained)
        self.assertEqual(len(app.state['turns']), 2)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self._process_receipts(), {})
        # Once actions.json commits the synthetic turn, both its queue and its
        # allocated pending attempt survive repeated reloads without duplication.
        for _ in range(2):
            app = self._load()
            self._run(app)
            pending, current_token = self._pending_execution(app)
            self.assertEqual(pending['attempt'], attempt)
            self.assertEqual(current_token, token)
            self.assertEqual(len(app.state['turns']), 2)
            self.assertEqual(submission.read_bytes(), retained)
            self.assertEqual(app.ledger.to_dict(), ledger)
            self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)
        self.coordinator.assert_not_called()

    def test_explicit_resume_after_handoff_failure_consumes_receipt_without_reexecution(self):
        self.responses = [self._proposal("from pathlib import Path\nPath('result.txt').write_text('21')\n")]
        app = self._create()
        self._run(app, actions=[self._prepare()])
        _, token = self._pending_execution(app)
        original = app.gateway.professional_action
        def handoff(observed, source, **options):
            if observed.get('tool_name') == 'execute_implementation':
                raise RuntimeError('consumer handoff interrupted after committed execution')
            return original(observed, source, **options)
        with patch.object(app.gateway, 'professional_action', side_effect=handoff):
            app.run(approved_execution=token, progress=lambda _: None)
        self.assertEqual(app.session.manifest.status, 'paused')
        self.assertIn('consumer handoff interrupted', app.session.manifest.status_reason)
        self.assertIsNone(app.state['pending'])
        observed = self._observations(app)[-1]
        self.assertEqual((observed['tool_name'], observed['status']), ('execute_implementation', 'ok'))
        receipts, ledger, calls = self._process_receipts(), app.ledger.to_dict(), self.send.call_count
        count = len(app.state['requests'])
        app = self._load()
        def consume(actual, source):
            if actual.get('tool_name') != 'execute_implementation':
                return None
            self.assertEqual(actual, observed)
            # This fixture consumer still proves the accepted source; routing
            # through the common gateway is not itself adoption authority.
            from simple_ar.tools.professional_outcomes import accepted_native_result
            accepted_native_result(self.root, app.session.manifest.session_id,
                actual, Path(source).parent.name)
            return {'tool_name': 'deliver_result', 'arguments': {
                'summary': 'Deliver the already completed output',
                'artifacts': [str(app.workspace.resolve(actual['data']['cwd']) / 'result.txt')]}}
        app.gateway._domain_gateways['retained_delivery'] = SimpleNamespace(consume_outcome=consume)
        app.run(progress=lambda _: None)
        self.assertEqual(app.session.manifest.status, 'completed')
        self.assertEqual(self._process_receipts(), receipts)
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(len(app.state['requests']), count)
        self.coordinator.assert_not_called()

    def test_mismatched_uncommitted_continuation_fails_before_dispatch(self):
        self.responses = [self._proposal("from pathlib import Path\nPath('result.txt').write_text('21')\n")]
        app = self._create()
        app.run(actions=[self._prepare()], progress=lambda _: None,
            should_stop=lambda: bool(self._observations(app)))
        self.assertEqual(self._observations(app)[0]['status'], 'ok')
        with patch.object(app, '_save', side_effect=KeyboardInterrupt('journal boundary')):
            with self.assertRaises(KeyboardInterrupt):
                app._continue_professional_work()
        path = 'tools/submissions/professional-1.json'
        orphan = app.session.store.read_json(path)
        orphan['source_result'] = 'attempts/different-source/native_result.json'
        app.session.store.write_json(path, orphan, kind='professional_actions')
        app = self._load()
        saved_actions = (self.root / 'actions.json').read_bytes()
        ledger = app.ledger.to_dict()
        with self.assertRaisesRegex(ValueError, 'differs from its retained source'):
            app._continue_professional_work()
        self.assertEqual((self.root / 'actions.json').read_bytes(), saved_actions)
        self.assertEqual(app.session.store.read_json(path), orphan)
        self.assertEqual(len(app.state['turns']), 1)
        self.assertIsNone(app.state['pending'])
        self.assertEqual(app.ledger.to_dict(), ledger)
        self.assertEqual(self._process_receipts(), {})
        self.assertEqual(self.send.call_count, 1)
        self.coordinator.assert_not_called()


class ProfessionalReportChainTests(unittest.TestCase):
    """One task and real consumers; transport responses are checked fixtures.

    The raw-value oracle deliberately does not import the producer's calculation
    code or read its output to construct expectations. These are offline fixture
    provider responses, never evidence of autonomous provider quality. Diagnostic
    process allowances include intentional defects/recovery and are explicitly
    NOT the failed real trial's 10/8 process acceptance contract.
    """

    def setUp(self):
        self.compile_pdf = False
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.directory / 'task'
        self.inputs = self.directory / 'original-results'
        self.inputs.mkdir()
        for name in ('overall_metrics.csv', 'recorded_results.json'):
            shutil.copyfile(_SAVED_RESULTS / name, self.inputs / name)
        self.original_bytes = {path.name: path.read_bytes() for path in self.inputs.iterdir()}
        self.raw = list(csv.DictReader(StringIO(self.original_bytes['overall_metrics.csv'].decode())))
        self.expected = {(row['model'], row['policy']): Decimal(row['rmse']) for row in self.raw}
        self.records = json.loads(self.original_bytes['recorded_results.json'])
        self.responses, self.provider_requests, self.provider_errors = [], [], []
        self.preparations = []
        self.preparation_bytes = {}
        client = LLMClient(LLMSettings(api_key='offline-fixture', model='fixture',
            api_mode='chat', max_output_tokens=4096, context_input_tokens=100000,
            retry_attempts=1))
        self.services = SimpleNamespace(llm_client=client)
        self.send = self.enterContext(patch('simple_ar.integrations.llm._call_openai_sdk',
            side_effect=self._provider))
        self.native_chat_turn = LLMClient.chat_turn
        self.coordinator = self.enterContext(patch.object(LLMClient, 'chat_turn',
            side_effect=AssertionError('Professional owner requested another coordinator decision')))
        self.app = ToolApplication.create(self.root,
            'Report the saved model-policy RMSE comparisons with a table and main figure. '
            'Independently check the original records and every delivered material. '
            'Separate measured model costs from unknown policy costs. Do not fit or rerun experiments.',
            model='fixture', sources={'results': self.inputs}, services=self.services,
            professional_inputs=True, investigate_results=False, report_protocol='canonical_report.v1',
            allow_execution=True, max_turns=28, max_tokens=700000, max_attempts=100,
            process_limits={'process_invocations': 16, 'process_wall_seconds': 480})

    def _provider(self, mode, request, **kwargs):
        self.assertEqual(mode, 'chat')
        native = bool(request.get('tools'))
        if native:
            self.assertEqual(self.app.state['config']['professional_work'], 'requests.v1')
            self.assertIn("Own the user's complete professional work request", request['messages'][0]['content'])
        self.assertTrue(self.responses, 'Unexpected provider call; never fall through to a network')
        messages = request['messages']
        prompt, _ = json.JSONDecoder().raw_decode(messages[1]['content'])
        self.provider_requests.append((messages[0]['content'], prompt))
        if (prompt.get('delivery_review_contract')
                and self.app.state['config'].get('publication_preparation') == 'bound_candidate.v1'):
            self.assertIn('prepared_publication', prompt)
            prepared = prompt['prepared_publication']
            self.assertEqual(prepared, self.app.writing.prepared_publication(prepared['prepared_result']))
            self.assertFalse(prepared['adopted'])
        consumer = self.responses.pop(0)
        try:
            response = consumer(messages[0]['content'], prompt)
        except Exception as exc:
            # Transport/domain owners correctly retain a failed response. Keep
            # the fixture's original assertion visible when that becomes a
            # composition_incomplete result before the intended crash point.
            self.provider_errors.append({'consumer': consumer.__name__,
                'error': f'{type(exc).__name__}: {exc}', 'prompt_fields': sorted(prompt)})
            raise
        return {'choices': [{'message': response if native else {'role': 'assistant', 'content': json.dumps(response)},
            'finish_reason': 'tool_calls' if native else 'stop'}], 'usage': {
                'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}

    def _observations(self):
        return ProfessionalWorkLoopTests._observations(self.app)

    def _writing_owner(self, identity=None):
        from simple_ar.tools.writing import composition_owner
        return composition_owner(root=self.root, workspace=self.app.workspace,
            session_id=self.app.session.manifest.session_id,
            identity=identity or self.app.state['pending']['attempt']).root

    @staticmethod
    def _completed_compositions(rows):
        return [row for row in rows if row['tool_name'] == 'compose_document'
            and row['data'].get('work_status') != 'awaiting_publication_preparation']

    def _perform(self, action, *, feedback='', allowances=None, stop_tool=None):
        """Submit at the public boundary; never write accepted app state."""
        count = len(self._observations())
        stop_start = count
        target = stop_tool or action['tool_name']
        def stopped():
            rows = self._observations()[stop_start:]
            return bool(rows and (rows[-1]['tool_name'] == target
                or rows[-1]['data'].get('work_status') == 'awaiting_publication_preparation'))
        options = {'feedback': feedback, 'progress': lambda _: None, 'should_stop': stopped}
        if action is not None:
            options['actions'] = [action]
        if allowances is not None:
            options.update(resource_allowances=allowances, authorization_id='offline-feedback',
                authorization_reason='Second offline fixture phase; preserve original usage and artifacts.')
        status = self.app.run(**options)
        while self._observations()[count:] and self._observations()[-1]['data'].get(
                'work_status') == 'awaiting_publication_preparation':
            self._prepare_publication(self._observations()[-1])
            stop_start = len(self._observations())
            status = self.app.run(progress=lambda _: None, should_stop=stopped)
        return status, self._observations()[count:]

    def _prepare_publication(self, suspended):
        """Use the cooperative consumer protocol in test_writing_coordination.

        Keep normal app dispatch, exact approval, the actual renderer and the
        owner's consume_outcome/resume_work handback at every revision.
        """
        self.assertEqual(self.app.state['config']['publication_preparation'], 'bound_candidate.v1')
        self.assertFalse(suspended['data']['adopted'])
        stage_path = self.app.workspace.resolve(suspended['data']['publication_stage'])
        physical = stage_path.parent.name
        proposal = self.app.writing.continuation(suspended, physical)
        self.assertEqual(proposal['tool_name'], 'export_report')
        self.assertTrue(proposal['arguments']['candidate_result'])
        self.assertFalse(proposal['arguments'].get('delivery'))
        frozen = {stage_path: stage_path.read_bytes(),
            **{self.app.workspace.resolve(path): self.app.workspace.resolve(path).read_bytes()
                for path in suspended['artifacts']}}
        delivery_path = self.root / 'delivery.json'
        delivery = delivery_path.read_bytes() if delivery_path.exists() else None
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self.app.run(progress=lambda _: None)  # Stop at this candidate's approval gate.
        pending = self.app.state['pending']
        self.assertEqual(pending['call']['function']['name'], 'export_report')
        self.assertEqual(json.loads(pending['call']['function']['arguments']), proposal['arguments'])
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        prepared = self._approve_publication()
        self.assertEqual(prepared['data']['candidate_preparation'], {
            'source_result': proposal['arguments']['candidate_result'],
            'adopted': False, 'delivery_authority': False})
        self.assertNotIn('delivery', prepared['data'])
        reference = 'records/' + Path(prepared['data']['project']).name + '/native_result.json'
        inspection = self.app.writing.prepared_publication(reference)
        self.assertFalse(inspection['adopted'])
        self.assertEqual(self.app.writing.consume_outcome(prepared, reference),
            {'tool_name': 'compose_document', 'arguments': {'resume_work': reference}})
        self.assertEqual({path: path.read_bytes() for path in frozen}, frozen)
        self.assertEqual(delivery_path.read_bytes() if delivery_path.exists() else None, delivery)
        self.preparations.append(prepared)
        self.preparation_bytes.update(frozen)
        self.preparation_bytes.update({self.app.workspace.resolve(path): self.app.workspace.resolve(path).read_bytes()
            for path in prepared['artifacts']})

    def _assert_accounting(self, *, fixture_calls=0):
        entries = self.app.ledger.entries
        self.assertTrue(all(entry.status in {'settled', 'released'} for entry in entries))
        calls = sum(entry.actual.get('llm_requests', 0) for entry in entries)
        self.assertEqual(calls, self.send.call_count - fixture_calls)
        self.assertEqual(sum(entry.actual.get('total_tokens', 0) for entry in entries), 120 * calls)
        receipts = [json.loads(path.read_bytes())
            for path in (self.root / 'tools/processes').glob('*/invocation.json')]
        self.assertEqual(sum(entry.actual.get('process_invocations', 0) for entry in entries), len(receipts))
        for record in receipts:
            charge, = [entry for entry in entries
                if entry.reservation_id == 'process-' + record['invocation_id']]
            self.assertEqual(charge.actual, {'process_invocations': 1,
                'process_wall_seconds': record['duration_sec']})
        self.assertEqual({path: path.read_bytes() for path in self.preparation_bytes}, self.preparation_bytes)
        self.assertFalse(self.provider_errors)

    def _produce(self, *, wrong_table=False, grayscale=False, missing=False, prior=None,
                 feedback='', allowances=None, review_repair=False):
        flags = ((['--wrong-table'] if wrong_table else []) + (['--grayscale'] if grayscale else [])
            + (['--omit-table'] if missing else []))
        argv = ['python', 'plot_results.py', '--metrics', str(self.inputs / 'overall_metrics.csv'),
            '--records', str(self.inputs / 'recorded_results.json'), *flags]
        def generation(system, prompt):
            self.assertIn('Implement one coherent, bounded code work unit', system)
            supplied = json.dumps(prompt, ensure_ascii=False)
            stack, texts = [prompt], []
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, str):
                    texts.append(value)
            for name, raw in self.original_bytes.items():
                self.assertIn(name, supplied)
                # Check the real owner supplied original data, not just locators.
                self.assertTrue(any(raw.decode() in text for text in texts), name)
            if prior is not None:
                if not any(_PRODUCER in text for text in texts):
                    # Plan-only registered feedback retains members by bytes,
                    # not by retransmitting every source as editable text.
                    self.responses.insert(0, generation)
                    return {'read_windows': [{'path': str(prior / 'plot_results.py'),
                        'max_bytes': 64000, 'full_digest': True}]}
                self.assertTrue(any(_PRODUCER in text for text in texts))
            if review_repair:
                view, = prompt['execution_observations']
                self.assertEqual(set(view), {'tool_name', 'diagnosis_record', 'delivery_review'})
                record = self.app.workspace.resolve(view['diagnosis_record']['path'])
                canonical_bytes = record.read_bytes()
                self.assertEqual(view['diagnosis_record']['sha256'], hashlib.sha256(canonical_bytes).hexdigest())
                canonical = json.loads(canonical_bytes)
                self.assertEqual(view['tool_name'], 'compose_document')
                from simple_ar.tools.writing import read_repair_review
                diagnosis = read_repair_review(self.app.workspace, canonical['data'])['delivery_review']
                self.assertEqual(view['delivery_review']['status'], diagnosis['status'])
                restored = json.loads(json.dumps(view['delivery_review']['defects']))
                already_supplied = [row['text'] for row in prompt['original_files'].values()]
                already_supplied.extend(row['text'] for row in prompt['source_windows']
                    if 'request' in row and isinstance(row.get('text'), str))
                references = unique_quotes = 0
                reference_containers = set()
                for item in restored:
                    for quote in item['finding']['evidence_quotes']:
                        reference = quote.pop('supplied_text_reference', None)
                        if reference is None:
                            unique_quotes += 1
                            if 'working_set' not in prompt['investigation']:
                                self.assertFalse(any(quote['quote'] in text for text in already_supplied))
                            else:
                                # Dynamic material positions are not durable
                                # quote owners. Keep the exact original quote.
                                self.assertFalse(any(quote['quote'] in row['text']
                                    for row in prompt['original_files'].values()))
                            continue
                        references += 1
                        self.assertNotIn('quote', quote)
                        container = reference['container']
                        reference_containers.add(container)
                        source = (prompt['original_files'][reference['key']] if container == 'original_files'
                            else prompt['source_windows'][reference['index']])
                        self.assertIn(container, {'original_files', 'source_windows'})
                        path = source['path'] if container == 'original_files' else source['request']['path']
                        self.assertEqual(self.app.workspace.resolve(reference['path']), self.app.workspace.resolve(path))
                        self.assertEqual(reference['sha256'], source['sha256'])
                        start, end = reference['start_char'], reference['end_char']
                        self.assertTrue(0 <= start < end <= len(source['text']))
                        quote['quote'] = source['text'][start:end]
                        self.assertEqual(reference['quote_sha256'],
                            hashlib.sha256(quote['quote'].encode('utf-8')).hexdigest())
                self.assertGreater(references, 0, 'Shared original counter-evidence should be referenced, not copied')
                self.assertEqual(reference_containers, {'original_files'}
                    if 'working_set' in prompt['investigation'] else {'original_files', 'source_windows'})
                self.assertGreater(unique_quotes, 0, 'Unique generated counter-evidence must remain literal')
                self.assertEqual(restored, diagnosis['defects'])
                self.assertEqual(sum(text.count(_PRODUCER) for text in texts), 1)
                if 'working_set' not in prompt['investigation']:
                    self.assertEqual(supplied.count('wrong-delivered-table'), 1)
                else:
                    self.assertGreaterEqual(supplied.count('wrong-delivered-table'), 1)
                self.assertEqual(record.read_bytes(), canonical_bytes)
            return {'summary': 'Plot and tabulate existing recorded values; no fitting',
                'unresolved': list(_PRODUCER_DECLARATIONS),
                'files': [] if prior else [{'path': 'plot_results.py', 'content': _PRODUCER}],
                'execution_plan': {'argv': argv,
                    'outputs': ['comparison.md', 'figure-data.csv', 'main.png', 'cost-scope.json'],
                    'timeout_sec': 30}}
        if prior is None:
            def read_originals(system, prompt):
                self.assertIn('own investigation and implementation', system)
                self.assertIn('investigation', prompt)
                return {'read_windows': [{'path': 'results/' + name, 'max_bytes': 64000,
                    'full_digest': True} for name in self.original_bytes]}
            self.responses.append(read_originals)
        self.responses.append(generation)
        # The native owner contract requires an explicit execution intention;
        # ordinary preparation no longer implicitly authorizes a complete unit.
        arguments = {'goal': ('Revise to grayscale and preserve measurements' if grayscale else
            'Produce the saved-result comparison table and figure'), 'complete_work': True,
            'max_repairs': 0 if missing else 1}
        if prior is not None:
            arguments['project_path'] = str(prior)
        else:
            arguments['material_roots'] = ['results']
        action = None if review_repair else {'tool_name': 'prepare_implementation', 'arguments': arguments}
        status, rows = self._perform(action,
            feedback=feedback, allowances=allowances, stop_tool='execute_implementation')
        self.assertEqual(status['status'], 'paused', status)
        self.assertTrue(rows, status)
        self.assertEqual(rows[-1]['tool_name'], 'prepare_implementation', rows[-1])
        self.assertEqual(rows[-1]['status'], 'ok', rows[-1])
        candidate = Path(rows[-1]['data']['project_dir'])
        if review_repair:
            self.assertEqual(self.app.state['turns'][-1]['origin'], 'professional')
            prepared = self.app.workspace.resolve(rows[-1]['data']['prepared_result'])
            source = json.loads((prepared.parent / 'request.json').read_text())
            self.assertTrue(source['request']['repair_review'])
        pending = self.app.state['pending']
        self.assertIsNotNone(pending, status)
        self.assertEqual(pending['call']['function']['name'], 'execute_implementation', status)
        token = self.app.approval_token(pending['call'])
        # Reload at the actual approval gate; lack of approval cannot run Python.
        self.app = ToolApplication.load(self.root, services=self.services)
        self.assertEqual(self.app.approval_token(self.app.state['pending']['call']), token)
        before = len(self._observations())
        self.app.run(approved_execution=token, progress=lambda _: None,
            should_stop=lambda: len(self._observations()) > before)
        executed = self._observations()[-1]
        self.assertEqual(executed['data']['unresolved'], _PRODUCER_DECLARATIONS,
            'Execution must retain producer declarations, not silently clear them')
        if missing:
            self.assertEqual((executed['tool_name'], executed['status'], executed['data']['returncode']),
                ('execute_implementation', 'blocked', 0), executed)
            self.assertIn('output_binding_error', executed['data'])
            self.assertIn('Saved recorded measurements', executed['data']['stdout'])
            self.assertTrue(executed['data']['receipt'])
            self.assertFalse((candidate / 'comparison.md').exists())
            stop = self.app.implementation.continuation(executed,
                Path(executed['data']['execution_ref']).parent.name)['stop']
            self.assertEqual(stop['reason'], 'repair_ceiling')
            self.assertFalse(stop['completed'])
            return candidate, executed
        self.assertEqual((executed['tool_name'], executed['status'], executed['data']['returncode']),
            ('execute_implementation', 'ok', 0), executed)
        provenance = self.app.workspace.artifact_provenance([str(candidate / 'comparison.md')])
        producer = provenance[str(candidate / 'comparison.md')]['producer']
        self.assertIsNotNone(producer)
        self.assertEqual(producer['invocation_id'], Path(executed['data']['execution_ref']).parent.name)
        # Receipts retain canonical mounted locators, not necessarily the
        # absolute spelling used by the caller. Compare resolved identities AND
        # actual bytes; accepting either spelling must not lose raw provenance.
        bindings = {}
        for kind in ('inputs', 'outputs'):
            bindings[kind] = {}
            for locator, digest in producer[kind].items():
                path = self.app.workspace.resolve(locator)
                self.assertTrue(path.is_file(), (kind, locator))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest,
                    (kind, locator))
                bindings[kind][path] = digest
        for name, raw in self.original_bytes.items():
            self.assertEqual(bindings['inputs'].get((self.inputs / name).resolve()),
                hashlib.sha256(raw).hexdigest(), name)
        table = (candidate / 'comparison.md').resolve()
        self.assertEqual(bindings['outputs'].get(table),
            hashlib.sha256(table.read_bytes()).hexdigest())
        self.assertEqual(json.loads((candidate / 'cost-scope.json').read_text())['policy_level_costs'], None)
        return candidate, executed

    def _audit_products(self, candidate, *, grayscale=False):
        """Independent comparison, not a production certification shortcut."""
        errors = []
        lines = (candidate / 'comparison.md').read_text().splitlines()[2:]
        actual = {}
        for line in lines:
            model, policy, value = (part.strip() for part in line.strip('|').split('|'))
            actual[model, policy] = Decimal(value)
        self.assertEqual(set(actual), set(self.expected))
        for key, value in self.expected.items():
            if actual[key] != value.quantize(Decimal('0.001')):
                errors.append({'material': str(candidate / 'comparison.md'), 'row': key,
                    'expected': str(value.quantize(Decimal('0.001'))), 'actual': str(actual[key])})
        points = list(csv.DictReader(StringIO((candidate / 'figure-data.csv').read_text())))
        self.assertEqual({(row['model'], row['policy']): float(row['rmse']) for row in points},
            {key: float(value) for key, value in self.expected.items()})
        from PIL import Image
        with Image.open(candidate / 'main.png') as image:
            self.assertEqual(image.size, (600, 300))
            self.assertTrue(any(low < high for low, high in image.convert('RGB').getextrema()))
            embedded = json.loads(image.info['Description'])
            self.assertEqual({(row['model'], row['policy']): row['rmse'] for row in embedded},
                {key: float(value) for key, value in self.expected.items()})
            self.assertEqual(image.info['Style'], 'grayscale' if grayscale else 'color')
            if grayscale:
                self.assertTrue(all(r == g == b for r, g, b in image.convert('RGB').getdata()))
        costs = json.loads((candidate / 'cost-scope.json').read_text())
        self.assertEqual(costs['measured_model_costs'], self.records['total_median_cost_seconds'])
        self.assertEqual(costs['timing_scope'], self.records['timing_scope'])
        self.assertEqual({path.name: path.read_bytes() for path in self.inputs.iterdir()}, self.original_bytes)
        return errors

    def _compose(self, candidate, *, prior=None, defect=False, grayscale=False,
                 prose_only=False, publish=False, resume=False, revision_defect=False, historical_project=None):
        goal_findings = self.app.state['config'].get('producer_completion') == 'goal_findings.v1'
        caption = ('Recorded RMSE; grayscale lines and distinct markers.' if grayscale else
            'Recorded RMSE comparison from saved measurements.')
        prior_text = self.app.workspace.resolve(prior).read_text(encoding='utf-8') if prior else ''
        attachments = [str(candidate / name) for name in (
            'plot_results.py', 'comparison.md', 'figure-data.csv', 'cost-scope.json')]
        subjects_expected = {*attachments, str(candidate / 'main.png')}
        # Actual producer inputs remain authoritative. A proven superseded
        # project is historical review context, not an extra current attachment.
        provenance = self.app.workspace.artifact_provenance([str(candidate / 'comparison.md')])
        producer = provenance[str(candidate / 'comparison.md')]['producer']
        execution_ref = f"records/{producer['invocation_id']}/candidate_execution.json"
        execution_path = self.app.workspace.resolve(execution_ref)
        execution = json.loads(execution_path.read_bytes())
        self.assertEqual(execution['unresolved'], _PRODUCER_DECLARATIONS)
        for source in provenance[str(candidate / 'comparison.md')]['producer']['inputs']:
            path = self.app.workspace.resolve(source)
            if historical_project is not None and path.is_relative_to(historical_project):
                continue
            if path.is_relative_to(self.app.workspace.workspace) and path.suffix == '.py':
                subjects_expected.add(str(path))
        writer_calls = reviewer_calls = 0
        producer_declarations = None
        initial_inspections = []
        rejected_subjects = None
        initial_body = ''
        process_count = sum(e.actual.get('process_invocations', 0) for e in self.app.ledger.entries)
        preparation_start = len(self.preparations)
        attachment_bytes = {path: Path(path).read_bytes() for path in subjects_expected}
        def writer(system, prompt):
            nonlocal writer_calls, initial_body, producer_declarations
            writer_calls += 1
            self.assertIn('source_handles', prompt)
            owner = self._writing_owner()
            binding = json.loads((owner / 'request.json').read_text(encoding='utf-8'))
            self.assertEqual(binding['request']['goal'], '\n\n'.join(self.app.state['requests']),
                'The bound report goal must come from actual user requests, not a coordinator paraphrase')
            self.assertIn('work_context', binding)
            expected_goal = (binding['request']['goal']
                + '\nOriginal task request and relevant accepted feedback (not source evidence):\n'
                + json.dumps(binding['work_context'], ensure_ascii=False))
            # This fixture freezes an article with no mandatory outline or
            # citation ceiling. The professional report owner appends those
            # mechanical constraints after its original feedback context too.
            requirement = binding['report_binding']['requirement']
            self.assertEqual(requirement['outline'], [])
            self.assertEqual(requirement['config'].get('max_cited_sources', 0), 0)
            self.assertEqual(binding['request'].get('max_cited_sources', 0), 0)
            expected_goal += ('\nMechanical output constraints (mandatory):\n'
                + json.dumps({'outline': [], 'max_cited_sources': 0}, ensure_ascii=False))
            self.assertEqual(prompt['goal'], expected_goal,
                'Writer must receive the exact goal plus the actual frozen owner context')
            stack, supplied_text = [prompt['source_handles']], []
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    stack.extend(value.values())
                elif isinstance(value, list):
                    stack.extend(value)
                elif isinstance(value, str):
                    supplied_text.append(value)
            for name, raw in self.original_bytes.items():
                self.assertTrue(any(raw.decode('utf-8') in text for text in supplied_text),
                    'Actual Writer lost native selected original material: ' + name)
            self.assertIn('delivery_review', prompt)
            groups = prompt['delivery_review']['producer_notes' if goal_findings else 'producer_declarations']
            if goal_findings:
                self.assertNotIn('producer_declarations', prompt['delivery_review'])
                self.assertNotIn('declaration_adoption_contract', prompt['delivery_review'])
            group, = groups
            self.assertEqual(group['execution_ref'], execution_ref)
            self.assertEqual(group['execution_sha256'], hashlib.sha256(execution_path.read_bytes()).hexdigest())
            self.assertEqual(group['producer']['invocation_id'], producer['invocation_id'])
            self.assertEqual(group['execution']['status'], 'run_succeeded')
            self.assertEqual(group['execution']['output_sha256'], execution['output_sha256'])
            if goal_findings:
                self.assertEqual(group['notes'], _PRODUCER_DECLARATIONS)
            else:
                self.assertEqual([row['statement'] for row in group['declarations']], _PRODUCER_DECLARATIONS)
                self.assertEqual([row['source_pointer'] for row in group['declarations']], ['/unresolved/0', '/unresolved/1'])
            prepared_path = self.app.workspace.resolve(group['prepared_result'])
            self.assertEqual(group['prepared_sha256'], hashlib.sha256(prepared_path.read_bytes()).hexdigest())
            self.assertEqual(group['source_binding_sha256'], hashlib.sha256(
                (prepared_path.parent / 'request.json').read_bytes()).hexdigest())
            if producer_declarations is None:
                producer_declarations = groups
            self.assertEqual(groups, producer_declarations)
            supplied = prompt['delivery_review']['artifacts']
            self.assertEqual({row['path'] for row in supplied}, subjects_expected)
            for row in supplied:
                if row['path'].endswith('/main.png'):
                    self.assertEqual(row['coverage'], 'visual_uninspected')
                    self.assertFalse(row['visual_reviewed'])
                    self.assertIsNotNone(row['producer'])
                    continue
                self.assertEqual(row['coverage'], 'complete')
                self.assertNotIn('text', row, 'Attachment text belongs to independent inspection')
                self.assertEqual(row['sha256'], hashlib.sha256(Path(row['path']).read_bytes()).hexdigest())
                if Path(row['path']).name != 'plot_results.py':
                    self.assertIsNotNone(row['producer'])
                else:
                    self.assertTrue(row.get('consumers'), 'Inspected script must be an actual producer input')
            expected_prior = initial_body if revision_defect and writer_calls == 2 else prior_text
            self.assertEqual(prompt.get('prior_draft', ''), expected_prior)
            if revision_defect and writer_calls == 2:
                self.assertIn('wrong-prose-only', json.dumps(prompt))
                self.assertIn('missing-cost-limitation', json.dumps(prompt))
                self.assertTrue(initial_inspections, 'The real initial inspection must precede prose repair')
                initial = initial_inspections[0]['delivery_review']
                self.assertEqual((initial['status'], initial['defects']),
                    ('reviewed' if goal_findings else 'needs_revision', []))
                if not goal_findings:
                    self.assertEqual(initial['declaration_adoption']['checks'][1]['status'], 'unresolved_work')
                self.assertNotIn(_PRODUCER_DECLARATIONS[1], expected_prior)
                self.assertEqual(sum(e.actual.get('process_invocations', 0)
                    for e in self.app.ledger.entries),
                    process_count + self._preparation_process_usage(preparation_start))
                self.assertEqual({path: Path(path).read_bytes() for path in attachment_bytes}, attachment_bytes)
            facts = '; '.join(f'{model}/{policy}: {value:.3f}'
                for (model, policy), value in self.expected.items())
            correct_table = ('| Model | Policy | RMSE |\n| --- | --- | ---: |\n'
                + '\n'.join(f'| {model} | {policy} | {value:.3f} |'
                    for (model, policy), value in self.expected.items()))
            citations = ' '.join('[@' + row['id'] + ']' for row in prompt['bibliography'])
            body = ('# Recorded model-policy comparison\n\n## Results\n\n' + facts + '. ' + citations
                + '\n\n' + correct_table
                + '\n\n![' + caption + '](figures/selected-1.png)\n\n'
                'The attached comparison table and plotting sources accompany the report.\n\n'
                '## Limitations\n\nCosts are measured per model, not per policy; policy costs remain unknown. '
                'Only saved results were used; no model fitting or experiment rerun.\n')
            if prose_only:
                body += '\nProse-only correction: the original measurements remain the reference.\n'
            self.correct_prose = body
            if revision_defect and writer_calls == 1:
                body = body.replace('linear/frozen: 2.526', 'linear/frozen: 3.026')
                body = body.replace(_PRODUCER_DECLARATIONS[1] + ' ', '')
                initial_body = body
            if expected_prior:
                return {'prior_draft_sha256': prompt['edit_contract']['prior_draft_sha256'],
                    'replacements': [{'old_text': expected_prior, 'new_text': body}]}
            return {'draft_markdown': body}
        def reviewer(system, prompt):
            nonlocal reviewer_calls, rejected_subjects
            reviewer_calls += 1
            self.assertEqual(prompt['delivery_review_contract'], 'delivery_artifacts.v1')
            draft = prompt['sections'][0]
            subjects = restore_delivery_producers(prompt)
            if goal_findings:
                self.assertEqual(prompt['producer_completion'], 'goal_findings.v1')
                self.assertNotIn('producer_declarations', prompt)
                self.assertNotIn('producer_claim_checks', prompt['output_schema']['section_reviews'][0])
                self.assertEqual(prompt['producer_notes'], producer_declarations)
                self.assertTrue(prompt['objective'])
                self.assertTrue(prompt['source_evidence'])
            else:
                self.assertEqual(prompt['declaration_adoption_contract'], 'producer_declarations.v1')
                self.assertEqual(prompt['producer_declarations'], producer_declarations)
            group, = producer_declarations
            stage, limitation = group.get('declarations', [None, None])
            limitation_evidence = [{'pointer': f"/delivery_artifacts/{next(i for i, row in enumerate(subjects) if row['path'].endswith('/cost-scope.json'))}/text"}]
            checks = [] if goal_findings else [
                {'declaration_id': stage['id'],
                    'status': 'unresolved_work' if defect and reviewer_calls >= 2 else 'satisfied_by_execution',
                    'explanation': ('Required joint manuscript/material checking is unfinished: the actionable finding below must be repaired and independently inspected.'
                        if defect and reviewer_calls >= 2 else
                        'The settled execution generated the current table, figure and recorded-data outputs; their text is jointly inspected below.'),
                    'evidence_quotes': [{'pointer': '/producer_declarations/0/execution/status'}]},
                {'declaration_id': limitation['id'], 'status': 'accepted_limitation',
                    'explanation': 'The inspected cost-scope JSON has model costs but no policy costs, and this limitation is explicitly disclosed in the current manuscript.',
                    'draft_quotes': [{'section_id': draft['section_id'], 'quote': _PRODUCER_DECLARATIONS[1]}],
                    'evidence_quotes': [{'pointer': '/producer_declarations/0/execution/status'},
                        {'pointer': f"/delivery_artifacts/{next(i for i, row in enumerate(subjects) if row['path'].endswith('/cost-scope.json'))}/text"}]},
            ]
            if revision_defect and reviewer_calls == 1:
                self.assertNotIn(_PRODUCER_DECLARATIONS[1], draft['markdown'])
                if not goal_findings:
                    checks[1].update(status='unresolved_work', draft_quotes=[],
                        explanation='This is a legitimate recorded-data limitation, but it is not disclosed in the current manuscript. Add the qualification in prose; do not rerun or change the producer.')
            else:
                self.assertIn(_PRODUCER_DECLARATIONS[1], draft['markdown'])
            if resume and not goal_findings:
                # Exercise rejection against the real current review view; no
                # synthetic accepted receipt or extra reviewer request is made.
                from simple_ar.integrations.llm import LLMResponseError
                from simple_ar.report.schema import ReportSectionReview
                from simple_ar.report.review_evidence import (
                    resolve_review_evidence, review_draft_quote_sources, validate_producer_claim_checks,
                )
                reviewed = ReportSectionReview.model_validate(resolve_review_evidence({
                    'section_id': draft['section_id'], 'verdict': 'pass',
                    'producer_claim_checks': checks}, prompt))
                draft_sources = review_draft_quote_sources(prompt)
                validate_producer_claim_checks([reviewed], draft_sources, prompt)
                for mutation, message in (('missing', 'every original'),
                        ('duplicate', 'repeated original'), ('undisclosed', 'disclosure'),
                        ('wrong-draft', 'absent from the current manuscript')):
                    negative = reviewed.model_copy(deep=True)
                    if mutation == 'missing':
                        negative.producer_claim_checks.pop()
                    elif mutation == 'duplicate':
                        negative.producer_claim_checks.append(negative.producer_claim_checks[0])
                    elif mutation == 'undisclosed':
                        negative.producer_claim_checks[1].draft_quotes = []
                    else:
                        negative.producer_claim_checks[1].draft_quotes[0].quote = 'Not present in this manuscript version.'
                    with self.subTest(declaration_rejection=mutation), self.assertRaisesRegex(LLMResponseError, message):
                        validate_producer_claim_checks([negative], draft_sources, prompt)
            self.assertEqual({row['path'] for row in subjects}, subjects_expected)
            if revision_defect and reviewer_calls == 3:
                correction = prompt['response_format_correction']
                self.assertIn('recorded_material', correction['validation_error'])
                self.assertEqual(subjects, rejected_subjects,
                    'Format correction must retain every frozen attachment, text, hash and producer')
                owner = self._writing_owner()
                rejections = list(owner.glob('writer/review/revision-*/inspection/rejection.json'))
                self.assertEqual(len(rejections), 1)
                self.assertEqual(json.loads(rejections[0].read_text())['validation_error'], correction['validation_error'])
                subset = json.loads(rejections[0].with_name('validated_subset.json').read_text())
                self.assertEqual(subset['review_status'], 'incomplete')
                self.assertFalse((owner / 'result.json').exists(), 'Rejected inspection cannot become a completed composition')
            if revision_defect and reviewer_calls == 1:
                # Correct generated tabular evidence refutes prose. Referencing
                # it must NOT accuse it or invoke the implementation owner.
                self.assertIn('linear/frozen: 3.026', draft['markdown'])
                index = next(i for i, row in enumerate(subjects) if row['path'].endswith('/figure-data.csv'))
                quote = next(line for line in subjects[index]['text'].splitlines()
                    if line.startswith('linear,frozen,'))
                self.assertEqual(Decimal(quote.split(',')[-1]), self.expected['linear', 'frozen'])
                return {'section_reviews': [{'section_id': draft['section_id'],
                    'verdict': 'revise_required', **({} if goal_findings else {'producer_claim_checks': checks}), 'findings': [{
                        'finding_id': 'wrong-prose-only', 'type': 'metric_mismatch',
                        'severity': 'major', 'required_action': 'revise',
                        'message': 'The generated data table is correct; only the prose value is wrong.',
                        'suggested_action': 'Correct the prose without changing or rerunning the producer.',
                        'delivery_targets': [],
                        'draft_quotes': [{'section_id': draft['section_id'], 'quote': 'linear/frozen: 3.026'}],
                        'evidence_quotes': [{'pointer': f'/delivery_artifacts/{index}/text',
                            'quote': quote, 'role': 'producer_output'}]},
                        {'finding_id': 'missing-cost-limitation', 'type': 'omission',
                            'severity': 'major', 'required_action': 'revise',
                            'message': 'The manuscript omits the legitimate model-versus-policy timing limitation.',
                            'suggested_action': 'Disclose that policy-level costs remain unknown; do not generate new measurements or modify attachments.',
                            'delivery_targets': [], 'evidence_quotes': limitation_evidence}]}]}
            self.assertIn('linear/frozen: 2.526', draft['markdown'])
            self.assertNotIn('linear/frozen: 3.026', draft['markdown'])
            table_index = next(i for i, row in enumerate(subjects) if row['path'].endswith('/comparison.md'))
            self.assertIsNotNone(subjects[table_index]['producer'])
            findings = []
            if defect:
                bad_row = '| linear | frozen | 3.026 |'
                self.assertIn(bad_row, subjects[table_index]['text'])
                quote = 'linear,frozen,3493,2.5260884003271107'
                stack = [('/source_evidence', prompt['source_evidence'])]
                evidence = None
                while stack:
                    pointer, value = stack.pop()
                    if isinstance(value, dict):
                        stack.extend((pointer + '/' + key.replace('~', '~0').replace('/', '~1'), val)
                            for key, val in value.items())
                    elif isinstance(value, list):
                        stack.extend((pointer + '/' + str(i), val) for i, val in enumerate(value))
                    elif isinstance(value, str) and quote in value:
                        evidence = {'pointer': pointer, 'quote': quote, 'role': 'recorded_material'}
                        break
                self.assertIsNotNone(evidence, 'Reviewer never received the original CSV counter-evidence')
                findings = [{'finding_id': 'wrong-delivered-table', 'type': 'metric_mismatch',
                    'severity': 'major', 'required_action': 'revise',
                    'message': 'The manuscript is correct but the delivered table differs from the original CSV.',
                    'suggested_action': 'Regenerate the table with its producer and jointly inspect the new version.',
                    'delivery_targets': [f'/delivery_artifacts/{table_index}/text'],
                    'evidence_quotes': [
                        {'pointer': f'/delivery_artifacts/{table_index}/text', 'quote': bad_row,
                            'role': 'producer_output'}, evidence]}]
                # This correct generated CSV row is not already in a supplied
                # original/window (the original CSV also has an n column).
                # Preserve this independent counter-evidence literally while
                # the shared original-source quotation becomes a text ref.
                index = next(i for i, row in enumerate(subjects) if row['path'].endswith('/figure-data.csv'))
                quote = next(line for line in subjects[index]['text'].splitlines()
                    if line.startswith('linear,frozen,'))
                findings[0]['evidence_quotes'].append({'pointer': f'/delivery_artifacts/{index}/text',
                    'quote': quote, 'role': 'producer_output'})
                # The canonical review may retain the whole inspected script.
                # The repair model already receives that exact registered
                # original: require a full-span reference instead of copies in
                # both delivery defects and the old compose unresolved result.
                index = next(i for i, row in enumerate(subjects) if row['path'].endswith('/plot_results.py'))
                self.assertEqual(subjects[index]['text'], _PRODUCER)
                self.assertIsNone(subjects[index]['producer'])
                self.assertTrue(subjects[index].get('consumers'))
                findings[0]['evidence_quotes'].append({'pointer': f'/delivery_artifacts/{index}/text',
                    'quote': subjects[index]['text'], 'role': 'recorded_material'})
                if revision_defect and reviewer_calls == 2:
                    self.assertFalse(prompt['response_format_correction'])
                    rejected_subjects = json.loads(json.dumps(subjects))
                    # Deliberately wrong once: the real validator/correction
                    # owner must retain the rejection AND full attachment view.
                    findings[0]['evidence_quotes'][-1]['role'] = 'producer_output'
            return {'section_reviews': [{'section_id': draft['section_id'],
                'verdict': 'revise_required' if defect else 'pass', 'findings': findings,
                **({} if goal_findings else {'producer_claim_checks': checks})}]}
        # All ordinary and repair compositions use the same real selection
        # consumer. Selecting generated text as well as originals is essential:
        # its actual receipts, not the attachment list, establish script users.
        windows = [{'path': str(path), 'max_bytes': 64000, 'full_digest': True}
            for path in [*(self.inputs / name for name in self.original_bytes),
                *(Path(path) for path in attachments)]]
        selection_calls = 0
        def select(system, prompt):
            nonlocal selection_calls
            selection_calls += 1
            self.assertLessEqual(selection_calls, 2, 'Selection failed to retain the requested complete reads')
            self.assertIn('observations', prompt)
            visible = {self.app.workspace.resolve(row[0])
                for inventory in prompt['inventories'] for row in inventory['files']}
            self.assertTrue({self.app.workspace.resolve(window['path']) for window in windows} <= visible,
                'The owner must authorize originals and current generated text in its actual inventory')
            observed = {self.app.workspace.resolve(row['request']['path']): row['data']
                for row in prompt['observations']}
            missing = []
            for window in windows:
                path = self.app.workspace.resolve(window['path'])
                data = observed.get(path)
                if data is None or data.get('has_more'):
                    missing.append(window)
                else:
                    self.assertEqual(data['text'], path.read_bytes().decode('utf-8'), str(path))
                    self.assertEqual(data['sha256'], hashlib.sha256(path.read_bytes()).hexdigest(), str(path))
            if missing:
                self.responses.insert(0, select)
                return {'read_windows': missing}
            return {'materials': windows}
        if not resume:
            self.responses.append(select)
        self.responses.extend([writer, reviewer])
        if revision_defect:
            self.responses.extend([writer, reviewer, reviewer])
        # The formal material-report public contract derives goal from the
        # bound user requests. Supply its native title/options and delegate
        # evidence selection to the actual material owner, not preselected text.
        arguments = {'title': 'Recorded model-policy comparison',
            'material_roots': ['results', str(candidate)],
            'figures': [str(candidate / 'main.png')],
            'delivery_artifacts': attachments,
            'publication': {'compile_pdf': self.compile_pdf, 'render_preview': self.compile_pdf}}
        if prior:
            arguments['prior_draft'] = prior
        self.assertFalse({'goal', 'materials', 'sources'} & arguments.keys())
        action = None if resume else {'tool_name': 'compose_document', 'arguments': arguments}
        target = 'export_report' if publish else 'compose_document'
        if revision_defect or resume:
            # Crash after the REAL inspection's complete atomic response: either
            # the initial revision defect or the repaired-input composition.
            # This is a journal-boundary injection, not a substitute reviewer.
            from simple_ar.report import writing as writing_module
            inspect_article = writing_module.inspect_article
            interrupted = []
            def interrupt_after_inspection(*args, **kwargs):
                result = inspect_article(*args, **kwargs)
                if revision_defect and ':revision-' not in kwargs['identity']:
                    self.assertEqual((result['delivery_review']['status'], result['delivery_review']['defects']),
                        ('reviewed' if goal_findings else 'needs_revision', []),
                        'Missing prose disclosure is not an attachment defect')
                    initial_inspections.append(result)
                if ((revision_defect and ':revision-' in kwargs['identity'] and result['delivery_review']['defects'])
                        or (resume and result['delivery_review']['status'] == 'reviewed')):
                    interrupted.append((kwargs['inspection_root'], result['inspection_source']))
                    raise KeyboardInterrupt('complete revision inspection before result assembly')
                return result
            with patch.object(writing_module, 'inspect_article', side_effect=interrupt_after_inspection):
                stopped, incomplete = self._perform(action, stop_tool=target)
            diagnostic = {'status': stopped, 'observations': incomplete,
                'provider_errors': self.provider_errors,
                'consumer_calls': {'selection': selection_calls, 'writer': writer_calls, 'reviewer': reviewer_calls},
                'queued_consumers': [consumer.__name__ for consumer in self.responses]}
            # Domain anchor rejection happens AFTER provider transport, so it
            # need not appear in provider_errors. Retain the original private
            # validation reason in failure output, before a format correction
            # or an earlier adopted review can hide which assertion was rejected.
            diagnostic['review_rejections'] = {path.relative_to(self.root).as_posix(): json.loads(path.read_text())
                for path in (self.root / 'tools/writing').glob('*/writer/review/**/rejection.json')}
            self.assertEqual(stopped['status'], 'paused', diagnostic)
            self.assertEqual(len(interrupted), 1, diagnostic)
            self.assertFalse(self._completed_compositions(incomplete), incomplete)
            pending = self.app.state['pending']
            self.assertIsNotNone(pending, stopped)
            self.assertEqual(pending['call']['function']['name'], 'compose_document')
            identity = pending['attempt']
            owner, proof = interrupted[0]
            self.assertFalse((owner / 'result.json').exists())
            checkpoint = owner / 'writer/review/checkpoint.json'
            if revision_defect:
                history = json.loads(checkpoint.read_text())['iterations']
                self.assertFalse(any(row['action'] == 'document_joint_verify' for row in history))
            proof_bytes = {owner / proof[kind + '_path']: (owner / proof[kind + '_path']).read_bytes()
                for kind in ('request', 'response')}
            recorded_request = json.loads(proof_bytes[owner / proof['request_path']])
            recorded_response = json.loads(proof_bytes[owner / proof['response_path']])
            self.assertEqual(recorded_request['response_record_contract'], 'atomic_complete.v2')
            for key, value in (('request_sha256', recorded_request),
                    ('response_sha256', recorded_response['response'])):
                self.assertEqual(recorded_response[key], hashlib.sha256(
                    json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
            ledger, calls = self.app.ledger.to_dict(), self.send.call_count
            ledger_bytes = self.app.ledger.storage_path.read_bytes()
            processes = {path: path.read_bytes()
                for path in (self.root / 'tools/processes').glob('*/invocation.json')}
            self.app = ToolApplication.load(self.root, services=self.services)
            self.assertEqual(self.app.state['pending']['attempt'], identity)
            self.assertEqual(self.app.ledger.to_dict(), ledger)
            status, rows = self._perform(None, stop_tool=target)
            self.assertEqual(self.send.call_count, calls)
            self.assertEqual(self.app.ledger.to_dict(), ledger)
            self.assertEqual(self.app.ledger.storage_path.read_bytes(), ledger_bytes)
            self.assertEqual({path: path.read_bytes()
                for path in (self.root / 'tools/processes').glob('*/invocation.json')}, processes)
            self.assertEqual({path: path.read_bytes() for path in proof_bytes}, proof_bytes)
            self.assertTrue(rows and rows[-1]['status'] == 'ok', (status, rows))
            history = json.loads(checkpoint.read_text())['iterations']
            if revision_defect:
                verification, = [row for row in history if row['action'] == 'document_joint_verify']
                self.assertEqual(verification['status'], 'failed')
                self.assertTrue(all(not row['adopted'] for row in history
                    if row['action'] == 'document_joint_revise'))
            else:
                self.assertTrue(resume)
                self.assertEqual(history, [],
                    'Recovered successful producer reuse must not create another prose revision or verification round')
        else:
            status, rows = self._perform(action, stop_tool=target)
        self.assertEqual(status['status'], 'paused', status)
        self.assertTrue(any(row['tool_name'] == 'compose_document' for row in rows), (status, rows))
        composed, = self._completed_compositions(rows)
        self.assertEqual(composed['status'], 'ok', composed)
        self.assertFalse(self.responses, 'Expected Writer/reviewer was not actually consumed')
        if resume:
            self.assertEqual((selection_calls, writer_calls, reviewer_calls), (0, 1, 1))
            self.assertEqual(Path(composed['data']['version']).name, self._writing_owner(identity).name)
            review = json.loads(self.app.workspace.resolve(composed['data']['review_path']).read_text())
            self.assertEqual(review['inspection_source'], proof)
        manuscript = self.app.workspace.resolve(composed['data']['editable_markdown']).read_text(encoding='utf-8')
        if revision_defect:
            self.assertEqual((writer_calls, reviewer_calls), (2, 3))
            self.assertEqual(sum(e.actual.get('process_invocations', 0)
                for e in self.app.ledger.entries),
                process_count + self._preparation_process_usage(preparation_start))
            self.assertEqual({path: Path(path).read_bytes() for path in attachment_bytes}, attachment_bytes)
            # The improved complete proposal is the public work candidate,
            # while its new material defect still prevents atomic adoption.
            self.assertEqual(manuscript.splitlines(),
                [line.rstrip() for line in self.correct_prose.splitlines()])
            self.assertTrue(composed['data']['editable_markdown'].endswith('/candidate.md'))
            self.assertFalse(composed['data']['retained_candidate']['adopted'])
            self.assertTrue(composed['data']['unresolved'])
            with self.assertRaisesRegex(ValueError, 'Unresolved work'):
                self.app.writing.check_delivery(composed['data'])
            review = json.loads(self.app.workspace.resolve(composed['data']['review_path']).read_text())
            source = review['inspection_source']
            owner = self.root / 'tools/writing' / Path(composed['data']['version']).name
            original_author = json.loads((owner / 'writer/writer.json').read_text())['sections'][0]['draft_markdown']
            self.assertEqual(original_author.splitlines(), initial_body.splitlines(),
                'Saving a negative work candidate must not replace the original author or adopt it')
            self.assertEqual(Path(composed['data']['version']).name, self._writing_owner(identity).name)
            self.assertEqual(source, proof)
            self.assertIn('/inspection/', source['request_path'])
            self.assertIn('revision-', source['request_path'])
            for kind in ('request', 'response'):
                path = owner / source[kind + '_path']
                self.assertTrue(path.resolve().is_relative_to(owner.resolve()))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source[kind + '_sha256'])
            inspected = json.loads(json.loads((owner / source['request_path']).read_text())['prompt'])
            self.assertEqual(inspected['sections'][0]['markdown'], self.correct_prose)
        for (model, policy), value in self.expected.items():
            expected = f'{value:.3f}'
            self.assertIn(f'{model}/{policy}: {expected}', manuscript)
        self.assertIn(caption, manuscript)
        copied_figure = self.app.workspace.resolve(composed['data']['version'] + '/figures/selected-1.png')
        self.assertEqual(hashlib.sha256(copied_figure.read_bytes()).hexdigest(),
            hashlib.sha256((candidate / 'main.png').read_bytes()).hexdigest())
        if goal_findings:
            inspection = composed['data']['delivery_review']
            self.assertNotIn('declaration_adoption', inspection)
            self.assertEqual(inspection['producer_notes'], producer_declarations)
            self.assertEqual(json.loads(execution_path.read_bytes())['unresolved'], _PRODUCER_DECLARATIONS)
            self.latest_composition = composed
            return composed
        adoption = composed['data']['delivery_review']['declaration_adoption']
        self.assertEqual(adoption['producers'], producer_declarations)
        ids = [row['id'] for group in producer_declarations for row in group['declarations']]
        self.assertEqual(sorted(row['declaration_id'] for row in adoption['checks']), sorted(ids))
        self.assertEqual(len(adoption['checks']), len(ids))
        self.assertEqual({row['status'] for row in adoption['checks']},
            {'unresolved_work' if revision_defect or defect else 'satisfied_by_execution', 'accepted_limitation'})
        self.assertEqual(json.loads(execution_path.read_bytes())['unresolved'], _PRODUCER_DECLARATIONS)
        self.latest_composition = composed
        return composed

    def _preparation_process_usage(self, start=0):
        identities = {Path(row['data']['project']).name for row in self.preparations[start:]}
        return sum(entry.actual.get('process_invocations', 0) for entry in self.app.ledger.entries
            if entry.attempt_id in identities)

    def _approve_publication(self, *, crash=False):
        pending = self.app.state['pending']
        self.assertEqual(pending['call']['function']['name'], 'export_report')
        token = self.app.approval_token(pending['call'])
        self.app = ToolApplication.load(self.root, services=self.services)
        count = len(self._observations())
        options = {'approved_execution': token, 'progress': lambda _: None,
            'should_stop': lambda: len(self._observations()) > count}
        if crash:
            with _interrupt_outcome_commit():
                self.app.run(**options)
            self.assertIsNotNone(self.app.state['pending'])
            ledger = self.app.ledger.to_dict()
            receipts = {path: path.read_bytes() for path in (self.root / 'tools/processes').glob('*/invocation.json')}
            calls = self.send.call_count
            self.app = ToolApplication.load(self.root, services=self.services)
            self.app.run(**options)
            self.assertEqual(self.app.ledger.to_dict(), ledger)
            self.assertEqual({path: path.read_bytes() for path in receipts}, receipts)
            self.assertEqual(self.send.call_count, calls)
        else:
            self.app.run(**options)
        result = self._observations()[-1]
        self.assertEqual((result['tool_name'], result['status']), ('export_report', 'ok'), result)
        export_id = Path(result['data']['project']).name
        physical = [json.loads(path.read_text())
            for path in (self.root / 'tools/processes').glob(export_id + '-render-*/invocation.json')]
        self.assertEqual(sum('--from=json' in row['argv'] and '--to=latex' in row['argv']
            for row in physical), 2, 'Only body and standalone TeX conversions are necessary')
        return result

    def _publish(self, *, crash=False):
        result = self._approve_publication(crash=crash)
        self.assertNotIn('candidate_preparation', result['data'])
        self.assertEqual(result['data']['report_identity']['composition'],
            self.latest_composition['data']['version'])
        exported = self.app.workspace.resolve(result['data']['project'] + '/source.md').read_text(encoding='utf-8')
        self.assertIn('2.526', exported)
        self.assertNotIn('3.026', exported)
        manifest = result['data']['manifest']
        self.assertEqual(manifest['compiled'], self.compile_pdf)
        if self.compile_pdf:
            self.assertTrue(manifest['pdf_preview']['rendered'])
            import subprocess
            text = subprocess.check_output(['pdftotext', str(self.app.workspace.resolve(
                result['data']['project'] + '/main.pdf')), '-'], text=True)
            self.assertIn('2.526', text)
            self.assertNotIn('3.026', text)
        return result

    def _deliver(self, candidate, composition, publication=None):
        artifacts = [*composition['artifacts'], *[str(candidate / name) for name in (
            'plot_results.py', 'comparison.md', 'figure-data.csv', 'main.png', 'cost-scope.json')],
            *(publication['artifacts'] if publication else [])]
        return self._perform({'tool_name': 'deliver_result', 'arguments': {
            'summary': 'Saved-result report with jointly inspected text materials; no new experiment.',
            'artifacts': artifacts, 'report': {'status': 'canonical',
                'manuscript': composition['data']['editable_markdown']}}})

    def _report_chain(self, *, compile_pdf):
        # This precision chain covers saved explicit-input owner.v1 semantics:
        # exact original attachment subjects, repair resubmission and historical
        # recovery. New tasks default to owner.v3; freeze the old fixture before
        # its first action and reload through the real saved-contract consumer.
        # The separate work-order test must continue creating owner.v3 tasks.
        self.assertFalse(self.app.state['turns'])
        self.app.state['config']['professional_inputs'] = 'owner.v1'
        self.app.state['config']['professional_work'] = 'owners.v3'
        self.app.state['config'].pop('producer_completion')
        # Frozen pre-preparation protocol: these assertions prove exact old
        # adoption/repair recovery, not acceptance of the current work order.
        self.app.state['config']['publication_preparation'] = None
        self.app._save()
        self.app = ToolApplication.load(self.root, services=self.services)
        self.assertEqual(self.app.state['config']['professional_inputs'], 'owner.v1')
        self.assertIsNone(self.app.state['config']['publication_preparation'])
        self.assertTrue(self.app.writing.professional_inputs)
        self.assertFalse(self.app.writing.document_work)
        self.compile_pdf = compile_pdf
        self.assertEqual(self.app.state['config']['professional_work'], 'owners.v3')
        status, rows = self._perform({'tool_name': 'update_work_plan', 'arguments': {
            'report_intent': {'request_index': 0, 'quote': self.app.state['requests'][0],
                'kind': 'report', 'method': 'article', 'formats': ['markdown', 'pdf' if compile_pdf else 'tex'],
                'professional_review': {'max_revision_rounds': 1}}}})
        self.assertEqual(rows[-1]['status'], 'ok', rows)
        wrong, _ = self._produce(wrong_table=True)
        self.assertEqual(len(self._audit_products(wrong)), 1)
        bad = self._compose(wrong, defect=True, revision_defect=True)
        self.assertTrue(any(row['status'] == 'unresolved_work'
            for row in bad['data']['delivery_review']['declaration_adoption']['checks']))
        self.assertTrue(self.app.implementation.delivery_issues(self._observations()),
            'Actual unfinished producer work must remain blocked without independent adoption')
        self.assertEqual(len(bad['data']['delivery_review']['defects']), 1,
            'Correct counter-evidence must not become a second attachment defect')
        defect = bad['data']['delivery_review']['defects'][0]
        self.assertEqual(defect['finding']['finding_id'], 'wrong-delivered-table')
        self.assertEqual([self.app.workspace.resolve(row['path']) for row in defect['targets']],
            [wrong / 'comparison.md'])
        self.assertFalse(defect['finding'].get('draft_quotes'))
        self.assertIsNotNone(defect['targets'][0]['producer'])
        # Consume the actual revision-inspection proof now, before a later
        # prose-only composition can inherit that defect under another receipt.
        from simple_ar.tools.implementation_work import review_repair_request
        repair, diagnosis = review_repair_request(self.app.implementation,
            f"records/{Path(bad['data']['version']).name}/native_result.json")
        self.assertEqual(diagnosis, bad)
        self.assertEqual(self.app.workspace.resolve(repair['project_path']), wrong)
        before = self.app.ledger.to_dict()
        _, rejected = self._deliver(wrong, bad)
        self.assertEqual(rejected[-1]['status'], 'blocked', rejected)
        self.assertEqual(self.app.ledger.to_dict(), before)
        self.assertFalse(self.app.session.store.exists('delivery.json'))
        calls = sum(e.actual.get('process_invocations', 0) for e in self.app.ledger.entries)
        prose = self._compose(wrong, prior=bad['data']['editable_markdown'], prose_only=True)
        self.assertEqual(prose['data']['delivery_review']['status'], 'needs_revision')
        self.assertIn(defect, prose['data']['delivery_review']['defects'])
        self.assertEqual(sum(e.actual.get('process_invocations', 0) for e in self.app.ledger.entries), calls)
        self.assertIsNone(self.app.state['pending'])
        retained_bytes = {}
        retained_bytes.update({path: path.read_bytes() for path in wrong.rglob('*') if path.is_file()})
        for composition in (bad, prose):
            identity = Path(composition['data']['version']).name
            record = self.app.workspace.resolve(f'records/{identity}/native_result.json')
            retained_bytes[record] = record.read_bytes()
            for directory in (self.root / 'tools/writing' / identity,
                    self.app.workspace.resolve(composition['data']['version'])):
                retained_bytes.update({path: path.read_bytes() for path in directory.rglob('*') if path.is_file()})
        old_inputs = {self.app.workspace.resolve(row['path']): row for row in prose['data']['inputs']}
        # Resume the actual application, which must journal the defect-derived
        # prepare action. No caller invents a replacement goal or repair binding.
        corrected, executed = self._produce(prior=wrong, review_repair=True)
        self.assertEqual(self._audit_products(corrected), [])
        resubmission = self.app.writing.resubmit_after_repair(executed)
        self.assertIsNotNone(resubmission, 'The actual producer repair must return to joint inspection')
        self.assertEqual(resubmission['arguments']['prior_draft'], prose['data']['editable_markdown'])
        self.assertTrue(resubmission['arguments']['reuse_prior_inputs'])
        self.assertEqual(resubmission['arguments']['producer_repair'], executed['data']['execution_ref'])
        for field in ('material_roots', 'materials', 'sources'):
            self.assertFalse(resubmission['arguments'].get(field))
        before, calls = self.app.ledger.to_dict(), self.send.call_count
        self.assertEqual(self.app.writing.resubmit_after_repair(executed), resubmission)
        self.assertEqual(self.app.ledger.to_dict(), before)
        self.assertEqual(self.send.call_count, calls)
        accepted = self._compose(corrected, prior=prose['data']['editable_markdown'], publish=True,
            resume=True, historical_project=wrong)
        self.assertEqual(self.send.call_count - calls, 2, 'Repair consumes Writer/reviewer only, including reload')
        self.assertEqual({path: path.read_bytes() for path in retained_bytes}, retained_bytes)
        owner = self.root / 'tools/writing' / Path(accepted['data']['version']).name
        self.assertFalse((owner / 'selection-input.json').exists())
        binding = json.loads((owner / 'request.json').read_text())
        linkage = binding['prior_input_reuse']
        recipe = linkage['producer_repair']
        self.assertEqual(recipe['contract'], 'producer_recipe.v1')
        self.assertEqual(recipe['execution_ref'], executed['data']['execution_ref'])
        self.assertEqual(self.app.workspace.resolve(recipe['historical_project']), wrong)
        history, = binding['delivery_review']['prior_defect_refs']
        review_record = self.app.workspace.resolve(history['review_ref'])
        self.assertEqual(history['review_ref'], prose['data'].get('pending_candidate') or recipe['review_ref'])
        self.assertEqual(history['review_sha256'], hashlib.sha256(review_record.read_bytes()).hexdigest())
        located = json.loads(review_record.read_text())
        for part in history['pointer'].split('/')[1:]:
            located = located[int(part)] if isinstance(located, list) else located[part]
        self.assertEqual(located['finding_id'], 'wrong-delivered-table')
        self.assertEqual(history['finding']['finding_id'], 'wrong-delivered-table')
        self.assertEqual([self.app.workspace.resolve(row['path']) for row in history['targets']], [wrong / 'comparison.md'])
        self.assertEqual([self.app.workspace.resolve(row['path']) for row in history['current_targets']],
            [corrected / 'comparison.md'])
        self.assertEqual(recipe['execution_sha256'], hashlib.sha256(
            self.app.workspace.resolve(recipe['execution_ref']).read_bytes()).hexdigest())
        new_inputs = {self.app.workspace.resolve(row['path']): row for row in accepted['data']['inputs']}
        for name, raw in self.original_bytes.items():
            path = (self.inputs / name).resolve()
            self.assertEqual(new_inputs[path], old_inputs[path], 'Original evidence windows and hashes must survive reuse')
            self.assertEqual(new_inputs[path]['sha256'], hashlib.sha256(raw).hexdigest())
        replacements = {self.app.workspace.resolve(row['old_path']): row for row in recipe['replacements']}
        for name in ('plot_results.py', 'comparison.md', 'figure-data.csv', 'cost-scope.json'):
            replacement = replacements[wrong / name]
            path = self.app.workspace.resolve(replacement['path'])
            self.assertEqual(path, corrected / name)
            self.assertEqual(replacement['old_sha256'], old_inputs[wrong / name]['sha256'])
            self.assertEqual(replacement['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertIn(path, new_inputs)
            if name != 'plot_results.py':
                self.assertNotIn(wrong / name, new_inputs)
            # The old script may remain an actual declared input of the new
            # producer; recipe replacement must not erase its real provenance.
        self.assertEqual({self.app.workspace.resolve(row['path']) for row in linkage['inputs']
            if self.app.workspace.resolve(row['path']).is_relative_to(self.app.workspace.workspace)},
            {corrected / name for name in ('plot_results.py', 'comparison.md', 'figure-data.csv', 'cost-scope.json')})
        self.assertEqual(accepted['data']['delivery_review']['status'], 'reviewed')
        identity = Path(accepted['data']['version']).name
        proposal = self.app.writing.continuation(accepted, identity)
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal['arguments'], json.loads(
            self.app.state['pending']['call']['function']['arguments']))
        # Exercise version refusal against real owner-produced receipts/assets,
        # replacing the old mock-hash "successful continuation" fixture.
        for locator in (accepted['data']['editable_markdown'], accepted['data']['review_path']):
            path = self.app.workspace.resolve(locator)
            with changed_record(path, path.read_bytes() + b'\nchanged-version'):
                self.assertIsNone(self.app.writing.continuation(accepted, identity))
        first_export = self._publish(crash=True)
        # Reuse the complete real manuscript/review/export and physical producer
        # receipts. These are proposed delivery variants, not fabricated passes.
        delivery = {'summary': 'Jointly reviewed recorded-result report',
            'artifacts': [*accepted['artifacts'], *[str(corrected / name) for name in (
                'plot_results.py', 'comparison.md', 'figure-data.csv', 'main.png', 'cost-scope.json')],
                *first_export['artifacts']],
            'report': {'status': 'canonical', 'manuscript': accepted['data']['editable_markdown']}}
        adoptions = self.app.writing.delivery_adoptions(self._observations(), delivery)
        self.assertEqual(adoptions, accepted['data']['delivery_review']['declaration_adoption']['producers'])
        self.assertTrue(adoptions)
        self.assertFalse(self.app.implementation.delivery_issues_for(self._observations(), delivery, adoptions))
        self.assertTrue(self.app.implementation.delivery_issues_for(self._observations(), delivery, []),
            'Successful execution alone must not erase the original declarations')
        no_manuscript = json.loads(json.dumps(delivery))
        manuscript_path = self.app.workspace.resolve(delivery['report']['manuscript'])
        no_manuscript['artifacts'] = [path for path in delivery['artifacts']
            if self.app.workspace.resolve(path) != manuscript_path]
        self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), no_manuscript), [])
        wrong_producer = json.loads(json.dumps(delivery))
        wrong_producer['artifacts'] = [str(wrong / self.app.workspace.resolve(path).relative_to(corrected))
            if self.app.workspace.resolve(path).is_relative_to(corrected) else path for path in delivery['artifacts']]
        self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), wrong_producer), [])
        self.assertTrue(self.app.implementation.delivery_issues_for(
            self._observations(), wrong_producer, adoptions),
            'A later executed/reviewed B cannot adopt explicitly delivered A under B receipts')
        prepared_path = self.app.workspace.resolve(adoptions[0]['prepared_result'])
        # Fault only the read-only status view of ACTUAL preparation entries;
        # do not fabricate physical calls, settle unknown use or edit the ledger.
        from unittest.mock import PropertyMock
        from simple_ar.tools.producer_declarations import declaration_group
        ledger = self.app.ledger
        entries = ledger.entries
        preparation_id = prepared_path.parent.parent.name
        preparation_entries = [entry for entry in entries if entry.attempt_id == preparation_id
            and entry.actual.get('llm_requests')]
        self.assertTrue(preparation_entries, 'The adopted producer must have a real charged preparation')
        self.assertTrue(all(entry.status == 'settled' for entry in preparation_entries))
        ledger_bytes = ledger.storage_path.read_bytes()
        ledger_before, calls = ledger.to_dict(), self.send.call_count
        for status in ('unknown', 'reserved'):
            altered = tuple(replace(entry, status=status) if entry in preparation_entries else entry
                for entry in entries)
            with self.subTest(prepare_accounting=status), patch.object(type(ledger), 'entries',
                    new_callable=PropertyMock, return_value=altered):
                with self.assertRaises(ValueError):
                    declaration_group(self.app.writing, adoptions[0]['execution_ref'])
                self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), delivery), [])
        self.assertEqual(ledger.storage_path.read_bytes(), ledger_bytes)
        self.assertEqual(ledger.to_dict(), ledger_before)
        self.assertEqual(self.send.call_count, calls)
        for path in (manuscript_path, corrected / 'comparison.md',
                self.inputs / 'overall_metrics.csv', prepared_path.parent / 'request.json'):
            with self.subTest(changed_adoption_source=str(path)), changed_record(path, path.read_bytes() + b'\nchanged-version'):
                self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), delivery), [])
        # Isolate the historical contract guard using the real receipt chain;
        # no saved request, approved source, review response or ledger is edited.
        from simple_ar.tools import implementation_work
        read_request = implementation_work._retained_request
        def historical_request(source):
            return read_request(source).model_copy(update={'work_contract': 'shared.v1'})
        with patch.object(implementation_work, '_retained_request', side_effect=historical_request):
            self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), delivery), [])
            self.assertTrue(self.app.implementation.delivery_issues_for(self._observations(), delivery, adoptions))
        self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), delivery), adoptions)
        for group in adoptions:
            execution = json.loads(self.app.workspace.resolve(group['execution_ref']).read_bytes())
            prepared = json.loads(self.app.workspace.resolve(group['prepared_result']).read_bytes())
            self.assertEqual(execution['unresolved'], _PRODUCER_DECLARATIONS)
            self.assertEqual(execution['approval']['binding']['unresolved'], _PRODUCER_DECLARATIONS)
            self.assertEqual(prepared['data']['unresolved'], _PRODUCER_DECLARATIONS)
        status, _ = self._deliver(corrected, accepted, first_export)
        self.assertEqual(status['status'], 'completed', status)
        first_files = {path: path.read_bytes() for parent in (corrected,
            self.app.workspace.resolve(accepted['data']['version']),
            self.app.workspace.resolve(first_export['data']['project']))
            for path in parent.rglob('*') if path.is_file()}
        ledger = self.app.ledger.to_dict()
        calls = self.send.call_count
        self.app = ToolApplication.load(self.root, services=self.services)
        self.assertEqual(self.app.run(progress=lambda _: None)['status'], 'completed')
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        feedback = ('Revise the main figure for grayscale using distinct markers and line styles. '
            'Update the caption and affected prose, preserve the original version and jointly review the new materials. '
            'Reuse the saved measurements without fitting; policy-level costs remain unknown.')
        revised, _ = self._produce(prior=corrected, grayscale=True, feedback=feedback,
            allowances={'llm_requests': 12, 'total_tokens': 350000,
                'process_invocations': 16, 'process_wall_seconds': 360})
        self.assertEqual(self._audit_products(revised, grayscale=True), [])
        self.assertNotEqual((corrected / 'main.png').read_bytes(), (revised / 'main.png').read_bytes())
        second = self._compose(revised, prior=accepted['data']['editable_markdown'], grayscale=True, publish=True)
        second_export = self._publish()
        before = self.app.ledger.to_dict()
        _, rejected = self._deliver(revised, second, first_export)
        self.assertEqual(rejected[-1]['status'], 'blocked', rejected)
        self.assertEqual(self.app.ledger.to_dict(), before)
        status, delivered = self._deliver(revised, second, second_export)
        self.assertEqual(status['status'], 'completed', (status, delivered))
        self.assertNotEqual(first_export['data']['project'], second_export['data']['project'])
        self.assertEqual({path: path.read_bytes() for path in first_files}, first_files)
        self.assertEqual(len(self.app.state['requests']), 2)
        self._assert_accounting()
        self.assertFalse(self.responses)
        self.assertEqual(self.app._work_focus()['purpose'], 'task')
        self.assertEqual(self.app.state['config']['professional_inputs'], 'owner.v1')
        self.assertFalse(self.app.writing.document_work)
        self.coordinator.assert_not_called()

    @unittest.skipUnless(shutil.which('pandoc'), 'Real offline publication requires installed Pandoc')
    def test_goal_findings_report_inspects_original_notes_without_adoption_and_replays(self):
        self.assertEqual(self.app.state['config']['producer_completion'], 'goal_findings.v1')
        self.app = ToolApplication.load(self.root, services=self.services)
        self.assertIsNone(self.app.writing.declaration_adoption_contract)
        _, rows = self._perform({'tool_name': 'update_work_plan', 'arguments': {
            'report_intent': {'request_index': 0, 'quote': self.app.state['requests'][0],
                'kind': 'report', 'method': 'article', 'formats': ['markdown', 'tex'],
                'professional_review': {'max_revision_rounds': 1}}}})
        self.assertEqual(rows[-1]['status'], 'ok', rows)
        candidate, _ = self._produce()
        composed = self._compose(candidate)
        self.assertEqual(composed['data']['delivery_review']['status'], 'reviewed')
        self.app.writing.check_delivery(composed['data'])
        calls, ledger = self.send.call_count, self.app.ledger.to_dict()
        self.app = ToolApplication.load(self.root, services=self.services)
        self.app.writing.check_delivery(composed['data'])
        self.assertEqual(self.send.call_count, calls)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.app.writing.delivery_adoptions(self._observations(), {
            'summary': 'Current independently inspected report', 'artifacts': composed['artifacts'],
            'report': {'status': 'canonical', 'manuscript': composed['data']['editable_markdown']}}), [])
        owner = self.root / 'tools/writing' / Path(composed['data']['version']).name
        binding = json.loads((owner / 'request.json').read_bytes())
        binding['delivery_review']['producer_notes'][0]['notes'][0] = 'silently replaced note'
        with changed_record(owner / 'request.json', json.dumps(binding).encode()):
            with self.assertRaises(ValueError):
                self.app.writing.check_delivery(composed['data'])
        self.assertEqual(self.send.call_count, calls)
        self._assert_accounting()

    @unittest.skipUnless(shutil.which('pandoc'), 'Candidate preparation requires real Pandoc')
    def test_goal_findings_report_still_rejects_real_prose_and_attachment_defects(self):
        self.assertEqual(self.app.state['config']['producer_completion'], 'goal_findings.v1')
        self.app = ToolApplication.load(self.root, services=self.services)
        _, rows = self._perform({'tool_name': 'update_work_plan', 'arguments': {
            'report_intent': {'request_index': 0, 'quote': self.app.state['requests'][0],
                'kind': 'report', 'method': 'article', 'formats': ['markdown', 'tex'],
                'professional_review': {'max_revision_rounds': 1}}}})
        self.assertEqual(rows[-1]['status'], 'ok', rows)
        candidate, _ = self._produce(wrong_table=True)
        composed = self._compose(candidate, defect=True, revision_defect=True)
        self.assertTrue(composed['data']['unresolved'])
        self.assertNotIn('declaration_adoption', composed['data']['delivery_review'])
        with self.assertRaises(ValueError):
            self.app.writing.check_delivery(composed['data'])
        self._assert_accounting()

    @unittest.skipUnless(shutil.which('pandoc'), 'Professional report publication requires existing Pandoc')
    def test_goal_findings_default_full_chain_repairs_reviews_publishes_and_delivers(self):
        self.assertEqual(self.app.state['config']['producer_completion'], 'goal_findings.v1')
        self.assertEqual(self.app.state['config']['professional_inputs'], 'owner.v3')
        self.assertEqual(self.app.state['config']['professional_work'], 'requests.v1')
        self.assertTrue(self.app.writing.document_work)
        self.assertIsNone(self.app.writing.declaration_adoption_contract)
        self.compile_pdf = False
        _, rows = self._perform({'tool_name': 'update_work_plan', 'arguments': {
            'report_intent': {'request_index': 0, 'quote': self.app.state['requests'][0],
                'kind': 'report', 'method': 'article', 'formats': ['markdown', 'tex'],
                'professional_review': {'max_revision_rounds': 1}}}})
        self.assertEqual(rows[-1]['status'], 'ok', rows)
        wrong, _ = self._produce(wrong_table=True)
        bad = self._compose(wrong, defect=True)
        self.assertTrue(bad['data']['delivery_review']['defects'])
        self.assertTrue(self.app.implementation.delivery_issues_for(self._observations(), {
            'summary': 'Omitting prose cannot hide a known defective table',
            'artifacts': [str(wrong / 'comparison.md')]}, []))
        # A dispatched blocked closing is a request-owner decision boundary,
        # not permission to rewind to an older professional observation. Prove
        # rejection through the same canonical-delivery validator without
        # replacing the latest compose outcome needed by its continuation.
        from simple_ar.tools.workspace import ReportDelivery
        artifacts = [*bad['artifacts'], *[str(wrong / name) for name in (
            'plot_results.py', 'comparison.md', 'figure-data.csv', 'main.png', 'cost-scope.json')]]
        state, ledger, calls = json.dumps(self.app.state, sort_keys=True), self.app.ledger.to_dict(), self.send.call_count
        with self.assertRaisesRegex(ValueError, 'unresolved|review|inspection'):
            self.app._validated_report_delivery(artifacts, claim=ReportDelivery(
                status='canonical', manuscript=bad['data']['editable_markdown']))
        self.assertEqual(json.dumps(self.app.state, sort_keys=True), state)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        self.assertFalse(self.app.session.store.exists('delivery.json'))
        corrected, executed = self._produce(prior=wrong, review_repair=True)
        self.assertEqual(self._audit_products(corrected), [])
        self.assertIsNotNone(self.app.writing.resubmit_after_repair(executed))
        accepted = self._compose(corrected, prior=bad['data']['editable_markdown'],
            publish=True, resume=True, historical_project=wrong)
        self.app.writing.check_delivery(accepted['data'])
        self.assertNotIn('declaration_adoption', accepted['data']['delivery_review'])
        publication = self._publish(crash=True)
        status, delivered = self._deliver(corrected, accepted, publication)
        self.assertEqual(delivered[-1]['status'], 'ok', delivered)
        self.assertEqual(status['status'], 'completed', status)
        self.assertEqual(self.original_bytes,
            {path.name: path.read_bytes() for path in self.inputs.iterdir()})
        self._assert_accounting()

    def test_professional_report_chain_material_repair_feedback_and_recovery(self):
        self._report_chain(compile_pdf=False)

    @unittest.skipUnless(os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1' and
        all(shutil.which(name) for name in ('pandoc', 'pdflatex', 'bibtex', 'pdfinfo', 'pdftoppm', 'pdftotext')),
        'Server opt-in requires real Pandoc, acmart-equipped TeX and Poppler')
    def test_professional_report_chain_real_pdf_two_versions(self):
        self._report_chain(compile_pdf=True)

    @unittest.skipUnless(shutil.which('pandoc'), 'Document-work integration requires real Pandoc')
    def test_document_owner_selects_packages_publishes_and_finishes_two_versions(self):
        """Structural takeover check, not evidence of autonomous model selection.

        Existing code fixtures prepare two real, independently checked material
        directories BEFORE the document task. Only professional provider
        transport is controlled; no writing, copy, review or export owner is
        replaced. The same fixture also exercises native request-owner entry,
        feedback and publication without substituting the domain owners.
        """
        self.compile_pdf = os.environ.get('SAR_TEST_EXPORT_INTEGRATION') == '1'
        if self.compile_pdf and not all(shutil.which(name) for name in
                ('pdflatex', 'bibtex', 'pdfinfo', 'pdftoppm', 'pdftotext')):
            self.skipTest('Requested real PDF variant requires existing TeX and Poppler')
        color, _ = self._produce()
        gray, _ = self._produce(prior=color, grayscale=True)
        self.assertEqual(self._audit_products(color), [])
        self.assertEqual(self._audit_products(gray, grayscale=True), [])
        self.assertFalse(self.responses)
        fixture_calls = self.send.call_count
        mounted_bytes = {path: path.read_bytes() for root in (self.inputs, color, gray)
                         for path in root.rglob('*') if path.is_file()}
        # Mount already-created artifacts as ordinary supplied materials, not
        # producer-certified results of this new task. The document owner must
        # inspect and package their actual bytes itself.
        self.root = self.directory / 'document-task'
        goal = ('Use these saved measurements and existing analysis products to deliver '
            'an editable report with the source, tables, data and main figure. '
            'Independently inspect the bundle, preserve recorded values and explain '
            'unknown policy costs. Do not fit models or rerun experiments.')
        self.app = ToolApplication.create(self.root, goal, model='fixture',
            sources={'results': self.inputs, 'color': color, 'gray': gray}, services=self.services,
            creation_declarations={'with_report': True,
                'material_roles': {'data_path': ['results'], 'material': ['color', 'gray']},
                'data_meanings': {'timing_scope': 'supplied saved model costs, not policy costs'}},
            professional_inputs=True, report_protocol='canonical_report.v1', allow_execution=True,
            max_turns=18, max_tokens=700000, max_attempts=100,
            # Each version renders once for inspection and once for final
            # publication; the opt-in PDF variant includes both full renders.
            process_limits={'process_invocations': 64, 'process_wall_seconds': 480})
        self.assertEqual(self.app.state['config']['professional_inputs'], 'owner.v3')
        self.assertEqual(self.app.state['config']['professional_work'], 'requests.v1')
        self.assertEqual(self.app.state['config']['publication_preparation'], 'bound_candidate.v1')
        self.enterContext(patch.object(LLMClient, 'chat_turn', self.native_chat_turn))
        feedback = ('Use the supplied grayscale figure version, revise the caption and affected '
            'prose consistently, and keep the old report and all original materials. '
            'Reuse the measured values without fitting or regenerating the analysis.')
        previous = None
        retained_files = {}
        versions, publications = [], []
        title = 'Recorded model-policy comparison'

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for child in value.values():
                    yield from strings(child)
            elif isinstance(value, list):
                for child in value:
                    yield from strings(child)

        for phase, (mount, candidate) in enumerate((('color', color), ('gray', gray))):
            names = ('plot_results.py', 'comparison.md', 'figure-data.csv', 'cost-scope.json')
            text_paths = [*(self.inputs / name for name in self.original_bytes),
                          *(candidate / name for name in names)]
            package = [*text_paths, candidate / 'main.png']
            expected_bytes = {path.resolve(): path.read_bytes() for path in package}
            selection_calls = writer_calls = review_calls = 0
            prior_text = (self.app.workspace.resolve(previous).read_text(encoding='utf-8')
                          if previous else '')
            caption = ('Recorded RMSE; grayscale lines and distinct markers.' if phase else
                       'Recorded RMSE comparison from saved measurements.')
            copied_paths = set()
            expected_aliases = {}

            def select(system, prompt):
                nonlocal selection_calls
                selection_calls += 1
                self.assertLessEqual(selection_calls, 2)
                actual_context = prompt['work_context']['work_context']
                self.assertEqual(actual_context['user_requests'], self.app.state['requests'])
                self.assertEqual(actual_context['creation_declarations'],
                    self.app.state['config']['creation_declarations'])
                self.assertEqual(prompt['title'], '', 'Caller must not choose the document title')
                schema = next(row['function']['parameters'] for row in self.app.schemas
                              if row['function']['name'] == 'compose_document')
                self.assertFalse({'title', 'materials', 'figures', 'delivery_artifacts', 'goal'}
                                 & schema['properties'].keys())
                visible = {self.app.workspace.resolve(row[0])
                    for inventory in prompt['inventories'] for row in inventory['files']}
                self.assertTrue(set(expected_bytes) <= visible)
                windows = [{'path': str(path), 'max_bytes': 64000, 'full_digest': True}
                           for path in text_paths]
                observed = {self.app.workspace.resolve(row['request']['path']): row['data']
                            for row in prompt['observations']}
                missing = []
                for window in windows:
                    path = self.app.workspace.resolve(window['path'])
                    data = observed.get(path)
                    if data is None or data.get('has_more'):
                        missing.append(window)
                    else:
                        self.assertEqual(data['text'], expected_bytes[path].decode('utf-8'))
                        self.assertEqual(data['sha256'], hashlib.sha256(expected_bytes[path]).hexdigest())
                if missing:
                    self.responses.insert(0, select)
                    return {'read_windows': missing}
                return {'title': title, 'materials': windows,
                    'figures': [str(candidate / 'main.png')],
                    'delivery_artifacts': [str(path) for path in package]}

            def inspect_subjects(subjects):
                # Owner tests cover byte/copy/alias rules. This loop test keeps
                # the consumer handoff, without replaying every owner invariant.
                supplied = {self.app.workspace.resolve(row['path']): row for row in subjects}
                self.assertEqual(set(supplied), set(expected_bytes))
                self.assertFalse(set(expected_aliases) & supplied.keys())
                self.assertTrue(all(row['producer'] is None for row in subjects))

            def writer(system, prompt):
                nonlocal writer_calls, copied_paths, expected_aliases
                writer_calls += 1
                owner = self.root / 'tools/writing' / self.app.state['pending']['attempt']
                binding = json.loads((owner / 'request.json').read_text(encoding='utf-8'))
                self.assertEqual(binding['request']['goal'], '\n\n'.join(self.app.state['requests']))
                self.assertEqual(binding['request']['title'], title)
                self.assertTrue(binding['request']['finish'])
                copies = binding['document_work']['materials']
                self.assertEqual(binding['document_work']['contract'], 'document_work.v2')
                self.assertEqual({self.app.workspace.resolve(row['source']) for row in copies}, set(expected_bytes))
                copied_paths = {self.app.workspace.resolve(row['path']) for row in copies}
                self.assertEqual(len(copied_paths), len(expected_bytes))
                expected_aliases = {self.app.workspace.resolve(row['path']):
                    (self.app.workspace.resolve(row['source']), row['sha256']) for row in copies}
                # The selected figure has both a packaged material copy and
                # the canonical image used by this exact manuscript version.
                for row in binding['inputs']:
                    if 'manuscript_path' in row:
                        target = self.app.workspace.resolve('workspace/manuscripts/' + owner.name
                            + '/' + row['manuscript_path'])
                        expected_aliases[target] = (self.app.workspace.resolve(row['path']), row['sha256'])
                version_root = self.app.workspace.resolve('workspace/manuscripts/' + owner.name)
                for row in copies:
                    source = self.app.workspace.resolve(row['source'])
                    destination = self.app.workspace.resolve(row['path'])
                    self.assertTrue(destination.is_relative_to(version_root))
                    self.assertEqual(destination.read_bytes(), expected_bytes[source])
                    self.assertEqual(row['sha256'], hashlib.sha256(expected_bytes[source]).hexdigest())
                supplied = list(strings(prompt['source_handles']))
                for raw in self.original_bytes.values():
                    self.assertTrue(any(raw.decode('utf-8') in text for text in supplied))
                inspect_subjects(prompt['delivery_review']['artifacts'])
                self.assertEqual(prompt.get('prior_draft', ''), prior_text)
                facts = '; '.join(f'{model}/{policy}: {value:.3f}'
                    for (model, policy), value in self.expected.items())
                citations = ' '.join('[@' + row['id'] + ']' for row in prompt['bibliography'])
                body = ('# ' + title + '\n\n## Results\n\n' + facts + '. ' + citations
                    + '\n\n![' + caption + '](figures/selected-1.png)\n\n'
                    + ('The grayscale rendering preserves the recorded values.\n\n' if phase else '')
                    + '## Limitations\n\n' + _PRODUCER_DECLARATIONS[1]
                    + ' Only saved results were used; no model fitting or experiment rerun.\n')
                if previous:
                    return {'prior_draft_sha256': prompt['edit_contract']['prior_draft_sha256'],
                        'replacements': [{'old_text': prior_text, 'new_text': body}]}
                return {'draft_markdown': body}

            def reviewer(system, prompt):
                nonlocal review_calls
                review_calls += 1
                inspect_subjects(restore_delivery_producers(prompt))
                # The mounted old products have no producer authority in this
                # task. Independent review must actually see original records.
                self.assertFalse(prompt.get('producer_declarations'))
                evidence = list(strings(prompt['source_evidence']))
                for raw in self.original_bytes.values():
                    self.assertTrue(any(raw.decode('utf-8') in text for text in evidence))
                section, = prompt['sections']
                for (model, policy), value in self.expected.items():
                    self.assertIn(f'{model}/{policy}: {value:.3f}', section['markdown'])
                self.assertIn(caption, section['markdown'])
                self.assertIn(_PRODUCER_DECLARATIONS[1], section['markdown'])
                return {'section_reviews': [{'section_id': section['section_id'],
                    'verdict': 'pass', 'findings': []}]}

            arguments = {'material_roots': ['results', mount], 'finish': True,
                'publication': {'compile_pdf': self.compile_pdf, 'render_preview': self.compile_pdf}}
            if previous:
                arguments['prior_draft'] = previous
            self.assertFalse({'title', 'goal', 'materials', 'figures', 'delivery_artifacts'} & arguments.keys())
            actions = [{'tool_name': 'compose_document', 'arguments': arguments}]
            if not phase:
                actions.insert(0, {'tool_name': 'update_work_plan', 'arguments': {
                    'report_intent': {'request_index': 0, 'quote': goal, 'kind': 'report',
                        'method': 'article', 'formats': ['markdown', 'pdf' if self.compile_pdf else 'tex'],
                        'professional_review': {'max_revision_rounds': 1}}}})
            def request_owner(system, context):
                self.assertEqual(context['requests'], [{'index': 0, 'text': goal}])
                self.assertEqual(context['request_count'], phase + 1)
                self.assertEqual(context['current_request_index'], phase)
                return {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': f'work-{phase}-{index}', 'type': 'function', 'function': {
                        'name': action['tool_name'], 'arguments': json.dumps(action['arguments'])}}
                    for index, action in enumerate(actions)]}
            self.responses.extend([request_owner, select, writer, reviewer])
            delivery_path = self.root / 'delivery.json'
            prior_delivery = delivery_path.read_bytes() if delivery_path.exists() else None
            status, observed = self._perform(None, feedback=feedback if phase else '',
                stop_tool='export_report')
            self.assertEqual(status['status'], 'paused', (status, observed, self.provider_errors))
            composition, = self._completed_compositions(observed)
            self.assertEqual(composition['status'], 'ok', composition)
            self.assertEqual([turn['origin'] for turn in self.app.state['turns']
                if turn.get('request_count') == phase + 1 and turn.get('origin') != 'professional'], ['request_owner'])
            self.assertEqual((writer_calls, review_calls), (1, 1), self.provider_errors)
            self.assertFalse(self.responses)
            self.assertEqual(composition['data']['title'], title)
            self.assertEqual(composition['data']['delivery_review']['status'], 'reviewed')
            self.assertEqual(self.app.state['pending']['call']['function']['name'], 'export_report')
            self.assertEqual(delivery_path.read_bytes() if delivery_path.exists() else None,
                prior_delivery, 'finish is an intention, not delivery before publication approval')
            self.latest_composition = composition
            publication = self._publish()  # Existing exact approval gate, real renderer.
            status = self.app.run(progress=lambda _: None)  # Publication closes without another decision.
            self.assertEqual(status['status'], 'completed', (status, self._observations(), self.provider_errors))
            self.assertNotEqual(delivery_path.read_bytes(), prior_delivery)
            delivered = json.loads((self.root / 'delivery.json').read_text(encoding='utf-8'))
            artifacts = {self.app.workspace.resolve(path) for path in delivered['artifacts']}
            self.assertTrue(copied_paths <= artifacts)
            previous = composition['data']['editable_markdown']
            self.assertIn(self.app.workspace.resolve(previous), artifacts)
            self.assertTrue({self.app.workspace.resolve(path) for path in publication['artifacts']} <= artifacts)
            self.assertTrue(set(expected_aliases) <= artifacts)
            inspection = composition['data']['delivery_review']['artifacts']
            self.assertEqual({self.app.workspace.resolve(row['path']) for row in inspection}, set(expected_bytes))
            reviewed_aliases = {self.app.workspace.resolve(alias['path']):
                (self.app.workspace.resolve(row['path']), alias['sha256'])
                for row in inspection for alias in row['copies']}
            self.assertEqual(reviewed_aliases, expected_aliases)
            # Both the complete mapping and the actual copied bytes remain
            # mandatory at the existing final-delivery gate, after publication.
            identity = publication['data']['report_identity']
            published = {self.app.workspace.resolve(path) for path in publication['artifacts']}
            self.app.writing.check_delivery_set(identity, delivered['artifacts'], published)
            gate_ledger, gate_calls = self.app.ledger.to_dict(), self.send.call_count
            # The public draft locator must resolve the actual completed
            # continuation, never its first publication-preparation receipt.
            from simple_ar.tools.writing import accepted_composition_receipt
            logical = Path(composition['data']['version']).name
            physical, receipt = self.app.writing._document_source_observation(logical)
            self.assertNotEqual(physical, logical)
            native = self.root / 'attempts' / physical / 'native_result.json'
            altered = {**receipt, 'data': {**receipt['data'], 'title': 'changed receipt'}}
            with changed_record(native, altered), self.assertRaisesRegex(ValueError, 'no exact completed'):
                self.app.writing._document_source_observation(logical)
            reservation = 'source-accounting-check'
            self.app.ledger.reserve(reservation, {'llm_requests': 1},
                session_id=self.app.session.manifest.session_id, attempt_id=physical,
                logical_call_id=reservation)
            try:
                for accounting in ('reserved', 'unknown'):
                    if accounting == 'unknown':
                        self.app.ledger.mark_unknown(reservation, reason='Fixture missing response')
                    with self.subTest(accounting=accounting), self.assertRaisesRegex(ValueError, 'unsettled'):
                        self.app.writing._document_source_observation(logical)
                    with self.assertRaisesRegex(ValueError, 'unsettled'):
                        accepted_composition_receipt(root=self.root, workspace=self.app.workspace,
                            session_id=self.app.session.manifest.session_id, ledger=self.app.ledger,
                            identity=physical, observed=receipt)
            finally:
                self.app.ledger._entries.pop(reservation)
                self.app.ledger.save()
            self.assertEqual(self.app.ledger.to_dict(), gate_ledger)
            self.assertEqual(self.send.call_count, gate_calls)
            owner_result = self.root / 'tools/writing' / Path(composition['data']['version']).name / 'result.json'
            changed = json.loads(owner_result.read_bytes())
            changed['data']['delivery_review']['artifacts'][0]['copies'].pop()
            with changed_record(owner_result, changed), self.assertRaisesRegex(ValueError, 'copy mapping'):
                self.app.writing.check_delivery_set(identity, delivered['artifacts'], published)
            target = next(path for path in copied_paths if path.suffix == '.py')
            with changed_record(target, target.read_bytes() + b'\n# changed after review\n'), \
                    self.assertRaisesRegex(ValueError, 'changed after inspection'):
                self.app.writing.check_delivery_set(identity, delivered['artifacts'], published)
            self.assertEqual(self.app.ledger.to_dict(), gate_ledger)
            self.assertEqual(self.send.call_count, gate_calls)
            self.app.writing.check_delivery_set(identity, delivered['artifacts'], published)
            self.assertEqual(self.app.workspace.resolve(composition['data']['version']
                + '/figures/selected-1.png').read_bytes(), expected_bytes[(candidate / 'main.png').resolve()])
            versions.append(composition['data']['version'])
            publications.append(publication['data']['project'])
            self.assertEqual({path: path.read_bytes() for path in retained_files}, retained_files)
            retained_files.update({path: path.read_bytes() for prefix in (versions[-1], publications[-1])
                for path in self.app.workspace.resolve(prefix).rglob('*') if path.is_file()})
            ledger, calls = self.app.ledger.to_dict(), self.send.call_count
            self.app = ToolApplication.load(self.root, services=self.services)
            self.assertEqual(self.app.state['config']['professional_inputs'], 'owner.v3')
            self.assertEqual(self.app.run(progress=lambda _: None)['status'], 'completed')
            self.assertEqual(self.app.ledger.to_dict(), ledger)
            self.assertEqual(self.send.call_count, calls)

        self.assertNotEqual(versions[0], versions[1])
        self.assertNotEqual(publications[0], publications[1])
        self.assertEqual(len(self.app.state['requests']), 2)
        self._assert_accounting(fixture_calls=fixture_calls)
        self.assertEqual({path: path.read_bytes() for path in mounted_bytes}, mounted_bytes)
        self.assertEqual({path.name: path.read_bytes() for path in self.inputs.iterdir()}, self.original_bytes)
        self.coordinator.assert_not_called()

    def test_exit_zero_missing_output_retains_receipt_without_replay(self):
        self._missing_output_recovery()

    def _missing_output_recovery(self):
        # This is the frozen zero-repair contract. New ordinary owners own
        # repairs against the shared budget rather than this historical cap.
        self.app.state['config']['professional_work'] = 'owners.v1'
        self.app._save()
        self.app = ToolApplication.load(self.root, services=self.services)
        missing, receipt = self._produce(missing=True)
        self.assertEqual(receipt['data']['returncode'], 0)
        old = {path.name: path.read_bytes() for path in missing.iterdir() if path.is_file()}
        ledger, calls = self.app.ledger.to_dict(), self.send.call_count
        self.app = ToolApplication.load(self.root, services=self.services)
        _, rows = self._perform({'tool_name': 'read_file', 'arguments': {
            'path': str(missing / 'cost-scope.json')}})
        self.assertEqual(rows[-1]['status'], 'ok', rows)
        self.assertEqual(self.app.ledger.to_dict(), ledger)
        self.assertEqual(self.send.call_count, calls)
        corrected, executed = self._produce(prior=missing)
        self.assertEqual(self._audit_products(corrected), [])
        self.assertNotEqual(missing, corrected)
        self.assertEqual({path.name: path.read_bytes() for path in missing.iterdir() if path.is_file()}, old)
        self.assertEqual(len(self.provider_requests), 3)  # read + candidate, then plan-only correction
        self.assertEqual(sum(entry.actual.get('process_invocations', 0) for entry in self.app.ledger.entries), 2)
        self.assertTrue(all(entry.status == 'settled' for entry in self.app.ledger.entries))
        binding = json.loads(self.app.workspace.resolve(executed['data']['execution_ref']).read_bytes())['approval']['binding']
        if self.app.state['config'].get('producer_completion') == 'goal_findings.v1':
            diagnostic = str(missing / 'cost-scope.json')
            self.assertIn(diagnostic, binding['context_sha256'])
            self.assertIn(diagnostic, binding['source_sha256'])
            process = json.loads((self.root / executed['data']['receipt']).read_bytes())
            actual = self.app.workspace.resolved_versions(process['input_binding']['workspace_artifacts']['inputs'])
            self.assertNotIn(Path(diagnostic), actual)
            self.assertIn(corrected / 'plot_results.py', actual)
