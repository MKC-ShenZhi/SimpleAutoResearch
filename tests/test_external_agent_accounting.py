from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from simple_ar.agent_backends import AgentRunRequest, AgentRunResult
from simple_ar.agent_backends.external_cli import ExternalCliBackend, ExternalCliBackendConfig
from simple_ar.code_task.generation.agent_backend import (
    check_agent_budget, retained_greenfield_plan, write_greenfield_project_from_agent_backend)
from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.core.process import verify_process_accounting
from simple_ar.core.artifacts import read_json, write_json


class ExternalAgentAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = AgentRunRequest(provider='external_cli', run_dir=self.root,
            handoff_dir=self.root / 'handoff')

    def backend(self, ledger=None, command=None, timeout=5):
        return ExternalCliBackend(ExternalCliBackendConfig(provider='external_cli', binary=sys.executable,
            enabled=True, timeout_sec=timeout, command_template=command or
                ('{binary}', '-c', "print('ordinary local process')")),
            ledger=ledger, session_id='session', attempt_id='attempt')

    def test_actual_process_keeps_unknown_model_use_in_original_ledger(self):
        ledger = BudgetLedger({'process_invocations': 2, 'process_wall_seconds': 10},
                              storage_path=self.root / 'original-ledger.json')
        result = self.backend(ledger).run(self.request)
        self.assertTrue(result.ok)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        saved = BudgetLedger.load(ledger.storage_path)
        entry = next(row for row in saved.entries if row.purpose == 'external_agent')
        self.assertEqual((entry.status, entry.session_id, entry.attempt_id), ('unknown', 'session', 'attempt'))
        record = read_json(Path(result.metadata['process_record']))
        verify_process_accounting(record, saved, session_id='session', attempt_id='attempt', timeout_sec=5,
                                  accounting_purpose='external_agent_process')
        self.assertNotIn('local_process', {row.purpose for row in saved.entries})
        self.assertTrue(record['argv_redacted'])
        self.assertNotIn('ordinary local process', str(record['argv']))
        self.assertEqual(set(ledger.unknown_dimensions()), {'llm_requests', 'total_tokens'})
        from simple_ar.code_task.execution.runner import recover_implementation_process
        with patch('simple_ar.code_task.execution.runner.load_code_task_manifest',
                   return_value={'code_task': {'native_binding': {'contract': {}}}}), \
             patch('simple_ar.code_task.execution.runner.code_task_paths',
                   return_value=SimpleNamespace(run_artifact_dir=self.root / 'artifacts')), \
             patch('simple_ar.code_task.execution.runner.native_preparation_processes', return_value=set()):
            self.assertIsNone(recover_implementation_process(self.root, budget_ledger=saved,
                                                             session_id='session', attempt_id='attempt'))

    def test_finite_model_budget_does_not_start_unaccountable_cli(self):
        ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 10000})
        with patch('simple_ar.agent_backends.external_cli.run_process') as launch:
            with self.assertRaises(BudgetError):
                self.backend(ledger).run(self.request)
        launch.assert_not_called()
        self.assertEqual(ledger.entries, ())
        with self.assertRaises(BudgetError):
            check_agent_budget(self.root, 'external_cli', enabled=True, binary=sys.executable,
                               mode='handoff', ledger=ledger)

    def test_standalone_cli_retains_unknown_usage_at_the_task_root(self):
        result = self.backend().run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        entry = next(row for row in BudgetLedger.load(self.root / 'budget_ledger.json').entries
                     if row.purpose == 'external_agent')
        self.assertEqual(entry.status, 'unknown')

    def test_completed_handoff_is_reused_and_interrupted_handoff_is_not_replayed(self):
        ledger = BudgetLedger({})
        backend = self.backend(ledger)
        result = backend.run(self.request)
        before = ledger.to_dict()
        with patch('simple_ar.agent_backends.external_cli.run_process', side_effect=AssertionError('replayed')):
            retained = backend.run(self.request)
        self.assertEqual(retained.status, result.status)
        self.assertEqual(retained.metadata, result.metadata)
        self.assertEqual(ledger.to_dict(), before)

        (self.request.handoff_dir / 'agent_run.json').unlink()
        with patch('simple_ar.agent_backends.external_cli.run_process', side_effect=AssertionError('replayed')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                backend.run(self.request)
        self.assertEqual(ledger.to_dict(), before)

        interrupted = self.request.model_copy(update={'handoff_dir': self.root / 'interrupted'})
        ledger.reserve('external-agent-pending', {'llm_requests': 0, 'total_tokens': 0},
            session_id='session', attempt_id='attempt', purpose='external_agent')
        write_json(interrupted.handoff_dir / 'agent_run.json', {'usage_status': 'starting',
            'usage_reservation': 'external-agent-pending', 'process_record': 'processes/pending/invocation.json'})
        with patch('simple_ar.agent_backends.external_cli.run_process', side_effect=AssertionError('replayed')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                backend.run(interrupted)

    def test_greenfield_reuses_the_original_plan_and_external_result(self):
        ledger = BudgetLedger({})
        command = ('{binary}', '-c', "from pathlib import Path; p=Path('generated_files'); "
            "p.mkdir(); (p/'main.py').write_text('print(1)\\n')")
        backend = self.backend(ledger, command=command)
        handoff = self.root / 'agent_handoff' / 'code-task-greenfield-external_cli'
        context = handoff / 'context'
        context.mkdir(parents=True)
        contract, schema, architecture = {'goal': 'Example'}, {'metric': 'value'}, {'files': [{'path': 'main.py'}]}
        for name, value in [('contract.json', contract), ('result_schema.json', schema),
                            ('architecture_plan.json', architecture)]:
            write_json(context / name, value)
        write_json(self.root / 'code_task/meta/implementation_plan.json',
            {'architecture_mode': 'existing', 'ablation': {'planning_snapshot': None}})
        request = AgentRunRequest(provider='external_cli', run_dir=self.root, handoff_dir=handoff,
            workspace_dir=self.root / 'code_task/workspace')
        backend.run(request)
        before = ledger.to_dict()
        with patch('simple_ar.code_task.generation.agent_backend.create_agent_backend', return_value=backend), \
             patch('simple_ar.code_task.generation.agent_backend.build_code_task_greenfield_handoff',
                   side_effect=AssertionError('replaced handoff')), \
             patch('simple_ar.agent_backends.external_cli.run_process', side_effect=AssertionError('replayed')):
            plan = retained_greenfield_plan(run_dir=self.root, provider='external_cli', client=None,
                enabled=True, binary=sys.executable, model='', timeout_sec=5, args=(), ledger=ledger,
                session_id='session', attempt_id='attempt', contract=contract, result_schema=schema)
            artifacts, result = write_greenfield_project_from_agent_backend(run_dir=self.root,
                project_dir=self.root / 'code_task/workspace/generated_project', provider='external_cli',
                agent_mode='handoff', contract=contract, result_schema=schema, architecture=architecture,
                memory={}, client=None, timeout_sec=5, external_enabled=True, budget_ledger=ledger,
                session_id='session', attempt_id='attempt')
        self.assertEqual(plan, (architecture, 'existing', None))
        self.assertEqual(result['usage_status'], 'unknown')
        self.assertTrue((self.root / 'code_task/workspace/generated_project/main.py').is_file())
        self.assertEqual(ledger.to_dict(), before)

    def test_missing_program_releases_reservation_but_timeout_remains_unknown(self):
        ledger = BudgetLedger({})
        result = self.backend(ledger, command=(str(self.root / 'missing-program'),)).run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'not_started')
        self.assertEqual(ledger.entries[0].status, 'released')
        result = self.backend(ledger, timeout=1, command=('{binary}', '-c',
            "import time; print('partial', flush=True); time.sleep(3)")).run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        self.assertEqual([row.status for row in ledger.entries if row.purpose == 'external_agent'], ['released', 'unknown'])
        self.assertEqual((self.request.handoff_dir / 'stdout.txt').read_text().strip(), 'partial')

    def test_unknown_external_failure_is_not_retried_by_code_task(self):
        handoff = self.root / 'handoff'
        handoff.mkdir()
        backend = Mock()
        backend.run.return_value = AgentRunResult(provider='external_cli', status='failed',
            metadata={'usage_status': 'unknown', 'usage_reservation': 'external-agent-example'})
        with patch('simple_ar.code_task.generation.agent_backend.build_code_task_greenfield_handoff',
                   return_value=SimpleNamespace(handoff_dir=handoff)), \
             patch('simple_ar.code_task.generation.agent_backend.create_agent_backend', return_value=backend), \
             patch('simple_ar.code_task.generation.agent_backend.ingest_agent_outputs', return_value={}):
            with self.assertRaisesRegex(RuntimeError, 'did not produce'):
                write_greenfield_project_from_agent_backend(run_dir=self.root,
                    project_dir=self.root / 'project', provider='external_cli', agent_mode='handoff',
                    contract={}, result_schema={}, architecture={}, memory={}, client=None,
                    timeout_sec=5, external_enabled=True)
        self.assertEqual(backend.run.call_count, 1)
