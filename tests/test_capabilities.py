from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from simple_ar.core import (
    ArtifactRef as PublicArtifactRef,
    ArtifactStore as PublicArtifactStore,
    CapabilityResult as PublicCapabilityResult,
)
from simple_ar.core.capabilities import (
    ArtifactRef,
    ArtifactStore,
    AttemptManifest,
    CapabilityContext,
    CapabilityResult,
    CapabilityRegistry,
)
from simple_ar.core.session import BudgetState, SessionController


class CapabilityBoundaryTests(unittest.TestCase):
    def test_core_package_exposes_stable_capability_types(self) -> None:
        self.assertIs(PublicArtifactRef, ArtifactRef)
        self.assertIs(PublicArtifactStore, ArtifactStore)
        self.assertIs(PublicCapabilityResult, CapabilityResult)


    def test_store_writes_relative_artifact_refs_without_hash_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            ref = store.write_text(
                "stage/result.txt",
                "done\n",
                kind="result",
                schema="result.v1",
                producer="fixture",
            )

            self.assertEqual(ref.path, "stage/result.txt")
            self.assertEqual(ref.kind, "result")
            self.assertEqual(store.read_text(ref), "done\n")
            self.assertNotIn("sha256", ref.to_dict())
            self.assertNotIn("hash", ref.to_dict())

    def test_store_rejects_paths_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))

            with self.assertRaises(ValueError):
                store.ref("../outside.json")

            with self.assertRaises(ValueError):
                store.new_attempt("../outside")

            with self.assertRaises(ValueError):
                store.new_attempt("nested/attempt")

    def test_missing_artifact_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            ref = store.ref("missing.json", status="missing")

            self.assertFalse(store.exists(ref))
            with self.assertRaises(FileNotFoundError):
                store.require(ref)

    def test_capability_result_has_small_common_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            ref = store.write_json("result.json", {"status": "ok"})
            manifest_ref = store.write_manifest((ref,), metadata={"profile": "fixture"})
            manifest_items = store.read_manifest()
            result = CapabilityResult(
                status="completed",
                artifacts=(ref,),
                diagnostics=("fixture completed",),
                usage={"calls": 0},
                provenance={"producer": "fixture"},
            )

            payload = result.to_dict()
            restored = CapabilityResult.from_dict(payload)

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["artifacts"][0]["path"], "result.json")
            self.assertEqual(payload["usage"]["calls"], 0)
            self.assertEqual(restored, result)
            self.assertEqual(manifest_ref.kind, "manifest")
            self.assertEqual(manifest_items[0].path, "result.json")

    def test_json_write_cleans_temporary_file_when_commit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            with patch(
                "simple_ar.core.artifacts.os.replace",
                side_effect=OSError("simulated commit failure"),
            ):
                with self.assertRaises(OSError):
                    store.write_json("state.json", {"status": "running"})

            self.assertFalse((Path(tmp) / "state.json").exists())
            self.assertEqual(tuple(Path(tmp).glob(".state.json.*.tmp")), ())

    def test_json_write_retries_a_transient_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            import simple_ar.core.artifacts as artifacts

            real_replace = artifacts.os.replace
            calls = 0

            def replace(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls < 3:
                    raise PermissionError("temporary lock")
                real_replace(source, target)

            with patch(
                "simple_ar.core.artifacts.os.replace",
                side_effect=replace,
            ):
                store.write_json("state.json", {"status": "running"})

            self.assertEqual(calls, 3)
            self.assertEqual(store.read_json("state.json"), {"status": "running"})

    def test_capability_result_round_trips_as_an_attempt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(Path(tmp))
            result = CapabilityResult(
                status="failed",
                diagnostics=("fixture failure",),
                usage={"calls": 1},
                provenance={"producer": "fixture"},
            )

            ref = store.write_capability_result(result)
            restored = store.read_capability_result()

            self.assertEqual(ref.kind, "capability_result")
            self.assertEqual(ref.schema, "capability_result.v1")
            self.assertEqual(restored, result)

    def test_attempt_isolated_from_parent_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parent = ArtifactStore(Path(tmp))
            parent.write_text("result.txt", "parent\n")

            child, manifest = parent.new_attempt(
                "attempt-001",
                parent_attempt="attempt-000",
                trigger="repair",
                profile="experiment",
                capability="toy-capability",
                inputs=(parent.ref("result.txt", kind="result"),),
            )
            child.write_text("result.txt", "child\n")

            loaded = child.read_attempt_manifest()

            self.assertEqual(loaded.capability, "toy-capability")
            self.assertEqual(loaded.attempt_id, "attempt-001")
            self.assertEqual(loaded.parent_attempt, "attempt-000")
            self.assertEqual(loaded.trigger, "repair")
            self.assertEqual(loaded.inputs[0].path, "result.txt")
            self.assertEqual(parent.read_text("result.txt"), "parent\n")
            self.assertEqual(child.read_text("result.txt"), "child\n")
            self.assertTrue((Path(tmp) / "attempts" / "attempt-001" / "attempt_manifest.json").is_file())

    def test_registry_runs_explicit_handler_and_lists_names(self) -> None:
        registry = CapabilityRegistry()

        def fixture(value: str) -> CapabilityResult:
            return CapabilityResult(
                status="completed",
                diagnostics=(value,),
                provenance={"producer": "fixture"},
            )

        registry.register("  fixture  ", fixture)

        result = registry.run("fixture", "ok")

        self.assertEqual(registry.names(), ("fixture",))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.diagnostics, ("ok",))

    def test_registry_rejects_duplicates_unknown_names_and_bad_results(self) -> None:
        registry = CapabilityRegistry()

        def fixture() -> CapabilityResult:
            return CapabilityResult(status="completed")

        registry.register("fixture", fixture)
        with self.assertRaises(ValueError):
            registry.register("fixture", fixture)
        with self.assertRaises(KeyError):
            registry.resolve("missing")

        with self.assertRaises(TypeError):
            registry.register("not-callable", object())  # type: ignore[arg-type]
        self.assertEqual(registry.names(), ("fixture",))

        registry.register("bad", lambda: "not a result")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            registry.run("bad")

    def test_session_controller_resolves_registered_inputs_from_session_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            source_store = ArtifactStore(Path(tmp))
            source_ref = source_store.write_json("source/input.json", {"value": 7}, kind="input")

            def copy_input(*, context: CapabilityContext) -> CapabilityResult:
                payload = context.read_input_json(context.inputs[0])
                output = context.store.write_json("copied.json", payload, kind="result")
                return CapabilityResult(status="completed", artifacts=(output,))

            registry.register("copy-input", copy_input)
            controller = SessionController.create(
                tmp,
                session_id="session-inputs",
                topic="input resolution",
                registry=registry,
            )
            result = controller.execute_attempt(
                "copy-input",
                attempt_id="attempt-001",
                inputs=(source_ref,),
                progressed=False,
            )

            self.assertEqual(result.status, "completed")
            # Input resolution is also the public persistence integration;
            # commit permutations belong to SessionTransitionTests.
            loaded = SessionController.load(tmp, registry=registry)
            self.assertIs(loaded.store.read_json(
                "attempts/attempt-001/capability_result.json")["session_progressed"], False)
            persisted = loaded.store.read_capability_result("attempts/attempt-001/capability_result.json")
            self.assertEqual(persisted.artifacts[0].path, "copied.json")
            self.assertEqual(
                loaded.store.read_json("attempts/attempt-001/copied.json"),
                {"value": 7},
            )
            loaded.complete("Application accepted the result.")
            self.assertEqual(loaded.reconcile_attempt(), result)
            self.assertEqual(loaded.manifest.status, "completed")

    def test_capability_context_rejects_unregistered_input(self) -> None:
        context = CapabilityContext(
            store=ArtifactStore(Path(".")),
            attempt=AttemptManifest(attempt_id="attempt-001"),
        )
        ref = ArtifactRef(path="missing.json")

        with self.assertRaises(ValueError):
            context.resolve_input(ref)

    def test_session_controller_blocks_repeated_no_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            def broken(*, context: CapabilityContext) -> CapabilityResult:
                return CapabilityResult(status="failed", diagnostics=("same failure",))

            registry.register("broken", broken)
            controller = SessionController.create(
                tmp,
                session_id="session-002",
                topic="offline failure",
                registry=registry,
                budget=BudgetState(max_attempts=5, max_no_progress=2),
            )

            first = controller.execute_attempt(
                "broken",
                attempt_id="attempt-001",
                trigger="initial",
            )
            second = controller.execute_attempt(
                "broken",
                attempt_id="attempt-002",
                trigger="repair",
            )

            self.assertEqual(first.status, "failed")
            self.assertEqual(second.status, "failed")
            self.assertEqual(second.diagnostics, ("same failure",))
            with self.assertRaisesRegex(RuntimeError, "budget is exhausted"):
                controller.execute_attempt("broken", attempt_id="attempt-003")
            self.assertEqual(controller.manifest.status, "blocked")
            self.assertEqual(len(controller.list_attempts()), 2)
            self.assertEqual(controller.manifest.budget.no_progress, 2)
            self.assertEqual(controller.manifest.decisions, [])
            self.assertTrue((Path(tmp) / "attempts" / "attempt-001" / "attempt_manifest.json").is_file())
            self.assertTrue((Path(tmp) / "attempts" / "attempt-002" / "attempt_manifest.json").is_file())

    def test_session_controller_does_not_implicitly_retry_or_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()
            def partial(*, context: CapabilityContext) -> CapabilityResult:
                return CapabilityResult(status="partial")

            registry.register("partial", partial)
            controller = SessionController.create(
                tmp,
                session_id="session-003",
                topic="offline partial",
                registry=registry,
                budget=BudgetState(max_attempts=2, max_no_progress=2),
            )

            controller.execute_attempt("partial", attempt_id="attempt-001", trigger="initial")

            self.assertEqual(controller.manifest.status, "running")
            with self.assertRaises(FileExistsError):
                controller.execute_attempt("partial", attempt_id="attempt-001", trigger="revise")
            self.assertEqual(controller.manifest.budget.attempts, 1)

    def test_session_controller_can_branch_from_an_explicit_completed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = CapabilityRegistry()

            def fixture(*, context: CapabilityContext) -> CapabilityResult:
                output = context.store.write_text(
                    "result.txt",
                    context.attempt.attempt_id,
                    kind="result",
                )
                if context.attempt.attempt_id == "attempt-002":
                    return CapabilityResult(
                        status="failed",
                        artifacts=(output,),
                        diagnostics=("fixture branch source failed",),
                    )
                return CapabilityResult(status="completed", artifacts=(output,))

            registry.register("plan", fixture)
            registry.register("search", fixture)
            controller = SessionController.create(
                tmp,
                session_id="branching-session",
                topic="explicit branch",
                registry=registry,
            )

            first = controller.execute_attempt(
                "plan",
                attempt_id="attempt-001",
            )
            second = controller.execute_attempt(
                "search",
                attempt_id="attempt-002",
            )
            branch = controller.execute_attempt(
                "search",
                attempt_id="attempt-003",
                parent_attempt_id="attempt-001",
            )

            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "failed")
            self.assertEqual(branch.status, "completed")
            self.assertEqual(
                controller.store.read_attempt_manifest(
                    "attempts/attempt-003/attempt_manifest.json"
                ).parent_attempt,
                "attempt-001",
            )
            self.assertEqual(
                controller.store.read_text("attempts/attempt-002/result.txt"),
                "attempt-002",
            )
            self.assertEqual(
                controller.store.read_text("attempts/attempt-003/result.txt"),
                "attempt-003",
            )
            self.assertEqual(
                [item.attempt_id for item in controller.attempt_lineage("attempt-003")],
                ["attempt-001", "attempt-003"],
            )
            self.assertEqual(len(controller.list_attempts()), 3)

    def test_explicit_branch_parent_is_validated_before_handler_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            calls: list[str] = []
            registry = CapabilityRegistry()

            def fixture(*, context: CapabilityContext) -> CapabilityResult:
                calls.append(context.attempt.attempt_id)
                return CapabilityResult(status="completed")

            registry.register("plan", fixture)
            registry.register("search", fixture)
            controller = SessionController.create(
                tmp,
                session_id="branch-parent-validation",
                topic="branch parent validation",
                registry=registry,
            )
            controller.execute_attempt(
                "plan",
                attempt_id="attempt-001",
            )

            with self.assertRaisesRegex(KeyError, "Unknown parent attempt"):
                controller.execute_attempt(
                    "search",
                    attempt_id="attempt-002",
                    parent_attempt_id="missing-parent",
                )

            self.assertEqual(calls, ["attempt-001"])
            self.assertEqual(
                [item.attempt_id for item in controller.list_attempts()],
                ["attempt-001"],
            )

    def test_attempt_lineage_reports_broken_persisted_parent_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            controller = SessionController.create(
                tmp,
                session_id="broken-lineage",
                topic="broken lineage",
                registry=CapabilityRegistry(),
            )
            controller.store.new_attempt(
                "attempt-001",
                parent_attempt="missing-parent",
            )
            with self.assertRaisesRegex(ValueError, "references missing parent"):
                controller.attempt_lineage("attempt-001")

            controller.store.new_attempt(
                "attempt-002",
                parent_attempt="attempt-001",
            )
            controller.store.write_attempt_manifest(
                replace(
                    controller.store.read_attempt_manifest(
                        "attempts/attempt-001/attempt_manifest.json"
                    ),
                    parent_attempt="attempt-002",
                ),
                path="attempts/attempt-001/attempt_manifest.json",
            )
            with self.assertRaisesRegex(ValueError, "contains a cycle"):
                controller.attempt_lineage("attempt-002")


if __name__ == "__main__":
    unittest.main()
