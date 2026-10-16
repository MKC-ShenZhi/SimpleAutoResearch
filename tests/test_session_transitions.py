from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from simple_ar.core import (
    AttemptManifest,
    ArtifactStore,
    BudgetState,
    CapabilityContext,
    CapabilityRegistry,
    CapabilityResult,
    DecisionRecord,
    SessionController,
    SessionManifest,
    lifecycle_profile_names,
    resolve_lifecycle_profile,
)


class SessionTransitionTests(unittest.TestCase):
    def test_legacy_missing_timestamps_remain_unknown_across_loads(self) -> None:
        decision = {"capability": "analysis", "attempt_id": "analysis-001", "action": "accept"}
        manifest = {"session_id": "legacy", "topic": "legacy", "decisions": [decision]}
        attempt = {"attempt_id": "analysis-001"}
        self.assertEqual(DecisionRecord.from_dict(decision).created_at, "")
        self.assertEqual(AttemptManifest.from_dict(attempt).created_at, "")
        self.assertEqual(AttemptManifest.from_dict(attempt).updated_at, "")
        first = SessionManifest.from_dict(manifest)
        second = SessionManifest.from_dict(manifest)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.created_at, "")
        self.assertEqual(first.updated_at, "")
        self.assertEqual(first.decisions[0].created_at, "")

    def test_budget_attempt_record_is_idempotent(self) -> None:
        budget = BudgetState()

        self.assertTrue(budget.record(True, attempt_id="attempt-001"))
        self.assertFalse(budget.record(False, attempt_id="attempt-001"))
        self.assertEqual(budget.attempts, 1)
        self.assertEqual(budget.no_progress, 0)
        self.assertEqual(budget.recorded_attempts, ["attempt-001"])

    def test_continuation_authorization_increases_caps_without_resetting_use(self) -> None:
        budget = BudgetState(max_attempts=2, max_no_progress=1)
        budget.record(False, attempt_id="attempt-001")

        self.assertTrue(budget.authorize_additional(
            "review-1", attempts=2, no_progress=1,
            resource_allowances={"total_tokens": 50}, reason="One bounded recovery.",
        ))
        self.assertFalse(budget.authorize_additional(
            "review-1", attempts=2, no_progress=1,
            resource_allowances={"total_tokens": 50}, reason="One bounded recovery.",
        ))
        restored = BudgetState.from_dict(budget.to_dict())
        self.assertEqual((restored.max_attempts, restored.max_no_progress), (4, 2))
        self.assertEqual((restored.attempts, restored.no_progress), (1, 1))
        with self.assertRaisesRegex(ValueError, "different terms"):
            restored.authorize_additional(
                "review-1", attempts=3, no_progress=1,
                resource_allowances={"total_tokens": 50}, reason="Changed allowance.",
            )
        with self.assertRaisesRegex(ValueError, "different terms"):
            restored.authorize_additional(
                "review-1", attempts=2, no_progress=1,
                resource_allowances={"total_tokens": 60}, reason="One bounded recovery.",
            )




    def test_builtin_lifecycle_profile_definitions_are_stable(self) -> None:
        survey = resolve_lifecycle_profile("survey")
        self.assertIsNotNone(survey)
        self.assertEqual(
            lifecycle_profile_names(),
            ("research_brief", "survey", "experiment", "paper_audit", "full_research"),
        )
        assert survey is not None
        self.assertEqual(
            survey.capabilities,
            ("plan", "search", "document_ingest", "read", "synthesize", "report"),
        )
        self.assertFalse(survey.allows("code"))


    def test_missing_available_output_is_visible_at_session_boundary(self) -> None:
        for initially_missing in (False, True):
            with self.subTest(initially_missing=initially_missing), tempfile.TemporaryDirectory() as tmp:
                registry = CapabilityRegistry()

                def handler(*, context: CapabilityContext) -> CapabilityResult:
                    if not initially_missing:
                        context.store.write_text("output.txt", "saved")
                    context.store.write_text("undeclared.txt", "not a handoff")
                    return CapabilityResult(
                        status="completed",
                        artifacts=(context.store.ref("output.txt", kind="result"),),
                    )

                registry.register("probe", handler)
                controller = SessionController.create(
                    tmp, session_id="missing-output", topic="declared outputs", registry=registry,
                )
                result = controller.execute_attempt("probe", attempt_id="attempt-001")
                self.assertEqual(result.status, "partial" if initially_missing else "completed")
                outcome_path = Path(tmp) / "attempts/attempt-001/capability_result.json"
                saved_outcome = outcome_path.read_bytes()
                output_path = outcome_path.parent / "output.txt"
                if not initially_missing:
                    output_path.unlink()

                loaded = SessionController.load(tmp, registry=registry)
                invalidated = loaded.reconcile_attempt()
                self.assertEqual(invalidated.status, "partial")
                self.assertEqual(invalidated.artifacts[0].status, "missing")
                self.assertEqual(len(invalidated.diagnostics), 1)
                self.assertIn("output.txt", invalidated.diagnostics[0])
                self.assertEqual(loaded.list_attempts()[0].status, "completed")
                self.assertEqual(loaded.list_attempts()[0].outputs, invalidated.artifacts)
                self.assertEqual(loaded.attempt_output_refs()[0].status, "missing")
                with self.assertRaisesRegex(ValueError, "marked missing"):
                    loaded.execute_attempt("probe", attempt_id="attempt-002", inputs=loaded.attempt_output_refs())
                self.assertEqual(loaded.manifest.budget.attempts, 1)
                self.assertEqual(loaded.reconcile_attempt(), invalidated)
                self.assertEqual(outcome_path.read_bytes(), saved_outcome)

                # Initially missing outputs remain invalid even if bytes appear;
                # later disappearance is a view over the saved available output.
                output_path.write_text("restored", encoding="utf-8")
                restored = loaded.reconcile_attempt()
                self.assertEqual(restored.status, "partial" if initially_missing else "completed")
                self.assertNotIn("undeclared.txt", [ref.path for ref in restored.artifacts])

    def test_saved_attempt_commit_recovers_each_manifest_gap_without_reexecution(self) -> None:
        cases = (
            ("completed", False, False, 1),
            ("partial", None, False, 1),
            ("failed", True, False, 1),
            ("blocked", None, False, 1),
            ("failed", None, True, 1),
            ("failed", None, True, 3),
        )
        for status, progressed, interrupted, limit in cases:
            # Confirmed interruption also owns its ordinary (no save fault)
            # closure here, alongside every interrupted commit boundary.
            for gap in (("none", "result", "attempt", "session") if interrupted else ("result", "attempt", "session")):
                with self.subTest(status=status, progressed=progressed, interrupted=interrupted, limit=limit, gap=gap), tempfile.TemporaryDirectory() as tmp:
                    registry = CapabilityRegistry()
                    calls = []

                    def handler(**_: object) -> CapabilityResult:
                        calls.append("called")
                        if interrupted:
                            raise KeyboardInterrupt()
                        return CapabilityResult(
                            status=status,  # type: ignore[arg-type]
                            provenance={"recovery": "explicit_interruption"} if status == "failed" else {},
                        )

                    registry.register("probe", handler)
                    controller = SessionController.create(
                        tmp, session_id="commit-gap", topic="saved result commit", registry=registry,
                        budget=BudgetState(max_attempts=limit),
                    )
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt):
                            controller.execute_attempt("probe", attempt_id="attempt-001")

                    write_json = ArtifactStore.write_json
                    injected = []

                    def interrupt_save(store, path, data, **kwargs):
                        if gap == "result" and path == "capability_result.json":
                            if interrupted:
                                # Historical manual recovery envelopes have only
                                # the provenance marker, with no core metadata.
                                data = dict(data)
                                data.pop("session_interruption_reason")
                            write_json(store, path, data, **kwargs)
                            injected.append(gap)
                            raise KeyboardInterrupt()
                        if gap == "attempt" and path == "attempt_manifest.json" and data["status"] in {"completed", "failed", "blocked"}:
                            self.assertTrue(store.exists("capability_result.json"))
                            injected.append(gap)
                            raise KeyboardInterrupt()
                        if gap == "session" and path == "session_manifest.json" and data["budget"]["attempts"]:
                            injected.append(gap)
                            raise KeyboardInterrupt()
                        return write_json(store, path, data, **kwargs)

                    if gap == "none":
                        closed = SessionController.load(tmp, registry=CapabilityRegistry())
                        result = closed.recover_interrupted(reason="Worker stopped.")
                        self.assertEqual(result.provenance["recovery"], "explicit_interruption")
                        self.assertEqual(result.diagnostics, ("Worker stopped.",))
                    else:
                        with patch.object(ArtifactStore, "write_json", new=interrupt_save):
                            with self.assertRaises(KeyboardInterrupt):
                                if interrupted:
                                    controller.recover_interrupted(reason="Worker stopped.")
                                else:
                                    controller.execute_attempt("probe", attempt_id="attempt-001", progressed=progressed)

                    self.assertEqual(injected, [] if gap == "none" else [gap])
                    # A result cannot be replaced by manual interruption recovery.
                    loaded = SessionController.load(tmp, registry=CapabilityRegistry())
                    outcome_path = Path(tmp) / "attempts/attempt-001/capability_result.json"
                    saved_outcome = outcome_path.read_bytes()
                    with self.assertRaises((RuntimeError, ValueError)):
                        loaded.recover_interrupted(reason="Must not replace saved facts.")
                    self.assertEqual(outcome_path.read_bytes(), saved_outcome)
                    result = loaded.reconcile_attempt()
                    self.assertEqual(result.status, status)
                    self.assertEqual(loaded.list_attempts()[0].status, status if status in {"failed", "blocked"} else "completed")
                    self.assertEqual(loaded.list_attempts()[0].outputs, result.artifacts)
                    self.assertEqual([ref.path for ref in result.artifacts], ["capability_result.json"])
                    expected_progress = progressed if progressed is not None else status in {"completed", "partial"}
                    self.assertEqual(loaded.manifest.budget.no_progress, 0 if expected_progress else 1)
                    self.assertEqual(loaded.manifest.budget.recorded_attempts, ["attempt-001"])
                    self.assertEqual(loaded.manifest.budget.attempts, 1)
                    self.assertEqual(len(loaded.list_attempts()), 1)
                    self.assertEqual(loaded.manifest.status, "blocked" if interrupted and limit == 1 else "running")
                    if interrupted:
                        self.assertEqual(loaded.manifest.status_reason, "Session budget exhausted." if limit == 1 else "Worker stopped.")
                        self.assertEqual(result.diagnostics, ("Worker stopped.",))
                        self.assertEqual(loaded.manifest.decisions, [])
                        self.assertEqual(loaded.status_snapshot()["running_attempts"], 0)
                        self.assertEqual(loaded.status_snapshot()["failed_attempts"], 1)
                    self.assertEqual(calls, ["called"])
                    before = loaded.manifest.to_dict()
                    reloaded = SessionController.load(tmp, registry=CapabilityRegistry())
                    self.assertEqual(reloaded.reconcile_attempt(), result)
                    self.assertEqual(reloaded.manifest.budget.to_dict(), before["budget"])
                    self.assertEqual(reloaded.manifest.status, before["status"])
                    self.assertEqual(reloaded.manifest.status_reason, before["status_reason"])
                    self.assertEqual(outcome_path.read_bytes(), saved_outcome)

    def test_full_research_allows_synthesis_to_experiment_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            calls: list[str] = []

            def handler(*, context: CapabilityContext) -> CapabilityResult:
                calls.append(context.attempt.capability or "")
                return CapabilityResult(status="completed")

            registry.register("synthesize", handler)
            registry.register("experiment", handler)
            controller = SessionController.create(
                tmp,
                session_id="synthesis-experiment-route",
                topic="composable experiment route",
                profile="full_research",
                registry=registry,
            )

            first = controller.execute_attempt(
                "synthesize",
                attempt_id="attempt-001",
            )
            second = controller.execute_attempt(
                "experiment",
                attempt_id="attempt-002",
            )

            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "completed")
            self.assertEqual(calls, ["synthesize", "experiment"])

    def test_canonical_analysis_capability_connects_from_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def handler(**_: object) -> CapabilityResult:
                return CapabilityResult(status="completed")

            registry.register("run", handler)
            registry.register("analysis", handler)
            controller = SessionController.create(
                tmp,
                session_id="canonical-analysis-route",
                topic="canonical analysis capability",
                profile="full_research",
                registry=registry,
            )

            _ = controller.execute_attempt(
                "run",
                attempt_id="attempt-001",
            )
            _ = controller.execute_attempt(
                "analysis",
                attempt_id="attempt-002",
            )

            self.assertEqual(controller.manifest.status, "running")

    def test_legacy_analyze_alias_remains_allowed(self) -> None:
        experiment = resolve_lifecycle_profile("experiment")
        full = resolve_lifecycle_profile("full_research")
        assert experiment is not None
        assert full is not None

        self.assertTrue(experiment.allows("analyze"))
        self.assertTrue(full.allows("analyze"))

    def test_all_builtin_profiles_execute_their_caller_owned_fixture_paths(self) -> None:
        for profile_name in lifecycle_profile_names():
            with self.subTest(profile=profile_name), tempfile.TemporaryDirectory() as tmp:
                registry = CapabilityRegistry()

                def handler(**_: object) -> CapabilityResult:
                    return CapabilityResult(status="completed")

                profile = resolve_lifecycle_profile(profile_name)
                assert profile is not None
                for capability in profile.capabilities:
                    registry.register(capability, handler)

                controller = SessionController.create(
                    tmp,
                    session_id=f"fixture-{profile_name}",
                    topic="profile fixture",
                    registry=registry,
                    profile=profile_name,
                )
                outcomes = []
                for index, capability in enumerate(profile.capabilities, start=1):
                    result = controller.execute_attempt(
                        capability,
                        attempt_id=f"attempt-{index:02d}",

                    )
                    outcomes.append(result)

                self.assertEqual(len(outcomes), len(profile.capabilities))
                self.assertTrue(all(result.status == "completed" for result in outcomes))
                self.assertEqual(controller.manifest.status, "running")
                if profile_name == "full_research":
                    self.assertEqual(controller.manifest.budget.max_attempts, len(profile.capabilities) + 2)

    def test_profiles_allow_only_named_legacy_capability_aliases(self) -> None:
        brief = resolve_lifecycle_profile("research_brief")
        experiment = resolve_lifecycle_profile("experiment")
        full = resolve_lifecycle_profile("full_research")
        assert brief is not None
        assert experiment is not None
        assert full is not None

        self.assertTrue(brief.allows("read"))
        self.assertTrue(brief.allows("synthesize"))
        self.assertFalse(brief.allows("research_brief"))
        self.assertFalse(brief.allows("report_audit"))
        self.assertTrue(experiment.allows("experiment"))
        self.assertTrue(experiment.allows("analysis"))
        self.assertTrue(experiment.allows("report_audit"))
        self.assertTrue(full.allows("report_audit"))


    def test_controller_rejects_out_of_scope_execution_before_running_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("design", lambda **_: CapabilityResult(status="completed"))
            controller = SessionController.create(
                tmp,
                session_id="scoped-execution",
                topic="profile scope",
                registry=registry,
                profile="survey",
            )

            with self.assertRaisesRegex(ValueError, "cannot override session profile"):
                controller.execute_attempt(
                    "design", attempt_id="attempt-001", profile="legacy-override"
                )
            self.assertEqual(controller.list_attempts(), ())

    def test_controller_does_not_allow_attempt_profile_to_escape_session_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []

            def handler(*, context: CapabilityContext) -> CapabilityResult:
                calls.append(context.attempt.attempt_id)
                return CapabilityResult(status="completed")

            registry = CapabilityRegistry()
            registry.register("read", handler)
            controller = SessionController.create(
                tmp,
                session_id="profile-override",
                topic="profile consistency",
                registry=registry,
                profile="survey",
            )

            with self.assertRaisesRegex(ValueError, "cannot override session profile"):
                controller.execute_attempt(
                    "read",
                    attempt_id="attempt-001",
                    profile="experiment",
                )

            self.assertEqual(calls, [])
            self.assertEqual(controller.list_attempts(), ())



    def test_status_snapshot_is_compact_and_domain_neutral(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("plan", lambda **_: CapabilityResult(status="completed"))
            registry.register("search", lambda **_: CapabilityResult(status="completed"))
            controller = SessionController.create(
                tmp,
                session_id="status-snapshot",
                topic="snapshot",
                registry=registry,
            )

            controller.execute_attempt("plan", attempt_id="attempt-001")
            snapshot = controller.status_snapshot()

            self.assertEqual(snapshot["schema_version"], "session_status.v1")
            self.assertEqual(snapshot["attempt_count"], 1)
            self.assertEqual(snapshot["running_attempts"], 0)
            self.assertEqual(snapshot["completed_attempts"], 1)
            self.assertEqual(snapshot["failed_attempts"], 0)
            self.assertNotIn("allowed_targets", snapshot)
            self.assertIsNone(snapshot["last_decision"])
            self.assertNotIn("artifacts", snapshot)

    def test_handler_observes_durable_running_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def inspect_running_state(*, context: CapabilityContext) -> CapabilityResult:
                attempt_store = context.store
                attempt = attempt_store.read_attempt_manifest()
                if context.input_store is None:
                    raise AssertionError("session input store is required")
                session = context.input_store.read_json("session_manifest.json")
                self.assertEqual(context.attempt.status, "running")
                self.assertEqual(attempt.status, "running")
                self.assertEqual(session["status"], "running")
                self.assertEqual(session["current_attempt"], "attempt-001")
                self.assertEqual(attempt.capability, "inspect-running")
                return CapabilityResult(status="completed")

            registry.register("inspect-running", inspect_running_state)
            controller = SessionController.create(
                tmp,
                session_id="durable-running",
                topic="durable state",
                registry=registry,
            )

            result = controller.execute_attempt("inspect-running", attempt_id="attempt-001")

            self.assertEqual(result.status, "completed")
            self.assertEqual(controller.status_snapshot()["running_attempts"], 0)

    def test_process_interrupt_leaves_attempt_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def interrupt(**_: object) -> CapabilityResult:
                raise KeyboardInterrupt()

            registry.register("interrupt", interrupt)
            controller = SessionController.create(
                tmp,
                session_id="process-interrupt",
                topic="interrupt state",
                registry=registry,
            )

            with self.assertRaises(KeyboardInterrupt):
                controller.execute_attempt("interrupt", attempt_id="attempt-001")

            resumed = SessionController.load(tmp, registry=registry)
            snapshot = resumed.status_snapshot()
            self.assertEqual(snapshot["status"], "running")
            self.assertEqual(snapshot["running_attempts"], 1)
            self.assertEqual(snapshot["current_attempt"], "attempt-001")
            with self.assertRaisesRegex(RuntimeError, "recover_interrupted"):
                resumed.execute_attempt("interrupt", attempt_id="attempt-002")
            self.assertFalse(
                (resumed.store.root / "attempts" / "attempt-002").exists()
            )
            self.assertEqual(
                snapshot["active_attempts"],
                [
                    {
                        "attempt_id": "attempt-001",
                        "capability": "interrupt",
                        "updated_at": snapshot["active_attempts"][0]["updated_at"],
                    }
                ],
            )
            self.assertEqual(
                resumed.store.read_attempt_manifest("attempts/attempt-001/attempt_manifest.json").status,
                "running",
            )

    def test_controller_normalizes_capability_and_attempt_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def handler(*, context: CapabilityContext) -> CapabilityResult:
                self.assertEqual(context.attempt.capability, "plan")
                self.assertEqual(context.attempt.attempt_id, "attempt-001")
                return CapabilityResult(status="completed")

            registry.register("plan", handler)
            controller = SessionController.create(
                tmp,
                session_id="normalized-metadata",
                topic="normalized metadata",
                registry=registry,
            )

            result = controller.execute_attempt(
                "  plan  ",
                attempt_id="  attempt-001  ",
            )

            self.assertEqual(result.status, "completed")
            attempt = controller.list_attempts()[0]
            self.assertEqual(attempt.capability, "plan")
            self.assertEqual(attempt.attempt_id, "attempt-001")

    def test_attempt_output_refs_are_explicit_session_root_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def produce(*, context: CapabilityContext) -> CapabilityResult:
                output = context.store.write_text("result.txt", "from-first\n", kind="result")
                return CapabilityResult(status="completed", artifacts=(output,))

            def consume(*, context: CapabilityContext) -> CapabilityResult:
                if len(context.inputs) != 1:
                    return CapabilityResult(status="failed", diagnostics=("missing handoff",))
                value = context.read_input_text(context.inputs[0])
                output = context.store.write_text("consumed.txt", value, kind="result")
                return CapabilityResult(status="completed", artifacts=(output,))

            registry.register("plan", produce)
            registry.register("search", consume)
            controller = SessionController.create(
                tmp,
                session_id="explicit-handoff",
                topic="attempt output handoff",
                registry=registry,
            )

            controller.execute_attempt("plan", attempt_id="attempt-001")
            refs = controller.attempt_output_refs("attempt-001")

            self.assertEqual(refs[0].path, "attempts/attempt-001/result.txt")
            self.assertEqual(controller.store.read_text(refs[0]), "from-first\n")
            result = controller.execute_attempt(
                "search",
                attempt_id="attempt-002",
                inputs=refs,
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(
                controller.store.read_text("attempts/attempt-002/consumed.txt"),
                "from-first\n",
            )

    def test_missing_input_is_rejected_before_attempt_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            calls: list[str] = []

            def handler(**_: object) -> CapabilityResult:
                calls.append("called")
                return CapabilityResult(status="completed")

            registry.register("search", handler)
            controller = SessionController.create(
                tmp,
                session_id="missing-input",
                topic="preflight handoff",
                registry=registry,
            )

            with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
                controller.execute_attempt(
                    "search",
                    attempt_id="attempt-001",
                    inputs=(controller.store.ref("missing.json", kind="input"),),
                )

            self.assertEqual(calls, [])
            self.assertEqual(controller.list_attempts(), ())
            self.assertEqual(controller.manifest.budget.attempts, 0)

    def test_unregistered_capability_is_rejected_before_attempt_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = SessionController.create(
                tmp,
                session_id="missing-capability",
                topic="preflight handler",
                registry=CapabilityRegistry(),
            )

            with self.assertRaisesRegex(KeyError, "Unknown capability"):
                controller.execute_attempt("missing", attempt_id="attempt-001")

            self.assertEqual(controller.list_attempts(), ())
            self.assertEqual(controller.manifest.budget.attempts, 0)

    def test_attempt_output_ref_selects_one_declared_domain_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def handler(*, context: CapabilityContext) -> CapabilityResult:
                result = context.store.write_json(
                    "results.json",
                    {"score": 1.0},
                    kind="experiment_result",
                    schema="results.v1",
                )
                alternate = context.store.write_json(
                    "results-summary.json",
                    {"score": 1.0},
                    kind="experiment_result",
                    schema="results-summary.v1",
                )
                context.store.write_text(
                    "execution/stdout.txt",
                    "score: 1.0\n",
                    kind="execution_log",
                    schema="text.v1",
                )
                return CapabilityResult(
                    status="completed",
                    artifacts=(result, alternate),
                )

            registry.register("experiment", handler)
            controller = SessionController.create(
                tmp,
                session_id="output-ref",
                topic="output ref",
                registry=registry,
            )

            controller.execute_attempt("experiment", attempt_id="attempt-001")
            result_ref = controller.attempt_output_ref(
                "attempt-001",
                kind="experiment_result",
                schema="results.v1",
            )

            self.assertEqual(result_ref.path, "attempts/attempt-001/results.json")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                controller.attempt_output_ref("attempt-001", kind="experiment_result")
            with self.assertRaisesRegex(KeyError, "no output"):
                controller.attempt_output_ref("attempt-001", kind="missing")

    def test_attempt_output_refs_require_a_persisted_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = SessionController.create(
                tmp,
                session_id="missing-handoff",
                topic="missing attempt",
                registry=CapabilityRegistry(),
            )

            with self.assertRaisesRegex(ValueError, "attempt id is required"):
                controller.attempt_output_refs()
            with self.assertRaisesRegex(KeyError, "Unknown attempt"):
                controller.attempt_output_refs("attempt-404")

    def test_old_manifest_shape_keeps_default_recipe(self) -> None:
        manifest = SessionManifest.from_dict(
            {
                "session_id": "legacy-session",
                "topic": "legacy topic",
                "status": "created",
                "budget": {},
            }
        )

        self.assertEqual(manifest.transition_recipe, "research-v1")
        self.assertEqual(manifest.decisions, [])
        self.assertEqual(manifest.revision, 0)
        self.assertEqual(manifest.next_attempt_sequence, 1)
        self.assertEqual(manifest.to_dict()["schema_version"], "session_manifest.v2")

    def test_loaded_v1_manifest_preserves_history_as_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            controller = SessionController.create(
                tmp,
                session_id="legacy-write-boundary",
                topic="legacy manifest",
                registry=registry,
            )
            controller.store.write_json(
                "session_manifest.json",
                {
                    "schema_version": "session_manifest.v1",
                    "session_id": "legacy-write-boundary",
                    "topic": "legacy manifest",
                    "transition_recipe": "retired-custom-recipe",
                    "status": "created",
                    "budget": {},
                    "decisions": [],
                },
                kind="session",
                schema="session_manifest.v1",
            )

            original = (Path(tmp) / "session_manifest.json").read_bytes()
            legacy = SessionController.load(tmp, registry=registry)
            self.assertEqual(legacy.manifest.transition_recipe, "retired-custom-recipe")
            with self.assertRaisesRegex(RuntimeError, "read-only"):
                legacy.save()
            self.assertFalse((Path(tmp) / "attempts").exists())

            self.assertEqual(
                (Path(tmp) / "session_manifest.json").read_bytes(), original,
            )

    def test_session_manifest_keeps_legacy_positional_field_order(self) -> None:
        manifest = SessionManifest(
            "legacy-session",
            "legacy topic",
            None,
            "created",
            None,
            BudgetState(),
            [],
            "created-at",
            "updated-at",
        )

        self.assertEqual(manifest.status, "created")
        self.assertEqual(manifest.created_at, "created-at")
        self.assertEqual(manifest.transition_recipe, "research-v1")

    def test_execute_attempt_leaves_research_sequence_and_decisions_to_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def handler(**_: object) -> CapabilityResult:
                return CapabilityResult(status="completed")

            registry.register("analysis", handler)
            registry.register("search", handler)
            controller = SessionController.create(
                tmp,
                session_id="new-attempt-entry",
                topic="shared attempt kernel",
                profile="full_research",
                registry=registry,
                budget=BudgetState(max_attempts=4, max_no_progress=2),
            )

            # This sequence is not an edge in the legacy fixed recipe. The
            # application may collect more evidence after analysis; Core records
            # physical execution without imposing a second research policy.
            first = controller.execute_attempt("analysis", attempt_id="attempt-001")
            second = controller.execute_attempt("search", attempt_id="attempt-002")

            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "completed")
            self.assertEqual(controller.manifest.decisions, [])
            self.assertEqual(controller.manifest.budget.attempts, 2)
            self.assertEqual(
                [item.status for item in controller.list_attempts()],
                ["completed", "completed"],
            )
            self.assertEqual(controller.manifest.status, "running")

    def test_execute_attempt_persists_failed_physical_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def handler(**_: object) -> CapabilityResult:
                raise RuntimeError("fixture failure")

            registry.register("probe", handler)
            controller = SessionController.create(
                tmp,
                session_id="failed-attempt-entry",
                topic="failed shared attempt",
                registry=registry,
            )

            result = controller.execute_attempt("probe", attempt_id="attempt-001")

            self.assertEqual(result.status, "failed")
            self.assertEqual(controller.manifest.budget.attempts, 1)
            self.assertEqual(controller.list_attempts()[0].status, "failed")
            self.assertEqual(controller.manifest.decisions, [])

    def test_new_entry_allocates_ids_and_requires_explicit_revision_to_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("plan", lambda **_: CapabilityResult(status="completed"))
            registry.register("search", lambda **_: CapabilityResult(status="completed"))
            controller = SessionController.create(
                tmp,
                session_id="lifecycle-entry",
                topic="explicit lifecycle",
                registry=registry,
                budget=BudgetState(max_attempts=4, max_no_progress=2),
            )

            first = controller.execute_attempt("plan")

            self.assertEqual(first.status, "completed")
            self.assertEqual(controller.manifest.current_attempt, "plan-0001")
            self.assertEqual(controller.manifest.next_attempt_sequence, 2)
            self.assertEqual(controller.manifest.status, "running")

            controller.pause("Waiting for the user to confirm the search scope.")
            paused = SessionController.load(tmp, registry=registry)
            self.assertEqual(paused.manifest.status, "paused")
            self.assertEqual(
                paused.manifest.status_reason,
                "Waiting for the user to confirm the search scope.",
            )
            with self.assertRaisesRegex(RuntimeError, "continue_with_revision"):
                paused.execute_attempt("search")

            revision = paused.continue_with_revision("User confirmed the search scope.")
            self.assertEqual(revision, 1)
            self.assertEqual(paused.manifest.status, "running")
            second = paused.execute_attempt("search")
            self.assertEqual(second.status, "completed")
            self.assertEqual(paused.manifest.current_attempt, "search-0002")

            paused.complete("The application validated the requested outputs.")
            restored = SessionController.load(tmp, registry=registry)
            self.assertEqual(restored.manifest.status, "completed")
            self.assertEqual(restored.manifest.revision, 1)
            self.assertEqual(
                restored.manifest.status_reason,
                "The application validated the requested outputs.",
            )

    def test_revision_does_not_reset_an_exhausted_legacy_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            registry.register("probe", lambda **_: CapabilityResult(status="completed"))
            controller = SessionController.create(
                tmp,
                session_id="lifecycle-budget",
                topic="budget preserving revision",
                registry=registry,
                budget=BudgetState(max_attempts=1, max_no_progress=2),
            )

            controller.execute_attempt("probe")
            controller.complete()

            with self.assertRaisesRegex(RuntimeError, "cannot reset the budget"):
                controller.continue_with_revision("Try again with a changed question.")

            self.assertEqual(controller.manifest.budget.attempts, 1)
            self.assertEqual(controller.manifest.status, "completed")

    def test_explicit_recovery_can_use_remaining_attempts_after_no_progress_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def broken(**_: object) -> CapabilityResult:
                return CapabilityResult(status="failed", diagnostics=("provider unavailable",))

            registry.register("report_write", broken)
            controller = SessionController.create(
                tmp,
                session_id="report-recovery-budget",
                topic="report provider recovery",
                registry=registry,
                budget=BudgetState(max_attempts=3, max_no_progress=1),
            )

            controller.execute_attempt("report_write", attempt_id="report_write-001")
            controller.pause("Report provider was unavailable.")
            with self.assertRaisesRegex(RuntimeError, "cannot reset the budget"):
                controller.continue_with_revision("Retry report")

            revision = controller.continue_with_revision(
                "Retry report with a replacement provider.",
                allow_no_progress_exhausted=True,
            )
            self.assertEqual(revision, 1)
            second_id = controller.allocate_attempt_id(
                "report_write",
                allow_no_progress_exhausted=True,
            )
            second = controller.execute_attempt(
                "report_write",
                attempt_id=second_id,
                allow_no_progress_exhausted=True,
            )
            self.assertEqual(second.status, "failed")
            self.assertEqual(controller.manifest.budget.attempts, 2)
            self.assertEqual(controller.manifest.budget.no_progress, 2)


if __name__ == "__main__":
    unittest.main()
