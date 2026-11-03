from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from simple_ar.core import (
    CapabilityContext,
    CapabilityRegistry,
    CapabilityResult,
    SessionBusyError,
    SessionController,
    SessionFileLock,
)


class SessionLockTests(unittest.TestCase):
    def test_lock_is_non_blocking_and_releases_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".session.lock"
            first = SessionFileLock(path)
            second = SessionFileLock(path)

            with first:
                with self.assertRaises(SessionBusyError):
                    second.acquire()

            owner = SessionFileLock.read_owner(path)
            self.assertIsNotNone(owner)
            assert owner is not None
            self.assertEqual(owner["pid"], os.getpid())
            second.acquire()
            second.release()

    def test_controller_holds_lock_during_physical_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            controller = SessionController.create(
                tmp,
                session_id="lock-session",
                topic="lock boundary",
                registry=registry,
            )
            other = SessionController.load(tmp, registry=registry)

            def handler(**_: object) -> CapabilityResult:
                with self.assertRaises(SessionBusyError):
                    other.pause("must not mutate while another attempt runs")
                return CapabilityResult(status="completed")

            registry.register("probe", handler)
            result = controller.execute_attempt("probe", attempt_id="probe-001")

            self.assertEqual(result.status, "completed")
            self.assertEqual(controller.manifest.status, "running")
            # A failed lock acquisition must not corrupt the re-entrant depth;
            # otherwise this controller could skip the OS lock on its next
            # mutation.
            self.assertEqual(other._lock_depth, 0)

    def test_result_on_disk_is_reconciled_without_running_handler_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []
            registry = CapabilityRegistry()

            def handler(*, context: CapabilityContext) -> CapabilityResult:
                calls.append(context.attempt.attempt_id)
                return CapabilityResult(status="completed")

            registry.register("probe", handler)
            controller = SessionController.create(
                tmp,
                session_id="reconcile-session",
                topic="result recovery",
                registry=registry,
            )
            attempt_store, attempt = controller.store.new_attempt(
                "probe-0001",
                capability="probe",
            )
            running_attempt = replace(attempt, status="running")
            attempt_store.write_attempt_manifest(running_attempt)
            controller.manifest.status = "running"
            controller.manifest.current_attempt = "probe-0001"
            controller.save()
            attempt_store.write_capability_result(CapabilityResult(status="completed"))

            resumed = SessionController.load(tmp, registry=registry)
            result = resumed.reconcile_attempt()

            self.assertEqual(result.status, "completed")
            self.assertEqual(calls, [])
            self.assertEqual(resumed.manifest.budget.attempts, 1)
            self.assertEqual(resumed.manifest.budget.recorded_attempts, ["probe-0001"])
            self.assertEqual(resumed.list_attempts()[0].status, "completed")
            resumed.reconcile_attempt("probe-0001")
            self.assertEqual(resumed.manifest.budget.attempts, 1)

    def test_stale_writer_cannot_reset_persisted_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("probe", lambda **_: CapabilityResult(status="completed"))
            first = SessionController.create(tmp, session_id="test", topic="test", registry=registry)
            stale = SessionController.load(tmp, registry=registry)
            first.execute_attempt("probe")
            with self.assertRaisesRegex(RuntimeError, "reload"):
                stale.pause("stale writer")
            reloaded = SessionController.load(tmp, registry=registry)
            reloaded.pause("fresh writer")
            self.assertEqual(reloaded.manifest.budget.attempts, 1)


class FeedbackReceptionTests(unittest.TestCase):
    def create(self, root):
        from simple_ar.app.tool_application import ToolApplication
        return ToolApplication.create(Path(root) / 'task', 'Original caller request',
            model=None, sources={})

    def test_reception_is_durable_under_execution_lock_and_does_not_change_authority(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            root = app.session.store.root
            before = {name: (root / name).read_bytes() for name in
                ('actions.json', 'session_manifest.json', 'budget_ledger.json')}
            receipts, failures = [], []

            def receive():
                try:
                    receipts.append(ToolApplication.submit_feedback(root,
                        submission_id='message-1', feedback='Revise only the explanation.\n'))
                except Exception as exc:
                    failures.append(exc)

            with app.session.mutation_scope():
                worker = threading.Thread(target=receive, daemon=True)
                worker.start()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), 'Reception must not wait for the run lock')
                self.assertEqual(failures, [])
                self.assertEqual(receipts[0]['status'], 'pending')
                self.assertTrue(receipts[0]['received'])
                self.assertEqual({name: (root / name).read_bytes() for name in before}, before)
            repeated = ToolApplication.submit_feedback(root, submission_id='message-1',
                feedback='Revise only the explanation.\n')
            self.assertEqual(repeated, receipts[0])
            self.assertEqual(ToolApplication.load(root).result_snapshot()['feedback_submissions'], receipts)
            with self.assertRaisesRegex(ValueError, 'different text'):
                ToolApplication.submit_feedback(root, submission_id='message-1',
                    feedback='Revise only the explanation.')
            self.assertEqual(ToolApplication.feedback_status(root, submission_id='message-1'), repeated)

    def test_adoption_is_once_per_id_in_arrival_order_across_reload_and_retry(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            root = app.session.store.root
            config = app.state['config'].copy()
            ledger = app.ledger.to_dict()
            for identity in ('first', 'second'):
                ToolApplication.submit_feedback(root, submission_id=identity, feedback='Same exact text')
            app = ToolApplication.load(root)
            app.run(submission_id='second', should_stop=lambda: True, progress=lambda _: None)
            self.assertEqual(app.state['requests'], ['Original caller request', 'Same exact text', 'Same exact text'])
            for index, identity in enumerate(('first', 'second'), 1):
                receipt = ToolApplication.feedback_status(root, submission_id=identity)
                self.assertEqual((receipt['status'], receipt['request_index']), ('applied', index))
            self.assertEqual(app.state['config'], config)
            self.assertEqual(app.ledger.to_dict(), ledger)
            actions = (root / 'actions.json').read_bytes()
            app = ToolApplication.load(root)
            with patch.object(app, '_run', side_effect=AssertionError('Duplicate input must not execute')):
                app.run(feedback='Same exact text', submission_id='second', progress=lambda _: None)
            self.assertEqual((root / 'actions.json').read_bytes(), actions)
            self.assertEqual(app.ledger.to_dict(), ledger)

    def test_live_tool_receipt_is_saved_before_feedback_cancels_unstarted_work(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            entered, release = threading.Event(), threading.Event()
            physical_call = app.workspace.call
            writes, failures = [], []

            def blocked(name, arguments, **kwargs):
                if name == 'write_file':
                    writes.append(arguments['path'])
                    entered.set()
                    if not release.wait(timeout=5):
                        raise RuntimeError('Test tool was not released')
                return physical_call(name, arguments, **kwargs)

            def run():
                try:
                    app.run(actions=[{'tool_name': 'write_file', 'arguments': {
                        'path': name, 'content': 'Real saved output'}} for name in ('first.txt', 'superseded.txt')],
                        progress=lambda _: None)
                except Exception as exc:
                    failures.append(exc)

            with patch.object(app.workspace, 'call', side_effect=blocked):
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(timeout=5))
                    receipt = ToolApplication.submit_feedback(app.session.store.root,
                        submission_id='during-tool', feedback='Keep the completed output; cancel the unstarted edit.')
                    self.assertEqual(receipt['status'], 'pending')
                finally:
                    release.set()
                    worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])
            self.assertEqual(writes, ['first.txt'])
            self.assertTrue((app.workspace.workspace / 'first.txt').is_file())
            self.assertFalse((app.workspace.workspace / 'superseded.txt').exists())
            self.assertEqual(app.state['turns'][0]['request_count'], 1)
            first = app.session.store.read_json(app.state['turns'][0]['tools'][0]['result'])
            self.assertEqual(first['status'], 'ok')
            cancelled = app.session.store.read_json(app.state['turns'][0]['tools'][1]['result'])
            self.assertFalse(cancelled['data']['executed'])
            self.assertEqual(ToolApplication.feedback_status(app.session.store.root,
                submission_id='during-tool')['status'], 'applied')

    def test_unreconciled_model_keeps_received_feedback_and_unknown_reservation(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            app.ledger.reserve('unknown-call', {'llm_requests': 1, 'total_tokens': 40})
            app.ledger.mark_unknown('unknown-call', reason='Retained unknown provider usage')
            app.state['pending'] = {'kind': 'model', 'attempt': 'unknown-call'}
            app._save()
            ledger = app.ledger.to_dict()
            ToolApplication.submit_feedback(app.session.store.root,
                submission_id='after-unknown', feedback='Revise after reconciliation')
            with app.session.mutation_scope():
                self.assertFalse(app._consume_feedback())
            app = ToolApplication.load(app.session.store.root)
            receipt = ToolApplication.feedback_status(app.session.store.root, submission_id='after-unknown')
            self.assertEqual(receipt['status'], 'pending')
            self.assertIn('Unreconciled model operation', receipt['reason'])
            self.assertEqual(app.state['requests'], ['Original caller request'])
            self.assertEqual(app.ledger.to_dict(), ledger)

    def test_request_and_adoption_recover_together_after_manifest_save_failure(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            root = app.session.store.root
            ToolApplication.submit_feedback(root, submission_id='crash', feedback='Retain this exact input')
            with app.session.mutation_scope(), patch.object(app.session, 'save', side_effect=OSError('Interrupted manifest write')):
                with self.assertRaisesRegex(OSError, 'Interrupted'):
                    app._consume_feedback()
            app = ToolApplication.load(root)
            self.assertEqual(app.state['requests'], ['Original caller request', 'Retain this exact input'])
            self.assertEqual(ToolApplication.feedback_status(root, submission_id='crash')['status'], 'applied')
            app.run(submission_id='crash', progress=lambda _: None)
            self.assertEqual(app.state['requests'].count('Retain this exact input'), 1)

    def test_interrupted_request_write_reloads_pending_input_without_an_extra_revision(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            root = app.session.store.root
            app.session.pause('Wait for caller feedback')
            ToolApplication.submit_feedback(root, submission_id='write-interrupted', feedback='Adopt after reload')
            write = app.session.store.write_json

            def interrupt(path, *args, **kwargs):
                if path == 'actions.json':
                    raise OSError('Interrupted request journal write')
                return write(path, *args, **kwargs)

            with app.session.mutation_scope(), patch.object(app.session.store, 'write_json', side_effect=interrupt):
                with self.assertRaisesRegex(OSError, 'Interrupted request'):
                    app._consume_feedback()
            app = ToolApplication.load(root)
            self.assertEqual(app.state['requests'], ['Original caller request'])
            self.assertEqual(app.session.manifest.revision, 1)
            self.assertEqual(ToolApplication.feedback_status(root, submission_id='write-interrupted')['status'], 'pending')
            app.run(submission_id='write-interrupted', should_stop=lambda: True, progress=lambda _: None)
            self.assertEqual(app.state['requests'].count('Adopt after reload'), 1)
            self.assertEqual(app.session.manifest.revision, 1)

    def test_stable_feedback_run_rejects_combined_authority_and_unknown_ids(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            for options in ({'allow_network': True}, {'resource_allowances': {'llm_requests': 1}},
                            {'retry_model': True}, {'approved_execution': 'old-token'}, {'actions': []}):
                with self.subTest(options=options), self.assertRaisesRegex(ValueError, 'separately'):
                    app.run(submission_id='not-received', feedback='Text only', **options)
            self.assertFalse(app.session.store.exists('tools/submissions/feedback.json'))
            with self.assertRaisesRegex(ValueError, 'Unknown feedback'):
                app.run(submission_id='not-received')

    def test_budget_exhaustion_retains_pending_input_and_legacy_feedback_still_works(self):
        from simple_ar.app.tool_application import ToolApplication
        with tempfile.TemporaryDirectory() as directory:
            app = self.create(directory)
            app.run(feedback='Legacy string feedback', should_stop=lambda: True, progress=lambda _: None)
            self.assertEqual(app.state['requests'], ['Original caller request', 'Legacy string feedback'])
            self.assertFalse(app.session.store.exists('tools/submissions/feedback.json'))
            app.session.manifest.budget.attempts = app.session.manifest.budget.max_attempts
            app.session.save()
            ToolApplication.submit_feedback(app.session.store.root, submission_id='no-capacity', feedback='Do not replenish resources')
            with app.session.mutation_scope():
                self.assertFalse(app._consume_feedback())
            receipt = ToolApplication.feedback_status(app.session.store.root, submission_id='no-capacity')
            self.assertEqual(receipt['status'], 'pending')
            self.assertIn('exhausted', receipt['reason'])
            self.assertEqual(len(app.state['requests']), 2)


if __name__ == "__main__":
    unittest.main()
