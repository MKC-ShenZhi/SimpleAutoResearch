"""Unit tests for v2.12 Milestone 1: budget-aware self-pacing and throttled process live tail."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from simple_ar.core.budget import BudgetLedger, budget_pacing_notice
from simple_ar.core.process import ProcessSpec, ThrottledProcessProgress, run_process


class M1ErgonomicsTests(unittest.TestCase):
    def test_budget_pacing_notice_silent_when_healthy_or_unlimited(self) -> None:
        unlimited = BudgetLedger({"llm_requests": None, "total_tokens": None})
        self.assertIsNone(budget_pacing_notice(unlimited))

        healthy = BudgetLedger({"llm_requests": 20, "total_tokens": 100_000})
        healthy.reserve("r1", {"llm_requests": 1, "total_tokens": 10_000})
        healthy.settle("r1", {"llm_requests": 1, "total_tokens": 10_000})
        self.assertIsNone(budget_pacing_notice(healthy))

    def test_budget_pacing_notice_triggers_on_low_requests_or_tokens(self) -> None:
        low_req = BudgetLedger({"llm_requests": 20, "total_tokens": 100_000})
        low_req.reserve("r1", {"llm_requests": 16, "total_tokens": 10_000})
        low_req.settle("r1", {"llm_requests": 16, "total_tokens": 10_000})
        notice = budget_pacing_notice(low_req)
        self.assertIsNotNone(notice)
        assert notice is not None
        self.assertIn("llm_requests 4/20 remaining (20%)", notice)
        self.assertNotIn("total_tokens", notice)
        self.assertIn("tool contracts and permissions are unchanged", notice)

        low_tok = BudgetLedger({"llm_requests": 50, "total_tokens": 100_000})
        low_tok.reserve("r1", {"llm_requests": 5, "total_tokens": 86_000})
        low_tok.settle("r1", {"llm_requests": 5, "total_tokens": 86_000})
        notice_tok = budget_pacing_notice(low_tok)
        self.assertIsNotNone(notice_tok)
        assert notice_tok is not None
        self.assertIn("total_tokens 14000/100000 remaining (14%)", notice_tok)
        self.assertNotIn("llm_requests", notice_tok)

    def test_throttled_process_progress_throttles_and_handles_cr_and_errors(self) -> None:
        now = [100.0]
        emitted: list[tuple[float, str, str]] = []
        tracker = ThrottledProcessProgress(
            lambda elapsed, stream, line: emitted.append((elapsed, stream, line)),
            started=100.0,
            interval_sec=5.0,
            clock=lambda: now[0],
        )
        tracker.feed("stdout", "epoch 1 loss=0.9\n")
        self.assertEqual(emitted, [(0.0, "stdout", "epoch 1 loss=0.9")])

        now[0] = 102.0
        tracker.feed("stdout", "bar 10%\rbar 20%\rbar 30%\n")
        self.assertEqual(len(emitted), 1)  # Throttled inside 5s window.

        now[0] = 105.0
        tracker.tick()
        self.assertEqual(emitted[-1], (5.0, "stdout", "bar 30%"))

        def broken(_elapsed: float, _stream: str, _line: str) -> None:
            raise BrokenPipeError("display disconnected")

        safe = ThrottledProcessProgress(broken, started=0.0, interval_sec=1.0, clock=lambda: 1.0)
        safe.feed("stderr", "warning line\n")
        safe.feed("stderr", "another line\n")

    def test_run_process_with_progress_callback_does_not_block_or_alter_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seen: list[tuple[float, str, str]] = []
            code = "for i in range(200):\n    print(f'step {i} ok', flush=True)\n"
            spec = ProcessSpec(
                argv=[sys.executable, "-c", code],
                cwd=tmp_path,
                timeout_sec=15.0,
                output_dir=tmp_path / "proc",
            )
            result = run_process(
                spec,
                progress_callback=lambda elapsed, stream, line: seen.append((elapsed, stream, line)),
                progress_interval_sec=0.05,
            )
            self.assertEqual(result.returncode, 0)
            self.assertIsNone(result.stop_reason)
            self.assertIn("step 199 ok", result.stdout)
            self.assertGreaterEqual(len(seen), 1)
            self.assertTrue(any("step " in line for _, stream, line in seen if stream == "stdout"))


if __name__ == "__main__":
    unittest.main()
