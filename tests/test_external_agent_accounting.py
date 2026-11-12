from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from simple_ar.agent_backends import AgentRunRequest, AgentRunResult
from simple_ar.agent_backends.external_cli import ExternalCliBackend, ExternalCliBackendConfig
from simple_ar.code_task.generation.agent_backend import check_agent_budget, write_greenfield_project_from_agent_backend
from simple_ar.core.budget import BudgetError, BudgetLedger


class ExternalAgentAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.request = AgentRunRequest(provider='external_cli', run_dir=self.root,
            handoff_dir=self.root / 'handoff')

    def backend(self, ledger=None, command=None):
        return ExternalCliBackend(ExternalCliBackendConfig(provider='external_cli', binary=sys.executable,
            enabled=True, timeout_sec=5, command_template=command or
                ('{binary}', '-c', "print('ordinary local process')")),
            ledger=ledger, session_id='session', attempt_id='attempt')

    def test_actual_process_keeps_unknown_model_use_in_original_ledger(self):
        ledger = BudgetLedger({'process_invocations': 2, 'process_wall_seconds': 10},
                              storage_path=self.root / 'original-ledger.json')
        result = self.backend(ledger).run(self.request)
        self.assertTrue(result.ok)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        entry, = BudgetLedger.load(ledger.storage_path).entries
        self.assertEqual((entry.status, entry.session_id, entry.attempt_id), ('unknown', 'session', 'attempt'))
        self.assertEqual(entry.actual['process_invocations'], 1)
        self.assertGreaterEqual(entry.actual['process_wall_seconds'], 0)
        self.assertEqual(set(ledger.unknown_dimensions()), {'llm_requests', 'total_tokens'})

    def test_finite_model_budget_does_not_start_unaccountable_cli(self):
        ledger = BudgetLedger({'llm_requests': 2, 'total_tokens': 10000})
        with patch('simple_ar.agent_backends.external_cli.subprocess.run') as launch:
            with self.assertRaises(BudgetError):
                self.backend(ledger).run(self.request)
        launch.assert_not_called()
        self.assertEqual(ledger.entries, ())
        with self.assertRaises(BudgetError):
            check_agent_budget(self.root, 'external_cli', enabled=True, binary=sys.executable,
                               mode='handoff', ledger=ledger)

    def test_standalone_cli_retains_unknown_usage_at_the_task_root(self):
        with patch('simple_ar.agent_backends.external_cli.subprocess.run',
                   return_value=subprocess.CompletedProcess(['agent'], 0, stdout='ok', stderr='')):
            result = self.backend().run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        entry, = BudgetLedger.load(self.root / 'budget_ledger.json').entries
        self.assertEqual(entry.status, 'unknown')

    def test_missing_program_releases_reservation_but_timeout_remains_unknown(self):
        ledger = BudgetLedger({})
        with patch('simple_ar.agent_backends.external_cli.subprocess.run', side_effect=FileNotFoundError('missing')):
            result = self.backend(ledger).run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'not_started')
        self.assertEqual(ledger.entries[0].status, 'released')
        with patch('simple_ar.agent_backends.external_cli.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('agent', 5, output=b'partial')):
            result = self.backend(ledger).run(self.request)
        self.assertEqual(result.metadata['usage_status'], 'unknown')
        self.assertEqual(ledger.entries[-1].status, 'unknown')
        self.assertEqual((self.request.handoff_dir / 'stdout.txt').read_text(), 'partial')

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
