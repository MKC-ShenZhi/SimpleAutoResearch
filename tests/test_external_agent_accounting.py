from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from simple_ar.agent_backends import AgentRunRequest, AgentRunResult
from simple_ar.agent_backends.external_cli import ExternalCliBackend, ExternalCliBackendConfig
from simple_ar.code_task.generation.agent_backend import check_agent_budget, write_greenfield_project_from_agent_backend
from simple_ar.core.budget import BudgetError, BudgetLedger
from simple_ar.core.process import verify_process_accounting
from simple_ar.core.artifacts import read_json


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
