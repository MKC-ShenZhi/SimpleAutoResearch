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
    def test_json_client_resumes_one_native_task_delivers_and_terminal_load_is_idle(self) -> None:
        content = "# Assessment\nCalibration error 0.12 alone cannot establish robustness.\n"
        client = _JSONClient([
            _reply("write_file", {"path": "workspace/assessment.md", "content": content}),
            _reply("deliver_result", {"summary": "Qualified assessment of the supplied observation.",
                "artifacts": ["workspace/assessment.md"], "unresolved": []}),
        ])
        services = ResearchApplicationServices(llm_client=client,
            budget_limits={"llm_requests": 2, "total_tokens": 1000000},
            config={"research_materials_only": True})
        brief = ResearchBrief(request_text="Assess the observation: calibration error is 0.12.",
            requested_outputs=("assessment",))
        with TemporaryDirectory() as directory, patch.object(LLMClient, "from_env",
                side_effect=AssertionError("Injected services must not use environment clients")):
            root = Path(directory) / "task"
            app = create_research_task(brief, root=root, services=services, model=None)
            self.assertIsInstance(app, ToolApplication)
            identity = app.session.manifest.session_id
            self.assertEqual(app.session.manifest.transition_recipe, ToolApplication.RECIPE)
            self.assertEqual(app.session.manifest.status, "created")
            self.assertEqual(app.session.list_attempts(), ())
            self.assertEqual(client.calls, [])
            self.assertEqual(app.ledger.entries, ())
            saved = (root / "actions.json").read_text(encoding="utf-8")
            self.assertNotIn(client.secret, saved)
            self.assertEqual(app.state["config"]["runtime_dependencies"]["llm_client"]["model"], client.model)

            resumed = ToolApplication.load(root, services=services)
            result = resumed.run(progress=lambda _: None)
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["session_id"], identity)
            self.assertEqual((root / "workspace/assessment.md").read_text(encoding="utf-8"), content)
            delivery = json.loads((root / "delivery.json").read_text(encoding="utf-8"))
            self.assertEqual(delivery["artifacts"], ["workspace/assessment.md"])
            self.assertEqual(len(client.calls), 2)
            tool_messages = [message for message in client.calls[1][1]["messages"]
                             if message["role"] == "tool"]
            self.assertTrue(tool_messages)
            self.assertEqual(json.loads(tool_messages[-1]["content"])["status"], "ok")
            entries = resumed.ledger.entries
            self.assertEqual(len(entries), 2)
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
            self.assertEqual(terminal.run(progress=lambda _: None)["status"], "completed")
            self.assertEqual(ToolApplication.load(root).run(progress=lambda _: None)["status"], "completed")
            self.assertEqual(len(client.calls), 2)
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
            catalog.write_text('version = 1\n[routes]\ndefault = "acceptance"\n'
                '[profiles.acceptance]\napi = "openai_chat"\n'
                'base_url = "https://acceptance.invalid/v1"\nmodel = "fixture-model"\n'
                'api_key_env = "ACCEPTANCE_MODEL_KEY"\ncapabilities = ["text"]\n', encoding="utf-8")
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
                calls.assert_not_called()

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
