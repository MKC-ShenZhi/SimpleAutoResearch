from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from simple_ar.core.process import ProcessSpec, process_observation, run_process
from simple_ar.core.process import settle_process_record
from simple_ar.core.budget import BudgetLedger, BudgetExceededError, BudgetConflictError
from simple_ar.experiment.execution.backend import LocalExecutionBackend, RunRequest
from simple_ar.code_task import initialize_code_task, execute_code_task
from simple_ar.code_task.execution.runner import _run_command


class ProcessControlTests(unittest.TestCase):
    def test_experiment_and_code_task_share_one_physical_process_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = BudgetLedger({"process_invocations": 2, "process_wall_seconds": 20}, storage_path=root / "ledger.json")
            first = LocalExecutionBackend(budget_ledger=ledger).run(RunRequest(
                [sys.executable, "-c", "print('score: 1')"], root, 5,
                output_dir=root / "experiment", session_id="s", attempt_id="experiment-1",
            ))
            project = root / "project"
            project.mkdir()
            (project / "main.py").write_text("print('score: 2')\n", encoding="utf-8")
            task = root / "task.md"
            task.write_text("Measure the baseline.", encoding="utf-8")
            initialize_code_task(run_dir=root / "code-task", code_root=project,
                                 task_file=task, benchmark_command="python main.py")
            second = execute_code_task(root / "code-task", to_step="baseline", use_llm=False, timeout_sec=5,
                                       budget_ledger=ledger, session_id="s", attempt_id="baseline-2")
            self.assertEqual(second.stop_reason, "stop_point")
            self.assertEqual(ledger.remaining("process_invocations"), 0)
            self.assertEqual({entry.attempt_id for entry in ledger.entries}, {"experiment-1", "baseline-2"})
            self.assertTrue(all(entry.actual_source == "executor_wall_clock" for entry in ledger.entries))
            execute_code_task(root / "code-task", to_step="baseline", use_llm=False, timeout_sec=5,
                              budget_ledger=ledger, session_id="s", attempt_id="baseline-2")
            self.assertEqual(len(ledger.entries), 2)
            # Reconciliation repeats accounting, never the actual process.
            restored = BudgetLedger.load(root / "ledger.json")
            settle_process_record(first.process_record, restored)
            self.assertEqual(restored.remaining("process_invocations"), 0)
            interrupted = BudgetLedger({"process_invocations": 1, "process_wall_seconds": 5})
            interrupted.reserve(f"process-{first.process_record['invocation_id']}",
                                {"process_invocations": 1, "process_wall_seconds": 5})
            settle_process_record(first.process_record, interrupted)
            self.assertEqual(interrupted.entries[0].status, "settled")
            with self.assertRaises(BudgetExceededError):
                LocalExecutionBackend(budget_ledger=restored).run(RunRequest(
                    [sys.executable, "-c", "from pathlib import Path; Path('unexpected').touch()"],
                    root, 5, output_dir=root / "blocked",
                ))
            self.assertFalse((root / "unexpected").exists())

    def test_launch_failure_releases_budget_but_timeout_is_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = BudgetLedger({"process_invocations": 1, "process_wall_seconds": 5})
            with self.assertRaises(OSError):
                run_process(ProcessSpec([str(root / "missing-executable")], root, 1,
                                        output_dir=root / "failed"), budget_ledger=ledger)
            self.assertEqual(ledger.remaining("process_invocations"), 1)
            spec = ProcessSpec([sys.executable, "-c", "import time; time.sleep(3)"], root, 0.5,
                               output_dir=root / "timeout")
            result = run_process(spec, budget_ledger=ledger)
            self.assertEqual(result.stop_reason, "timeout")
            restored = process_observation(result.record, root / "timeout")
            self.assertEqual(restored["stop_reason"], "timeout")
            self.assertEqual(restored["returncode"], result.returncode)
            self.assertEqual(restored["stdout"], result.stdout)
            self.assertEqual(ledger.remaining("process_invocations"), 0)
            with self.assertRaises(BudgetConflictError):
                run_process(spec, budget_ledger=ledger)

    def test_large_output_is_drained_with_bounded_logs_and_metric_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw_streams = {"stdout": ("界" * 100000 + "\r\n准确率: 0.75\r\n").encode(),
                           "stderr": b"y" * 300000 + b"\xffwarning\r\n"}
            code = ("import os; os.write(1, ('界'*100000+'\\r\\n准确率: 0.75\\r\\n').encode()); "
                    "os.write(2, b'y'*300000+b'\\xffwarning\\r\\n'); raise SystemExit(3)")
            for log_bytes in (4096, 0):
                with self.subTest(log_bytes=log_bytes):
                    output = root / str(log_bytes)
                    result = run_process(ProcessSpec([sys.executable, "-u", "-c", code],
                        root, 5, output_dir=output, log_bytes=log_bytes, tail_bytes=128))
                    stored = json.loads((output / "invocation.json").read_text(encoding="utf-8"))
                    recovered = process_observation(stored, output)
                    self.assertEqual(result.returncode, 3)
                    self.assertEqual(recovered["returncode"], 3)
                    self.assertIsNone(recovered["stop_reason"])
                    self.assertIn("准确率: 0.75", recovered["stdout"])
                    self.assertEqual(recovered["streams"], result.record["streams"])
                    for name, raw in raw_streams.items():
                        expected = raw[-128:].decode("utf-8", errors="replace").replace("\r\n", "\n")
                        self.assertEqual(getattr(result, name), expected)
                        self.assertEqual(recovered[name], expected)
                        self.assertEqual(stored["output"][name], expected)
                        self.assertEqual((output / f"{name}.log").read_bytes(), raw[:log_bytes])
                        metadata = recovered["streams"][name]
                        self.assertEqual(metadata["bytes_seen"], len(raw))
                        self.assertEqual(metadata["tail_bytes_discarded"], len(raw) - 128)
                        self.assertEqual(metadata["observation_bytes_discarded"], len(raw) - 128)
                        self.assertEqual(metadata["log_bytes_discarded"], len(raw) - log_bytes)
                        self.assertTrue(metadata["final_tail_available"])

    def test_timeout_preserves_output_and_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_process(ProcessSpec(
                [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(3)"],
                Path(tmp), 0.5,
            ))
            self.assertEqual(result.stop_reason, "timeout")
            self.assertIn("started", result.stdout)

    def test_callback_cancellation_and_error_end_the_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = ProcessSpec(
                [sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(3)"],
                Path(tmp), 5,
            )
            cancel = threading.Event()
            result = run_process(spec, output_callback=lambda *_: cancel.set(), cancel=cancel)
            self.assertEqual(result.stop_reason, "cancelled")

            def broken_display(*_):
                raise RuntimeError("display failed")

            with self.assertRaisesRegex(RuntimeError, "display failed"):
                run_process(spec, output_callback=broken_display)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object integration")
    def test_timeout_terminates_a_real_child_process(self):
        # Retain a real process handle before termination to avoid PID reuse and
        # verify OS-observed exit, rather than merely checking a mocked kill call.
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = []

        def observe(name, chunk):
            if name == "stdout" and chunk.strip():
                handles.append(kernel.OpenProcess(0x100000, False, int(chunk.strip())))

        with tempfile.TemporaryDirectory() as tmp:
            code = "import subprocess, sys, time; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)']); print(p.pid, flush=True); time.sleep(3)"
            try:
                result = run_process(ProcessSpec([sys.executable, "-u", "-c", code], Path(tmp), 0.75), output_callback=observe)
                self.assertEqual(result.stop_reason, "timeout")
                self.assertTrue(handles and handles[0])
                self.assertEqual(kernel.WaitForSingleObject(handles[0], 2000), 0)
            finally:
                for handle in handles:
                    kernel.CloseHandle(handle)

    def test_code_task_progress_adapter_retains_final_progress_and_metrics(self):
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            code = "import os; os.write(1, b'progress 1/2\\rprogress 2/2\\r\\naccuracy: 0.75\\n')"
            result = _run_command(
                [sys.executable, "-u", "-c", code], cwd=Path(tmp), timeout_sec=5,
                stream_output="auto", output_callback=lambda name, text: messages.append(text),
            )
            self.assertEqual(result.returncode, 0)
            self.assertIn("progress 2/2", messages)
            self.assertIn("accuracy: 0.75", result.stdout)


if __name__ == "__main__":
    unittest.main()
