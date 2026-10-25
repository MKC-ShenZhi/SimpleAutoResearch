from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from simple_ar.core.budget import (
    BudgetConflictError,
    BudgetError,
    BudgetExceededError,
    BudgetLedger,
    BudgetUnknownError,
)


class BudgetLedgerTests(unittest.TestCase):
    def test_response_accounting_boundary_requires_unchanged_complete_unknown_records(self) -> None:
        from types import SimpleNamespace
        from simple_ar.code_task.generation.agent_step import (
            response_accounting_known, response_accounting_snapshot,
        )

        ledger = BudgetLedger({'tokens': 100})
        ledger.reserve('old', {'tokens': 10}, logical_call_id='original-call')
        entry = ledger.mark_unknown('old', reason='lost response', retain_reservation=True)
        client = SimpleNamespace(_budget_ledger=ledger)
        before = response_accounting_snapshot(client)
        original = entry.to_dict()
        self.assertFalse(response_accounting_known(client))
        self.assertTrue(response_accounting_known(client, before))
        self.assertEqual(ledger.remaining('tokens'), 90)
        # All fields matter, even when usage dimensions are fully accounted.
        for field, value in original.items():
            with self.subTest(field=field):
                replacement = ({'tokens': 1} if isinstance(value, dict) else
                               'settled' if field == 'status' else 'changed')
                setattr(entry, field, replacement)
                self.assertFalse(response_accounting_known(client, before))
                setattr(entry, field, value)
        self.assertTrue(response_accounting_known(client, before))
        ledger.reserve('current', {'tokens': 10})
        self.assertFalse(response_accounting_known(client, before))
        pending_before = response_accounting_snapshot(client)
        self.assertFalse(response_accounting_known(client, pending_before))
        ledger.settle('current', {'tokens': 2})
        self.assertTrue(response_accounting_known(client, before))
        # A formerly settled entry must not become an ignorable unknown.
        settled_before = response_accounting_snapshot(client)
        ledger.entries[-1].status = 'unknown'
        self.assertFalse(response_accounting_known(client, settled_before))
        ledger.entries[-1].status = 'settled'
        ledger.reserve('new', {'tokens': 1})
        ledger.mark_unknown('new', reason='current uncertainty', known_actual={'tokens': 1})
        self.assertFalse(response_accounting_known(client, before))
        self.assertEqual(entry.to_dict(), original)
        client._budget_ledger = BudgetLedger()
        self.assertFalse(response_accounting_known(client, before))
        for fake in (SimpleNamespace(), SimpleNamespace(_budget_ledger=object())):
            self.assertIsNone(response_accounting_snapshot(fake))
            self.assertTrue(response_accounting_known(fake))
        client._budget_ledger = None
        self.assertFalse(response_accounting_known(client, before))

    def test_temporary_floors_preserve_accounting_and_guard_each_reservation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger(
                {"tokens": 30, "requests": 4, "uncertain": 10,
                 "bounded": 10, "unlimited": None},
                storage_path=path,
            )
            seed = ledger.reserve("seed", {"tokens": 4, "requests": 1})
            ledger.settle("seed", {"tokens": 4, "requests": 1})
            ledger.reserve("lost", {"uncertain": 2, "unlimited": 1})
            ledger.mark_unknown("lost", reason="response lost")
            ledger.reserve("bounded-lost", {"bounded": 4})
            ledger.mark_unknown("bounded-lost", reason="timeout", retain_reservation=True)
            baseline = ledger.to_dict()

            with ledger.protect_remaining(
                {"tokens": 10, "requests": 1, "uncertain": 2,
                 "bounded": 3, "unlimited": 100, "unconfigured": 100},
            ):
                self.assertEqual(ledger.to_dict(), baseline)
                self.assertEqual(ledger.remaining("tokens"), 16)
                self.assertEqual(ledger.remaining("requests"), 2)
                self.assertEqual(ledger.remaining("bounded"), 3)
                self.assertIsNone(ledger.remaining("uncertain"))
                self.assertIsNone(ledger.remaining("unlimited"))
                self.assertIsNone(ledger.remaining("unconfigured"))
                self.assertIn('bounded', ledger.unknown_dimensions())
                self.assertNotIn('bounded', ledger.unknown_dimensions(unbounded_only=True))
                self.assertIn('uncertain', ledger.unknown_dimensions(unbounded_only=True))
                # Nested work leaves its own result capacity in addition to
                # the caller's floor; ordinary scopes keep max-floor semantics.
                with ledger.protect_remaining({"tokens": 3, "requests": 1}, additional=True):
                    self.assertEqual(ledger.remaining("tokens"), 13)
                    self.assertEqual(ledger.remaining("requests"), 1)
                    self.assertEqual(ledger.to_dict(), baseline)
                self.assertEqual(ledger.remaining("tokens"), 16)
                self.assertEqual(ledger.remaining("requests"), 2)
                self.assertIs(ledger.reserve("seed", {"tokens": 4, "requests": 1}), seed)
                with self.assertRaises(BudgetUnknownError):
                    ledger.reserve("unknown-retry", {"uncertain": 1})
                with self.assertRaises(BudgetExceededError):
                    ledger.reserve("bounded-retry", {"bounded": 4})

                with self.assertRaisesRegex(RuntimeError, "scope failure"):
                    with ledger.protect_remaining({"tokens": 6, "requests": 2}):
                        self.assertEqual(ledger.remaining("tokens"), 16)
                        self.assertEqual(ledger.remaining("requests"), 1)
                        work = ledger.reserve("work", {"tokens": 16, "requests": 1})
                        self.assertEqual(ledger.remaining("tokens"), 0)
                        self.assertEqual(ledger.remaining("requests"), 0)
                        for retry in ("retry-1", "retry-2"):
                            with self.assertRaises(BudgetExceededError):
                                ledger.reserve(retry, {"tokens": 1, "requests": 1})
                        self.assertIs(
                            ledger.reserve("work", {"tokens": 16, "requests": 1}), work,
                        )
                        ledger.settle("work", {"tokens": 14, "requests": 1})
                        raise RuntimeError("scope failure")

                self.assertEqual(ledger.remaining("tokens"), 2)
                self.assertEqual(ledger.remaining("requests"), 1)
                with ledger.protect_remaining({"tokens": 12}):
                    self.assertEqual(ledger.remaining("tokens"), 0)
                self.assertEqual(ledger.remaining("tokens"), 2)
                loaded = BudgetLedger.load(path)
                self.assertEqual(loaded.to_dict(), ledger.to_dict())
                self.assertEqual(loaded.remaining("tokens"), 12)
                self.assertEqual(loaded.remaining("bounded"), 6)
                self.assertEqual(loaded.unknown_dimensions(), ledger.unknown_dimensions())
                self.assertEqual(ledger.entries[0].to_dict(), baseline["entries"][0])
                ledger.reserve("unlimited-use", {"unlimited": 1000, "unconfigured": 1000})
                ledger.authorize_remaining(
                    "tokens", 8, authorization_id="allowance", reason="Explicit allowance",
                )
                self.assertEqual(ledger.limits["tokens"], 8)
                self.assertEqual(ledger.remaining("tokens"), 0)
                with self.assertRaises(BudgetExceededError):
                    ledger.reserve("after-allowance", {"tokens": 1})

            self.assertEqual(ledger.remaining("tokens"), 8)
            self.assertEqual(ledger.remaining("requests"), 2)
            self.assertEqual(ledger.remaining("bounded"), 6)
            with self.assertRaisesRegex(RuntimeError, "outer failure"):
                with ledger.protect_remaining({"tokens": 20}):
                    self.assertEqual(ledger.remaining("tokens"), 0)
                    raise RuntimeError("outer failure")
            self.assertEqual(ledger.remaining("tokens"), 8)

    def test_unlimited_api_usage_survives_reload_with_unknown_calls(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger(
                {"total_tokens": None, "llm_requests": None, "process_invocations": 0},
                storage_path=path,
            )
            ledger.reserve("completed", {"total_tokens": 3000000, "llm_requests": 100})
            ledger.settle("completed", {"total_tokens": 2000000, "llm_requests": 100})
            ledger.reserve("interrupted", {"total_tokens": 1000, "llm_requests": 1})
            ledger.mark_unknown("interrupted", reason="connection lost")
            loaded = BudgetLedger.load(path)
            self.assertEqual(loaded.entries[0].actual["total_tokens"], 2000000)
            self.assertEqual(loaded.entries[1].status, "unknown")
            self.assertIsNone(loaded.remaining("total_tokens"))
            loaded.reserve("continued", {"total_tokens": 3000000, "llm_requests": 1})
            with self.assertRaises(BudgetExceededError):
                loaded.reserve("process", {"process_invocations": 1})

    def test_one_explicit_authorization_extends_multiple_dimensions_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger({"total_tokens": 100, "llm_requests": 3}, storage_path=path)
            with self.assertRaisesRegex(BudgetError, "positive"):
                ledger.validate_remaining(
                    {"total_tokens": 0}, authorization_id="invalid", reason="Reject zero allowance.",
                )
            ledger.reserve("uncertain", {"total_tokens": 35, "llm_requests": 1})
            ledger.mark_unknown("uncertain", reason="provider response lost")
            ledger.authorize_remaining(
                {"total_tokens": 50, "llm_requests": 2},
                authorization_id="continuation-1", reason="Permit one bounded continuation.",
            )
            ledger.reserve("after-auth", {"total_tokens": 20, "llm_requests": 1})
            ledger.settle("after-auth", {"total_tokens": 18, "llm_requests": 1})

            loaded = BudgetLedger.load(path)
            loaded.authorize_remaining(
                {"total_tokens": 50, "llm_requests": 2},
                authorization_id="continuation-1", reason="Replay the same authorization.",
            )
            self.assertEqual(loaded.limits["total_tokens"], 50)
            self.assertEqual(loaded.remaining("total_tokens"), 32)
            self.assertEqual(loaded.remaining("llm_requests"), 1)
            self.assertEqual(loaded.unknown_dimensions(), ("llm_requests", "total_tokens"))
            with self.assertRaises(BudgetConflictError):
                loaded.authorize_remaining(
                    {"total_tokens": 60, "llm_requests": 2},
                    authorization_id="continuation-1", reason="Changed token allowance.",
                )

    def test_explicit_allowance_preserves_unknown_history_and_does_not_reset_on_reload(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger({"total_tokens": 100, "llm_requests": 3}, storage_path=path)
            ledger.reserve("timeout", {"total_tokens": 40, "llm_requests": 1})
            ledger.mark_unknown("timeout", reason="provider timeout", known_actual={"llm_requests": 1})
            historical = ledger.entries[0].to_dict()
            ledger.authorize_remaining("total_tokens", 60, authorization_id="user-1", reason="User permits 60 additional tokens")
            ledger.reserve("next", {"total_tokens": 20, "llm_requests": 1})
            ledger.settle("next", {"total_tokens": 15, "llm_requests": 1})
            loaded = BudgetLedger.load(path)
            loaded.authorize_remaining("total_tokens", 60, authorization_id="user-1", reason="Replay same authorization")
            self.assertEqual(loaded.entries[0].to_dict(), historical)
            self.assertEqual(loaded.unknown_dimensions(), ("total_tokens",))
            self.assertEqual(loaded.remaining("total_tokens"), 45)
            self.assertEqual(loaded.remaining("llm_requests"), 1)
            with self.assertRaises(BudgetExceededError):
                loaded.reserve("too-much", {"total_tokens": 46})
            with self.assertRaises(BudgetConflictError):
                loaded.authorize_remaining("total_tokens", 100, authorization_id="user-1", reason="Changed terms")

    def test_bounded_unknown_consumption_retains_capacity_after_reload(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger({"llm_requests": 3, "total_tokens": 30}, storage_path=path)
            ledger.reserve("first", {"llm_requests": 1, "total_tokens": 20})
            ledger.mark_unknown("first", reason="timeout", known_actual={"llm_requests": 1},
                                retain_reservation=True)
            loaded = BudgetLedger.load(path)
            self.assertEqual(loaded.remaining("llm_requests"), 2)
            self.assertEqual(loaded.remaining("total_tokens"), 10)
            with self.assertRaises(BudgetExceededError):
                loaded.reserve("too-large", {"llm_requests": 1, "total_tokens": 11})
            loaded.reserve("fits", {"llm_requests": 1, "total_tokens": 10})
            self.assertEqual(loaded.unknown_dimensions(), ("total_tokens",))

    def test_reservation_counts_until_settlement_and_settlement_is_idempotent(self) -> None:
        ledger = BudgetLedger({"llm_requests": 2, "total_tokens": 10})

        entry = ledger.reserve(
            "request-1",
            {"llm_requests": 1, "total_tokens": 6},
            session_id="session-1",
            attempt_id="attempt-1",
            logical_call_id="plan",
            purpose="research planning",
        )

        self.assertEqual(entry.status, "reserved")
        self.assertEqual(ledger.remaining("llm_requests"), 1)
        self.assertEqual(ledger.remaining("total_tokens"), 4)
        with self.assertRaises(BudgetExceededError):
            ledger.reserve("request-2", {"llm_requests": 1, "total_tokens": 5})

        settled = ledger.settle(
            "request-1",
            {"llm_requests": 1, "total_tokens": 4},
            actual_source="provider",
        )
        self.assertIs(settled, entry)
        self.assertEqual(ledger.remaining("llm_requests"), 1)
        self.assertEqual(ledger.remaining("total_tokens"), 6)
        self.assertIs(
            ledger.settle(
                "request-1",
                {"llm_requests": 1, "total_tokens": 4},
                actual_source="provider",
            ),
            entry,
        )
        with self.assertRaises(BudgetConflictError):
            ledger.settle(
                "request-1",
                {"llm_requests": 1, "total_tokens": 3},
                actual_source="provider",
            )

    def test_release_returns_preflight_capacity(self) -> None:
        ledger = BudgetLedger({"process_seconds": 10})
        ledger.reserve("preflight", {"process_seconds": 8})

        self.assertEqual(ledger.remaining("process_seconds"), 2)
        ledger.release("preflight", reason="local validation failed")
        self.assertEqual(ledger.remaining("process_seconds"), 10)
        self.assertEqual(ledger.entries[0].status, "released")
        ledger.release("preflight")

    def test_unknown_consumption_blocks_finite_dimension_and_survives_reload(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "budget.json"
            ledger = BudgetLedger(
                {"llm_requests": 3, "total_tokens": 100},
                storage_path=path,
            )
            ledger.reserve("request-1", {"llm_requests": 1, "total_tokens": 20})
            ledger.mark_unknown("request-1", reason="provider disconnected after acceptance")

            self.assertIsNone(ledger.remaining("llm_requests"))
            self.assertIsNone(ledger.remaining("total_tokens"))
            self.assertEqual(ledger.unknown_dimensions(), ("llm_requests", "total_tokens"))
            with self.assertRaises(BudgetUnknownError):
                ledger.reserve("request-2", {"llm_requests": 1})

            loaded = BudgetLedger.load(path)
            self.assertEqual(loaded.unknown_dimensions(), ("llm_requests", "total_tokens"))
            self.assertEqual(loaded.entries[0].status, "unknown")
            self.assertEqual(loaded.entries[0].reason, "provider disconnected after acceptance")

    def test_replayed_reservation_cannot_change_amounts(self) -> None:
        ledger = BudgetLedger({"requests": 5})
        first = ledger.reserve("same-id", {"requests": 1})

        self.assertIs(ledger.reserve("same-id", {"requests": 1}), first)
        with self.assertRaises(BudgetConflictError):
            ledger.reserve("same-id", {"requests": 2})

    def test_settlement_requires_every_reserved_dimension(self) -> None:
        ledger = BudgetLedger({"requests": 5, "tokens": 50})
        ledger.reserve("request-1", {"requests": 1, "tokens": 10})

        with self.assertRaises(BudgetError):
            ledger.settle("request-1", {"requests": 1})
        self.assertEqual(ledger.entries[0].status, "reserved")

    def test_measured_over_limit_is_recorded_instead_of_hidden(self) -> None:
        ledger = BudgetLedger({"tokens": 10})
        ledger.reserve("request-1", {"tokens": 4})
        ledger.settle("request-1", {"tokens": 14}, actual_source="provider")

        self.assertEqual(ledger.remaining("tokens"), 0)
        self.assertEqual(ledger.over_limit(), {"tokens": 4})
        self.assertEqual(ledger.snapshot()["over_limit"], {"tokens": 4})


if __name__ == "__main__":
    unittest.main()
