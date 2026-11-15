from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from simple_ar.app.research_application import ResearchApplicationServices
from simple_ar.app.research_intake import (
    ResearchInputError,
    create_research_task,
    create_structured_research_task,
    declared_research_work,
    revise_research_task,
    start_research_task,
    normalize_assets,
    parse_research_input,
    validate_brief,
    write_intake_artifacts,
)
from simple_ar.app.tool_application import ToolApplication
from simple_ar.integrations.llm import LLMClient
from simple_ar.research.sources import SearchProviderRegistry
from simple_ar.research.workflow_contracts import ResearchBrief


class _JSONClient:
    """Only the public JSON/budget port; no native chat or transport methods."""

    model = "acceptance-json-client"
    context_input_tokens = 128000

    def __init__(self, replies=()) -> None:
        self.replies = list(replies)
        self.calls = []
        self.bindings = []
        self.secret = "runtime-only-credential"

    def with_budget(self, ledger, *, session_id, attempt_id):
        clone = copy.copy(self)
        clone._budget_ledger = ledger
        clone._budget_session_id = session_id
        clone._budget_attempt_id = attempt_id
        self.bindings.append(clone)
        return clone

    def ask_json(self, system, user, **kwargs):
        self.calls.append((system, json.loads(user), kwargs))
        reservation = self._budget_ledger.reserve(kwargs["budget_call_id"],
            {"llm_requests": 1, "total_tokens": 20},
            session_id=self._budget_session_id, attempt_id=self._budget_attempt_id,
            purpose="acceptance-json-call")
        reply = self.replies.pop(0)
        self._budget_ledger.settle(reservation.reservation_id,
            {"llm_requests": 1, "total_tokens": 20}, actual_source="acceptance")
        return reply


def _reply(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": name, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]}


class ResearchIntakeTests(unittest.TestCase):
    def test_legacy_planning_controls_are_honored_by_common_research(self):
        from simple_ar.core.capabilities import ArtifactStore, AttemptManifest, CapabilityContext

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            client = _JSONClient()
            controls = {'research_materials_only': True, 'research_plan_mode': ' Deterministic ',
                        'research_max_queries': 2, 'research_planning_max_output_tokens': 1600}
            services = ResearchApplicationServices(llm_client=client, config=controls,
                budget_limits={'llm_requests': 2, 'total_tokens': 100000})
            app = create_research_task(ResearchBrief(request_text='Compare reliable methods.'),
                root=base / 'research', services=services)
            store = ArtifactStore(app.session.store.root / 'attempts' / 'planning')
            context = CapabilityContext(store, AttemptManifest('planning', capability='native_tool'))
            result = app.sources.execute_tool('research', context,
                {'operation': 'plan', 'question': 'Compare methods', 'use_llm': True})
            self.assertEqual(result.status, 'ok', result.error)
            self.assertLessEqual(len(store.read_json('research_plan.json')['query_plan']['queries']), 2)
            self.assertEqual(client.calls, [])
            self.assertEqual(app.ledger.entries, ())
            self.assertEqual(app.sources.policy.plan_mode, 'deterministic')

            # Model planning uses the same caller controls when explicitly enabled.
            controls['research_plan_mode'] = 'llm'
            services = replace(services, config=controls)
            app = create_research_task(ResearchBrief(request_text='Compare reliable methods.'),
                root=base / 'model-research', services=services)
            store = ArtifactStore(app.session.store.root / 'attempts' / 'planning')
            context = CapabilityContext(store, AttemptManifest('planning', capability='native_tool'))
            with patch.object(client, 'ask_json', return_value={
                    'queries': ['method one', 'method two', 'method three'],
                    'questions': [{'question': 'Which methods?', 'facet': 'method'}]}) as ask:
                result = app.sources.execute_tool('research', context,
                    {'operation': 'plan', 'question': 'Compare methods', 'use_llm': True})
            self.assertEqual(result.status, 'ok', result.error)
            self.assertEqual(ask.call_args.kwargs['max_output_tokens'], 1600)
            self.assertLessEqual(len(store.read_json('research_plan.json')['query_plan']['queries']), 2)

    def test_common_research_starts_from_saved_brief_and_retains_extraction_location(self):
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            source = base / 'source.html'
            source.write_text('<h1>Evidence</h1><p>A supplied observation.</p>', encoding='utf-8')
            extra = base / 'extra.html'
            extra.write_text('<p>A second supplied observation.</p>', encoding='utf-8')
            extracted = base / 'extracted'
            messages = []
            brief = ResearchBrief(request_text='Summarize the supplied observation.',
                requested_outputs=('summary',), asset_requests=({'locator': str(source), 'role': 'paper'},))
            app = create_research_task(brief, root=base / 'research', services=ResearchApplicationServices(
                extraction_dir=extracted, message_callback=messages.append,
                config={'research_materials_only': True, 'research_materials': ['source0'],
                    'research_task_kind': 'writing',
                    'research_local_documents': [str(source), str(extra)]}))
            result = start_research_task(app)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(messages)
            self.assertEqual(source.read_text(encoding='utf-8'), '<h1>Evidence</h1><p>A supplied observation.</p>')
            self.assertTrue(any('A supplied observation.' in path.read_text(encoding='utf-8')
                for path in extracted.glob('*.txt')))
            restored = ToolApplication.load(base / 'research')
            self.assertEqual(restored.sources.policy.extraction_dir, str(extracted))
            self.assertEqual(len(restored.sources.policy.materials), 2)
            self.assertEqual(len(restored.session.store.read_json(
                restored.session.manifest.state_refs['assets'])['assets']), 2)
            self.assertEqual(len(restored.session.store.read_json(
                restored.session.manifest.state_refs['brief'])['asset_requests']), 1)
            self.assertIn('"research_task_kind": "writing"', restored.state['requests'][0])
            self.assertEqual(restored.session.manifest.session_id, app.session.manifest.session_id)
            self.assertFalse(restored.ledger.entries)
            overlap = create_research_task(ResearchBrief(request_text='Use the selected document.',
                asset_requests=({'locator': str(source), 'role': 'code'},)), root=base / 'overlap',
                services=ResearchApplicationServices(config={'research_local_documents': [str(source)]}))
            self.assertEqual(overlap.sources.policy.materials, ('source0',))

    def test_common_declared_measurement_uses_original_task_and_process_ledger(self):
        import sys
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            brief = ResearchBrief(request_text='Measure the declared command.', requested_outputs=('experiment',))
            execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            app = create_structured_research_task(brief, root=base / 'measurement', model=None,
                config={'execution': execution}, budget_limits={'process_invocations': 1,
                    'process_wall_seconds': 5}, max_results=5)
            result = start_research_task(app, progress=lambda _: None)
            self.assertEqual(result['status'], 'completed')
            restored = ToolApplication.load(base / 'measurement')
            saved_brief = restored.session.store.read_json(restored.session.manifest.state_refs['brief'])
            self.assertEqual(saved_brief['requested_outputs'], ['experiment'])
            measurements = [row for row in restored._operation_observations()
                if row['tool_name'] == 'measure_experiment']
            self.assertEqual(len(measurements), 1)
            self.assertEqual(measurements[0]['data']['metrics']['score'], 3)
            self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in restored.ledger.entries), 1)
            self.assertEqual(restored.session.manifest.session_id, app.session.manifest.session_id)

    def test_deterministic_collection_keeps_other_requested_outputs_unfinished(self):
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            source = base / 'source.html'
            source.write_text('<p>A supplied observation.</p>', encoding='utf-8')
            for index, outputs in enumerate((('assessment',), ('summary', 'assessment'), ('data_analysis',))):
                with self.subTest(outputs=outputs):
                    brief = ResearchBrief(request_text='Work from the supplied material.',
                        requested_outputs=outputs, asset_requests=({'locator': str(source), 'role': 'paper'},))
                    app = create_research_task(brief, root=base / str(index),
                        services=ResearchApplicationServices(config={'research_materials_only': True}))
                    status = start_research_task(app, progress=lambda _: None)
                    self.assertNotEqual(status['status'], 'completed')
                    self.assertFalse(app.session.store.exists('delivery.json'))
                    observations = app._operation_observations()
                    self.assertEqual(observations[0]['tool_name'], 'research')
                    self.assertEqual(observations[-1]['tool_name'], 'ask_user')
                    self.assertTrue(all(not row.actual.get('llm_requests') for row in app.ledger.entries))

    def test_declared_measurement_does_not_claim_other_requested_outputs(self):
        import sys

        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            for paired in (False, True):
                with self.subTest(paired=paired):
                    execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                        'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
                    if paired:
                        execution['baseline_policy'] = 'run'
                    brief = ResearchBrief(request_text='Measure the condition and assess its implications.',
                        requested_outputs=('experiments', 'assessment'))
                    app = create_structured_research_task(brief, root=base / str(paired), model=None,
                        config={'execution': execution, 'require_design': False},
                        budget_limits={'process_invocations': 2 if paired else 1,
                            'process_wall_seconds': 10 if paired else 5}, max_results=5, research_work=False)
                    status = start_research_task(app, progress=lambda _: None)
                    self.assertNotEqual(status['status'], 'completed')
                    self.assertFalse(app.session.store.exists('delivery.json'))
                    observations = app._operation_observations(domain='scientific')
                    measurements = [row for row in observations if row['tool_name'] == 'measure_experiment']
                    self.assertEqual(len(measurements), 2 if paired else 1)
                    self.assertTrue(all(row['data']['execution_status'] == 'passed' for row in measurements))
                    if paired:
                        self.assertEqual(len([row for row in observations
                            if row['tool_name'] == 'analyze_measurements']), 1)
                        self.assertEqual(app.measurement_completion_actions(), [])
                    self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in app.ledger.entries),
                        2 if paired else 1)

    def test_declared_material_intent_is_shared_by_startup_and_creation(self):
        import sys

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            note = base / 'note.md'
            note.write_text('Reference material for the declared run.', encoding='utf-8')
            sources = ({'locator': str(note), 'role': 'paper'},)
            outputs = ('experiments',)
            brief = ResearchBrief(request_text='Run the supplied conditions.',
                requested_outputs=outputs, asset_requests=sources)
            execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            for name, config, expected in (('mounted', None, False), ('source-work', {}, True)):
                with self.subTest(intent=name):
                    research_work = declared_research_work(outputs, sources, config, explicit_research=False)
                    self.assertEqual(research_work, expected)
                    app = create_structured_research_task(brief, root=base / name, model=None,
                        config={'execution': execution, 'research_materials_only': True,
                            'require_design': False}, budget_limits={'process_invocations': 1,
                            'process_wall_seconds': 5}, max_results=5, research_work=research_work)
                    saved = ToolApplication.load(base / name).state['config']
                    self.assertEqual(saved['research_policy'] is not None, expected)
                    self.assertIn(note, map(Path, saved['sources'].values()))
                    self.assertFalse(saved['allow_network'])
                    self.assertFalse(app.ledger.entries)

    def test_research_then_declared_measurement_uses_same_task_and_caller_authority(self):
        import sys

        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            note = base / 'source.html'
            note.write_text('<h1>Method</h1><p>The supplied condition is measured once.</p>', encoding='utf-8')
            brief = ResearchBrief(request_text='Read the source and measure the supplied condition.',
                requested_outputs=('experiments',), asset_requests=({'locator': str(note), 'role': 'paper'},))
            execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            app = create_structured_research_task(brief, root=base / 'research', model=None,
                config={'execution': execution, 'require_design': False, 'research_materials_only': True},
                budget_limits={'process_invocations': 1, 'process_wall_seconds': 5},
                max_results=5, research_work=True)
            outcome = start_research_task(app, progress=lambda _: None)
            self.assertEqual(outcome['status'], 'completed')
            restored = ToolApplication.load(base / 'research')
            observations = restored._operation_observations()
            self.assertEqual([row['tool_name'] for row in observations], ['research', 'measure_experiment'])
            self.assertEqual(observations[-1]['data']['metrics']['score'], 3)
            self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in restored.ledger.entries), 1)
            self.assertEqual(restored.session.manifest.session_id, app.session.manifest.session_id)

    def test_declared_measurement_does_not_finish_a_still_required_report(self):
        import sys

        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            note = base / 'source.html'
            note.write_text('<h1>Method</h1><p>Inspect the measured result.</p>', encoding='utf-8')
            brief = ResearchBrief(request_text='Measure and explain the result.',
                requested_outputs=('experiments', 'report'),
                asset_requests=({'locator': str(note), 'role': 'paper'},))
            execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            app = create_structured_research_task(brief, root=base / 'research', model=None,
                config={'execution': execution, 'require_design': False, 'research_materials_only': True},
                budget_limits={'process_invocations': 1, 'process_wall_seconds': 5},
                max_results=5, research_work=True)
            outcome = start_research_task(app, progress=lambda _: None)
            self.assertNotEqual(outcome['status'], 'completed')
            self.assertFalse(app.session.store.exists('delivery.json'))
            measurements = [row for row in app._operation_observations()
                if row['tool_name'] == 'measure_experiment']
            self.assertEqual(measurements[-1]['data']['metrics']['score'], 3)
            self.assertNotIn('delivery', measurements[-1]['arguments'])

    def test_research_revision_preserves_the_session_evidence_and_budget(self):
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('No model was authorized')):
            base = Path(directory).resolve()
            first, second = base / 'first.html', base / 'second.html'
            first.write_text('<h1>First</h1><p>Original evidence.</p>', encoding='utf-8')
            second.write_text('<h1>Second</h1><p>Additional evidence.</p>', encoding='utf-8')
            brief = ResearchBrief(request_text='Compare the supplied materials.',
                asset_requests=({'locator': str(first), 'role': 'paper'},))
            app = create_research_task(brief, root=base / 'task',
                services=ResearchApplicationServices(config={'research_materials_only': True}))
            app.run(actions=[{'tool_name': 'research', 'arguments': {'operation': 'summary',
                'question': brief.request_text, 'materials': ['source0'], 'use_llm': False}}],
                progress=lambda _: None, should_stop=lambda: app.state['pending'] is None
                    and not any(turn['remaining'] for turn in app.state['turns']))
            observed = app._operation_observations(domain='sources')[-1]
            self.assertEqual(observed['status'], 'ok')
            record = app.session.store.resolve(observed['record'])
            original = record.read_bytes()
            entries = app.ledger.to_dict()['entries']
            previous_attempts = app.session.manifest.budget.attempts
            previous_limit = app.session.manifest.budget.max_attempts
            changed = revise_research_task(app, materials=[(second, 'paper')],
                resource_allowances={'source_operations': 20},
                additional_attempts=3, additional_no_progress=1,
                authorization_id='research-revision', authorization_reason='Review the additional material.')
            self.assertTrue(changed)
            restored = ToolApplication.load(base / 'task')
            self.assertEqual(restored.session.manifest.session_id, app.session.manifest.session_id)
            self.assertEqual(restored.state['config']['research_policy']['materials'], ['source0', 'source1'])
            self.assertEqual(restored.state['config']['sources']['source1'], str(second))
            self.assertEqual(record.read_bytes(), original)
            self.assertTrue(restored._operation_observations(domain='sources')[0]['contract_current'])
            self.assertTrue(any('Required research summary' in issue
                for issue in restored.sources.delivery_issues([])))
            self.assertEqual(restored.ledger.to_dict()['entries'][:len(entries)], entries)
            self.assertEqual(restored.session.manifest.budget.attempts, previous_attempts)
            self.assertEqual(restored.session.manifest.budget.max_attempts, previous_limit + 3)
            self.assertEqual(restored.ledger.remaining('source_operations'), 20)
            self.assertTrue(revise_research_task(restored, research_settings={'max_results': 7}))
            restored = ToolApplication.load(base / 'task')
            self.assertEqual(restored.state['config']['research_policy']['max_results'], 7)
            self.assertFalse(restored._operation_observations(domain='sources')[0]['contract_current'])

    def test_plain_resume_does_not_rewrite_an_older_source_policy(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            app = create_research_task(ResearchBrief(request_text='Inspect the supplied work.'),
                root=base / 'task', services=ResearchApplicationServices(config={'research_materials_only': True}))
            app.state['config']['research_policy'].pop('max_pdf_pages')
            app._save()  # Simulate a retained task from before this optional default was saved.
            restored = ToolApplication.load(base / 'task')
            before = copy.deepcopy(restored.state)
            ledger = restored.ledger.to_dict()
            self.assertFalse(revise_research_task(restored))
            self.assertEqual(restored.state, before)
            self.assertEqual(restored.ledger.to_dict(), ledger)

    def test_attempt_allowance_reuses_the_saved_task_without_replenishing_on_replay(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            app = create_research_task(ResearchBrief(request_text='Use this saved task.'), root=base / 'task',
                services=ResearchApplicationServices(config={'research_materials_only': True},
                    budget_limits={'source_operations': 0}))
            before = app.session.manifest.budget.to_dict()
            for _ in range(2):
                self.assertTrue(revise_research_task(app, additional_attempts=3, additional_no_progress=1,
                    authorization_id='attempts-1', authorization_reason='Continue the original task.',
                ))
            restored = ToolApplication.load(base / 'task')
            budget = restored.session.manifest.budget
            self.assertEqual(budget.max_attempts, before['max_attempts'] + 3)
            self.assertEqual(budget.max_no_progress, before['max_no_progress'] + 1)
            self.assertEqual(budget.attempts, before['attempts'])
            self.assertFalse(restored.ledger.entries)
            self.assertIn('attempts-1', restored._professional_resource_authorizations())
            for _ in range(2):
                self.assertTrue(revise_research_task(restored, resource_allowances={'source_operations': 3},
                    authorization_id='source-1', authorization_reason='Continue with the saved materials.'))
            restored = ToolApplication.load(base / 'task')
            self.assertEqual(restored.ledger.remaining('source_operations'), 3)

    def test_adding_a_reference_retains_an_unchanged_table_analysis(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            table, note = base / 'observations.csv', base / 'notes.md'
            table.write_text('score\n1\n2\n3\n', encoding='utf-8')
            note.write_text('Context for the same observations.', encoding='utf-8')
            app = ToolApplication.create(base / 'task', 'Summarize the observations.', model=None,
                sources={'source0': table}, table_analysis={'path': 'source0',
                    'spec': {'value_columns': ['score'], 'observation_unit': 'one supplied row'}})
            app.run(actions=[{'tool_name': 'analyze_table', 'arguments': app.state['config']['table_analysis']}],
                progress=lambda _: None, should_stop=lambda: app.state['pending'] is None
                    and not any(turn['remaining'] for turn in app.state['turns']))
            before = app._operation_observations(domain='analysis')[-1]
            self.assertEqual(before['status'], 'ok')
            self.assertTrue(revise_research_task(app, materials=[(note, 'material')]))
            restored = ToolApplication.load(base / 'task')
            self.assertTrue(restored._operation_observations(domain='analysis')[-1]['contract_current'])
            replacement = base / 'replacement.csv'
            replacement.write_text('score\n9\n', encoding='utf-8')
            restored.supply(supply_id='replace-data', reason='Use the replacement dataset.',
                changes={'sources': {**restored.state['config']['sources'], 'source0': str(replacement)}},
                replace_existing=True)
            self.assertFalse(restored._operation_observations(domain='analysis')[-1]['contract_current'])

    def test_research_revision_keeps_explicit_independent_review_on_the_common_task(self):
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            services = ResearchApplicationServices(llm_client=_JSONClient(),
                config={'research_materials_only': True},
                budget_limits={'llm_requests': 2, 'total_tokens': 100000})
            app = create_research_task(ResearchBrief(request_text='Review the supplied research.'),
                root=base / 'task', services=services)
            ledger = app.ledger.to_dict()
            self.assertTrue(revise_research_task(app, independent_review_required=True))
            restored = ToolApplication.load(base / 'task', services=services)
            self.assertTrue(restored.state['config']['independent_review_required'])
            self.assertEqual(restored.ledger.to_dict(), ledger)
            self.assertEqual(restored.session.manifest.session_id, app.session.manifest.session_id)

    def test_structured_analysis_can_require_review_without_a_report(self):
        from types import SimpleNamespace
        from simple_ar.tools.workspace import DeliveryRequest

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            table = base / 'observations.csv'
            table.write_text('score\n1\n2\n3\n', encoding='utf-8')
            services = SimpleNamespace(llm_client=_JSONClient())
            brief = ResearchBrief(request_text='Review the analysis of the supplied values.',
                requested_outputs=('data_analysis',),
                asset_requests=({'locator': str(table), 'role': 'dataset'},))
            app = create_structured_research_task(brief, root=base / 'task', model=None,
                services=services, max_results=1,
                config={'data_analysis': {'file': str(table), 'value_columns': ['score'],
                    'observation_unit': 'one supplied row'}, 'independent_review_required': True},
                budget_limits={'llm_requests': 2, 'total_tokens': 100000})
            self.assertIsNone(app.state['config'].get('report_requirement'))
            self.assertTrue(app.state['config']['independent_review_required'])
            app.run(actions=[{'tool_name': 'analyze_table', 'arguments': app.state['config']['table_analysis']}],
                progress=lambda _: None, should_stop=lambda: app.state['pending'] is None
                    and not any(turn['remaining'] for turn in app.state['turns']))
            result = app._operation_observations(domain='analysis')[-1]
            self.assertEqual(result['status'], 'ok')
            claim = DeliveryRequest.model_validate({'summary': 'Analysis of supplied values',
                'artifacts': result['artifacts'], 'independent_review': False})
            self.assertTrue(app.operations.delivery.requires_review(claim))
            self.assertFalse(app.ledger.entries)

    def test_new_structured_work_can_reuse_results_without_forcing_a_script(self):
        import sys
        from simple_ar.app.operation_tools import _executable_work_system

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            services = ResearchApplicationServices(llm_client=_JSONClient(), config={'execution': {
                'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}},
                budget_limits={'llm_requests': 2, 'total_tokens': 100000})
            app = create_research_task(ResearchBrief(request_text='Check the supplied measurement.',
                requested_outputs=('experiments',)), root=base / 'task', services=services)
            self.assertEqual(app.state['config']['work_method'], 'execute.v3')
            self.assertIn('Use existing results and the available reading, analysis, editing and execution operations',
                app.operations.system_text())
            self.assertNotIn('Use a complete script or command', app.operations.system_text())
            self.assertIn('Use a complete script or command', _executable_work_system())

    def test_collection_budget_allows_multiple_sources_and_preserves_explicit_limit(self):
        from simple_ar.research.contracts import DocumentRecord
        from simple_ar.research.documents.fulltext import build_fulltext_manifest
        from simple_ar.tools.sources import SourcePolicy

        self.assertNotIn('extraction_dir', SourcePolicy().model_dump(mode='json'))
        # A saved task without a source policy reconstructs the gateway default.
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'saved-task'
            ToolApplication.create(root, 'Use the supplied work.', model=None, sources={})
            resumed = ToolApplication.load(root)
            self.assertIsNone(resumed.state['config']['research_policy'])
            self.assertEqual((resumed.sources.policy.max_fulltext_documents,
                resumed.sources.policy.max_fulltext_fetch_attempts), (1, 3))
        records = [DocumentRecord(document_id=str(index), title=f'Source {index}', source='test',
            url=f'https://example.org/{index}.html') for index in range(4)]

        def acquired(hint, **_kwargs):
            return replace(hint, status='cached', local_path='/recorded/source.html')

        with TemporaryDirectory() as directory, patch(
                'simple_ar.research.documents.fulltext._cache_selected_hint', side_effect=acquired):
            for attempts, expected in ((None, 4), (2, 2), (0, 0)):
                options = {'max_results': 4, 'max_documents': 4}
                if attempts is not None:
                    options['max_fulltext_fetch_attempts'] = attempts
                policy = SourcePolicy.model_validate(options)
                manifest = build_fulltext_manifest(records=records, source_plan=policy.source_plan(),
                    cache_dir=Path(directory))
                self.assertEqual(manifest['cached_count'], expected)
                self.assertEqual(manifest['fetch_attempt_count'], expected)
                self.assertEqual(SourcePolicy.model_validate(policy.model_dump()).source_plan().budget,
                    policy.source_plan().budget)

    def test_structured_creation_recognizes_named_and_injected_models(self):
        import sys
        from types import SimpleNamespace

        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            catalog = base / 'models.toml'
            catalog.write_text('version = 1\n[routes]\ndefault = "acceptance"\n'
                '[profiles.acceptance]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-model"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["text"]\n', encoding='utf-8')
            execution = {'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            brief = ResearchBrief(request_text='Measure the provided conditions.',
                requested_outputs=('experiments',))
            for name, model, runtime, expected in (
                    ('injected', None, SimpleNamespace(llm_client=_JSONClient()), 4),
                    ('named', 'profile:acceptance', None, 4),
                    ('no-model', None, None, 0)):
                with self.subTest(name=name), patch.dict(os.environ, {
                        'SIMPLE_AR_MODELS_CONFIG': str(catalog), 'ACCEPTANCE_MODEL_KEY': 'fixture-key'}):
                    app = create_structured_research_task(brief, root=base / name,
                        config={'execution': execution, 'research_materials_only': True},
                        budget_limits={'llm_requests': 4, 'total_tokens': 100000},
                        model=model, max_results=10, services=runtime)
                    self.assertEqual(app.ledger.remaining('llm_requests'), expected)
                    self.assertEqual(app.state['config']['model'],
                        'runtime' if name == 'injected' else model)
                    self.assertEqual(app.session.manifest.budget.max_attempts,
                        max(20, expected * 10 + 16))
                    self.assertFalse(app.ledger.entries)
            collection = create_structured_research_task(
                ResearchBrief(request_text='Read these conditions.', requested_outputs=('assessment',)),
                root=base / 'collection', config={'research_materials_only': True},
                budget_limits={}, model=None, max_results=10,
                services=SimpleNamespace(llm_client=_JSONClient()))
            self.assertEqual(collection.ledger.remaining('llm_requests'), 30)
            self.assertEqual(collection.session.manifest.budget.max_attempts, 300)
            policy = collection.state['config']['research_policy']
            self.assertEqual((policy['max_fulltext_documents'], policy['max_fulltext_fetch_attempts']), (10, 20))

    def test_direct_creation_keeps_runtime_bindings_and_legacy_creation_equivalent(self):
        from types import SimpleNamespace
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            note = base / 'note.md'
            note.write_text('Supplied evidence only.', encoding='utf-8')
            brief = ResearchBrief(request_text='Assess supplied evidence.', requested_outputs=('assessment',),
                asset_requests=({'locator': 'note.md', 'role': 'material'},))
            client, reviewer = _JSONClient(), _JSONClient()
            registry = SearchProviderRegistry()
            runtime = SimpleNamespace(llm_client=client, feasibility_llm_client=reviewer, search_registry=registry)
            options = dict(config={'research_materials_only': True}, input_base_dir=base,
                budget_limits={'llm_requests': 4, 'total_tokens': 100000, 'process_invocations': 0,
                    'process_wall_seconds': 0, 'source_operations': 0},
                max_attempts=7, max_no_progress=5, cache_dir=base / 'cache')
            original = copy.deepcopy(options)
            legacy = ResearchApplicationServices(llm_client=client, feasibility_llm_client=reviewer,
                search_registry=registry, **options)
            with patch.object(LLMClient, 'from_env', side_effect=AssertionError('No environment fallback')):
                old = create_research_task(brief, root=base / 'old-call', services=legacy)
                new = create_research_task(brief, root=base / 'direct-call', services=runtime, **options)
            for field in ('model', 'research_policy', 'sources', 'allow_network', 'allow_execution'):
                self.assertEqual(new.state['config'][field], old.state['config'][field], field)
            self.assertIs(new.services, runtime)
            self.assertIs(new.sources.registry, registry)
            self.assertEqual(new.session.manifest.budget.max_attempts, 7)
            self.assertEqual(new.session.manifest.budget.max_no_progress, 5)
            self.assertEqual(new.ledger.remaining('process_invocations'), 0)
            self.assertEqual(new.ledger.remaining('source_operations'), 0)
            self.assertEqual(new.state['config']['research_policy']['providers'], [])
            self.assertEqual(new.session.list_attempts(), ())
            self.assertEqual(client.calls, [])
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(options, original)

    def test_legacy_unlimited_model_budget_survives_common_creation_and_resume(self):
        from types import SimpleNamespace
        from simple_ar.integrations.llm import LLMSettings

        with TemporaryDirectory() as directory:
            base = Path(directory)
            brief = ResearchBrief(request_text='Assess the supplied finding.', requested_outputs=('assessment',))
            client = LLMClient(LLMSettings(api_key='fixture', model='fixture', api_mode='chat'))
            legacy = ResearchApplicationServices(llm_client=client, config={'research_materials_only': True})
            app = create_research_task(brief, root=base / 'legacy-settings', services=legacy)
            self.assertIsNone(app.ledger.limits['llm_requests'])
            self.assertIsNone(app.ledger.limits['total_tokens'])
            self.assertIsNone(app._request_capacity())
            self.assertTrue(app._can_request(2))
            resources = json.loads(app._messages()[1]['content'])['resources']
            self.assertIsNone(resources['model_turns_remaining'])
            self.assertIsNone(resources['decision_budget']['provider_slots_after_one_attempt'])
            before = app.ledger.to_dict()
            app.supply(supply_id='require-review', reason='Require an independent check',
                changes={'independent_review_required': True})
            self.assertEqual(app.ledger.to_dict(), before)
            loaded = ToolApplication.load(base / 'legacy-settings', services=legacy)
            self.assertIsNone(loaded._request_capacity())
            self.assertEqual(loaded.ledger.to_dict(), before)
            direct = create_research_task(brief, root=base / 'new-defaults',
                services=SimpleNamespace(llm_client=client), config={'research_materials_only': True})
            self.assertEqual(direct.ledger.limits['llm_requests'], 30)
            self.assertEqual(direct.ledger.limits['total_tokens'], 600000)
            self.assertEqual(client.budget_ledger, None)

    def test_custom_model_client_can_join_the_existing_task_ledger(self):
        from simple_ar.core.budget import BudgetLedger
        from simple_ar.integrations.llm import bind_task_budget, task_budget_identity

        ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 100})
        client = _JSONClient()
        bound = bind_task_budget(client, ledger, session_id='original', attempt_id='writing')
        self.assertIs(bound._budget_ledger, ledger)
        self.assertEqual(task_budget_identity(bound, None), (ledger, 'original', 'writing'))
        resumed = bind_task_budget(bound, ledger, session_id='original', attempt_id='review')
        self.assertEqual(resumed._budget_attempt_id, 'review')
        self.assertEqual(bound._budget_attempt_id, 'writing')
        ledger_only = bind_task_budget(client, ledger)
        identified = bind_task_budget(ledger_only, ledger, session_id='original', attempt_id='execution')
        self.assertEqual(task_budget_identity(identified, None), (ledger, 'original', 'execution'))
        self.assertEqual(task_budget_identity(ledger_only, None), (ledger, '', ''))
        self.assertEqual(ledger.entries, ())

    def test_direct_creation_rejects_unowned_controls_before_mutation(self):
        brief = ResearchBrief(request_text='Retain explicit ownership.')
        with TemporaryDirectory() as directory:
            root = Path(directory) / 'absent'
            for options, error in (
                    ({'config': {'unknown_creation_control': True}}, 'explicit execution/revision owner'),
                    ({'budget_limits': {'unknown_resource': 1}}, 'Unsupported creation resource'),
                    ({'model': 'named', 'budget_limits': {'llm_requests': 0}}, 'finite positive'),
                    ({'max_attempts': True}, 'positive integer'),
                    ({'max_no_progress': 0}, 'positive'),
                    ({'services': ResearchApplicationServices(), 'config': {}}, 'direct creation settings')):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, error):
                    create_research_task(brief, root=root, **options)
            self.assertFalse(root.exists())

    def test_ordinary_outcomes_directly_edits_and_delivers_chinese_markdown(self):
        from tests.test_professional_work_loop import OperationsWorkLoopTests, _action
        case = OperationsWorkLoopTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        request = '仅依据所给材料整理 Markdown 摘要，不生成 PDF。'
        content = '# 材料摘要\n单次观察不能证明稳定性。\n'
        raw = {'write_file', 'edit_file', 'run_process'}
        permitted_raw = raw - {'run_process'}
        case.decisions.extend([
            _action('write_file', path='workspace/summary.md', content=content),
            _action('deliver_result', summary='已交付材料摘要。',
                artifacts=['workspace/summary.md'], unresolved=[]),
        ])
        services = ResearchApplicationServices(llm_client=case.services.llm_client,
            config={'research_materials_only': True},
            budget_limits={'llm_requests': 8, 'total_tokens': 1000000})
        with TemporaryDirectory() as directory:
            base = Path(directory)
            note = base / 'notes.md'
            note.write_text('单次观察不能证明稳定性。', encoding='utf-8')
            original = note.read_bytes()
            app = create_research_task(ResearchBrief(request_text=request,
                requested_outputs=('assessment',), asset_requests=({'locator': str(note)},)),
                root=base / 'task', services=services)
            case.app, case.root = app, base / 'task'
            self.assertEqual(app.state['config']['tool_contracts'], 'on_demand')
            permissions = set(app.gateway.policy.allowed_permissions)
            self.assertNotIn('inspect_artifacts', app._specialist_catalog())
            self.assertNotIn('export_report', app._specialist_catalog())
            self.assertIsNone(app.implementation)
            self.assertFalse({'source_generation_protocol', 'source_decision_protocol'} & app.state['config'].keys())
            result = app.run(progress=lambda _: None)
            self.assertEqual(result['status'], 'completed', result)
            first_request = case.send.call_args_list[0].args[1]
            first = {row['function']['name'] for row in first_request['tools']}
            self.assertNotIn('select_work', first)
            first_view = json.loads(first_request['messages'][1]['content'])
            self.assertNotIn('work_selection', first_view)
            self.assertTrue({'read_file', 'list_directory',
                'ask_user', 'deliver_result'} <= first)
            self.assertNotIn('inspect_artifacts', first)
            self.assertTrue(permitted_raw <= first)
            self.assertNotIn('run_process', first)
            self.assertNotIn('prepare_implementation', first)
            self.assertEqual(app.gateway.policy.allowed_permissions, permissions)
            observations = [app.session.store.read_json(item['result'])
                for turn in app.state['turns'] for item in turn['tools']]
            self.assertEqual([row['tool_name'] for row in observations],
                ['write_file', 'deliver_result'])
            self.assertTrue(all(row['status'] == 'ok' for row in observations), observations)
            self.assertTrue(case.inspection_requests)
            self.assertEqual((base / 'task/workspace/summary.md').read_text(encoding='utf-8'), content)
            self.assertEqual(note.read_bytes(), original)
            self.assertIsNone(app.state['config'].get('report_requirement'))
            self.assertFalse(list((base / 'task/workspace').rglob('*.pdf')))

    def test_operation_contract_modes_and_saved_defaults_preserve_authority(self):
        raw = {'write_file', 'edit_file', 'run_process'}
        names = lambda app: {row['function']['name'] for row in app.operation_schemas}
        with TemporaryDirectory() as directory:
            base = Path(directory)
            preview, _, _, _ = ToolApplication._configuration(base / 'preview', 'Preview',
                model=None, sources={})
            self.assertEqual(preview['tool_contracts'], 'on_demand')
            for mode in ('outcomes', 'full', 'on_demand', None):
                with self.subTest(mode=mode):
                    services = ResearchApplicationServices(llm_client=_JSONClient())
                    app = ToolApplication.create(base / str(mode), 'Ordinary work', model='fixture',
                        sources={}, allow_execution=True, services=services,
                        tool_contracts=mode if mode is not None else 'full')
                    if mode is None:
                        app.state['config'].pop('tool_contracts')
                        app._save()
                    expected = names(app)
                    self.assertEqual({row['function']['name'] for row in app.schemas}, expected)
                    self.assertIsNone(app.operations.selection)
                    allowed = {spec.name for spec in app.gateway.list_specs()
                        if app.gateway.policy.is_allowed(spec)[0]
                        and app.operations.coordinator_eligible(spec)} - {'select_work', 'update_work_plan'}
                    if mode == 'outcomes':
                        self.assertEqual(expected, allowed)
                        self.assertEqual(set(app._specialist_catalog()), set())
                        self.assertTrue({'write_file', 'edit_file', 'run_process', 'export_report',
                            'read_file', 'review_result'} <= expected)
                    elif mode in ('full', None):
                        self.assertEqual(expected, allowed)
                    else:
                        self.assertEqual(expected, {name for name in allowed
                            if app.gateway.registry.request_visible(name)})
                        self.assertTrue(raw <= expected)
                    saved_config = copy.deepcopy(app.state['config'])
                    saved_bytes = (app.session.store.root / 'actions.json').read_bytes()
                    restored = ToolApplication.load(app.session.store.root, services=services)
                    self.assertEqual(restored.state['config'], saved_config)
                    self.assertEqual(names(restored), expected)
                    self.assertEqual((app.session.store.root / 'actions.json').read_bytes(), saved_bytes)
                    if mode == 'outcomes':
                        permissions = set(restored.gateway.policy.allowed_permissions)
                        ledger = restored.ledger.to_dict()
                        restored.run(actions=[
                            {'tool_name': 'load_tools', 'arguments': {'names': sorted(raw)}},
                            {'tool_name': 'ask_user', 'arguments': {'question': 'Which operation next?'}}],
                            progress=lambda _: None)
                        self.assertTrue(raw <= names(restored))
                        self.assertEqual(restored.gateway.policy.allowed_permissions, permissions)
                        self.assertEqual(restored.ledger.to_dict(), ledger)
                        self.assertEqual(names(ToolApplication.load(restored.session.store.root,
                            services=services)), names(restored))
            denied = ToolApplication.create(base / 'denied', 'No execution', model=None, sources={})
            denied.run(actions=[{'tool_name': 'load_tools', 'arguments': {'names': ['run_process']}}],
                progress=lambda _: None)
            receipt = denied.session.store.read_json(denied.state['turns'][0]['tools'][0]['result'])
            self.assertEqual(receipt['status'], 'blocked')
            self.assertNotIn('run_process', names(denied))
            self.assertNotIn('run_process', denied._specialist_catalog())

    def test_native_default_attempts_do_not_inherit_legacy_stage_units(self):
        from simple_ar.app.research_application import create_session, load_session
        with TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / 'note.md'
            note.write_text('A supplied observation, not a verified conclusion.', encoding='utf-8')
            brief = ResearchBrief(request_text='Assess the supplied observation.',
                requested_outputs=('assessment',), asset_requests=({'locator': str(note)},))
            client = _JSONClient()
            services = ResearchApplicationServices(llm_client=client,
                config={'research_materials_only': True})
            self.assertIsNone(services.max_attempts)
            app = create_research_task(brief, root=root / 'native', services=services)
            self.assertEqual(app.state['config']['context_protocol'],'working.v4')
            self.assertEqual(app.session.manifest.budget.max_attempts, 300)
            actions = [{'tool_name': 'write_file', 'arguments': {
                'path': f'workspace/note-{index}.md', 'content': 'Observed evidence only.'}}
                for index in range(17)]
            actions.append({'tool_name': 'ask_user', 'arguments': {
                'question': 'Which scientific interpretation should the next comparison address?'}})
            app.run(actions=actions, progress=lambda _: None)
            self.assertTrue((root / 'native/workspace/note-16.md').is_file())
            self.assertEqual(len(app.session.list_attempts()), 18)
            self.assertEqual(client.calls, [])
            self.assertEqual(app.ledger.entries, ())
            for name, options, expected in (
                    ('bounded-model', replace(services, budget_limits={'llm_requests': 4, 'total_tokens': 100000}), 40),
                    ('explicit', replace(services, max_attempts=7), 7),
                    ('caller', replace(services, llm_client=None), 16)):
                with self.subTest(creation=name):
                    created = create_research_task(brief, root=root / name, services=options)
                    self.assertEqual(created.session.manifest.budget.max_attempts, expected)
                    restored = ToolApplication.load(root / name, services=replace(options, max_attempts=999))
                    self.assertEqual(restored.session.manifest.budget.max_attempts, expected)
            experiments = create_research_task(replace(brief, requested_outputs=('experiments',)),
                root=root / 'experiments', services=services)
            self.assertEqual(experiments.session.manifest.budget.max_attempts, 300)
            self.assertEqual(experiments.ledger.remaining('process_invocations'), 16)
            legacy = create_session(brief, root=root / 'legacy', services=replace(services, llm_client=None))
            self.assertEqual(legacy.services.max_attempts, 16)
            with patch('simple_ar.app.research_intake.create_research_task',
                    side_effect=AssertionError('Restore must not create a successor')):
                restored = load_session(root / 'legacy', services=ResearchApplicationServices(max_attempts=999))
            self.assertEqual(restored.services.max_attempts, 16)
            self.assertEqual(restored.controller.manifest.budget.max_attempts, 16)
            for invalid in (0, -1, True, 1.5, '16'):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    ResearchApplicationServices(max_attempts=invalid)

    def test_deferred_experiment_retains_materials_then_supplies_execution_in_same_task(self):
        import sys
        from simple_ar.tools.scientific import measurement_contract
        with TemporaryDirectory() as directory, patch.object(LLMClient, 'from_env',
                side_effect=AssertionError('Collection and caller measurements need no model')):
            base = Path(directory).resolve()
            notes = base / 'notes.md'
            notes.write_text('# Conditions\nOne observation does not establish stability.\n', encoding='utf-8')
            original_notes = notes.read_bytes()
            brief = ResearchBrief(request_text='Collect these conditions, then measure the result.',
                requested_outputs=('experiments',),
                asset_requests=({'locator': str(notes), 'role': 'paper'},))
            app = create_research_task(brief, root=base / 'task', services=ResearchApplicationServices(
                max_attempts=30, config={'research_materials_only': True},
                budget_limits={'process_invocations': 1, 'process_wall_seconds': 5}))
            identity = app.session.manifest.session_id
            self.assertIsNone(app.state['config']['model'])
            self.assertIsNone(app.state['config'].get('execution_contract'))
            self.assertTrue(app.sources.policy.require_design)
            app.run(actions=[{'tool_name': 'research', 'arguments': {'operation': 'summary',
                'question': brief.request_text, 'materials': ['source0'], 'use_llm': False}}], progress=lambda _: None)
            collected = app.session.store.read_json(app.state['turns'][-1]['tools'][0]['result'])
            self.assertEqual(collected['status'], 'ok', collected.get('error'))
            retained = {path: app.workspace.resolve(path).read_bytes() for path in collected['artifacts']}
            self.assertTrue(app.sources.delivery_issues([]))
            original_ledger = app.ledger.to_dict()
            contract = measurement_contract({'command': [sys.executable, '-c', "print('score: 3')"],
                'cwd': str(base), 'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}})
            app = ToolApplication.load(base / 'task')
            app.supply(supply_id='prepared-command', reason='Caller supplied the missing measurement',
                changes={'execution_contract': contract})
            self.assertEqual(app.session.manifest.session_id, identity)
            self.assertEqual(app.ledger.to_dict(), original_ledger)
            self.assertEqual({path: app.workspace.resolve(path).read_bytes() for path in retained}, retained)
            decision = app.request_execution_confirmation()
            app.run(decision={'decision_id': decision['decision_id'], 'decision_response': 'accept'},
                progress=lambda _: None)
            app.run(actions=[{'tool_name': 'measure_experiment', 'arguments': contract}], progress=lambda _: None)
            self.assertFalse(app.ledger.entries)
            token = app.approval_token(app.state['pending']['call'])
            app.run(approved_execution=token, progress=lambda _: None)
            measured = app.session.store.read_json(app.state['turns'][-1]['tools'][0]['result'])
            self.assertEqual(measured['status'], 'ok', measured.get('error'))
            self.assertEqual(measured['data']['metrics']['score'], 3)
            self.assertEqual(sum(row.actual.get('process_invocations', 0) for row in app.ledger.entries), 1)
            self.assertEqual(notes.read_bytes(), original_notes)

    def test_open_execution_does_not_freeze_omitted_comparison_as_skip(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, '-c', "print('score: 3')"], 'cwd': str(base),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            app = create_research_task(ResearchBrief(request_text='Investigate a justified comparison.',
                requested_outputs=('experiments',)), root=base / 'task',
                services=ResearchApplicationServices(config={'execution': execution,
                    'require_design': True, 'research_materials_only': True}, max_attempts=9))
            self.assertIsNone(app.scientific.execution_contract)
            self.assertNotIn('baseline_policy', app.sources.policy.execution_boundary)
            self.assertEqual(app.sources.policy.execution_boundary['command'], execution['command'])
            self.assertEqual(app.ledger.remaining('process_invocations'), 9)
            self.assertTrue(app.scientific.execution_issues())
            self.assertEqual(app.session.list_attempts(), ())

    def test_json_client_resumes_one_slot_task_delivers_honest_partial_and_terminal_load_is_idle(self) -> None:
        content = "# Assessment\nCalibration error 0.12 alone cannot establish robustness.\n"
        request = "Assess the observation: calibration error is 0.12."
        # This request asks for an assessment, not a file or formal report.
        # One physical request cannot also fund independent acceptance. Retain
        # this JSON budget-port boundary with a truthful typed partial exit.
        obstacle = 'The remaining shared allowance cannot fund independent checking of this assessment.'
        final_reply = _reply('deliver_result', {'summary': content,
            'artifacts': [], 'unresolved': [obstacle]})
        client = _JSONClient([final_reply])
        services = ResearchApplicationServices(llm_client=client,
            budget_limits={"llm_requests": 1, "total_tokens": 1000000},
            config={"research_materials_only": True})
        brief = ResearchBrief(request_text=request,
            requested_outputs=("assessment",))
        with TemporaryDirectory() as directory, patch.object(LLMClient, "from_env",
                side_effect=AssertionError("Injected services must not use environment clients")):
            root = Path(directory) / "task"
            app = create_research_task(brief, root=root, services=services, model=None)
            self.assertIsInstance(app, ToolApplication)
            identity = app.session.manifest.session_id
            self.assertEqual(app.session.manifest.transition_recipe, ToolApplication.RECIPE)
            self.assertEqual(app.state['config']['control_protocol'], 'operations.v1')
            self.assertIsNone(app.writing)
            self.assertEqual(app.session.manifest.status, "created")
            self.assertEqual(app.session.list_attempts(), ())
            self.assertEqual(client.calls, [])
            self.assertEqual(app.ledger.entries, ())
            saved = (root / "actions.json").read_text(encoding="utf-8")
            self.assertNotIn(client.secret, saved)
            self.assertEqual(app.state["config"]["runtime_dependencies"]["llm_client"]["model"], client.model)

            resumed = ToolApplication.load(root, services=services)
            result = resumed.run(progress=lambda _: None)
            self.assertEqual(result["status"], "partial", result)
            observations = [resumed.session.store.read_json(item['result'])
                for turn in resumed.state['turns'] for item in turn['tools']]
            self.assertEqual([row['tool_name'] for row in observations],
                ['deliver_result'])
            self.assertTrue(all(row['status'] == 'ok' for row in observations), observations)
            self.assertIsNone(resumed.state['config'].get('report_requirement'))
            self.assertEqual(result["session_id"], identity)
            delivery = json.loads((root / "delivery.json").read_text(encoding="utf-8"))
            self.assertEqual(delivery["summary"], content)
            self.assertEqual(delivery["artifacts"], [])
            self.assertEqual(delivery['unresolved'], [obstacle])
            self.assertFalse(any((root / 'workspace').iterdir()))
            self.assertEqual(len(client.calls), 1)
            entries = resumed.ledger.entries
            self.assertEqual(len(entries), 1)
            attempts = {a.attempt_id: a for a in resumed.session.list_attempts()}
            for entry in entries:
                self.assertEqual(entry.session_id, identity)
                self.assertEqual(attempts[entry.attempt_id].capability, "native_turn")
                self.assertEqual(entry.status, "settled")
                self.assertEqual(entry.actual["llm_requests"], 1)
            self.assertEqual(resumed.ledger.remaining("llm_requests"), 0)
            self.assertTrue(client.bindings)
            for clone in client.bindings:
                self.assertIsNot(clone, client)
                self.assertIs(clone._budget_ledger, resumed.ledger)
                self.assertEqual(clone._budget_session_id, identity)

            ledger_before = (root / "budget_ledger.json").read_bytes()
            attempts_before = tuple(attempts)
            terminal = ToolApplication.load(root, services=services)
            self.assertEqual(terminal.run(progress=lambda _: None)["status"], "partial")
            self.assertEqual(ToolApplication.load(root).run(progress=lambda _: None)["status"], "partial")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(tuple(a.attempt_id for a in terminal.session.list_attempts()), attempts_before)
            self.assertEqual((root / "budget_ledger.json").read_bytes(), ledger_before)

    def test_saved_runtime_services_must_be_restored_without_environment_fallback(self) -> None:
        primary, reviewer = _JSONClient(), _JSONClient()
        registry = SearchProviderRegistry()
        services = ResearchApplicationServices(llm_client=primary,
            feasibility_llm_client=reviewer, search_registry=registry,
            config={"research_materials_only": True})
        with TemporaryDirectory() as directory, patch.object(LLMClient, "from_env",
                side_effect=AssertionError("Missing injected clients must fail closed")):
            root = Path(directory) / "task"
            app = create_research_task(ResearchBrief(request_text="Assess the evidence.",
                requested_outputs=("assessment",)), root=root, services=services)
            dependencies = app.state["config"]["runtime_dependencies"]
            self.assertEqual(set(dependencies), {"llm_client", "feasibility_llm_client", "search_registry"})
            self.assertEqual(dependencies["search_registry"], [])
            restored = ToolApplication.load(root, services=services)
            self.assertIs(restored.sources.registry, registry)
            absent = ToolApplication.load(root)
            result = absent.run(progress=lambda _: None)
            self.assertIn("saved injected llm_client", result["status_reason"])
            self.assertEqual(absent.session.list_attempts(), ())
            self.assertEqual(absent.ledger.entries, ())
            for name in ("llm_client", "feasibility_llm_client"):
                with self.subTest(missing=name):
                    missing = ToolApplication.load(root, services=replace(services, **{name: None}))
                    if name == "llm_client":
                        result = missing.run(progress=lambda _: None)
                        self.assertIn("Restore the saved injected llm_client", result["status_reason"])
                        self.assertNotEqual(result["status"], "completed")
                    else:
                        with self.assertRaisesRegex(ValueError, "saved injected feasibility_llm_client"):
                            missing._feasibility_client("native-tool-check")
                    self.assertEqual(missing.session.list_attempts(), ())
                    self.assertEqual(missing.ledger.entries, ())
                changed = _JSONClient()
                changed.model = "different-model"
                with self.subTest(changed=name), self.assertRaisesRegex(ValueError, "differs from the saved task"):
                    ToolApplication.load(root, services=replace(services, **{name: changed}))
            restored = ToolApplication.load(root, services=services)
            restored.services = replace(services, llm_client=changed)
            before = ((root / 'actions.json').read_bytes(), (root / 'budget_ledger.json').read_bytes())
            with self.assertRaisesRegex(ValueError, 'unchanged runtime dependencies'):
                restored.supply(supply_id='changed-service', reason='Add network access',
                    changes={'allow_network': True})
            self.assertEqual(((root / 'actions.json').read_bytes(),
                (root / 'budget_ledger.json').read_bytes()), before)
            self.assertEqual(primary.calls, [])
            self.assertEqual(reviewer.calls, [])

    def test_named_model_selector_reconstructs_from_catalog_without_saved_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = root / "models.toml"
            catalog.write_text('version = 1\n[routes]\ndefault = "acceptance"\nvision = "visual"\n'
                '[profiles.acceptance]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-model"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["text"]\n'
                '[profiles.visual]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-vision"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["vision"]\n', encoding="utf-8")
            with patch.dict(os.environ, {"SIMPLE_AR_MODELS_CONFIG": str(catalog),
                    "ACCEPTANCE_MODEL_KEY": "fixture-secret-never-persist"}), patch.object(
                    LLMClient, "chat_turn", side_effect=AssertionError("Creation/loading must not call models")) as calls:
                app = create_research_task(ResearchBrief(request_text="Assess the evidence.",
                    requested_outputs=("assessment",)), root=root / "task", model="profile:acceptance")
                self.assertEqual(app.state["config"]["model"], "profile:acceptance")
                self.assertNotIn("llm_client", app.state["config"].get("runtime_dependencies", {}))
                resumed = ToolApplication.load(root / "task")
                reconstructed = resumed._client("native-turn-check")
                self.assertEqual(reconstructed.model, "fixture-model")
                self.assertIs(reconstructed._budget_ledger, resumed.ledger)
                self.assertEqual(reconstructed._budget_session_id, app.session.manifest.session_id)
                self.assertEqual(reconstructed._budget_attempt_id, "native-turn-check")
                self.assertNotIn("fixture-secret-never-persist",
                    (root / "task/actions.json").read_text(encoding="utf-8"))
                self.assertEqual(resumed.session.list_attempts(), ())
                self.assertEqual(resumed.ledger.entries, ())
                default = create_research_task(ResearchBrief(request_text="Measure a local result.",
                    requested_outputs=("experiments",)), root=root / "default-route", model="env",
                    services=ResearchApplicationServices(config={"research_materials_only": True},
                        max_chunks=None))
                self.assertEqual(default._client().model, "fixture-model")
                self.assertFalse(default.state['config']['allow_network'])
                self.assertTrue(default.state['config']['allow_execution'])
                self.assertGreater(default.ledger.remaining('process_invocations'), 0)
                self.assertIsNone(default.state['config']['research_policy']['max_chunks'])
                bounded = create_research_task(ResearchBrief(request_text="Measure within caller limits.",
                    requested_outputs=("experiments",)), root=root / "zero-process", model="env",
                    services=ResearchApplicationServices(config={"research_materials_only": True},
                        budget_limits={'process_invocations': 0, 'process_wall_seconds': 0}))
                self.assertEqual(bounded.ledger.remaining('process_invocations'), 0)
                self.assertEqual(bounded.ledger.remaining('process_wall_seconds'), 0)
                writing = create_research_task(ResearchBrief(request_text="Check outside evidence for this article.",
                    requested_outputs=("assessment",)), root=root / "writing-research", model="env",
                    services=ResearchApplicationServices(config={'research_task_kind': 'writing', 'allow_network': True}))
                self.assertTrue(writing.state['config']['allow_network'])
                figure = root / "figure.svg"
                figure.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
                visual_brief = ResearchBrief(request_text="Inspect the supplied image and explain its evidence.",
                    requested_outputs=("assessment",),
                    asset_requests=({'locator': str(figure), 'role': 'material'},))
                visual_services = ResearchApplicationServices(config={'research_materials_only': True},
                    budget_limits={'llm_requests': 2, 'total_tokens': 1000,
                        'process_invocations': 0, 'process_wall_seconds': 0, 'source_operations': 0})
                for label, options in (('omitted', {}), ('denied', {'allow_vision': False}),
                        ('authorized', {'allow_vision': True})):
                    with self.subTest(vision=label), patch.object(ToolApplication, 'create',
                            wraps=ToolApplication.create) as create:
                        visual = create_research_task(visual_brief, root=root / label,
                            model='profile:acceptance', services=visual_services, **options)
                        create.assert_called_once()
                        granted = options.get('allow_vision', False)
                        self.assertIs(create.call_args.kwargs['allow_vision'], granted)
                        self.assertIsInstance(visual, ToolApplication)
                        config = visual.state['config']
                        self.assertIs(config['allow_vision'], granted)
                        if granted:
                            self.assertEqual(config['vision_connection']['profile'], 'visual')
                            self.assertEqual(config['vision_connection']['settings']['model'], 'fixture-vision')
                        else:
                            self.assertIsNone(config['vision_connection'])
                        self.assertFalse(config['allow_network'])
                        self.assertFalse(config['allow_execution'])
                        self.assertEqual(config['max_images'], 0)
                        for resource, limit in visual_services.budget_limits.items():
                            self.assertEqual(visual.ledger.remaining(resource), limit)
                        self.assertEqual(visual.session.list_attempts(), ())
                        self.assertEqual(visual.ledger.entries, ())
                        restored = ToolApplication.load(root / label)
                        self.assertIs(restored.state['config']['allow_vision'], granted)
                        self.assertEqual(restored.state['config']['vision_connection'], config['vision_connection'])
                for invalid in (None, 0, 1, 'true', 'false', [], {}):
                    with self.subTest(allow_vision=invalid), patch.object(ToolApplication, 'create') as create, \
                            patch.object(LLMClient, 'from_env') as connection:
                        with self.assertRaisesRegex(ValueError, 'allow_vision must be a bool'):
                            create_research_task(visual_brief, root=root / 'invalid-vision',
                                model='profile:acceptance', allow_vision=invalid)
                        create.assert_not_called()
                        connection.assert_not_called()
                        self.assertFalse((root / 'invalid-vision').exists())
                calls.assert_not_called()


    def test_public_minimal_code_creation_uses_code_task_defaults_and_runtime_services(self):
        from simple_ar.app.research_execution import code_task_execution_options
        from simple_ar.code_task.runtime.config import load_code_task_init_options, load_code_task_execute_options
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            project = base / 'project'
            project.mkdir()
            (project / 'check.py').write_text('pass\n', encoding='utf-8')
            client, reviewer = _JSONClient(), _JSONClient()
            registry = SearchProviderRegistry()
            task = {'code_root': 'project', 'validation_command': ['python', 'check.py'],
                'validation_timeout_sec': 7}
            execution = {'command': ['python', 'measure.py'], 'cwd': 'project', 'timeout_sec': 9,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'skip', 'code_task': task}
            original = copy.deepcopy(execution)
            services = ResearchApplicationServices(llm_client=client, feasibility_llm_client=reviewer,
                search_registry=registry, input_base_dir=base,
                config={'execution': execution, 'require_design': False, 'independent_review_required': True},
                budget_limits={'llm_requests': 2, 'total_tokens': 100000,
                    'process_invocations': 0, 'process_wall_seconds': 0, 'source_operations': 0})
            with patch.object(LLMClient, 'from_env', side_effect=AssertionError('Use supplied services')):
                app = create_research_task(ResearchBrief(request_text='Implement an adapter.',
                    requested_outputs=('experiments',)), root=base / 'task', services=services)
            saved = app.state['config']
            defaults = code_task_execution_options(load_code_task_init_options(code_root=str(project),
                require_task_file=False), load_code_task_execute_options())
            exact = saved['code_task']
            self.assertEqual(exact['preparation']['kind'], defaults['kind'])
            self.assertEqual(exact['preparation']['workspace_mode'], defaults['workspace_mode'])
            self.assertEqual(exact['preparation']['max_repairs'], defaults['max_repairs'])
            self.assertEqual(exact['allowed_patterns'], defaults['allowed_patterns'] or ['**'])
            self.assertEqual(exact['protected_patterns'], defaults['protected_patterns'])
            self.assertEqual(exact['project_path'], 'source0')
            self.assertEqual(exact['validation_command'], ['python', 'check.py'])
            self.assertEqual(saved['code_work_policy'], 'shared.v2')
            self.assertEqual(saved['code_implementation_policy'], 'direct.v1')
            self.assertEqual(saved['code_review_policy'], 'delivery.v1')
            self.assertEqual(app.operations.implementation.code_work_policy, 'shared.v2')
            self.assertTrue(app.operations.implementation.defer_code_review)
            self.assertTrue(app.operations.implementation.direct_code_edit)
            self.assertEqual(saved['delivery_review_selection'], 'model.v3')
            optional = create_research_task(ResearchBrief(request_text='Implement an adapter.',
                requested_outputs=('experiments',)), root=base / 'optional-review',
                services=replace(services, config={'execution': execution, 'require_design': False}))
            self.assertIsNone(optional.state['config']['code_review_policy'])
            self.assertFalse(optional.operations.implementation.defer_code_review)
            optional.supply(supply_id='require-independent', reason='The caller requested independent review',
                changes={'independent_review_required': True})
            self.assertEqual(optional.state['config']['code_review_policy'], 'delivery.v1')
            self.assertTrue(optional.operations.implementation.defer_code_review)
            optional.supply(supply_id='make-review-optional', reason='The caller changed the review requirement',
                changes={'independent_review_required': False}, replace_existing=True)
            self.assertFalse(optional.state['config']['independent_review_required'])
            self.assertIsNone(optional.state['config']['code_review_policy'])
            self.assertFalse(optional.operations.implementation.defer_code_review)
            alias = create_research_task(ResearchBrief(request_text='Implement an adapter.',
                requested_outputs=('code',)), root=base / 'code-alias', services=services)
            self.assertIsNotNone(alias.state['config']['code_task'])
            self.assertIsNone(alias.state['config']['execution_contract'])
            self.assertEqual(alias.session.store.read_json(alias.session.manifest.state_refs['brief'])['requested_outputs'],
                ['code'])
            self.assertIn('model review belongs to the independent task delivery review',
                app.gateway.registry.get('implement_code_task').description)
            self.assertIs(app.operations.implementation.ledger, app.ledger)
            self.assertIn('implement_code_task', app.operations.system_text())
            self.assertNotIn('write_file and run_process', app.operations.system_text())
            self.assertFalse(app.gateway.policy.is_allowed(app.gateway.registry.get('write_file'))[0])
            self.assertEqual(app.operations.implementation.work_context([str(project)])['user_requests'],
                app.state['requests'])
            self.assertIs(app.operations.document.ledger, app.ledger)
            restored = ToolApplication.load(base / 'task', services=services)
            self.assertEqual(restored.operations.implementation.code_work_policy, 'shared.v2')
            self.assertTrue(restored.operations.implementation.defer_code_review)
            self.assertTrue(restored.operations.implementation.direct_code_edit)
            self.assertIs(restored.operations.implementation.ledger, restored.ledger)
            self.assertEqual(restored.operations.implementation.work_context([str(project)])['user_requests'],
                app.state['requests'])
            # Saved code sessions predate this policy. An unrelated explicit
            # review requirement must not change their in-flight editor path.
            historical = create_research_task(ResearchBrief(request_text='Implement an adapter.',
                requested_outputs=('experiments',)), root=base / 'historical-task', services=services)
            historical.state['config'].pop('code_implementation_policy')
            historical._save()
            historical = ToolApplication.load(base / 'historical-task', services=services)
            self.assertFalse(historical.operations.implementation.direct_code_edit)
            historical.supply(supply_id='require-review', reason='Require independent review',
                changes={'independent_review_required': True})
            historical = ToolApplication.load(base / 'historical-task', services=services)
            self.assertNotIn('code_implementation_policy', historical.state['config'])
            self.assertFalse(historical.operations.implementation.direct_code_edit)
            self.assertEqual(saved['execution_contract']['conditions'][0]['source'], 'implementation')
            self.assertEqual(saved['execution_contract']['conditions'][0]['execution']['command'][1], 'measure.py')
            self.assertIs(app.services, services)
            self.assertIs(app.sources.registry, registry)
            self.assertFalse(saved['allow_network'])
            self.assertEqual(app.ledger.remaining('process_invocations'), 0)
            self.assertEqual(app.ledger.remaining('process_wall_seconds'), 0)
            self.assertEqual(app.ledger.remaining('source_operations'), 0)
            self.assertEqual(app.session.list_attempts(), ())
            self.assertEqual(client.calls, [])
            self.assertEqual(reviewer.calls, [])
            self.assertEqual(execution, original)
            self.assertEqual((project / 'check.py').read_text(), 'pass\n')
            supplied_services = replace(services, config={'research_materials_only': True})
            continuing = create_research_task(ResearchBrief(request_text='Inspect the supplied project.',
                requested_outputs=('summary',),
                asset_requests=({'locator': str(project), 'role': 'code'},)),
                root=base / 'continued-task', services=supplied_services)
            self.assertNotIn('code_work_policy', continuing.state['config'])
            before_ledger = continuing.ledger.to_dict()
            continuing.supply(supply_id='code-work', reason='The caller provided the code scope and validator',
                changes={'allow_execution': True, 'code_task': exact})
            self.assertEqual(continuing.state['config']['code_work_policy'], 'shared.v2')
            self.assertTrue(continuing.operations.implementation.direct_code_edit)
            self.assertEqual(continuing.operations.implementation.code_work_policy, 'shared.v2')
            self.assertFalse(continuing.operations.implementation.defer_code_review)
            self.assertIn('CodeTask also performs its own independent review',
                continuing.gateway.registry.get('implement_code_task').description)
            self.assertIs(continuing.operations.implementation.ledger, continuing.ledger)
            self.assertEqual(continuing.ledger.to_dict(), before_ledger)
            self.assertEqual(continuing.operations.implementation.work_context([str(project)])['user_requests'],
                continuing.state['requests'])
            reviewed = create_research_task(ResearchBrief(request_text='Inspect and independently check the project.',
                requested_outputs=('summary',), asset_requests=({'locator': str(project), 'role': 'code'},)),
                root=base / 'continued-reviewed-task', services=supplied_services)
            before = reviewed.ledger.to_dict()
            reviewed.supply(supply_id='reviewed-code', reason='The caller supplied code scope and required review',
                changes={'allow_execution': True, 'code_task': exact, 'independent_review_required': True})
            self.assertTrue(reviewed.operations.implementation.defer_code_review)
            self.assertEqual(reviewed.ledger.to_dict(), before)
            reviewed = ToolApplication.load(base / 'continued-reviewed-task', services=supplied_services)
            self.assertTrue(reviewed.operations.implementation.defer_code_review)
            reviewed.supply(supply_id='optional-review', reason='The caller revised the review requirement',
                changes={'independent_review_required': False}, replace_existing=True)
            self.assertFalse(reviewed.operations.implementation.defer_code_review)
            self.assertEqual(reviewed.ledger.to_dict(), before)
            paper = base / 'method.md'
            paper.write_text('A supplied method description, not verified implementation evidence.', encoding='utf-8')
            for outputs in (('code_task', 'summary'), ('code_task', 'experiments'), ('code_task', 'report')):
                combined = create_research_task(ResearchBrief(request_text='Use the supplied method and implement it.',
                    requested_outputs=outputs, asset_requests=({'locator': str(paper), 'role': 'paper'},)),
                    root=base / '-'.join(outputs), services=services)
                self.assertEqual(combined.state['config']['code_task']['project_path'], 'source0')
                self.assertEqual(combined.sources.policy.materials, ('source1',))
                self.assertFalse(combined.sources.policy.require_design)  # Explicit caller choice remains authoritative.
                self.assertEqual(combined.sources.policy.require_summary, 'summary' in outputs)
                self.assertEqual(combined.state['config'].get('execution_contract') is not None,
                    'experiments' in outputs)
                if 'report' in outputs:
                    self.assertIn('requested_report', combined.operations.document.work_context([]))
                self.assertEqual(combined.session.list_attempts(), ())
            with self.assertRaisesRegex(ValueError, 'execution.code_task'):
                create_research_task(ResearchBrief(request_text='Do not claim code work from a bare command.',
                    requested_outputs=('code_task', 'experiments')), root=base / 'invalid-code',
                    services=replace(services, config={'execution': {key: value for key, value in execution.items()
                        if key != 'code_task'}, 'require_design': False}))
            self.assertFalse((base / 'invalid-code').exists())

    def test_public_fixed_conditions_environment_and_confirmation_only_create(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': '.', 'timeout_sec': 5,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'run',
                'seeds': [4, 9], 'seed_flag': '--seed',
                'environment': {'mode': 'current', 'timeout_sec': 5,
                    'check_command': [sys.executable, '-c', 'pass']}}
            original = copy.deepcopy(execution)
            for confirm in (False, True):
                services = ResearchApplicationServices(input_base_dir=base,
                    config={'execution': execution, 'execution_confirmation': confirm, 'require_design': False})
                app = create_research_task(ResearchBrief(request_text='Measure these exact conditions.',
                    requested_outputs=('experiments',)), root=base / str(confirm), services=services)
                saved = app.state['config']
                conditions = saved['execution_contract']['conditions']
                self.assertEqual([(row['role'], row['seed']) for row in conditions],
                    [('baseline', 4), ('candidate', 4), ('baseline', 9), ('candidate', 9)])
                self.assertTrue(all(row['execution']['cwd'] == str(base) for row in conditions))
                self.assertIsNone(saved.get('code_task'))
                self.assertEqual(app.ledger.remaining('process_invocations'), 5)
                self.assertEqual(app.ledger.remaining('process_wall_seconds'), 25)
                self.assertFalse(saved['allow_network'])
                if confirm:
                    self.assertTrue(saved['research_policy']['require_design'])
                    self.assertFalse(saved['research_policy']['allow_network'])
                    self.assertEqual(app.sources.pending_decision()['proposal_kind'], 'caller_execution_protocol')
                self.assertEqual(app.session.list_attempts(), ())
                self.assertFalse(app.ledger.entries)
            self.assertEqual(execution, original)

    def test_execution_mount_projection_preserves_explicit_source_aliases_and_suffixes(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            docs, project = base / 'docs', base / 'project'
            docs.mkdir()
            project.mkdir()
            (docs / 'paper.md').write_text('Declared method', encoding='utf-8')
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': 'project', 'timeout_sec': 5,
                'result_schema': {'primary_metric': 'score'}, 'baseline_policy': 'skip'}
            for with_code in (False, True):
                declared = {**execution, **({'code_task': {'code_root': 'project',
                    'validation_command': ['python', 'check.py'], 'validation_timeout_sec': 5}} if with_code else {})}
                config = {'execution': declared, 'require_design': True, 'research_materials_only': True,
                    'materials': ['source0/paper.md'], 'source_workspace': 'source1' if with_code else 'source0'}
                original = copy.deepcopy(config)
                services = ResearchApplicationServices(llm_client=_JSONClient(), input_base_dir=base, config=config)
                brief = ResearchBrief(request_text='Honor declared materials.', requested_outputs=('experiments',),
                    asset_requests=({'locator': 'docs', 'role': 'material'},
                        *(({'locator': 'project', 'role': 'code'},) if with_code else ())))
                app = create_research_task(brief, root=base / str(with_code), services=services)
                policy = app.state['config']['research_policy']
                self.assertEqual(policy['materials'], ['source1/paper.md'] if with_code else ['source0/paper.md'])
                self.assertEqual(policy['source_workspace'], 'source0')
                if with_code:
                    self.assertEqual(app.state['config']['sources']['source1'], str(docs))
                    self.assertEqual(app.state['config']['sources']['source0'], str(project))
                else:
                    self.assertEqual(app.state['config']['sources']['source0'], str(docs))
                self.assertEqual(config, original)
                absolute = create_research_task(brief, root=base / f'absolute-{with_code}',
                    services=replace(services, config={**config, 'materials': [str(docs / 'paper.md')],
                        'source_workspace': str(project if with_code else docs)}))
                self.assertEqual(absolute.sources.policy.materials, (str(docs / 'paper.md'),))
                with patch.object(ToolApplication, 'create') as create, self.assertRaisesRegex(ValueError, 'original asset mount'):
                    create_research_task(brief, root=base / 'invalid', services=replace(services,
                        config={**config, 'materials': ['source9/paper.md']}))
                create.assert_not_called()
                if with_code:
                    with patch.object(ToolApplication, 'create') as create, self.assertRaisesRegex(ValueError, 'conflicts'):
                        create_research_task(brief, root=base / 'conflict', services=replace(services,
                            config={**config, 'source_workspace': 'source0'}))
                    create.assert_not_called()

    def test_public_execution_rejects_adoption_open_design_and_unknown_controls_before_creation(self):
        from simple_ar.app.research_intake import UnsupportedExecutionCreation
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            execution = {'command': [sys.executable, 'measure.py'], 'cwd': str(base),
                'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'}}
            cases = [
                ({**execution, 'code_task': {'run_dir': str(base / 'historical')}}, False),
                ({**execution, 'baseline_policy': 'reuse'}, False),
                ({**execution, 'unmapped_control': True}, False),
            ]
            with patch.object(ToolApplication, 'create') as create:
                for declared, design in cases:
                    with self.subTest(declared=declared), self.assertRaises(UnsupportedExecutionCreation):
                        create_research_task(ResearchBrief(request_text='Keep explicit ownership.',
                            requested_outputs=('experiments',)), root=base / 'task',
                            services=ResearchApplicationServices(input_base_dir=base,
                                config={'execution': declared, 'require_design': design}))
                create.assert_not_called()
            self.assertFalse((base / 'task').exists())

    def test_public_reuse_keeps_producing_lineage_with_original_owner(self):
        import sys
        with TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            baseline = base / 'baseline.json'
            baseline.write_text(json.dumps({'implementation_ref': 'records/old/implementation.json',
                'preparation': {'attempt_id': 'old'}}), encoding='utf-8')
            original = baseline.read_bytes()
            with self.assertRaisesRegex(ValueError, 'baseline'):
                create_research_task(ResearchBrief(request_text='Reuse a measured baseline.',
                    requested_outputs=('experiments',)), root=base / 'task',
                    services=ResearchApplicationServices(input_base_dir=base,
                        config={'execution': {'command': [sys.executable, 'measure.py'], 'cwd': '.',
                            'timeout_sec': 5, 'result_schema': {'primary_metric': 'score'},
                            'baseline_policy': 'reuse', 'baseline_ref': 'baseline.json'},
                            'require_design': False}))
            self.assertEqual(baseline.read_bytes(), original)
            self.assertFalse((base / 'task').exists())

    def test_plain_text_is_preserved_without_inferred_intent(self) -> None:
        brief = parse_research_input("  Compare two small classifiers.\n")

        self.assertEqual(brief.request_text, "  Compare two small classifiers.\n")
        self.assertEqual(brief.intents, ())
        self.assertEqual(brief.requested_outputs, ())
        self.assertEqual(brief.asset_requests, ())
        diagnostics = validate_brief(brief, ())
        self.assertEqual([item.code for item in diagnostics], ["outputs_unspecified"])
        self.assertEqual(diagnostics[0].severity, "info")

    def test_markdown_request_and_relative_asset_get_light_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            request_path = root / "request.md"
            paper_path = root / "notes" / "paper.md"
            paper_path.parent.mkdir()
            request_path.write_text("# Reproduce\nCompare the baseline and candidate.", encoding="utf-8")
            paper_path.write_text("A small evidence note.", encoding="utf-8")

            brief = parse_research_input(request_path, asset_paths=(Path("notes/paper.md"),))
            assets = normalize_assets(brief, input_base_dir=request_path.parent)

        self.assertEqual(brief.source_path, str(request_path.resolve()))
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].kind, "document")
        self.assertEqual(assets[0].availability, "available")
        self.assertEqual(assets[0].identity["fingerprint_strength"], "content")
        self.assertTrue(assets[0].identity["sha256"])
        self.assertEqual(assets[0].diagnostics, ())

    def test_toml_explicit_fields_and_same_asset_roles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "digits.csv"
            dataset.write_text("x,y\n1,0\n", encoding="utf-8")
            config = root / "research.toml"
            config.write_text(
                """
request_text = "Evaluate a lightweight feature change."
objective = "Compare the candidate against the baseline."
intents = ["evaluate", "implement"]
requested_outputs = ["experiment", "report"]

[[assets]]
path = "digits.csv"
role = "dataset"

[[assets]]
path = "digits.csv"
role = "baseline"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            brief = parse_research_input("", config_path=config)
            assets = normalize_assets(brief, input_base_dir=config.parent)

            output = root / "intake"
            paths = write_intake_artifacts(brief, assets, output)
            restored = ResearchBrief.from_dict(json.loads(paths["brief"].read_text()))
            markdown = paths["markdown"].read_text(encoding="utf-8")

        self.assertEqual(brief.objective, "Compare the candidate against the baseline.")
        self.assertEqual(brief.intents, ("evaluate", "implement"))
        self.assertEqual(brief.requested_outputs, ("experiment", "report"))
        self.assertEqual(len(assets), 2)
        self.assertEqual(assets[0].asset_id, assets[1].asset_id)
        self.assertEqual({asset.role for asset in assets}, {"dataset", "baseline"})
        self.assertEqual(validate_brief(brief, assets), ())
        self.assertEqual(restored.request_text, brief.request_text)
        self.assertIn("## Assets", markdown)

    def test_missing_asset_is_explicit_warning_not_research_failure(self) -> None:
        brief = parse_research_input(
            "Summarize the literature.",
            asset_paths=("missing-paper.pdf",),
        )
        assets = normalize_assets(brief, input_base_dir=Path.cwd())
        diagnostics = validate_brief(brief, assets)

        self.assertEqual(assets[0].availability, "missing")
        self.assertIn("asset_missing", [item.code for item in diagnostics])
        self.assertNotIn("error", [item.severity for item in diagnostics])

    def test_invalid_input_reports_diagnostic(self) -> None:
        with self.assertRaises(ResearchInputError) as raised:
            parse_research_input("")

        self.assertEqual(raised.exception.diagnostics[0].code, "request_text_required")

if __name__ == "__main__":
    unittest.main()
